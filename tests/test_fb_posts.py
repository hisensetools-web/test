"""Meta part 2 (B): captured Sponsored posts, logged-out counts, deltas, join to Ad Library ads."""
import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from earlyscale import config, db, fb_posts

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"
TODAY = "2026-09-06"


def d(days_ago):
    return (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()


class CaptureParseTests(unittest.TestCase):
    def test_post_ids_and_clean_permalink(self):
        cases = {
            "https://www.facebook.com/123456789012/posts/987654321098765?__cft__[0]=abc&__tn__=x": ("987654321098765", "123456789012"),
            "https://m.facebook.com/BioRoot/posts/pfbid02AbCdEfGh123?mibextid=z": ("02AbCdEfGh123", None),
            "https://www.facebook.com/permalink.php?story_fbid=555&id=123456789012": ("555", "123456789012"),
            "https://www.facebook.com/watch/?v=44455566677": ("44455566677", None),
            "https://www.facebook.com/BioRoot/videos/1122334455/": ("1122334455", None),
        }
        for url, (pid, page) in cases.items():
            with self.subTest(url=url):
                cap = fb_posts.parse_capture(url)
                self.assertEqual((cap["post_id"], cap["page_id"]), (pid, page))
        self.assertEqual(fb_posts.clean_permalink("https://m.facebook.com/x/posts/1?fbclid=Q&foo=1"), "https://www.facebook.com/x/posts/1?foo=1")
        with self.assertRaises(ValueError):
            fb_posts.parse_capture("https://www.facebook.com/BioRoot/")

    def test_captures_text_forms(self):
        text = json.dumps({"permalink": "https://www.facebook.com/1/posts/2", "page_name": "BioRoot", "reactions": "1.2K", "comments": 57, "shares": "9"})
        c = fb_posts.parse_captures_text(text)[0]
        self.assertEqual(c["counts"], {"reactions": 1200, "comments": 57, "shares": 9})
        lines = "https://www.facebook.com/1/posts/3\n\n" + json.dumps({"url": "https://www.facebook.com/1/posts/4", "text": "hi"}) + "\nnot a url\n"
        got = fb_posts.parse_captures_text(lines)
        self.assertEqual([g["post_id"] for g in got], ["3", "4"])
        self.assertEqual(fb_posts.parse_captures_text(json.dumps([{"permalink": "https://www.facebook.com/1/posts/5"}]))[0]["post_id"], "5")


class DbTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "biorootlabs.com")
        self.conn.execute("""INSERT INTO meta_ads (ad_id, store_id, page_id, page_name, ad_start_date, first_seen_date, last_seen_date, primary_text, creative_hash, product_handle)
                             VALUES ('1001', ?, '777', 'BioRoot', ?, ?, ?, 'Ceylon cinnamon softgels that actually work for blood sugar support, try them', 'HASH1', 'ceylon')""",
                          (self.sid, d(20), d(1), TODAY))
        self.conn.execute("""INSERT INTO meta_ads (ad_id, store_id, page_id, page_name, ad_start_date, first_seen_date, last_seen_date, primary_text, product_handle)
                             VALUES ('1002', ?, '777', 'BioRoot', ?, ?, ?, 'Completely different oregano oil copy about immunity and winter', 'oregano')""",
                          (self.sid, d(20), d(1), TODAY))
        self.conn.execute("INSERT INTO meta_ads_daily (snapshot_date, ad_id, store_id, is_active, fetched_at) VALUES (?, '1001', ?, 1, 'x')", (TODAY, self.sid))
        self.conn.commit()

    def test_capture_match_by_text_and_by_creative(self):
        cap = fb_posts.parse_capture({"permalink": "https://www.facebook.com/777/posts/900", "page_name": "bioroot",
                                      "text": "Ceylon cinnamon softgels that actually work for blood sugar support, try them today",
                                      "reactions": 100, "comments": 10, "shares": 2})
        self.assertTrue(fb_posts.record_capture(self.conn, cap, TODAY))
        self.assertFalse(fb_posts.record_capture(self.conn, cap, TODAY))   # dedupe on post_id
        self.assertEqual(fb_posts.match_posts(self.conn), 1)
        p = self.conn.execute("SELECT ad_id, match_via, match_score, store_id FROM fb_posts WHERE post_id = '900'").fetchone()
        self.assertEqual((p["ad_id"], p["match_via"], p["store_id"]), ("1001", "text", self.sid))
        self.assertGreaterEqual(p["match_score"], 0.8)
        cap2 = fb_posts.parse_capture({"permalink": "https://www.facebook.com/777/posts/901", "page_id": "777", "text": "unrelated words entirely"})
        cap2["image_hash"] = "HASH1"
        fb_posts.record_capture(self.conn, cap2, TODAY)
        self.assertEqual(fb_posts.match_posts(self.conn), 1)
        self.assertEqual(self.conn.execute("SELECT match_via FROM fb_posts WHERE post_id = '901'").fetchone()[0], "creative")
        # low similarity, no hash -> unmatched
        cap3 = fb_posts.parse_capture({"permalink": "https://www.facebook.com/777/posts/902", "page_id": "777", "text": "something about socks"})
        fb_posts.record_capture(self.conn, cap3, TODAY)
        self.assertEqual(fb_posts.match_posts(self.conn), 0)
        # counts propagate to the matched ad's daily row
        fb_posts.propagate_to_ads(self.conn, TODAY)
        r = self.conn.execute("SELECT reactions, comments, shares, engagement, post_status FROM meta_ads_daily WHERE ad_id = '1001' AND snapshot_date = ?", (TODAY,)).fetchone()
        self.assertEqual(tuple(r), (100, 10, 2, 112, "feed"))
        self.assertEqual(self.conn.execute("SELECT post_id FROM meta_ads WHERE ad_id = '1001'").fetchone()[0], "900")

    def test_refresh_engagement_deltas_and_gating(self):
        for pid in ("900", "901", "902"):
            fb_posts.record_capture(self.conn, fb_posts.parse_capture(f"https://www.facebook.com/777/posts/{pid}"), d(3))
        answers = {"900": ({"reactions": 50, "comments": 5, "shares": 1}, "ok"), "901": ({}, "gated"), "902": ({}, "removed")}
        for days_ago in (3, 2, 1, 0):
            day = d(days_ago)
            answers["900"] = ({"reactions": 50 + 20 * (3 - days_ago), "comments": 5 + 3 * (3 - days_ago), "shares": 1}, "ok")
            c = fb_posts.refresh_engagement(self.conn, None, day, fetch=lambda b, u: answers[u.rsplit("/", 1)[-1]], wait=lambda: None)
            if days_ago == 3:
                self.assertEqual((c["ok"], c["gated"], c["removed"]), (1, 1, 1))
            else:
                self.assertEqual(c["candidates"], 1)   # gated / removed posts are not fetched again
        r = self.conn.execute("SELECT comment_delta_1d, engagement_per_day_7d FROM fb_posts_daily WHERE post_id = '900' AND snapshot_date = ?", (TODAY,)).fetchone()
        self.assertEqual((r["comment_delta_1d"], r["engagement_per_day_7d"]), (3, 23.0))
        st = {r[0]: r[1] for r in self.conn.execute("SELECT post_id, status FROM fb_posts")}
        self.assertEqual(st, {"900": "ok", "901": "gated", "902": "removed"})
        # same day again: nothing due
        c = fb_posts.refresh_engagement(self.conn, None, TODAY, fetch=lambda b, u: (_ for _ in ()).throw(AssertionError("no fetch")), wait=lambda: None)
        self.assertEqual(c["candidates"], 0)


if __name__ == "__main__":
    unittest.main()
