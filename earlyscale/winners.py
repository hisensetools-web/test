"""The derived numbers: per landing URL per day, from ads / ads_daily / products_daily only.

  normalise_landing_path(url)      pure: host + path, lower-case, no query string, no trailing slash
  family_key / build_families      pure: handles of one store grouped into product families
  compute_url_daily(...)           per store per day -> url_daily rows (delivering, wow, proven_days, pages, ...)
  winners_rows / stores_rows       the two sheet tabs

A delivering ad is an ad present in the day's active search whose card did NOT show the 'Low impression count'
badge. An ad whose badge could not be read (the card never rendered) is not delivering: better a smaller number
than a wrong one. Nothing here guesses what a lander sells: /pages/ URLs get no product family.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from urllib.parse import urlparse

log = logging.getLogger("earlyscale.winners")

# handle suffixes Shopify merchants add when duplicating a product: -copy, -1, -2, -cc, -otp, -sub, -es, -copy-2 ...
SUFFIX_TOKENS = {"copy", "cc", "otp", "sub", "es", "de", "fr", "nl", "it", "pt", "us", "uk", "eu", "ca", "au", "new", "old",
                 "v2", "v3", "test", "tt", "b", "a", "x", "alt", "dup", "duplicate", "clone", "google", "tiktok", "fb", "meta"}
SUFFIX_RE = re.compile(r"(?:[-_ ](?:%s|\d{1,3}))+$" % "|".join(re.escape(t) for t in sorted(SUFFIX_TOKENS, key=len, reverse=True)), re.I)
PRODUCT_PATH_RE = re.compile(r"/products/([^/?#]+)", re.I)


# ---------------------------------------------------------------- pure

def normalise_landing_path(url: str | None) -> str | None:
    """'https://www.Shop.com/pages/Prostate/?utm=x#top' -> 'shop.com/pages/prostate'. None for an empty URL."""
    if not url:
        return None
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    try:
        p = urlparse(u)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    try:
        if p.port and p.port not in (80, 443):
            host = f"{host}:{p.port}"          # local mock stores; real stores never carry a port
    except ValueError:
        pass
    path = re.sub(r"/{2,}", "/", p.path or "/").lower()
    if len(path) > 1:
        path = path.rstrip("/")
    return host + path


def product_handle_of(landing_path: str | None) -> str | None:
    """The Shopify handle in a /products/<handle> path; None for landers, collections and everything else."""
    if not landing_path:
        return None
    m = PRODUCT_PATH_RE.search("/" + landing_path.split("/", 1)[1] if "/" in landing_path else "")
    return m.group(1).lower() if m else None


def family_key(handle: str | None, title: str | None = None) -> str:
    """The name shared by every duplicate of a product: the handle without its -copy / -1 / -cc / -otp / -sub /
    -es suffixes (and the title treated the same way when there is no handle)."""
    base = (handle or "").strip().lower()
    if not base and title:
        base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    stripped = SUFFIX_RE.sub("", base)
    return stripped or base


def title_key(title: str | None) -> str:
    """Titles compared without case, punctuation, sizes and the same duplicate markers ('(copy)', '- ES')."""
    t = re.sub(r"\(([^)]*)\)", r" \1 ", (title or "").lower())
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    words = [w for w in t.split() if w]
    while words and (words[-1] in SUFFIX_TOKENS or words[-1].isdigit()):
        words.pop()
    return " ".join(words)


def build_families(products: list[dict]) -> dict[str, dict]:
    """products: rows with product_id, handle, title, created_at (every snapshot of one store, duplicates fine).
    Returns {handle: {"family": <key>, "created": <oldest created_at>, "handles": [...]}}.
    Handles that share a product id are one family; so are handles that share a suffix-stripped handle
    or a normalised title."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_pid: dict = defaultdict(set)
    by_hkey: dict = defaultdict(set)
    by_tkey: dict = defaultdict(set)
    created: dict[str, str] = {}
    for p in products:
        h = (p.get("handle") or "").lower()
        if not h:
            continue
        find(h)
        if p.get("product_id") is not None:
            by_pid[p["product_id"]].add(h)
        by_hkey[family_key(h)].add(h)
        tk = title_key(p.get("title"))
        if tk:
            by_tkey[tk].add(h)
        c = (p.get("created_at") or "")[:10]
        if c and (h not in created or c < created[h]):
            created[h] = c
    for group in list(by_pid.values()) + list(by_hkey.values()) + list(by_tkey.values()):
        hs = sorted(group)
        for other in hs[1:]:
            union(hs[0], other)
    members: dict[str, list[str]] = defaultdict(list)
    for h in parent:
        members[find(h)].append(h)
    out: dict[str, dict] = {}
    for root, hs in members.items():
        hs.sort(key=lambda x: (len(x), x))
        key = family_key(hs[0])
        dates = [created[h] for h in hs if h in created]
        rec = {"family": key, "created": min(dates) if dates else None, "handles": hs}
        for h in hs:
            out[h] = rec
    return out


def days_between(later: str, earlier: str | None) -> int | None:
    if not earlier:
        return None
    try:
        return (date.fromisoformat(later[:10]) - date.fromisoformat(earlier[:10])).days
    except ValueError:
        return None


def wow_cell(delivering: int, seven_days_ago: int | None):
    """'' without history; 'new' when the URL had 0 delivering ads a week ago; else the ratio rounded to 2 places."""
    if seven_days_ago is None:
        return ""
    if seven_days_ago == 0:
        return "new" if delivering > 0 else ""
    return round(delivering / seven_days_ago, 2)


def wow_sort_value(cell) -> float:
    if cell == "new":
        return 1e9
    if cell in ("", None):
        return -1.0
    try:
        return float(cell)
    except (TypeError, ValueError):
        return -1.0


# ---------------------------------------------------------------- per store per day

def store_families(conn: sqlite3.Connection, store_id: int) -> dict[str, dict]:
    rows = [dict(r) for r in conn.execute("""SELECT DISTINCT product_id, handle, title, created_at FROM products_daily
                                             WHERE store_id = ?""", (store_id,))]
    return build_families(rows)


def scrape_dates(conn: sqlite3.Connection, store_id: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM ads_daily WHERE store_id = ? ORDER BY snapshot_date", (store_id,))]


def week_ago_scrape(dates: list[str], today: str) -> str | None:
    """The scrape day closest to today - 7 within today - 8 .. today - 6, else None (no week-over-week)."""
    t = date.fromisoformat(today)
    best, best_gap = None, 99
    for d in dates:
        gap = abs((t - date.fromisoformat(d)).days - 7)
        if gap <= 1 and gap < best_gap:
            best, best_gap = d, gap
    return best


def compute_url_daily(conn: sqlite3.Connection, store_id: int, today: str, families: dict | None = None) -> int:
    """Rewrite url_daily for one store and one day from ads_daily + ads. Returns the number of URL rows."""
    families = store_families(conn, store_id) if families is None else families
    rows = conn.execute("""SELECT a.ad_id, a.page_id, a.page_name, a.landing_path, a.first_seen, d.low_impressions
                           FROM ads_daily d JOIN ads a ON a.ad_id = d.ad_id AND a.store_id = d.store_id
                           WHERE d.store_id = ? AND d.snapshot_date = ? AND d.still_active = 1 AND a.landing_path IS NOT NULL""",
                        (store_id, today)).fetchall()
    per_url: dict[str, list] = defaultdict(list)
    for r in rows:
        if r["low_impressions"] == 0:                     # 1 = badge, NULL = unknown: neither is delivering
            per_url[r["landing_path"]].append(r)
    prev_day = week_ago_scrape(scrape_dates(conn, store_id), today)
    prev: dict[str, int] = {}
    if prev_day:
        prev = {r[0]: r[1] for r in conn.execute("SELECT landing_path, delivering FROM url_daily WHERE store_id = ? AND snapshot_date = ?",
                                                  (store_id, prev_day))}
    out = []
    for path, ads in per_url.items():
        pages: Counter = Counter()
        names: dict[str, str] = {}
        first_by_page: dict[str, str] = {}
        oldest = None
        for a in ads:
            pid = a["page_id"] or a["page_name"] or "?"
            pages[pid] += 1
            names.setdefault(pid, a["page_name"] or pid)
            fs = (a["first_seen"] or "")[:10]
            if fs:
                if pid not in first_by_page or fs < first_by_page[pid]:
                    first_by_page[pid] = fs
                if oldest is None or fs < oldest:
                    oldest = fs
        new_pages = sum(1 for pid in pages if pid in first_by_page and (days_between(today, first_by_page[pid]) or 0) < 7)
        top = names[pages.most_common(1)[0][0]] if pages else None
        handle = product_handle_of(path)
        fam = families.get(handle) if handle else None
        out.append((today, store_id, path, len(ads), prev.get(path, 0) if prev_day else None,
                    days_between(today, oldest) if oldest else None, len(pages), new_pages, top,
                    fam["family"] if fam else (family_key(handle) if handle else None), fam["created"] if fam else None))
    with conn:
        conn.execute("DELETE FROM url_daily WHERE store_id = ? AND snapshot_date = ?", (store_id, today))
        conn.executemany("""INSERT INTO url_daily (snapshot_date, store_id, landing_path, delivering, delivering_7d_ago, proven_days,
                                                   pages, pages_new_7d, top_page, family_key, family_created) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", out)
    return len(out)


def rebuild_url_daily(conn: sqlite3.Connection, store_id: int | None = None) -> int:
    """Recompute every day's url_daily rows (oldest first, so the week-ago values exist when a day needs them)."""
    stores = [store_id] if store_id is not None else [r[0] for r in conn.execute("SELECT DISTINCT store_id FROM ads_daily")]
    n = 0
    for sid in stores:
        fams = store_families(conn, sid)
        for d in scrape_dates(conn, sid):
            n += compute_url_daily(conn, sid, d, fams)
    return n


# ---------------------------------------------------------------- sheet rows

WINNERS_HEADERS = ["store", "landing_url", "delivering", "delivering_7d_ago", "delivering_wow", "proven_days", "pages", "pages_new_7d",
                   "top_page", "family_age_days", "store_age_days", "ads_as_of"]
STORES_HEADERS = ["store", "shop_id", "store_age_days", "products", "ads_as_of", "last error"]
MIN_DELIVERING = 3


def _store_age(conn: sqlite3.Connection, as_of: str) -> dict[int, int | None]:
    from . import store_age
    out = {}
    for r in conn.execute("SELECT id, store_created_est FROM stores"):
        out[r["id"]] = store_age.age_days(r["store_created_est"], as_of) if r["store_created_est"] else None
    return out


def latest_scrape(conn: sqlite3.Connection, store_id: int, as_of: str | None = None) -> str | None:
    return conn.execute("SELECT MAX(snapshot_date) FROM ads_daily WHERE store_id = ? AND snapshot_date <= ?",
                        (store_id, as_of or "9999")).fetchone()[0]


def winners_rows(conn: sqlite3.Connection, stores, as_of: str | None = None) -> list[list]:
    """One row per landing URL with >= MIN_DELIVERING delivering ads in the store's latest scrape. Sorted by
    delivering_wow desc ('new' first, no history last), then delivering desc."""
    today = as_of or date.today().isoformat()
    ages = _store_age(conn, today)
    rows = []
    for s in stores:
        snap = latest_scrape(conn, s["id"], as_of)
        if not snap:
            continue
        for u in conn.execute("""SELECT * FROM url_daily WHERE store_id = ? AND snapshot_date = ? AND delivering >= ?
                                 ORDER BY delivering DESC""", (s["id"], snap, MIN_DELIVERING)):
            fam_age = days_between(snap, u["family_created"]) if u["family_created"] else None
            fam_age = max(0, fam_age) if fam_age is not None else None     # a product created later today (time zones) is 0 days old, not -1
            rows.append([s["store_domain"], u["landing_path"], u["delivering"],
                         "" if u["delivering_7d_ago"] is None else u["delivering_7d_ago"],
                         wow_cell(u["delivering"], u["delivering_7d_ago"]),
                         "" if u["proven_days"] is None else u["proven_days"], u["pages"], u["pages_new_7d"], u["top_page"] or "",
                         "" if fam_age is None else fam_age, "" if ages.get(s["id"]) is None else ages[s["id"]], snap])
    rows.sort(key=lambda r: (-wow_sort_value(r[4]), -r[2], r[0], r[1]))
    return rows


def stores_rows(conn: sqlite3.Connection, stores, as_of: str | None = None) -> list[list]:
    today = as_of or date.today().isoformat()
    ages = _store_age(conn, today)
    rows = []
    for s in stores:
        last = conn.execute("SELECT status, error FROM store_runs WHERE store_id = ? ORDER BY run_id DESC LIMIT 1", (s["id"],)).fetchone()
        err = "" if last is None or last["status"] == "ok" else (last["error"] or "error")[:120]
        if last is None:
            err = "never run"
        pd, products = conn.execute("""SELECT snapshot_date, COUNT(*) FROM products_daily WHERE store_id = ? AND snapshot_date =
                                       (SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ? AND snapshot_date <= ?)""",
                                    (s["id"], s["id"], as_of or "9999")).fetchone()
        rows.append([s["store_domain"], "" if s["shop_id"] is None else str(s["shop_id"]),
                     "" if ages.get(s["id"]) is None else ages[s["id"]], products if pd else "",
                     latest_scrape(conn, s["id"], as_of) or "", err])
    rows.sort(key=lambda r: r[0])
    return rows
