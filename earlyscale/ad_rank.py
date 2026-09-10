"""Impression rank: where each ad sits when the Ad Library search is sorted "Impressions: high to low".

  rank_url                 the store's search (page id, page name or domain) with the impressions sort
  compare_orders           is the sorted order different from the default (newest first) order? If the first
                           N ids come back in the same sequence the sort is not informative for that store.
  record_ranks             meta_ads_daily.impression_rank = 1-based position in the sorted result, per day;
                           one rank_checks row per store per day with the comparison
  ad_rank_metrics          per ad: rank, rank 7 days ago, delta (negative = climbing), top5_days;
                           per product: best_rank, best_rank_7d_ago, best_rank_delta_7d, ads_in_top5, top5 ads
  run_alerts               15: ad entered the top 5 for a product created < 30 days ago
                           16: ad climbed 5+ ranks in 7 days
                           17: product ads_delivering doubled week over week
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import config, meta_ads

log = logging.getLogger("earlyscale.ad_rank")

RULES = {
    15: "ad entered the top 5 by impressions for a product created < 30 days ago",
    16: "ad climbed 5+ impression ranks in 7 days",
    17: "product's delivering ads doubled week over week",
}
TOP_N = 5


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def rank_url(store: dict, country: str = "ALL") -> tuple[str, str]:
    """(url, query) for the store's Ad Library search sorted by impressions, for one country view."""
    domain = store["store_domain"]
    query = meta_ads.store_query(store)
    url = meta_ads.build_search_url(query=None if store.get("meta_page_id") else query, page_id=store.get("meta_page_id") or None,
                                    sort="impressions", country=country)
    return url, query


def compare_orders(default_ids: list[str], sorted_ids: list[str], n: int | None = None) -> dict:
    """Compare the first n ids of both orders. identical = same ids in the same sequence; informative = enough
    ids to judge (META_RANK_MIN_COMPARE) and not identical."""
    n = n or config.META_RANK_MIN_COMPARE
    m = min(len(default_ids), len(sorted_ids), n)
    a, b = list(default_ids[:m]), list(sorted_ids[:m])
    identical = m > 0 and a == b
    overlap = len(set(a) & set(b))
    same_pos = sum(1 for x, y in zip(a, b) if x == y)
    return {"n": m, "identical": identical, "overlap": overlap, "same_position": same_pos,
            "informative": (m >= min(n, 5)) and not identical and same_pos < m}


def record_ranks(conn: sqlite3.Connection, store_id: int, today: str, sorted_ads: list[dict], query: str,
                 default_ids: list[str] | None = None) -> dict:
    """Write today's impression_rank for the ads in the sorted result (ads unknown so far are recorded as a scrape
    first, so an old high-impression ad missing from the newest-first pass still gets a row). Returns the comparison."""
    if sorted_ads:
        meta_ads.record_scrape(conn, store_id, today, sorted_ads, f"rank:{query}")
    ids = [a["ad_id"] for a in sorted_ads]
    cmp = compare_orders(default_ids or [], ids) if default_ids is not None else {"n": 0, "identical": False, "overlap": 0,
                                                                                    "same_position": 0, "informative": None}
    with conn:
        conn.execute("UPDATE meta_ads_daily SET impression_rank = NULL WHERE store_id = ? AND snapshot_date = ?", (store_id, today))
        if cmp["informative"] is not False:   # a non-informative order is just newest-first: storing it would rank the newest ads on top
            for i, aid in enumerate(ids, start=1):
                conn.execute("UPDATE meta_ads_daily SET impression_rank = ? WHERE ad_id = ? AND snapshot_date = ?", (i, aid, today))
        conn.execute("""INSERT OR REPLACE INTO rank_checks (snapshot_date, store_id, query, n_default, n_sorted, n_compared, overlap,
                        same_position, identical, informative, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (today, store_id, query, len(default_ids or []), len(ids), cmp["n"], cmp["overlap"], cmp["same_position"],
                      1 if cmp["identical"] else 0, None if cmp["informative"] is None else (1 if cmp["informative"] else 0), _utcnow()))
        if cmp["informative"] is not None:
            conn.execute("UPDATE stores SET sort_informative = ?, rank_checked_at = ? WHERE id = ?",
                         (1 if cmp["informative"] else 0, today, store_id))
    return {"ranked": len(ids), **cmp}


def _last_informative(conn: sqlite3.Connection, store_id: int, today: str, within_days: int = 7) -> tuple[str | None, bool]:
    """(country, fresh): the view last found informative for this store by a real comparison, and whether that
    comparison is recent enough (within_days) to reuse without scraping the baseline again."""
    # only a day on which the view was actually compared with its own newest-first list counts ('-> informative' in the
    # note); a day that merely reused an earlier verdict ('confirmed earlier') does not, nor do rows from before the
    # per-view comparison existed (no note). So a real comparison happens at least weekly.
    r = conn.execute("SELECT country, snapshot_date FROM rank_checks WHERE store_id = ? AND informative = 1 AND country IS NOT NULL "
                     "AND note LIKE '%-> informative%' ORDER BY snapshot_date DESC LIMIT 1", (store_id,)).fetchone()
    if not r:
        return None, False
    try:
        return r["country"], (date.fromisoformat(today) - date.fromisoformat(r["snapshot_date"])).days <= within_days
    except ValueError:
        return r["country"], False


def _confirmed_country(conn: sqlite3.Connection, store_id: int, today: str, within_days: int = 7) -> str | None:
    country, fresh = _last_informative(conn, store_id, today, within_days)
    return country if fresh else None


def _newest_ids(store: dict, country: str, browser, max_scrolls: int | None) -> list[str]:
    """Newest-first order of the same country view: the only valid baseline for that view's impressions sort."""
    query = meta_ads.store_query(store)
    url = meta_ads.build_search_url(query=None if store.get("meta_page_id") else query, page_id=store.get("meta_page_id") or None,
                                    country=country)
    res = meta_ads.scrape_page(url, max_scrolls=max_scrolls or config.META_RANK_SCROLLS, browser=browser)
    if res.blocked:
        raise meta_ads.MetaBlocked(res.note)
    return [a["ad_id"] for a in res.ads]


def scrape_ranks(conn: sqlite3.Connection, browser, store: dict, store_id: int, today: str, default_ids: list[str],
                 max_scrolls: int | None = None, countries: list[str] | None = None) -> dict:
    """Run the impressions-sorted search for one store and record ranks + the comparison with the newest-first order
    OF THE SAME VIEW. Meta ignores the sort in the worldwide view for most commercial ads (impressions are only
    published for EU delivery), so the views in META_RANK_COUNTRIES are tried in turn: `default_ids` (the day's
    worldwide newest-first list) is the baseline for ALL; an EU view gets its own newest-first scrape as baseline
    (skipped when that view was confirmed informative within the last 7 days). A view with 0 ads is skipped, not
    treated as the answer. The first informative view is kept; rank_checks.note records what every view returned."""
    countries = list(countries or config.META_RANK_COUNTRIES)
    preferred, fresh = _last_informative(conn, store_id, today)
    confirmed = preferred if fresh else None
    if preferred in countries:          # the view that worked last time goes first (with a fresh baseline when the verdict is stale)
        countries.remove(preferred)
        countries.insert(0, preferred)
    query = meta_ads.store_query(store)
    notes: list[str] = []
    last: dict | None = None
    first = True
    for country in countries:
        if not first:
            meta_ads._wait()
        first = False
        url, query = rank_url(store, country)
        res = meta_ads.scrape_page(url, max_scrolls=max_scrolls or config.META_RANK_SCROLLS, browser=browser)
        if res.blocked:
            raise meta_ads.MetaBlocked(res.note)
        if not res.ads:
            notes.append(f"{country}: 0 ads")
            continue
        if country == "ALL":
            baseline: list[str] | None = list(default_ids)
        elif country == confirmed:
            baseline = None          # confirmed informative this week: rank without re-scraping the baseline
        else:
            meta_ads._wait()
            baseline = _newest_ids(store, country, browser, max_scrolls)
        out = record_ranks(conn, store_id, today, res.ads, query, baseline)
        if baseline is None:
            out["informative"] = True
            with conn:
                conn.execute("UPDATE rank_checks SET informative = 1 WHERE store_id = ? AND snapshot_date = ?", (store_id, today))
                conn.execute("UPDATE stores SET sort_informative = 1, rank_checked_at = ? WHERE id = ?", (today, store_id))
            notes.append(f"{country}: {len(res.ads)} ads, informative (confirmed earlier this week)")
        else:
            notes.append(f"{country}: {len(res.ads)} ads, {out['same_position']}/{out['n']} in the same position as that view's newest-first"
                         f" -> {'informative' if out['informative'] else 'same order'}")
        out["note"], out["country"] = res.note, country
        last = out
        if out["informative"]:
            break
    if last is None:
        # no view returned an ad: leave sort_informative untouched, record the attempt
        record_ranks(conn, store_id, today, [], query, None)
        last = {"ranked": 0, "n": 0, "identical": False, "overlap": 0, "same_position": 0, "informative": None, "note": "", "country": None}
    summary = "; ".join(notes) or "no view returned an ad"
    with conn:
        conn.execute("UPDATE rank_checks SET country = ?, note = ? WHERE store_id = ? AND snapshot_date = ?",
                     (last["country"], summary, store_id, today))
    last["summary"] = (f"{last['ranked']} ads ranked from the {last['country']} view; " if last["country"] else "") + summary
    return last


def sort_informative(conn: sqlite3.Connection, store_id: int) -> int | None:
    r = conn.execute("SELECT sort_informative FROM stores WHERE id = ?", (store_id,)).fetchone()
    return None if r is None else r["sort_informative"]


# ---------------------------------------------------------------- derived

def _snap(conn, store_id, day):
    return conn.execute("SELECT MAX(snapshot_date) FROM meta_ads_daily WHERE store_id = ? AND snapshot_date <= ? AND impression_rank IS NOT NULL",
                        (store_id, day)).fetchone()[0]


def ad_rank_metrics(conn: sqlite3.Connection, store_id: int, today: str) -> dict:
    """{'ads': {ad_id: {...}}, 'products': {handle: {...}}, 'as_of': day, 'prev_as_of': day7}."""
    snap = _snap(conn, store_id, today)
    if not snap:
        return {"ads": {}, "products": {}, "as_of": None, "prev_as_of": None}
    prev = _snap(conn, store_id, (date.fromisoformat(snap) - timedelta(days=7)).isoformat())
    if prev == snap:
        prev = None
    ranks_prev = {r["ad_id"]: r["impression_rank"] for r in conn.execute(
        "SELECT ad_id, impression_rank FROM meta_ads_daily WHERE store_id = ? AND snapshot_date = ? AND impression_rank IS NOT NULL",
        (store_id, prev))} if prev else {}
    top5_days = {r["ad_id"]: r["n"] for r in conn.execute(
        "SELECT ad_id, COUNT(*) n FROM meta_ads_daily WHERE store_id = ? AND impression_rank <= ? AND snapshot_date <= ? GROUP BY ad_id",
        (store_id, TOP_N, snap))}
    ads: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT d.ad_id, d.impression_rank, a.product_handle, a.ad_start_date, a.first_seen_date, a.page_name, d.low_impressions
           FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
           WHERE d.store_id = ? AND d.snapshot_date = ? AND d.impression_rank IS NOT NULL AND COALESCE(a.page_ignored, 0) = 0
           ORDER BY d.impression_rank""", (store_id, snap)):
        rk = r["impression_rank"]
        p7 = ranks_prev.get(r["ad_id"])
        launch = r["ad_start_date"] or r["first_seen_date"]
        try:
            days = (date.fromisoformat(snap) - date.fromisoformat(launch[:10])).days if launch else None
        except ValueError:
            days = None
        ads[r["ad_id"]] = {"rank": rk, "rank_7d_ago": p7, "rank_delta_7d": (rk - p7) if p7 is not None else None,
                           "started": launch[:10] if launch else None,
                           "top5_days": top5_days.get(r["ad_id"], 0), "product_handle": r["product_handle"],
                           "days_running": days, "page_name": r["page_name"], "low_impressions": r["low_impressions"],
                           "entered_top5": rk <= TOP_N and (p7 is None or p7 > TOP_N)}
    products: dict[str, dict] = {}
    by_h: dict[str, list] = defaultdict(list)
    for aid, m in ads.items():
        if m["product_handle"]:
            by_h[m["product_handle"]].append((aid, m))
    prev_best: dict[str, int] = {}
    if prev:
        for r in conn.execute("""SELECT a.product_handle h, MIN(d.impression_rank) b FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
                                 WHERE d.store_id = ? AND d.snapshot_date = ? AND d.impression_rank IS NOT NULL AND a.product_handle IS NOT NULL
                                 GROUP BY a.product_handle""", (store_id, prev)):
            prev_best[r["h"]] = r["b"]
    for h, members in by_h.items():
        members.sort(key=lambda x: x[1]["rank"])
        best = members[0][1]["rank"]
        pb = prev_best.get(h)
        products[h] = {"best_rank": best, "best_rank_7d_ago": pb, "best_rank_delta_7d": (best - pb) if pb is not None else None,
                       "ads_in_top5": sum(1 for _, m in members if m["rank"] <= TOP_N),
                       "top5_ads": [dict(m, ad_id=aid) for aid, m in members if m["rank"] <= TOP_N],
                       "ads_ranked": len(members)}
    return {"ads": ads, "products": products, "as_of": snap, "prev_as_of": prev}


def run_alerts(conn: sqlite3.Connection, store_id: int, store_domain: str, today: str) -> list[dict]:
    from . import ad_metrics
    found = []
    m = ad_rank_metrics(conn, store_id, today)
    created = {r["handle"]: r["created_at"] for r in conn.execute(
        """SELECT handle, created_at FROM products_daily WHERE store_id = ? AND snapshot_date =
           (SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ?)""", (store_id, store_id))}
    t = date.fromisoformat(today)
    for aid, a in m["ads"].items():
        h = a["product_handle"]
        if a["entered_top5"] and h and created.get(h):
            try:
                age = (t - date.fromisoformat(created[h][:10])).days
            except ValueError:
                age = None
            if age is not None and age < 30:
                found.append({"rule": 15, "handle": h, "key": f"15|{aid}",
                              "detail": f"{h}: ad {aid} entered the top {TOP_N} by impressions (rank {a['rank']}) on a product created {age}d ago"})
        if a["rank_delta_7d"] is not None and a["rank_delta_7d"] <= -5:
            found.append({"rule": 16, "handle": h, "key": f"16|{aid}|{m['as_of']}",
                          "detail": f"{h or a['page_name'] or store_domain}: ad {aid} climbed from rank {a['rank_7d_ago']} to {a['rank']} in 7 days"})
    dm = ad_metrics.delivering_metrics(conn, store_id, today)
    for h, d in dm.items():
        if h == "__store__":
            continue
        prev = d["ads_delivering_7d_ago"]
        if isinstance(prev, int) and prev >= 2 and d["ads_delivering"] >= 2 * prev:
            found.append({"rule": 17, "handle": h, "key": f"17|{h}",
                          "detail": f"{h}: {d['ads_delivering']} delivering ads vs {prev} a week ago (x{d['ads_delivering'] / prev:.1f})"})
    now = _utcnow()
    since = (t - timedelta(days=6)).isoformat()
    written = []
    for f in found:
        if conn.execute("SELECT 1 FROM alerts WHERE snapshot_date >= ? AND store_id = ? AND dedupe_key = ?", (since, store_id, f["key"])).fetchone():
            continue
        conn.execute("INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at, dedupe_key) VALUES (?,?,?,?,?,?,?)",
                     (today, store_id, f["handle"], f["rule"], f["detail"], now, f["key"]))
        written.append(f)
    conn.commit()
    return written
