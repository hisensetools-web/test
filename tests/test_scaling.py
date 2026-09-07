"""Part A: pages per domain, new pages, landing paths per product, alerts 13/14, Pages tab."""
import unittest
from datetime import date, timedelta
from pathlib import Path

from earlyscale import config, db, scaling, sheets

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"
TODAY = "2026-09-08"


def d(n):
    return (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()


def seed(conn, sid, ads):
    """ads: (ad_id, page_id, page_name, start, landing_url, handle, active)"""
    for aid, pid, pname, start, url, handle, active in ads:
        conn.execute("""INSERT INTO meta_ads (ad_id, store_id, page_id, page_name, ad_start_date, first_seen_date, last_seen_date, landing_url,
                        landing_domain, product_handle) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (aid, sid, pid, pname, start, d(1), TODAY, url, url.split("/")[2] if url else None, handle))
        conn.execute("INSERT INTO meta_ads_daily (snapshot_date, ad_id, store_id, is_active, fetched_at) VALUES (?,?,?,?,'x')", (TODAY, aid, sid, active))
    conn.commit()


class ScalingTests(unittest.TestCase):
    def test_landing_path(self):
        self.assertEqual(scaling.landing_path("https://www.Shop.com/pages/story?utm=1#top"), "shop.com/pages/story")
        self.assertEqual(scaling.landing_path("https://shop.com/"), "shop.com/")
        self.assertIsNone(scaling.landing_path(None))

    def test_pages_paths_and_alerts(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "shop.com")
        seed(conn, sid, [
            ("1", "p1", "Old Page", d(40), "https://shop.com/products/a?utm=x", "a", 1),
            ("2", "p1", "Old Page", d(3), "https://shop.com/products/a?utm=y", "a", 1),      # same path, new ad
            ("3", "p2", "New Page A", d(2), "https://shop.com/pages/story-1", "a", 1),        # new page, new path
            ("4", "p3", "New Page B", d(1), "https://shop.com/pages/story-2", "a", 1),        # new page, new path
            ("5", "p3", "New Page B", d(1), "https://shop.com/pages/story-3", "a", 0),        # inactive ad, new path
            ("6", "p4", "Other Advertiser", d(1), "https://elsewhere.com/x", None, 1),        # not on the store: ignored
            ("7", "p1", "Old Page", d(30), "https://shop.com/products/b", "b", 0),            # b: only inactive
        ])
        sm = scaling.store_page_metrics(conn, sid, "shop.com", TODAY)
        self.assertEqual((sm["pages_per_domain"], sm["pages_new_7d"]), (3, 2))
        self.assertEqual(sorted(sm["new_pages"]), ["New Page A", "New Page B"])
        pm = scaling.product_metrics(conn, sid, "shop.com", TODAY)
        self.assertEqual((pm["a"]["pages_pointing_here"], pm["a"]["pages_new_7d"]), (3, 2))
        self.assertEqual((pm["a"]["landing_paths"], pm["a"]["landing_paths_new_7d"]), (4, 3))
        self.assertEqual(pm["a"]["new_paths"], ["shop.com/pages/story-1", "shop.com/pages/story-2", "shop.com/pages/story-3"])
        self.assertEqual((pm["b"]["pages_pointing_here"], pm["b"]["landing_paths"]), (0, 1))
        found = scaling.run_alerts(conn, sid, "shop.com", TODAY)
        self.assertEqual(sorted(f["rule"] for f in found), [13, 14])
        self.assertEqual(scaling.run_alerts(conn, sid, "shop.com", TODAY), [])   # weekly dedupe
        rows = scaling.pages_tab_rows(conn, [{"id": sid, "store_domain": "shop.com"}], TODAY)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][1], "New Page B")            # newest first
        self.assertEqual(len(rows[0]), len(sheets.PAGES_HEADERS))
        srows = {r[0]: r for r in sheets.stores_rows(conn, TODAY)}
        H = sheets.STORES_HEADERS
        self.assertEqual([srows["shop.com"][H.index("pages_per_domain")], srows["shop.com"][H.index("pages_new_7d")]], [3, 2])
        # Signals carries the product columns
        from earlyscale import shopify
        db.write_product_snapshot(conn, sid, TODAY, [{"product_id": 1, "handle": "a", "title": "A", "vendor": "", "product_type": None, "tags": [],
            "created_at": d(10), "published_at": d(10), "updated_at": d(10), "variant_count": 1, "sold_out_variants": 0, "min_price": 1,
            "max_price": 1, "collection_position": None, "variants": []}])
        sig = {r[2]: r for r in sheets.signals_rows(conn, TODAY)}
        S = sheets.SIGNALS_HEADERS
        self.assertEqual([sig["a"][S.index(c)] for c in ("pages_pointing_here", "pages_new_7d", "landing_paths", "landing_paths_new_7d")], [3, 2, 4, 3])


if __name__ == "__main__":
    unittest.main()
