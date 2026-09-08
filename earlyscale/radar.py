"""Radar: find Shopify brands running advertorial-style ads that are not on the watchlist yet.

Sources
  hooks     radar/hooks.txt phrases searched in the Ad Library (country RADAR_COUNTRY, active ads),
            up to RADAR_MAX_ADS_PER_QUERY ads each, weekly
  copycat   for each hero product on the watchlist (top 3 by collection rank + published <= 30 days)
            the product title's distinctive words (brand words stripped) searched the same way, plus a
            web search for the title whose result domains are triaged
  manual    `tracker.py radar-add ...` and radar/imports/*.csv|txt: straight to the watchlist, source=manual

Triage (sources hooks / copycat), per landing domain not on the watchlist:
  1. /products.json -> Shopify? If not, follow the ads' landing pages to their checkout / shop links
     and test those domains; still nothing -> type=funnel, parked but tracked (ads, pages, landing URLs).
  2. Shopify: shop id + store_created_est (calibration), store_first_created (min created_at), products.
  3. Ad Library search for the domain: active ads, distinct pages, top page, example text, and whether a
     product published <= 30 days ago has >= 3 ads pointing at it.
  4. Promote when (store_age_days <= RADAR_MAX_AGE_DAYS OR hot new product) AND active_ads >= RADAR_MIN_ACTIVE_ADS;
     else Candidates. Every candidate is re-triaged daily and promoted the day it crosses, or when its
     "promote" cell on the Candidates tab is Y.
"""
from __future__ import annotations

import csv
import logging
import re
import sqlite3
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

from . import ad_metrics, config, meta_ads, shopify, store_age
from .watchlist import append_to_watchlist, normalise_domain, read_watchlist

log = logging.getLogger("earlyscale.radar")

ROOT = Path(config.__file__).resolve().parent.parent
HOOKS_PATH = ROOT / "radar" / "hooks.txt"
IMPORTS_DIR = ROOT / "radar" / "imports"

STOP = set("""a an the and or of for with to in on at by from is are was be this that these those your you my our their
its it as into over under about after before new best top free get buy now shop official original premium natural
formula supplement supplements capsules capsule tablets tablet softgels softgel gummies gummy drops oil powder pack bundle
kit set size count ct oz mg ml x per day daily plus pro max ultra advanced complete support health care""".split())
SKIP_DOMAINS = ("facebook.com", "fb.com", "instagram.com", "fbcdn.net", "google.com", "youtube.com", "amazon.com", "amzn.to",
                "tiktok.com", "bit.ly", "linktr.ee", "apple.com", "play.google.com", "shopify.com", "myshopify.com",
                "walmart.com", "ebay.com", "etsy.com", "wikipedia.org", "reddit.com", "duckduckgo.com")


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- inputs (pure)

def load_hooks(path: Path | None = None) -> list[str]:
    path = path or HOOKS_PATH
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in out:
            out.append(line)
    return out


def distinctive_words(title: str, brand_words: set[str] | None = None, max_words: int = 4) -> str:
    """The product title minus brand names, stop words and sizes: what a copycat would also have to say."""
    brand = {w.lower() for w in (brand_words or set())}
    words = re.findall(r"[a-z][a-z'\-]+", (title or "").lower())
    out = []
    for w in words:
        w = w.strip("'-")
        if len(w) < 3 or w in STOP or w in brand or w in out:
            continue
        out.append(w)
    return " ".join(out[:max_words])


def brand_words_for(store_domain: str, vendor: str | None = None) -> set[str]:
    host = re.sub(r"^https?://", "", store_domain).split("/")[0].lower()
    label = host.split(".")[0] if not host.startswith("www.") else host.split(".")[1]
    words = {label, host}
    for w in re.findall(r"[a-z]+", (vendor or "").lower()):
        words.add(w)
    for w in re.findall(r"[a-z]+", label):
        words.add(w)
    return words


def hero_queries(conn: sqlite3.Connection, today: str, top_n: int = 3, new_days: int = 30) -> list[dict]:
    """[(store_domain, handle, query)] for every watchlist store's hero products."""
    from . import inventory
    out = []
    t = date.fromisoformat(today)
    for s in conn.execute("SELECT id, store_domain FROM stores"):
        _, products = inventory.latest_products(conn, s["id"])
        if not products:
            continue
        ranked = sorted((p for p in products if p.get("collection_position") is not None), key=lambda p: p["collection_position"])[:top_n]
        chosen = {p["product_id"]: p for p in ranked}
        for p in products:
            pub = (p.get("published_at") or "")[:10]
            try:
                if pub and (t - date.fromisoformat(pub)).days <= new_days:
                    chosen.setdefault(p["product_id"], p)
            except ValueError:
                pass
        vendor = conn.execute("SELECT vendor FROM products_daily WHERE store_id = ? AND vendor IS NOT NULL LIMIT 1", (s["id"],)).fetchone()
        bw = brand_words_for(s["store_domain"], vendor[0] if vendor else None)
        for p in chosen.values():
            if inventory.is_excluded(p.get("title"), p.get("handle"), p.get("product_type"), p.get("tags")):
                continue                 # shipping protection, gift cards, memberships: not products anyone copies
            q = distinctive_words(p.get("title") or "", bw)
            if len(q.split()) >= 2:
                out.append({"store": s["store_domain"], "handle": p["handle"], "query": q, "title": p.get("title")})
    return out


def normalise_landing_domain(url_or_domain: str | None) -> str | None:
    if not url_or_domain:
        return None
    s = url_or_domain.strip().lower()
    if "://" not in s:
        s = "https://" + s
    netloc = urlparse(s).netloc.split("@")[-1]
    host, _, port = netloc.partition(":")
    host = re.sub(r"^www\.", "", host)
    if not host or "." not in host:
        return None
    if port and port not in ("80", "443"):
        host = f"{host}:{port}"          # local mock stores; real stores never carry a port
        if s.startswith("http://"):
            host = "http://" + host      # keep the explicit scheme the mock needs (watchlist does the same)
    if any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS):
        return None
    return host


# ---------------------------------------------------------------- Ad Library searches

def search_ads(browser, query: str | None, max_ads: int | None = None, search_type: str = "keyword_unordered",
               page_id: str | None = None) -> list[dict]:
    """One Ad Library search: a phrase / domain (keyword_unordered) or every active ad of one page (page_id)."""
    url = meta_ads.build_search_url(query=query, page_id=page_id, country=config.RADAR_COUNTRY, search_type=search_type)
    if hasattr(browser, "get"):          # meta_ads.BrowserHandle: relaunches a dead Chromium
        browser = browser.get()
    res = meta_ads.scrape_page(url, max_ads=max_ads or config.RADAR_MAX_ADS_PER_QUERY, browser=browser)
    if res.blocked:
        raise meta_ads.MetaBlocked(res.note)
    return res.ads


def _retry_locked(fn, tries: int = 6, pause: float = 10.0):
    """Run fn(); on 'database is locked' (another tracker command writing) wait and retry."""
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == tries - 1:
                raise
            log.warning("radar: database is locked (another tracker command running?); retrying in %.0f s", pause)
            time.sleep(pause)


def record_ads(conn: sqlite3.Connection, ads: list[dict], query: str, source: str, today: str) -> tuple[int, set[str]]:
    """Upsert every ad (first query keeps the row; every query gets a radar_ad_hits row for the yield report)."""
    domains: set[str] = set()

    def write():
        for a in ads:
            dom = normalise_landing_domain(a.get("landing_url") or a.get("landing_domain"))
            body = a.get("primary_text") or ""
            conn.execute("""INSERT INTO radar_ads (ad_id, query, source, page_id, page_name, landing_url, landing_domain, body_len, body_snippet,
                              start_date, first_seen, last_seen, is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(ad_id) DO UPDATE SET last_seen = excluded.last_seen, is_active = excluded.is_active,
                              landing_domain = COALESCE(excluded.landing_domain, landing_domain), page_name = COALESCE(excluded.page_name, page_name),
                              page_id = COALESCE(excluded.page_id, page_id)""",
                         (a["ad_id"], query, source, a.get("page_id"), a.get("page_name"), a.get("landing_url"), dom, len(body), body[:200],
                          a.get("start_date"), today, today, 1 if a.get("is_active", True) else 0))
            conn.execute("INSERT OR IGNORE INTO radar_ad_hits (ad_id, source, query, landing_domain, seen) VALUES (?,?,?,?,?)",
                         (a["ad_id"], source, query, dom, today))
            if dom:
                domains.add(dom)
        conn.commit()
    _retry_locked(write)
    return len(ads), domains


def _run(conn, kind, query, started, ads=None, domains=None, note=""):
    def write():
        conn.execute("INSERT INTO radar_runs (kind, query, started_at, ended_at, ads_found, domains_found, note) VALUES (?,?,?,?,?,?,?)",
                     (kind, query, started, _utcnow(), ads, domains, note[:300]))
        conn.commit()
    _retry_locked(write)


def recently_searched(conn: sqlite3.Connection, kind: str, days: int) -> set[str]:
    """Queries of this kind that completed without error in the last `days` days (so a crashed or budget-capped
    sweep resumes where it stopped instead of repeating the phrases it already did)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    return {r[0] for r in conn.execute("SELECT DISTINCT query FROM radar_runs WHERE kind = ? AND note = '' AND started_at >= ?", (kind, cutoff))}


def sweep(conn: sqlite3.Connection, browser, queries: list[tuple[str, str]], today: str, search=search_ads, wait=None,
          deadline: float | None = None, skip_days: int | None = None) -> dict:
    """queries: [(query, source)]. Returns {queries, ads, domains, deferred, skipped}."""
    wait = wait or meta_ads._wait
    total_ads, all_domains = 0, set()
    deferred = skipped = 0
    done_recently = recently_searched(conn, queries[0][1].split(":")[0], skip_days) if (skip_days and queries) else set()
    pending = [(q, src) for q, src in queries if q not in done_recently]
    skipped = len(queries) - len(pending)
    if skipped:
        log.info("radar: %d quer%s already searched in the last %d days; continuing with the other %d",
                 skipped, "y" if skipped == 1 else "ies", skip_days, len(pending))
    for i, (q, source) in enumerate(pending):
        if deadline is not None and time.monotonic() > deadline:
            deferred = len(pending) - i
            log.warning("radar: budget spent; %d quer%s deferred to the next sweep", deferred, "y" if deferred == 1 else "ies")
            break
        started = _utcnow()
        try:
            ads = search(browser, q)
        except meta_ads.MetaBlocked as e:
            _run(conn, source.split(":")[0], q, started, note=f"blocked: {e}")
            log.error("radar: Ad Library blocked on %r (%s); stopping this sweep", q, e)
            break
        except Exception as e:  # noqa: BLE001
            _run(conn, source.split(":")[0], q, started, note=f"error: {e}")
            log.warning("radar: %r failed: %s", q, e)
            continue
        try:
            n, doms = record_ads(conn, ads, q, source, today)
        except sqlite3.OperationalError as e:
            _run(conn, source.split(":")[0], q, started, note=f"db error: {e}")
            log.error("radar: could not store the ads for %r (%s); the phrase is searched again next time", q, e)
            continue
        _run(conn, source.split(":")[0], q, started, n, len(doms))
        log.info("radar: %-40s ads=%-4d domains=%d", q[:40], n, len(doms))
        total_ads += n
        all_domains |= doms
        if i < len(pending) - 1:
            wait()
    return {"queries": len(pending) - deferred, "ads": total_ads, "domains": all_domains, "deferred": deferred, "skipped": skipped}


# ---------------------------------------------------------------- web search (copycat, best effort)

def web_search_domains(query: str, session: requests.Session | None = None) -> list[str]:
    """Result domains for a query from DuckDuckGo's HTML endpoint (no JS, no login). Best effort."""
    session = session or shopify.make_session()
    try:
        r = session.get("https://html.duckduckgo.com/html/", params={"q": query + " shop"}, timeout=config.REQUEST_TIMEOUT,
                        headers={"Accept": "text/html"})
        if r.status_code != 200:
            return []
    except requests.RequestException:
        return []
    out = []
    for href in re.findall(r'href="([^"]+)"', r.text):
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        dom = normalise_landing_domain(href)
        if dom and dom not in out:
            out.append(dom)
    return out[:10]


# ---------------------------------------------------------------- triage

def known_domains(conn: sqlite3.Connection, watchlist_path: Path | None = None) -> tuple[set[str], set[str]]:
    wl = {normalise_landing_domain(s["store_domain"]) or s["store_domain"] for s in read_watchlist(watchlist_path)}
    seen = {r[0] for r in conn.execute("SELECT domain FROM radar_domains")}
    return wl, seen


def new_domains(conn: sqlite3.Connection, watchlist_path: Path | None = None) -> list[tuple[str, str, int]]:
    """[(domain, source, ads_in_sweeps)] seen by radar ads but neither on the watchlist nor triaged before, most ads
    first. Discarded non-Shopify domains come back once enough ads point at them to count as a funnel."""
    wl, seen = known_domains(conn, watchlist_path)
    revive = {r[0] for r in conn.execute("SELECT domain FROM radar_domains WHERE status = 'discarded'")}
    out = []
    for r in conn.execute("""SELECT landing_domain, MIN(source) AS src, COUNT(*) AS n FROM radar_ads
                             WHERE landing_domain IS NOT NULL AND COALESCE(is_active, 1) = 1
                             GROUP BY landing_domain ORDER BY n DESC"""):
        d = r["landing_domain"]
        if not d or d in wl:
            continue
        if d not in seen or (d in revive and r["n"] >= config.RADAR_FUNNEL_MIN_ADS):
            out.append((d, r["src"], r["n"]))
    return out


def is_shopify(domain: str, session=None) -> tuple[bool, list[dict]]:
    """products.json check; returns (True, normalised products) or (False, [])."""
    try:
        raw, positions, _ = shopify.fetch_store(domain, session)
        return True, shopify.normalise_products(raw, positions)
    except Exception:  # noqa: BLE001 - StoreFetchError, JSON errors, network
        return False, []


CTA_HINT = re.compile(r"/products/|/cart|/checkout|/collections|/order|shop|buy|store|get-|offer|discount", re.I)


def outbound_shop_domains(html: str, own_domain: str) -> list[str]:
    """Other domains a lander links to, most CTA-like first."""
    scores: Counter = Counter()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html or ""):
        href = m.group(1)
        dom = normalise_landing_domain(href)
        if not dom or dom == own_domain or dom.endswith("." + own_domain):
            continue
        scores[dom] += 3 if CTA_HINT.search(href) else 1
    return [d for d, _ in scores.most_common(5)]


def landing_urls(conn: sqlite3.Connection, domain: str, limit: int = 3) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT landing_url FROM radar_ads WHERE landing_domain = ? AND landing_url IS NOT NULL LIMIT ?",
                                       (domain, limit))]


def follow_lander_urls(urls: list[str], domain: str, session=None, fetch=ad_metrics.fetch_landing, check=is_shopify) -> tuple[str | None, list[dict]]:
    """For a non-Shopify landing domain: open up to 3 of its ad landing pages, collect the shop domains they
    link to, and return the first that is a Shopify storefront (with its products). Pure HTTP, thread-safe."""
    session = session or shopify.make_session()
    cands: Counter = Counter()
    for u in urls:
        try:
            _, html, status = fetch(session, u)
        except Exception:  # noqa: BLE001
            continue
        for i, d in enumerate(outbound_shop_domains(html, domain)):
            cands[d] += 5 - i
    for d, _ in cands.most_common(3):
        ok, products = check(d, session)
        if ok:
            return d, products
    return None, []


def follow_lander(conn: sqlite3.Connection, domain: str, session=None, fetch=ad_metrics.fetch_landing, check=is_shopify) -> tuple[str | None, list[dict]]:
    return follow_lander_urls(landing_urls(conn, domain), domain, session, fetch=fetch, check=check)


def classify_domain(domain: str, urls: list[str], session=None, check=is_shopify, fetch=ad_metrics.fetch_landing) -> dict:
    """Stage 1 of triage, HTTP only (runs in a thread pool): Shopify storefront, a lander in front of one, or a funnel.
    Returns {type, store_domain, products, lander_domain}."""
    session = session or shopify.make_session()
    ok, products = check(domain, session)
    if ok:
        return {"type": "shopify", "store_domain": domain, "products": products, "lander_domain": None}
    shop, products = follow_lander_urls(urls, domain, session, fetch=fetch, check=check)
    if shop:
        return {"type": "shopify", "store_domain": shop, "products": products, "lander_domain": domain}
    return {"type": "funnel", "store_domain": domain, "products": [], "lander_domain": None}


def classify_many(conn: sqlite3.Connection, domains: list[str], check=is_shopify, fetch=ad_metrics.fetch_landing,
                  workers: int | None = None, deadline: float | None = None):
    """Yield (domain, classification) for many domains, checked concurrently (each on its own HTTP session)."""
    from concurrent.futures import ThreadPoolExecutor
    urls = {d: landing_urls(conn, d) for d in domains}
    workers = workers or config.RADAR_CHECK_WORKERS

    def one(d):
        try:
            return d, classify_domain(d, urls[d], shopify.make_session(), check=check, fetch=fetch)
        except Exception as e:  # noqa: BLE001
            log.warning("radar: classify %s failed: %s", d, e)
            return d, {"type": "funnel", "store_domain": d, "products": [], "lander_domain": None}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i in range(0, len(domains), workers * 4):          # small batches so a spent budget stops quickly
            if deadline is not None and time.monotonic() > deadline:
                return
            for d, cls in pool.map(one, domains[i:i + workers * 4]):
                yield d, cls


def domain_ads(conn: sqlite3.Connection, domain: str, products: list[dict], today: str) -> dict:
    """Active ads / pages / top page / example text / hot new product for a domain from radar_ads."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM radar_ads WHERE landing_domain = ? AND COALESCE(is_active, 1) = 1", (domain,))]
    pages = Counter((r["page_name"] or r["page_id"] or "") for r in rows)
    handles: Counter = Counter()
    for r in rows:
        h, _ = ad_metrics.handle_from_url(r["landing_url"])
        if h:
            handles[h] += 1
    t = date.fromisoformat(today)
    hot = None
    for p in products:
        pub = (p.get("published_at") or "")[:10]
        try:
            if pub and (t - date.fromisoformat(pub)).days <= 30 and handles.get(p["handle"], 0) >= 3:
                hot = p["handle"]
                break
        except ValueError:
            continue
    example = max((r["body_snippet"] or "" for r in rows), key=len, default="")
    return {"active_ads": len(rows), "pages": len([p for p in pages if p]), "top_page": (pages.most_common(1)[0][0] if pages else None),
            "example_text": example[:200], "hot_new_product": hot}


def top_page_ids(conn: sqlite3.Connection, domains: list[str], limit: int = 3) -> list[str]:
    c: Counter = Counter()
    for d in domains:
        for r in conn.execute("SELECT page_id, COUNT(*) n FROM radar_ads WHERE landing_domain = ? AND page_id IS NOT NULL GROUP BY page_id", (d,)):
            c[r["page_id"]] += r["n"]
    return [pid for pid, _ in c.most_common(limit)]


def decide(age_days: int | None, active_ads: int, hot_new_product: str | None) -> bool:
    young = age_days is not None and age_days <= config.RADAR_MAX_AGE_DAYS
    return (young or bool(hot_new_product)) and active_ads >= config.RADAR_MIN_ACTIVE_ADS


def _eligible(rec: dict) -> bool:
    """Could this row ever promote? Shopify stores that are young or have a hot new product. Old stores without one
    can only get there through a new product, which the weekly products refresh picks up."""
    if rec.get("type") != "shopify":
        return False
    age = rec.get("store_age_days")
    return (age is not None and age <= config.RADAR_MAX_AGE_DAYS) or bool(rec.get("hot_new_product"))


def _needs_search(rec: dict, today: str) -> bool:
    """An Ad Library search (minutes) is spent only where it can change the verdict: eligible stores and funnels with
    enough ads, and not more often than every RADAR_RESEARCH_DAYS days."""
    if rec.get("status") not in (None, "candidate"):
        return False
    last = rec.get("searched_at")
    if last and (date.fromisoformat(today) - date.fromisoformat(last)).days < config.RADAR_RESEARCH_DAYS:
        return False
    if rec.get("type") == "funnel":
        return (rec.get("ads_in_sweeps") or 0) >= config.RADAR_FUNNEL_MIN_ADS
    return _eligible(rec) and (rec.get("active_ads") or 0) < config.RADAR_MIN_ACTIVE_ADS


def _store_facts(conn, rec: dict, store_domain: str, products: list[dict], today: str, identity, session) -> None:
    rec["products"] = len(products)
    firsts = sorted(p["created_at"][:10] for p in products if p.get("created_at"))
    rec["store_first_created"] = firsts[0] if firsts else rec.get("store_first_created")
    rec["products_fetched"] = today
    rec["handles_new"] = ",".join(sorted(p["handle"] for p in products if _published_within(p, today, 30)))[:2000]
    if rec.get("shop_id") is None:
        try:
            rec["shop_id"] = identity(store_domain, session).get("shop_id")
        except Exception:  # noqa: BLE001
            pass
    cal = store_age.build_calibration(conn)
    est = store_age.estimate_created(rec.get("shop_id"), cal)["created"] if rec.get("shop_id") else None
    rec["store_created_est"] = est.isoformat() if est else rec["store_first_created"]


def _published_within(p: dict, today: str, days: int) -> bool:
    pub = (p.get("published_at") or "")[:10]
    try:
        return bool(pub) and (date.fromisoformat(today) - date.fromisoformat(pub)).days <= days
    except ValueError:
        return False


def _stats(conn, rec: dict, store_domain: str, products: list[dict], today: str) -> None:
    """active_ads / pages / top page / example / hot from what radar_ads holds for the store (+ its lander)."""
    if not products and rec.get("handles_new"):
        # no fresh catalogue this run: the handles published <= 30 days ago from the last fetch still identify a hot product
        products = [{"handle": h, "published_at": today} for h in rec["handles_new"].split(",") if h]
    stats = domain_ads(conn, store_domain, products, today)
    if rec.get("lander_domain"):
        ls = domain_ads(conn, rec["lander_domain"], [], today)
        stats["active_ads"] += ls["active_ads"]
        stats["pages"] = max(stats["pages"], ls["pages"])
        stats["top_page"] = stats["top_page"] or ls["top_page"]
        stats["example_text"] = stats["example_text"] or ls["example_text"]
    rec.update({k: stats[k] for k in ("active_ads", "pages", "top_page", "example_text", "hot_new_product")})
    rec["ads_in_sweeps"] = conn.execute("""SELECT COUNT(DISTINCT ad_id) FROM radar_ad_hits WHERE landing_domain IN (?, ?)
                                           AND (source LIKE 'hook:%' OR source LIKE 'copycat:%')""",
                                        (store_domain, rec.get("lander_domain") or store_domain)).fetchone()[0]
    rec["store_age_days"] = store_age.age_days(rec["store_created_est"], today) if rec.get("store_created_est") else None


DOMAIN_COLS = ("domain", "type", "status", "source", "first_seen", "last_checked", "lander_domain", "shop_id", "store_created_est",
               "store_first_created", "store_age_days", "products", "active_ads", "pages", "top_page", "hot_new_product", "example_text",
               "promote_flag", "promoted_at", "note", "searched_at", "ads_in_sweeps", "products_fetched", "handles_new", "store_domain")


def _save(conn, rec: dict) -> None:
    def write():
        conn.execute(f"INSERT OR REPLACE INTO radar_domains ({', '.join(DOMAIN_COLS)}) VALUES ({', '.join('?' * len(DOMAIN_COLS))})",
                     tuple(rec.get(c) for c in DOMAIN_COLS))
        conn.commit()
    _retry_locked(write)


def triage_domain(conn: sqlite3.Connection, browser, domain: str, source: str, today: str, session=None,
                  search=search_ads, check=is_shopify, follow=None, identity=store_age.fetch_identity,
                  watchlist_path: Path | None = None, fetch=ad_metrics.fetch_landing, classified: dict | None = None,
                  allow_search: bool = True, refetch: bool = True) -> dict:
    """Classify one domain, write its radar_domains row, promote when it qualifies. Returns the row (+ 'promoted', 'searched').

    Stage 1 (HTTP): Shopify check, lander follow, store facts. Skipped when `classified` is passed (thread pool) or
    `refetch` is False (daily re-check from stored facts). Stage 2 (browser, minutes): an Ad Library search by the
    store's top pages (fallback: the domain as a keyword), only when `_needs_search` says it can change the verdict."""
    session = session or shopify.make_session()
    row = conn.execute("SELECT * FROM radar_domains WHERE domain = ?", (domain,)).fetchone()
    rec = dict(row) if row else {"domain": domain, "status": None, "source": source, "first_seen": today}
    rec["last_checked"] = today
    products: list[dict] = []
    if classified is None and (refetch or not rec.get("type")):
        if follow is not None:                      # legacy hook used by tests: (conn, domain, session) -> (shop, products)
            ok, products = check(domain, session)
            if ok:
                classified = {"type": "shopify", "store_domain": domain, "products": products, "lander_domain": None}
            else:
                shop, products = follow(conn, domain, session)
                classified = ({"type": "shopify", "store_domain": shop, "products": products, "lander_domain": domain} if shop
                              else {"type": "funnel", "store_domain": domain, "products": [], "lander_domain": None})
        else:
            classified = classify_domain(domain, landing_urls(conn, domain), session, check=check, fetch=fetch)
    if classified is not None:
        rec["type"], rec["lander_domain"] = classified["type"], classified["lander_domain"]
        products = classified["products"]
        if classified["lander_domain"]:
            log.info("radar: %s is a lander; its checkout is %s", domain, classified["store_domain"])
    if classified is not None:
        rec["store_domain"] = classified["store_domain"]
    store_domain = rec.get("store_domain") or domain
    if rec["type"] == "shopify" and products:
        _store_facts(conn, rec, store_domain, products, today, identity, session)
    elif rec["type"] != "shopify":
        rec["products"] = 0
    _stats(conn, rec, store_domain, products, today)
    forced = (rec.get("promote_flag") or "").upper() == "Y"
    promote = rec["type"] == "shopify" and (forced or decide(rec["store_age_days"], rec["active_ads"], rec["hot_new_product"]))
    rec["searched"] = False
    if not promote and allow_search and _needs_search(rec, today):
        if True:
            pids = top_page_ids(conn, [store_domain] + ([rec["lander_domain"]] if rec.get("lander_domain") else []))
            try:
                if pids:
                    for pid in pids:
                        ads = search(browser, None, max_ads=config.RADAR_SEARCH_MAX_ADS, page_id=pid)
                        record_ads(conn, ads, f"page:{pid}", f"page:{pid}", today)
                else:
                    ads = search(browser, store_domain, max_ads=config.RADAR_SEARCH_MAX_ADS)
                    record_ads(conn, ads, store_domain, f"domain:{store_domain}", today)
                rec["searched_at"], rec["searched"] = today, True
            except Exception as e:  # noqa: BLE001
                log.warning("radar: ad search for %s failed: %s", store_domain, e)
            _stats(conn, rec, store_domain, products, today)
            promote = rec["type"] == "shopify" and decide(rec["store_age_days"], rec["active_ads"], rec["hot_new_product"])
    if promote and rec.get("status") != "promoted":
        note = f"radar: {rec['source']} {today}" + (f" via {domain}" if rec.get("lander_domain") else "")
        append_to_watchlist({"store_domain": store_domain, "meta_page_name": rec.get("top_page") or "", "notes": note}, watchlist_path)
        rec["status"], rec["promoted_at"] = "promoted", today
        log.info("radar: PROMOTED %s (age %s d, %s active ads, hot=%s) -> watchlist", store_domain, rec.get("store_age_days"),
                 rec["active_ads"], rec.get("hot_new_product"))
    elif rec.get("status") not in ("promoted", "watchlist"):
        if rec["type"] == "funnel" and (rec.get("ads_in_sweeps") or 0) < config.RADAR_FUNNEL_MIN_ADS:
            rec["status"] = "discarded"          # non-Shopify with too few ads to be worth tracking (revived if more show up)
        else:
            rec["status"] = "candidate"
    rec["domain"] = domain
    _save(conn, rec)
    rec["promoted"] = promote and rec.get("promoted_at") == today
    return rec


# ---------------------------------------------------------------- manual import

def parse_import_text(text: str) -> list[str]:
    """Domains/URLs from a txt/csv: one per line, or a CSV with a domain/website/url/landing/link column."""
    text = text.strip()
    if not text:
        return []
    lines = text.splitlines()
    out: list[str] = []
    if "," in lines[0] or ";" in lines[0]:
        rows = list(csv.DictReader(lines, delimiter=";" if ";" in lines[0] and "," not in lines[0] else ","))
        if rows:
            key = next((k for k in rows[0].keys() if k and re.search(r"domain|website|url|landing|link|store", k, re.I)), None)
            if key:
                for r in rows:
                    d = normalise_landing_domain(r.get(key) or "")
                    if d and d not in out:
                        out.append(d)
                return out
    for line in lines:
        line = line.strip().strip(",;")
        if not line or line.startswith("#"):
            continue
        d = normalise_landing_domain(line.split(",")[0])
        if d and d not in out:
            out.append(d)
    return out


def manual_add(conn: sqlite3.Connection, items: list[str], today: str, source: str = "manual", watchlist_path: Path | None = None) -> dict:
    added, existing, bad = [], [], []
    for item in items:
        d = normalise_landing_domain(item)
        if not d:
            bad.append(item)
            continue
        if append_to_watchlist({"store_domain": d, "notes": f"radar: {source} {today}"}, watchlist_path):
            added.append(d)
        else:
            existing.append(d)
        conn.execute("""INSERT OR REPLACE INTO radar_domains (domain, type, status, source, first_seen, last_checked, promoted_at)
                        VALUES (?, 'shopify', 'watchlist', ?, ?, ?, ?)""", (d, source, today, today, today))
    conn.commit()
    return {"added": added, "existing": existing, "bad": bad}


def import_folder(conn: sqlite3.Connection, today: str, folder: Path | None = None, watchlist_path: Path | None = None) -> dict:
    folder = folder or IMPORTS_DIR
    done = folder / "done"
    summary = {"files": 0, "added": [], "existing": [], "bad": []}
    if not folder.exists():
        return summary
    for f in sorted(folder.glob("*")):
        if not f.is_file() or f.suffix.lower() not in (".csv", ".txt"):
            continue
        items = parse_import_text(f.read_text(encoding="utf-8-sig", errors="replace"))
        r = manual_add(conn, items, today, source=f"manual:{f.name}", watchlist_path=watchlist_path)
        for k in ("added", "existing", "bad"):
            summary[k] += r[k]
        summary["files"] += 1
        done.mkdir(exist_ok=True)
        f.rename(done / f"{today}_{f.name}")
    return summary


# ---------------------------------------------------------------- orchestration + tab

def run_radar(conn: sqlite3.Connection, browser, today: str, do_sweep: bool | None = None, session=None,
              search=search_ads, check=is_shopify, identity=store_age.fetch_identity, web_search=web_search_domains,
              watchlist_path: Path | None = None, wait=None, max_minutes: float | None = None,
              fetch=ad_metrics.fetch_landing, workers: int | None = None) -> dict:
    """imports -> (weekly) hook + copycat sweeps -> classify every new landing domain (HTTP, threads) ->
    daily re-check of every candidate from stored facts -> Ad Library searches where they can change a verdict."""
    session = session or shopify.make_session()
    wait = wait or meta_ads._wait
    if do_sweep is None:
        do_sweep = date.fromisoformat(today).weekday() == config.RADAR_SWEEP_WEEKDAY
    budget = (max_minutes if max_minutes is not None else config.RADAR_MAX_MINUTES) * 60
    t0 = time.monotonic()
    deadline = t0 + budget
    # sweeps stop early enough to leave RADAR_TRIAGE_MINUTES for the classification + searches that follow
    sweep_deadline = t0 + max(0.0, budget - min(config.RADAR_TRIAGE_MINUTES * 60, budget / 2))
    out = {"imports": import_folder(conn, today, watchlist_path=watchlist_path), "sweep": None, "copycat": None, "web": None,
           "found": 0, "shopify": 0, "discarded": 0, "promoted": 0, "parked": 0, "funnels": 0, "retriaged": 0, "promoted_later": 0,
           "deferred_triage": 0, "searched": 0, "deferred_search": 0, "refreshed": 0}
    if do_sweep:
        hooks = load_hooks()
        out["sweep"] = sweep(conn, browser, [(h, f"hook:{h}") for h in hooks], today, search=search, wait=wait,
                             deadline=sweep_deadline, skip_days=config.RADAR_RESWEEP_DAYS)
        heroes = hero_queries(conn, today)
        # copycat queries rotate: least recently searched first, RADAR_MAX_COPYCAT_QUERIES per sweep
        last = {r[0]: r[1] for r in conn.execute("SELECT query, MAX(started_at) FROM radar_runs WHERE kind = 'copycat' GROUP BY query")}
        seen_q = set()
        qs = []
        for h in heroes:
            if h["query"] not in seen_q:
                seen_q.add(h["query"])
                qs.append((h["query"], f"copycat:{h['store']}/{h['handle']}"))
        qs.sort(key=lambda x: last.get(x[0]) or "")
        qs = qs[: config.RADAR_MAX_COPYCAT_QUERIES]
        out["copycat"] = sweep(conn, browser, qs, today, search=search, wait=wait, deadline=sweep_deadline,
                               skip_days=config.RADAR_RESWEEP_DAYS)
        if config.RADAR_WEB_SEARCH and web_search is not None:
            web_domains = set()
            for h in heroes[: config.RADAR_MAX_WEB_QUERIES]:
                if time.monotonic() > sweep_deadline:
                    break
                for d in web_search(h["title"] or h["query"], session):
                    web_domains.add((d, f"web:{h['store']}/{h['handle']}"))
                time.sleep(1.0)
            for d, src in web_domains:
                conn.execute("""INSERT OR IGNORE INTO radar_ads (ad_id, query, source, landing_domain, first_seen, last_seen, is_active)
                                VALUES (?,?,?,?,?,?,0)""", (f"web:{d}", src, src, d, today, today))
            conn.commit()
            out["web"] = {"queries": min(len(heroes), config.RADAR_MAX_WEB_QUERIES), "domains": len(web_domains)}
    # stage 1: classify every new landing domain (HTTP only, concurrent), plus a weekly catalogue refresh of Shopify candidates
    todo = new_domains(conn, watchlist_path)
    out["found"] = len(todo)
    src_of = {d: src for d, src, _ in todo}
    stale = (date.fromisoformat(today) - timedelta(days=config.RADAR_REFRESH_DAYS)).isoformat()
    refresh_rows = {r["domain"]: r["source"] for r in conn.execute("""SELECT domain, source FROM radar_domains WHERE status = 'candidate'
                                                                       AND type = 'shopify' AND COALESCE(products_fetched, '') < ?""", (stale,))}
    stage1 = [d for d, _, _ in todo] + [d for d in refresh_rows if d not in src_of]
    done = 0
    for dom, cls in classify_many(conn, stage1, check=check, fetch=fetch, workers=workers, deadline=deadline):
        done += 1
        rec = triage_domain(conn, browser, dom, src_of.get(dom) or refresh_rows.get(dom) or "?", today, session, search=search, check=check,
                            identity=identity, watchlist_path=watchlist_path, fetch=fetch, classified=cls, allow_search=False)
        if dom not in src_of:
            out["refreshed"] += 1
            if rec["promoted"]:
                out["promoted_later"] += 1
    if done < len(stage1):
        out["deferred_triage"] = len(stage1) - done
        log.warning("radar: budget spent; %d domain(s) wait for the next run", out["deferred_triage"])
    # daily re-check of every other candidate from stored facts (age moves, promote=Y from the sheet, new sweep ads)
    checked_today = set(stage1)
    for c in [dict(r) for r in conn.execute("SELECT * FROM radar_domains WHERE status = 'candidate' AND COALESCE(last_checked, '') < ?", (today,))]:
        if c["domain"] in checked_today:
            continue
        rec = triage_domain(conn, browser, c["domain"], c["source"], today, session, search=search, check=check, identity=identity,
                            watchlist_path=watchlist_path, fetch=fetch, allow_search=False, refetch=False)
        out["retriaged"] += 1
        if rec["promoted"]:
            out["promoted_later"] += 1
    # stage 2: Ad Library searches where they can change a verdict, most sweep ads first, within the budget
    cands = [dict(r) for r in conn.execute("SELECT * FROM radar_domains WHERE status = 'candidate' ORDER BY ads_in_sweeps DESC, store_age_days ASC")]
    queue = [c for c in cands if _needs_search(c, today)]
    queue.sort(key=lambda c: (0 if c["type"] == "shopify" else 1, -(c.get("ads_in_sweeps") or 0)))
    batch = queue[: config.RADAR_MAX_TRIAGE]
    out["deferred_search"] = len(queue) - len(batch)
    for i, c in enumerate(batch):
        if time.monotonic() > deadline:
            out["deferred_search"] = len(queue) - i
            log.warning("radar: budget spent; %d Ad Library search(es) wait for the next run", out["deferred_search"])
            break
        rec = triage_domain(conn, browser, c["domain"], c["source"], today, session, search=search, check=check, identity=identity,
                            watchlist_path=watchlist_path, fetch=fetch, allow_search=True, refetch=False)
        out["searched"] += 1 if rec.get("searched") else 0
        if rec["promoted"] and c["domain"] not in src_of:
            out["promoted_later"] += 1
        if i < len(batch) - 1:
            wait()
    # what became of this run's new domains, after every stage
    for r in conn.execute("SELECT domain, type, status, last_checked FROM radar_domains"):
        if r["domain"] not in src_of or r["last_checked"] != today:
            continue
        out["shopify"] += 1 if r["type"] == "shopify" else 0
        if r["status"] == "promoted":
            out["promoted"] += 1
        elif r["status"] == "discarded":
            out["discarded"] += 1
        else:
            out["parked"] += 1
            out["funnels"] += 1 if r["type"] == "funnel" else 0
    return out


CANDIDATES_HEADERS = ["domain", "type", "status", "first_seen", "store_age_days", "store_created_est", "store_first_created", "products",
                      "active_ads", "ads_in_sweeps", "searched_at", "pages", "top page", "example ad text", "hot new product", "source",
                      "lander_domain", "last_checked", "promote"]


def candidates_rows(conn: sqlite3.Connection) -> list[list]:
    """Candidates first (most active ads first), then promoted rows. active_ads counts every ad radar has seen landing
    on the domain; until searched_at is set that is a lower bound from the sweeps only."""
    rows = []
    for r in conn.execute("""SELECT * FROM radar_domains WHERE status IN ('candidate', 'promoted') ORDER BY
                             CASE status WHEN 'candidate' THEN 0 ELSE 1 END, active_ads DESC, first_seen DESC"""):
        rows.append([r["domain"], r["type"] or "", r["status"], r["first_seen"], "" if r["store_age_days"] is None else r["store_age_days"],
                     r["store_created_est"] or "", r["store_first_created"] or "", "" if r["products"] is None else r["products"],
                     "" if r["active_ads"] is None else r["active_ads"], "" if r["ads_in_sweeps"] is None else r["ads_in_sweeps"],
                     r["searched_at"] or "", "" if r["pages"] is None else r["pages"], r["top_page"] or "",
                     (r["example_text"] or "")[:200], r["hot_new_product"] or "", r["source"] or "", r["lander_domain"] or "",
                     r["last_checked"] or "", r["promote_flag"] or ""])
    return rows


def apply_promote_marks(conn: sqlite3.Connection, sheet_rows: list[list]) -> int:
    """Read the Candidates tab back: a Y in the promote column marks the domain for promotion on the next triage."""
    n = 0
    for row in sheet_rows:
        # full tab rows (domain first, promote last) or the compact [domain, promote] pairs from ?cols=domain,promote
        if not row or len(row) not in (2, len(CANDIDATES_HEADERS)):
            continue
        dom, flag = str(row[0]).strip().lower(), str(row[-1]).strip().upper()
        if dom and flag == "Y":
            cur = conn.execute("UPDATE radar_domains SET promote_flag = 'Y', last_checked = '' WHERE domain = ? AND status = 'candidate'", (dom,))
            n += 1 if cur.rowcount else 0
    conn.commit()
    return n


def summary_counts(conn: sqlite3.Connection) -> dict:
    c = {k: 0 for k in ("candidate", "promoted", "discarded", "watchlist", "funnel", "shopify")}
    for r in conn.execute("SELECT status, type, COUNT(*) n FROM radar_domains GROUP BY status, type"):
        c[r["status"]] = c.get(r["status"], 0) + r["n"]
        if r["type"] == "funnel" and r["status"] == "candidate":
            c["funnel"] += r["n"]
        if r["type"] == "shopify" and r["status"] == "candidate":
            c["shopify"] += r["n"]
    c["domains"] = sum(v for k, v in c.items() if k in ("candidate", "promoted", "discarded", "watchlist"))
    c["unsearched"] = conn.execute("SELECT COUNT(*) FROM radar_domains WHERE status = 'candidate' AND searched_at IS NULL").fetchone()[0]
    return c


def hook_yield(conn: sqlite3.Connection) -> list[dict]:
    """Per hook phrase: sweeps run, ads seen, landing domains, and how many of those domains were promoted /
    are Shopify candidates / are funnels / were discarded. Every phrase that returned an ad gets credit for it
    (radar_ad_hits), not only the first phrase that saw it. A phrase with sweeps >= 2 and 0 promoted is flagged."""
    out = []
    status = {r["domain"]: (r["status"], r["type"]) for r in conn.execute("SELECT domain, status, type FROM radar_domains")}
    for h in load_hooks():
        src = f"hook:{h}"
        sweeps = conn.execute("SELECT COUNT(*) FROM radar_runs WHERE kind = 'hook' AND query = ? AND note = ''", (h,)).fetchone()[0]
        ads = conn.execute("SELECT COUNT(DISTINCT ad_id) FROM radar_ad_hits WHERE source = ?", (src,)).fetchone()[0]
        doms = {r[0] for r in conn.execute("SELECT DISTINCT landing_domain FROM radar_ad_hits WHERE source = ? AND landing_domain IS NOT NULL", (src,))}
        promoted = sum(1 for d in doms if status.get(d, ("", ""))[0] == "promoted")
        cands = sum(1 for d in doms if status.get(d, ("", ""))[0] == "candidate" and status[d][1] == "shopify")
        funnels = sum(1 for d in doms if status.get(d, ("", ""))[0] == "candidate" and status[d][1] == "funnel")
        discarded = sum(1 for d in doms if status.get(d, ("", ""))[0] == "discarded")
        out.append({"hook": h, "sweeps": sweeps, "ads": ads, "domains": len(doms), "promoted": promoted, "candidates": cands,
                    "funnels": funnels, "discarded": discarded,
                    "verdict": "delete?" if sweeps >= 2 and promoted == 0 else ("add siblings" if promoted >= 2 else "")})
    out.sort(key=lambda r: (-r["promoted"], -r["candidates"], -r["domains"]))
    return out
