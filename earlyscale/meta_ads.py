"""Part B, increment 1: Meta Ad Library scrape -> SQLite (meta_ads, meta_ads_daily).

Strategy: drive the public Ad Library page with headless Chromium and capture the
GraphQL responses it loads while scrolling. Those responses carry structured ad
records (ad_archive_id, page, start_date, is_active, snapshot text/links/creatives,
EU reach), which is far more robust than scraping the obfuscated DOM. Layers:

  extract_ads(obj)           pure: find ad records anywhere in a JSON payload
  normalise_ad(node)         pure: flatten one record into our columns
  scrape_page(...)           Playwright: open the search, scroll, collect, detect blocks
  record_scrape(...)         SQLite: upsert ads, write today's rows, mark disappearances

Engagement (reactions/comments/shares) is NOT exposed by the Ad Library; the columns
exist and stay NULL unless a payload happens to include such counts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import quote, urlparse

from . import config

log = logging.getLogger("earlyscale.meta_ads")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]
BLOCK_MARKERS = ("you must log in", "log in to continue", "temporarily blocked", "sorry, something went wrong",
                 "this content isn't available", "checkpoint", "confirm you're human")


class MetaBlocked(Exception):
    """Meta served a login wall / rate-limit page instead of the Ad Library."""


class MetaScrapeError(Exception):
    pass


# ---------------------------------------------------------------- pure parsing

def build_search_url(query: str | None = None, page_id: str | None = None, active_only: bool = True) -> str:
    status = "active" if active_only else "all"
    base = f"{config.META_AD_LIBRARY_BASE}?active_status={status}&ad_type=all&country=ALL&media_type=all"
    if page_id:
        return f"{base}&view_all_page_id={quote(str(page_id))}&search_type=page"
    if not query:
        raise ValueError("query or page_id required")
    if "." in query and " " not in query:   # looks like a domain -> keyword search
        return f"{base}&q={quote(query)}&search_type=keyword_unordered"
    return f"{base}&q={quote(query)}&search_type=page"


def _walk(obj, seen: list):
    if isinstance(obj, dict):
        if "ad_archive_id" in obj and "snapshot" in obj:
            seen.append(obj)
            return
        for v in obj.values():
            _walk(v, seen)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, seen)


def extract_ads(payload) -> list[dict]:
    """Find every ad record (dict with ad_archive_id + snapshot) in a decoded JSON payload,
    whatever wrapper the Ad Library puts around it. Returns raw nodes, de-duplicated by id."""
    found: list = []
    _walk(payload, found)
    out, ids = [], set()
    for n in found:
        aid = str(n.get("ad_archive_id") or "")
        if aid and aid not in ids:
            ids.add(aid)
            out.append(n)
    return out


def parse_json_lines(text: str) -> list:
    """Facebook GraphQL bodies are often several JSON documents back to back (deferred
    payloads), sometimes prefixed with the `for (;;);` anti-hijack guard. Decode every
    document we can find, whatever the whitespace between them."""
    docs = []
    text = text.lstrip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        j = text.find("{", i)
        if j < 0:
            break
        try:
            doc, end = dec.raw_decode(text, j)
        except ValueError:
            i = j + 1          # not a document here; skip ahead
            continue
        docs.append(doc)
        i = end
    return docs


def _unix_to_date(v) -> str | None:
    if v in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("text") or v.get("markup", {}).get("__html") if isinstance(v.get("markup"), dict) else v.get("text")
    if isinstance(v, list):
        v = " ".join(str(x) for x in v if x)
    s = re.sub(r"<[^>]+>", " ", str(v))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _first(*vals):
    for v in vals:
        if v:
            return v
    return None


def _strip_query(url: str | None) -> str:
    if not url:
        return ""
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}{u.path}"


def normalise_ad(node: dict) -> dict:
    snap = node.get("snapshot") or {}
    cards = snap.get("cards") or []
    videos = snap.get("videos") or []
    images = snap.get("images") or []
    first_card = cards[0] if cards else {}
    body_text = _first(_text(snap.get("body")), _text(first_card.get("body")))
    headline = _first(_text(snap.get("title")), _text(first_card.get("title")))
    link = _first(snap.get("link_url"), first_card.get("link_url"), snap.get("link_description") and None)
    fmt = (snap.get("display_format") or "").upper()
    if videos or fmt == "VIDEO":
        ctype = "video"
    elif len(cards) > 1 or fmt in ("CAROUSEL", "MULTI_IMAGES"):
        ctype = "carousel"
    elif images or fmt == "IMAGE":
        ctype = "image"
    elif fmt:
        ctype = fmt.lower()
    else:
        ctype = "unknown"
    asset = _first(
        videos[0].get("video_preview_image_url") if videos else None,
        videos[0].get("video_hd_url") if videos else None,
        videos[0].get("video_sd_url") if videos else None,
        images[0].get("original_image_url") if images else None,
        images[0].get("resized_image_url") if images else None,
        first_card.get("original_image_url"), first_card.get("resized_image_url"),
        first_card.get("video_preview_image_url"),
    )
    aaa = node.get("aaa_info") or {}
    eu_reach = _first(aaa.get("eu_total_reach"), node.get("eu_total_reach"))
    fp_src = f"{_strip_query(asset)}|{(body_text or '').lower()[:500]}"
    social = {}
    for key in ("reactions", "comments", "shares"):
        for cand in (f"{key}_count", key, f"{key[:-1]}_count"):
            v = node.get(cand) if cand in node else snap.get(cand)
            if isinstance(v, (int, float)):
                social[key] = int(v)
                break
    return {
        "ad_id": str(node.get("ad_archive_id")),
        "page_id": str(node.get("page_id") or "") or None,
        "page_name": node.get("page_name"),
        "start_date": _unix_to_date(node.get("start_date")),
        "end_date": _unix_to_date(node.get("end_date")),
        "is_active": 1 if node.get("is_active") in (True, 1, "true") else 0,
        "primary_text": body_text,
        "headline": headline,
        "landing_url": link,
        "landing_domain": urlparse(link).netloc.lower() if link else None,
        "caption": _text(snap.get("caption")),
        "cta": _text(snap.get("cta_text")),
        "creative_type": ctype,
        "asset_url": asset,
        "platforms": ",".join(node.get("publisher_platform") or []) or None,
        "collation_count": node.get("collation_count"),
        "eu_total_reach": int(eu_reach) if isinstance(eu_reach, (int, float)) else None,
        "reactions": social.get("reactions"),
        "comments": social.get("comments"),
        "shares": social.get("shares"),
        "fingerprint": hashlib.sha1(fp_src.encode("utf-8")).hexdigest()[:16],
        "raw_json": json.dumps(node, ensure_ascii=False)[:20000],
    }


# ---------------------------------------------------------------- browser

def launch_kwargs() -> dict:
    """Extra Chromium launch options: META_CHROMIUM_PATH points at a specific binary
    (e.g. a pre-installed Chromium when Playwright's own download is unavailable)."""
    return {"executable_path": config.META_CHROMIUM_PATH} if config.META_CHROMIUM_PATH else {}

@dataclass
class ScrapeResult:
    url: str
    ads: list[dict] = field(default_factory=list)
    scrolls: int = 0
    responses: int = 0
    blocked: bool = False
    note: str = ""


def _wait(lo: float | None = None, hi: float | None = None) -> None:
    lo = config.META_WAIT_MIN if lo is None else lo
    hi = config.META_WAIT_MAX if hi is None else hi
    time.sleep(random.uniform(lo, hi))


def scrape_page(url: str, *, headless: bool = True, max_scrolls: int | None = None,
                max_ads: int | None = None, browser=None) -> ScrapeResult:
    """Open one Ad Library search and collect every ad record the page loads.

    Pass an existing Playwright `browser` to reuse it across stores (one concurrent
    browser, a fresh context + user agent per store)."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # local import: optional dep

    max_scrolls = config.META_MAX_SCROLLS if max_scrolls is None else max_scrolls
    max_ads = config.META_MAX_ADS if max_ads is None else max_ads
    result = ScrapeResult(url=url)
    nodes: dict[str, dict] = {}

    def ingest(text: str) -> int:
        before = len(nodes)
        for doc in parse_json_lines(text):
            for n in extract_ads(doc):
                nodes.setdefault(str(n["ad_archive_id"]), n)
        return len(nodes) - before

    def on_response(resp):
        try:
            if "graphql" not in resp.url and "ads/library" not in resp.url:
                return
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype and "javascript" not in ctype and "text" not in ctype:
                return
            body = resp.text()
        except Exception:  # noqa: BLE001 - detached responses, binary bodies
            return
        if "ad_archive_id" in body:
            result.responses += 1
            ingest(body)

    def run(b):
        ctx = b.new_context(user_agent=random.choice(USER_AGENTS), viewport={"width": 1366, "height": 850},
                            locale="en-US", timezone_id="America/New_York")
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=config.META_NAV_TIMEOUT_MS)
            _wait(2, 4)
            _dismiss_dialogs(page)
            # ads embedded in the initial HTML
            for html in page.evaluate(
                "() => Array.from(document.querySelectorAll('script[type=\"application/json\"]')).map(s => s.textContent)"
            ):
                if html and "ad_archive_id" in html:
                    ingest(html)
            _check_blocked(page, result)
            if result.blocked:
                return
            stale = 0
            for i in range(max_scrolls):
                before = len(nodes)
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                result.scrolls += 1
                _wait()
                _check_blocked(page, result)
                if result.blocked or len(nodes) >= max_ads:
                    break
                stale = stale + 1 if len(nodes) == before else 0
                if stale >= 2:
                    result.note = "no new ads after 2 scrolls"
                    break
            else:
                result.note = f"hit max_scrolls={max_scrolls}"
        except PWTimeout as e:
            raise MetaScrapeError(f"navigation timeout: {e}") from None
        finally:
            ctx.close()

    if browser is not None:
        run(browser)
    else:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=headless, **launch_kwargs())
            try:
                run(b)
            finally:
                b.close()
    result.ads = [normalise_ad(n) for n in nodes.values()]
    if result.blocked:
        raise MetaBlocked(result.note or "blocked")
    return result


def _dismiss_dialogs(page) -> None:
    """Best-effort: cookie consent / 'log in' modals that cover the results."""
    for label in ("Decline optional cookies", "Allow all cookies", "Only allow essential cookies", "Close"):
        try:
            btn = page.get_by_role("button", name=label, exact=False).first
            if btn.is_visible(timeout=800):
                btn.click(timeout=1500)
                _wait(1, 2)
                break
        except Exception:  # noqa: BLE001
            continue


def _check_blocked(page, result: ScrapeResult) -> None:
    try:
        url = page.url.lower()
        if "/login" in url or "/checkpoint" in url:
            result.blocked, result.note = True, f"redirected to {page.url}"
            return
        title = (page.title() or "").lower()
        body = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 4000).toLowerCase()")
        for m in BLOCK_MARKERS:
            if m in title or m in body:
                result.blocked, result.note = True, f"page shows '{m}'"
                return
    except Exception:  # noqa: BLE001
        return


# ---------------------------------------------------------------- SQLite

def record_scrape(conn: sqlite3.Connection, store_id: int, snapshot_date: str, ads: list[dict],
                  query: str) -> dict:
    """Write one page's scrape. Returns counts: new, seen, disappeared."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ids_today = {a["ad_id"] for a in ads}
    new = 0
    with conn:
        for pos, a in enumerate(ads):
            existing = conn.execute("SELECT ad_id FROM meta_ads WHERE ad_id = ?", (a["ad_id"],)).fetchone()
            if existing is None:
                new += 1
                conn.execute(
                    """INSERT INTO meta_ads (ad_id, store_id, page_id, page_name, ad_start_date, ad_end_date,
                         first_seen_date, last_seen_date, primary_text, headline, landing_url, landing_domain,
                         caption, cta, creative_type, asset_url, platforms, fingerprint, query, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (a["ad_id"], store_id, a["page_id"], a["page_name"], a["start_date"], a["end_date"],
                     snapshot_date, snapshot_date, a["primary_text"], a["headline"], a["landing_url"],
                     a["landing_domain"], a["caption"], a["cta"], a["creative_type"], a["asset_url"],
                     a["platforms"], a["fingerprint"], query, a["raw_json"]))
            else:
                conn.execute(
                    """UPDATE meta_ads SET last_seen_date = ?, ad_end_date = COALESCE(?, ad_end_date),
                         page_name = COALESCE(?, page_name), primary_text = COALESCE(?, primary_text),
                         headline = COALESCE(?, headline), landing_url = COALESCE(?, landing_url),
                         landing_domain = COALESCE(?, landing_domain), asset_url = COALESCE(?, asset_url),
                         raw_json = ? WHERE ad_id = ?""",
                    (snapshot_date, a["end_date"], a["page_name"], a["primary_text"], a["headline"],
                     a["landing_url"], a["landing_domain"], a["asset_url"], a["raw_json"], a["ad_id"]))
            conn.execute(
                """INSERT OR REPLACE INTO meta_ads_daily
                   (snapshot_date, ad_id, store_id, is_active, position, eu_total_reach, reactions, comments,
                    shares, collation_count, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_date, a["ad_id"], store_id, a["is_active"], pos, a["eu_total_reach"], a["reactions"],
                 a["comments"], a["shares"], a["collation_count"], now))
        # ads seen on an earlier day for this store that did not show up today -> inactive row
        gone = [r["ad_id"] for r in conn.execute(
            "SELECT ad_id FROM meta_ads WHERE store_id = ? AND last_seen_date < ?", (store_id, snapshot_date))
                if r["ad_id"] not in ids_today]
        for aid in gone:
            conn.execute(
                """INSERT OR IGNORE INTO meta_ads_daily (snapshot_date, ad_id, store_id, is_active, fetched_at)
                   VALUES (?,?,?,0,?)""", (snapshot_date, aid, store_id, now))
    return {"new": new, "seen": len(ads) - new, "disappeared": len(gone), "total": len(ads)}


def record_page_run(conn: sqlite3.Connection, store_id: int, snapshot_date: str, query: str, status: str,
                    detail: str, ads_found: int, scrolls: int, duration_s: float) -> None:
    conn.execute(
        """INSERT INTO meta_page_runs (store_id, snapshot_date, query, status, detail, ads_found, scrolls,
                                       duration_s, ran_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (store_id, snapshot_date, query, status, detail[:500], ads_found, scrolls, round(duration_s, 1),
         datetime.now(timezone.utc).replace(microsecond=0).isoformat()))
    conn.commit()


def days_running(ad_start: str | None, first_seen: str, as_of: str) -> int | None:
    start = ad_start or first_seen
    try:
        return (date.fromisoformat(as_of) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None
