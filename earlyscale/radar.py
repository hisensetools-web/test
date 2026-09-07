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

def search_ads(browser, query: str, max_ads: int | None = None, search_type: str = "keyword_unordered") -> list[dict]:
    url = meta_ads.build_search_url(query=query, country=config.RADAR_COUNTRY, search_type=search_type)
    if hasattr(browser, "get"):          # meta_ads.BrowserHandle: relaunches a dead Chromium
        browser = browser.get()
    res = meta_ads.scrape_page(url, max_ads=max_ads or config.RADAR_MAX_ADS_PER_QUERY, browser=browser)
    if res.blocked:
        raise meta_ads.MetaBlocked(res.note)
    return res.ads


def record_ads(conn: sqlite3.Connection, ads: list[dict], query: str, source: str, today: str) -> tuple[int, set[str]]:
    domains: set[str] = set()
    for a in ads:
        dom = normalise_landing_domain(a.get("landing_url") or a.get("landing_domain"))
        body = a.get("primary_text") or ""
        conn.execute("""INSERT INTO radar_ads (ad_id, query, source, page_id, page_name, landing_url, landing_domain, body_len, body_snippet,
                          start_date, first_seen, last_seen, is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(ad_id) DO UPDATE SET last_seen = excluded.last_seen, is_active = excluded.is_active,
                          landing_domain = COALESCE(excluded.landing_domain, landing_domain), page_name = COALESCE(excluded.page_name, page_name)""",
                     (a["ad_id"], query, source, a.get("page_id"), a.get("page_name"), a.get("landing_url"), dom, len(body), body[:200],
                      a.get("start_date"), today, today, 1 if a.get("is_active", True) else 0))
        if dom:
            domains.add(dom)
    conn.commit()
    return len(ads), domains


def _run(conn, kind, query, started, ads=None, domains=None, note=""):
    conn.execute("INSERT INTO radar_runs (kind, query, started_at, ended_at, ads_found, domains_found, note) VALUES (?,?,?,?,?,?,?)",
                 (kind, query, started, _utcnow(), ads, domains, note[:300]))
    conn.commit()


def sweep(conn: sqlite3.Connection, browser, queries: list[tuple[str, str]], today: str, search=search_ads, wait=None,
          deadline: float | None = None) -> dict:
    """queries: [(query, source)]. Returns {queries, ads, domains, deferred}."""
    wait = wait or meta_ads._wait
    total_ads, all_domains = 0, set()
    deferred = 0
    for i, (q, source) in enumerate(queries):
        if deadline is not None and time.monotonic() > deadline:
            deferred = len(queries) - i
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
        n, doms = record_ads(conn, ads, q, source, today)
        _run(conn, source.split(":")[0], q, started, n, len(doms))
        log.info("radar: %-40s ads=%-4d domains=%d", q[:40], n, len(doms))
        total_ads += n
        all_domains |= doms
        if i < len(queries) - 1:
            wait()
    return {"queries": len(queries) - deferred, "ads": total_ads, "domains": all_domains, "deferred": deferred}


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


def new_domains(conn: sqlite3.Connection, watchlist_path: Path | None = None) -> list[tuple[str, str]]:
    """[(domain, source)] seen by radar ads but neither on the watchlist nor triaged before."""
    wl, seen = known_domains(conn, watchlist_path)
    out = []
    for r in conn.execute("""SELECT landing_domain, MIN(source) AS src, COUNT(*) AS n FROM radar_ads WHERE landing_domain IS NOT NULL
                             GROUP BY landing_domain ORDER BY n DESC"""):
        d = r["landing_domain"]
        if d and d not in wl and d not in seen:
            out.append((d, r["src"]))
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


def follow_lander(conn: sqlite3.Connection, domain: str, session=None, fetch=ad_metrics.fetch_landing, check=is_shopify) -> tuple[str | None, list[dict]]:
    """For a non-Shopify landing domain: open up to 3 of its ad landing pages, collect the shop domains they
    link to, and return the first that is a Shopify storefront (with its products)."""
    session = session or shopify.make_session()
    urls = [r[0] for r in conn.execute("SELECT DISTINCT landing_url FROM radar_ads WHERE landing_domain = ? AND landing_url IS NOT NULL LIMIT 3", (domain,))]
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


def decide(age_days: int | None, active_ads: int, hot_new_product: str | None) -> bool:
    young = age_days is not None and age_days <= config.RADAR_MAX_AGE_DAYS
    return (young or bool(hot_new_product)) and active_ads >= config.RADAR_MIN_ACTIVE_ADS


def triage_domain(conn: sqlite3.Connection, browser, domain: str, source: str, today: str, session=None,
                  search=search_ads, check=is_shopify, follow=None, identity=store_age.fetch_identity,
                  watchlist_path: Path | None = None) -> dict:
    """Classify one domain and write its radar_domains row. Returns the row as a dict (+ 'promoted')."""
    session = session or shopify.make_session()
    follow = follow or (lambda c, d, s: follow_lander(c, d, s, check=check))
    row = conn.execute("SELECT * FROM radar_domains WHERE domain = ?", (domain,)).fetchone()
    rec = dict(row) if row else {"domain": domain, "status": "candidate", "source": source, "first_seen": today}
    rec["last_checked"] = today
    store_domain, products = domain, []
    ok, products = check(domain, session)
    if not ok:
        shop, products = follow(conn, domain, session)
        if shop:
            rec["lander_domain"], store_domain = domain, shop
            rec["type"] = "shopify"
            log.info("radar: %s is a lander; its checkout is %s", domain, shop)
        else:
            rec["type"] = "funnel"
    else:
        rec["type"] = "shopify"
    # ads pointing at the domain (a fresh search so counts are current), then the lander's own ads
    try:
        ads = search(browser, store_domain, max_ads=200)
        record_ads(conn, ads, store_domain, f"domain:{store_domain}", today)
    except Exception as e:  # noqa: BLE001
        log.warning("radar: ad search for %s failed: %s", store_domain, e)
    stats = domain_ads(conn, store_domain, products, today)
    if rec.get("lander_domain"):
        lander_stats = domain_ads(conn, domain, [], today)
        stats["active_ads"] += lander_stats["active_ads"]
        stats["pages"] = max(stats["pages"], lander_stats["pages"])
        stats["top_page"] = stats["top_page"] or lander_stats["top_page"]
        stats["example_text"] = stats["example_text"] or lander_stats["example_text"]
    rec.update({k: stats[k] for k in ("active_ads", "pages", "top_page", "example_text", "hot_new_product")})
    if rec["type"] == "shopify":
        rec["products"] = len(products)
        firsts = sorted(p["created_at"][:10] for p in products if p.get("created_at"))
        rec["store_first_created"] = firsts[0] if firsts else None
        try:
            ident = identity(store_domain, session)
            rec["shop_id"] = ident.get("shop_id")
        except Exception:  # noqa: BLE001
            ident = {}
        cal = store_age.build_calibration(conn)
        est = store_age.estimate_created(rec.get("shop_id"), cal)["created"] if rec.get("shop_id") else None
        created = est.isoformat() if est else rec["store_first_created"]
        rec["store_created_est"] = created
        rec["store_age_days"] = store_age.age_days(created, today) if created else None
        promote = decide(rec["store_age_days"], rec["active_ads"], rec["hot_new_product"]) or (rec.get("promote_flag") or "").upper() == "Y"
    else:
        rec["products"] = 0
        promote = False
    if promote and rec["status"] != "promoted":
        note = f"radar: {rec['source']} {today}" + (f" via {domain}" if rec.get("lander_domain") else "")
        append_to_watchlist({"store_domain": store_domain, "meta_page_name": rec.get("top_page") or "", "notes": note}, watchlist_path)
        rec["status"], rec["promoted_at"] = "promoted", today
        log.info("radar: PROMOTED %s (age %s d, %s active ads, hot=%s) -> watchlist", store_domain, rec.get("store_age_days"),
                 rec["active_ads"], rec.get("hot_new_product"))
    elif rec["status"] not in ("promoted", "watchlist"):
        rec["status"] = "candidate"
    rec["domain"] = domain
    cols = ("domain", "type", "status", "source", "first_seen", "last_checked", "lander_domain", "shop_id", "store_created_est",
            "store_first_created", "store_age_days", "products", "active_ads", "pages", "top_page", "hot_new_product", "example_text",
            "promote_flag", "promoted_at", "note")
    conn.execute(f"INSERT OR REPLACE INTO radar_domains ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(rec.get(c) for c in cols))
    conn.commit()
    rec["promoted"] = promote
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
              watchlist_path: Path | None = None, wait=None, max_minutes: float | None = None) -> dict:
    """imports -> (weekly) hook + copycat sweeps -> triage new domains -> re-triage candidates."""
    session = session or shopify.make_session()
    wait = wait or meta_ads._wait
    if do_sweep is None:
        do_sweep = date.fromisoformat(today).weekday() == config.RADAR_SWEEP_WEEKDAY
    deadline = time.monotonic() + (max_minutes if max_minutes is not None else config.RADAR_MAX_MINUTES) * 60
    out = {"imports": import_folder(conn, today, watchlist_path=watchlist_path), "sweep": None, "copycat": None, "web": None,
           "found": 0, "discarded": 0, "promoted": 0, "parked": 0, "funnels": 0, "retriaged": 0, "promoted_later": 0, "deferred_triage": 0}
    if do_sweep:
        hooks = load_hooks()
        out["sweep"] = sweep(conn, browser, [(h, f"hook:{h}") for h in hooks], today, search=search, wait=wait, deadline=deadline)
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
        out["copycat"] = sweep(conn, browser, qs, today, search=search, wait=wait, deadline=deadline)
        if config.RADAR_WEB_SEARCH and web_search is not None:
            web_domains = set()
            for h in heroes[: config.RADAR_MAX_WEB_QUERIES]:
                for d in web_search(h["title"] or h["query"], session):
                    web_domains.add((d, f"web:{h['store']}/{h['handle']}"))
                time.sleep(1.0)
            for d, src in web_domains:
                conn.execute("""INSERT OR IGNORE INTO radar_ads (ad_id, query, source, landing_domain, first_seen, last_seen, is_active)
                                VALUES (?,?,?,?,?,?,0)""", (f"web:{d}", src, src, d, today, today))
            conn.commit()
            out["web"] = {"queries": min(len(heroes), config.RADAR_MAX_WEB_QUERIES), "domains": len(web_domains)}
    # triage new domains
    todo = new_domains(conn, watchlist_path)[: config.RADAR_MAX_TRIAGE]
    out["found"] = len(todo)
    for i, (dom, src) in enumerate(todo):
        if time.monotonic() > deadline:
            out["deferred_triage"] = len(todo) - i
            log.warning("radar: budget spent; %d new domain(s) wait for the next run", out["deferred_triage"])
            break
        rec = triage_domain(conn, browser, dom, src, today, session, search=search, check=check, identity=identity, watchlist_path=watchlist_path)
        if rec["type"] == "funnel":
            out["funnels"] += 1
            out["parked"] += 1
        elif rec["promoted"]:
            out["promoted"] += 1
        else:
            out["parked"] += 1
        if i < len(todo) - 1:
            wait()
    # re-triage existing candidates daily (thresholds may be crossed, or promote=Y set on the sheet)
    cands = [dict(r) for r in conn.execute("SELECT * FROM radar_domains WHERE status = 'candidate' AND last_checked < ?", (today,))]
    for i, c in enumerate(cands):
        if time.monotonic() > deadline:
            break
        rec = triage_domain(conn, browser, c["domain"], c["source"], today, session, search=search, check=check, identity=identity, watchlist_path=watchlist_path)
        out["retriaged"] += 1
        if rec["promoted"]:
            out["promoted_later"] += 1
        if i < len(cands) - 1:
            wait()
    return out


CANDIDATES_HEADERS = ["domain", "type", "status", "first_seen", "store_age_days", "store_created_est", "store_first_created", "products",
                      "active_ads", "pages", "top page", "example ad text", "hot new product", "source", "lander_domain", "last_checked",
                      "promote"]


def candidates_rows(conn: sqlite3.Connection) -> list[list]:
    rows = []
    for r in conn.execute("""SELECT * FROM radar_domains WHERE status IN ('candidate', 'promoted') ORDER BY
                             CASE status WHEN 'candidate' THEN 0 ELSE 1 END, active_ads DESC, first_seen DESC"""):
        rows.append([r["domain"], r["type"] or "", r["status"], r["first_seen"], "" if r["store_age_days"] is None else r["store_age_days"],
                     r["store_created_est"] or "", r["store_first_created"] or "", "" if r["products"] is None else r["products"],
                     "" if r["active_ads"] is None else r["active_ads"], "" if r["pages"] is None else r["pages"], r["top_page"] or "",
                     (r["example_text"] or "")[:200], r["hot_new_product"] or "", r["source"] or "", r["lander_domain"] or "",
                     r["last_checked"] or "", r["promote_flag"] or ""])
    return rows


def apply_promote_marks(conn: sqlite3.Connection, sheet_rows: list[list]) -> int:
    """Read the Candidates tab back: a Y in the promote column marks the domain for promotion on the next triage."""
    n = 0
    for row in sheet_rows:
        if not row or len(row) < len(CANDIDATES_HEADERS):
            continue
        dom, flag = str(row[0]).strip().lower(), str(row[-1]).strip().upper()
        if dom and flag == "Y":
            cur = conn.execute("UPDATE radar_domains SET promote_flag = 'Y', last_checked = '' WHERE domain = ? AND status = 'candidate'", (dom,))
            n += 1 if cur.rowcount else 0
    conn.commit()
    return n


def summary_counts(conn: sqlite3.Connection) -> dict:
    c = {k: 0 for k in ("candidate", "promoted", "discarded", "watchlist", "funnel")}
    for r in conn.execute("SELECT status, type, COUNT(*) n FROM radar_domains GROUP BY status, type"):
        c[r["status"]] = c.get(r["status"], 0) + r["n"]
        if r["type"] == "funnel":
            c["funnel"] += r["n"]
    c["domains"] = sum(v for k, v in c.items() if k in ("candidate", "promoted", "discarded", "watchlist"))
    return c


def hook_yield(conn: sqlite3.Connection) -> list[dict]:
    """Per hook phrase: sweeps run, ads seen, landing domains, and how many of those domains were promoted /
    are candidates / are funnels. A phrase with sweeps >= 2 and 0 promoted is flagged for deletion."""
    out = []
    status = {r["domain"]: (r["status"], r["type"]) for r in conn.execute("SELECT domain, status, type FROM radar_domains")}
    for h in load_hooks():
        src = f"hook:{h}"
        sweeps = conn.execute("SELECT COUNT(*) FROM radar_runs WHERE kind = 'hook' AND query = ? AND note = ''", (h,)).fetchone()[0]
        ads = conn.execute("SELECT COUNT(*) FROM radar_ads WHERE source = ?", (src,)).fetchone()[0]
        doms = {r[0] for r in conn.execute("SELECT DISTINCT landing_domain FROM radar_ads WHERE source = ? AND landing_domain IS NOT NULL", (src,))}
        promoted = sum(1 for d in doms if status.get(d, ("", ""))[0] == "promoted")
        cands = sum(1 for d in doms if status.get(d, ("", ""))[0] == "candidate" and status[d][1] != "funnel")
        funnels = sum(1 for d in doms if status.get(d, ("", ""))[1] == "funnel")
        out.append({"hook": h, "sweeps": sweeps, "ads": ads, "domains": len(doms), "promoted": promoted, "candidates": cands,
                    "funnels": funnels, "verdict": "delete?" if sweeps >= 2 and promoted == 0 else ("add siblings" if promoted >= 2 else "")})
    out.sort(key=lambda r: (-r["promoted"], -r["candidates"], -r["domains"]))
    return out
