"""prune-dead, find-page helpers, set-page, and the Early tab."""
import argparse
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from earlyscale import cli, db, sheets
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
        conn.execute("INSERT INTO meta_ads (ad_id, store_id, first_seen_date, last_seen_date) VALUES ('a1', ?, '2026-09-08', '2026-09-08')", (adsonly,))
        conn.commit()
        stores = [{"store_domain": d} for d in ("good.com", "dead.com", "adsonly.com", "neverrun.com")]
        got = cli.dead_stores(conn, stores)
        self.assertEqual([d["store_domain"] for d in got], ["dead.com"])
        self.assertEqual((got[0]["runs"], got[0]["last_error"][:8]), (1, "HTTP 404"))


class RestoreStoresTests(unittest.TestCase):
    def test_restore_puts_db_stores_back_on_the_watchlist(self):
        import argparse
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
    def test_brand_query_strips_prefixes(self):
        self.assertEqual(cli.brand_query("tryhappyharvest.com"), "happyharvest")
        self.assertEqual(cli.brand_query("getdovi.com"), "dovi")
        self.assertEqual(cli.brand_query("theethiopica.com"), "ethiopica")
        self.assertEqual(cli.brand_query("https://shop.pipitea.com/"), "pipitea")
        self.assertEqual(cli.brand_query("neuro-bella.com"), "neuro bella")
        self.assertEqual(cli.brand_query("try.com"), "try")            # too short to strip

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


class EarlyTabTests(unittest.TestCase):
    def test_early_rows_keep_young_products_with_ads_youngest_first(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)

        def prod(pid, handle, created_days, published_days=None):
            c = (now - timedelta(days=created_days)).isoformat()
            pub = (now - timedelta(days=published_days if published_days is not None else created_days)).isoformat()
            return {"product_id": pid, "handle": handle, "title": handle, "vendor": "", "product_type": None, "tags": [],
                    "created_at": c, "published_at": pub, "updated_at": pub, "variant_count": 1, "sold_out_variants": 0,
                    "min_price": 20.0, "max_price": 20.0, "collection_position": pid, "variants": [
                        {"variant_id": pid * 10, "title": "d", "sku": None, "price": 20.0, "compare_at_price": None, "available": True}]}
        db.write_product_snapshot(conn, sid, "2026-09-08", [prod(1, "young-with-ads", 10), prod(2, "young-no-ads", 5),
                                                            prod(3, "old-with-ads", 400, 20), prod(4, "younger-with-ads", 3)])
        for aid, handle in (("a1", "young-with-ads"), ("a2", "younger-with-ads"), ("a3", "old-with-ads")):
            conn.execute("""INSERT INTO meta_ads (ad_id, store_id, first_seen_date, last_seen_date, product_handle, ad_start_date)
                            VALUES (?,?,?,?,?,?)""", (aid, sid, "2026-09-08", "2026-09-08", handle, "2026-09-05"))
            conn.execute("INSERT INTO meta_ads_daily (snapshot_date, ad_id, store_id, is_active, fetched_at) VALUES ('2026-09-08', ?, ?, 1, 'x')", (aid, sid))
        conn.commit()
        with mock.patch.object(sheets, "watched_store_ids", lambda c: None):
            rows = sheets.early_rows(conn)
        self.assertEqual([r[1] for r in rows], ["younger-with-ads", "young-with-ads"])      # old (relaunched) and ad-less products excluded
        self.assertEqual(len(rows[0]), len(sheets.EARLY_HEADERS))
        H = sheets.EARLY_HEADERS
        self.assertEqual((rows[0][H.index("days_since_created")], rows[0][H.index("ads_pointing_here")]), (3, 1))


if __name__ == "__main__":
    unittest.main()


class RankDueTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "x.com")

    def _check(self, day, informative, note):
        self.conn.execute("INSERT OR REPLACE INTO rank_checks (snapshot_date, store_id, informative, note) VALUES (?,?,?,?)", (day, self.sid, informative, note))
        self.conn.execute("UPDATE stores SET sort_informative = ?, rank_checked_at = ? WHERE id = ?", (informative, day, self.sid))
        self.conn.commit()

    def test_unknown_or_informative_is_daily(self):
        self.assertTrue(cli._rank_due(self.conn, self.sid, "2026-09-10"))
        self._check("2026-09-10", 1, "ALL: 40 ads, 3/20 ... -> informative")
        self.assertTrue(cli._rank_due(self.conn, self.sid, "2026-09-10"))

    def test_not_informative_is_weekly(self):
        self._check("2026-09-08", 0, "ALL: 40 ads, 20/20 ... -> same order; DE: 0 ads; NL: 0 ads")
        self.assertFalse(cli._rank_due(self.conn, self.sid, "2026-09-10"))
        self.assertTrue(cli._rank_due(self.conn, self.sid, "2026-09-15"))

    def test_verdict_from_before_per_view_comparison_is_redone(self):
        self._check("2026-09-09", 0, None)
        self.assertTrue(cli._rank_due(self.conn, self.sid, "2026-09-10"))


class LandingRefreshAllTests(unittest.TestCase):
    """`landing --refresh-all`: every cached lander is fetched again under the current rules and the ads re-resolved."""

    def test_refresh_all_reattributes_ads_to_the_page_s_cta_product(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from earlyscale import ad_metrics, meta_ads
        from tests.test_ad_metrics import _ad, _prod

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/pages/prostate"):
                    body = (b'<header><nav><a href="/products/cinnamon-copy">c</a><a href="/products/cinnamon-copy">c</a></nav></header>'
                            b'<main><a class="btn" href="/products/prostate-softgels">Order Now</a><a href="/products/cinnamon-copy">also</a></main>')
                    ct = "text/html"
                else:
                    body, ct = b"<html>x</html>", "text/html"
                self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{srv.server_port}"
        with tempfile.TemporaryDirectory() as d:
            wl = Path(d) / "watchlist.csv"
            wl.write_text(f"store_domain,meta_page_name,meta_page_id,notes\n{origin},Brand,,\n")
            conn = db.connect(Path(d) / "t.db")
            sid = db.upsert_store(conn, origin, "Brand")
            db.write_product_snapshot(conn, sid, "2026-09-10", [_prod(1, "prostate-softgels", "P"), _prod(2, "cinnamon-copy", "C")])
            ads = [_ad(f"a{i}", "Brand", "2026-09-01", f"{origin}/pages/prostate", f"copy {i}") for i in range(3)]
            meta_ads.record_scrape(conn, sid, "2026-09-10", ads, "Brand")
            # what the old link-count rule left behind: the lander cached as selling the cinnamon product
            conn.execute("INSERT INTO landing_pages (url, fetched_at, status, final_url, product_handle, page_handle, candidates) VALUES (?,?,?,?,?,?,?)",
                         (f"{origin}/pages/prostate", "2026-09-10", 200, f"{origin}/pages/prostate", "cinnamon-copy", "prostate", "cinnamon-copy:4,prostate-softgels:1"))
            conn.execute("UPDATE meta_ads SET product_handle = 'cinnamon-copy', page_handle = 'prostate'")
            conn.commit()
            conn.close()
            with mock.patch.object(cli, "console"):
                rc = cli.cmd_landing(argparse.Namespace(db=str(Path(d) / "t.db"), url=None, apply=False, refresh_all=True,
                                                        only=None, force=False, watchlist=str(wl)))
            conn = db.connect(Path(d) / "t.db")
            self.assertEqual(rc, 0)
            self.assertEqual({r[0] for r in conn.execute("SELECT product_handle FROM meta_ads")}, {"prostate-softgels"})
            lp = conn.execute("SELECT product_handle, fetched_at FROM landing_pages").fetchone()
            self.assertEqual(lp[0], "prostate-softgels")
            self.assertEqual(conn.execute("SELECT resolver FROM landing_pages").fetchone()[0], ad_metrics.RESOLVER_VERSION)
            # a second run has nothing left to fetch (rows already carry the current resolver version)
            with mock.patch.object(cli, "console"), mock.patch.object(ad_metrics, "fetch_landing", side_effect=AssertionError("must not fetch")):
                conn.close()
                self.assertEqual(cli.cmd_landing(argparse.Namespace(db=str(Path(d) / "t.db"), url=None, apply=False, refresh_all=True,
                                                                    only=None, force=False, watchlist=str(wl))), 0)
                conn = db.connect(Path(d) / "t.db")
            self.assertFalse(ad_metrics.POLITE_LANDING_FETCH)   # restored
            conn.close()
        srv.shutdown()
