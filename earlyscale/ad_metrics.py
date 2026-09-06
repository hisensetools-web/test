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
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from . import config, db as _db, shopify

log = logging.getLogger("earlyscale.ad_metrics")

RULES = {
    5: "engagement_per_day up 2x week over week on one ad",
    6: "concept fully alive 14+ days while the page's active concepts fell",
    7: "new ad with lineage to an ad running 20+ days",
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

def handle_from_url(url: str | None) -> tuple[str | None, str | None]:
    """(product_handle, page_handle) straight from the URL path, if present."""
    if not url:
        return None, None
    path = urlparse(url).path
    m = PRODUCT_RE.search(path)
    if m:
        return m.group(1).lower().rstrip("-"), None
    m = PAGE_RE.search(path)
    if m:
        return None, m.group(1).lower()
    return None, None


def handles_from_html(html: str, variant_to_handle: dict[int, str] | None = None) -> list[tuple[str, int]]:
    """Product handles referenced by a landing page, most-referenced first.
    Looks at /products/<handle> links, /cart/add + variant ids, and embedded product JSON."""
    counts: dict[str, int] = defaultdict(int)
    for m in PRODUCT_RE.finditer(html):
        h = m.group(1).lower().rstrip("-").split(".")[0]
        if h and h not in ("json", "js"):
            counts[h] += 1
    if variant_to_handle:
        for m in VARIANT_RE.finditer(html):
            h = variant_to_handle.get(int(m.group(1)))
            if h:
                counts[h] += 2   # a buy button is stronger evidence than a link
    for m in HTML_PRODUCT_JSON_RE.finditer(html):
        counts[m.group(1).lower()] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


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

def _known_handles(conn: sqlite3.Connection, store_id: int) -> tuple[set[str], dict[int, str]]:
    latest = conn.execute("SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ?", (store_id,)).fetchone()[0]
    if not latest:
        return set(), {}
    handles = {r[0] for r in conn.execute(
        "SELECT handle FROM products_daily WHERE store_id = ? AND snapshot_date = ?", (store_id, latest))}
    v2h = {r["variant_id"]: r["handle"] for r in conn.execute(
        """SELECT v.variant_id, p.handle FROM variants_daily v JOIN products_daily p
           ON p.store_id = v.store_id AND p.snapshot_date = v.snapshot_date AND p.product_id = v.product_id
           WHERE v.store_id = ? AND v.snapshot_date = ?""", (store_id, latest))}
    return handles, v2h


def fetch_landing(session: requests.Session, url: str) -> tuple[str, str, int]:
    """GET a landing page (follows redirects). Returns (final_url, html, status)."""
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
            for part in hit["candidates"].split(","):
                h = part.split(":")[0].replace("via:", "")
                m = match_handle(h, known) if h and not h.startswith(("error", "unlisted-product")) else None
                if m:
                    hit["product_handle"] = m
                    conn.execute("UPDATE landing_pages SET product_handle = ? WHERE url = ?", (m, key))
                    break
        cache[key] = hit
        return hit
    if session is None:
        cache[key] = dict(row) if row else {"product_handle": None, "page_handle": handle_from_url(url)[1]}
        return cache[key]
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
        """INSERT OR REPLACE INTO landing_pages (url, fetched_at, status, final_url, product_handle, page_handle, candidates)
           VALUES (?,?,?,?,?,?,?)""",
        (hit["url"], hit["fetched_at"], hit["status"], hit["final_url"], hit["product_handle"],
         hit["page_handle"], hit["candidates"][:500]))
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
        if ph and ph not in known and same_store(a.get("landing_domain"), store_domain) and ph not in wanted:
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
                        product = shopify.normalise_product(data)
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


def process_store(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str,
                  session: requests.Session | None = None, fetch_landings: bool = True) -> dict:
    """Resolve landings, cluster concepts, find lineage, write per-ad and per-concept rows for `today`.
    Returns a summary dict."""
    ads = [dict(r) for r in conn.execute(
        """SELECT a.*, d.is_active, d.reactions, d.comments, d.shares
           FROM meta_ads a JOIN meta_ads_daily d ON d.ad_id = a.ad_id AND d.snapshot_date = ?
           WHERE a.store_id = ?""", (today, store_id))]
    if not ads:
        return {"ads": 0}
    known, v2h = _known_handles(conn, store_id)
    cache: dict[str, dict] = {}
    unlisted = discover_unlisted_products(conn, store_id, store_domain, ads, known, session if fetch_landings else None,
                                          cache, today)
    if unlisted:
        known, v2h = _known_handles(conn, store_id)
    resolved = 0
    for a in ads:
        r = resolve_landing(conn, store_id, store_domain, a, known, v2h, session if fetch_landings else None, cache, today)
        a["product_handle"], a["page_handle"] = r["product_handle"], r["page_handle"]
        a["landing_handle"] = handle_from_url(a.get("landing_url"))[0]   # raw advertised handle, suffix and all
        resolved += 1 if r["product_handle"] else 0
        conn.execute("""UPDATE meta_ads SET product_handle = ?, page_handle = ?, landing_resolved_via = ?, landing_handle = ?
                        WHERE ad_id = ?""",
                     (r["product_handle"], r["page_handle"], r["resolved_via"], a["landing_handle"], a["ad_id"]))

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

    # per-concept daily rows
    by_c: dict[str, list[dict]] = defaultdict(list)
    for a in ads:
        by_c[a["concept_id"]].append(a)
    conn.execute("DELETE FROM meta_concepts_daily WHERE snapshot_date = ? AND store_id = ?", (today, store_id))
    for cid, members in by_c.items():
        ever = conn.execute("SELECT COUNT(*) FROM meta_ads WHERE concept_id = ?", (cid,)).fetchone()[0] or len(members)
        active = sum(1 for m in members if m["is_active"])
        launch = min((m["ad_start_date"] or m["first_seen_date"]) for m in members)
        m0 = members[0]
        conn.execute(
            """INSERT INTO meta_concepts_daily
               (snapshot_date, store_id, concept_id, page_name, landing_url, product_handle, page_handle, launch_date,
                days_running, ads_ever, ads_active, survival)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (today, store_id, cid, m0.get("page_name"), _strip(m0["landing_url"]) if m0["landing_url"] else None,
             m0.get("product_handle"), m0.get("page_handle"), launch, _days(launch, today), ever, active,
             round(active / ever, 3) if ever else None))
    conn.commit()
    return {"ads": len(all_ads), "ignored": len(all_ads) - len(ads), "resolved": resolved, "concepts": len(by_c),
            "unlisted": unlisted,
            "lineage": len(lineage), "pages_fetched": sum(1 for h in cache.values() if h.get("status") is not None)}


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

    # rule 6: concept with all ads active 14+ days while the page's active concepts fell
    active_concepts_by_page = defaultdict(int)
    prev_active_by_page = defaultdict(int)
    for r in conn.execute("SELECT page_name, ads_active FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ?",
                          (store_id, today)):
        if r["ads_active"]:
            active_concepts_by_page[r["page_name"]] += 1
    prev_date = conn.execute("SELECT MAX(snapshot_date) FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date <= ?",
                             (store_id, week_ago)).fetchone()[0]
    if prev_date:
        for r in conn.execute("SELECT page_name, ads_active FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ?",
                              (store_id, prev_date)):
            if r["ads_active"]:
                prev_active_by_page[r["page_name"]] += 1
        for r in conn.execute(
            """SELECT concept_id, page_name, product_handle, days_running, ads_ever, ads_active, landing_url
               FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ?
                 AND days_running >= 14 AND ads_ever >= 2 AND ads_active = ads_ever""", (store_id, today)):
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
    lines = [f"# Alerts {today}", ""]
    for r in rows:
        lines.append(f"- **rule {r['rule']}** ({RULES.get(r['rule'], '')}) {r['store_domain']} "
                     f"{r['product_handle'] or ''}: {r['detail']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- Signals join

def meta_for_signals(conn: sqlite3.Connection, store_id: int, today: str) -> dict[str, dict]:
    """{product_handle: {ads_pointing_here, engagement_per_day, days_running_max, concept_status}}
    from the latest ad snapshot on/before `today`."""
    snap = conn.execute("SELECT MAX(snapshot_date) FROM meta_ads_daily WHERE store_id = ? AND snapshot_date <= ?",
                        (store_id, today)).fetchone()[0]
    if not snap:
        return {}
    out: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT a.product_handle, COUNT(*) AS n, MAX(d.days_running) AS dmax, AVG(d.engagement_per_day) AS epd
           FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
           WHERE d.store_id = ? AND d.snapshot_date = ? AND d.is_active = 1 AND a.product_handle IS NOT NULL
             AND COALESCE(a.page_ignored, 0) = 0
           GROUP BY a.product_handle""", (store_id, snap)):
        out[r["product_handle"]] = {"ads_pointing_here": r["n"], "days_running_max": r["dmax"],
                                    "engagement_per_day": None if r["epd"] is None else round(r["epd"], 1),
                                    "concept_status": ""}
    for r in conn.execute(
        """SELECT product_handle, COUNT(*) AS concepts, SUM(ads_active > 0) AS alive, MAX(days_running) AS oldest,
                  MAX(CASE WHEN ads_active = ads_ever THEN days_running END) AS oldest_intact
           FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ? AND product_handle IS NOT NULL
           GROUP BY product_handle""", (store_id, snap)):
        h = r["product_handle"]
        if h in out:
            intact = f", intact {r['oldest_intact']}d" if r["oldest_intact"] is not None else ""
            out[h]["concept_status"] = f"{r['alive']}/{r['concepts']} concepts alive{intact}"
    return out
