"""products_daily snapshots, store upserts, and the schema stamp / migration behaviour of db.connect."""
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

    def test_snapshot_writes_id_handle_title_and_dates_only(self):
        n = db.write_product_snapshot(self.conn, self.store_id, "2026-09-06", self.products)
        self.assertEqual(n, 3)
        self.assertEqual(self.count("products_daily", "2026-09-06"), 3)
        row = self.conn.execute("SELECT * FROM products_daily WHERE handle='cloud-hoodie'").fetchone()
        self.assertEqual((row["product_id"], row["title"], row["url_path"]), (101, "Cloud Hoodie", "/products/cloud-hoodie"))
        self.assertTrue(row["created_at"] and row["published_at"])
        self.assertEqual(set(row.keys()), {"snapshot_date", "store_id", "product_id", "handle", "title", "created_at", "published_at",
                                           "updated_at", "url_path", "fetched_at"})
        d, latest = db.latest_products(self.conn, self.store_id)
        self.assertEqual((d, [p["handle"] for p in latest]), ("2026-09-06", ["cloud-hoodie", "no-variants", "ridge-wallet"]))

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


class ConnectSetupTests(unittest.TestCase):
    """The schema check runs once per code version (stamped in user_version) and waits for a busy database."""

    def test_stamp_is_written_and_a_stale_database_is_migrated_again(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            conn = db.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.schema_stamp())
            conn.execute("ALTER TABLE stores DROP COLUMN platform_note")
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
            conn.close()
            conn = db.connect(path)
            self.assertIn("platform_note", {r[1] for r in conn.execute("PRAGMA table_info(stores)")})
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


class MigrationTests(unittest.TestCase):
    """A database from the previous layout (meta_ads / meta_ads_daily, the wide products_daily, a dozen signal tables)
    is carried over: ads and their daily rows are kept, everything else (the Radar tables included) is dropped, url_daily is derived."""

    OLD_SCHEMA = """
    CREATE TABLE stores (id INTEGER PRIMARY KEY, store_domain TEXT NOT NULL UNIQUE, meta_page_name TEXT, meta_page_id TEXT, notes TEXT,
        added_at TEXT NOT NULL, shop_id INTEGER, myshopify TEXT, platform TEXT, sort_informative INTEGER);
    CREATE TABLE products_daily (snapshot_date TEXT NOT NULL, store_id INTEGER NOT NULL, product_id INTEGER NOT NULL, handle TEXT NOT NULL,
        title TEXT, vendor TEXT, product_type TEXT, tags TEXT, created_at TEXT, published_at TEXT, updated_at TEXT, variant_count INTEGER NOT NULL,
        sold_out_variants INTEGER NOT NULL, min_price REAL, max_price REAL, collection_position INTEGER, fetched_at TEXT NOT NULL,
        url_path TEXT, unlisted INTEGER DEFAULT 0, PRIMARY KEY (snapshot_date, store_id, product_id));
    CREATE INDEX idx_products_daily_handle ON products_daily(store_id, handle, snapshot_date);
    CREATE TABLE variants_daily (snapshot_date TEXT, store_id INTEGER, variant_id INTEGER, PRIMARY KEY (snapshot_date, store_id, variant_id));
    CREATE TABLE meta_ads (ad_id TEXT PRIMARY KEY, store_id INTEGER NOT NULL, page_id TEXT, page_name TEXT, ad_start_date TEXT, ad_end_date TEXT,
        first_seen_date TEXT NOT NULL, last_seen_date TEXT NOT NULL, primary_text TEXT, headline TEXT, landing_url TEXT, product_handle TEXT, raw_json TEXT);
    CREATE TABLE meta_ads_daily (snapshot_date TEXT NOT NULL, ad_id TEXT NOT NULL REFERENCES meta_ads(ad_id), store_id INTEGER NOT NULL,
        is_active INTEGER NOT NULL, position INTEGER, eu_total_reach INTEGER, fetched_at TEXT NOT NULL, low_impressions INTEGER, impression_rank INTEGER,
        PRIMARY KEY (snapshot_date, ad_id));
    CREATE TABLE landing_pages (url TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, product_handle TEXT);
    CREATE TABLE rank_checks (snapshot_date TEXT, store_id INTEGER, informative INTEGER, note TEXT);
    CREATE TABLE alerts (id INTEGER PRIMARY KEY, snapshot_date TEXT, store_id INTEGER, rule INTEGER, detail TEXT);
    CREATE TABLE fb_posts (post_id TEXT PRIMARY KEY); CREATE TABLE fb_posts_daily (snapshot_date TEXT, post_id TEXT REFERENCES fb_posts(post_id));
    CREATE TABLE radar_domains (domain TEXT PRIMARY KEY, type TEXT, status TEXT NOT NULL, source TEXT, first_seen TEXT NOT NULL, last_checked TEXT,
        lander_domain TEXT, shop_id INTEGER, store_created_est TEXT, store_first_created TEXT, store_age_days INTEGER, products INTEGER, active_ads INTEGER,
        pages INTEGER, top_page TEXT, hot_new_product TEXT, example_text TEXT, promote_flag TEXT, promoted_at TEXT, note TEXT);
    CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
    INSERT INTO schema_migrations VALUES ('2026-09-10-clear-non-informative-ranks', 'x');
    """

    def test_old_layout_is_carried_over(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            raw = sqlite3.connect(str(path))
            raw.executescript(self.OLD_SCHEMA)
            raw.execute("INSERT INTO stores (id, store_domain, meta_page_name, added_at, shop_id) VALUES (1, 'elivorahealth.com', 'Elivora', 'x', 12345)")
            raw.execute("INSERT INTO products_daily VALUES ('2026-09-10', 1, 7, 'prostate', 'Prostate', 'v', NULL, '[]', '2026-08-01T00:00:00Z', "
                        "'2026-08-01T00:00:00Z', NULL, 1, 0, 9.0, 9.0, 0, 'f', NULL, 0)")
            raw.execute("INSERT INTO products_daily VALUES ('2026-09-10', 1, 8, 'ghost', 'Ghost', 'v', NULL, '[]', NULL, NULL, NULL, 0, 0, NULL, NULL, NULL, 'f', NULL, 1)")
            for aid, url, low in (("1", "https://elivorahealth.com/pages/prostate?utm=1", 0), ("2", "https://elivorahealth.com/pages/prostate", 1),
                                  ("3", "https://elivorahealth.com/pages/prostate", 0), ("4", "https://elivorahealth.com/pages/prostate", 0)):
                raw.execute("INSERT INTO meta_ads (ad_id, store_id, page_id, page_name, ad_start_date, first_seen_date, last_seen_date, primary_text, landing_url, product_handle) "
                            "VALUES (?, 1, 'p1', 'Elivora', '2026-09-01', '2026-09-03', '2026-09-10', 't', ?, 'berberine-copy')", (aid, url))
                for day in ("2026-09-03", "2026-09-10"):
                    raw.execute("INSERT INTO meta_ads_daily (snapshot_date, ad_id, store_id, is_active, position, fetched_at, low_impressions) VALUES (?, ?, 1, 1, 0, 'x', ?)",
                                (day, aid, low if day == "2026-09-10" else None))
            raw.execute("INSERT INTO landing_pages VALUES ('u', 'x', 'h')")
            raw.commit()
            raw.close()
            conn = db.connect(path)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for gone in ("meta_ads", "meta_ads_daily", "variants_daily", "landing_pages", "rank_checks", "alerts", "fb_posts", "fb_posts_daily",
                         "products_daily_old", "radar_domains"):
                self.assertNotIn(gone, tables)
            self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(products_daily)")],
                             ["snapshot_date", "store_id", "product_id", "handle", "title", "created_at", "published_at", "updated_at", "url_path", "fetched_at"])
            self.assertEqual([tuple(r) for r in conn.execute("SELECT handle, url_path FROM products_daily")], [("prostate", "/products/prostate")])   # unlisted row dropped
            ads = {r["ad_id"]: dict(r) for r in conn.execute("SELECT * FROM ads")}
            self.assertEqual((ads["1"]["landing_path"], ads["1"]["first_seen"], ads["1"]["first_scraped"], ads["1"]["last_scraped"]),
                             ("elivorahealth.com/pages/prostate", "2026-09-01", "2026-09-03", "2026-09-10"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ads_daily").fetchone()[0], 8)
            u = {r["snapshot_date"]: dict(r) for r in conn.execute("SELECT * FROM url_daily")}
            self.assertEqual((u["2026-09-10"]["delivering"], u["2026-09-10"]["delivering_7d_ago"]), (3, 0))    # badges unknown a week earlier: 0 delivering then
            self.assertNotIn("2026-09-03", u)                                                                    # no delivering ad that day: no row
            self.assertEqual(conn.execute("SELECT shop_id FROM stores").fetchone()[0], 12345)
            self.assertIn("2026-09-13-six-data-points", {r[0] for r in conn.execute("SELECT name FROM schema_migrations")})
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.schema_stamp())
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            conn.close()
            db.connect(path).close()      # a second open is a no-op


if __name__ == "__main__":
    unittest.main()
