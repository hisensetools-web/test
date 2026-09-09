"""Impression rank: sort URL, order comparison, per-day ranks, per-ad / per-product metrics, ranking, alerts."""
import unittest
from unittest import mock

from earlyscale import ad_metrics, ad_rank, db, meta_ads, sheets
from tests.test_ad_metrics import _ad, _prod

TODAY = "2026-09-08"
WEEK_AGO = "2026-09-01"


class PureTests(unittest.TestCase):
    def test_sort_url(self):
        u = meta_ads.build_search_url(query="getruffs.com", sort="impressions")
        self.assertIn("sort_data[mode]=total_impressions", u)
        self.assertIn("sort_data[direction]=desc", u)
        self.assertNotIn("sort_data", meta_ads.build_search_url(query="getruffs.com"))
        self.assertIn("view_all_page_id=42", ad_rank.rank_url({"store_domain": "x.com", "meta_page_id": "42"})[0])

    def test_compare_orders(self):
        ids = [str(i) for i in range(30)]
        same = ad_rank.compare_orders(ids, ids)
        self.assertEqual((same["identical"], same["informative"], same["n"]), (True, False, 20))
        rev = ad_rank.compare_orders(ids, list(reversed(ids)))
        self.assertEqual((rev["identical"], rev["informative"], rev["same_position"]), (False, True, 0))
        few = ad_rank.compare_orders(ids[:3], ids[:3])
        self.assertEqual((few["n"], few["informative"]), (3, False))
        shuffled = ids[:10] + ids[10:20][::-1]
        self.assertTrue(ad_rank.compare_orders(ids, shuffled)["informative"])


def seed_day(conn, sid, day, order, handles, default_order=None):
    """order: ad ids in impressions order; default_order: ids in newest-first order (defaults to reversed)."""
    ads = [_ad(aid, "Brand", "2026-08-15", f"https://supp.com/products/{handles[aid]}", f"copy {aid}") for aid in order]
    meta_ads.record_scrape(conn, sid, day, ads, "q")
    for aid in order:
        conn.execute("UPDATE meta_ads SET product_handle = ? WHERE ad_id = ?", (handles[aid], aid))
    conn.commit()
    return ad_rank.record_ranks(conn, sid, day, ads, "q", default_order if default_order is not None else list(reversed(order)))


class RankTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "supp.com")
        db.write_product_snapshot(self.conn, self.sid, TODAY, [_prod(1, "calming-diffuser", "Diffuser", 0), _prod(2, "kombucha-gummy", "Gummy", 1)])
        self.conn.execute("UPDATE products_daily SET created_at = '2026-08-25T00:00:00Z' WHERE handle = 'kombucha-gummy'")
        self.conn.commit()
        self.handles = {f"d{i}": "calming-diffuser" for i in range(8)} | {f"g{i}": "kombucha-gummy" for i in range(4)}
        # a week ago: diffuser ads own the top; g0 sits at rank 9
        seed_day(self.conn, self.sid, WEEK_AGO, ["d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7", "g0", "g1", "g2", "g3"], self.handles)
        # today: g0 climbed to rank 2, g1 entered the top 5 at rank 4
        self.cmp = seed_day(self.conn, self.sid, TODAY, ["d0", "g0", "d1", "g1", "d2", "d3", "d4", "d5", "d6", "d7", "g2", "g3"], self.handles)

    def test_ranks_recorded_and_comparison(self):
        self.assertEqual((self.cmp["ranked"], self.cmp["identical"], self.cmp["informative"]), (12, False, True))
        r = {x[0]: x[1] for x in self.conn.execute("SELECT ad_id, impression_rank FROM meta_ads_daily WHERE snapshot_date = ?", (TODAY,))}
        self.assertEqual((r["d0"], r["g0"], r["g1"], r["g3"]), (1, 2, 4, 12))
        self.assertEqual(self.conn.execute("SELECT sort_informative FROM stores WHERE id = ?", (self.sid,)).fetchone()[0], 1)
        chk = self.conn.execute("SELECT * FROM rank_checks WHERE store_id = ? AND snapshot_date = ?", (self.sid, TODAY)).fetchone()
        self.assertEqual((chk["n_compared"], chk["identical"], chk["informative"]), (12, 0, 1))

    def test_identical_order_marks_not_informative(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "same.com")
        ids = [f"s{i}" for i in range(25)]
        cmp = seed_day(conn, sid, TODAY, ids, {i: "p" for i in ids}, default_order=ids)
        self.assertEqual((cmp["identical"], cmp["informative"]), (True, False))
        self.assertEqual(conn.execute("SELECT sort_informative FROM stores WHERE id = ?", (sid,)).fetchone()[0], 0)

    def test_metrics_per_ad_and_product(self):
        m = ad_rank.ad_rank_metrics(self.conn, self.sid, TODAY)
        self.assertEqual((m["as_of"], m["prev_as_of"]), (TODAY, WEEK_AGO))
        g0 = m["ads"]["g0"]
        self.assertEqual((g0["rank"], g0["rank_7d_ago"], g0["rank_delta_7d"], g0["top5_days"], g0["entered_top5"]), (2, 9, -7, 1, True))
        self.assertEqual(m["ads"]["d0"]["top5_days"], 2)
        g = m["products"]["kombucha-gummy"]
        self.assertEqual((g["best_rank"], g["best_rank_7d_ago"], g["best_rank_delta_7d"], g["ads_in_top5"]), (2, 9, -7, 2))
        d = m["products"]["calming-diffuser"]
        self.assertEqual((d["best_rank"], d["ads_in_top5"], d["best_rank_delta_7d"]), (1, 3, 0))
        self.assertEqual([a["ad_id"] for a in g["top5_ads"]], ["g0", "g1"])
        self.assertEqual(g["top5_ads"][0]["days_running"], 24)

    def test_alerts_15_16(self):
        written = ad_rank.run_alerts(self.conn, self.sid, "supp.com", TODAY)
        rules = sorted((w["rule"], w["handle"]) for w in written)
        self.assertIn((15, "kombucha-gummy"), rules)              # g0 and g1 entered the top 5 on a 14-day-old product
        self.assertIn((16, "kombucha-gummy"), rules)              # g0 climbed 7 ranks
        self.assertNotIn((15, "calming-diffuser"), rules)         # old product: no rule 15
        self.assertEqual(sum(1 for r, _ in rules if r == 15), 2)
        again = ad_rank.run_alerts(self.conn, self.sid, "supp.com", TODAY)
        self.assertEqual(again, [])                               # deduped

    def test_rule_17_delivering_doubled(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        db.write_product_snapshot(conn, sid, TODAY, [_prod(1, "p", "P", 0)])
        for day, n in ((WEEK_AGO, 2), (TODAY, 5)):
            ads = [_ad(f"a{i}", "B", "2026-08-15", "https://x.com/products/p", "c") for i in range(n)]
            for a in ads:
                a["low_impressions"] = 0
            meta_ads.record_scrape(conn, sid, day, ads, "q")
            conn.execute("UPDATE meta_ads SET product_handle = 'p'")
            conn.commit()
        written = ad_rank.run_alerts(conn, sid, "x.com", TODAY)
        self.assertEqual([(w["rule"], w["handle"]) for w in written], [(17, "p")])

    def test_signals_rank_on_top5_then_trend(self):
        ad_metrics.write_concept_rows(self.conn, self.sid, TODAY)
        with mock.patch.object(sheets, "watched_store_ids", lambda c: None):
            rows = sheets.signals_rows(self.conn)
        H = sheets.SIGNALS_HEADERS
        by = {r[H.index("handle")]: r for r in rows}
        d, g = by["calming-diffuser"], by["kombucha-gummy"]
        self.assertEqual((d[H.index("ads_in_top5")], d[H.index("best_rank")], d[H.index("best_rank_delta_7d")]), (3, 1, 0))
        self.assertEqual((g[H.index("ads_in_top5")], g[H.index("best_rank")], g[H.index("best_rank_delta_7d")]), (2, 2, -7))
        self.assertEqual([r[H.index("handle")] for r in rows], ["calming-diffuser", "kombucha-gummy"])   # 3 in top 5 beats 2, before trend
        with mock.patch.object(sheets, "watched_store_ids", lambda c: None):
            srows = sheets.stores_rows(self.conn)
        self.assertEqual(srows[0][sheets.STORES_HEADERS.index("sort_informative")], "true")


if __name__ == "__main__":
    unittest.main()
