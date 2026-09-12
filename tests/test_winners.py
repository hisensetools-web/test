"""The derived numbers: landing path normalisation, product families, url_daily per day, the Winners / Stores rows."""
import unittest
from datetime import date, timedelta

from earlyscale import db, meta_ads, winners

TODAY = "2026-09-13"


def d(n):
    return (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()


def ad(aid, page, start, url, low=0, active=1, page_id=None):
    return {"ad_id": aid, "page_id": page_id or page.lower().replace(" ", ""), "page_name": page, "start_date": start, "end_date": None,
            "is_active": active, "primary_text": "t", "landing_url": url, "landing_domain": "elivorahealth.com", "low_impressions": low}


def prod(pid, handle, title, created):
    return {"product_id": pid, "handle": handle, "title": title, "created_at": created + "T00:00:00Z", "published_at": created + "T00:00:00Z"}


class PureTests(unittest.TestCase):
    def test_landing_path_drops_query_case_www_and_trailing_slash(self):
        self.assertEqual(winners.normalise_landing_path("https://www.Elivorahealth.com/pages/Prostate/?utm_source=fb&fbclid=1#top"),
                         "elivorahealth.com/pages/prostate")
        self.assertEqual(winners.normalise_landing_path("elivorahealth.com"), "elivorahealth.com/")
        self.assertEqual(winners.normalise_landing_path("http://127.0.0.1:8001/products/x?variant=1"), "127.0.0.1:8001/products/x")
        self.assertIsNone(winners.normalise_landing_path(None))
        self.assertIsNone(winners.normalise_landing_path(""))

    def test_product_handle_only_for_products_paths(self):
        self.assertEqual(winners.product_handle_of("shop.com/products/glow-serum-copy"), "glow-serum-copy")
        self.assertEqual(winners.product_handle_of("shop.com/collections/all/products/glow"), "glow")
        self.assertIsNone(winners.product_handle_of("shop.com/pages/prostate"))
        self.assertIsNone(winners.product_handle_of("shop.com/"))

    def test_family_key_strips_duplicate_suffixes(self):
        for h in ("glow-serum", "glow-serum-copy", "glow-serum-1", "glow-serum-2", "glow-serum-cc", "glow-serum-otp", "glow-serum-sub",
                  "glow-serum-es", "glow-serum-copy-2", "glow-serum-tt"):
            self.assertEqual(winners.family_key(h), "glow-serum", h)
        self.assertEqual(winners.family_key("omega-3"), "omega")          # a trailing number is a duplicate marker; the family is still one product
        self.assertEqual(winners.family_key("copy"), "copy")              # never empty

    def test_families_group_by_product_id_then_handle_then_title(self):
        rows = [prod(1, "glow-serum", "Glow Serum", "2026-01-10"), prod(1, "glow-serum-renamed", "Glow Serum", "2026-01-10"),   # same id, handle renamed
                prod(2, "glow-serum-copy", "Glow Serum (Copy)", "2026-03-01"),                                                    # duplicate by handle
                prod(3, "radiance-oil", "Glow Serum - ES", "2026-04-01"),                                                          # duplicate by title only
                prod(4, "night-cream", "Night Cream", "2026-02-01")]
        fam = winners.build_families(rows)
        self.assertEqual({fam[h]["family"] for h in ("glow-serum", "glow-serum-renamed", "glow-serum-copy", "radiance-oil")}, {"glow-serum"})
        self.assertEqual(fam["glow-serum-copy"]["created"], "2026-01-10")            # oldest created_at in the family
        self.assertEqual(fam["night-cream"]["family"], "night-cream")
        self.assertEqual(sorted(fam["glow-serum"]["handles"]), ["glow-serum", "glow-serum-copy", "glow-serum-renamed", "radiance-oil"])

    def test_wow_cell(self):
        self.assertEqual(winners.wow_cell(6, 3), 2.0)
        self.assertEqual(winners.wow_cell(6, None), "")
        self.assertEqual(winners.wow_cell(6, 0), "new")
        self.assertEqual(winners.wow_cell(0, 0), "")
        self.assertGreater(winners.wow_sort_value("new"), winners.wow_sort_value(9.9))
        self.assertLess(winners.wow_sort_value(""), winners.wow_sort_value(0.1))

    def test_week_ago_scrape_picks_the_closest_within_a_day(self):
        self.assertEqual(winners.week_ago_scrape([d(9), d(8), d(6), d(1)], TODAY), d(8))
        self.assertEqual(winners.week_ago_scrape([d(6), d(7)], TODAY), d(7))
        self.assertIsNone(winners.week_ago_scrape([d(9), d(5)], TODAY))


class UrlDailyTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "elivorahealth.com", "Elivora")
        db.write_product_snapshot(self.conn, self.sid, d(1), [prod(1, "elivora-prostate-softgels", "Prostate", "2026-08-20"),
                                                            prod(2, "elivora-prostate-softgels-copy", "Prostate (copy)", "2026-09-01"),
                                                            prod(3, "berberine", "Berberine", "2026-01-01")])

    def test_delivering_counts_only_unbadged_active_ads_and_never_guesses_a_product_for_a_lander(self):
        ads = [ad("1", "Elivora", d(30), "https://elivorahealth.com/pages/prostate?utm=1"),
               ad("2", "Elivora", d(3), "https://elivorahealth.com/pages/prostate/"),
               ad("3", "Elivora", d(3), "https://elivorahealth.com/pages/prostate", low=1),           # badge: not delivering
               ad("4", "Second Page", d(2), "https://www.elivorahealth.com/pages/Prostate", low=None),   # card never rendered: not delivering
               ad("5", "Second Page", d(5), "https://elivorahealth.com/pages/prostate"),
               ad("6", "Elivora", d(10), "https://elivorahealth.com/products/elivora-prostate-softgels-copy?variant=9"),
               ad("7", "Elivora", d(10), "https://elivorahealth.com/products/berberine", active=0)]
        c = meta_ads.record_scrape(self.conn, self.sid, TODAY, ads, "Elivora")
        self.assertEqual((c["total"], c["badge_known"], c["urls"]), (7, 6, 2))
        rows = {r["landing_path"]: dict(r) for r in self.conn.execute("SELECT * FROM url_daily WHERE snapshot_date = ?", (TODAY,))}
        lander = rows["elivorahealth.com/pages/prostate"]
        self.assertEqual((lander["delivering"], lander["pages"], lander["top_page"], lander["proven_days"], lander["pages_new_7d"]),
                         (3, 2, "Elivora", 30, 1))                        # Second Page's first delivering ad is 5 days old
        self.assertIsNone(lander["family_key"])                           # a /pages/ lander gets no product family
        self.assertIsNone(lander["delivering_7d_ago"])                    # first scrape: no history
        prod_row = rows["elivorahealth.com/products/elivora-prostate-softgels-copy"]
        self.assertEqual((prod_row["family_key"], prod_row["family_created"], prod_row["delivering"]), ("elivora-prostate-softgels", "2026-08-20", 1))
        self.assertNotIn("elivorahealth.com/products/berberine", rows)    # inactive ad: not counted
        daily = {r["ad_id"]: r["low_impressions"] for r in self.conn.execute("SELECT ad_id, low_impressions FROM ads_daily WHERE snapshot_date = ?", (TODAY,))}
        self.assertEqual((daily["3"], daily["4"], daily["5"]), (1, None, 0))

    def test_week_over_week_and_still_active(self):
        week_ago = d(7)
        first = [ad("1", "Elivora", d(20), "https://elivorahealth.com/pages/prostate"), ad("2", "Elivora", d(20), "https://elivorahealth.com/pages/prostate")]
        meta_ads.record_scrape(self.conn, self.sid, week_ago, first, "Elivora")
        today = first + [ad(str(i), "Elivora", d(2), "https://elivorahealth.com/pages/prostate") for i in range(3, 9)]
        today = [a for a in today if a["ad_id"] != "2"]                   # ad 2 vanished
        c = meta_ads.record_scrape(self.conn, self.sid, TODAY, today, "Elivora")
        self.assertEqual(c["disappeared"], 1)
        gone = self.conn.execute("SELECT still_active FROM ads_daily WHERE snapshot_date = ? AND ad_id = '2'", (TODAY,)).fetchone()
        self.assertEqual(gone["still_active"], 0)
        row = self.conn.execute("SELECT * FROM url_daily WHERE snapshot_date = ? AND landing_path = 'elivorahealth.com/pages/prostate'", (TODAY,)).fetchone()
        self.assertEqual((row["delivering"], row["delivering_7d_ago"], row["proven_days"]), (7, 2, 20))
        stores = [dict(r) for r in self.conn.execute("SELECT id, store_domain, shop_id, store_created_est FROM stores")]
        W = winners.WINNERS_HEADERS
        rows = winners.winners_rows(self.conn, stores, TODAY)
        self.assertEqual(len(rows), 1)
        r = dict(zip(W, rows[0]))
        self.assertEqual((r["store"], r["landing_url"], r["delivering"], r["delivering_7d_ago"], r["delivering_wow"], r["proven_days"], r["ads_as_of"]),
                         ("elivorahealth.com", "elivorahealth.com/pages/prostate", 7, 2, 3.5, 20, TODAY))
        self.assertEqual((r["family_age_days"], r["store_age_days"]), ("", ""))
        # a URL that was not delivering a week ago but is now: 'new', sorted first
        db.write_product_snapshot(self.conn, self.sid, TODAY, [prod(1, "elivora-prostate-softgels", "Prostate", "2026-08-20")])
        extra = today + [ad(str(i), "Elivora", d(1), "https://elivorahealth.com/products/elivora-prostate-softgels") for i in range(20, 23)]
        meta_ads.record_scrape(self.conn, self.sid, TODAY, extra, "Elivora")
        rows = winners.winners_rows(self.conn, stores, TODAY)
        self.assertEqual([r[1] for r in rows], ["elivorahealth.com/products/elivora-prostate-softgels", "elivorahealth.com/pages/prostate"])
        self.assertEqual((rows[0][W.index("delivering_wow")], rows[0][W.index("family_age_days")]), ("new", 24))
        # the same rows are what rebuild produces from scratch
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM url_daily ORDER BY 1, 2, 3")]
        winners.rebuild_url_daily(self.conn)
        self.assertEqual(before, [tuple(r) for r in self.conn.execute("SELECT * FROM url_daily ORDER BY 1, 2, 3")])

    def test_min_delivering_threshold_and_stores_rows(self):
        meta_ads.record_scrape(self.conn, self.sid, TODAY, [ad("1", "P", d(1), "https://elivorahealth.com/pages/a"), ad("2", "P", d(1), "https://elivorahealth.com/pages/a")], "q")
        stores = [dict(r) for r in self.conn.execute("SELECT id, store_domain, shop_id, store_created_est FROM stores")]
        self.assertEqual(winners.winners_rows(self.conn, stores, TODAY), [])
        self.conn.execute("UPDATE stores SET shop_id = 987654321012, store_created_est = ? WHERE id = ?", (d(100), self.sid))
        run_id = db.start_run(self.conn, TODAY, 1)
        db.record_store_run(self.conn, run_id, self.sid, TODAY, "error", "HTTP 404 (not a Shopify storefront?)", 0, 0, 0.1)
        stores = [dict(r) for r in self.conn.execute("SELECT id, store_domain, shop_id, store_created_est FROM stores")]
        row = dict(zip(winners.STORES_HEADERS, winners.stores_rows(self.conn, stores, TODAY)[0]))
        self.assertEqual(row, {"store": "elivorahealth.com", "shop_id": "987654321012", "store_age_days": 100, "products": 3, "ads_as_of": TODAY,
                               "last error": "HTTP 404 (not a Shopify storefront?)"})

    def test_two_stores_sharing_a_page_keep_their_own_rows(self):
        other = db.upsert_store(self.conn, "beast.viture.com", "VITURE")
        shared = [ad("1", "VITURE", d(1), "https://viture.com/products/luma")]
        meta_ads.record_scrape(self.conn, self.sid, TODAY, shared, "VITURE")
        meta_ads.record_scrape(self.conn, other, TODAY, shared, "VITURE")     # used to violate the PRIMARY KEY on ad_id
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
