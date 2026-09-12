"""Google Sheets sync client: row building, chunking, redirect/retry/error handling, the Code.gs header contract."""
import json
import unittest
from pathlib import Path
from unittest import mock

import requests

from earlyscale import config, db, meta_ads, sheets, shopify

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"

FIX = Path(__file__).parent / "fixtures"
D1, D2 = "2026-09-05", "2026-09-06"


def load(name):
    return shopify.normalise_products(json.loads((FIX / name).read_text())["products"])


def _ad(aid, url, low=0, start="2026-08-20"):
    return {"ad_id": aid, "page_id": "p1", "page_name": "Example", "start_date": start, "end_date": None, "is_active": 1, "primary_text": "t",
            "landing_url": url, "landing_domain": "example.com", "low_impressions": low}


def two_day_db():
    conn = db.connect(":memory:")
    sid = db.upsert_store(conn, "example.com", "Example")
    db.write_product_snapshot(conn, sid, D1, load("products_page1.json"))
    db.write_product_snapshot(conn, sid, D2, load("products_day2.json"))
    run_id = db.start_run(conn, D2, 2)
    db.record_store_run(conn, run_id, sid, D2, "ok", None, 3, 3, 1.0)
    dead = db.upsert_store(conn, "dead.com")
    db.record_store_run(conn, run_id, dead, D2, "error", "HTTP 404 (not a Shopify storefront?)", 0, 0, 0.1)
    meta_ads.record_scrape(conn, sid, D2, [_ad(str(i), "https://example.com/products/cloud-hoodie?utm=x") for i in range(4)]
                           + [_ad("9", "https://example.com/pages/story", low=1)], "Example")
    return conn


class RowBuildingTests(unittest.TestCase):
    def setUp(self):
        self.conn = two_day_db()

    def test_stores_rows_cover_every_store_including_failed(self):
        rows = sheets.stores_rows(self.conn)
        self.assertEqual(len(rows), 2)
        H = sheets.STORES_HEADERS
        ok = dict(zip(H, next(r for r in rows if r[0] == "example.com")))
        self.assertEqual((ok["products"], ok["ads_as_of"], ok["last error"], ok["shop_id"], ok["store_age_days"]), (3, D2, "", "", ""))
        dead = dict(zip(H, next(r for r in rows if r[0] == "dead.com")))
        self.assertTrue(dead["last error"].startswith("HTTP 404"))
        self.assertEqual((dead["products"], dead["ads_as_of"]), ("", ""))       # no snapshot -> blanks, not zeros

    def test_winners_rows_one_per_landing_url_with_three_delivering_ads(self):
        rows = sheets.winners_rows(self.conn)
        self.assertEqual(len(rows), 1)
        r = dict(zip(sheets.WINNERS_HEADERS, rows[0]))
        self.assertEqual((r["store"], r["landing_url"], r["delivering"], r["delivering_wow"], r["pages"], r["top_page"], r["ads_as_of"]),
                         ("example.com", "example.com/products/cloud-hoodie", 4, "", 1, "Example", D2))
        self.assertIsInstance(r["family_age_days"], int)      # created_at from the fixture

    def test_removed_store_drops_out_of_the_sheet(self):
        with mock.patch.object(sheets, "watched_store_ids", lambda c: {2}):
            self.assertEqual([r[0] for r in sheets.stores_rows(self.conn)], ["dead.com"])
            self.assertEqual(sheets.winners_rows(self.conn), [])


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
        session.post.side_effect = [_resp(500, "<html>boom</html>", ctype="text/html"), _resp(200, '{"ok":true,"written":1,"skipped":0}')]
        with mock.patch("earlyscale.sheets.time.sleep") as sleep:
            data = sheets.post_payload(session, self.URL, {"tab": "Stores", "rows": []})
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


def _tabs_session(headers=None, tabs=None):
    """A fake web app: ?tabs=1 answers with counts (+ the deployed headers), POSTs say written."""
    session = mock.Mock()
    session.post.return_value = _resp(200, '{"ok":true,"written":1,"skipped":0}')

    def get(url, params=None, **kw):
        if params and params.get("tabs"):
            body = {"ok": True, "tabs": tabs or {}}
            if headers is not None:
                body["headers"] = headers
            return _resp(200, json.dumps(body))
        if params and params.get("tab"):
            return _resp(200, json.dumps({"ok": True, "tab": params["tab"], "rows": []}))
        return _resp(404, "nope")
    session.get.side_effect = get
    return session


class SyncTests(unittest.TestCase):
    TABS = ["Winners", "Stores"]

    def test_sync_posts_every_tab_in_replace_mode(self):
        conn = two_day_db()
        session = _tabs_session(headers=sheets.expected_headers())
        out = sheets.sync(conn, "https://x/exec", session=session)
        payloads = [json.loads(c.kwargs["data"]) for c in session.post.call_args_list]
        self.assertEqual([p["tab"] for p in payloads], self.TABS)
        self.assertEqual([p["mode"] for p in payloads], ["replace"] * 2)
        self.assertEqual([(p["chunk"], p["chunks"]) for p in payloads], [(1, 1)] * 2)
        self.assertEqual([x["tab"] for x in out], self.TABS)

    def test_stale_code_gs_refuses_to_write(self):
        """A deployment with different columns would put every value under the wrong header: refuse, say what to do."""
        conn = two_day_db()
        stale = dict(sheets.expected_headers(), Winners=sheets.WINNERS_HEADERS[:8])
        session = _tabs_session(headers=stale)
        with self.assertRaises(sheets.SheetsSyncError) as cm:
            sheets.sync(conn, "https://x/exec", session=session)
        self.assertIn("Winners", str(cm.exception))
        self.assertIn("Deploy", str(cm.exception))
        session.post.assert_not_called()

    def test_old_deployment_without_headers_only_warns(self):
        conn = two_day_db()
        session = _tabs_session(headers=None)
        with self.assertLogs("earlyscale.sheets", level="WARNING") as logs:
            sheets.sync(conn, "https://x/exec", session=session)
        self.assertTrue(any("does not report its headers" in x for x in logs.output))
        self.assertEqual(session.post.call_count, 2)

    def test_dry_run_sends_nothing_and_missing_url_is_clear(self):
        conn = two_day_db()
        session = mock.Mock()
        sheets.sync(conn, "", session=session, dry_run=True)
        session.post.assert_not_called()
        with self.assertRaises(sheets.SheetsSyncError) as cm:
            sheets.sync(conn, "", session=session)
        self.assertIn("SHEETS_WEBHOOK_URL", str(cm.exception))

    def test_second_sync_at_the_same_time_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "sync.lock"
            with sheets.SyncLock(lock):
                with self.assertRaises(sheets.SheetsSyncError) as cm:
                    sheets.SyncLock(lock).__enter__()
                self.assertIn("another sync-sheets", str(cm.exception))
            self.assertFalse(lock.exists())


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


class VerifyTests(unittest.TestCase):
    """verify() reads counts back from the web app's doGet and compares with the DB."""

    def _session(self, tabs_json):
        session = mock.Mock()

        def get(url, params=None, **kw):
            if params and params.get("tabs"):
                return _resp(200, json.dumps({"ok": True, "tabs": tabs_json}))
            return _resp(404, "nope")
        session.get.side_effect = get
        return session

    def test_match_and_mismatch(self):
        conn = two_day_db()
        plan = {p["tab"]: len(p["rows"]) for p in sheets.build_plan(conn)}
        v = sheets.verify(conn, "https://x/exec", session=self._session(plan))
        self.assertEqual(v["problems"], [])
        self.assertTrue(all(t["ok"] for t in v["tabs"]))
        short = dict(plan, Winners=plan["Winners"] + 5)
        v = sheets.verify(conn, "https://x/exec", session=self._session(short))
        self.assertEqual([t["tab"] for t in v["tabs"] if not t["ok"]], ["Winners"])
        self.assertTrue(any("Winners:" in x for x in v["problems"]))

    def test_old_deployment_without_doget_json_is_explained(self):
        conn = two_day_db()
        session = mock.Mock()
        session.get.return_value = _resp(200, "ok", ctype="text/plain")
        with self.assertRaises(sheets.SheetsSyncError) as cm:
            sheets.verify(conn, "https://x/exec", session=session)
        self.assertIn("re-paste sheets/Code.gs", str(cm.exception))


class CodeGsContractTests(unittest.TestCase):
    """sheets/Code.gs must carry exactly the headers Python sends, with text-column indexes inside the row."""

    def test_headers_match_and_textcols_in_range(self):
        import re
        src = (Path(__file__).resolve().parent.parent / "sheets" / "Code.gs").read_text(encoding="utf-8")
        expected = sheets.expected_headers()
        self.assertEqual(set(re.findall(r"^  (\w+): \{", src, re.M)), set(expected))     # no extra tabs in Code.gs either
        for name, headers in expected.items():
            m = re.search(r'  %s: \{\n    headers: (\[.*?\]),.*?textCols: (\[[^\]]*\])' % re.escape(name), src, re.S)
            self.assertIsNotNone(m, name)
            self.assertEqual(json.loads(m.group(1)), headers, f"{name} headers differ between Code.gs and sheets.py")
            for i in json.loads(m.group(2)):
                self.assertLess(i, len(headers), f"{name} textCols index {i} outside its {len(headers)} columns")


class ReadBackTests(unittest.TestCase):
    def test_redirect_loop_is_reported(self):
        session = mock.Mock()
        r = _resp(302, "")
        r.headers["Location"] = "https://script.googleusercontent.com/macros/echo?x=1"
        session.get.return_value = r
        with self.assertRaises(sheets.SheetsSyncError) as cm:
            sheets._get_json_once(session, "https://x/exec", {"tabs": "1"})
        self.assertIn("kept redirecting", str(cm.exception))
        self.assertIn("script.googleusercontent.com", str(cm.exception))

    def test_login_redirect_is_explained(self):
        session = mock.Mock()
        r = _resp(302, "")
        r.headers["Location"] = "https://accounts.google.com/ServiceLogin?continue=x"
        session.get.return_value = r
        with self.assertRaises(sheets.SheetsSyncError) as cm:
            sheets.get_json(session, "https://x/exec", {"tabs": "1"})
        self.assertIn("Who has access", str(cm.exception))


class EchoHiccupTests(unittest.TestCase):
    """script.googleusercontent.com occasionally answers the one-time redirect with Google's generic 404 page."""
    PAGE = "<!DOCTYPE html><html lang=\"th\"><head><script>window['ppConfig'] = {productName: 'x'}</script></head></html>"
    EXEC = "https://script.google.com/macros/s/x/exec"
    ECHO = "https://script.googleusercontent.com/macros/echo?user_content_key=k"

    def test_post_retries_a_404_error_page_from_the_echo_host(self):
        session = mock.Mock()
        session.post.return_value = _resp(302, "", location=self.ECHO)
        session.get.side_effect = [_resp(404, self.PAGE, url=self.ECHO, ctype="text/html"),
                                   _resp(200, json.dumps({"ok": True, "written": 1, "skipped": 0}), url=self.ECHO)]
        with mock.patch("earlyscale.sheets.time.sleep") as sleep:
            data = sheets.post_payload(session, self.EXEC, {"tab": "Winners", "rows": []})
        self.assertEqual((data["written"], session.post.call_count), (1, 2))
        self.assertTrue(sleep.called)

    def test_a_404_on_the_exec_url_itself_is_not_retried(self):
        session = mock.Mock()
        session.post.return_value = _resp(404, "nope", ctype="text/html")
        with self.assertRaises(sheets.SheetsSyncError):
            sheets.post_payload(session, self.EXEC, {"tab": "Winners", "rows": []})
        self.assertEqual(session.post.call_count, 1)

    def test_get_retries_the_echo_error_page(self):
        session = mock.Mock()
        session.get.side_effect = [_resp(302, "", location=self.ECHO), _resp(404, self.PAGE, url=self.ECHO, ctype="text/html"),
                                   _resp(302, "", location=self.ECHO + "2"),
                                   _resp(200, json.dumps({"ok": True, "tabs": {"Winners": 1}}), url=self.ECHO + "2")]
        with mock.patch("earlyscale.sheets.time.sleep"):
            self.assertEqual(sheets.get_json(session, self.EXEC, {"tabs": "1"})["tabs"]["Winners"], 1)

    def test_post_retries_when_the_health_check_text_comes_back(self):
        session = mock.Mock()
        session.post.return_value = _resp(302, "", location=self.ECHO)
        session.get.side_effect = [_resp(200, "ok", url=self.ECHO, ctype="text/plain"),
                                   _resp(200, json.dumps({"ok": True, "written": 3, "skipped": 0}), url=self.ECHO)]
        with mock.patch("earlyscale.sheets.time.sleep"):
            with self.assertLogs("earlyscale.sheets", level="WARNING") as logs:
                data = sheets.post_payload(session, self.EXEC, {"tab": "Winners", "rows": []})
        self.assertEqual((data["written"], session.post.call_count), (3, 2))
        self.assertIn("health-check", logs.output[0])

    def test_a_lost_reply_does_not_resend_a_chunk_the_script_already_wrote(self):
        session = mock.Mock()
        session.post.return_value = _resp(302, "", location=self.ECHO)
        session.get.return_value = _resp(404, self.PAGE, url=self.ECHO, ctype="text/html")
        with mock.patch("earlyscale.sheets.time.sleep"):
            data = sheets.post_payload(session, self.EXEC, {"tab": "Winners", "mode": "replace", "chunk": 2, "rows": [[1], [2], [3]]},
                                       already_applied=lambda: True)
        self.assertEqual((data["written"], data.get("recovered"), session.post.call_count), (3, True, 1))

    def test_sync_checks_the_tab_count_before_resending_a_replace_chunk(self):
        conn = two_day_db()
        state = {"posts": 0, "count": 0}
        session = mock.Mock()

        def post(url, data=None, **kw):
            body = json.loads(data)
            state["posts"] += 1
            state["count"] = len(body["rows"])          # the script writes the rows ...
            return _resp(302, "", location=self.ECHO)    # ... but the reply is a redirect that will fail

        def get(url, params=None, **kw):
            if params and params.get("tabs"):
                return _resp(200, json.dumps({"ok": True, "tabs": {"Stores": state["count"]}, "headers": {}}))
            return _resp(404, self.PAGE, url=self.ECHO, ctype="text/html")
        session.post.side_effect, session.get.side_effect = post, get
        with mock.patch("earlyscale.sheets.time.sleep"):
            out = sheets.sync(conn, self.EXEC, tabs=["stores"], session=session, check_headers=False)
        self.assertEqual((state["posts"], out[0]["written"]), (1, 2))

    def test_an_unreachable_web_app_is_a_sync_error_not_a_traceback(self):
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError("Failed to resolve 'script.google.com'")
        with mock.patch("earlyscale.sheets.time.sleep"):
            with self.assertRaises(sheets.SheetsSyncError) as cm:
                sheets.get_json(session, self.EXEC, {"tabs": "1"})
        self.assertIn("cannot reach", str(cm.exception))
        self.assertEqual(session.get.call_count, 3)

    def test_post_re_posts_when_the_echo_host_keeps_redirecting(self):
        session = mock.Mock()
        session.post.return_value = _resp(302, "", location=self.ECHO)
        loop = _resp(302, "", url=self.ECHO, location=self.ECHO)
        session.get.side_effect = [loop] * 10 + [_resp(200, json.dumps({"ok": True, "written": 1, "skipped": 0}), url=self.ECHO)]
        with mock.patch("earlyscale.sheets.time.sleep"):
            with self.assertLogs("earlyscale.sheets", level="WARNING") as logs:
                data = sheets.post_payload(session, self.EXEC, {"tab": "Winners", "rows": []})
        self.assertEqual((data["written"], session.post.call_count), (1, 2))
        self.assertIn("kept redirecting", logs.output[0])


if __name__ == "__main__":
    unittest.main()
