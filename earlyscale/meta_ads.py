"""Meta Ad Library scrape -> SQLite (ads, ads_daily).

Strategy: drive the public Ad Library page with headless Chromium and capture the GraphQL responses it loads
while scrolling. Those carry structured ad records (ad_archive_id, page, start_date, is_active, landing link).
The 'Low impression count' badge is not in the payload: it is read from the rendered result cards. Layers:

  extract_ads(obj)           pure: find ad records anywhere in a JSON payload
  normalise_ad(node)         pure: flatten one record into the columns we keep
  scrape_page(...)           Playwright: open the search, scroll, collect, read the badges, detect blocks
  record_scrape(...)         SQLite: upsert ads, write today's rows, mark disappearances, derive url_daily
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

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

_short_name_warned: set[str] = set()


def store_query(store: dict) -> str:
    """What to type into the Ad Library search for a store: its Meta page name, or the domain when no usable name is
    set. A one- or two-character 'name' (a mis-parsed footer link) would match thousands of unrelated pages."""
    domain = (store.get("store_domain") or "").split("//")[-1]
    name = (store.get("meta_page_name") or "").strip()
    if len(name) < 3:
        if name and domain not in _short_name_warned:
            _short_name_warned.add(domain)
            log.warning("%s: meta_page_name %r is too short to be a page name; searching the domain instead "
                        "(fix it with `python tracker.py set-page %s --name \"<page name>\"`)", domain, name, domain)
        return domain
    return name


def build_search_url(query: str | None = None, page_id: str | None = None, active_only: bool = True,
                     country: str = "ALL", search_type: str | None = None) -> str:
    status = "active" if active_only else "all"
    base = f"{config.META_AD_LIBRARY_BASE}?active_status={status}&ad_type=all&country={quote(country)}&media_type=all"
    if page_id:
        return f"{base}&view_all_page_id={quote(str(page_id))}&search_type=page"
    if not query:
        raise ValueError("query or page_id required")
    if search_type is None:
        search_type = "keyword_unordered" if ("." in query and " " not in query) else "page"
    return f"{base}&q={quote(query)}&search_type={search_type}"


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
    """Facebook GraphQL bodies are often several JSON documents back to back (deferred payloads), sometimes
    prefixed with the `for (;;);` anti-hijack guard. Decode every document we can find."""
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
            i = j + 1
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


def normalise_ad(node: dict) -> dict:
    """The columns kept per ad. low_impressions is filled from the rendered card (apply_card_badges); the payload
    has no badge field, so it starts as None (unknown)."""
    snap = node.get("snapshot") or {}
    cards = snap.get("cards") or []
    first_card = cards[0] if cards else {}
    link = _first(snap.get("link_url"), first_card.get("link_url"))
    return {
        "ad_id": str(node.get("ad_archive_id")),
        "page_id": str(node.get("page_id") or "") or None,
        "page_name": node.get("page_name"),
        "start_date": _unix_to_date(node.get("start_date")),
        "end_date": _unix_to_date(node.get("end_date")),
        "is_active": 1 if node.get("is_active") in (True, 1, "true") else 0,
        "primary_text": _first(_text(snap.get("body")), _text(first_card.get("body"))),
        "landing_url": link,
        "landing_domain": urlparse(link).netloc.lower() if link else None,
        "low_impressions": None,
    }


# ---------------------------------------------------------------- browser

def launch_kwargs() -> dict:
    """META_CHROMIUM_PATH points at a specific binary (e.g. a pre-installed Chromium)."""
    return {"executable_path": config.META_CHROMIUM_PATH} if config.META_CHROMIUM_PATH else {}


class KeepAwake:
    """While a long pass runs, stop Windows from sleeping (the display may still turn off). No-op elsewhere."""
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
    """Lazily launched Chromium that is relaunched when it dies (a heavy page can take the whole browser
    process down; the pass must carry on with the next store instead of failing)."""

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


class LazyBrowser:
    """A BrowserHandle that starts Playwright only when a browser is first needed (a command that opens no page
    must not start and stop the driver for nothing)."""

    def __init__(self, headless: bool = True):
        self.headless, self.pw, self.handle, self.relaunches = headless, None, None, 0

    def get(self):
        if self.handle is None:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
            self.handle = BrowserHandle(self.pw, self.headless)
        b = self.handle.get()
        self.relaunches = self.handle.relaunches
        return b

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
        if self.pw is not None:
            try:
                self.pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self.pw = self.handle = None


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
    badges_read: int = 0       # ads whose card was seen (badge known)


def _wait(lo: float | None = None, hi: float | None = None) -> None:
    lo = config.META_WAIT_MIN if lo is None else lo
    hi = config.META_WAIT_MAX if hi is None else hi
    time.sleep(random.uniform(lo, hi))


def scrape_page(url: str, *, headless: bool = True, max_scrolls: int | None = None,
                max_ads: int | None = None, browser=None) -> ScrapeResult:
    """Open one Ad Library search and collect every ad record the page loads, reading the 'Low impression count'
    badge off every rendered card. Pass an existing Playwright `browser` to reuse it across stores (one concurrent
    browser, a fresh context + user agent per store)."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # local import: optional dep

    max_scrolls = config.META_MAX_SCROLLS if max_scrolls is None else max_scrolls
    max_ads = config.META_MAX_ADS if max_ads is None else max_ads
    result = ScrapeResult(url=url)
    nodes: dict[str, dict] = {}
    dom_badges: dict[str, bool] = {}

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

    def read_badges(page) -> None:
        try:
            dom_badges.update(read_card_badges(page))
        except Exception as e:  # noqa: BLE001
            log.debug("%s: card badge read failed: %s", _short(url), e)

    def run(b):
        ctx = b.new_context(user_agent=random.choice(USER_AGENTS), viewport={"width": 1366, "height": 850},
                            locale="en-US", timezone_id="America/New_York")
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=config.META_NAV_TIMEOUT_MS)
            _wait(2, 4)
            _dismiss_dialogs(page)
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
                read_badges(page)      # before the scroll: cards rendered so far, while they are still in the DOM
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
            try:
                page.wait_for_timeout(1500)            # let the last batch of cards render before the final read
            except Exception:  # noqa: BLE001
                pass
            read_badges(page)
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
    result.badges_read = apply_card_badges(result.ads, dom_badges)
    if result.blocked:
        raise MetaBlocked(result.note or "blocked")
    return result


CARD_BADGE_JS = r"""() => {
  // Every result card carries a 'Library ID: <id>' line. Walk up from that text to the card container and
  // report whether the card's text shows the 'Low impression count' badge.
  const out = {};
  const rx = /Library ID:?\s*(\d{3,20})/i;
  const badge = /low[ _-]?impression/i;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const m = rx.exec(node.nodeValue || "");
    if (!m) continue;
    let el = node.parentElement, card = null;
    for (let i = 0; i < 14 && el && el !== document.body; i++, el = el.parentElement) {
      const t = el.innerText || "";
      const ids = (t.match(/Library ID/gi) || []).length;
      if (ids > 1) break;                               // climbed past this card into the list
      if (el.getBoundingClientRect().height >= 200 || /Sponsored/i.test(t)) card = el;
    }
    const text = (card || node.parentElement).innerText || "";
    const id = m[1];
    out[id] = out[id] || badge.test(text);
  }
  return out;
}"""


def read_card_badges(page) -> dict[str, bool]:
    """{ad_id: has 'Low impression count' badge} for every result card rendered on the page."""
    got = page.evaluate(CARD_BADGE_JS) or {}
    return {str(k): bool(v) for k, v in got.items()}


def apply_card_badges(ads: list[dict], badges: dict[str, bool]) -> int:
    """1 for a card with the badge, 0 for a card seen without it; None (unknown) when the card never rendered."""
    n = 0
    for a in ads:
        v = badges.get(str(a.get("ad_id")))
        if v is None:
            continue
        a["low_impressions"] = 1 if v else 0
        n += 1
    return n


def _short(url: str) -> str:
    """The search term from an Ad Library URL, for log lines."""
    q = parse_qs(urlparse(url).query)
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

def record_scrape(conn: sqlite3.Connection, store_id: int, snapshot_date: str, ads: list[dict], query: str) -> dict:
    """Write one page's scrape: ads (upsert), today's ads_daily rows, still_active=0 rows for ads of this store
    that were not in today's results, then the store's url_daily rows for the day.
    Returns counts: new, seen, disappeared, total, badge_known."""
    from .winners import compute_url_daily, normalise_landing_path
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ids_today = {a["ad_id"] for a in ads}
    new = known = 0
    with conn:
        for pos, a in enumerate(ads):
            path = normalise_landing_path(a.get("landing_url"))
            row = conn.execute("SELECT first_seen FROM ads WHERE ad_id = ? AND store_id = ?", (a["ad_id"], store_id)).fetchone()
            if row is None:
                new += 1
                conn.execute("""INSERT INTO ads (ad_id, store_id, page_id, page_name, landing_url, landing_path, first_seen, first_scraped,
                                                 last_scraped, primary_text) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                             (a["ad_id"], store_id, a.get("page_id"), a.get("page_name"), a.get("landing_url"), path,
                              a.get("start_date") or snapshot_date, snapshot_date, snapshot_date, (a.get("primary_text") or "")[:500] or None))
            else:
                conn.execute("""UPDATE ads SET last_scraped = ?, page_id = COALESCE(?, page_id), page_name = COALESCE(?, page_name),
                                  landing_url = COALESCE(?, landing_url), landing_path = COALESCE(?, landing_path),
                                  first_seen = COALESCE(?, first_seen), primary_text = COALESCE(?, primary_text)
                                WHERE ad_id = ? AND store_id = ?""",
                             (snapshot_date, a.get("page_id"), a.get("page_name"), a.get("landing_url"), path, a.get("start_date"),
                              (a.get("primary_text") or "")[:500] or None, a["ad_id"], store_id))
            low = a.get("low_impressions")
            if low is None:      # a same-day re-run keeps the badge it read earlier
                prev = conn.execute("SELECT low_impressions FROM ads_daily WHERE snapshot_date = ? AND store_id = ? AND ad_id = ?",
                                    (snapshot_date, store_id, a["ad_id"])).fetchone()
                low = prev["low_impressions"] if prev else None
            known += 1 if low is not None else 0
            conn.execute("""INSERT OR REPLACE INTO ads_daily (snapshot_date, store_id, ad_id, still_active, low_impressions, position, fetched_at)
                            VALUES (?,?,?,?,?,?,?)""", (snapshot_date, store_id, a["ad_id"], a.get("is_active", 1), low, pos, now))
        gone = [r["ad_id"] for r in conn.execute("SELECT ad_id FROM ads WHERE store_id = ? AND last_scraped < ?", (store_id, snapshot_date))
                if r["ad_id"] not in ids_today]
        conn.executemany("""INSERT OR IGNORE INTO ads_daily (snapshot_date, store_id, ad_id, still_active, low_impressions, position, fetched_at)
                            VALUES (?,?,?,0,NULL,NULL,?)""", [(snapshot_date, store_id, aid, now) for aid in gone])
    urls = compute_url_daily(conn, store_id, snapshot_date)
    return {"new": new, "seen": len(ads) - new, "disappeared": len(gone), "total": len(ads), "badge_known": known, "urls": urls}


def record_page_run(conn: sqlite3.Connection, store_id: int, snapshot_date: str, query: str, status: str,
                    detail: str, ads_found: int, scrolls: int, duration_s: float) -> None:
    conn.execute(
        """INSERT INTO meta_page_runs (store_id, snapshot_date, query, status, detail, ads_found, scrolls, duration_s, ran_at)
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
