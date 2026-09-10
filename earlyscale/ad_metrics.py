"""Part B, increment 2: turn raw Ad Library rows into signals.

  resolve_landing(...)     landing URL -> product handle (direct /products/<handle>, or fetch the
                           page and read its buy links); /pages/<handle> advertorials keep page_handle
  cluster_concepts(...)    same page + launch date within 1 day + same landing URL = one concept
  find_lineage(...)        new ad whose copy is > 0.7 similar to an ad older than 14 days on the same page
  compute_daily(...)       per-ad days_running / engagement deltas, per-concept survival, writes tables
  run_alerts(...)          rules 5-7 into the alerts table (+ alerts/YYYY-MM-DD.md)
  meta_for_signals(...)    per-handle numbers for the Signals tab (ads_pointing_here, ...)

Everything that decides something is a pure function over dicts so it can be unit-tested
on fixtures; the DB wrappers only load/store rows.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

from . import config, db as _db, shopify

log = logging.getLogger("earlyscale.ad_metrics")

RULES = {
    2: "store ad velocity: launches this week >= 2x last week (and >= 3)",
    5: "engagement_per_day up 2x week over week on one ad",
    6: "concept fully alive 14+ days while the page's active concepts fell",
    7: "new ad with lineage to an ad running 20+ days",
    8: "EU reach 7-day slope doubled vs the prior 7 days on one ad",
}
PRODUCT_RE = re.compile(r"/products/([a-z0-9][a-z0-9\-_.%]*)", re.I)
PAGE_RE = re.compile(r"/pages/([a-z0-9][a-z0-9\-_.%]*)", re.I)
VARIANT_RE = re.compile(
    r"(?:[?&]id=|/cart/add/?\?[^\"']*?id=|variant=|\"variant_id\"\s*:\s*\"?|variantId[\"']?\s*[:=]\s*[\"']?"
    r"|name=[\"']id[\"'][^>]*?value=[\"']|value=[\"'](?=\d{9,16}[\"'][^>]*?name=[\"']id[\"']))(\d{9,16})", re.I)
HTML_PRODUCT_JSON_RE = re.compile(r"\"handle\"\s*:\s*\"([a-z0-9\-_.]+)\"", re.I)


def _norm_domain(d: str | None) -> str:
    d = (d or "").lower().split("//")[-1].split("/")[0].split(":")[0]
    return d[4:] if d.startswith("www.") else d


def same_store(landing_domain: str | None, store_domain: str) -> bool:
    a, b = _norm_domain(landing_domain), _norm_domain(store_domain)
    return bool(a) and (a == b or a.endswith("." + b))


# ---------------------------------------------------------------- landing URL -> product (pure)

GENERIC_PRODUCT_RE = re.compile(r"/(?:product|shop/p|p|item|items|store/p|producto|produkt|artikel)/([a-z0-9][a-z0-9\-_.%]*)", re.I)


def handle_from_url(url: str | None) -> tuple[str | None, str | None]:
    """(product_handle, page_handle) straight from the URL path, if present. Shopify's /products/<handle> and the
    product paths of the other platforms (/product/<slug>, /shop/p/<slug>, /p/<slug>, ...)."""
    if not url:
        return None, None
    path = urlparse(url).path
    m = PRODUCT_RE.search(path) or GENERIC_PRODUCT_RE.search(path)
    if m:
        return clean_handle(m.group(1)), None
    m = PAGE_RE.search(path)
    if m:
        return None, clean_handle(m.group(1))
    return None, None


def clean_handle(raw: str) -> str:
    """Shopify handles may contain ® ™ etc.; ad URLs percent-encode them. Decode and lower-case
    so 'nervana-%C2%AE-magnesium-patches' matches the products.json handle."""
    h = unquote(raw).lower().rstrip("-")
    if h.endswith(".json") or h.endswith(".js"):
        h = h.rsplit(".", 1)[0]
    return h


# Handles that are app widgets rather than products: never let them win a page-fetch resolution.
JUNK_HANDLE_RE = re.compile(r"(shipping[-_]?protection|package[-_]?protection|route[-_]?package|order[-_]?protection|"
                            r"insurance|gift[-_]?card|tip[-_]?jar|^tip$|^tips$|priority[-_]?processing|extended[-_]?warranty)", re.I)


def is_junk_handle(h: str | None) -> bool:
    return bool(h) and bool(JUNK_HANDLE_RE.search(h))


CHROME_RE = re.compile(r"<(header|nav|footer)\b[^>]*>.*?</\1\s*>", re.I | re.S)     # menus and footers link every product
ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.I | re.S)
HREF_PRODUCT_RE = re.compile(r"href=[\"']([^\"']*/products/([a-z0-9][a-z0-9\-_.%]*)[^\"']*)", re.I)
CTA_RE = re.compile(r"add\s*to\s*(?:cart|bag)|\bbuy\b|\border\b|shop\s*now|get\s*(?:yours|started|it|mine|my|the\s*offer|\d+)|claim|"
                    r"check\s*out|\btry\b|purchase|subscribe|select\s*(?:package|bundle|offer|plan)|choose\s*(?:your|a|package|bundle)|"
                    r"yes[,!]?\s*i\s*want|\bgrab\b|start\s*(?:now|my|your)|unlock|apply\s*discount|save\s*\d", re.I)
BTN_ATTR_RE = re.compile(r"(?:class|id|data-[a-z\-]+)=[\"'][^\"']*(?:btn|button|cta|buy|order|checkout|add-to-cart|purchase)", re.I)
CART_PERMALINK_RE = re.compile(r"/cart/(\d{9,16}):\d+", re.I)
TAG_RE = re.compile(r"<[^>]+>")
W_LINK, W_JSON, W_CTA, W_BUY = 1, 1, 4, 6
RESOLVER_VERSION = 2   # landing_pages.resolver: 2 = CTA-weighted (bump when the rules change; refresh-all re-fetches older rows)


def handles_from_html(html: str, variant_to_handle: dict[int, str] | None = None) -> list[tuple[str, int]]:
    """Product handles referenced by a landing page, strongest evidence first: what the page SELLS, not what it
    links to. A buy action (a /cart/add form or /cart/<variant>:<qty> link with a known variant, a ?variant= link)
    weighs W_BUY; a link whose text or class is a call to action ("Order now", "Add to cart", class=btn) weighs
    W_CTA on top of the link; plain links and embedded product JSON weigh 1 each. Links inside <header>, <nav> and
    <footer> (menus that list every product) are ignored unless nothing else names a product."""
    counts: dict[str, int] = defaultdict(int)
    body = CHROME_RE.sub(" ", html)

    def add(raw: str, w: int) -> None:
        h = clean_handle(raw)
        if h and h not in ("json", "js"):
            counts[h] += w

    for m in PRODUCT_RE.finditer(body):
        add(m.group(1), W_LINK)
    for m in ANCHOR_RE.finditer(body):
        attrs, inner = m.group(1), m.group(2)
        hm = HREF_PRODUCT_RE.search(attrs)
        if not hm:
            continue
        text = TAG_RE.sub(" ", inner)
        if "variant=" in hm.group(1).lower():
            add(hm.group(2), W_BUY)
        elif CTA_RE.search(text) or BTN_ATTR_RE.search(attrs) or CTA_RE.search(attrs):
            add(hm.group(2), W_CTA)
    if not counts:
        for m in GENERIC_PRODUCT_RE.finditer(body):
            add(m.group(1), W_LINK)
    if variant_to_handle:
        for m in VARIANT_RE.finditer(body):
            h = variant_to_handle.get(int(m.group(1)))
            if h:
                counts[h] += W_BUY   # a buy button with a known variant: the strongest evidence
        for m in CART_PERMALINK_RE.finditer(body):
            h = variant_to_handle.get(int(m.group(1)))
            if h:
                counts[h] += W_BUY
    for m in HTML_PRODUCT_JSON_RE.finditer(body):
        add(m.group(1), W_JSON)
    if not counts:   # only menus name a product: better than nothing
        for m in PRODUCT_RE.finditer(html):
            add(m.group(1), W_LINK)
    # widgets like shipping protection appear on every page; sort them last
    return sorted(counts.items(), key=lambda kv: (is_junk_handle(kv[0]), -kv[1], kv[0]))


def match_handle(candidate: str | None, known: set[str]) -> str | None:
    """Exact match first; else the longest known handle the candidate starts with (channel
    suffix variants), else the known handle that starts with the candidate."""
    if not candidate:
        return None
    if candidate in known:
        return candidate
    starts = [k for k in known if candidate.startswith(k + "-")]
    if starts:
        return max(starts, key=len)
    ends = [k for k in known if k.startswith(candidate + "-")]
    if ends:
        return min(ends, key=len)
    return None


# ---------------------------------------------------------------- landing URL -> product (DB + network)

def _latest_listed_date(conn: sqlite3.Connection, store_id: int) -> str | None:
    return conn.execute("SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ? AND COALESCE(unlisted, 0) = 0",
                        (store_id,)).fetchone()[0]


def _known_handles(conn: sqlite3.Connection, store_id: int, today: str | None = None) -> tuple[set[str], dict[int, str]]:
    """Handles we can resolve ads to: the latest listed catalogue plus any unlisted products
    already recorded for `today` (or the latest day when today is not given)."""
    listed_date = _latest_listed_date(conn, store_id)
    unl_date = today or conn.execute("SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ? AND unlisted = 1",
                                     (store_id,)).fetchone()[0]
    handles: set[str] = set()
    v2h: dict[int, str] = {}
    for d, flag in ((listed_date, 0), (unl_date, 1)):
        if not d:
            continue
        handles.update(r[0] for r in conn.execute(
            "SELECT handle FROM products_daily WHERE store_id = ? AND snapshot_date = ? AND COALESCE(unlisted, 0) = ?",
            (store_id, d, flag)))
        v2h.update({r["variant_id"]: r["handle"] for r in conn.execute(
            """SELECT v.variant_id, p.handle FROM variants_daily v JOIN products_daily p
               ON p.store_id = v.store_id AND p.snapshot_date = v.snapshot_date AND p.product_id = v.product_id
               WHERE v.store_id = ? AND v.snapshot_date = ? AND COALESCE(p.unlisted, 0) = ?""", (store_id, d, flag))})
    return handles, v2h


POLITE_LANDING_FETCH = False   # set by `landing --refresh-all`: pause REQUEST_DELAY_S between back-to-back page fetches


def fetch_landing(session: requests.Session, url: str) -> tuple[str, str, int]:
    """GET a landing page (follows redirects). Returns (final_url, html, status)."""
    if POLITE_LANDING_FETCH:
        from . import shopify
        shopify._polite_pause()
    r = session.get(url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True,
                    headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    return r.url, r.text if r.status_code == 200 else "", r.status_code


def resolve_landing(conn: sqlite3.Connection, store_id: int, store_domain: str, ad: dict,
                    known: set[str], v2h: dict[int, str], session: requests.Session | None,
                    cache: dict[str, dict], today: str) -> dict:
    """Decide product_handle / page_handle for one ad. Uses (and fills) `cache` keyed by URL
    sans query so a page is fetched once per run; landing_pages persists it across days."""
    url = ad.get("landing_url")
    out = {"product_handle": None, "page_handle": None, "resolved_via": None}
    if not url:
        return out
    ph, pg = handle_from_url(url)
    if not ph and same_store(ad.get("landing_domain"), store_domain):
        # platforms with bare product paths (/wormwood-tincture.html, /<slug>): the last path segment is the handle
        slug = _path_slug(url)
        if slug and match_handle(slug, known):
            ph = slug
    if ph and same_store(ad.get("landing_domain"), store_domain):
        m = match_handle(ph, known)
        if m:
            return {"product_handle": m, "page_handle": None, "resolved_via": "url"}
        out["resolved_via"] = "url-unmatched"
    if pg:
        out["page_handle"] = pg
    hit = _resolve_url(conn, url, store_domain, known, v2h, session, cache, today, depth=0)
    if hit.get("product_handle"):
        out["product_handle"] = hit["product_handle"]
        out["resolved_via"] = "page-fetch"
    if hit.get("page_handle") and not out["page_handle"]:
        out["page_handle"] = hit["page_handle"]
    return out


def _path_slug(url: str) -> str | None:
    segs = [x for x in urlparse(url).path.split("/") if x]
    if not segs:
        return None
    slug = unquote(segs[-1]).lower()
    for ext in (".html", ".htm", ".php"):
        if slug.endswith(ext):
            slug = slug[: -len(ext)]
    return slug or None


def _fresh(row, today: str) -> bool:
    return row is not None and row["fetched_at"] >= (
        date.fromisoformat(today) - timedelta(days=config.LANDING_REFRESH_DAYS)).isoformat()


def _resolve_url(conn: sqlite3.Connection, url: str, store_domain: str, known: set[str], v2h: dict[int, str],
                 session: requests.Session | None, cache: dict[str, dict], today: str, depth: int) -> dict:
    """Fetch (or reuse) one landing page and work out which listed product it sells.

    Candidates are the handles found on the page. A candidate that is itself an unlisted
    product page (e.g. an offer variant not in products.json) is followed one hop: its own
    page usually names the listed product. Results persist in landing_pages."""
    key = _strip(url)
    if key in cache:
        return cache[key]
    row = conn.execute("SELECT * FROM landing_pages WHERE url = ?", (key,)).fetchone()
    if _fresh(row, today):
        hit = dict(row)
        if hit.get("product_handle") is None and hit.get("candidates"):
            # a page cached as unresolved may match now (e.g. an unlisted product was discovered since)
            parts = [p.split(":")[0].replace("via:", "") for p in hit["candidates"].split(",")]
            parts = sorted((clean_handle(h) for h in parts if h and not h.startswith(("error", "unlisted-product", "redirect"))),
                           key=is_junk_handle)
            for h in parts:
                m = match_handle(h, known)
                if m:
                    hit["product_handle"] = m
                    conn.execute("UPDATE landing_pages SET product_handle = ? WHERE url = ?", (m, key))
                    break
        cache[key] = hit
        return hit
    if session is None or cache.get("__fetched__", 0) >= config.META_MAX_LANDING_FETCH:
        # no session, or this run's fetch budget for the store is spent: leave it for another day
        cache[key] = dict(row) if row else {"product_handle": None, "page_handle": handle_from_url(url)[1]}
        return cache[key]
    cache["__fetched__"] = cache.get("__fetched__", 0) + 1
    hit = {"url": key, "fetched_at": today, "status": None, "final_url": None, "product_handle": None,
           "page_handle": handle_from_url(url)[1], "candidates": ""}
    cache[key] = hit
    try:
        final, html, status = fetch_landing(session, url)
        hit["status"], hit["final_url"] = status, final
        fph, fpg = handle_from_url(final)
        cands = handles_from_html(html, v2h) if html else []
        if fph and same_store(urlparse(final).netloc, store_domain):
            cands.insert(0, (fph, 99))
        hit["candidates"] = ",".join(f"{h}:{n}" for h, n in cands[:8])
        for h, _ in cands:
            m = match_handle(h, known)
            if m and not is_junk_handle(m):
                hit["product_handle"] = m
                break
        if hit["product_handle"] is None:
            for h, _ in cands:
                m = match_handle(h, known)
                if m:
                    hit["product_handle"] = m
                    break
        if hit["product_handle"] is None and depth < 1:
            # follow unlisted product pages one hop (skip the page we are already on)
            origin = f"{urlparse(final or url).scheme}://{urlparse(final or url).netloc}"
            for h, _ in [c for c in cands if c[0] != fph][:2]:
                sub = _resolve_url(conn, f"{origin}/products/{h}", store_domain, known, v2h, session, cache, today,
                                   depth + 1)
                if sub.get("product_handle"):
                    hit["product_handle"] = sub["product_handle"]
                    hit["candidates"] += f",via:{h}"
                    break
        hit["page_handle"] = fpg or hit["page_handle"]
    except requests.RequestException as e:
        hit["status"] = -1
        hit["candidates"] = f"error:{str(e)[:80]}"
    conn.execute(
        """INSERT OR REPLACE INTO landing_pages (url, fetched_at, status, final_url, product_handle, page_handle, candidates, resolver)
           VALUES (?,?,?,?,?,?,?,?)""",
        (hit["url"], hit["fetched_at"], hit["status"], hit["final_url"], hit["product_handle"],
         hit["page_handle"], hit["candidates"][:500], RESOLVER_VERSION))
    return hit


def discover_unlisted_products(conn: sqlite3.Connection, store_id: int, store_domain: str, ads: list[dict],
                               known: set[str], session: requests.Session | None, cache: dict[str, dict],
                               today: str, max_fetch: int = 40) -> int:
    """Ads often point at product pages that are live but not in products.json (offer /
    subscription variants). Fetch /products/<handle>.json for each such advertised handle and
    record it in products_daily as unlisted, so it gets its own Signals row. Returns count added."""
    if session is None:
        return 0
    wanted: dict[str, str] = {}
    for a in ads:
        ph, _ = handle_from_url(a.get("landing_url"))
        if ph and ph not in known and same_store(a.get("landing_domain"), store_domain) and ph not in wanted \
                and not is_junk_handle(ph):
            u = urlparse(a["landing_url"])
            wanted[ph] = f"{u.scheme}://{u.netloc}/products/{ph}.json"
    added = 0
    for handle, url in list(wanted.items())[:max_fetch]:
        row = conn.execute("SELECT * FROM landing_pages WHERE url = ?", (url,)).fetchone()
        product = None
        if _fresh(row, today) and row["status"] == 200 and row["candidates"].startswith("unlisted-product:"):
            product = _cached_unlisted(conn, store_id, handle)
        if product is None and not _fresh(row, today):
            hit = {"url": url, "fetched_at": today, "status": None, "final_url": None, "product_handle": None,
                   "page_handle": None, "candidates": ""}
            try:
                r = session.get(url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True,
                                headers={"Accept": "application/json"})
                hit["status"], hit["final_url"] = r.status_code, r.url
                if r.status_code == 200 and "json" in r.headers.get("Content-Type", ""):
                    data = r.json().get("product") or {}
                    if data.get("id") and data.get("handle"):
                        got = shopify.normalise_product(data)
                        if got["handle"] != handle or got["handle"] in known:
                            # the store redirected an old handle to a product we already know:
                            # remember the redirect, do not create an unlisted twin
                            hit["product_handle"] = match_handle(got["handle"], known) or got["handle"]
                            hit["candidates"] = f"redirect:{got['handle']}"
                        else:
                            product = got
                            hit["product_handle"] = product["handle"]
                            hit["candidates"] = f"unlisted-product:{product['product_id']}"
            except (requests.RequestException, ValueError) as e:
                hit["status"], hit["candidates"] = -1, f"error:{str(e)[:80]}"
            conn.execute("""INSERT OR REPLACE INTO landing_pages (url, fetched_at, status, final_url, product_handle, page_handle, candidates)
                            VALUES (?,?,?,?,?,?,?)""", (hit["url"], hit["fetched_at"], hit["status"], hit["final_url"],
                                                       hit["product_handle"], hit["page_handle"], hit["candidates"]))
        if product is not None:
            _db.write_unlisted_product(conn, store_id, today, product)
            known.add(product["handle"])
            added += 1
    return added


def _cached_unlisted(conn: sqlite3.Connection, store_id: int, handle: str) -> dict | None:
    """Re-use the most recent unlisted product row for this handle (carry it into today)."""
    r = conn.execute("""SELECT * FROM products_daily WHERE store_id = ? AND handle = ? AND unlisted = 1
                        ORDER BY snapshot_date DESC LIMIT 1""", (store_id, handle)).fetchone()
    if r is None:
        return None
    variants = [dict(v) for v in conn.execute(
        "SELECT * FROM variants_daily WHERE store_id = ? AND product_id = ? AND snapshot_date = ?",
        (store_id, r["product_id"], r["snapshot_date"]))]
    import json as _json
    return {"product_id": r["product_id"], "handle": r["handle"], "title": r["title"], "vendor": r["vendor"],
            "product_type": r["product_type"], "tags": _json.loads(r["tags"] or "[]"), "created_at": r["created_at"],
            "published_at": r["published_at"], "updated_at": r["updated_at"], "variant_count": r["variant_count"],
            "sold_out_variants": r["sold_out_variants"], "min_price": r["min_price"], "max_price": r["max_price"],
            "collection_position": None,
            "variants": [{"variant_id": v["variant_id"], "title": v["title"], "sku": v["sku"], "price": v["price"],
                          "compare_at_price": v["compare_at_price"], "available": bool(v["available"])} for v in variants]}


def _strip(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}{u.path}"


# ---------------------------------------------------------------- concepts (pure)

def concept_key(page: str, landing: str | None, launch: str) -> str:
    raw = f"{page}|{landing or ''}|{launch}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def cluster_concepts(ads: list[dict]) -> dict[str, str]:
    """ads: dicts with ad_id, page (page_id or page_name), landing (url sans query), launch (YYYY-MM-DD).
    Returns {ad_id: concept_id}. Ads on the same page + landing whose launch dates chain within
    1 day of each other share a concept."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for a in ads:
        groups[(a["page"] or "", a["landing"] or "")].append(a)
    out: dict[str, str] = {}
    for (page, landing), members in groups.items():
        members.sort(key=lambda a: (a["launch"] or "9999", a["ad_id"]))
        current: list[dict] = []
        prev: date | None = None
        first: str | None = None

        def flush():
            for m in current:
                out[m["ad_id"]] = concept_key(page, landing, first or "?")

        for a in members:
            d = date.fromisoformat(a["launch"]) if a["launch"] else None
            if current and (d is None or prev is None or (d - prev).days > 1):
                flush()
                current, first = [], None
            if not current:
                first = a["launch"]
            current.append(a)
            prev = d if d is not None else prev
        flush()
    return out


# ---------------------------------------------------------------- lineage (pure)

_WORD = re.compile(r"[a-z0-9']+")


def _shingles(text: str, n: int = 3) -> set[str]:
    words = _WORD.findall((text or "").lower())
    if len(words) < n:
        return set(words)
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def text_similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def find_lineage(new_ads: list[dict], old_ads: list[dict], threshold: float = 0.7) -> dict[str, tuple[str, float]]:
    """new_ads / old_ads: dicts with ad_id, page, text. Returns {new_ad_id: (old_ad_id, similarity)}
    for the best old ad on the same page above the threshold."""
    by_page: dict[str, list[dict]] = defaultdict(list)
    for o in old_ads:
        by_page[o["page"] or ""].append(o)
    out = {}
    for n in new_ads:
        best, best_s = None, 0.0
        for o in by_page.get(n["page"] or "", []):
            if o["ad_id"] == n["ad_id"]:
                continue
            s = text_similarity(n["text"], o["text"])
            if s > best_s:
                best, best_s = o["ad_id"], s
        if best is not None and best_s > threshold:
            out[n["ad_id"]] = (best, round(best_s, 3))
    return out


# ---------------------------------------------------------------- daily processing (DB)

def _days(a: str | None, b: str) -> int | None:
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days if a else None
    except ValueError:
        return None


def _store_ads(conn: sqlite3.Connection, store_id: int, today: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT a.*, d.is_active, d.reactions, d.comments, d.shares
           FROM meta_ads a JOIN meta_ads_daily d ON d.ad_id = a.ad_id AND d.snapshot_date = ?
           WHERE a.store_id = ?""", (today, store_id))]


def resolve_store_landings(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str,
                           session: requests.Session | None, ads: list[dict] | None = None) -> tuple[int, int, int]:
    """Landing URL -> product for every ad of the store's `today` snapshot (fetching pages when a session is given);
    writes product_handle / page_handle on meta_ads. Returns (ads resolved, unlisted products discovered). This is
    the first step of process_store and all that `landing --refresh-all` needs: concepts, lineage and the daily
    metrics are recomputed by the next Meta pass."""
    ads = _store_ads(conn, store_id, today) if ads is None else ads
    known, v2h = _known_handles(conn, store_id, today)
    cache: dict[str, dict] = {}
    unlisted = discover_unlisted_products(conn, store_id, store_domain, ads, known, session, cache, today)
    if unlisted:
        known, v2h = _known_handles(conn, store_id, today)
    resolved = 0
    for a in ads:
        r = resolve_landing(conn, store_id, store_domain, a, known, v2h, session, cache, today)
        a["product_handle"], a["page_handle"] = r["product_handle"], r["page_handle"]
        a["landing_handle"] = handle_from_url(a.get("landing_url"))[0]   # raw advertised handle, suffix and all
        resolved += 1 if r["product_handle"] else 0
        conn.execute("""UPDATE meta_ads SET product_handle = ?, page_handle = ?, landing_resolved_via = ?, landing_handle = ?
                        WHERE ad_id = ?""",
                     (r["product_handle"], r["page_handle"], r["resolved_via"], a["landing_handle"], a["ad_id"]))
    conn.commit()
    return resolved, unlisted, sum(1 for h in cache.values() if isinstance(h, dict) and h.get("status") is not None)


def process_store(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str,
                  session: requests.Session | None = None, fetch_landings: bool = True) -> dict:
    """Resolve landings, cluster concepts, find lineage, write per-ad and per-concept rows for `today`.
    Returns a summary dict."""
    ads = _store_ads(conn, store_id, today)
    if not ads:
        return {"ads": 0}
    resolved, unlisted, pages_fetched = resolve_store_landings(conn, store_id, store_domain, today, session if fetch_landings else None, ads)

    page_of = lambda a: a.get("page_id") or a.get("page_name") or ""

    # Page relevance: a keyword search also returns unrelated advertisers. A page none of whose
    # ads (with a URL) lands on the store or resolves to a product is ignored downstream.
    pages = page_relevance(ads, store_domain)
    conn.execute("DELETE FROM meta_pages_daily WHERE snapshot_date = ? AND store_id = ?", (today, store_id))
    for name, rec in pages.items():
        conn.execute("""INSERT INTO meta_pages_daily (snapshot_date, store_id, page_name, page_id, ads, ads_with_url, on_store, ignored)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (today, store_id, name, rec["page_id"], rec["ads"], rec["with_url"], rec["on_store"], rec["ignored"]))
    for a in ads:
        a["page_ignored"] = pages[a.get("page_name") or ""]["ignored"]
        conn.execute("UPDATE meta_ads SET page_ignored = ? WHERE ad_id = ?", (a["page_ignored"], a["ad_id"]))
    all_ads = ads
    ads = [a for a in ads if not a["page_ignored"]]
    concepts = cluster_concepts([{"ad_id": a["ad_id"], "page": page_of(a), "landing": _strip(a["landing_url"]) if a["landing_url"] else None,
                                  "launch": a["ad_start_date"] or a["first_seen_date"]} for a in ads])
    for a in all_ads:
        a["concept_id"] = concepts.get(a["ad_id"])
        conn.execute("UPDATE meta_ads SET concept_id = ? WHERE ad_id = ?", (a["concept_id"], a["ad_id"]))

    def text_of(a):
        return f"{a.get('headline') or ''} {a.get('primary_text') or ''}"
    # "new" = Meta's start date within the last 7 days (not "first time we saw it": on a store's
    # first scrape every ad is first-seen today). "old" = started 14+ days ago. Disjoint by design.
    def age(a):
        return _days(a["ad_start_date"] or a["first_seen_date"], today)
    new = [{"ad_id": a["ad_id"], "page": page_of(a), "text": text_of(a)} for a in ads
           if age(a) is not None and age(a) <= 7]
    old = [{"ad_id": a["ad_id"], "page": page_of(a), "text": text_of(a)} for a in ads
           if age(a) is not None and age(a) >= 14]
    lineage = find_lineage(new, old)
    for aid, (parent, sim) in lineage.items():
        conn.execute("UPDATE meta_ads SET lineage_of = ?, lineage_similarity = ? WHERE ad_id = ? AND lineage_of IS NULL",
                     (parent, sim, aid))

    # per-ad daily metrics
    prev_rows = {r["ad_id"]: dict(r) for r in conn.execute(
        """SELECT d.ad_id, d.reactions, d.comments, d.shares, d.snapshot_date FROM meta_ads_daily d
           WHERE d.store_id = ? AND d.snapshot_date = (SELECT MAX(snapshot_date) FROM meta_ads_daily
                                                       WHERE store_id = ? AND snapshot_date < ?)""",
        (store_id, store_id, today))}
    for a in all_ads:
        eng = _engagement(a)
        prev = prev_rows.get(a["ad_id"])
        delta = None
        if eng is not None and prev and _engagement(prev) is not None:
            delta = eng - _engagement(prev)
        per_day = _engagement_per_day(conn, a["ad_id"], today)
        conn.execute(
            """UPDATE meta_ads_daily SET days_running = ?, engagement = ?, engagement_delta = ?, engagement_per_day = ?
               WHERE snapshot_date = ? AND ad_id = ?""",
            (_days(a["ad_start_date"] or a["first_seen_date"], today), eng, delta, per_day, today, a["ad_id"]))

    # reach curve + comment curve (for every ad, ignored pages included: they are per-ad facts)
    backfill_from_raw(conn, all_ads, today)
    for a in all_ads:
        compute_reach_metrics(conn, a["ad_id"], today)

    n_concepts = write_concept_rows(conn, store_id, today)
    conn.commit()
    return {"ads": len(all_ads), "ignored": len(all_ads) - len(ads), "resolved": resolved, "concepts": n_concepts,
            "unlisted": unlisted,
            "lineage": len(lineage), "pages_fetched": pages_fetched}


def delivering(ad: dict) -> bool:
    """Is this ad delivering today? The 'Low impression count' badge says no whatever else we know; then the
    single-ad page's end_date (delivery_status) wins when we have it; otherwise presence in the active results.
    An ad whose payload carried no badge field (low_impressions NULL) counts as delivering."""
    if ad.get("low_impressions") == 1:
        return False
    ds = ad.get("delivery_status")
    if ds == "on":
        return True
    if ds == "off":
        return False
    return bool(ad.get("is_active"))


def write_concept_rows(conn: sqlite3.Connection, store_id: int, today: str) -> int:
    """One meta_concepts_daily row per concept for `today`; survival = delivering / ever, where delivering
    uses end_date advancement when the single-ad page has been read (survival_source = delivery) and
    search presence otherwise (survival_source = search)."""
    ads = [dict(r) for r in conn.execute(
        """SELECT a.ad_id, a.concept_id, a.page_name, a.landing_url, a.product_handle, a.page_handle, a.ad_start_date,
                  a.first_seen_date, a.delivery_status, d.is_active, d.low_impressions
           FROM meta_ads a JOIN meta_ads_daily d ON d.ad_id = a.ad_id AND d.snapshot_date = ?
           WHERE a.store_id = ? AND COALESCE(a.page_ignored, 0) = 0 AND a.concept_id IS NOT NULL""", (today, store_id))]
    by_c: dict[str, list[dict]] = defaultdict(list)
    for a in ads:
        by_c[a["concept_id"]].append(a)
    conn.execute("DELETE FROM meta_concepts_daily WHERE snapshot_date = ? AND store_id = ?", (today, store_id))
    for cid, members in by_c.items():
        ever = conn.execute("SELECT COUNT(*) FROM meta_ads WHERE concept_id = ?", (cid,)).fetchone()[0] or len(members)
        active = sum(1 for m in members if m["is_active"])
        deliv = sum(1 for m in members if delivering(m))
        if any(m.get("low_impressions") is not None for m in members):
            source = "badge+delivery" if any(m.get("delivery_status") for m in members) else "badge"
        else:
            source = "delivery" if any(m.get("delivery_status") for m in members) else "search"
        launch = min((m["ad_start_date"] or m["first_seen_date"]) for m in members)
        m0 = members[0]
        conn.execute(
            """INSERT INTO meta_concepts_daily
               (snapshot_date, store_id, concept_id, page_name, landing_url, product_handle, page_handle, launch_date,
                days_running, ads_ever, ads_active, ads_delivering, survival, survival_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (today, store_id, cid, m0.get("page_name"), _strip(m0["landing_url"]) if m0["landing_url"] else None,
             m0.get("product_handle"), m0.get("page_handle"), launch, _days(launch, today), ever, active, deliv,
             round(deliv / ever, 3) if ever else None, source))
    return len(by_c)


LANDING_KINDS = ("product", "unlisted-product", "advertorial", "homepage", "collection", "other-store-path",
                 "external", "no-url", "page-ignored")


def landing_kind(ad: dict, store_domain: str) -> str:
    """Where an ad lands, as a bucket that explains why it does or does not attach to a product."""
    if ad.get("page_ignored"):
        return "page-ignored"
    if ad.get("product_handle"):
        return "product"
    url = ad.get("landing_url")
    if not url:
        return "no-url"
    if not same_store(ad.get("landing_domain") or urlparse(url).netloc, store_domain):
        return "external"
    path = urlparse(url).path.rstrip("/").lower()
    if not path:
        return "homepage"
    if PRODUCT_RE.search(url):
        return "unlisted-product"
    if PAGE_RE.search(url):
        return "advertorial"
    if path.startswith("/collections"):
        return "collection"
    return "other-store-path"


def landing_breakdown(conn: sqlite3.Connection, store_id: int, store_domain: str, as_of: str | None = None) -> dict:
    """Counts of today's active ads by landing kind for one store (+ total, to_products)."""
    snap = conn.execute("SELECT MAX(snapshot_date) FROM meta_ads_daily WHERE store_id = ? AND snapshot_date <= ?",
                        (store_id, as_of or "9999")).fetchone()[0]
    out = {k: 0 for k in LANDING_KINDS}
    out.update({"snapshot": snap, "active": 0, "to_products": 0, "products": 0})
    if not snap:
        return out
    handles = set()
    for a in conn.execute("""SELECT a.* FROM meta_ads a JOIN meta_ads_daily d ON d.ad_id = a.ad_id AND d.snapshot_date = ?
                             WHERE a.store_id = ? AND d.is_active = 1""", (snap, store_id)):
        a = dict(a)
        k = landing_kind(a, store_domain)
        out[k] += 1
        out["active"] += 1
        if k == "product":
            out["to_products"] += 1
            handles.add(a["product_handle"])
    out["products"] = len(handles)
    return out


def breakdown_summary(b: dict) -> str:
    parts = [(k, b[k]) for k in LANDING_KINDS if k != "product" and b.get(k)]
    parts.sort(key=lambda kv: -kv[1])
    return ", ".join(f"{k} {n}" for k, n in parts[:4])


def page_relevance(ads: list[dict], store_domain: str) -> dict[str, dict]:
    """{page_name: {ads, with_url, on_store, ignored, page_id}} over one store's ads.
    on_store counts ads whose landing domain is the store's or that resolved to a product."""
    out: dict[str, dict] = {}
    for a in ads:
        rec = out.setdefault(a.get("page_name") or "", {"ads": 0, "with_url": 0, "on_store": 0, "ignored": 0,
                                                         "page_id": a.get("page_id")})
        rec["ads"] += 1
        if a.get("landing_url"):
            rec["with_url"] += 1
            if a.get("product_handle") or same_store(a.get("landing_domain"), store_domain):
                rec["on_store"] += 1
    for rec in out.values():
        rec["ignored"] = 1 if (rec["with_url"] > 0 and rec["on_store"] == 0) else 0
    return out


def backfill_from_raw(conn: sqlite3.Connection, ads: list[dict], today: str) -> int:
    """Ads scraped before the reach/post fields existed still carry their full GraphQL node in
    raw_json; re-derive today's reach columns and the post link from it when missing."""
    import json as _json
    from . import meta_ads as _m
    n = 0
    for a in ads:
        row = conn.execute("SELECT eu_total_reach, uk_reach, reach_range_lower, reach_source FROM meta_ads_daily "
                           "WHERE snapshot_date = ? AND ad_id = ?", (today, a["ad_id"])).fetchone()
        raw = a.get("raw_json")
        if row is None or not raw or (row["reach_source"] is not None and a.get("engagement_type")):
            continue
        try:
            node = _json.loads(raw)
        except ValueError:
            continue
        reach = _m.extract_reach(node)
        post = extract_post_or_existing(a, node)
        conn.execute(
            """UPDATE meta_ads_daily SET eu_total_reach = COALESCE(eu_total_reach, ?), uk_reach = COALESCE(uk_reach, ?),
                 reach_range_lower = COALESCE(reach_range_lower, ?), reach_range_upper = COALESCE(reach_range_upper, ?),
                 reach_source = COALESCE(reach_source, ?) WHERE snapshot_date = ? AND ad_id = ?""",
            (reach["eu_reach"], reach["uk_reach"], reach["range_lower"], reach["range_upper"], reach["source"] or "none",
             today, a["ad_id"]))
        conn.execute("""UPDATE meta_ads SET post_url = COALESCE(post_url, ?), reach_keys = COALESCE(reach_keys, ?),
                          engagement_type = CASE WHEN COALESCE(post_url, ?) IS NULL THEN 'dark' ELSE 'boosted' END
                        WHERE ad_id = ?""", (post, reach["keys"], post, a["ad_id"]))
        n += 1
    return n


def extract_post_or_existing(a: dict, node: dict) -> str | None:
    from . import meta_ads as _m
    return a.get("post_url") or _m.extract_post_url(node)


def slope(points: list[tuple[str, int]]) -> float | None:
    """Reach per day between the first and last point (dates ISO, reach cumulative). None if < 2 points."""
    pts = sorted((d, v) for d, v in points if v is not None)
    if len(pts) < 2:
        return None
    days = _days(pts[0][0], pts[-1][0]) or 0
    if days <= 0:
        return None
    return round((pts[-1][1] - pts[0][1]) / days, 1)


def compute_reach_metrics(conn: sqlite3.Connection, ad_id: str, today: str) -> None:
    """reach_delta_1d, reach_slope_7d / prev_7d (EU exact reach, else UK exact), comment_delta_1d."""
    rows = conn.execute(
        """SELECT snapshot_date, eu_total_reach, uk_reach, comments FROM meta_ads_daily
           WHERE ad_id = ? AND snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 15""", (ad_id, today)).fetchall()
    if not rows or rows[0]["snapshot_date"] != today:
        return
    def reach_of(r):
        return r["eu_total_reach"] if r["eu_total_reach"] is not None else r["uk_reach"]
    series = [(r["snapshot_date"], reach_of(r)) for r in rows]
    t = date.fromisoformat(today)
    cur = [(d, v) for d, v in series if (t - date.fromisoformat(d)).days <= 7]
    prev = [(d, v) for d, v in series if 7 <= (t - date.fromisoformat(d)).days <= 14]
    delta = None
    if len(series) >= 2 and series[0][1] is not None and series[1][1] is not None:
        delta = series[0][1] - series[1][1]
    cdelta = None
    if len(rows) >= 2 and rows[0]["comments"] is not None and rows[1]["comments"] is not None:
        cdelta = rows[0]["comments"] - rows[1]["comments"]
    conn.execute(
        """UPDATE meta_ads_daily SET reach_delta_1d = ?, reach_slope_7d = ?, reach_slope_prev_7d = ?, comment_delta_1d = ?
           WHERE snapshot_date = ? AND ad_id = ?""",
        (delta, slope(cur), slope(prev), cdelta, today, ad_id))


def _engagement(row: dict) -> int | None:
    vals = [row.get(k) for k in ("reactions", "comments", "shares")]
    if all(v is None for v in vals):
        return None
    return sum(v or 0 for v in vals)


def _engagement_per_day(conn: sqlite3.Connection, ad_id: str, today: str) -> float | None:
    """7-day average of daily engagement deltas (NULL when engagement is not available)."""
    rows = conn.execute(
        """SELECT snapshot_date, reactions, comments, shares FROM meta_ads_daily
           WHERE ad_id = ? AND snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 8""", (ad_id, today)).fetchall()
    vals = [(r["snapshot_date"], _engagement(dict(r))) for r in rows]
    vals = [(d, e) for d, e in vals if e is not None]
    if len(vals) < 2:
        return None
    (d1, e1), (d0, e0) = vals[0], vals[-1]
    days = _days(d0, d1) or 1
    return round((e1 - e0) / days, 2)


# ---------------------------------------------------------------- alerts

def run_alerts(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str) -> list[dict]:
    """Evaluate rules 5-7 for one store on `today`. Idempotent per (day, store, handle, rule, detail)."""
    found: list[dict] = []
    week_ago = (date.fromisoformat(today) - timedelta(days=7)).isoformat()

    # rule 2 (spec): the store's ad velocity doubled week over week with at least 3 launches this week
    v = ad_velocity(conn, store_id, today)
    if v["new_ads_7d"] >= 3 and v["new_ads_prev_7d"] and v["ad_velocity_wow"] >= 2.0:
        found.append({"rule": 2, "handle": None, "key": "2|store",
                      "detail": f"{store_domain}: {v['new_ads_7d']} ads launched in 7 days vs {v['new_ads_prev_7d']} the week before "
                                f"(x{v['ad_velocity_wow']})"})

    # rule 5: engagement_per_day >= 2x week over week on a single ad
    for r in conn.execute(
        """SELECT d.ad_id, d.engagement_per_day AS now, p.engagement_per_day AS then_, a.product_handle, a.page_name
           FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
           LEFT JOIN meta_ads_daily p ON p.ad_id = d.ad_id AND p.snapshot_date = ?
           WHERE d.store_id = ? AND d.snapshot_date = ? AND d.engagement_per_day IS NOT NULL""",
        (week_ago, store_id, today)):
        if r["then_"] and r["then_"] > 0 and r["now"] >= 2 * r["then_"]:
            found.append({"rule": 5, "handle": r["product_handle"], "key": f"5|{r['ad_id']}",
                          "detail": f"ad {r['ad_id']} ({r['page_name']}): engagement/day {r['then_']} -> {r['now']}"})

    # rule 8: EU reach slope doubled vs the prior 7 days (spend proxy), reach large enough to matter
    for r in conn.execute(
        """SELECT d.ad_id, d.reach_slope_7d, d.reach_slope_prev_7d, d.eu_total_reach, d.uk_reach, a.page_name, a.product_handle
           FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
           WHERE d.store_id = ? AND d.snapshot_date = ? AND d.reach_slope_7d IS NOT NULL AND d.reach_slope_prev_7d IS NOT NULL
             AND COALESCE(a.page_ignored, 0) = 0""", (store_id, today)):
        reach = r["eu_total_reach"] if r["eu_total_reach"] is not None else (r["uk_reach"] or 0)
        if r["reach_slope_prev_7d"] > 0 and r["reach_slope_7d"] >= 2 * r["reach_slope_prev_7d"] and reach >= config.META_REACH_MIN:
            found.append({"rule": 8, "handle": r["product_handle"], "key": f"8|{r['ad_id']}",
                          "detail": f"ad {r['ad_id']} ({r['page_name']}): reach/day {r['reach_slope_prev_7d']:.0f} -> "
                                    f"{r['reach_slope_7d']:.0f} (total reach {reach:,})"})

    # rule 6: concept with all ads active 14+ days while the page's active concepts fell
    active_concepts_by_page = defaultdict(int)
    prev_active_by_page = defaultdict(int)
    for r in conn.execute("SELECT page_name, COALESCE(ads_delivering, ads_active) AS ads_active FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ?",
                          (store_id, today)):
        if r["ads_active"]:
            active_concepts_by_page[r["page_name"]] += 1
    prev_date = conn.execute("SELECT MAX(snapshot_date) FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date <= ?",
                             (store_id, week_ago)).fetchone()[0]
    if prev_date:
        for r in conn.execute("SELECT page_name, COALESCE(ads_delivering, ads_active) AS ads_active FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ?",
                              (store_id, prev_date)):
            if r["ads_active"]:
                prev_active_by_page[r["page_name"]] += 1
        for r in conn.execute(
            """SELECT concept_id, page_name, product_handle, days_running, ads_ever, COALESCE(ads_delivering, ads_active) AS ads_active, landing_url
               FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ?
                 AND days_running >= 14 AND ads_ever >= 2 AND COALESCE(ads_delivering, ads_active) = ads_ever""", (store_id, today)):
            if active_concepts_by_page[r["page_name"]] < prev_active_by_page.get(r["page_name"], 0):
                found.append({"rule": 6, "handle": r["product_handle"], "key": f"6|{r['concept_id']}",
                              "detail": f"concept {r['concept_id']} on {r['page_name']}: {r['ads_active']}/{r['ads_ever']} ads "
                                        f"alive {r['days_running']}d while page concepts {prev_active_by_page[r['page_name']]}"
                                        f" -> {active_concepts_by_page[r['page_name']]} ({r['landing_url']})"})

    # rule 7: new ads with lineage to a 20+ day ad, one alert per (page, parent ad) so a store
    # that launches 40 copies of one proven ad yields one line, not forty.
    groups: dict[tuple, dict] = {}
    for r in conn.execute(
        """SELECT a.ad_id, a.page_name, a.product_handle, a.lineage_of, a.lineage_similarity, o.ad_start_date, o.first_seen_date
           FROM meta_ads a JOIN meta_ads o ON o.ad_id = a.lineage_of
           WHERE a.store_id = ? AND a.first_seen_date = ? AND a.lineage_of IS NOT NULL AND COALESCE(a.page_ignored, 0) = 0""",
        (store_id, today)):
        age = _days(r["ad_start_date"] or r["first_seen_date"], today) or 0
        if age < 20:
            continue
        g = groups.setdefault((r["page_name"], r["lineage_of"]), {"ids": [], "sims": [], "handles": [], "age": age})
        g["ids"].append(r["ad_id"])
        g["sims"].append(r["lineage_similarity"] or 0)
        if r["product_handle"]:
            g["handles"].append(r["product_handle"])
    for (page, parent), g in groups.items():
        n = len(g["ids"])
        handle = max(set(g["handles"]), key=g["handles"].count) if g["handles"] else None
        found.append({"rule": 7, "handle": handle, "key": f"7|{page}|{parent}",
                      "detail": f"{n} new ad{'s' if n > 1 else ''} on {page} re-use copy from ad {parent} running "
                                f"{g['age']}d (avg {sum(g['sims']) / n:.0%} similar; e.g. {', '.join(g['ids'][:3])})"})

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn.execute("DELETE FROM alerts WHERE snapshot_date = ? AND store_id = ? AND rule >= 5 AND dedupe_key IS NULL",
                 (today, store_id))
    written = []
    for f in found:
        key = f.get("key") or f"{f['rule']}|{f['detail']}"
        exists = conn.execute(
            "SELECT 1 FROM alerts WHERE snapshot_date = ? AND store_id = ? AND dedupe_key = ?",
            (today, store_id, key)).fetchone()
        if exists:
            continue
        conn.execute("""INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at, dedupe_key)
                        VALUES (?,?,?,?,?,?,?)""", (today, store_id, f["handle"], f["rule"], f["detail"], now, key))
        written.append(f)
    conn.commit()
    return written


def write_alerts_markdown(conn: sqlite3.Connection, today: str, path: Path | None = None) -> Path | None:
    rows = conn.execute(
        """SELECT a.rule, a.product_handle, a.detail, s.store_domain FROM alerts a JOIN stores s ON s.id = a.store_id
           WHERE a.snapshot_date = ? ORDER BY a.rule, s.store_domain""", (today,)).fetchall()
    if not rows:
        return None
    path = path or (config.ALERTS_DIR / f"{today}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    from . import ad_detail, inventory, scaling
    from . import ad_rank
    names = {**RULES, **inventory.RULES, **ad_detail.RULES, **scaling.RULES, **ad_rank.RULES}
    lines = [f"# Alerts {today}", ""]
    for r in rows:
        lines.append(f"- **rule {r['rule']}** ({names.get(r['rule'], '')}) {r['store_domain']} "
                     f"{r['product_handle'] or ''}: {r['detail']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- Signals join

def _launch_date(a) -> str | None:
    return a["ad_start_date"] or a["first_seen_date"]


def ad_velocity(conn: sqlite3.Connection, store_id: int, today: str, product_handle: str | None = None) -> dict:
    """Ads launched (Meta start date) in the last 7 days vs the 7 before, for a store or one product.
    An ad counts once it has ever been seen, whether or not it is still active."""
    t = date.fromisoformat(today)
    lo7, lo14 = (t - timedelta(days=7)).isoformat(), (t - timedelta(days=14)).isoformat()
    sql = "SELECT ad_start_date, first_seen_date FROM meta_ads WHERE store_id = ? AND COALESCE(page_ignored, 0) = 0"
    args: list = [store_id]
    if product_handle is not None:
        sql += " AND product_handle = ?"
        args.append(product_handle)
    new7 = prev7 = 0
    for a in conn.execute(sql, args):
        d = _launch_date(a)
        if not d:
            continue
        if lo7 < d <= today:
            new7 += 1
        elif lo14 < d <= lo7:
            prev7 += 1
    wow = round(new7 / prev7, 2) if prev7 else (None if new7 == 0 else float("inf"))
    return {"new_ads_7d": new7, "new_ads_prev_7d": prev7, "ad_velocity_wow": wow}


def _snapshot_on_or_before(conn: sqlite3.Connection, store_id: int, day: str) -> str | None:
    return conn.execute("SELECT MAX(snapshot_date) FROM meta_ads_daily WHERE store_id = ? AND snapshot_date <= ?",
                        (store_id, day)).fetchone()[0]


def delivering_metrics(conn: sqlite3.Connection, store_id: int, today: str) -> dict:
    """Ads that are actually delivering (active and without the 'Low impression count' badge, and not switched
    off per the single-ad page), per product handle and for the store ('__store__'):
      ads_delivering, ads_low_impressions, ads_badge_unknown, ads_delivering_7d_ago, delivering_velocity_wow,
      concepts_delivering, ads_as_of. The week-ago count uses the nearest snapshot on/before today-7 and the same
      badge rule; when that day carried no badge data at all the ratio is left blank rather than inflated."""
    snap = _snapshot_on_or_before(conn, store_id, today)
    if not snap:
        return {}
    prev_day = (date.fromisoformat(snap) - timedelta(days=7)).isoformat()
    prev = _snapshot_on_or_before(conn, store_id, prev_day)

    def counts(day: str) -> dict[str, dict]:
        out: dict[str, dict] = defaultdict(lambda: {"active": 0, "delivering": 0, "low": 0, "unknown": 0})
        for a in conn.execute(
            """SELECT a.product_handle, a.delivery_status, d.is_active, d.low_impressions
               FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
               WHERE d.store_id = ? AND d.snapshot_date = ? AND d.is_active = 1 AND COALESCE(a.page_ignored, 0) = 0""", (store_id, day)):
            rec = dict(a)
            for key in (a["product_handle"], "__store__"):
                if key is None:
                    continue
                c = out[key]
                c["active"] += 1
                c["low"] += 1 if a["low_impressions"] == 1 else 0
                c["unknown"] += 1 if a["low_impressions"] is None else 0
                c["delivering"] += 1 if delivering(rec) else 0
        return out
    now = counts(snap)
    before = counts(prev) if prev and prev != snap else {}
    prev_has_badge = any(c["low"] or c["unknown"] < c["active"] for c in before.values()) if before else False
    concepts: dict[str, int] = defaultdict(int)
    for r in conn.execute("""SELECT product_handle, COUNT(*) n FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ?
                             AND COALESCE(ads_delivering, ads_active) > 0 GROUP BY product_handle""", (store_id, snap)):
        concepts[r["product_handle"] or "__none__"] += r["n"]
        concepts["__store__"] += r["n"]
    out = {}
    for key, c in now.items():
        b = before.get(key)
        prev_n = b["delivering"] if b else None
        if prev_n is None:
            wow = ""
        elif b["unknown"] == b["active"] and c["unknown"] < c["active"]:
            wow = ""                          # week-ago rows carried no badge data: not comparable
        elif prev_n == 0:
            wow = _wow_cell(float("inf")) if c["delivering"] else ""
        else:
            wow = _wow_cell(round(c["delivering"] / prev_n, 2))
        out[key] = {"ads_delivering": c["delivering"], "ads_low_impressions": c["low"], "ads_badge_unknown": c["unknown"],
                    "ads_active": c["active"], "ads_delivering_7d_ago": "" if prev_n is None else prev_n,
                    "delivering_velocity_wow": wow, "concepts_delivering": concepts.get(key, 0), "ads_as_of": snap,
                    "prev_as_of": prev or ""}
    return out


def _wow_cell(v):
    if v is None:
        return ""
    return "new" if v == float("inf") else v


def meta_for_signals(conn: sqlite3.Connection, store_id: int, today: str) -> dict[str, dict]:
    """{product_handle: {ads_pointing_here, ads_launched_7d, ads_launched_prev_7d, ad_velocity_wow,
    engagement_per_day, days_running_max, concept_status, ...}} from the latest ad snapshot on/before `today`."""
    snap = conn.execute("SELECT MAX(snapshot_date) FROM meta_ads_daily WHERE store_id = ? AND snapshot_date <= ?",
                        (store_id, today)).fetchone()[0]
    if not snap:
        return {}
    out: dict[str, dict] = {}
    # launches per product over every ad ever seen (an ad launched 5 days ago may already be off)
    t = date.fromisoformat(snap)
    lo7, lo14 = (t - timedelta(days=7)).isoformat(), (t - timedelta(days=14)).isoformat()
    launches: dict[str, list[int]] = {}
    for a in conn.execute("""SELECT product_handle, ad_start_date, first_seen_date FROM meta_ads
                             WHERE store_id = ? AND product_handle IS NOT NULL AND COALESCE(page_ignored, 0) = 0""", (store_id,)):
        d = _launch_date(a)
        rec = launches.setdefault(a["product_handle"], [0, 0])
        if d and lo7 < d <= snap:
            rec[0] += 1
        elif d and lo14 < d <= lo7:
            rec[1] += 1
    for h, (n7, p7) in launches.items():
        out[h] = {"ads_as_of": snap, "ads_pointing_here": 0, "ads_launched_7d": n7, "ads_launched_prev_7d": p7,
                  "ad_velocity_wow": _wow_cell(round(n7 / p7, 2) if p7 else (None if n7 == 0 else float("inf"))),
                  "days_running_max": None, "engagement_per_day": None, "concept_status": "",
                  "eu_reach_slope_7d": None, "comment_delta_1d": None}
    for r in conn.execute(
        """SELECT a.product_handle, COUNT(*) AS n, MAX(d.days_running) AS dmax, AVG(d.engagement_per_day) AS epd,
                  SUM(d.reach_slope_7d) AS slope, SUM(d.reach_slope_7d IS NOT NULL) AS slope_n,
                  SUM(d.comment_delta_1d) AS cdelta, SUM(d.comment_delta_1d IS NOT NULL) AS cdelta_n
           FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
           WHERE d.store_id = ? AND d.snapshot_date = ? AND d.is_active = 1 AND a.product_handle IS NOT NULL
             AND COALESCE(a.page_ignored, 0) = 0
           GROUP BY a.product_handle""", (store_id, snap)):
        rec = out.setdefault(r["product_handle"], {"ads_launched_7d": 0, "ads_launched_prev_7d": 0, "ad_velocity_wow": "", "concept_status": ""})
        rec.update({"ads_as_of": snap, "ads_pointing_here": r["n"], "days_running_max": r["dmax"],
                    "engagement_per_day": None if r["epd"] is None else round(r["epd"], 1),
                    "eu_reach_slope_7d": None if not r["slope_n"] else round(r["slope"], 1),
                    "comment_delta_1d": None if not r["cdelta_n"] else int(r["cdelta"])})
    dm = delivering_metrics(conn, store_id, today)
    for h, m in dm.items():
        if h == "__store__":
            continue
        rec = out.setdefault(h, {"ads_as_of": snap, "ads_pointing_here": 0, "ads_launched_7d": 0, "ads_launched_prev_7d": 0,
                                 "ad_velocity_wow": "", "days_running_max": None, "engagement_per_day": None, "concept_status": "",
                                 "eu_reach_slope_7d": None, "comment_delta_1d": None})
        rec.update({k: m[k] for k in ("ads_delivering", "ads_low_impressions", "ads_delivering_7d_ago", "delivering_velocity_wow", "concepts_delivering")})
    from . import ad_rank
    rm = ad_rank.ad_rank_metrics(conn, store_id, today)
    for h, pm in rm["products"].items():
        rec = out.setdefault(h, {"ads_as_of": snap, "ads_pointing_here": 0, "ads_launched_7d": 0, "ads_launched_prev_7d": 0,
                                 "ad_velocity_wow": "", "days_running_max": None, "engagement_per_day": None, "concept_status": "",
                                 "eu_reach_slope_7d": None, "comment_delta_1d": None})
        rec.update({"ads_in_top5": pm["ads_in_top5"], "best_rank": pm["best_rank"], "best_rank_delta_7d": pm["best_rank_delta_7d"]})
    for r in conn.execute(
        """SELECT product_handle, COUNT(*) AS concepts, SUM(COALESCE(ads_delivering, ads_active) > 0) AS alive, MAX(days_running) AS oldest,
                  MAX(CASE WHEN COALESCE(ads_delivering, ads_active) = ads_ever THEN days_running END) AS oldest_intact
           FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ? AND product_handle IS NOT NULL
           GROUP BY product_handle""", (store_id, snap)):
        h = r["product_handle"]
        if h in out:
            intact = f", intact {r['oldest_intact']}d" if r["oldest_intact"] is not None else ""
            out[h]["concept_status"] = f"{r['alive']}/{r['concepts']} concepts alive{intact}"
    return out


# ---------------------------------------------------------------- coverage

def coverage(conn: sqlite3.Connection, as_of: str) -> list[dict]:
    """Per store (plus a TOTAL row): how measurable the scraped ads are on `as_of`."""
    rows = []
    stores = conn.execute(
        """SELECT DISTINCT s.id, s.store_domain FROM meta_ads_daily d JOIN stores s ON s.id = d.store_id
           WHERE d.snapshot_date = ? ORDER BY s.store_domain""", (as_of,)).fetchall()
    total = {"store": "TOTAL", "ads": 0, "eu_exact": 0, "uk_exact": 0, "range_only": 0, "no_reach": 0,
             "boosted": 0, "with_comments": 0, "dark": 0, "keys": {}}
    for s in stores:
        rec = {"store": s["store_domain"], "ads": 0, "eu_exact": 0, "uk_exact": 0, "range_only": 0, "no_reach": 0,
               "boosted": 0, "with_comments": 0, "dark": 0, "keys": {}}
        for r in conn.execute(
            """SELECT d.eu_total_reach, d.uk_reach, d.reach_range_lower, d.comments, a.engagement_type, a.reach_keys, d.post_status
               FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
               WHERE d.store_id = ? AND d.snapshot_date = ? AND COALESCE(a.page_ignored, 0) = 0""", (s["id"], as_of)):
            rec["ads"] += 1
            if r["eu_total_reach"] is not None:
                rec["eu_exact"] += 1
            elif r["uk_reach"] is not None:
                rec["uk_exact"] += 1
            elif r["reach_range_lower"] is not None:
                rec["range_only"] += 1
            else:
                rec["no_reach"] += 1
            if r["engagement_type"] == "boosted":
                rec["boosted"] += 1
                if r["comments"] is not None:
                    rec["with_comments"] += 1
            else:
                rec["dark"] += 1
            for k in (r["reach_keys"] or "").split(","):
                name = k.split("=")[0].split(":")[0].strip()
                if name:
                    rec["keys"][name] = rec["keys"].get(name, 0) + 1
        rows.append(rec)
        for k in ("ads", "eu_exact", "uk_exact", "range_only", "no_reach", "boosted", "with_comments", "dark"):
            total[k] += rec[k]
        for k, v in rec["keys"].items():
            total["keys"][k] = total["keys"].get(k, 0) + v
    if len(rows) > 1:
        rows.append(total)
    return rows


def payload_shape(conn: sqlite3.Connection, as_of: str, max_depth: int = 2) -> list[tuple[str, int, int]]:
    """Which fields the stored raw ad nodes actually contain: (key path, ads carrying it, ads where it is null).
    Depth-limited so the list stays readable; list items are collapsed to '[]'."""
    import json as _json
    from . import meta_ads as _m
    counts: dict[str, int] = defaultdict(int)
    nulls: dict[str, int] = defaultdict(int)
    n = 0
    for r in conn.execute("""SELECT a.raw_json FROM meta_ads a JOIN meta_ads_daily d ON d.ad_id = a.ad_id
                             WHERE d.snapshot_date = ? AND a.raw_json IS NOT NULL""", (as_of,)):
        try:
            node = _json.loads(r[0])
        except ValueError:
            continue
        n += 1
        seen: set[str] = set()
        for path, k, v in _m._walk_items(node):
            depth = path.count(".") + path.count("[") + (1 if path else 0)
            if depth >= max_depth:
                continue
            base = re.sub(r"\[\d+\]", "[]", path)
            key = f"{base}.{k}" if base else k
            if key in seen:
                continue
            seen.add(key)
            counts[key] += 1
            if v is None or v == [] or v == {}:
                nulls[key] += 1
    return sorted(((k, c, nulls[k]) for k, c in counts.items()), key=lambda t: (-t[1], t[0]))
