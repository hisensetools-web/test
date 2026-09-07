"""Meta layer part 2 (A): per-ad detail from the single-ad Ad Library page + creative fingerprints.

  facebook.com/ads/library/?id={ad_archive_id} embeds the ad record in the page HTML inside
  <script type="application/json" data-sjs> blocks, under deeplink_ad_archive_result.deeplink_ad_archive.
  There is no post id in it (ad_id is null); we do not look for one.

  parse_ad_detail_html   -> {ad_id, start_date, end_date, is_active, page_id, page_name, page_like_count,
                             page_profile_id, page_profile_uri, page_categories, images, videos}
  record_detail          one meta_ad_detail_daily row per ad per day (source = detail | list)
  update_delivery        last_delivered = end_date; "off" when end_date stops advancing for 2+ days
  record_page_likes      one meta_page_likes_daily row per page per day, likes_delta_1d, 7d slope, prev 7d slope
  fingerprint_creatives  download images / videos, sha256 them into meta_creatives (lineage by creative)
  creative_lineage       ads on the same page sharing a creative hash -> lineage_of (via = creative)
  select_for_detail      which ads to fetch today (flagged, new, one per page, then the rest; capped)
  run_alerts             rule 12: page-likes 7-day slope doubled week over week
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

from . import config, meta_ads

log = logging.getLogger("earlyscale.ad_detail")

RULES = {12: "page likes/day (7d slope) doubled week over week - page-level spend proxy"}

SJS_RE = re.compile(r'<script[^>]*\bdata-sjs\b[^>]*>(.*?)</script>', re.S | re.I)
PROFILE_ID_RE = re.compile(r"(?:profile\.php\?id=|/)(\d{6,20})(?:[/?#]|$)")


# ---------------------------------------------------------------- parsing (pure)

def sjs_blocks(html: str) -> list:
    out = []
    for m in SJS_RE.finditer(html or ""):
        txt = m.group(1).strip()
        if not txt:
            continue
        try:
            out.append(json.loads(txt))
        except ValueError:
            out.extend(meta_ads.parse_json_lines(txt))
    return out


def find_detail_node(obj, ad_id: str | None = None):
    """The deeplink_ad_archive record (or, failing that, any ad record with a snapshot) in a payload."""
    fallback = None
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            node = cur.get("deeplink_ad_archive")
            if isinstance(node, dict) and node.get("ad_archive_id"):
                if ad_id is None or str(node["ad_archive_id"]) == str(ad_id):
                    return node
            if "ad_archive_id" in cur and isinstance(cur.get("snapshot"), dict):
                if ad_id is None or str(cur["ad_archive_id"]) == str(ad_id):
                    fallback = fallback or cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return fallback


def _unix_to_date(v):
    return meta_ads._unix_to_date(v)


def creative_urls(node: dict) -> list[tuple[str, str]]:
    """[(kind, url)] for every image / video the ad record carries, in a stable order."""
    snap = node.get("snapshot") or {}
    out: list[tuple[str, str]] = []
    seen = set()

    def add(kind, url):
        if url and url not in seen:
            seen.add(url)
            out.append((kind, url))
    for im in snap.get("images") or []:
        add("image", im.get("original_image_url") or im.get("resized_image_url"))
    for v in snap.get("videos") or []:
        add("video", v.get("video_hd_url") or v.get("video_sd_url"))
    for c in snap.get("cards") or []:
        add("image", c.get("original_image_url") or c.get("resized_image_url"))
        add("video", c.get("video_hd_url") or c.get("video_sd_url"))
    return out


def normalise_detail(node: dict) -> dict:
    snap = node.get("snapshot") or {}
    uri = node.get("page_profile_uri") or snap.get("page_profile_uri")
    m = PROFILE_ID_RE.search(uri or "")
    cats = node.get("page_categories") or snap.get("page_categories") or []
    if isinstance(cats, dict):
        cats = list(cats.values())
    cats = [str(c.get("name") if isinstance(c, dict) else c) for c in cats if c]
    like = node.get("page_like_count", snap.get("page_like_count"))
    urls = creative_urls(node)
    return {
        "ad_id": str(node.get("ad_archive_id")),
        "start_date": _unix_to_date(node.get("start_date")),
        "end_date": _unix_to_date(node.get("end_date")),
        "is_active": None if node.get("is_active") is None else (1 if node.get("is_active") else 0),
        "page_id": str(node.get("page_id") or snap.get("page_id") or "") or None,
        "page_name": node.get("page_name") or snap.get("page_name"),
        "page_like_count": int(like) if isinstance(like, (int, float)) else None,
        "page_profile_uri": uri,
        "page_profile_id": (m.group(1) if m else None) or (str(node.get("page_id")) if node.get("page_id") else None),
        "page_categories": cats,
        "images": [u for k, u in urls if k == "image"],
        "videos": [u for k, u in urls if k == "video"],
    }


def parse_ad_detail_html(html: str, ad_id: str | None = None) -> dict | None:
    for block in sjs_blocks(html):
        node = find_detail_node(block, ad_id)
        if node:
            return normalise_detail(node)
    return None


REMOVED_MARKERS = ("no longer available", "isn't available", "is not available", "couldn't find", "could not find",
                   "not currently available", "ad has been removed", "this content isn't available")


def no_record_reason(html: str, ad_id: str) -> str:
    """Why a single-ad page carried no record: removed (the library says the ad is gone), no-record:<hint>."""
    low = (html or "").lower()
    text = re.sub(r"<[^>]+>", " ", low)
    if any(m in text for m in REMOVED_MARKERS):
        return "removed"
    blocks = len(SJS_RE.findall(html or ""))
    has_id = str(ad_id) in (html or "")
    return f"no-record:sjs={blocks},id_in_html={'y' if has_id else 'n'},bytes={len(html or '')}"


def detail_from_list_node(node: dict) -> dict:
    """The same record shape from a search-results node (the list payload also carries end_date and
    page_like_count), so every scraped ad gets a 'list' reading for free each day."""
    return normalise_detail(node)


# ---------------------------------------------------------------- browser fetch

def detail_url(ad_id: str) -> str:
    return f"{config.META_AD_LIBRARY_BASE}?id={ad_id}"


BLOCKED_RESOURCE_TYPES = ("image", "media", "font", "stylesheet")


def new_context(browser, light: bool | None = None):
    """A context for single-ad pages. `light` (default META_DETAIL_LIGHT) aborts images, media, fonts,
    stylesheets and external scripts: the record is in the server-rendered HTML, and Facebook's
    JS bundle is what makes 60 pages in a row exhaust a desktop Chromium."""
    light = config.META_DETAIL_LIGHT if light is None else light
    ctx = browser.new_context(user_agent=random.choice(meta_ads.USER_AGENTS), viewport={"width": 1366, "height": 850},
                              locale="en-US")
    if light:
        def _route(route):
            rt = route.request.resource_type
            if rt in BLOCKED_RESOURCE_TYPES or (rt == "script" and route.request.url.startswith("http")):
                return route.abort()
            return route.continue_()
        ctx.route("**/*", _route)
    return ctx


def _browser_of(browser):
    """`browser` may be a Playwright Browser or a meta_ads.BrowserHandle."""
    return browser.get() if hasattr(browser, "get") else browser


def fetch_ad_detail(browser, ad_id: str, page=None, dismiss: bool = True) -> tuple[dict | None, str]:
    """Open the single-ad page and parse it. Returns (detail, status): ok | login-wall | no-record | error:<type>.
    `page` lets one browser context serve a whole store's pass (a new context per ad costs seconds)."""
    ctx = None
    if page is None:
        ctx = new_context(_browser_of(browser))
        page = ctx.new_page()
    try:
        page.goto(detail_url(ad_id), wait_until="domcontentloaded", timeout=config.META_NAV_TIMEOUT_MS)
        meta_ads._wait(0.8, 1.6)
        if dismiss:   # cookie banner: once per context is enough (each probe costs ~3 s)
            meta_ads._dismiss_dialogs(page)
        u = page.url.lower()
        if "/login" in u or "/checkpoint" in u:
            return None, "login-wall"
        html = page.content()
        d = parse_ad_detail_html(html, ad_id)
        if d is None:
            d = parse_ad_detail_html(html)   # record present but under another id key
            if d is not None and d["ad_id"] != str(ad_id):
                d = None
        if d is None:
            return None, no_record_reason(html, ad_id)
        return d, "ok"
    except Exception as e:  # noqa: BLE001
        return None, f"error:{type(e).__name__}"
    finally:
        if ctx is not None:
            ctx.close()


# ---------------------------------------------------------------- creatives

def _hash_url(url: str, session: requests.Session | None = None, max_bytes: int | None = None) -> dict:
    max_bytes = config.META_CREATIVE_MAX_BYTES if max_bytes is None else max_bytes
    s = session or requests.Session()
    h = hashlib.sha256()
    n = 0
    with s.get(url, stream=True, timeout=config.REQUEST_TIMEOUT, headers={"User-Agent": meta_ads.USER_AGENTS[0]}) as r:
        r.raise_for_status()
        for chunk in r.iter_content(65536):
            if not chunk:
                continue
            take = chunk if n + len(chunk) <= max_bytes else chunk[: max_bytes - n]
            h.update(take)
            n += len(take)
            if n >= max_bytes:
                break
        scope = "full" if n < max_bytes else f"first_{max_bytes}"
    return {"sha256": h.hexdigest(), "bytes": n, "scope": scope}


def fingerprint_creatives(conn: sqlite3.Connection, ad_id: str, detail: dict, fetch=_hash_url,
                          session: requests.Session | None = None) -> dict:
    """Hash every creative of the ad not hashed before. Returns {hashed, cached, failed, first_hash}."""
    now = _utcnow()
    urls = ([("image", u) for u in detail.get("images") or []] + [("video", u) for u in detail.get("videos") or []])[:config.META_CREATIVES_PER_AD]
    pos = {"image": 0, "video": 0}
    hashed = cached = failed = 0
    first_hash = None
    for kind, url in urls:
        p = pos[kind]
        pos[kind] += 1
        row = conn.execute("SELECT sha256 FROM meta_creatives WHERE ad_id = ? AND kind = ? AND position = ?",
                           (ad_id, kind, p)).fetchone()
        if row and row["sha256"]:
            cached += 1
            first_hash = first_hash or row["sha256"]
            continue
        try:
            r = fetch(url, session) if session is not None else fetch(url)
            conn.execute("""INSERT OR REPLACE INTO meta_creatives (ad_id, kind, position, url, sha256, bytes, hash_scope, fetched_at, status)
                            VALUES (?,?,?,?,?,?,?,?,'ok')""", (ad_id, kind, p, url, r["sha256"], r["bytes"], r["scope"], now))
            hashed += 1
            first_hash = first_hash or r["sha256"]
        except Exception as e:  # noqa: BLE001
            failed += 1
            conn.execute("""INSERT OR REPLACE INTO meta_creatives (ad_id, kind, position, url, sha256, bytes, hash_scope, fetched_at, status)
                            VALUES (?,?,?,?,NULL,NULL,NULL,?,?)""", (ad_id, kind, p, url, now, f"error:{type(e).__name__}"[:80]))
    if first_hash:
        conn.execute("UPDATE meta_ads SET creative_hash = COALESCE(creative_hash, ?) WHERE ad_id = ?", (first_hash, ad_id))
    return {"hashed": hashed, "cached": cached, "failed": failed, "first_hash": first_hash}


def creative_lineage(conn: sqlite3.Connection, store_id: int, today: str) -> int:
    """Ads without a text lineage that share a creative hash with an older ad on the same page."""
    rows = conn.execute(
        """SELECT a.ad_id, a.page_id, a.page_name, a.ad_start_date, a.first_seen_date, a.lineage_of, c.sha256
           FROM meta_ads a JOIN meta_creatives c ON c.ad_id = a.ad_id AND c.sha256 IS NOT NULL
           WHERE a.store_id = ?""", (store_id,)).fetchall()
    by_key: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_key[(r["page_id"] or r["page_name"] or "", r["sha256"])].append(dict(r))
    n = 0
    for members in by_key.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (m["ad_start_date"] or m["first_seen_date"] or "", m["ad_id"]))
        parent = members[0]
        p_start = parent["ad_start_date"] or parent["first_seen_date"] or ""
        for m in members[1:]:
            if m["lineage_of"] or m["ad_id"] == parent["ad_id"]:
                continue
            if (m["ad_start_date"] or m["first_seen_date"] or "") <= p_start:
                continue   # launched together: siblings of one concept, not an iteration
            conn.execute("""UPDATE meta_ads SET lineage_of = ?, lineage_similarity = 1.0, lineage_via = 'creative'
                            WHERE ad_id = ? AND lineage_of IS NULL""", (parent["ad_id"], m["ad_id"]))
            m["lineage_of"] = parent["ad_id"]
            n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------- DB: detail readings, delivery, page likes

def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record_detail(conn: sqlite3.Connection, store_id: int, ad_id: str, today: str, detail: dict | None,
                  status: str, source: str = "detail") -> None:
    """One reading per ad per day. A 'detail' reading overrides a 'list' one; a 'list' reading never
    overwrites a 'detail' one from the same day."""
    existing = conn.execute("SELECT source FROM meta_ad_detail_daily WHERE snapshot_date = ? AND ad_id = ?",
                            (today, ad_id)).fetchone()
    if existing and existing["source"] == "detail" and source == "list":
        return
    d = detail or {}
    conn.execute(
        """INSERT OR REPLACE INTO meta_ad_detail_daily
           (snapshot_date, ad_id, store_id, source, start_date, end_date, is_active, page_like_count, status, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (today, ad_id, store_id, source, d.get("start_date"), d.get("end_date"), d.get("is_active"),
         d.get("page_like_count"), status, _utcnow()))
    if status == "removed":
        conn.execute("""UPDATE meta_ads SET delivery_status = 'off', detail_fetched_date = ?,
                          switched_off_date = COALESCE(switched_off_date, last_delivered, ad_end_date, ?) WHERE ad_id = ?""",
                     (today, today, ad_id))
    if detail:
        conn.execute("""UPDATE meta_ads SET page_profile_id = COALESCE(?, page_profile_id),
                          page_categories = COALESCE(?, page_categories), page_id = COALESCE(page_id, ?),
                          ad_end_date = COALESCE(?, ad_end_date), ad_start_date = COALESCE(ad_start_date, ?),
                          detail_fetched_date = CASE WHEN ? = 'detail' THEN ? ELSE detail_fetched_date END
                        WHERE ad_id = ?""",
                     (detail.get("page_profile_id"), json.dumps(detail["page_categories"]) if detail.get("page_categories") else None,
                      detail.get("page_id"), detail.get("end_date"), detail.get("start_date"), source, today, ad_id))


def delivery_state(readings: list[tuple[str, str | None]], today: str, stale_days: int = 2) -> dict:
    """readings: (snapshot_date, end_date) ascending. An ad is delivering while its end_date keeps
    advancing; it is off when the end_date has not moved for `stale_days`+ days, or lies that far in
    the past. switched_off_date = the last day it delivered (its final end_date)."""
    pts = [(d, e) for d, e in readings if e]
    if not pts:
        return {"last_delivered": None, "status": None, "switched_off_date": None}
    last_d, last_e = pts[-1]
    last_delivered = max(e for _, e in pts)
    t = date.fromisoformat(today)
    try:
        age = (t - date.fromisoformat(last_delivered)).days
    except ValueError:
        return {"last_delivered": last_delivered, "status": None, "switched_off_date": None}
    off = age >= stale_days
    if not off:
        # same end_date reported across readings spanning stale_days+ days
        first_same = next((d for d, e in pts if e == last_e), last_d)
        try:
            span = (date.fromisoformat(last_d) - date.fromisoformat(first_same)).days
        except ValueError:
            span = 0
        off = span >= stale_days
    return {"last_delivered": last_delivered, "status": "off" if off else "on",
            "switched_off_date": last_delivered if off else None}


def update_delivery(conn: sqlite3.Connection, ad_id: str, today: str) -> dict:
    rows = conn.execute(
        "SELECT snapshot_date, end_date, status FROM meta_ad_detail_daily WHERE ad_id = ? AND snapshot_date <= ? ORDER BY snapshot_date",
        (ad_id, today)).fetchall()
    readings = [(r[0], r[1]) for r in rows]
    st = delivery_state(readings, today)
    if rows and rows[-1]["status"] == "removed":
        st = {"last_delivered": st["last_delivered"], "status": "off", "switched_off_date": st["last_delivered"] or rows[-1][0]}
    if st["status"]:
        conn.execute("""UPDATE meta_ads SET last_delivered = ?, delivery_status = ?,
                          switched_off_date = CASE WHEN ? = 'off' THEN COALESCE(switched_off_date, ?) ELSE NULL END
                        WHERE ad_id = ?""", (st["last_delivered"], st["status"], st["status"], st["switched_off_date"], ad_id))
    return st


def likes_slope(points: list[tuple[str, int]]) -> float | None:
    """Likes per day between the first and last reading (dates ascending); None with < 2 readings."""
    pts = sorted((d, v) for d, v in points if v is not None)
    if len(pts) < 2:
        return None
    days = (date.fromisoformat(pts[-1][0]) - date.fromisoformat(pts[0][0])).days
    return round((pts[-1][1] - pts[0][1]) / days, 2) if days > 0 else None


def record_page_likes(conn: sqlite3.Connection, store_id: int, today: str) -> int:
    """Roll today's per-ad page_like_count readings up to one row per page, with deltas and slopes."""
    # the single-ad page's number wins over the list payload's for a page on a given day
    rows = conn.execute(
        """SELECT COALESCE(a.page_id, a.page_name) AS pid, a.page_name, MAX(a.page_categories) AS cats, MAX(a.page_profile_id) AS prof,
                  MAX(CASE WHEN d.source = 'detail' THEN d.page_like_count END) AS likes_detail,
                  MAX(CASE WHEN d.source = 'list' THEN d.page_like_count END) AS likes_list
           FROM meta_ad_detail_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
           WHERE d.store_id = ? AND d.snapshot_date = ? AND d.page_like_count IS NOT NULL
           GROUP BY COALESCE(a.page_id, a.page_name)""", (store_id, today)).fetchall()
    rows = [dict(r, likes=r["likes_detail"] if r["likes_detail"] is not None else r["likes_list"]) for r in rows]
    t = date.fromisoformat(today)
    n = 0
    for r in rows:
        hist = [(x[0], x[1]) for x in conn.execute(
            """SELECT snapshot_date, page_like_count FROM meta_page_likes_daily WHERE page_id = ? AND snapshot_date < ?
               AND snapshot_date >= ? ORDER BY snapshot_date""", (r["pid"], today, (t - timedelta(days=14)).isoformat()))]
        series = hist + [(today, r["likes"])]
        cur = [(d, v) for d, v in series if (t - date.fromisoformat(d)).days <= 7]
        prev = [(d, v) for d, v in series if 7 <= (t - date.fromisoformat(d)).days <= 14]
        delta = (r["likes"] - hist[-1][1]) if hist and hist[-1][1] is not None else None
        conn.execute(
            """INSERT OR REPLACE INTO meta_page_likes_daily
               (snapshot_date, page_id, store_id, page_name, page_like_count, likes_delta_1d, likes_slope_7d, likes_slope_prev_7d,
                page_categories, page_profile_id, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (today, r["pid"], store_id, r["page_name"], r["likes"], delta, likes_slope(cur), likes_slope(prev),
             r["cats"], r["prof"], _utcnow()))
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------- selection

def flagged_ad_ids(conn: sqlite3.Connection, store_id: int) -> set[str]:
    """Ads referenced by the store's alerts: rules 5/8 name the ad, 7 names (page, parent) and its
    lineage children, 6 names a concept."""
    out: set[str] = set()
    for r in conn.execute("SELECT dedupe_key, detail FROM alerts WHERE store_id = ? AND rule BETWEEN 5 AND 8", (store_id,)):
        key = r["dedupe_key"] or ""
        parts = key.split("|")
        if parts[0] in ("5", "8") and len(parts) > 1:
            out.add(parts[1])
        elif parts[0] == "7" and len(parts) > 2:
            parent = parts[-1]
            out.add(parent)
            out.update(x[0] for x in conn.execute("SELECT ad_id FROM meta_ads WHERE lineage_of = ?", (parent,)))
        elif parts[0] == "6" and len(parts) > 1:
            out.update(x[0] for x in conn.execute("SELECT ad_id FROM meta_ads WHERE concept_id = ?", (parts[1],)))
        for m in re.finditer(r"\bad (\d{8,20})", r["detail"] or ""):
            out.add(m.group(1))
    return out


def select_for_detail(conn: sqlite3.Connection, store_id: int, today: str, cap: int | None = None) -> list[dict]:
    """Which ads get their single-ad page fetched today, most valuable first:
    0 flagged by an alert, 1 started in the last 14 days, 2 first ad of a page not read today (page likes),
    3 everything else still delivering by last_seen; ads already off are re-checked weekly."""
    cap = config.META_DETAIL_MAX if cap is None else cap
    flagged = flagged_ad_ids(conn, store_id)
    t = date.fromisoformat(today)
    done_today = {r[0] for r in conn.execute(
        "SELECT ad_id FROM meta_ad_detail_daily WHERE store_id = ? AND snapshot_date = ? AND source = 'detail'", (store_id, today))}
    pages_read = {r[0] for r in conn.execute(
        """SELECT COALESCE(a.page_id, a.page_name) FROM meta_ad_detail_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
           WHERE d.store_id = ? AND d.snapshot_date = ? AND d.page_like_count IS NOT NULL""", (store_id, today))}
    rows = conn.execute(
        """SELECT ad_id, page_id, page_name, ad_start_date, first_seen_date, last_seen_date, delivery_status,
                  detail_fetched_date, page_ignored FROM meta_ads WHERE store_id = ? AND COALESCE(page_ignored, 0) = 0""",
        (store_id,)).fetchall()
    out = []
    for r in rows:
        if r["ad_id"] in done_today:
            continue
        if r["delivery_status"] == "off" and r["detail_fetched_date"]:
            if (t - date.fromisoformat(r["detail_fetched_date"])).days < 7:
                continue
        start = r["ad_start_date"] or r["first_seen_date"]
        try:
            age = (t - date.fromisoformat(start)).days
        except (TypeError, ValueError):
            age = 999
        pid = r["page_id"] or r["page_name"] or ""
        if r["ad_id"] in flagged:
            pri = 0
        elif age <= 14:
            pri = 1
        elif pid not in pages_read:
            pri = 2
            pages_read.add(pid)
        else:
            pri = 3
        out.append({"ad_id": r["ad_id"], "priority": pri, "age": age, "last_seen": r["last_seen_date"], "page": pid})
    out.sort(key=lambda x: (x["priority"], x["age"], x["last_seen"] or "", x["ad_id"]))
    return out[:cap]


# ---------------------------------------------------------------- orchestration

def fetch_store_details(conn: sqlite3.Connection, browser, store_id: int, today: str, cap: int | None = None,
                        fetch=fetch_ad_detail, hash_fetch=_hash_url, wait=None) -> dict:
    """Fetch the single-ad page for the selected ads, record readings, hash creatives. Stops on a login wall."""
    todo = select_for_detail(conn, store_id, today, cap)
    wait = wait or (lambda: meta_ads._wait(1, 2.5))
    counts = {"selected": len(todo), "ok": 0, "no_record": 0, "removed": 0, "errors": 0, "login_wall": 0, "hashed": 0,
              "hash_failed": 0, "relaunches": 0, "context_recycles": 0}
    session = requests.Session()
    live = bool(todo) and browser is not None and fetch is fetch_ad_detail
    holder = {"ctx": None, "page": None, "pages": 0}

    def fresh_page():
        if holder["ctx"] is not None:
            try:
                holder["ctx"].close()
            except Exception:  # noqa: BLE001
                pass
        holder["ctx"] = new_context(_browser_of(browser))
        holder["page"] = holder["ctx"].new_page()
        holder["pages"] = 0
        return holder["page"]

    if live:
        fresh_page()
    try:
        _fetch_loop(conn, browser, holder, fresh_page, store_id, today, todo, counts, fetch, hash_fetch, session, wait)
    finally:
        if holder["ctx"] is not None:
            try:
                holder["ctx"].close()
            except Exception:  # noqa: BLE001
                pass
    return counts


def _fetch_one(browser, item, holder, fresh_page, fetch, counts):
    """One fetch that survives a dead browser / context (relaunch + retry once) and recycles the
    context every META_DETAIL_CONTEXT_PAGES pages."""
    page = holder["page"]
    if page is None:
        return fetch(browser, item["ad_id"])
    if holder["pages"] >= config.META_DETAIL_CONTEXT_PAGES:
        page = fresh_page()
        counts["context_recycles"] += 1
    first = holder["pages"] == 0
    holder["pages"] += 1
    detail, status = fetch(browser, item["ad_id"], page, first)
    dead = False
    try:
        dead = page.is_closed() or not _browser_of(browser).is_connected()
    except Exception:  # noqa: BLE001
        dead = True
    if status.startswith("error") and dead:
        counts["relaunches"] += 1
        log.warning("browser/page died on ad %s; relaunching and retrying once", item["ad_id"])
        page = fresh_page()
        holder["pages"] += 1
        detail, status = fetch(browser, item["ad_id"], page, True)
    return detail, status


def _fetch_loop(conn, browser, holder, fresh_page, store_id, today, todo, counts, fetch, hash_fetch, session, wait) -> None:
    for i, item in enumerate(todo):
        detail, status = _fetch_one(browser, item, holder, fresh_page, fetch, counts)
        if status == "login-wall":
            counts["login_wall"] += 1
            log.error("Ad Library single-ad page shows a login wall; stopping the detail pass for today")
            record_detail(conn, store_id, item["ad_id"], today, None, status)
            conn.commit()
            break
        record_detail(conn, store_id, item["ad_id"], today, detail, status)
        if detail:
            counts["ok"] += 1
            fp = fingerprint_creatives(conn, item["ad_id"], detail, fetch=hash_fetch, session=session)
            counts["hashed"] += fp["hashed"]
            counts["hash_failed"] += fp["failed"]
        elif status == "removed":
            counts["removed"] += 1
        elif status.startswith("no-record"):
            counts["no_record"] += 1
        else:
            counts["errors"] += 1
        conn.commit()
        if i < len(todo) - 1:
            wait()


def detail_from_ad(a: dict) -> dict:
    """The reading shape from a normalised ad (meta_ads.normalise_ad), no raw payload needed."""
    return {"ad_id": str(a["ad_id"]), "start_date": a.get("start_date"), "end_date": a.get("end_date"),
            "is_active": None if a.get("is_active_flag") is None else (1 if a.get("is_active_flag") else 0),
            "page_id": a.get("page_id"), "page_name": a.get("page_name"), "page_like_count": a.get("page_like_count"),
            "page_profile_uri": None, "page_profile_id": None, "page_categories": [], "images": [], "videos": []}


def record_list_readings(conn: sqlite3.Connection, store_id: int, today: str, ads: list[dict]) -> int:
    """end_date / page_like_count from the search-results payload, for every ad scraped today.
    `ads` are normalised ads (preferred: the stored raw_json is truncated) or raw payload nodes."""
    n = 0
    for a in ads:
        try:
            d = detail_from_ad(a) if "ad_id" in a else detail_from_list_node(a)
        except Exception:  # noqa: BLE001
            continue
        if d["ad_id"] and (d["end_date"] or d["page_like_count"] is not None):
            record_detail(conn, store_id, d["ad_id"], today, d, "ok", source="list")
            n += 1
    conn.commit()
    return n


def finalize_store(conn: sqlite3.Connection, store_id: int, today: str) -> dict:
    """After the readings are in: delivery status per ad, page-likes rows, creative lineage, concept
    survival by delivery, rule 12."""
    ads = [r[0] for r in conn.execute("SELECT ad_id FROM meta_ads WHERE store_id = ?", (store_id,))]
    on = off = 0
    for aid in ads:
        st = update_delivery(conn, aid, today)
        if st["status"] == "on":
            on += 1
        elif st["status"] == "off":
            off += 1
    pages = record_page_likes(conn, store_id, today)
    lin = creative_lineage(conn, store_id, today)
    from . import ad_metrics
    ad_metrics.write_concept_rows(conn, store_id, today)
    alerts = run_alerts(conn, store_id, today)
    conn.commit()
    return {"delivering": on, "off": off, "pages": pages, "creative_lineage": lin, "alerts": len(alerts)}


# ---------------------------------------------------------------- alerts

def run_alerts(conn: sqlite3.Connection, store_id: int, today: str) -> list[dict]:
    found = []
    for r in conn.execute(
        """SELECT page_id, page_name, page_like_count, likes_slope_7d, likes_slope_prev_7d, likes_delta_1d
           FROM meta_page_likes_daily WHERE store_id = ? AND snapshot_date = ?""", (store_id, today)):
        cur, prev = r["likes_slope_7d"], r["likes_slope_prev_7d"]
        if cur is not None and prev is not None and prev >= config.META_LIKES_MIN_SLOPE and cur >= 2 * prev:
            found.append({"rule": 12, "handle": None, "key": f"12|{r['page_id']}",
                          "detail": f"page {r['page_name']} ({r['page_id']}): likes/day {prev:.1f} -> {cur:.1f} "
                                    f"(x{cur / prev:.1f}), {r['page_like_count']} likes"})
    now = _utcnow()
    t = date.fromisoformat(today)
    written = []
    for f in found:
        if conn.execute("SELECT 1 FROM alerts WHERE snapshot_date >= ? AND store_id = ? AND dedupe_key = ?",
                        ((t - timedelta(days=6)).isoformat(), store_id, f["key"])).fetchone():
            continue
        conn.execute("INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at, dedupe_key) VALUES (?,?,?,?,?,?,?)",
                     (today, store_id, f["handle"], f["rule"], f["detail"], now, f["key"]))
        written.append(f)
    conn.commit()
    return written
