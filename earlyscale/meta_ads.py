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


REACH_KEY_RE = re.compile(r"reach", re.I)
POST_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|web\.)?(?:facebook|instagram)\.com[^\s\"'<>]*?"
    r"(?:/posts/|/videos/|/reel/|/reels/|/photos/|permalink\.php\?story_fbid=|story\.php\?story_fbid=|/p/)[^\s\"'<>]*",
    re.I)
POST_ID_KEYS = ("post_id", "story_id", "boosted_post_id", "root_post_id", "source_post_id")


def _walk_items(obj, path=""):
    """Yield (path, key, value) for every dict entry in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from _walk_items(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_items(v, f"{path}[{i}]")


def extract_reach(node: dict) -> dict:
    """EU exact reach, UK exact reach, and any reach *range*, wherever the Ad Library puts them.

    Known: aaa_info.eu_total_reach (int) and aaa_info.age_country_gender_reach_breakdown
    (per-country rows; GB summed gives an exact UK figure when present). Anything else with
    "reach" in its key is recorded too: ints as exact, {lower_bound, upper_bound} as a range.
    `keys` lists what was found so the coverage report can show it."""
    out = {"eu_reach": None, "uk_reach": None, "range_lower": None, "range_upper": None, "source": None, "keys": []}
    for path, k, v in _walk_items(node):
        if not REACH_KEY_RE.search(k):
            continue
        full = f"{path}.{k}" if path else k
        kl = k.lower()
        if isinstance(v, bool):
            continue
        if v is None:
            out["keys"].append(f"{full}=null")
            continue
        if isinstance(v, (int, float)):
            out["keys"].append(f"{full}={int(v)}")
            if kl == "eu_total_reach" and out["eu_reach"] is None:
                out["eu_reach"] = int(v)
            elif ("uk" in kl or "gb" in kl) and out["uk_reach"] is None:
                out["uk_reach"] = int(v)
        elif isinstance(v, dict) and ("lower_bound" in v or "upper_bound" in v):
            lo, hi = v.get("lower_bound"), v.get("upper_bound")
            out["keys"].append(f"{full}=[{lo}..{hi}]")
            if out["range_lower"] is None and (lo is not None or hi is not None):
                try:
                    out["range_lower"] = int(lo) if lo is not None else None
                    out["range_upper"] = int(hi) if hi is not None else None
                    out["source"] = full
                except (TypeError, ValueError):
                    pass
        elif isinstance(v, list) and kl == "age_country_gender_reach_breakdown":
            gb = 0
            found = False
            for row in v:
                if isinstance(row, dict) and str(row.get("country", "")).upper() in ("GB", "UK"):
                    found = True
                    for ag in row.get("age_gender_breakdowns") or []:
                        for g in ("male", "female", "unknown"):
                            val = ag.get(g)
                            if isinstance(val, (int, float)):
                                gb += int(val)
            out["keys"].append(f"{full}:{len(v)} countries" + (f" GB={gb}" if found else ""))
            if found and out["uk_reach"] is None:
                out["uk_reach"] = gb
    if out["eu_reach"] is not None:
        out["source"] = "eu_total_reach" if out["source"] is None else out["source"]
    elif out["uk_reach"] is not None and out["source"] is None:
        out["source"] = "uk"
    out["keys"] = ",".join(out["keys"])[:400] or None
    return out


def extract_post_url(node: dict) -> str | None:
    """Underlying Page/Instagram post for a boosted-post ad, if the payload exposes one."""
    snap = node.get("snapshot") or {}
    for key in POST_ID_KEYS:
        v = node.get(key) or snap.get(key)
        if v and str(v).isdigit():
            page_id = node.get("page_id") or snap.get("page_id")
            return f"https://www.facebook.com/{page_id}/posts/{v}" if page_id else f"https://www.facebook.com/{v}"
    rs = snap.get("root_reshared_post")
    if isinstance(rs, dict):
        for key in ("url", "permalink", "permalink_url", "link_url"):
            if rs.get(key):
                return str(rs[key])
        if rs.get("id") and str(rs["id"]).isdigit():
            return f"https://www.facebook.com/{rs['id']}"
    for _, k, v in _walk_items(node):
        if isinstance(v, str) and ("facebook.com" in v or "instagram.com" in v):
            m = POST_URL_RE.search(v)
            if m and "ads/library" not in m.group(0):
                return m.group(0)
    return None


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
    reach = extract_reach(node)
    eu_reach = reach["eu_reach"]
    # Meta gives every ad its own asset URL, so hashing the asset made every fingerprint unique.
    # Hash the copy instead: identical text+headline across ads/pages = same creative concept.
    fp_src = f"{(headline or '').lower().strip()}|{(body_text or '').lower()[:500]}"
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
        "uk_reach": reach["uk_reach"],
        "reach_range_lower": reach["range_lower"],
        "reach_range_upper": reach["range_upper"],
        "reach_source": reach["source"],
        "reach_keys": reach["keys"],
        "post_url": extract_post_url(node),
        "page_like_count": (lambda v: int(v) if isinstance(v, (int, float)) else None)(node.get("page_like_count", snap.get("page_like_count"))),
        "is_active_flag": node.get("is_active"),
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

class KeepAwake:
    """While a long pass runs, stop Windows from sleeping (the display may still turn off).
    A sleeping laptop pauses the pass but not the wall-clock budget: one night showed a 3-minute
    step taking 5.8 hours. No-op on other platforms or if the call fails. Use as a context manager."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __enter__(self):
        self.active = False
        try:
            import ctypes
            if hasattr(ctypes, "windll"):
                ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
                self.active = True
        except Exception:  # noqa: BLE001
            self.active = False
        return self

    def __exit__(self, *exc):
        if self.active:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
            except Exception:  # noqa: BLE001
                pass
        return False


class BrowserHandle:
    """Lazily launched Chromium that is relaunched when it dies (a heavy page can take the whole
    browser process down; the pass must carry on with the next ad / store instead of failing)."""

    def __init__(self, pw, headless: bool = True):
        self.pw, self.headless, self.browser, self.relaunches = pw, headless, None, 0

    def get(self):
        if self.browser is None or not self.browser.is_connected():
            if self.browser is not None:
                self.relaunches += 1
                log.warning("browser died; relaunching (%d)", self.relaunches)
            self.browser = self.pw.chromium.launch(headless=self.headless, **launch_kwargs())
        return self.browser

    def close(self) -> None:
        try:
            if self.browser is not None and self.browser.is_connected():
                self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        self.browser = None


def browser_closed_error(e: BaseException) -> bool:
    m = str(e).lower()
    return "has been closed" in m or "target closed" in m or "browser closed" in m or "connection closed" in m


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
            log.info("%s: page open, %d ads in the first load; scrolling (%d-%ds between scrolls, up to %d scrolls)",
                     _short(url), len(nodes), int(config.META_WAIT_MIN), int(config.META_WAIT_MAX), max_scrolls)
            for i in range(max_scrolls):
                before = len(nodes)
                page.evaluate("() => window.scrollTo(0, (document.scrollingElement || document.body || document.documentElement || {scrollHeight: 100000}).scrollHeight)")
                result.scrolls += 1
                _wait()
                if (i + 1) % 5 == 0:
                    log.info("%s: scroll %d/%d, %d ads so far", _short(url), i + 1, max_scrolls, len(nodes))
                _check_blocked(page, result)
                if result.blocked:
                    break
                if len(nodes) >= max_ads:
                    result.note = f"stopped at max_ads={max_ads} (page has more)"
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


def _short(url: str) -> str:
    """The search term from an Ad Library URL, for log lines."""
    from urllib.parse import parse_qs, urlparse as _up
    q = parse_qs(_up(url).query)
    return (q.get("q") or q.get("view_all_page_id") or ["?"])[0]


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
                """UPDATE meta_ads SET post_url = COALESCE(?, post_url), reach_keys = ?,
                     engagement_type = CASE WHEN COALESCE(?, post_url) IS NULL THEN 'dark' ELSE 'boosted' END
                   WHERE ad_id = ?""",
                (a.get("post_url"), a.get("reach_keys"), a.get("post_url"), a["ad_id"]))
            prev = conn.execute("SELECT reactions, comments, shares, post_status FROM meta_ads_daily WHERE snapshot_date = ? AND ad_id = ?",
                                (snapshot_date, a["ad_id"])).fetchone()
            conn.execute(
                """INSERT OR REPLACE INTO meta_ads_daily
                   (snapshot_date, ad_id, store_id, is_active, position, eu_total_reach, uk_reach, reach_range_lower,
                    reach_range_upper, reach_source, reactions, comments, shares, post_status, collation_count, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_date, a["ad_id"], store_id, a["is_active"], pos, a["eu_total_reach"], a.get("uk_reach"),
                 a.get("reach_range_lower"), a.get("reach_range_upper"), a.get("reach_source"),
                 a["reactions"] if a["reactions"] is not None else (prev["reactions"] if prev else None),
                 a["comments"] if a["comments"] is not None else (prev["comments"] if prev else None),
                 a["shares"] if a["shares"] is not None else (prev["shares"] if prev else None),
                 prev["post_status"] if prev else None, a["collation_count"], now))
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


# ---------------------------------------------------------------- boosted posts (browser, best effort)

_COUNT_PATTERNS = {
    "comments": [r'"comment_count"\s*:\s*\{\s*"total_count"\s*:\s*(\d+)', r'"comments"\s*:\s*\{\s*"total_count"\s*:\s*(\d+)',
                 r'"comment_count"\s*:\s*(\d+)', r'"commentCount"\s*:\s*(\d+)', r'(\d[\d,.]*[KkMm]?)\s+comments?\b'],
    "reactions": [r'"reaction_count"\s*:\s*\{\s*"count"\s*:\s*(\d+)', r'"reactions"\s*:\s*\{\s*"count"\s*:\s*(\d+)',
                  r'"reaction_count"\s*:\s*(\d+)', r'"like_count"\s*:\s*(\d+)'],
    "shares": [r'"share_count"\s*:\s*\{\s*"count"\s*:\s*(\d+)', r'"share_count"\s*:\s*(\d+)', r'(\d[\d,.]*[KkMm]?)\s+shares?\b'],
}


def _to_int(text: str) -> int | None:
    t = text.replace(",", "").strip()
    mult = 1
    if t[-1:].lower() == "k":
        mult, t = 1000, t[:-1]
    elif t[-1:].lower() == "m":
        mult, t = 1_000_000, t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        return None


def parse_post_counts(html: str) -> dict:
    """Pull comment / reaction / share counts out of a Facebook post page's HTML, if present."""
    out = {}
    for key, pats in _COUNT_PATTERNS.items():
        for pat in pats:
            m = re.search(pat, html, re.I)
            if m:
                v = _to_int(m.group(1))
                if v is not None:
                    out[key] = v
                    break
    return out


def fetch_post_counts(browser, url: str) -> tuple[dict, str]:
    """Open a public post in a fresh context and read its counts. Returns (counts, status):
    status is 'ok', 'login-wall', 'no-counts' or 'error:<reason>'."""
    ctx = browser.new_context(user_agent=random.choice(USER_AGENTS), viewport={"width": 1366, "height": 850},
                              locale="en-US")
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.META_NAV_TIMEOUT_MS)
        _wait(2, 4)
        _dismiss_dialogs(page)
        if "/login" in page.url.lower() or "/checkpoint" in page.url.lower():
            return {}, "login-wall"
        html = page.content()
        counts = parse_post_counts(html)
        if not counts:
            body = (page.evaluate("() => document.body ? document.body.innerText : ''") or "")[:20000]
            counts = parse_post_counts(body)
        return counts, ("ok" if counts else "no-counts")
    except Exception as e:  # noqa: BLE001
        return {}, f"error:{type(e).__name__}:{str(e).splitlines()[0][:60] if str(e) else ''}"
    finally:
        ctx.close()


def fetch_boosted_engagement(conn: sqlite3.Connection, browser, store_id: int, snapshot_date: str,
                             max_posts: int | None = None) -> dict:
    """For today's active boosted ads, fetch each underlying post once per day and store counts."""
    max_posts = config.META_MAX_POSTS if max_posts is None else max_posts
    rows = conn.execute(
        """SELECT a.ad_id, a.post_url FROM meta_ads a JOIN meta_ads_daily d ON d.ad_id = a.ad_id AND d.snapshot_date = ?
           WHERE a.store_id = ? AND a.post_url IS NOT NULL AND d.is_active = 1 AND d.post_status IS NULL
             AND COALESCE(a.page_ignored, 0) = 0 ORDER BY d.position""", (snapshot_date, store_id)).fetchall()
    seen: dict[str, tuple[dict, str]] = {}
    fetched = ok = 0
    for r in rows:
        url = r["post_url"]
        if url not in seen:
            if fetched >= max_posts:
                break
            seen[url] = fetch_post_counts(browser, url)
            fetched += 1
            _wait()
        counts, status = seen[url]
        if counts:
            ok += 1
        conn.execute(
            """UPDATE meta_ads_daily SET reactions = COALESCE(?, reactions), comments = COALESCE(?, comments),
                 shares = COALESCE(?, shares), post_status = ? WHERE snapshot_date = ? AND ad_id = ?""",
            (counts.get("reactions"), counts.get("comments"), counts.get("shares"), status, snapshot_date, r["ad_id"]))
    conn.commit()
    return {"candidates": len(rows), "fetched": fetched, "with_counts": ok}
