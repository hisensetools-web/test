"""Google Sheets sync client: row building, chunking, redirect/retry/error handling."""
import json
import unittest
from pathlib import Path
from unittest import mock

import requests

from earlyscale import config, db, sheets, shopify

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"

FIX = Path(__file__).parent / "fixtures"
D1, D2 = "2026-09-05", "2026-09-06"


def load(name):
    return shopify.normalise_products(json.loads((FIX / name).read_text())["products"])


def two_day_db():
    conn = db.connect(":memory:")
    sid = db.upsert_store(conn, "example.com", "Example")
    db.write_product_snapshot(conn, sid, D1, load("products_page1.json"))
    db.write_product_snapshot(conn, sid, D2, load("products_day2.json"))
    run_id = db.start_run(conn, D2, 2)
    db.record_store_run(conn, run_id, sid, D2, "ok", None, 3, 3, 1.0)
    dead = db.upsert_store(conn, "dead.com")
    db.record_store_run(conn, run_id, dead, D2, "error", "HTTP 404 (not a Shopify storefront?)", 0, 0, 0.1)
    conn.execute("INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at) "
                 "VALUES (?,?,?,?,?,?)", (D2, sid, "launch-drop", 1, "test alert", "2026-09-06T06:00:00+00:00"))
    return conn


class RowBuildingTests(unittest.TestCase):
    def setUp(self):
        self.conn = two_day_db()

    def test_stores_rows_cover_every_store_including_failed(self):
        rows = sheets.stores_rows(self.conn)
        self.assertEqual(len(rows), 2)
        ok = next(r for r in rows if r[0] == "example.com")
        self.assertEqual(len(ok), len(sheets.STORES_HEADERS))
        self.assertEqual(ok[1], "Example")
        self.assertEqual(ok[2], "ok")
        self.assertEqual(ok[3], 3)              # products on latest day
        self.assertEqual(ok[7], 0)              # sold-out delta 2 -> 2
        self.assertEqual(ok[8], 2)              # price changes
        self.assertEqual(ok[10], D2)
        dead = next(r for r in rows if r[0] == "dead.com")
        self.assertTrue(dead[2].startswith("error: HTTP 404"))
        self.assertEqual(dead[3], "")           # no snapshot -> blanks, not zeros
        self.assertEqual(rows[0][0], "example.com")  # sorted by score desc

    def test_stores_rows_single_day_blank_deltas(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "one.com")
        db.write_product_snapshot(conn, sid, D2, load("products_page1.json"))
        r = sheets.stores_rows(conn)[0]
        self.assertEqual((r[7], r[8]), ("", ""))
        self.assertEqual(r[2], "never run")

    def test_products_rows_are_latest_snapshot_only(self):
        rows = sheets.products_rows(self.conn)
        self.assertEqual({r[0] for r in rows}, {D2})
        self.assertEqual(len(rows), 3)
        hoodie = next(r for r in rows if r[2] == "cloud-hoodie")
        self.assertEqual(len(hoodie), len(sheets.PRODUCTS_HEADERS))
        self.assertEqual(hoodie[6], 49.0)       # min price on day 2
        self.assertEqual((hoodie[7], hoodie[8]), (1, 3))  # available, total
        self.assertEqual(hoodie[9], "")         # no collection position in fixture
        # as_of an earlier date picks that day's snapshot
        self.assertEqual({r[0] for r in sheets.products_rows(self.conn, as_of=D1)}, {D1})

    def test_alerts_rows(self):
        rows = sheets.alerts_rows(self.conn)
        self.assertEqual(rows, [[D2, "example.com", "launch-drop", 1, "test alert", "2026-09-06T06:00:00+00:00"]])
        self.assertEqual(sheets.alerts_rows(db.connect(":memory:")), [])


class ChunkTests(unittest.TestCase):
    def test_chunks_respect_byte_limit_and_keep_order(self):
        rows = [[f"2026-09-06", f"store{i % 7}.com", f"handle-{i}", "T" * 50, "x", "y", 9.99, 1, 2, i] for i in range(2000)]
        chunks = sheets.chunk_rows(rows, max_bytes=40_000)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(json.dumps(c, separators=(",", ":")).encode()), 40_000 - 200)
        self.assertEqual([r for c in chunks for r in c], rows)

    def test_oversized_row_goes_alone_and_empty_gives_one_chunk(self):
        big = [["x" * 50_000]]
        self.assertEqual(sheets.chunk_rows(big + [["small"]], max_bytes=40_000), [big, [["small"]]])
        self.assertEqual(sheets.chunk_rows([]), [[]])


def _resp(status, body="", url="https://script.google.com/macros/s/x/exec", location=None, ctype="application/json"):
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = body.encode()
    r.encoding = "utf-8"
    r.headers["Content-Type"] = ctype
    if location:
        r.headers["Location"] = location
    return r


class PostTests(unittest.TestCase):
    URL = "https://script.google.com/macros/s/x/exec"

    def test_follows_302_with_get_and_parses_json(self):
        session = mock.Mock()
        session.post.return_value = _resp(302, location="https://script.googleusercontent.com/echo?x=1")
        session.get.return_value = _resp(200, '{"ok":true,"written":5,"skipped":0}')
        data = sheets.post_payload(session, self.URL, {"tab": "Stores", "rows": []})
        self.assertEqual(data["written"], 5)
        self.assertEqual(session.get.call_args.args[0], "https://script.googleusercontent.com/echo?x=1")
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.post.call_args.kwargs["headers"]["Content-Type"], "application/json")

    def test_retries_on_5xx_then_succeeds(self):
        session = mock.Mock()
        session.post.side_effect = [_resp(500, "<html>boom</html>", ctype="text/html"),
                                    _resp(200, '{"ok":true,"written":1,"skipped":0}')]
        with mock.patch("earlyscale.sheets.time.sleep") as sleep:
            data = sheets.post_payload(session, self.URL, {"tab": "Alerts", "rows": []})
        self.assertTrue(data["ok"])
        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once()

    def test_html_response_gives_deployment_hint(self):
        session = mock.Mock()
        session.post.return_value = _resp(200, "<!DOCTYPE html><html>Sign in</html>", ctype="text/html")
        with self.assertRaises(sheets.SheetsSyncError) as cm:
            sheets.post_payload(session, self.URL, {"tab": "Stores", "rows": []})
        self.assertIn("'Anyone'", str(cm.exception))
        self.assertEqual(session.post.call_count, 1)

    def test_apps_script_error_is_surfaced(self):
        session = mock.Mock()
        session.post.return_value = _resp(200, '{"ok":false,"error":"unknown tab: Foo"}')
        with self.assertRaises(sheets.SheetsSyncError) as cm:
            sheets.post_payload(session, self.URL, {"tab": "Foo", "rows": []})
        self.assertIn("unknown tab: Foo", str(cm.exception))

    def test_4xx_is_fatal_not_retried(self):
        session = mock.Mock()
        session.post.return_value = _resp(404, "not found", ctype="text/plain")
        with mock.patch("earlyscale.sheets.time.sleep") as sleep:
            with self.assertRaises(sheets.SheetsSyncError):
                sheets.post_payload(session, self.URL, {"tab": "Stores", "rows": []})
        sleep.assert_not_called()


class SyncTests(unittest.TestCase):
    def test_sync_posts_every_tab_with_replace_then_append(self):
        conn = two_day_db()
        session = mock.Mock()
        session.post.return_value = _resp(200, '{"ok":true,"written":1,"skipped":0}')
        out = sheets.sync(conn, "https://x/exec", session=session)
        payloads = [json.loads(c.kwargs["data"]) for c in session.post.call_args_list]
        self.assertEqual([p["tab"] for p in payloads], ["Signals", "Families", "Categories", "Stores", "Store Age", "Products", "Alerts"])
        self.assertEqual([p["mode"] for p in payloads], ["replace"] * 5 + ["append", "append"])
        self.assertEqual([(p["chunk"], p["chunks"]) for p in payloads], [(1, 1)] * 7)
        self.assertEqual([x["tab"] for x in out], ["Signals", "Families", "Categories", "Stores", "Store Age", "Products", "Alerts"])

    def test_dry_run_sends_nothing_and_missing_url_is_clear(self):
        conn = two_day_db()
        session = mock.Mock()
        sheets.sync(conn, "", session=session, dry_run=True)
        session.post.assert_not_called()
        with self.assertRaises(sheets.SheetsSyncError) as cm:
            sheets.sync(conn, "", session=session)
        self.assertIn("SHEETS_WEBHOOK_URL", str(cm.exception))


class WatchlistRemoveTests(unittest.TestCase):
    def test_remove_from_watchlist(self):
        import tempfile
        from earlyscale.watchlist import append_to_watchlist, read_watchlist, remove_from_watchlist
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.csv"
            for dom in ("a.com", "b.com", "c.com"):
                append_to_watchlist({"store_domain": dom, "meta_page_name": dom.upper()}, p)
            removed, missing = remove_from_watchlist(["B.com", "https://c.com/", "zzz.com"], p)
            self.assertEqual(removed, ["b.com", "c.com"])
            self.assertEqual(missing, ["zzz.com"])
            left = read_watchlist(p)
            self.assertEqual([r["store_domain"] for r in left], ["a.com"])
            self.assertEqual(left[0]["meta_page_name"], "A.COM")  # other columns preserved


if __name__ == "__main__":
    unittest.main()
