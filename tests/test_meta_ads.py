"""Meta Ad Library: payload parsing, normalisation, SQLite recording, URL building."""
import json
import unittest
from pathlib import Path

from earlyscale import db, meta_ads

FIX = Path(__file__).parent / "fixtures"


def fixture_ads():
    return [meta_ads.normalise_ad(n) for n in meta_ads.extract_ads(json.loads((FIX / "ad_library_graphql.json").read_text()))]


class ParseTests(unittest.TestCase):
    def test_extracts_every_ad_from_graphql_wrapper(self):
        ads = fixture_ads()
        self.assertEqual([a["ad_id"] for a in ads], ["1001", "1002", "1003"])

    def test_video_ad_fields(self):
        a = fixture_ads()[0]
        self.assertEqual(a["page_name"], "Ceylon Health")
        self.assertEqual(a["page_id"], "55501")
        self.assertEqual(a["start_date"], "2025-08-30")
        self.assertIsNone(a["end_date"])
        self.assertEqual(a["is_active"], 1)
        self.assertEqual(a["creative_type"], "video")
        self.assertEqual(a["headline"], "Ceylon Cinnamon - 50% off today")
        self.assertIn("cinnamon ritual", a["primary_text"])
        self.assertEqual(a["landing_domain"], "ceylonhealth.com")
        self.assertTrue(a["landing_url"].startswith("https://ceylonhealth.com/products/ceylon-cinnamon-capsules-tt"))
        self.assertEqual(a["asset_url"], "https://scontent.xx.fbcdn.net/v/prev1.jpg?_nc_cat=9&oh=abc")
        self.assertEqual(a["eu_total_reach"], 12345)
        self.assertEqual(a["platforms"], "FACEBOOK,INSTAGRAM")
        self.assertEqual(a["cta"], "Shop now")
        self.assertIsNone(a["reactions"])   # not exposed by the Ad Library
        self.assertEqual(len(a["fingerprint"]), 16)

    def test_image_and_carousel(self):
        img, car = fixture_ads()[1], fixture_ads()[2]
        self.assertEqual(img["creative_type"], "image")
        self.assertIsNone(img["headline"])
        self.assertIsNone(img["eu_total_reach"])
        self.assertEqual(img["landing_url"], "https://ceylonhealth.com/pages/cinnamon-story")
        self.assertEqual(car["creative_type"], "carousel")
        self.assertEqual(car["is_active"], 0)
        self.assertEqual(car["end_date"], "2025-09-10")
        self.assertEqual(car["headline"], "Original")             # first card title
        self.assertEqual(car["landing_url"], "https://ceylonhealth.com/products/ceylon-cinnamon-capsules")
        self.assertEqual(car["asset_url"], "https://scontent.xx.fbcdn.net/v/card1.jpg?oh=1")

    def test_fingerprint_is_copy_based_not_asset_based(self):
        n = meta_ads.extract_ads(json.loads((FIX / "ad_library_graphql.json").read_text()))[0]
        a = meta_ads.normalise_ad(n)
        n2 = json.loads(json.dumps(n))
        n2["ad_archive_id"] = "other"
        n2["snapshot"]["videos"][0]["video_preview_image_url"] = "https://scontent.xx.fbcdn.net/v/OTHER_ASSET.jpg"
        n2["snapshot"]["body"]["text"] = n["snapshot"]["body"]["text"].upper()
        self.assertEqual(meta_ads.normalise_ad(n2)["fingerprint"], a["fingerprint"])   # same copy -> same fp
        n2["snapshot"]["body"]["text"] = "completely different copy"
        self.assertNotEqual(meta_ads.normalise_ad(n2)["fingerprint"], a["fingerprint"])

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
        self.assertEqual(a["creative_type"], "unknown")
        self.assertIsNone(a["landing_url"])
        self.assertIsNone(a["start_date"])


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


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "ceylonhealth.com", "Ceylon Health")

    def test_first_day_inserts_everything(self):
        c = meta_ads.record_scrape(self.conn, self.sid, "2026-09-05", fixture_ads(), "Ceylon Health")
        self.assertEqual(c, {"new": 3, "seen": 0, "disappeared": 0, "total": 3})
        rows = self.conn.execute("SELECT ad_id, first_seen_date, last_seen_date FROM meta_ads ORDER BY ad_id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("1001", "2026-09-05", "2026-09-05")] +
                         [(i, "2026-09-05", "2026-09-05") for i in ("1002", "1003")])
        daily = self.conn.execute("SELECT ad_id, is_active, position, eu_total_reach FROM meta_ads_daily ORDER BY ad_id").fetchall()
        self.assertEqual([tuple(r) for r in daily], [("1001", 1, 0, 12345), ("1002", 1, 1, None), ("1003", 0, 2, 800)])

    def test_second_day_marks_disappeared_and_keeps_first_seen(self):
        ads = fixture_ads()
        meta_ads.record_scrape(self.conn, self.sid, "2026-09-05", ads, "q")
        day2 = [a for a in ads if a["ad_id"] != "1002"]      # 1002 vanished
        day2[0]["headline"] = "new headline"
        new_ad = dict(ads[0], ad_id="2001", start_date="2026-09-06")
        c = meta_ads.record_scrape(self.conn, self.sid, "2026-09-06", day2 + [new_ad], "q")
        self.assertEqual(c, {"new": 1, "seen": 2, "disappeared": 1, "total": 3})
        r = self.conn.execute("SELECT first_seen_date, last_seen_date, headline FROM meta_ads WHERE ad_id='1001'").fetchone()
        self.assertEqual(tuple(r), ("2026-09-05", "2026-09-06", "new headline"))
        gone = self.conn.execute("SELECT is_active FROM meta_ads_daily WHERE ad_id='1002' AND snapshot_date='2026-09-06'").fetchone()
        self.assertEqual(gone["is_active"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM meta_ads_daily").fetchone()[0], 3 + 4)
        # re-running the same day is idempotent
        meta_ads.record_scrape(self.conn, self.sid, "2026-09-06", day2 + [new_ad], "q")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM meta_ads_daily").fetchone()[0], 7)

    def test_other_store_untouched(self):
        other = db.upsert_store(self.conn, "other.com")
        meta_ads.record_scrape(self.conn, other, "2026-09-05", [dict(fixture_ads()[0], ad_id="7")], "o")
        meta_ads.record_scrape(self.conn, self.sid, "2026-09-06", fixture_ads(), "q")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM meta_ads_daily WHERE ad_id='7'").fetchone()[0], 1)

    def test_days_running(self):
        self.assertEqual(meta_ads.days_running("2026-08-30", "2026-09-05", "2026-09-06"), 7)
        self.assertEqual(meta_ads.days_running(None, "2026-09-05", "2026-09-06"), 1)
        self.assertIsNone(meta_ads.days_running("bad", "also-bad", "2026-09-06"))


if __name__ == "__main__":
    unittest.main()
