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


class ConnectSetupTests(unittest.TestCase):
    """The schema check runs once per code version (stamped in user_version) and waits for a busy database."""

    def test_stamp_is_written_and_a_stale_database_is_migrated_again(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            conn = db.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.schema_stamp())
            conn.execute("ALTER TABLE rank_checks DROP COLUMN note")
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
            conn.close()
            conn = db.connect(path)
            self.assertIn("note", {r[1] for r in conn.execute("PRAGMA table_info(rank_checks)")})
            conn.close()
            # a matching stamp means no schema work: connect must not write (open a second connection holding the write lock)
            holder = sqlite3.connect(str(path), timeout=1)
            holder.execute("BEGIN IMMEDIATE")
            try:
                conn = db.connect(path)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0], 0)
                conn.close()
            finally:
                holder.rollback()
                holder.close()

    def test_setup_waits_for_a_locked_database(self):
        import sqlite3
        import tempfile
        import threading
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            db.connect(path).close()
            holder = sqlite3.connect(str(path), timeout=1, check_same_thread=False)
            holder.execute("PRAGMA user_version = 0")   # force the schema pass on the next connect ...
            holder.commit()
            holder.execute("BEGIN IMMEDIATE")           # ... and hold the write lock while it runs
            threading.Timer(0.5, lambda: (holder.rollback(), holder.close())).start()
            real_connect = sqlite3.connect
            with mock.patch.object(sqlite3, "connect", lambda p, timeout=60: real_connect(p, timeout=0.05)):
                real_sleep = db.time.sleep
                with mock.patch.object(db.time, "sleep", lambda s: real_sleep(0.3)), self.assertLogs("earlyscale.db", level="WARNING"):
                    conn = db.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.schema_stamp())
            conn.close()
