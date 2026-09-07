"""Store age from the Shopify shop ID.

Shopify assigns shop IDs sequentially, so the numeric ID orders stores by creation date. The ID
and the myshopify handle are in every storefront's HTML and never change:

  <meta name="shopify-digital-wallet" content="/SHOP_ID/digital_wallets/dialog">
  "shopId":SHOP_ID             (Trekkie / ShopifyAnalytics config, shopify-features JSON)
  Shopify.shop = "x.myshopify.com"

calibration/shop_ids.csv (shop_id,created_date) holds a handful of stores whose creation dates are
known. Every store's created date is interpolated from that table: linear between the two
neighbouring calibration points, extrapolated with the end segment's slope outside the range.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from . import config, shopify

log = logging.getLogger("earlyscale.store_age")

CALIBRATION_PATH = Path(os.environ.get("CALIBRATION_PATH") or (Path(config.__file__).resolve().parent.parent / "calibration" / "shop_ids.csv"))

WALLET_RE = re.compile(r'<meta\s+name="shopify-digital-wallet"\s+content="/(\d+)/digital_wallets/dialog"', re.I)
WALLET_RE2 = re.compile(r'content="/(\d+)/digital_wallets/dialog"[^>]*name="shopify-digital-wallet"', re.I)
SHOPID_RES = [
    re.compile(r'"shopId"\s*:\s*"?(\d{3,})"?'),
    re.compile(r'\bshopId\s*[:=]\s*"?(\d{3,})"?'),
    re.compile(r'"shop_id"\s*:\s*"?(\d{3,})"?'),
    re.compile(r'\bshop_id\s*[:=]\s*"?(\d{3,})"?'),
]
MYSHOPIFY_RES = [
    re.compile(r'Shopify\.shop\s*=\s*"([a-z0-9][a-z0-9\-]*\.myshopify\.com)"', re.I),
    re.compile(r'"myshopifyDomain"\s*:\s*"([a-z0-9][a-z0-9\-]*\.myshopify\.com)"', re.I),
    re.compile(r'([a-z0-9][a-z0-9\-]*\.myshopify\.com)', re.I),
]


# ---------------------------------------------------------------- extraction (pure)

def extract_identity(html: str) -> dict:
    """{shop_id, myshopify, source} from a storefront page. shop_id is None when not found."""
    html = html or ""
    shop_id, source = None, None
    m = WALLET_RE.search(html) or WALLET_RE2.search(html)
    if m:
        shop_id, source = int(m.group(1)), "digital-wallet meta"
    else:
        for rx in SHOPID_RES:
            m = rx.search(html)
            if m:
                shop_id, source = int(m.group(1)), "shopId in analytics config"
                break
    myshopify = None
    for rx in MYSHOPIFY_RES:
        m = rx.search(html)
        if m:
            myshopify = m.group(1).lower()
            break
    return {"shop_id": shop_id, "myshopify": myshopify, "source": source}


def fetch_identity(store_domain: str, session: requests.Session | None = None) -> dict:
    """Fetch the homepage (and /collections/all as a fallback) and extract the identity.
    Returns {shop_id, myshopify, source, http, error}."""
    session = session or shopify.make_session()
    try:
        base = shopify.resolve_base_url(session, store_domain)
    except shopify.StoreFetchError as e:
        return {"shop_id": None, "myshopify": None, "source": None, "http": None, "error": str(e)[:200]}
    out = {"shop_id": None, "myshopify": None, "source": None, "http": None, "error": None}
    for path in ("/", "/collections/all", "/password"):
        try:
            r = session.get(base + path, timeout=config.REQUEST_TIMEOUT,
                            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        except requests.RequestException as e:
            out["error"] = f"{type(e).__name__}: {e}"[:200]
            continue
        out["http"] = r.status_code
        ident = extract_identity(r.text)
        if ident["shop_id"]:
            return {**ident, "http": r.status_code, "error": None}
        if ident["myshopify"] and not out["myshopify"]:
            out["myshopify"] = ident["myshopify"]
    if out["error"] is None:
        out["error"] = "no shop id in HTML"
    return out


# ---------------------------------------------------------------- calibration + interpolation (pure)

@dataclass
class Calibration:
    points: list[tuple[int, date]]   # sorted by shop_id
    path: Path | None = None
    manual_ids: set = None           # shop ids with a verified CSV row
    auto_ids: set = None             # shop ids whose point came from a first product date

    @property
    def ok(self) -> bool:
        return len(self.points) >= 2

    def kind(self, shop_id: int) -> str:
        if self.manual_ids and shop_id in self.manual_ids:
            return "verified"
        if self.auto_ids and shop_id in self.auto_ids:
            return "first product"
        return ""


def load_calibration(path: Path | None = None) -> Calibration:
    path = path or CALIBRATION_PATH
    pts: dict[int, date] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sid = (row.get("shop_id") or "").strip()
                d = (row.get("created_date") or "").strip()
                if not sid or not d or sid.startswith("#"):
                    continue
                try:
                    pts[int(sid)] = date.fromisoformat(d[:10])
                except ValueError:
                    log.warning("calibration: bad row %s", row)
    return Calibration(sorted(pts.items()), path)


def first_product_dates(conn: sqlite3.Connection) -> dict[int, date]:
    """{shop_id: earliest product created_at seen for that store}. A store exists before its first
    product, so this is a 'no later than' date for the shop id."""
    out: dict[int, date] = {}
    for r in conn.execute("""SELECT s.shop_id, MIN(p.created_at) AS first FROM stores s JOIN products_daily p ON p.store_id = s.id
                             WHERE s.shop_id IS NOT NULL AND p.created_at IS NOT NULL GROUP BY s.shop_id"""):
        try:
            out[int(r["shop_id"])] = date.fromisoformat(str(r["first"])[:10])
        except ValueError:
            continue
    return out


def lower_envelope(observations: dict[int, date]) -> list[tuple[int, date]]:
    """Shop ids grow with creation date, so a store cannot be younger than any higher-id store.
    Walking ids from high to low and keeping the running minimum date turns 'no later than'
    observations (first product dates) into a monotone curve."""
    pts = sorted(observations.items())
    out: list[tuple[int, date]] = []
    best: date | None = None
    for sid, d in reversed(pts):
        best = d if best is None or d < best else best
        out.append((sid, best))
    out.reverse()
    return out


def build_calibration(conn: sqlite3.Connection | None, path: Path | None = None) -> Calibration:
    """Verified rows from the CSV plus automatic points from every store's first product date,
    combined through the lower envelope. CSV rows are true dates, so they are exact for their own
    store and bound everything below them."""
    manual = load_calibration(path)
    obs: dict[int, date] = {}
    if conn is not None:
        obs.update(first_product_dates(conn))
    obs.update(dict(manual.points))     # verified rows win over the observation for the same id
    cal = Calibration(lower_envelope(obs), manual.path)
    cal.manual_ids = {sid for sid, _ in manual.points}
    cal.auto_ids = set(obs) - cal.manual_ids
    return cal


def estimate_created(shop_id: int | None, cal: Calibration) -> dict:
    """{created: date|None, method, lower: (id, date)|None, upper: (id, date)|None}."""
    out = {"created": None, "method": None, "lower": None, "upper": None}
    if shop_id is None or not cal.ok:
        out["method"] = "no shop id" if shop_id is None else "calibration needs >= 2 rows"
        return out
    pts = cal.points
    for sid, d in pts:
        if sid == shop_id:
            k = cal.kind(sid)
            method = {"verified": "verified row", "first product": "own first product (no later than)"}.get(k, "exact")
            return {"created": d, "method": method, "lower": (sid, d), "upper": (sid, d)}
    if shop_id < pts[0][0]:
        (a_id, a_d), (b_id, b_d) = pts[0], pts[1]
        method = "extrapolated (below range)"
    elif shop_id > pts[-1][0]:
        (a_id, a_d), (b_id, b_d) = pts[-2], pts[-1]
        method = "extrapolated (above range)"
    else:
        i = next(k for k in range(len(pts) - 1) if pts[k][0] <= shop_id <= pts[k + 1][0])
        (a_id, a_d), (b_id, b_d) = pts[i], pts[i + 1]
        method = "interpolated"
    days_per_id = (b_d - a_d).days / (b_id - a_id) if b_id != a_id else 0
    est = a_d + timedelta(days=round((shop_id - a_id) * days_per_id))
    out.update({"created": est, "method": method, "lower": (a_id, a_d), "upper": (b_id, b_d)})
    return out


# ---------------------------------------------------------------- DB

def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record_identity(conn: sqlite3.Connection, store_id: int, ident: dict) -> None:
    conn.execute("""UPDATE stores SET shop_id = COALESCE(?, shop_id), myshopify = COALESCE(?, myshopify),
                      shop_id_source = COALESCE(?, shop_id_source), shop_id_checked_at = ?, shop_id_error = ?
                    WHERE id = ?""",
                 (ident.get("shop_id"), ident.get("myshopify"), ident.get("source"), _utcnow(),
                  None if ident.get("shop_id") else (ident.get("error") or "no shop id"), store_id))
    conn.commit()


def ensure_identity(conn: sqlite3.Connection, store_id: int, store_domain: str, session=None,
                    refresh: bool = False, fetch=fetch_identity) -> dict:
    """Fetch the identity once (it never changes); retry on later runs only while it is missing."""
    row = conn.execute("SELECT shop_id, myshopify, shop_id_source FROM stores WHERE id = ?", (store_id,)).fetchone()
    if row and row["shop_id"] and not refresh:
        return {"shop_id": row["shop_id"], "myshopify": row["myshopify"], "source": row["shop_id_source"], "cached": True}
    ident = fetch(store_domain, session)
    record_identity(conn, store_id, ident)
    return {**ident, "cached": False}


def refresh_estimates(conn: sqlite3.Connection, cal: Calibration | None = None) -> dict:
    """Recompute store_created_est for every store from the calibration (CSV + first product dates)."""
    cal = cal or build_calibration(conn)
    n = est = 0
    for r in conn.execute("SELECT id, shop_id FROM stores").fetchall():
        n += 1
        e = estimate_created(r["shop_id"], cal)
        conn.execute("UPDATE stores SET store_created_est = ?, store_created_method = ? WHERE id = ?",
                     (e["created"].isoformat() if e["created"] else None, e["method"], r["id"]))
        est += 1 if e["created"] else 0
    conn.commit()
    return {"stores": n, "estimated": est, "calibration_rows": len(cal.points)}


def age_days(created_iso: str | None, as_of: str | None = None) -> int | None:
    if not created_iso:
        return None
    t = date.fromisoformat(as_of) if as_of else date.today()
    try:
        return (t - date.fromisoformat(created_iso[:10])).days
    except ValueError:
        return None


def missing_ids(conn: sqlite3.Connection, store_domains: set[str] | None = None) -> list[dict]:
    rows = [dict(r) for r in conn.execute("SELECT store_domain, shop_id_error, shop_id_checked_at, myshopify FROM stores WHERE shop_id IS NULL")]
    if store_domains is not None:
        rows = [r for r in rows if r["store_domain"] in store_domains]
    return rows


def store_age_rows(conn: sqlite3.Connection, as_of: str | None = None, cal: Calibration | None = None,
                   store_domains: set[str] | None = None) -> list[list]:
    """Store Age tab: newest first. Columns match sheets.STORE_AGE_HEADERS."""
    cal = cal or build_calibration(conn)
    firsts = first_product_dates(conn)
    rows = []
    for s in conn.execute("SELECT * FROM stores ORDER BY store_domain"):
        if store_domains is not None and s["store_domain"] not in store_domains:
            continue
        e = estimate_created(s["shop_id"], cal)
        created = e["created"].isoformat() if e["created"] else (s["store_created_est"] or "")
        first = firsts.get(s["shop_id"]) if s["shop_id"] else None
        prods = conn.execute("SELECT COUNT(*) FROM products_daily WHERE store_id = ? AND snapshot_date = (SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ?)",
                             (s["id"], s["id"])).fetchone()[0]
        first_snap = conn.execute("SELECT MIN(snapshot_date) FROM products_daily WHERE store_id = ?", (s["id"],)).fetchone()[0] or ""
        def pt(x):
            return "" if not x else f"{x[0]} = {x[1]} ({cal.kind(x[0]) or 'envelope'})"
        rows.append([
            s["store_domain"], "" if s["shop_id"] is None else s["shop_id"], s["myshopify"] or "", created,
            "" if not created else age_days(created, as_of), e["method"] or "",
            pt(e["lower"]), pt(e["upper"]), first.isoformat() if first else "",
            prods, first_snap, s["shop_id_source"] or "", s["shop_id_error"] or "",
        ])
    rows.sort(key=lambda r: (0 if r[3] else 1, r[3] and -int(r[3].replace("-", "")) or 0, r[0]))
    return rows
