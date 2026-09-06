import json
import unittest
from pathlib import Path

from earlyscale import db, shopify

FIX = Path(__file__).parent / "fixtures"


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.store_id = db.upsert_store(self.conn, "example.com", "Example")
        raw = json.loads((FIX / "products_page1.json").read_text())["products"]
        self.products = shopify.normalise_products(raw)

    def count(self, table, date):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE snapshot_date = ?", (date,)).fetchone()[0]

    def test_snapshot_writes_products_and_variants(self):
        n = db.write_product_snapshot(self.conn, self.store_id, "2026-09-06", self.products)
        self.assertEqual(n, 3)
        self.assertEqual(self.count("products_daily", "2026-09-06"), 3)
        self.assertEqual(self.count("variants_daily", "2026-09-06"), 4)
        row = self.conn.execute("SELECT tags, sold_out_variants FROM products_daily WHERE handle='cloud-hoodie'").fetchone()
        self.assertEqual(json.loads(row["tags"]), ["new", "core"])
        self.assertEqual(row["sold_out_variants"], 2)

    def test_history_is_append_only_across_days(self):
        db.write_product_snapshot(self.conn, self.store_id, "2026-09-05", self.products)
        db.write_product_snapshot(self.conn, self.store_id, "2026-09-06", self.products[:1])
        self.assertEqual(self.count("products_daily", "2026-09-05"), 3)
        self.assertEqual(self.count("products_daily", "2026-09-06"), 1)

    def test_same_day_rerun_replaces_only_that_day(self):
        db.write_product_snapshot(self.conn, self.store_id, "2026-09-05", self.products)
        db.write_product_snapshot(self.conn, self.store_id, "2026-09-06", self.products)
        db.write_product_snapshot(self.conn, self.store_id, "2026-09-06", self.products[:2])
        self.assertEqual(self.count("products_daily", "2026-09-05"), 3)
        self.assertEqual(self.count("products_daily", "2026-09-06"), 2)
        self.assertEqual(self.count("variants_daily", "2026-09-06"), 4)

    def test_other_store_same_day_untouched(self):
        other = db.upsert_store(self.conn, "other.com")
        db.write_product_snapshot(self.conn, other, "2026-09-06", self.products)
        db.write_product_snapshot(self.conn, self.store_id, "2026-09-06", self.products[:1])
        rows = self.conn.execute("SELECT store_id, COUNT(*) c FROM products_daily GROUP BY store_id ORDER BY store_id").fetchall()
        self.assertEqual([(r["store_id"], r["c"]) for r in rows], [(self.store_id, 1), (other, 3)])

    def test_upsert_store_keeps_existing_metadata(self):
        sid = db.upsert_store(self.conn, "example.com", "", "", "")
        self.assertEqual(sid, self.store_id)
        row = self.conn.execute("SELECT meta_page_name FROM stores WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["meta_page_name"], "Example")


if __name__ == "__main__":
    unittest.main()
