"""Meta Ad Library: payload parsing, normalisation, badge application, SQLite recording, URL building."""
import json
import unittest
from pathlib import Path

from earlyscale import db, meta_ads

FIX = Path(__file__).parent / "fixtures"


def fixture_ads():
    return [meta_ads.normalise_ad(n) for n in meta_ads.extract_ads(json.loads((FIX / "ad_library_graphql.json").read_text()))]


class ParseTests(unittest.TestCase):
    def test_extracts_every_ad_from_graphql_wrapper(self):
        self.assertEqual([a["ad_id"] for a in fixture_ads()], ["1001", "1002", "1003"])

    def test_video_ad_fields(self):
        a = fixture_ads()[0]
        self.assertEqual((a["page_name"], a["page_id"], a["start_date"], a["end_date"], a["is_active"]), ("Ceylon Health", "55501", "2025-08-30", None, 1))
        self.assertIn("cinnamon ritual", a["primary_text"])
        self.assertEqual(a["landing_domain"], "ceylonhealth.com")
        self.assertTrue(a["landing_url"].startswith("https://ceylonhealth.com/products/ceylon-cinnamon-capsules-tt"))
        self.assertIsNone(a["low_impressions"])          # the payload has no badge; the card supplies it

    def test_image_and_carousel(self):
        img, car = fixture_ads()[1], fixture_ads()[2]
        self.assertEqual(img["landing_url"], "https://ceylonhealth.com/pages/cinnamon-story")
        self.assertEqual((car["is_active"], car["end_date"]), (0, "2025-09-10"))
        self.assertEqual(car["landing_url"], "https://ceylonhealth.com/products/ceylon-cinnamon-capsules")   # first card's link

    def test_json_lines_and_for_loop_prefix(self):
        doc = (FIX / "ad_library_graphql.json").read_text().strip()
        text = 'for (;;);' + doc + "\n" + '{"other": 1}\n' + "garbage line\n"
        docs = meta_ads.parse_json_lines(text)
        self.assertEqual(len(docs), 2)
        self.assertEqual(len(meta_ads.extract_ads(docs)), 3)

    def test_html_wrapped_body_and_missing_snapshot_bits(self):
        node = {"ad_archive_id": "9", "snapshot": {"body": {"markup": {"__html": "<p>Hi <b>there</b></p>"}}}}
        a = meta_ads.normalise_ad(node)
        self.assertEqual(a["primary_text"], "Hi there")
        self.assertIsNone(a["landing_url"])
        self.assertIsNone(a["start_date"])

    def test_card_badges_override_nothing_else(self):
        ads = fixture_ads()
        n = meta_ads.apply_card_badges(ads, {"1001": True, "1002": False})
        self.assertEqual((n, ads[0]["low_impressions"], ads[1]["low_impressions"], ads[2]["low_impressions"]), (2, 1, 0, None))

    def test_card_script_keeps_its_regex_escapes(self):
        self.assertIn(r"\d{3,20}", meta_ads.CARD_BADGE_JS)     # a non-raw string would turn \d into a SyntaxWarning + a broken regex
        self.assertIn(r"\s*", meta_ads.CARD_BADGE_JS)


class UrlTests(unittest.TestCase):
    def test_search_urls(self):
        self.assertIn("view_all_page_id=55501", meta_ads.build_search_url(page_id="55501"))
        u = meta_ads.build_search_url("Ceylon Health")
        self.assertIn("q=Ceylon%20Health", u)
        self.assertIn("search_type=page", u)
        self.assertIn("active_status=active", u)
        self.assertIn("country=ALL", u)
        self.assertIn("search_type=keyword_unordered", meta_ads.build_search_url("ceylonhealth.com"))
        with self.assertRaises(ValueError):
            meta_ads.build_search_url()

    def test_store_query_is_always_the_domain(self):
        self.assertEqual(meta_ads.store_query({"store_domain": "x.com", "meta_page_name": "Sarah Bennett"}), "x.com")
        self.assertEqual(meta_ads.store_query({"store_domain": "http://127.0.0.1:8001", "meta_page_name": ""}), "127.0.0.1:8001")


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "ceylonhealth.com", "Ceylon Health")

    def test_first_day_inserts_everything(self):
        ads = fixture_ads()
        meta_ads.apply_card_badges(ads, {"1001": False, "1002": True})
        c = meta_ads.record_scrape(self.conn, self.sid, "2026-09-05", ads, "Ceylon Health")
        self.assertEqual(c, {"new": 3, "seen": 0, "disappeared": 0, "total": 3, "badge_known": 2, "urls": 1})
        rows = self.conn.execute("SELECT ad_id, first_seen, first_scraped, last_scraped, landing_path FROM ads ORDER BY ad_id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("1001", "2025-08-30", "2026-09-05", "2026-09-05", "ceylonhealth.com/products/ceylon-cinnamon-capsules-tt"),
                                                    ("1002", "2025-09-04", "2026-09-05", "2026-09-05", "ceylonhealth.com/pages/cinnamon-story"),
                                                    ("1003", "2025-08-09", "2026-09-05", "2026-09-05", "ceylonhealth.com/products/ceylon-cinnamon-capsules")])
        daily = self.conn.execute("SELECT ad_id, still_active, low_impressions, position FROM ads_daily ORDER BY ad_id").fetchall()
        self.assertEqual([tuple(r) for r in daily], [("1001", 1, 0, 0), ("1002", 1, 1, 1), ("1003", 0, None, 2)])

    def test_second_day_marks_disappeared_and_keeps_first_scraped(self):
        ads = fixture_ads()
        meta_ads.record_scrape(self.conn, self.sid, "2026-09-05", ads, "q")
        day2 = [a for a in ads if a["ad_id"] != "1002"]      # 1002 vanished
        new_ad = dict(ads[0], ad_id="2001", start_date="2026-09-06")
        c = meta_ads.record_scrape(self.conn, self.sid, "2026-09-06", day2 + [new_ad], "q")
        self.assertEqual((c["new"], c["seen"], c["disappeared"], c["total"]), (1, 2, 1, 3))
        r = self.conn.execute("SELECT first_scraped, last_scraped FROM ads WHERE ad_id='1001'").fetchone()
        self.assertEqual(tuple(r), ("2026-09-05", "2026-09-06"))
        gone = self.conn.execute("SELECT still_active FROM ads_daily WHERE ad_id='1002' AND snapshot_date='2026-09-06'").fetchone()
        self.assertEqual(gone["still_active"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ads_daily").fetchone()[0], 3 + 4)
        meta_ads.record_scrape(self.conn, self.sid, "2026-09-06", day2 + [new_ad], "q")      # re-running the same day is idempotent
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ads_daily").fetchone()[0], 7)

    def test_same_day_rerun_keeps_a_badge_read_earlier(self):
        ads = fixture_ads()
        meta_ads.apply_card_badges(ads, {"1001": False})
        meta_ads.record_scrape(self.conn, self.sid, "2026-09-05", ads, "q")
        meta_ads.record_scrape(self.conn, self.sid, "2026-09-05", fixture_ads(), "q")        # second pass: card not rendered
        self.assertEqual(self.conn.execute("SELECT low_impressions FROM ads_daily WHERE ad_id = '1001'").fetchone()[0], 0)

    def test_other_store_untouched(self):
        other = db.upsert_store(self.conn, "other.com")
        meta_ads.record_scrape(self.conn, other, "2026-09-05", [dict(fixture_ads()[0], ad_id="7")], "o")
        meta_ads.record_scrape(self.conn, self.sid, "2026-09-06", fixture_ads(), "q")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ads_daily WHERE ad_id='7'").fetchone()[0], 1)

    def test_days_running(self):
        self.assertEqual(meta_ads.days_running("2026-08-30", "2026-09-05", "2026-09-06"), 7)
        self.assertEqual(meta_ads.days_running(None, "2026-09-05", "2026-09-06"), 1)
        self.assertIsNone(meta_ads.days_running("bad", "also-bad", "2026-09-06"))


if __name__ == "__main__":
    unittest.main()
