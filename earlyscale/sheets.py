"""Google Sheets sync via an Apps Script web app (sheets/Code.gs).

The client side is deliberately simple: build rows from SQLite, cut them into
JSON chunks under SHEETS_CHUNK_BYTES, POST each chunk, follow the 302 that Apps
Script answers POSTs with, retry on 5xx/network errors, and surface Apps Script's
own {"ok": false, "error": ...} replies as clear exceptions.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from urllib.parse import urljoin, urlparse

import requests

from . import ad_metrics, config, deltas, radar, scaling, signals
from .watchlist import read_watchlist

log = logging.getLogger("earlyscale.sheets")

STORES_HEADERS = ["store", "meta page", "platform", "sort_informative", "last status", "products", "sold-out variants", "new products 7d",
                  "updated products 7d", "sold-out delta", "price changes", "change score", "last snapshot date",
                  "ads_scraped_on", "ads_active", "ads_delivering", "ads_delivering_7d_ago", "delivering_velocity_wow",
                  "ads_low_impressions", "new_ads_7d", "new_ads_prev_7d", "ad_velocity_wow",
                  "pages_per_domain", "pages_new_7d", "page_likes_slope_max",
                  "ads_to_products", "products_with_ads", "ads_not_attached (why)"]
PAGES_HEADERS = ["store", "page name", "page_id", "first_seen", "active_ads", "last_delivered", "page_likes",
                 "page_likes_7d_slope", "ads_as_of"]
PRODUCTS_HEADERS = ["date", "store", "handle", "title", "published_at", "updated_at", "price",
                    "available variants", "total variants", "collection position"]
ALERTS_HEADERS = ["date", "store", "handle", "rule", "detail", "created_at"]

SIGNALS_HEADERS = ["store", "product family", "handle", "channel tag", "days_since_published", "published_at",
                   "days_since_created", "created_at", "relaunch",
                   "price", "sold_out", "collection_rank", "collection_rank_delta_7d",
                   "variants_of_family_published_7d", "ads_pointing_here", "ads_in_top5", "best_rank", "best_rank_delta_7d",
                   "ads_delivering", "ads_delivering_7d_ago",
                   "delivering_velocity_wow", "ads_low_impressions", "concepts_delivering", "ads_launched_7d", "ads_launched_prev_7d",
                   "ad_velocity_wow", "ads_as_of", "pages_pointing_here", "pages_new_7d", "landing_paths", "landing_paths_new_7d",
                   "engagement_per_day",
                   "days_running_max", "concept_status", "eu_reach_slope_7d", "comment_delta_1d",
                   "signal_source", "inventory_tracked", "stock_level", "units_sold_1d", "units_per_day_7d",
                   "units_per_day_wow", "store_badge"]
FAMILIES_HEADERS = ["store", "family", "title", "handles", "newest published_at", "oldest published_at",
                    "published 7d", "published 14d", "published 30d", "best collection rank", "handle list"]
CATEGORIES_HEADERS = ["category", "stores", "families", "newest published_at", "families published 7d",
                      "store list", "example families"]

EARLY_HEADERS = ["store", "handle", "product family", "days_since_created", "created_at", "relaunch", "days_since_published",
                 "ads_in_top5", "best_rank", "best_rank_delta_7d",
                 "ads_delivering", "ads_delivering_7d_ago", "delivering_velocity_wow", "concepts_delivering", "ads_pointing_here",
                 "ads_launched_7d", "ads_launched_prev_7d", "ad_velocity_wow", "pages_pointing_here", "pages_new_7d",
                 "landing_paths_new_7d", "days_running_max", "concept_status", "price", "sold_out", "collection_rank", "stock_level",
                 "units_per_day_7d", "ads_as_of", "store_badge"]
EARLY_MAX_AGE_DAYS = 90

TAB_ORDER = ("signals", "early", "families", "categories", "stores", "pages", "candidates", "products", "alerts")
TAB_NAMES = {"signals": "Signals", "early": "Early", "families": "Families", "categories": "Categories",
             "stores": "Stores", "pages": "Pages", "candidates": "Candidates", "products": "Products", "alerts": "Alerts"}
TAB_MODES = {"signals": "replace", "early": "replace", "families": "replace", "categories": "replace",
             "stores": "replace", "pages": "replace", "candidates": "replace", "products": "replace", "alerts": "append"}


_watchlist_path = None   # set by set_watchlist_path(); None = default watchlist.csv


def set_watchlist_path(path) -> None:
    """Use an alternate watchlist.csv for the store filter (the CLI passes --watchlist through)."""
    global _watchlist_path
    _watchlist_path = path


def watched_store_ids(conn: sqlite3.Connection) -> set[int] | None:
    """Stores currently in watchlist.csv (None = no watchlist, use every store in the DB).
    Removed stores keep their history in SQLite but drop out of the sheet."""
    domains = {r["store_domain"] for r in read_watchlist(_watchlist_path)}
    if not domains:
        return None
    return {r["id"] for r in conn.execute("SELECT id, store_domain FROM stores") if r["store_domain"] in domains}


def _stores(conn: sqlite3.Connection):
    keep = watched_store_ids(conn)
    for s in conn.execute("SELECT id, store_domain, meta_page_name, platform, sort_informative FROM stores ORDER BY store_domain"):
        if keep is None or s["id"] in keep:
            yield s


class SheetsSyncError(Exception):
    """Fatal sync problem (bad URL, deployment not public, Apps Script error)."""


class _Retryable(SheetsSyncError):
    pass


# ---------------------------------------------------------------- rows from SQLite

def stores_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    by_id = {d.store_id: d for d in deltas.all_store_deltas(conn, as_of)}
    rows = []
    for s in _stores(conn):
        last = conn.execute(
            "SELECT status, error FROM store_runs WHERE store_id = ? ORDER BY run_id DESC LIMIT 1", (s["id"],)
        ).fetchone()
        if last is None:
            status = "never run"
        elif last["status"] == "ok":
            status = "ok"
        else:
            status = "error: " + (last["error"] or "")[:120]
        d = by_id.get(s["id"])
        b = ad_metrics.landing_breakdown(conn, s["id"], s["store_domain"], as_of)
        if b["snapshot"]:
            v = ad_metrics.ad_velocity(conn, s["id"], b["snapshot"])
            pm = scaling.store_page_metrics(conn, s["id"], s["store_domain"], b["snapshot"])
            dm = ad_metrics.delivering_metrics(conn, s["id"], b["snapshot"]).get("__store__", {})
            age = [b["snapshot"], b["active"], _blank(dm.get("ads_delivering")), _blank(dm.get("ads_delivering_7d_ago")),
                   dm.get("delivering_velocity_wow", ""), _blank(dm.get("ads_low_impressions")),
                   v["new_ads_7d"], v["new_ads_prev_7d"], ad_metrics._wow_cell(v["ad_velocity_wow"]),
                   _blank(pm["pages_per_domain"]), _blank(pm["pages_new_7d"]), _blank(pm["page_likes_slope_max"]),
                   b["to_products"], b["products"], ad_metrics.breakdown_summary(b)]
        else:
            age = ["never"] + [""] * 14
        platform = s["platform"] or ""
        si = {1: "true", 0: "false"}.get(s["sort_informative"], "")
        if d is None:
            rows.append([s["store_domain"], s["meta_page_name"] or "", platform, si, status, "", "", "", "", "", "", "", ""] + age)
            continue
        rows.append([
            d.store_domain, s["meta_page_name"] or "", platform, si, status, d.products, d.sold_out_variants,
            d.new_products_7d, d.updated_products_7d,
            "" if d.sold_out_variants_delta is None else d.sold_out_variants_delta,
            "" if d.price_changes is None else d.price_changes,
            d.change_score, d.snapshot_date,
        ] + age)
    rows.sort(key=lambda r: (-(r[9] if isinstance(r[9], int) else -1), r[0]))
    return rows


def _blank(v):
    return "" if v is None else v


def candidates_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    return radar.candidates_rows(conn)


def read_promote_marks(conn: sqlite3.Connection, url: str, session: requests.Session | None = None, retries: int = 3) -> int:
    """Before rewriting the Candidates tab, read its domain + promote columns back and honour any Y."""
    session = session or requests.Session()
    last: Exception | None = None
    for attempt in range(retries):
        try:
            data = get_json(session, url, {"tab": TAB_NAMES["candidates"], "rows": "1", "cols": "domain,promote"})
            n = radar.apply_promote_marks(conn, data.get("rows") or [])
            if n:
                log.info("sheets: %d promote mark(s) read back from the Candidates tab", n)
            return n
        except SheetsSyncError as e:
            last = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    log.warning("sheets: could not read Candidates back after %d tries (%s); promote marks not applied this time", retries, last)
    return 0


def pages_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    return scaling.pages_tab_rows(conn, list(_stores(conn)), as_of)


def products_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    rows = []
    for s in _stores(conn):
        dates = deltas.snapshot_dates(conn, s["id"], as_of)
        if not dates:
            continue
        for p in conn.execute(
            """SELECT snapshot_date, handle, title, published_at, updated_at, min_price, variant_count,
                      sold_out_variants, collection_position
               FROM products_daily WHERE store_id = ? AND snapshot_date = ? ORDER BY handle""",
            (s["id"], dates[0]),
        ):
            rows.append([
                p["snapshot_date"], s["store_domain"], p["handle"], p["title"] or "",
                p["published_at"] or "", p["updated_at"] or "",
                "" if p["min_price"] is None else p["min_price"],
                p["variant_count"] - p["sold_out_variants"], p["variant_count"],
                "" if p["collection_position"] is None else p["collection_position"],
            ])
    return rows


def alerts_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    if as_of is None:
        row = conn.execute("SELECT MAX(snapshot_date) FROM alerts").fetchone()
        as_of = row[0] if row else None
        if as_of is None:
            return []
    return [
        [a["snapshot_date"], a["store_domain"], a["product_handle"] or "", a["rule"], a["detail"] or "",
         a["created_at"]]
        for a in conn.execute(
            """SELECT a.snapshot_date, s.store_domain, a.product_handle, a.rule, a.detail, a.created_at
               FROM alerts a JOIN stores s ON s.id = a.store_id WHERE a.snapshot_date = ? ORDER BY a.id""",
            (as_of,))
    ]


_ctx_cache: dict = {}      # only filled while build_plan runs, so a lone signals_rows() call is always fresh


def _contexts(conn: sqlite3.Connection, as_of: str | None):
    """Per-store signal context; built once per build_plan and shared by the Signals, Families and Categories
    builders (it is the expensive part of a sync)."""
    if not _ctx_cache.get("active"):
        for s in _stores(conn):
            ctx = signals.store_signal_context(conn, s["id"], s["store_domain"], as_of)
            if ctx:
                yield ctx
        return
    key = (id(conn), as_of)
    if key not in _ctx_cache:
        _ctx_cache[key] = [c for c in (signals.store_signal_context(conn, s["id"], s["store_domain"], as_of) for s in _stores(conn)) if c]
    yield from list(_ctx_cache[key])


def signals_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    rows = [r for ctx in _contexts(conn, as_of) for r in signals.signals_rows_for_store(ctx)]
    # what we compare: ads in the impressions top 5 (desc) and the best-rank trend (climbing first), then the
    # delivering trend, then delivering ads, then launches this week (testing volume only), then youngest by created_at
    H = SIGNALS_HEADERS
    itop, idelta = H.index("ads_in_top5"), H.index("best_rank_delta_7d")
    idl, iwow = H.index("ads_delivering"), H.index("delivering_velocity_wow")
    il, ip = H.index("ads_launched_7d"), H.index("ads_pointing_here")
    icreated, ipub = H.index("days_since_created"), H.index("days_since_published")

    def key(r):
        top = r[itop] if isinstance(r[itop], int) else -1
        delta = r[idelta] if isinstance(r[idelta], int) else 0          # no history: neither climbing nor falling
        deliv = r[idl] if isinstance(r[idl], int) else -1
        wow = _wow_value(r[iwow])
        launched = r[il] if isinstance(r[il], int) else -1
        pointing = r[ip] if isinstance(r[ip], int) else -1
        days = r[icreated] if r[icreated] != "" else (r[ipub] if r[ipub] != "" else 10**6)
        return (-top, delta, -wow, -deliv, -launched, -pointing, days, r[0], r[2])
    rows.sort(key=key)
    return rows


def _wow_value(cell) -> float:
    """Sort value of a week-over-week cell ('' -> -1, 'new' / inf -> very large, '2.1' -> 2.1)."""
    if cell in ("", None):
        return -1.0
    if isinstance(cell, (int, float)):
        return float(cell)
    txt = str(cell).strip().lower().rstrip("x")
    if txt in ("inf", "new", "∞"):
        return 1e9
    try:
        return float(txt)
    except ValueError:
        return -1.0


def early_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    """The tab a truncating reader can rely on: every product created in the last EARLY_MAX_AGE_DAYS that has at
    least one ad, most delivering ads first, then their week-over-week trend, then youngest. A subset of Signals' columns."""
    H = SIGNALS_HEADERS
    idx = {h: H.index(h) for h in EARLY_HEADERS}
    out = []
    for r in signals_rows(conn, as_of):
        created = r[idx["days_since_created"]]
        ads = r[idx["ads_pointing_here"]]
        launched = r[idx["ads_launched_7d"]]
        if created == "" or created > EARLY_MAX_AGE_DAYS:
            continue
        if not ((isinstance(ads, int) and ads > 0) or (isinstance(launched, int) and launched > 0)):
            continue
        out.append([r[idx[h]] for h in EARLY_HEADERS])
    E = EARLY_HEADERS
    ic, il = E.index("days_since_created"), E.index("ads_launched_7d")
    idl, iwow, itop, idelta = E.index("ads_delivering"), E.index("delivering_velocity_wow"), E.index("ads_in_top5"), E.index("best_rank_delta_7d")
    out.sort(key=lambda r: (-(r[itop] if isinstance(r[itop], int) else -1), r[idelta] if isinstance(r[idelta], int) else 0,
                            -_wow_value(r[iwow]), -(r[idl] if isinstance(r[idl], int) else -1),
                            -(r[il] if isinstance(r[il], int) else -1), r[ic], r[0], r[1]))
    return out


def families_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    rows = [r for ctx in _contexts(conn, as_of) for r in signals.families_rows_for_store(ctx)]
    rows.sort(key=lambda r: (-r[6], -r[7], r[4] or "", r[0], r[1]))   # most 7d launches first
    return rows


def categories_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    return signals.categories_rows(families_rows(conn, as_of))


# ---------------------------------------------------------------- chunking

def _size(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def chunk_rows(rows: list[list], max_bytes: int = config.SHEETS_CHUNK_BYTES) -> list[list[list]]:
    """Greedy split so each chunk's JSON stays under max_bytes (~200 bytes envelope reserved).
    A single oversized row still goes out alone. Zero rows -> one empty chunk."""
    if not rows:
        return [[]]
    budget = max(1000, max_bytes - 200)
    chunks: list[list[list]] = []
    cur: list[list] = []
    size = 2  # "[]"
    for r in rows:
        rsize = _size(r) + 1
        if cur and size + rsize > budget:
            chunks.append(cur)
            cur, size = [], 2
        cur.append(r)
        size += rsize
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- HTTP

def _explain_non_json(text: str, status: int) -> str:
    head = text.strip()[:160].replace("\n", " ")
    hint = ("Apps Script returned a web page instead of JSON. Usual causes: the deployment's "
            "'Who has access' is not 'Anyone', the URL is not the /exec URL, or you edited Code.gs "
            "without doing Deploy > Manage deployments > Edit > New version.")
    return f"{hint} (HTTP {status}; starts with: {head!r})"


def _echo_hiccup(r, hops: int) -> bool:
    """Google's script.googleusercontent.com response host sometimes answers the one-time redirect with its own
    generic 404 error page (the body carries 'ppConfig'). That is not the script's answer and goes away on retry."""
    if hops == 0 or "googleusercontent" not in urlparse(r.url).netloc:
        return False
    head = (r.text or "")[:2000]
    return r.status_code == 404 or ("ppConfig" in head and not head.lstrip().startswith("{"))


def post_payload(session: requests.Session, url: str, payload: dict, retries: int = 5) -> dict:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session.post(url, data=body, headers={"Content-Type": "application/json"},
                             timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
            hops = 0
            chain = []
            while r.status_code in (301, 302, 303, 307, 308) and hops < 10:
                # Apps Script answers every POST with a 302 to a one-time googleusercontent URL
                # that serves the script's response; that hop must be a GET.
                loc = urljoin(r.url, r.headers.get("Location", ""))
                chain.append(urlparse(loc).netloc)
                if "accounts.google" in loc:
                    raise SheetsSyncError("Apps Script redirected to a Google login page: the deployment's 'Who has access' must be "
                                          "'Anyone' (Deploy > Manage deployments > Edit), and the URL must be the /exec URL")
                r = session.get(loc, timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
                hops += 1
            if r.status_code in (301, 302, 303, 307, 308):
                # the one-time response URL kept bouncing: its key is spent or Google hiccupped; a fresh POST gets a new one
                raise _Retryable(f"Google's response host kept redirecting ({hops} hops via {', '.join(dict.fromkeys(chain))})")
            if r.status_code >= 500:
                raise _Retryable(f"HTTP {r.status_code} from Apps Script")
            if _echo_hiccup(r, hops):
                raise _Retryable(f"HTTP {r.status_code} error page from Google's response host (transient)")
            if r.status_code != 200:
                raise SheetsSyncError(f"HTTP {r.status_code} from {r.url}: {r.text.strip()[:200]!r}")
            text = r.text.strip()
            if text.lower() == "ok":
                # doGet's health-check text: Google served the deployment's GET reply instead of the POST's
                # one-time response (seen once in a while mid-sync); a fresh POST gets the real answer
                raise _Retryable("got the script's health-check text ('ok') instead of the POST reply (transient)")
            if not text.startswith("{"):
                raise SheetsSyncError(_explain_non_json(text, r.status_code))
            data = json.loads(text)
            if not data.get("ok"):
                raise SheetsSyncError(f"Apps Script reported an error: {data.get('error')}")
            return data
        except (_Retryable, requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        if attempt < retries:
            wait = min(60, (2 ** attempt) * 3) + random.uniform(0, 2)
            log.warning("sheets: retry %d/%d after %s (sleep %.1fs)", attempt + 1, retries, last_err, wait)
            time.sleep(wait)
    raise SheetsSyncError(f"giving up after {retries + 1} attempts: {last_err}")


def get_json(session: requests.Session, url: str, params: dict, _retry: int = 0) -> dict:
    """GET the web app (follows the same 302 hop as POST) and parse its JSON."""
    r = session.get(url, params=params, timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
    hops = 0
    chain = []
    while r.status_code in (301, 302, 303, 307, 308) and hops < 10:
        nxt = urljoin(r.url, r.headers.get("Location", ""))
        chain.append(urlparse(nxt).netloc)
        if "accounts.google" in nxt:
            raise SheetsSyncError("Apps Script redirected to a Google login page: the deployment's 'Who has access' must be "
                                  "'Anyone' (Deploy > Manage deployments > Edit), and the URL must be the /exec URL")
        r = session.get(nxt, timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
        hops += 1
    if _echo_hiccup(r, hops) and _retry < 3:
        time.sleep(5 * (_retry + 1))
        return get_json(session, url, params, _retry=_retry + 1)
    text = r.text.strip()
    if r.status_code in (301, 302, 303, 307, 308):
        raise SheetsSyncError(f"Apps Script kept redirecting ({hops} hops via {', '.join(dict.fromkeys(chain))}); usually a temporary "
                              "Google hiccup, sometimes a response too large for a GET")
    if r.status_code != 200 or not text.startswith("{"):
        raise SheetsSyncError(_explain_non_json(text, r.status_code) if not text.startswith("{") else f"HTTP {r.status_code}")
    data = json.loads(text)
    if not data.get("ok"):
        raise SheetsSyncError(f"Apps Script reported an error: {data.get('error')}")
    return data


def verify(conn: sqlite3.Connection, url: str, as_of: str | None = None,
           session: requests.Session | None = None) -> dict:
    """Compare what the sheet holds with what the DB says it should: rows per tab, and for
    Products rows per store. Returns {"tabs": [...], "products": [...], "problems": [...]}.
    Needs the doGet of the current Code.gs (older deployments answer plain 'ok')."""
    session = session or requests.Session()
    try:
        counts = get_json(session, url, {"tabs": "1"}).get("tabs") or {}
    except (SheetsSyncError, ValueError) as e:
        raise SheetsSyncError(f"the deployed Code.gs does not answer ?tabs=1 (re-paste sheets/Code.gs and deploy a new version): {e}") from e
    plan = build_plan(conn, TAB_ORDER, as_of)
    tabs, problems = [], []
    for item in plan:
        expected = len(item["rows"])
        have = counts.get(item["tab"])
        ok = (have is not None) and (have == expected if item["mode"] == "replace" else have >= expected)
        tabs.append({"tab": item["tab"], "mode": item["mode"], "expected": expected, "sheet": have, "ok": ok})
        if not ok:
            problems.append(f"{item['tab']}: sheet has {have} rows, DB has {expected}")
    products = []
    try:
        by = get_json(session, url, {"tab": TAB_NAMES["products"], "group": "1"}).get("byValue") or {}
    except SheetsSyncError as e:
        problems.append(f"could not read Products per store: {e}")
        by = None
    if by is not None:
        want: dict[str, int] = {}
        for r in products_rows(conn, as_of):
            want[r[1]] = want.get(r[1], 0) + 1
        for store in sorted(set(want) | set(by)):
            w, h = want.get(store, 0), int(by.get(store, 0))
            products.append({"store": store, "expected": w, "sheet": h, "ok": w == h})
            if w != h:
                problems.append(f"Products/{store}: sheet has {h} rows, DB has {w}")
    return {"tabs": tabs, "products": products, "problems": problems}


# ---------------------------------------------------------------- orchestration

def build_plan(conn: sqlite3.Connection, tabs=TAB_ORDER, as_of: str | None = None) -> list[dict]:
    builders = {"signals": signals_rows, "early": early_rows, "families": families_rows, "categories": categories_rows,
                "stores": stores_rows, "pages": pages_rows, "candidates": candidates_rows, "products": products_rows, "alerts": alerts_rows}
    plan = []
    _ctx_cache.clear()
    _ctx_cache["active"] = True
    for tab in TAB_ORDER:
        if tab not in tabs:
            continue
        t0 = time.monotonic()
        rows = builders[tab](conn, as_of)
        took = time.monotonic() - t0
        if took >= 5:
            log.info("sheets: built %s (%d rows) in %.0fs", TAB_NAMES[tab], len(rows), took)
        plan.append({"tab": TAB_NAMES[tab], "mode": TAB_MODES[tab], "rows": rows, "chunks": chunk_rows(rows)})
    _ctx_cache.clear()
    return plan


def expected_headers() -> dict[str, list[str]]:
    return {"Signals": SIGNALS_HEADERS, "Early": EARLY_HEADERS, "Families": FAMILIES_HEADERS, "Categories": CATEGORIES_HEADERS,
            "Stores": STORES_HEADERS, "Pages": PAGES_HEADERS, "Candidates": radar.CANDIDATES_HEADERS,
            "Products": PRODUCTS_HEADERS, "Alerts": ALERTS_HEADERS}


def check_deployed_headers(session: requests.Session, url: str, tabs=TAB_ORDER) -> list[str]:
    """Compare the headers the deployed Code.gs writes with the columns this code sends. Returns the names of
    tabs that differ (a stale deployment would put every value under the wrong column). An old deployment
    that does not report headers yields ['?'] so the caller can warn instead of writing blind."""
    try:
        data = get_json(session, url, {"tabs": "1"})
    except SheetsSyncError as e:
        raise SheetsSyncError(f"cannot read the deployed Code.gs ({e})") from e
    deployed = data.get("headers")
    if not isinstance(deployed, dict):
        return ["?"]
    want = expected_headers()
    bad = []
    for t in tabs:
        name = TAB_NAMES[t]
        if deployed.get(name) != want[name]:
            bad.append(name)
    return bad


def sync(conn: sqlite3.Connection, url: str, tabs=TAB_ORDER, as_of: str | None = None,
         session: requests.Session | None = None, dry_run: bool = False, check_headers: bool = True) -> list[dict]:
    """POST every tab. Returns one summary dict per tab: tab, rows, chunks, written, skipped.
    Refuses to write when the deployed Code.gs carries different headers than this code (column shift)."""
    if not url and not dry_run:
        raise SheetsSyncError("SHEETS_WEBHOOK_URL is not set (put it in .env)")
    session = session or requests.Session()
    if url and not dry_run and check_headers:
        bad = check_deployed_headers(session, url, tabs)
        if bad == ["?"]:
            log.warning("sheets: the deployed Code.gs does not report its headers (old version); cannot check for a column shift. "
                        "Re-paste sheets/Code.gs and deploy a new version.")
        elif bad:
            raise SheetsSyncError(f"the deployed Code.gs writes different columns than this code for: {', '.join(bad)}. "
                                  "Every value would land under the wrong header. Paste sheets/Code.gs into the Apps Script editor "
                                  "and Deploy > Manage deployments > Edit > New version, then sync again.")
    if "candidates" in tabs and url and not dry_run:
        read_promote_marks(conn, url, session)
    summaries = []
    for item in build_plan(conn, tabs, as_of):
        n = len(item["chunks"])
        written = skipped = 0
        for i, chunk in enumerate(item["chunks"], start=1):
            payload = {"tab": item["tab"], "mode": item["mode"], "chunk": i, "chunks": n, "rows": chunk}
            size = _size(payload)
            if dry_run:
                log.info("sheets: [dry-run] %s chunk %d/%d: %d rows, %d bytes", item["tab"], i, n, len(chunk), size)
                continue
            resp = post_payload(session, url, payload)
            written += int(resp.get("written", 0))
            skipped += int(resp.get("skipped", 0))
            log.info("sheets: %s chunk %d/%d: %d rows -> written %s skipped %s (%d bytes)", item["tab"], i, n,
                     len(chunk), resp.get("written"), resp.get("skipped"), size)
        summaries.append({"tab": item["tab"], "rows": len(item["rows"]), "chunks": n,
                          "written": written, "skipped": skipped})
    return summaries
