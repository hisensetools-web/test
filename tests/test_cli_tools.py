"""add-store, prune-dead, restore-stores, the pages helper, the Meta pass planning and the stop time."""
import argparse
import tempfile
import unittest
from pathlib import Path

from earlyscale import cli, db
from earlyscale.watchlist import append_to_watchlist, read_watchlist, update_watchlist_entry


class DeadStoreTests(unittest.TestCase):
    def test_never_ok_and_no_ads_is_dead(self):
        conn = db.connect(":memory:")
        good = db.upsert_store(conn, "good.com")
        dead = db.upsert_store(conn, "dead.com")
        adsonly = db.upsert_store(conn, "adsonly.com")
        run_id = db.start_run(conn, "2026-09-08", 3)
        for sid, status, err in ((good, "ok", None), (dead, "error", "HTTP 404 from https://dead.com/products.json"), (adsonly, "error", "unreachable")):
            conn.execute("INSERT INTO store_runs (run_id, store_id, snapshot_date, status, error) VALUES (?,?,?,?,?)", (run_id, sid, "2026-09-08", status, err))
        conn.execute("INSERT INTO ads (ad_id, store_id, first_scraped, last_scraped) VALUES ('a1', ?, '2026-09-08', '2026-09-08')", (adsonly,))
        conn.commit()
        stores = [{"store_domain": d} for d in ("good.com", "dead.com", "adsonly.com", "neverrun.com")]
        got = cli.dead_stores(conn, stores)
        self.assertEqual([d["store_domain"] for d in got], ["dead.com"])
        self.assertEqual((got[0]["runs"], got[0]["last_error"][:8]), (1, "HTTP 404"))


class RestoreStoresTests(unittest.TestCase):
    def test_restore_puts_db_stores_back_on_the_watchlist(self):
        with tempfile.TemporaryDirectory() as d:
            wl = Path(d) / "w.csv"
            append_to_watchlist({"store_domain": "kept.com"}, wl)
            dbp = Path(d) / "t.db"
            conn = db.connect(dbp)
            db.upsert_store(conn, "kept.com")
            db.upsert_store(conn, "gone.com", "Gone Page", None, "was pruned")
            conn.execute("UPDATE stores SET platform = 'generic', platform_checked_at = '2026-09-01' WHERE store_domain = 'gone.com'")
            conn.commit()
            conn.close()
            args = argparse.Namespace(db=dbp, watchlist=str(wl), domains=[], apply=True)
            self.assertEqual(cli.cmd_restore_stores(args), 0)
            rows = {r["store_domain"]: r for r in read_watchlist(wl)}
            self.assertEqual(sorted(rows), ["gone.com", "kept.com"])
            self.assertEqual((rows["gone.com"]["meta_page_name"], rows["gone.com"]["notes"]), ("Gone Page", "was pruned"))
            conn = db.connect(dbp)
            self.assertIsNone(conn.execute("SELECT platform FROM stores WHERE store_domain = 'gone.com'").fetchone()[0])   # re-detected next run


class PageFinderTests(unittest.TestCase):
    def test_page_candidates_rank_store_landing_pages_first(self):
        ads = [{"page_id": "1", "page_name": "Happy Harvest", "landing_url": "https://tryhappyharvest.com/products/x"},
               {"page_id": "1", "page_name": "Happy Harvest", "landing_url": "https://tryhappyharvest.com/pages/offer"},
               {"page_id": "2", "page_name": "Harvest Blog", "landing_url": "https://harvestblog.example/a"},
               {"page_id": "2", "page_name": "Harvest Blog", "landing_url": "https://harvestblog.example/b"},
               {"page_id": "2", "page_name": "Harvest Blog", "landing_url": "https://harvestblog.example/c"},
               {"page_id": None, "page_name": "", "landing_url": "https://x.example"}]
        c = cli.page_candidates(ads, "tryhappyharvest.com")
        self.assertEqual([(p["page_name"], p["ads"], p["on_store"]) for p in c], [("Happy Harvest", 2, 2), ("Harvest Blog", 3, 0)])
        self.assertEqual(c[0]["top_domain"], "tryhappyharvest.com")

    def test_update_watchlist_entry(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.csv"
            append_to_watchlist({"store_domain": "getdovi.com", "notes": "keep me"}, p)
            self.assertTrue(update_watchlist_entry("https://getdovi.com/", p, meta_page_name="Dovi", meta_page_id="123"))
            self.assertFalse(update_watchlist_entry("nope.com", p, meta_page_name="x"))
            row = read_watchlist(p)[0]
            self.assertEqual((row["meta_page_name"], row["meta_page_id"], row["notes"]), ("Dovi", "123", "keep me"))


class AddStoreTests(unittest.TestCase):
    def test_list_and_file_without_any_page_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            wl, dbp, lst = Path(d) / "w.csv", Path(d) / "t.db", Path(d) / "domains.txt"
            lst.write_text("# my list\nhttps://www.Three.com/products/x\nfour.com,some note\n\nthree.com\n", encoding="utf-8")
            args = argparse.Namespace(db=dbp, watchlist=str(wl), domains=["one.com", "two.com"], file=str(lst), page_id=None, notes="sep")
            self.assertEqual(cli.cmd_add_store(args), 0)
            rows = read_watchlist(wl)
            self.assertEqual([r["store_domain"] for r in rows], ["one.com", "two.com", "three.com", "four.com"])
            self.assertTrue(all(r["meta_page_name"] == "" and r["meta_page_id"] == "" for r in rows))   # the domain is the search key
            self.assertEqual(db.connect(dbp).execute("SELECT COUNT(*) FROM stores").fetchone()[0], 4)


class PlanTests(unittest.TestCase):
    def test_least_recently_scraped_first(self):
        conn = db.connect(":memory:")
        stores = [{"store_domain": d} for d in ("a.com", "b.com", "c.com")]
        for d in stores:
            db.upsert_store(conn, d["store_domain"])
        conn.execute("INSERT INTO meta_page_runs (store_id, snapshot_date, status, ran_at) VALUES (1, '2026-09-10', 'ok', 'x'), (2, '2026-09-08', 'ok', 'x')")
        conn.commit()
        self.assertEqual([s["store_domain"] for s in cli.plan_meta_stores(conn, stores)], ["c.com", "b.com", "a.com"])
        self.assertEqual([s["store_domain"] for s in cli.plan_meta_stores(conn, stores, only={"a.com"})], ["a.com"])
        self.assertEqual([s["store_domain"] for s in cli.plan_meta_stores(conn, stores, scope=["B.com"])], ["b.com"])


class StopAtTests(unittest.TestCase):
    def test_stop_time_is_the_next_occurrence(self):
        from datetime import datetime
        night = datetime(2026, 9, 11, 22, 5)
        self.assertEqual(cli._stop_at_clock("08:30", night), datetime(2026, 9, 12, 8, 30))
        morning = datetime(2026, 9, 12, 7, 0)
        self.assertEqual(cli._stop_at_clock("08:30", morning), datetime(2026, 9, 12, 8, 30))
        self.assertIsNone(cli._stop_at_clock("", night))
        self.assertIsNone(cli._stop_at_clock("nope", night))


if __name__ == "__main__":
    unittest.main()
