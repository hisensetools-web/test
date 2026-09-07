"""Store age from the Shopify shop ID: extraction, calibration interpolation, DB wiring, tabs."""
import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from earlyscale import config, db, sheets, store_age

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"

HTML_WALLET = '''<html><head><meta name="shopify-digital-wallet" content="/61234567/digital_wallets/dialog">
<script>window.Shopify = window.Shopify || {}; Shopify.shop = "Cool-Store.myshopify.com";</script></head><body></body></html>'''
HTML_TREKKIE = '''<html><head><script>window.ShopifyAnalytics.meta = {"page":{"pageType":"home"},"currency":"USD"};
var trekkie = {"Trekkie":{"appName":"storefront","defaultAttributes":{"shopId":70000123,"themeId":1}}};
window.Shopify.shop = "another.myshopify.com";</script></head></html>'''
HTML_FEATURES = '''<script id="shopify-features" type="application/json">{"accessToken":"x","betas":[],"domain":"shop.example.com","shopId":88000000,"locale":"en"}</script>'''
HTML_NONE = "<html><head><title>Not a Shopify store</title></head><body>hello</body></html>"


class ExtractTests(unittest.TestCase):
    def test_wallet_meta_wins_and_handle_is_lowercased(self):
        got = store_age.extract_identity(HTML_WALLET)
        self.assertEqual((got["shop_id"], got["myshopify"], got["source"]), (61234567, "cool-store.myshopify.com", "digital-wallet meta"))

    def test_shop_id_from_analytics_config(self):
        got = store_age.extract_identity(HTML_TREKKIE)
        self.assertEqual((got["shop_id"], got["myshopify"]), (70000123, "another.myshopify.com"))
        self.assertEqual(store_age.extract_identity(HTML_FEATURES)["shop_id"], 88000000)

    def test_no_shopify_markup(self):
        got = store_age.extract_identity(HTML_NONE)
        self.assertEqual((got["shop_id"], got["myshopify"], got["source"]), (None, None, None))


def cal(*pairs):
    return store_age.Calibration([(sid, date.fromisoformat(d)) for sid, d in sorted(pairs)])


class InterpolationTests(unittest.TestCase):
    C = cal((20_000_000, "2019-01-01"), (40_000_000, "2021-01-01"), (70_000_000, "2023-01-01"))

    def test_between_neighbours(self):
        e = store_age.estimate_created(30_000_000, self.C)
        self.assertEqual(e["method"], "interpolated")
        self.assertLessEqual(abs((e["created"] - date(2020, 1, 1)).days), 1)   # halfway between 2019-01-01 and 2021-01-01
        self.assertEqual((e["lower"][0], e["upper"][0]), (20_000_000, 40_000_000))
        e = store_age.estimate_created(55_000_000, self.C)
        self.assertLessEqual(abs((e["created"] - date(2022, 1, 1)).days), 1)   # halfway on the 2021-2023 segment

    def test_extrapolation_uses_the_end_segment_slope(self):
        e = store_age.estimate_created(85_000_000, self.C)   # 15M above the top; 30M ids = 730 days on the top segment
        self.assertEqual(e["method"], "extrapolated (above range)")
        self.assertLessEqual(abs((e["created"] - date(2024, 1, 1)).days), 1)
        e = store_age.estimate_created(10_000_000, self.C)   # 10M below the bottom; 20M ids = 731 days on the bottom segment
        self.assertEqual(e["method"], "extrapolated (below range)")
        self.assertLessEqual(abs((e["created"] - date(2018, 1, 1)).days), 1)

    def test_exact_and_degenerate(self):
        self.assertEqual(store_age.estimate_created(40_000_000, self.C)["method"], "exact")
        e = store_age.estimate_created(1, cal((5, "2020-01-01")))
        self.assertEqual((e["created"], e["method"]), (None, "calibration needs >= 2 rows"))
        self.assertEqual(store_age.estimate_created(None, self.C)["method"], "no shop id")

    def test_calibration_file_parsing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["shop_id", "created_date", "store_domain", "notes"])
            w.writerow(["70000000", "2023-01-01", "a.com", "Koala"])
            w.writerow(["", "", "", ""])
            w.writerow(["20000000", "2019-01-01T00:00:00Z", "b.com", ""])
            w.writerow(["bad", "2020-01-01", "", ""])
            path = Path(f.name)
        c = store_age.load_calibration(path)
        self.assertEqual(c.points, [(20000000, date(2019, 1, 1)), (70000000, date(2023, 1, 1))])
        self.assertTrue(c.ok)
        self.assertFalse(store_age.load_calibration(Path("/nonexistent/x.csv")).ok)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.a = db.upsert_store(self.conn, "a.com")
        self.b = db.upsert_store(self.conn, "b.com")
        self.c = db.upsert_store(self.conn, "c.com")
        self.conn.commit()

    def test_identity_fetched_once_and_missing_is_retried(self):
        calls = []

        def fake(domain, session=None):
            calls.append(domain)
            if domain == "a.com":
                return {"shop_id": 61234567, "myshopify": "a.myshopify.com", "source": "digital-wallet meta", "http": 200, "error": None}
            return {"shop_id": None, "myshopify": None, "source": None, "http": 200, "error": "no shop id in HTML"}
        store_age.ensure_identity(self.conn, self.a, "a.com", fetch=fake)
        store_age.ensure_identity(self.conn, self.a, "a.com", fetch=fake)      # cached: no second fetch
        store_age.ensure_identity(self.conn, self.b, "b.com", fetch=fake)
        store_age.ensure_identity(self.conn, self.b, "b.com", fetch=fake)      # still missing: fetched again
        self.assertEqual(calls, ["a.com", "b.com", "b.com"])
        rows = {r["store_domain"]: dict(r) for r in self.conn.execute("SELECT * FROM stores")}
        self.assertEqual((rows["a.com"]["shop_id"], rows["a.com"]["myshopify"]), (61234567, "a.myshopify.com"))
        self.assertEqual(rows["b.com"]["shop_id_error"], "no shop id in HTML")
        missing = store_age.missing_ids(self.conn, {"a.com", "b.com"})
        self.assertEqual([m["store_domain"] for m in missing], ["b.com"])

    def test_estimates_and_tabs(self):
        self.conn.execute("UPDATE stores SET shop_id = 30000000, myshopify = 'a.myshopify.com' WHERE id = ?", (self.a,))
        self.conn.execute("UPDATE stores SET shop_id = 85000000 WHERE id = ?", (self.c,))
        self.conn.commit()
        C = cal((20_000_000, "2019-01-01"), (40_000_000, "2021-01-01"), (70_000_000, "2023-01-01"))
        counts = store_age.refresh_estimates(self.conn, C)
        self.assertEqual((counts["stores"], counts["estimated"]), (3, 2))
        rows = store_age.store_age_rows(self.conn, "2024-01-11", C)
        self.assertEqual([r[0] for r in rows], ["c.com", "a.com", "b.com"])      # newest first, unknown last
        c_row = rows[0]
        self.assertEqual((c_row[1], c_row[5]), (85000000, "extrapolated (above range)"))
        self.assertIn(c_row[3], ("2023-12-31", "2024-01-01", "2024-01-02"))
        self.assertIn(c_row[4], (9, 10, 11))
        self.assertEqual(rows[2][5], "no shop id")
        # Stores tab carries the four new columns
        srows = {r[0]: r for r in sheets.stores_rows(self.conn, "2024-01-11")}
        self.assertEqual(len(srows["a.com"]), len(sheets.STORES_HEADERS))
        self.assertEqual(srows["a.com"][-4:-2], [30000000, "a.myshopify.com"])
        self.assertIn(srows["a.com"][-2], ("2019-12-31", "2020-01-01", "2020-01-02"))
        self.assertIn(srows["a.com"][-1], (1470, 1471, 1472))
        self.assertEqual(srows["b.com"][-4:], ["", "", "", ""])
        self.assertEqual(len(sheets.STORE_AGE_HEADERS), len(rows[0]))
        self.assertIn("store_age", sheets.TAB_ORDER)
        self.assertEqual(sheets.TAB_NAMES["store_age"], "Store Age")


if __name__ == "__main__":
    unittest.main()
