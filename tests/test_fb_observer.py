"""The browser observer's extraction code, executed for real in Chromium against a fake feed.

tools/fb_observer/content.js only reads the DOM of a page the user is already viewing. This test
loads that exact file into a page whose markup mirrors a Facebook feed (Sponsored ads marked the
two ways Facebook marks them, an organic post, a timestamp link whose href only appears on hover,
an l.facebook.com redirect for the landing URL) and asserts what it captures.
"""
import json
import unittest
from pathlib import Path

from earlyscale import config, fb_posts

CONTENT_JS = Path(__file__).resolve().parent.parent / "tools" / "fb_observer" / "content.js"
FIXTURE = Path(__file__).parent / "fixtures" / "fb_feed.html"

STUB = """window.__caps = []; window.chrome = {runtime: {sendMessage: function (m) { window.__caps.push(m); }}};"""


def browser_or_skip():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:                       # pragma: no cover - environment without playwright
        raise unittest.SkipTest(f"playwright not installed: {e}")
    from earlyscale import meta_ads
    pw = sync_playwright().start()
    try:
        return pw, pw.chromium.launch(headless=True, **meta_ads.launch_kwargs())
    except Exception as e:                         # pragma: no cover - no browser binary
        pw.stop()
        raise unittest.SkipTest(f"chromium not available: {e}")


class ObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw, cls.browser = browser_or_skip()
        cls.page = cls.browser.new_page()
        cls.page.set_content(FIXTURE.read_text(encoding="utf-8"))
        cls.page.evaluate(STUB)
        cls.page.add_script_tag(path=str(CONTENT_JS))
        cls.page.wait_for_function("() => window.__caps.length >= 2", timeout=10000)
        cls.caps = [m["capture"] for m in cls.page.evaluate("() => window.__caps")]

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def by_page(self, name):
        return next(c for c in self.caps if (c.get("page_name") or "") == name)

    def test_only_sponsored_posts_are_captured(self):
        self.assertEqual(len(self.caps), 2)
        self.assertNotIn("Some Friend", [c.get("page_name") for c in self.caps])

    def test_ad_with_data_ad_preview(self):
        c = self.by_page("BioRoot Labs")
        self.assertEqual(c["page_id"], "777000111222")
        self.assertEqual(c["permalink"], "https://www.facebook.com/777000111222/posts/9988776655443322?__cft__[0]=abc&__tn__=x")
        self.assertIn("3,000 year old cinnamon ritual", c["primary_text"])
        self.assertEqual(c["landing_url"], "https://biorootlabs.com/products/ceylon-cinnamon?utm=fb")
        self.assertEqual(c["image_url"], "https://scontent.xx.fbcdn.net/v/t39/creative1.jpg?_nc_cat=1")
        self.assertEqual((c["reactions"], c["comments"], c["shares"]), (1200, 57, 9))

    def test_ad_with_sponsored_label_and_no_permalink(self):
        c = self.by_page("Metabolae")
        self.assertEqual(c["page_id"], "998877665544")
        self.assertIsNone(c["permalink"])
        self.assertTrue(c["post_id"].startswith("feed_"))
        self.assertEqual((c["reactions"], c["comments"], c["shares"]), (340, 12, 2))

    def test_captures_survive_the_parser_end_to_end(self):
        from earlyscale import db
        conn = db.connect(":memory:")
        parsed = fb_posts.parse_captures_text(json.dumps(self.caps))
        self.assertEqual(len(parsed), 2)
        for cap in parsed:
            fb_posts.record_capture(conn, cap, "2026-09-06", source="observer")
        rows = {r["post_id"]: dict(r) for r in conn.execute("SELECT * FROM fb_posts")}
        self.assertEqual(len(rows), 2)
        self.assertIn("9988776655443322", rows)
        self.assertEqual(rows["9988776655443322"]["landing_url"], "https://biorootlabs.com/products/ceylon-cinnamon?utm=fb")
        # tracking params are stripped from the stored permalink
        self.assertEqual(rows["9988776655443322"]["permalink"], "https://www.facebook.com/777000111222/posts/9988776655443322")
        counts = conn.execute("SELECT reactions, comments, shares FROM fb_posts_daily WHERE post_id = '9988776655443322'").fetchone()
        self.assertEqual(tuple(counts), (1200, 57, 9))

    def test_new_post_scrolled_into_the_feed_is_captured_once(self):
        self.page.evaluate("() => window.__appendAd3()")
        self.page.wait_for_function("() => window.__caps.length >= 3", timeout=10000)
        caps = [m["capture"] for m in self.page.evaluate("() => window.__caps")]
        c = next(x for x in caps if x.get("page_name") == "Neurobella")
        self.assertEqual(c["permalink"], "https://www.facebook.com/555444333222/posts/1122334455667788")
        self.assertEqual((c["reactions"], c["comments"]), (88, 4))
        # a re-render of the same feed does not capture it again
        self.page.evaluate("() => { const d = document.createElement('div'); d.textContent = 'x'; document.body.appendChild(d); }")
        self.page.wait_for_timeout(1200)
        self.assertEqual(len(self.page.evaluate("() => window.__caps")), 3)


if __name__ == "__main__":
    unittest.main()
