"""Scaling columns derived from the ads already in the DB (Part A).

  pages     distinct Facebook pages with active ads landing on the store (or resolved to a product),
            when each page's first ad for the store launched, page likes and their 7-day slope
  landing   distinct landing URL paths (query stripped) pointing at each product and how many appeared
            in the last 7 days
  alerts    13: store gained 2+ new pages in 7 days; 14: product gained 3+ new landing paths in 7 days
            (12, page-likes slope doubling, lives in ad_detail)
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from . import ad_metrics

RULES = {
    13: "store gained 2+ new advertising pages in 7 days",
    14: "product gained 3+ new landing paths in 7 days",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def landing_path(url: str | None) -> str | None:
    """Host + path, query and fragment stripped, trailing slash dropped, lower-cased."""
    if not url:
        return None
    u = urlparse(url)
    host = u.netloc.lower().replace("www.", "", 1)
    path = (u.path or "/").rstrip("/") or "/"
    return f"{host}{path}".lower()


def _store_ads(conn: sqlite3.Connection, store_id: int, store_domain: str, snap: str) -> list[dict]:
    """Every ad of the store that lands on the store's domain or resolved to a product, with today's
    active flag and page / launch fields."""
    rows = []
    for r in conn.execute(
        """SELECT a.ad_id, a.page_id, a.page_name, a.ad_start_date, a.first_seen_date, a.landing_url, a.landing_domain,
                  a.product_handle, a.page_ignored, a.last_delivered, a.ad_end_date, a.delivery_status, d.is_active
           FROM meta_ads a LEFT JOIN meta_ads_daily d ON d.ad_id = a.ad_id AND d.snapshot_date = ?
           WHERE a.store_id = ?""", (snap, store_id)):
        a = dict(r)
        if a["page_ignored"]:
            continue
        on_store = bool(a["product_handle"]) or ad_metrics.same_store(a["landing_domain"] or urlparse(a["landing_url"] or "").netloc, store_domain)
        if not on_store:
            continue
        a["page_key"] = a["page_id"] or a["page_name"] or ""
        a["launch"] = a["ad_start_date"] or a["first_seen_date"]
        a["active"] = 1 if (a["delivery_status"] == "on" or (a["delivery_status"] is None and a["is_active"])) else 0
        rows.append(a)
    return rows


def _snap(conn, store_id, today):
    return conn.execute("SELECT MAX(snapshot_date) FROM meta_ads_daily WHERE store_id = ? AND snapshot_date <= ?",
                        (store_id, today)).fetchone()[0]


def page_rows(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str) -> list[dict]:
    """One row per page advertising the store: first_seen (first ad launch), active_ads, last_delivered, likes, slope."""
    snap = _snap(conn, store_id, today)
    if not snap:
        return []
    by: dict[str, dict] = {}
    for a in _store_ads(conn, store_id, store_domain, snap):
        p = by.setdefault(a["page_key"], {"page_id": a["page_id"], "page_name": a["page_name"], "first_seen": None,
                                          "active_ads": 0, "ads": 0, "last_delivered": None})
        p["ads"] += 1
        p["active_ads"] += a["active"]
        if a["launch"] and (p["first_seen"] is None or a["launch"] < p["first_seen"]):
            p["first_seen"] = a["launch"]
        ld = a["last_delivered"] or a["ad_end_date"]
        if ld and (p["last_delivered"] is None or ld > p["last_delivered"]):
            p["last_delivered"] = ld
        if not p["page_name"] and a["page_name"]:
            p["page_name"] = a["page_name"]
    likes = {r["page_id"]: dict(r) for r in conn.execute(
        """SELECT page_id, page_like_count, likes_slope_7d, likes_slope_prev_7d, snapshot_date FROM meta_page_likes_daily
           WHERE store_id = ? AND snapshot_date = (SELECT MAX(snapshot_date) FROM meta_page_likes_daily l2 WHERE l2.page_id = meta_page_likes_daily.page_id)""",
        (store_id,))}
    out = []
    for key, p in by.items():
        lk = likes.get(key) or likes.get(p["page_name"] or "") or {}
        p.update({"page_likes": lk.get("page_like_count"), "page_likes_7d_slope": lk.get("likes_slope_7d"),
                  "page_likes_prev_7d_slope": lk.get("likes_slope_prev_7d"), "likes_as_of": lk.get("snapshot_date")})
        out.append(p)
    out.sort(key=lambda p: (-p["active_ads"], p["first_seen"] or ""))
    return out


def store_page_metrics(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str) -> dict:
    pages = page_rows(conn, store_id, store_domain, today)
    lo7 = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    active_pages = [p for p in pages if p["active_ads"] > 0]
    new = [p for p in pages if p["first_seen"] and p["first_seen"] > lo7]
    slopes = [p["page_likes_7d_slope"] for p in pages if p["page_likes_7d_slope"] is not None]
    return {"pages_per_domain": len(active_pages) if pages else None, "pages_new_7d": len(new) if pages else None,
            "page_likes_slope_max": max(slopes) if slopes else None, "new_pages": [p["page_name"] or p["page_id"] for p in new]}


def product_metrics(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str) -> dict[str, dict]:
    """{handle: {pages_pointing_here, pages_new_7d, landing_paths, landing_paths_new_7d, new_paths}}"""
    snap = _snap(conn, store_id, today)
    if not snap:
        return {}
    lo7 = (date.fromisoformat(snap) - timedelta(days=7)).isoformat()
    pages: dict[str, dict[str, dict]] = defaultdict(dict)     # handle -> page_key -> {active, first}
    paths: dict[str, dict[str, str | None]] = defaultdict(dict)   # handle -> path -> first launch
    for a in _store_ads(conn, store_id, store_domain, snap):
        h = a["product_handle"]
        if not h:
            continue
        pg = pages[h].setdefault(a["page_key"], {"active": 0, "first": None})
        pg["active"] += a["active"]
        if a["launch"] and (pg["first"] is None or a["launch"] < pg["first"]):
            pg["first"] = a["launch"]
        lp = landing_path(a["landing_url"])
        if lp:
            cur = paths[h].get(lp)
            if lp not in paths[h] or (a["launch"] and (cur is None or a["launch"] < cur)):
                paths[h][lp] = a["launch"]
    out = {}
    for h in set(pages) | set(paths):
        pg = pages.get(h, {})
        pt = paths.get(h, {})
        new_paths = sorted(p for p, first in pt.items() if first and first > lo7)
        out[h] = {"pages_pointing_here": sum(1 for v in pg.values() if v["active"] > 0),
                  "pages_new_7d": sum(1 for v in pg.values() if v["first"] and v["first"] > lo7),
                  "landing_paths": len(pt), "landing_paths_new_7d": len(new_paths), "new_paths": new_paths}
    return out


def run_alerts(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str) -> list[dict]:
    found = []
    sm = store_page_metrics(conn, store_id, store_domain, today)
    if (sm["pages_new_7d"] or 0) >= 2:
        found.append({"rule": 13, "handle": None, "key": "13|store",
                      "detail": f"{store_domain}: {sm['pages_new_7d']} new advertising page(s) in 7 days "
                                f"({', '.join(sm['new_pages'][:4])}); {sm['pages_per_domain']} pages active"})
    for h, m in product_metrics(conn, store_id, store_domain, today).items():
        if m["landing_paths_new_7d"] >= 3:
            found.append({"rule": 14, "handle": h, "key": f"14|{h}",
                          "detail": f"{h}: {m['landing_paths_new_7d']} new landing path(s) in 7 days of {m['landing_paths']} total "
                                    f"({', '.join(m['new_paths'][:3])})"})
    now = _utcnow()
    since = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
    written = []
    for f in found:
        if conn.execute("SELECT 1 FROM alerts WHERE snapshot_date >= ? AND store_id = ? AND dedupe_key = ?",
                        (since, store_id, f["key"])).fetchone():
            continue
        conn.execute("INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at, dedupe_key) VALUES (?,?,?,?,?,?,?)",
                     (today, store_id, f["handle"], f["rule"], f["detail"], now, f["key"]))
        written.append(f)
    conn.commit()
    return written


def pages_tab_rows(conn: sqlite3.Connection, stores: list, as_of: str | None = None) -> list[list]:
    """Pages tab: store, page name, page_id, first_seen, active_ads, last_delivered, page_likes, page_likes_7d_slope, ads_as_of."""
    rows = []
    for s in stores:
        snap = _snap(conn, s["id"], as_of or "9999")
        if not snap:
            continue
        for p in page_rows(conn, s["id"], s["store_domain"], snap):
            rows.append([s["store_domain"], p["page_name"] or "", p["page_id"] or "", p["first_seen"] or "", p["active_ads"],
                         p["last_delivered"] or "", "" if p["page_likes"] is None else p["page_likes"],
                         "" if p["page_likes_7d_slope"] is None else p["page_likes_7d_slope"], snap])
    rows.sort(key=lambda r: (r[3] and -int(r[3][:10].replace("-", "")) or 0, -r[4]))   # newest pages first
    return rows
