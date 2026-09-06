"""Store-side demand signal: inventory-delta sales tracking.

  select_hero_variants   top-N products by collection rank + anything published in the last 30 days,
                         minus widgets / digital goods / $0 items
  probe_variant          POST /cart/add.js quantity 9999 with a fresh session; parse Shopify's 422
                         ("You can only add N ..." / "sold out") into a stock level
  theme_inventory        fallback: read inventory_quantity the theme exposes in the product page JSON
  record_reading         one row per hero variant per day, with the rung of the fallback chain used
  compute_sales          units_sold vs the previous available reading (restocks excluded), 7-day and
                         prior-7-day units/day, week-over-week ratio
  run_alerts             rules 9-11
  inventory_for_signals  per-product numbers for the Signals tab

The probe never touches checkout and never creates an order: a fresh session per probe
means the 9999-quantity cart is orphaned, and when an add succeeds we clear it anyway.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import requests

from . import config, shopify

log = logging.getLogger("earlyscale.inventory")

RULES = {
    9: "product <30d old: units/day up 2x week over week and >= 5/day",
    10: "product at >= 50 units/day that was not in the store's top 10 last week",
    11: "product sold out within 14 days of publish",
}

EXCLUDE_RE = re.compile(
    r"(shipping[-\s_]?protection|package[-\s_]?protection|route[-\s_]?package|order[-\s_]?protection|"
    r"\bwarranty\b|\binsurance\b|gift[-\s_]?card|\bmembership\b|\bmembers?[-\s_]?only\b|\be-?book\b|"
    r"\bdigital\b|\bdownload(able)?\b|\bpdf\b|\btip[-\s_]?jar\b|^tips?$|priority[-\s_]?processing|"
    r"\bdonation\b|\bcourse\b)", re.I)
BUNDLE_RE = re.compile(r"\b(bundle|kit|set|pack|stack|combo)\b", re.I)

# Shopify /cart/add.js 422 descriptions vary by locale and version; the number is what matters.
ONLY_N_RES = [
    re.compile(r"only\s+add\s+(\d[\d,]*)", re.I),            # "You can only add 43 X to the cart."
    re.compile(r"only\s+(\d[\d,]*)\s+items?\s+were\s+added", re.I),
    re.compile(r"only\s+(\d[\d,]*)", re.I),
    re.compile(r"all\s+(\d[\d,]*)\s+.*?(?:are|is)\s+in\s+your\s+cart", re.I),   # "All 12 X are in your cart."
    re.compile(r"(\d[\d,]*)\s+(?:items?\s+)?(?:left|available|remaining)", re.I),
]
SOLD_OUT_RE = re.compile(r"sold\s*out|out\s+of\s+stock|no\s+longer\s+available|not\s+available", re.I)


# ---------------------------------------------------------------- hero selection (pure)

def is_excluded(title: str | None, handle: str | None, product_type: str | None, tags) -> bool:
    text = " ".join(str(x or "") for x in (title, handle, product_type,
                                            " ".join(tags) if isinstance(tags, list) else tags))
    return bool(EXCLUDE_RE.search(text))


def select_heroes(products: list[dict], as_of: date, top_n: int = 15, new_days: int = 30,
                  max_variants: int | None = None) -> list[dict]:
    """products: rows with product_id, handle, title, product_type, tags, published_at,
    collection_position, variants: [{variant_id, title, price, available}]. Returns variant dicts
    with a `role` (new | rank), newest products first, then by rank."""
    from . import deltas
    max_variants = config.INVENTORY_MAX_VARIANTS if max_variants is None else max_variants
    chosen: dict[int, dict] = {}
    ranked = sorted((p for p in products if p.get("collection_position") is not None),
                    key=lambda p: p["collection_position"])[:top_n]
    for p in products:
        pub = deltas.parse_ts(p.get("published_at"))
        if pub and (as_of - pub.date()).days <= new_days:
            chosen.setdefault(p["product_id"], dict(p, role="new"))
    for p in ranked:
        chosen.setdefault(p["product_id"], dict(p, role="rank"))
    out: list[dict] = []
    order = sorted(chosen.values(), key=lambda p: (p["role"] != "new", p.get("collection_position") if p.get("collection_position") is not None else 9999))
    for p in order:
        if is_excluded(p.get("title"), p.get("handle"), p.get("product_type"), p.get("tags")):
            continue
        for v in p.get("variants") or []:
            if v.get("price") is None or float(v["price"]) <= 0:
                continue
            out.append({"product_id": p["product_id"], "variant_id": v["variant_id"], "handle": p["handle"],
                        "title": p.get("title"), "variant_title": v.get("title"), "price": float(v["price"]),
                        "available": bool(v.get("available")), "role": p["role"],
                        "bundle_like": bool(BUNDLE_RE.search(f"{p.get('title') or ''} {p.get('handle') or ''}"))})
    return out[:max_variants]


# ---------------------------------------------------------------- probes (network)

def parse_cart_error(text: str) -> tuple[str, int | None]:
    """('count', N) | ('sold_out', 0) | ('unknown', None) from a /cart/add.js error body."""
    try:
        data = json.loads(text)
        msg = " ".join(str(data.get(k) or "") for k in ("description", "message", "error", "errors"))
    except ValueError:
        msg = text
    for rx in ONLY_N_RES:
        m = rx.search(msg)
        if m:
            return "count", int(m.group(1).replace(",", ""))
    if SOLD_OUT_RE.search(msg):
        return "sold_out", 0
    return "unknown", None


def _cart_message(text: str) -> str:
    """The human sentence out of a /cart/add.js error body (kept as raw_message for auditing)."""
    try:
        data = json.loads(text)
        return " ".join(str(data.get(k)) for k in ("description", "message") if data.get(k))[:200] or text[:200]
    except ValueError:
        return text[:200]


def fresh_session(base_url: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(config.BROWSER_HEADERS)
    s.headers.update({"Accept": "application/json, text/javascript, */*; q=0.01",
                      "X-Requested-With": "XMLHttpRequest", "Origin": base_url, "Referer": base_url + "/"})
    return s


def _diag_headers(r: requests.Response) -> str:
    keys = ("server", "cf-mitigated", "cf-ray", "x-shopify-stage", "x-sorting-hat-shopid", "location", "content-type", "retry-after")
    return " ".join(f"{k}={r.headers[k][:60]}" for k in keys if r.headers.get(k))


def probe_variant(base_url: str, variant_id: int, handle: str | None = None,
                  session: requests.Session | None = None, warm_up: bool = True) -> dict:
    """One /cart/add.js probe. Returns {status, stock, message, http}.
    status: count | sold_out | untracked | not_found | blocked | error | unknown"""
    s = session or fresh_session(base_url)
    if handle:
        s.headers["Referer"] = f"{base_url}/products/{handle}"
        if warm_up:
            # behave like a shopper: open the product page first so the session carries the storefront cookies
            try:
                s.get(f"{base_url}/products/{handle}", timeout=config.REQUEST_TIMEOUT,
                      headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "X-Requested-With": None})
            except requests.RequestException:
                pass
    url = f"{base_url}/cart/add.js"
    try:
        r = s.post(url, json={"items": [{"id": int(variant_id), "quantity": config.INVENTORY_PROBE_QTY}]},
                   timeout=config.REQUEST_TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        return {"status": "error", "stock": None, "message": f"{type(e).__name__}: {e}"[:200], "http": None}
    text = r.text or ""
    hdr = _diag_headers(r)
    if r.status_code == 200:
        # add succeeded: inventory is not tracked (or "continue selling when out of stock"); drop the cart
        try:
            s.post(f"{base_url}/cart/clear.js", timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException:
            pass
        return {"status": "untracked", "stock": None, "message": "add succeeded (no inventory limit)", "http": 200}
    if r.status_code == 422:
        kind, n = parse_cart_error(text)
        msg = _cart_message(text)
        if kind == "count":
            return {"status": "count", "stock": n, "message": msg, "http": 422}
        if kind == "sold_out":
            return {"status": "sold_out", "stock": 0, "message": msg, "http": 422}
        return {"status": "unknown", "stock": None, "message": msg, "http": 422}
    if r.status_code == 404:
        return {"status": "not_found", "stock": None, "message": f"HTTP 404 {text[:120]} | {hdr}", "http": 404}
    if r.status_code in (401, 403, 429, 430, 503) or "captcha" in text.lower() or "challenge" in text.lower():
        return {"status": "blocked", "stock": None, "message": f"HTTP {r.status_code} {_squash(text)[:120]} | {hdr}", "http": r.status_code}
    if 300 <= r.status_code < 400:
        return {"status": "blocked", "stock": None, "message": f"HTTP {r.status_code} redirect | {hdr}", "http": r.status_code}
    return {"status": "error", "stock": None, "message": f"HTTP {r.status_code} {_squash(text)[:120]} | {hdr}", "http": r.status_code}


def _squash(text: str) -> str:
    """HTML body -> its title/first words, so a raw_message stays readable."""
    if "<" in text[:200]:
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        body = re.sub(r"<[^>]+>", " ", text)
        return ("title=" + m.group(1).strip() + " " if m else "") + re.sub(r"\s+", " ", body).strip()
    return re.sub(r"\s+", " ", text).strip()


INV_NEAR_ID_RE_TMPL = r'"id"\s*:\s*{vid}\b.{{0,2500}}?"inventory_quantity"\s*:\s*(-?\d+)'
INV_BEFORE_ID_RE_TMPL = r'"inventory_quantity"\s*:\s*(-?\d+).{{0,2500}}?"id"\s*:\s*{vid}\b'
DATA_ATTR_RE_TMPL = r'data-variant-id="{vid}"[^>]*data-(?:inventory-)?quantity="(-?\d+)"'


def theme_inventory_from_html(html: str, variant_id: int) -> int | None:
    """inventory_quantity for one variant if the theme embeds it (only-X-left badges do)."""
    for tmpl in (INV_NEAR_ID_RE_TMPL, INV_BEFORE_ID_RE_TMPL, DATA_ATTR_RE_TMPL):
        m = re.search(tmpl.format(vid=variant_id), html, re.S)
        if m:
            return int(m.group(1))
    return None


def fetch_product_page(base_url: str, handle: str, session: requests.Session | None = None) -> str:
    s = session or shopify.make_session()
    r = s.get(f"{base_url}/products/{handle}", timeout=config.REQUEST_TIMEOUT,
              headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    return r.text if r.status_code == 200 else ""


def component_variant_ids(html: str, own_variant_ids: set[int], known_variant_ids: set[int]) -> list[int]:
    """Variant ids on a bundle page that belong to *other* known products (bundle components)."""
    ids = {int(m) for m in re.findall(r'"(?:id|variant_id|variantId)"\s*:\s*"?(\d{1,16})"?', html)}
    ids |= {int(m) for m in re.findall(r'data-variant-id="(\d{1,16})"', html)}
    return sorted(i for i in ids if i in known_variant_ids and i not in own_variant_ids)


def _pause() -> None:
    time.sleep(random.uniform(config.INVENTORY_WAIT_MIN, config.INVENTORY_WAIT_MAX))


# ---------------------------------------------------------------- DB

def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def latest_products(conn: sqlite3.Connection, store_id: int) -> tuple[str | None, list[dict]]:
    d = conn.execute("SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ? AND COALESCE(unlisted, 0) = 0",
                     (store_id,)).fetchone()[0]
    if not d:
        return None, []
    products = {r["product_id"]: dict(r, tags=json.loads(r["tags"] or "[]"), variants=[]) for r in conn.execute(
        """SELECT product_id, handle, title, product_type, tags, published_at, collection_position
           FROM products_daily WHERE store_id = ? AND snapshot_date = ?""", (store_id, d))}
    for v in conn.execute("SELECT product_id, variant_id, title, price, available FROM variants_daily WHERE store_id = ? AND snapshot_date = ?",
                          (store_id, d)):
        if v["product_id"] in products:
            products[v["product_id"]]["variants"].append(dict(v))
    return d, list(products.values())


def refresh_hero_variants(conn: sqlite3.Connection, store_id: int, today: str) -> list[dict]:
    """(Re)select heroes from the latest snapshot; keep probe state for variants already known."""
    _, products = latest_products(conn, store_id)
    heroes = select_heroes(products, date.fromisoformat(today))
    for h in heroes:
        conn.execute(
            """INSERT INTO hero_variants (store_id, variant_id, product_id, handle, variant_title, price, role, bundle_like, selected_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(store_id, variant_id) DO UPDATE SET product_id = excluded.product_id, handle = excluded.handle,
                 variant_title = excluded.variant_title, price = excluded.price, role = excluded.role,
                 bundle_like = excluded.bundle_like, last_selected_at = excluded.selected_at""",
            (store_id, h["variant_id"], h["product_id"], h["handle"], h["variant_title"], h["price"], h["role"],
             1 if h["bundle_like"] else 0, today))
    conn.commit()
    return heroes


def record_reading(conn: sqlite3.Connection, store_id: int, today: str, variant_id: int, product_id: int,
                   stock: int | None, source: str, message: str | None) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO inventory_daily (snapshot_date, store_id, variant_id, product_id, stock_level, signal_source,
             raw_message, probed_at) VALUES (?,?,?,?,?,?,?,?)""",
        (today, store_id, variant_id, product_id, stock, source, (message or "")[:300], _utcnow()))


def _resolve(store_domain: str) -> str:
    """Origin that actually serves the store (apex -> www etc.), falling back to the literal domain."""
    try:
        return shopify.resolve_base_url(shopify.make_session(), store_domain)
    except shopify.StoreFetchError as e:
        base = shopify.base_url(store_domain)
        log.warning("%s: could not resolve host (%s); probing %s", store_domain, e, base)
        return base


def probe_store(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str,
                probe=probe_variant, page_fetch=fetch_product_page, pause=_pause, resolve=_resolve) -> dict:
    """Walk the fallback chain for every hero variant of one store that has not been probed today."""
    base = resolve(store_domain)
    heroes = refresh_hero_variants(conn, store_id, today)
    known_vids = {r[0] for r in conn.execute("SELECT DISTINCT variant_id FROM variants_daily WHERE store_id = ?", (store_id,))}
    counts = {"cart_probe": 0, "theme_inventory": 0, "ads_only": 0, "skipped": 0, "blocked": 0, "components": 0}
    state = {r["variant_id"]: dict(r) for r in conn.execute("SELECT * FROM hero_variants WHERE store_id = ?", (store_id,))}
    todo = [h for h in heroes]
    seen = set()
    blocked_run = 0
    while todo:
        h = todo.pop(0)
        vid = h["variant_id"]
        if vid in seen:
            continue
        seen.add(vid)
        st = state.get(vid, {})
        if st.get("last_probe_date") == today:
            counts["skipped"] += 1
            continue
        if blocked_run >= config.INVENTORY_MAX_BLOCKED:
            # the store is refusing cart probes today; do not hammer it, record the rest as blocked
            record_reading(conn, store_id, today, vid, h["product_id"], None, "blocked",
                           f"not probed: {blocked_run} consecutive blocks on this store today")
            counts["blocked"] += 1
            continue
        if st.get("inventory_tracked") == 0:
            # ads_only: decided earlier, do not probe again
            record_reading(conn, store_id, today, vid, h["product_id"], None, "ads_only", "not tracked (previous decision)")
            counts["ads_only"] += 1
            continue
        res = probe(base, vid, h.get("handle"))
        source, stock, tracked = None, None, None
        if res["status"] in ("count", "sold_out"):
            source, stock, tracked = "cart_probe", res["stock"], 1
        elif res["status"] == "blocked":
            counts["blocked"] += 1
            blocked_run += 1
            conn.execute("UPDATE hero_variants SET consecutive_failures = COALESCE(consecutive_failures, 0) + 1, note = ? WHERE store_id = ? AND variant_id = ?",
                         (res["message"], store_id, vid))
            record_reading(conn, store_id, today, vid, h["product_id"], None, "blocked", res["message"])
            conn.commit()
            pause()
            continue
        else:
            # untracked / unknown / error: try the theme
            html = ""
            try:
                html = page_fetch(base, h["handle"])
            except requests.RequestException as e:
                res["message"] = f"{res['message']} | page: {e}"[:200]
            qty = theme_inventory_from_html(html, vid) if html else None
            if qty is not None:
                source, stock, tracked = "theme_inventory", max(qty, 0), 1
            else:
                source, stock, tracked = "ads_only", None, 0
            if html and h.get("bundle_like"):
                own = {x["variant_id"] for x in heroes if x["product_id"] == h["product_id"]}
                for cvid in component_variant_ids(html, own, known_vids)[:6]:
                    if cvid not in seen and cvid not in state:
                        row = conn.execute("""SELECT p.product_id, p.handle, v.title, v.price FROM variants_daily v JOIN products_daily p
                                              ON p.store_id = v.store_id AND p.snapshot_date = v.snapshot_date AND p.product_id = v.product_id
                                              WHERE v.store_id = ? AND v.variant_id = ? ORDER BY v.snapshot_date DESC LIMIT 1""",
                                           (store_id, cvid)).fetchone()
                        if row:
                            conn.execute("""INSERT OR IGNORE INTO hero_variants (store_id, variant_id, product_id, handle, variant_title, price, role, bundle_like, selected_at)
                                            VALUES (?,?,?,?,?,?,?,0,?)""", (store_id, cvid, row["product_id"], row["handle"], row["title"], row["price"], "bundle_component", today))
                            todo.append({"variant_id": cvid, "product_id": row["product_id"], "handle": row["handle"], "bundle_like": False})
                            counts["components"] += 1
        blocked_run = 0
        record_reading(conn, store_id, today, vid, h["product_id"], stock, source, res["message"])
        conn.execute("""UPDATE hero_variants SET signal_source = ?, inventory_tracked = ?, last_probe_date = ?, consecutive_failures = 0,
                        note = ? WHERE store_id = ? AND variant_id = ?""",
                     (source, tracked, today, res["message"][:200], store_id, vid))
        counts[source] += 1
        conn.commit()
        pause()
    compute_sales(conn, store_id, today)
    return counts


# ---------------------------------------------------------------- derived (pure + DB)

def sales_from_readings(readings: list[tuple[str, int | None]], today: str) -> dict:
    """readings: (date, stock) ascending, None = no reading. Sales are positive drops between
    consecutive available readings; rises are restocks and excluded."""
    pts = [(d, s) for d, s in readings if s is not None]
    out = {"units_sold_1d": None, "restock_units": None, "units_per_day_7d": None, "units_per_day_prev_7d": None,
           "units_per_day_wow": None, "prev_date": None}
    if len(pts) < 2 or pts[-1][0] != today:
        return out
    t = date.fromisoformat(today)
    prev_d, prev_s = pts[-2]
    drop = prev_s - pts[-1][1]
    out["prev_date"] = prev_d
    out["units_sold_1d"] = max(0, drop)
    out["restock_units"] = max(0, -drop)

    def units_per_day(lo: int, hi: int) -> float | None:
        """Units sold per measured day inside the window. An interval that contains a restock
        (stock went up) is unmeasurable and is dropped from both the units and the day count."""
        window = [(d, s) for d, s in pts if lo <= (t - date.fromisoformat(d)).days <= hi]
        if len(window) < 2:
            return None
        units = span = 0
        for a, b in zip(window, window[1:]):
            if b[1] > a[1]:
                continue
            units += a[1] - b[1]
            span += (date.fromisoformat(b[0]) - date.fromisoformat(a[0])).days
        return round(units / span, 2) if span > 0 else None
    out["units_per_day_7d"] = units_per_day(0, 7)
    out["units_per_day_prev_7d"] = units_per_day(7, 14)
    a, b = out["units_per_day_7d"], out["units_per_day_prev_7d"]
    if a is not None and b:
        out["units_per_day_wow"] = round(a / b, 2)
    return out


def compute_sales(conn: sqlite3.Connection, store_id: int, today: str) -> None:
    vids = [r[0] for r in conn.execute("SELECT variant_id FROM inventory_daily WHERE store_id = ? AND snapshot_date = ?",
                                       (store_id, today))]
    for vid in vids:
        readings = [(r[0], r[1]) for r in conn.execute(
            """SELECT snapshot_date, stock_level FROM inventory_daily WHERE store_id = ? AND variant_id = ? AND snapshot_date <= ?
               ORDER BY snapshot_date""", (store_id, vid, today))]
        s = sales_from_readings(readings, today)
        conn.execute("""UPDATE inventory_daily SET units_sold_1d = ?, restock_units = ?, units_per_day_7d = ?,
                          units_per_day_prev_7d = ?, units_per_day_wow = ?, prev_reading_date = ?
                        WHERE store_id = ? AND variant_id = ? AND snapshot_date = ?""",
                     (s["units_sold_1d"], s["restock_units"], s["units_per_day_7d"], s["units_per_day_prev_7d"],
                      s["units_per_day_wow"], s["prev_date"], store_id, vid, today))
    conn.commit()


def product_rows(conn: sqlite3.Connection, store_id: int, as_of: str) -> dict[str, dict]:
    """Per product (handle) aggregates for one day: sums over its hero variants."""
    snap = conn.execute("SELECT MAX(snapshot_date) FROM inventory_daily WHERE store_id = ? AND snapshot_date <= ?",
                        (store_id, as_of)).fetchone()[0]
    if not snap:
        return {}
    out: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT h.handle, i.signal_source, i.stock_level, i.units_sold_1d, i.units_per_day_7d, i.units_per_day_prev_7d,
                  h.inventory_tracked
           FROM inventory_daily i JOIN hero_variants h ON h.store_id = i.store_id AND h.variant_id = i.variant_id
           WHERE i.store_id = ? AND i.snapshot_date = ?""", (store_id, snap)):
        p = out.setdefault(r["handle"], {"sources": set(), "tracked": 0, "stock": None, "units_1d": None,
                                          "upd_7d": None, "upd_prev": None, "date": snap})
        p["sources"].add(r["signal_source"] or "")
        if r["inventory_tracked"]:
            p["tracked"] = 1
        for key, col in (("stock", "stock_level"), ("units_1d", "units_sold_1d"), ("upd_7d", "units_per_day_7d"),
                         ("upd_prev", "units_per_day_prev_7d")):
            if r[col] is not None:
                p[key] = (p[key] or 0) + r[col]
    for p in out.values():
        order = ["cart_probe", "theme_inventory", "blocked", "ads_only"]
        p["signal_source"] = next((s for s in order if s in p["sources"]), "") or ""
        p["units_per_day_wow"] = round(p["upd_7d"] / p["upd_prev"], 2) if p["upd_7d"] is not None and p["upd_prev"] else None
    return out


def inventory_for_signals(conn: sqlite3.Connection, store_id: int, as_of: str) -> dict[str, dict]:
    rows = product_rows(conn, store_id, as_of)
    return {h: {"signal_source": p["signal_source"], "inventory_tracked": "Y" if p["tracked"] else "N",
                "stock_level": p["stock"], "units_sold_1d": p["units_1d"], "units_per_day_7d": p["upd_7d"],
                "units_per_day_wow": p["units_per_day_wow"]} for h, p in rows.items()}


# ---------------------------------------------------------------- alerts

def run_alerts(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str) -> list[dict]:
    from . import deltas
    rows = product_rows(conn, store_id, today)
    if not rows:
        return []
    pub = {r["handle"]: r["published_at"] for r in conn.execute(
        """SELECT handle, published_at FROM products_daily WHERE store_id = ? AND snapshot_date =
           (SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ?)""", (store_id, store_id))}
    t = date.fromisoformat(today)

    def age(handle):
        d = deltas.parse_ts(pub.get(handle))
        return None if d is None else (t - d.date()).days
    found = []
    # rule 9
    for h, p in rows.items():
        a = age(h)
        if a is not None and a < 30 and p["units_per_day_wow"] is not None and p["units_per_day_wow"] >= 2.0 \
                and (p["upd_7d"] or 0) >= 5:
            found.append({"rule": 9, "handle": h, "key": f"9|{h}",
                          "detail": f"{h}: {a}d since publish, {p['upd_7d']:.1f} units/day (prev {p['upd_prev']:.1f}, "
                                    f"x{p['units_per_day_wow']}) stock {p['stock']}"})
    # rule 10: >= 50/day and not in the store's top 10 a week ago. "Top 10" is the store's own
    # best-selling collection order (collection_position <= 10) in the snapshot nearest to 7 days back.
    week_ago = (t - timedelta(days=7)).isoformat()
    snap = conn.execute("SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ? AND snapshot_date <= ?",
                        (store_id, week_ago)).fetchone()[0] or conn.execute(
        "SELECT MIN(snapshot_date) FROM products_daily WHERE store_id = ?", (store_id,)).fetchone()[0]
    top10_prev = {r[0] for r in conn.execute(
        "SELECT handle FROM products_daily WHERE store_id = ? AND snapshot_date = ? AND collection_position <= 10",
        (store_id, snap))} if snap else set()
    for h, p in rows.items():
        if (p["upd_7d"] or 0) >= 50 and h not in top10_prev:
            found.append({"rule": 10, "handle": h, "key": f"10|{h}",
                          "detail": f"{h}: {p['upd_7d']:.1f} units/day this week, was not in the store's top 10 on {snap} "
                                    f"(prev week {p['upd_prev'] if p['upd_prev'] is not None else 'n/a'}/day), {age(h)}d since publish"})
    # rule 11: sold out within 14 days of publish
    for h, p in rows.items():
        a = age(h)
        if a is not None and a <= 14 and p["stock"] == 0 and p["tracked"]:
            found.append({"rule": 11, "handle": h, "key": f"11|{h}",
                          "detail": f"{h}: stock hit 0 at {a}d since publish ({p['units_1d'] or 0} units sold since previous reading)"})
    now = _utcnow()
    written = []
    for f in found:
        # rule 11 is a one-off event (fires once per product); 9 and 10 re-fire at most weekly while still true
        since = "0000-00-00" if f["rule"] == 11 else (t - timedelta(days=6)).isoformat()
        if conn.execute("SELECT 1 FROM alerts WHERE snapshot_date >= ? AND store_id = ? AND dedupe_key = ?",
                        (since, store_id, f["key"])).fetchone():
            continue
        conn.execute("INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at, dedupe_key) VALUES (?,?,?,?,?,?,?)",
                     (today, store_id, f["handle"], f["rule"], f["detail"], now, f["key"]))
        written.append(f)
    conn.commit()
    return written
