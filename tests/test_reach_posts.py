"""Reach curve (EU / UK / range), boosted-post resolution, comment deltas, rule 8, coverage."""
import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from earlyscale import ad_metrics, config, db, meta_ads, sheets

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"
FIX = Path(__file__).parent / "fixtures"


def nodes():
    return meta_ads.extract_ads(json.loads((FIX / "ad_library_graphql.json").read_text()))


class ReachExtractionTests(unittest.TestCase):
    def test_eu_exact_and_gb_breakdown(self):
        r = meta_ads.extract_reach(nodes()[0])
        self.assertEqual(r["eu_reach"], 12345)
        self.assertEqual(r["uk_reach"], 1500 + 2000 + 20 + 700 + 780)
        self.assertIsNone(r["range_lower"])
        self.assertEqual(r["source"], "eu_total_reach")
        self.assertIn("aaa_info.eu_total_reach=12345", r["keys"])
        self.assertIn("GB=5000", r["keys"])

    def test_range_only(self):
        r = meta_ads.extract_reach(nodes()[1])
        self.assertIsNone(r["eu_reach"])
        self.assertIsNone(r["uk_reach"])
        self.assertEqual((r["range_lower"], r["range_upper"]), (10000, 15000))
        self.assertEqual(r["source"], "reach_estimate")

    def test_unknown_uk_key_shapes_are_picked_up(self):
        n = {"ad_archive_id": "x", "snapshot": {}, "uk_total_reach": 4200}
        self.assertEqual(meta_ads.extract_reach(n)["uk_reach"], 4200)
        n = {"ad_archive_id": "x", "snapshot": {}, "transparency": {"gb_reach": {"lower_bound": 1000, "upper_bound": 5000}}}
        r = meta_ads.extract_reach(n)
        self.assertEqual((r["range_lower"], r["range_upper"]), (1000, 5000))
        self.assertEqual(r["source"], "transparency.gb_reach")
        self.assertEqual(meta_ads.extract_reach({"ad_archive_id": "x", "snapshot": {}})["source"], None)

    def test_normalise_ad_carries_reach_and_post(self):
        ads = [meta_ads.normalise_ad(n) for n in nodes()]
        self.assertEqual(ads[0]["uk_reach"], 5000)
        self.assertIsNone(ads[0]["post_url"])
        self.assertEqual(ads[1]["post_url"], "https://www.facebook.com/55501/posts/987654321012345")
        self.assertEqual(ads[1]["reach_range_lower"], 10000)


class PostUrlTests(unittest.TestCase):
    def test_post_url_forms(self):
        self.assertEqual(meta_ads.extract_post_url({"page_id": "9", "snapshot": {"post_id": "123456"}}),
                         "https://www.facebook.com/9/posts/123456")
        self.assertEqual(meta_ads.extract_post_url({"snapshot": {"link_url": "https://www.facebook.com/brand/videos/55/"}}),
                         "https://www.facebook.com/brand/videos/55/")
        self.assertEqual(meta_ads.extract_post_url({"snapshot": {"caption": "https://www.instagram.com/p/AbC123/"}}),
                         "https://www.instagram.com/p/AbC123/")
        self.assertIsNone(meta_ads.extract_post_url({"snapshot": {"link_url": "https://store.com/products/x"}}))
        self.assertIsNone(meta_ads.extract_post_url({"snapshot": {}, "ad_snapshot_url": "https://www.facebook.com/ads/library/?id=1"}))

    def test_parse_post_counts(self):
        html = '... "comment_count":{"total_count":57} ... "reaction_count":{"count":1203} ... "share_count":{"count":9} ...'
        self.assertEqual(meta_ads.parse_post_counts(html), {"comments": 57, "reactions": 1203, "shares": 9})
        self.assertEqual(meta_ads.parse_post_counts("1.2K comments and 34 shares"), {"comments": 1200, "shares": 34})
        self.assertEqual(meta_ads.parse_post_counts("<html>login</html>"), {})


class SlopeTests(unittest.TestCase):
    def test_slope(self):
        self.assertEqual(ad_metrics.slope([("2026-09-01", 1000), ("2026-09-08", 8000)]), 1000.0)
        self.assertEqual(ad_metrics.slope([("2026-09-08", 8000), ("2026-09-01", 1000), ("2026-09-04", None)]), 1000.0)
        self.assertIsNone(ad_metrics.slope([("2026-09-01", 1000)]))
        self.assertIsNone(ad_metrics.slope([("2026-09-01", 1000), ("2026-09-01", 2000)]))


def _ad(ad_id, reach=None, uk=None, comments=None, post=None):
    return {"ad_id": ad_id, "page_id": "p", "page_name": "Brand", "start_date": "2026-08-01", "end_date": None,
            "is_active": 1, "primary_text": f"copy {ad_id}", "headline": "H", "landing_url": "https://supp.com/products/hero",
            "landing_domain": "supp.com", "caption": None, "cta": None, "creative_type": "image", "asset_url": None,
            "platforms": None, "collation_count": 1, "eu_total_reach": reach, "uk_reach": uk, "reach_range_lower": None,
            "reach_range_upper": None, "reach_source": "eu_total_reach" if reach is not None else ("uk" if uk else None),
            "reach_keys": None, "post_url": post, "reactions": None, "comments": comments, "shares": None,
            "fingerprint": "f", "raw_json": "{}"}


def _prod(pid, handle):
    return {"product_id": pid, "handle": handle, "title": handle, "vendor": "", "product_type": None, "tags": [],
            "created_at": "2026-08-01T00:00:00Z", "published_at": "2026-08-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
            "variant_count": 1, "sold_out_variants": 0, "min_price": 1.0, "max_price": 1.0, "collection_position": 0,
            "variants": [{"variant_id": pid * 10, "title": "x", "sku": None, "price": 1.0, "compare_at_price": None, "available": True}]}


class ReachCurveTests(unittest.TestCase):
    def _run_days(self, conn, sid, reach_by_day, comments_by_day=None, post=None):
        for i, (d, reach) in enumerate(reach_by_day):
            c = comments_by_day[i] if comments_by_day else None
            meta_ads.record_scrape(conn, sid, d, [_ad("a", reach=reach, comments=c, post=post)], "q")
            ad_metrics.process_store(conn, sid, "supp.com", d, None, fetch_landings=False)

    def test_delta_slope_and_rule8(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        db.write_product_snapshot(conn, sid, "2026-09-20", [_prod(1, "hero")])
        start = date(2026, 9, 6)
        # 15 days: flat 1000/day for the first week, then 2500/day
        days = []
        reach = 10000
        for i in range(15):
            days.append(((start + timedelta(days=i)).isoformat(), reach))
            reach += 1000 if i < 7 else 2500
        self._run_days(conn, sid, days)
        last = days[-1][0]
        row = conn.execute("SELECT reach_delta_1d, reach_slope_7d, reach_slope_prev_7d FROM meta_ads_daily WHERE snapshot_date = ?",
                           (last,)).fetchone()
        self.assertEqual(row["reach_delta_1d"], 2500)
        self.assertEqual(row["reach_slope_7d"], 2500.0)
        self.assertEqual(row["reach_slope_prev_7d"], 1000.0)
        alerts = ad_metrics.run_alerts(conn, sid, "supp.com", last)
        self.assertEqual([a["rule"] for a in alerts], [8])
        self.assertIn("reach/day 1000 -> 2500", alerts[0]["detail"])
        # first day: nothing to compare, no crash
        first = conn.execute("SELECT reach_delta_1d, reach_slope_7d FROM meta_ads_daily WHERE snapshot_date = ?", (days[0][0],)).fetchone()
        self.assertEqual(tuple(first), (None, None))

    def test_uk_reach_used_when_eu_missing_and_small_reach_never_alerts(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        start = date(2026, 9, 6)
        days = []
        for i in range(15):
            d = (start + timedelta(days=i)).isoformat()
            meta_ads.record_scrape(conn, sid, d, [_ad("u", uk=100 + (10 * i if i < 7 else 60 + 40 * (i - 7)))], "q")
            ad_metrics.process_store(conn, sid, "supp.com", d, None, fetch_landings=False)
            days.append(d)
        row = conn.execute("SELECT reach_slope_7d, reach_slope_prev_7d FROM meta_ads_daily WHERE snapshot_date = ?", (days[-1],)).fetchone()
        self.assertAlmostEqual(row["reach_slope_prev_7d"], 8.6)   # days 0..7: 100 -> 160 over 7 days
        self.assertEqual(row["reach_slope_7d"], 40.0)              # days 7..14: 160 -> 440 over 7 days
        self.assertEqual(ad_metrics.run_alerts(conn, sid, "supp.com", days[-1]), [])   # total reach < META_REACH_MIN

    def test_comment_delta_and_signals_columns(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        db.write_product_snapshot(conn, sid, "2026-09-07", [_prod(1, "hero")])
        self._run_days(conn, sid, [("2026-09-06", 5000), ("2026-09-07", 6200)], comments_by_day=[40, 55],
                       post="https://www.facebook.com/p/posts/1")
        row = conn.execute("SELECT comment_delta_1d, reach_slope_7d FROM meta_ads_daily WHERE snapshot_date='2026-09-07'").fetchone()
        self.assertEqual(row["comment_delta_1d"], 15)
        self.assertEqual(row["reach_slope_7d"], 1200.0)
        self.assertEqual(conn.execute("SELECT engagement_type FROM meta_ads WHERE ad_id='a'").fetchone()[0], "boosted")
        sig = {r[2]: r for r in sheets.signals_rows(conn)}
        self.assertEqual(len(sig["hero"]), len(sheets.SIGNALS_HEADERS))
        H = sheets.SIGNALS_HEADERS
        self.assertEqual([sig["hero"][H.index("eu_reach_slope_7d")], sig["hero"][H.index("comment_delta_1d")]], [1200.0, 15])

    def test_dark_ad_has_blank_engagement(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        meta_ads.record_scrape(conn, sid, "2026-09-06", [_ad("d", reach=100)], "q")
        ad_metrics.process_store(conn, sid, "supp.com", "2026-09-06", None, fetch_landings=False)
        self.assertEqual(conn.execute("SELECT engagement_type, post_url FROM meta_ads").fetchone()[:], ("dark", None))
        self.assertIsNone(conn.execute("SELECT comment_delta_1d FROM meta_ads_daily").fetchone()[0])


class BackfillAndCoverageTests(unittest.TestCase):
    def test_backfill_from_raw_json_and_coverage(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "ceylonhealth.com")
        # simulate ads scraped by the old code: raw_json present, reach/post fields absent
        old_style = []
        for n in nodes():
            a = meta_ads.normalise_ad(n)
            for k in ("uk_reach", "reach_range_lower", "reach_range_upper", "reach_source", "reach_keys", "post_url"):
                a[k] = None
            old_style.append(a)
        meta_ads.record_scrape(conn, sid, "2026-09-06", old_style, "q")
        conn.execute("UPDATE meta_ads SET engagement_type = NULL, reach_keys = NULL")
        conn.execute("UPDATE meta_ads_daily SET reach_source = NULL, uk_reach = NULL, reach_range_lower = NULL")
        conn.commit()
        ad_metrics.process_store(conn, sid, "ceylonhealth.com", "2026-09-06", None, fetch_landings=False)
        rows = {r["ad_id"]: dict(r) for r in conn.execute(
            "SELECT d.ad_id, d.uk_reach, d.reach_range_lower, a.post_url, a.engagement_type FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id")}
        self.assertEqual(rows["1001"]["uk_reach"], 5000)
        self.assertEqual(rows["1002"]["reach_range_lower"], 10000)
        self.assertEqual(rows["1002"]["engagement_type"], "boosted")
        self.assertEqual(rows["1001"]["engagement_type"], "dark")
        cov = ad_metrics.coverage(conn, "2026-09-06")
        self.assertEqual(len(cov), 1)
        c = cov[0]
        self.assertEqual((c["ads"], c["eu_exact"], c["uk_exact"], c["range_only"], c["no_reach"]), (3, 2, 0, 1, 0))
        self.assertEqual((c["boosted"], c["with_comments"], c["dark"]), (1, 0, 2))
        self.assertIn("aaa_info.eu_total_reach", c["keys"])

    def test_coverage_total_row_with_two_stores(self):
        conn = db.connect(":memory:")
        a = db.upsert_store(conn, "a.com")
        b = db.upsert_store(conn, "b.com")
        meta_ads.record_scrape(conn, a, "2026-09-06", [_ad("1", reach=10), _ad("2")], "q")
        meta_ads.record_scrape(conn, b, "2026-09-06", [_ad("3", post="https://www.facebook.com/x/posts/1")], "q")
        cov = ad_metrics.coverage(conn, "2026-09-06")
        self.assertEqual([c["store"] for c in cov], ["a.com", "b.com", "TOTAL"])
        self.assertEqual((cov[-1]["ads"], cov[-1]["eu_exact"], cov[-1]["no_reach"], cov[-1]["boosted"], cov[-1]["dark"]), (3, 1, 2, 1, 2))


if __name__ == "__main__":
    unittest.main()
