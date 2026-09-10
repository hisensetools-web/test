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
        # a newest-first order must not be stored as ranks: the newest ads would sit in the "top 5" on Signals
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta_ads_daily WHERE impression_rank IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(ad_rank.ad_rank_metrics(conn, sid, TODAY)["products"], {})

    def test_short_page_name_searches_the_domain(self):
        self.assertEqual(meta_ads.store_query({"store_domain": "gutbiowellness.com", "meta_page_name": "p"}), "gutbiowellness.com")
        self.assertEqual(meta_ads.store_query({"store_domain": "x.com", "meta_page_name": "Ruffs"}), "Ruffs")
        self.assertIn("q=gutbiowellness.com", ad_rank.rank_url({"store_domain": "gutbiowellness.com", "meta_page_name": "p"})[0])

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


def _res(ids, handle="calming-diffuser"):
    return meta_ads.ScrapeResult(url="u", ads=[_ad(a, "Brand", "2026-08-15", f"https://supp.com/products/{handle}", f"copy {a}") for a in ids])


class ScrapeRanksTests(unittest.TestCase):
    """Each country view is judged against its OWN newest-first order; an empty view is skipped, not the answer."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "supp.com")
        self.store = {"store_domain": "supp.com", "meta_page_name": "Brand"}
        self.world = [f"w{i}" for i in range(25)]
        self.calls = []

    def _fake(self, script):
        def scrape_page(url, **kw):
            self.calls.append(url)
            country = url.split("country=")[1].split("&")[0]
            kind = "sorted" if "sort_data" in url else "newest"
            return script[(country, kind)]
        return scrape_page

    def test_empty_view_is_skipped_and_eu_view_uses_its_own_baseline(self):
        nl = [f"n{i}" for i in range(22)]
        script = {("ALL", "sorted"): _res(self.world),            # worldwide: same order as newest -> ignored sort
                  ("DE", "sorted"): _res([]),                      # no DE delivery
                  ("NL", "sorted"): _res(list(reversed(nl))), ("NL", "newest"): _res(nl)}
        with mock.patch.object(meta_ads, "scrape_page", self._fake(script)), mock.patch.object(meta_ads, "_wait"):
            out = ad_rank.scrape_ranks(self.conn, None, self.store, self.sid, TODAY, self.world, countries=["ALL", "DE", "NL"])
        self.assertEqual((out["country"], out["informative"], out["ranked"]), ("NL", True, 22))
        self.assertIn("DE: 0 ads", out["summary"])
        self.assertIn("ALL: 25 ads, 20/20", out["summary"])
        chk = self.conn.execute("SELECT country, informative, note FROM rank_checks WHERE store_id = ?", (self.sid,)).fetchone()
        self.assertEqual((chk["country"], chk["informative"]), ("NL", 1))
        self.assertIn("NL: 22 ads", chk["note"])
        self.assertEqual(self.conn.execute("SELECT impression_rank FROM meta_ads_daily WHERE ad_id = 'n21'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT sort_informative FROM stores WHERE id = ?", (self.sid,)).fetchone()[0], 1)

    def test_eu_view_identical_to_its_own_newest_order_is_not_informative(self):
        de = [f"d{i}" for i in range(22)]   # differs from the worldwide list, but sorted == newest within the DE view
        script = {("ALL", "sorted"): _res(self.world), ("DE", "sorted"): _res(de), ("DE", "newest"): _res(de), ("NL", "sorted"): _res([])}
        with mock.patch.object(meta_ads, "scrape_page", self._fake(script)), mock.patch.object(meta_ads, "_wait"):
            out = ad_rank.scrape_ranks(self.conn, None, self.store, self.sid, TODAY, self.world, countries=["ALL", "DE", "NL"])
        self.assertEqual((out["country"], out["informative"]), ("DE", False))
        self.assertIn("DE: 22 ads, 20/20", out["summary"])
        self.assertIn("NL: 0 ads", out["summary"])
        self.assertEqual(self.conn.execute("SELECT sort_informative FROM stores WHERE id = ?", (self.sid,)).fetchone()[0], 0)

    def test_no_view_with_ads_leaves_the_verdict_open(self):
        script = {(c, "sorted"): _res([]) for c in ("ALL", "DE", "NL")}
        with mock.patch.object(meta_ads, "scrape_page", self._fake(script)), mock.patch.object(meta_ads, "_wait"):
            out = ad_rank.scrape_ranks(self.conn, None, self.store, self.sid, TODAY, self.world, countries=["ALL", "DE", "NL"])
        self.assertEqual((out["ranked"], out["informative"], out["country"]), (0, None, None))
        self.assertIsNone(self.conn.execute("SELECT sort_informative FROM stores WHERE id = ?", (self.sid,)).fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT note FROM rank_checks WHERE store_id = ?", (self.sid,)).fetchone()[0], "ALL: 0 ads; DE: 0 ads; NL: 0 ads")

    def test_a_verdict_without_a_note_is_not_trusted_as_confirmed(self):
        # the overnight run judged DE against the worldwide list (no note column yet): compare again, baseline and all
        self.conn.execute("INSERT INTO rank_checks (snapshot_date, store_id, informative, country) VALUES (?,?,1,'DE')", (WEEK_AGO, self.sid))
        self.conn.commit()
        self.assertIsNone(ad_rank._confirmed_country(self.conn, self.sid, TODAY))
        de = [f"d{i}" for i in range(22)]
        script = {("ALL", "sorted"): _res(self.world), ("DE", "sorted"): _res(de), ("DE", "newest"): _res(de), ("NL", "sorted"): _res([])}
        with mock.patch.object(meta_ads, "scrape_page", self._fake(script)), mock.patch.object(meta_ads, "_wait"):
            out = ad_rank.scrape_ranks(self.conn, None, self.store, self.sid, TODAY, self.world, countries=["ALL", "DE", "NL"])
        self.assertEqual((out["informative"], len(self.calls)), (False, 4))
        self.assertEqual(self.conn.execute("SELECT sort_informative FROM stores WHERE id = ?", (self.sid,)).fetchone()[0], 0)

    def test_a_reused_verdict_does_not_extend_the_confirmation(self):
        # day 1: real comparison; day 2: reused ("confirmed earlier"); day 9: the day-1 comparison is older than 7 days
        # and day 2's reuse must not count, so the baseline is scraped again
        nl = [f"n{i}" for i in range(22)]
        script = {("NL", "sorted"): _res(list(reversed(nl))), ("NL", "newest"): _res(nl), ("ALL", "sorted"): _res(self.world), ("DE", "sorted"): _res([])}
        with mock.patch.object(meta_ads, "scrape_page", self._fake(script)), mock.patch.object(meta_ads, "_wait"):
            ad_rank.scrape_ranks(self.conn, None, self.store, self.sid, "2026-09-01", self.world, countries=["ALL", "DE", "NL"])
            ad_rank.scrape_ranks(self.conn, None, self.store, self.sid, "2026-09-02", self.world, countries=["ALL", "DE", "NL"])
            self.calls.clear()
            out = ad_rank.scrape_ranks(self.conn, None, self.store, self.sid, "2026-09-09", self.world, countries=["ALL", "DE", "NL"])
        self.assertEqual(len(self.calls), 2)        # NL sorted + NL newest, no "confirmed earlier" shortcut
        self.assertIn("-> informative", out["summary"])
        self.assertNotIn("confirmed earlier", out["summary"])

    def test_confirmed_country_goes_first_without_a_baseline_scrape(self):
        nl = [f"n{i}" for i in range(22)]
        script = {("NL", "sorted"): _res(list(reversed(nl))), ("NL", "newest"): _res(nl), ("ALL", "sorted"): _res(self.world), ("DE", "sorted"): _res([])}
        fake = self._fake(script)
        with mock.patch.object(meta_ads, "scrape_page", fake), mock.patch.object(meta_ads, "_wait"):
            ad_rank.scrape_ranks(self.conn, None, self.store, self.sid, WEEK_AGO, self.world, countries=["ALL", "DE", "NL"])
            self.calls.clear()
            out = ad_rank.scrape_ranks(self.conn, None, self.store, self.sid, TODAY, self.world, countries=["ALL", "DE", "NL"])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("country=NL", self.calls[0])
        self.assertEqual((out["country"], out["informative"]), ("NL", True))
        self.assertIn("confirmed earlier", out["summary"])
        self.assertEqual(self.conn.execute("SELECT informative FROM rank_checks WHERE store_id = ? AND snapshot_date = ?", (self.sid, TODAY)).fetchone()[0], 1)


class StaleRankTests(unittest.TestCase):
    def test_ranks_without_a_real_verdict_are_cleared_once_and_never_shown(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "t.db")
            sid = db.upsert_store(conn, "elivorahealth.com")
            ids = [f"e{i}" for i in range(6)]
            # what the overnight run left behind: ranks stored under a verdict that had no per-view note
            seed_day(conn, sid, TODAY, ids, {i: "cinnamon-copy" for i in ids})
            conn.execute("UPDATE rank_checks SET note = NULL")
            conn.execute("DELETE FROM schema_migrations WHERE name LIKE '%clear-non-informative-ranks'")
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
            conn.close()
            conn = db.connect(Path(d) / "t.db")   # the one-off step runs here
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta_ads_daily WHERE impression_rank IS NOT NULL").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT sort_informative FROM stores WHERE id = ?", (sid,)).fetchone()[0])
            self.assertEqual(ad_rank.ad_rank_metrics(conn, sid, TODAY)["products"], {})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], len(db.MIGRATION_STEPS))
            conn.close()

    def test_metrics_need_an_informative_verdict(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        ids = [f"e{i}" for i in range(6)]
        seed_day(conn, sid, TODAY, ids, {i: "p" for i in ids})
        self.assertEqual(ad_rank.ad_rank_metrics(conn, sid, TODAY)["products"]["p"]["ads_in_top5"], 5)
        conn.execute("UPDATE stores SET sort_informative = 0 WHERE id = ?", (sid,))
        self.assertEqual(ad_rank.ad_rank_metrics(conn, sid, TODAY)["products"], {})
