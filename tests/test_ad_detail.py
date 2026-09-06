"""Meta part 2 (A): single-ad page parsing, delivery status, page likes, creative fingerprints, selection, rule 12."""
import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from earlyscale import ad_detail, ad_metrics, config, db

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"
TODAY = "2026-09-06"


def d(days_ago):
    return (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()


def epoch(iso):
    return int((date.fromisoformat(iso) - date(1970, 1, 1)).total_seconds())


def page_html(node, extra_block='{"define":[]}'):
    doc = {"require": [["X", "handle", None, [{"__bbox": {"result": {"data": {"deeplink_ad_archive_result": {"deeplink_ad_archive": node}}}}}]]]}
    return ('<html><body><script type="application/json" data-content-len="9" data-sjs>' + extra_block + '</script>'
            '<script type="application/json" data-sjs>' + json.dumps(doc) + '</script></body></html>')


def node(ad_id="1001", end=TODAY, likes=5000, images=1, videos=0, page_id="777"):
    snap = {"body": {"text": "Ceylon cinnamon softgels"}, "title": "Buy now", "link_url": "https://x.com/products/a",
            "images": [{"original_image_url": f"https://cdn/img{i}.jpg?sig=1"} for i in range(images)],
            "videos": [{"video_hd_url": f"https://cdn/v{i}.mp4", "video_sd_url": f"https://cdn/v{i}s.mp4"} for i in range(videos)],
            "cards": []}
    return {"ad_archive_id": ad_id, "ad_id": None, "start_date": epoch(d(20)), "end_date": epoch(end), "is_active": True,
            "page_id": page_id, "page_name": "BioRoot", "page_like_count": likes, "page_profile_uri": f"https://www.facebook.com/{page_id}/",
            "page_categories": ["Health/beauty"], "snapshot": snap}


class ParseTests(unittest.TestCase):
    def test_parse_single_ad_page(self):
        html = page_html(node(videos=1))
        got = ad_detail.parse_ad_detail_html(html, "1001")
        self.assertEqual(got["ad_id"], "1001")
        self.assertEqual(got["end_date"], TODAY)
        self.assertEqual(got["start_date"], d(20))
        self.assertEqual(got["page_like_count"], 5000)
        self.assertEqual(got["page_profile_id"], "777")
        self.assertEqual(got["page_categories"], ["Health/beauty"])
        self.assertEqual(got["images"], ["https://cdn/img0.jpg?sig=1"])
        self.assertEqual(got["videos"], ["https://cdn/v0.mp4"])
        self.assertIsNone(ad_detail.parse_ad_detail_html(html, "9999"))
        self.assertIsNone(ad_detail.parse_ad_detail_html("<html><body>No ads</body></html>"))

    def test_no_post_id_is_looked_for(self):
        got = ad_detail.parse_ad_detail_html(page_html(node()))
        self.assertNotIn("post_id", got)

    def test_page_categories_as_dicts_and_profile_php(self):
        n = node()
        n["page_categories"] = [{"name": "Vitamins"}, {"name": "Shop"}]
        n["page_profile_uri"] = "https://www.facebook.com/profile.php?id=123456789"
        got = ad_detail.normalise_detail(n)
        self.assertEqual(got["page_categories"], ["Vitamins", "Shop"])
        self.assertEqual(got["page_profile_id"], "123456789")


class NoRecordTests(unittest.TestCase):
    def test_removed_vs_no_record_reason(self):
        self.assertEqual(ad_detail.no_record_reason("<html><body><div>This ad is no longer available.</div></body></html>", "1"), "removed")
        r = ad_detail.no_record_reason('<html><script data-sjs>{"a":1}</script>1001 x</html>', "1001")
        self.assertEqual(r, "no-record:sjs=1,id_in_html=y,bytes=52")

    def test_removed_marks_ad_off(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        conn.execute("INSERT INTO meta_ads (ad_id, store_id, first_seen_date, last_seen_date, ad_end_date) VALUES ('1', ?, ?, ?, ?)", (sid, d(5), d(1), d(3)))
        ad_detail.record_detail(conn, sid, "1", TODAY, None, "removed")
        ad_detail.update_delivery(conn, "1", TODAY)
        r = conn.execute("SELECT delivery_status, switched_off_date FROM meta_ads WHERE ad_id = '1'").fetchone()
        self.assertEqual(tuple(r), ("off", d(3)))


class DeliveryTests(unittest.TestCase):
    def test_on_while_end_date_advances(self):
        r = [(d(2), d(2)), (d(1), d(1)), (TODAY, TODAY)]
        self.assertEqual(ad_detail.delivery_state(r, TODAY)["status"], "on")
        r = [(d(2), d(3)), (d(1), d(2)), (TODAY, d(1))]   # reported a day behind, still moving
        self.assertEqual(ad_detail.delivery_state(r, TODAY)["status"], "on")

    def test_off_when_end_date_stops_for_two_days(self):
        r = [(d(3), d(3)), (d(2), d(2)), (d(1), d(2)), (TODAY, d(2))]
        st = ad_detail.delivery_state(r, TODAY)
        self.assertEqual((st["status"], st["switched_off_date"], st["last_delivered"]), ("off", d(2), d(2)))
        # one stale day is not enough
        r = [(d(2), d(2)), (d(1), d(1)), (TODAY, d(1))]
        self.assertEqual(ad_detail.delivery_state(r, TODAY)["status"], "on")
        # a single reading with an old end_date is off at once
        self.assertEqual(ad_detail.delivery_state([(TODAY, d(5))], TODAY)["status"], "off")
        self.assertIsNone(ad_detail.delivery_state([(TODAY, None)], TODAY)["status"])


class DbTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "biorootlabs.com")
        for aid, start in (("1001", d(20)), ("1002", d(3)), ("1003", d(30))):
            self.conn.execute("""INSERT INTO meta_ads (ad_id, store_id, page_id, page_name, ad_start_date, first_seen_date, last_seen_date,
                                 primary_text, concept_id) VALUES (?,?,?,?,?,?,?,?,?)""",
                              (aid, self.sid, "777", "BioRoot", start, d(1), TODAY, "Ceylon cinnamon softgels", "c1"))
            self.conn.execute("INSERT INTO meta_ads_daily (snapshot_date, ad_id, store_id, is_active, fetched_at) VALUES (?,?,?,1,'x')", (TODAY, aid, self.sid))
        self.conn.commit()

    def test_record_detail_delivery_and_page_likes(self):
        for days_ago, likes in ((2, 5000), (1, 5100), (0, 5300)):
            day = d(days_ago)
            ad_detail.record_detail(self.conn, self.sid, "1001", day, ad_detail.normalise_detail(node(end=day, likes=likes)), "ok")
            ad_detail.record_detail(self.conn, self.sid, "1003", day, ad_detail.normalise_detail(node("1003", end=d(2), likes=likes)), "ok")
            ad_detail.record_detail(self.conn, self.sid, "1002", day, ad_detail.normalise_detail(node("1002", end=day, likes=likes - 10)), "ok", source="list")
            ad_detail.finalize_store(self.conn, self.sid, day)
        rows = {r["ad_id"]: dict(r) for r in self.conn.execute("SELECT * FROM meta_ads")}
        self.assertEqual((rows["1001"]["delivery_status"], rows["1001"]["last_delivered"]), ("on", TODAY))
        self.assertEqual((rows["1003"]["delivery_status"], rows["1003"]["switched_off_date"]), ("off", d(2)))
        self.assertEqual(rows["1001"]["page_profile_id"], "777")
        self.assertEqual(json.loads(rows["1001"]["page_categories"]), ["Health/beauty"])
        # a list reading never overwrites a detail reading of the same day, a detail reading replaces a list one
        ad_detail.record_detail(self.conn, self.sid, "1001", TODAY, ad_detail.normalise_detail(node(likes=1)), "ok", source="list")
        self.assertEqual(self.conn.execute("SELECT page_like_count FROM meta_ad_detail_daily WHERE ad_id = '1001' AND snapshot_date = ?", (TODAY,)).fetchone()[0], 5300)
        ad_detail.record_detail(self.conn, self.sid, "1002", TODAY, ad_detail.normalise_detail(node("1002", likes=5300)), "ok")
        self.assertEqual(self.conn.execute("SELECT source FROM meta_ad_detail_daily WHERE ad_id = '1002' AND snapshot_date = ?", (TODAY,)).fetchone()[0], "detail")
        # page likes: one row per page per day, max over its ads, delta and slope
        pl = self.conn.execute("SELECT * FROM meta_page_likes_daily WHERE snapshot_date = ?", (TODAY,)).fetchone()
        self.assertEqual((pl["page_id"], pl["page_like_count"], pl["likes_delta_1d"], pl["likes_slope_7d"]), ("777", 5300, 200, 150.0))
        # concept survival now comes from delivery: 2 of 3 delivering
        c = self.conn.execute("SELECT ads_delivering, ads_active, survival, survival_source FROM meta_concepts_daily WHERE snapshot_date = ?", (TODAY,)).fetchone()
        self.assertEqual((c["ads_delivering"], c["ads_active"], c["survival"], c["survival_source"]), (2, 3, 0.667, "delivery"))

    def test_rule_12_page_likes_doubling(self):
        for i in range(14, -1, -1):
            day = d(i)
            likes = 10000 + (10 * (14 - i) if i >= 7 else 70 + 40 * (7 - i))   # 10/day then 40/day
            ad_detail.record_detail(self.conn, self.sid, "1001", day, ad_detail.normalise_detail(node(end=day, likes=likes)), "ok")
            ad_detail.record_page_likes(self.conn, self.sid, day)
        found = ad_detail.run_alerts(self.conn, self.sid, TODAY)
        self.assertEqual([f["rule"] for f in found], [12])
        self.assertIn("10.0 -> 40.0", found[0]["detail"])
        self.assertEqual(ad_detail.run_alerts(self.conn, self.sid, TODAY), [])   # weekly dedupe

    def test_fingerprints_and_creative_lineage(self):
        calls = []

        def fake_hash(url, session=None):
            calls.append(url)
            return {"sha256": "H" + url.split("/")[-1].split("?")[0], "bytes": 10, "scope": "full"}
        det = ad_detail.normalise_detail(node("1003"))          # older ad, img0
        fp = ad_detail.fingerprint_creatives(self.conn, "1003", det, fetch=fake_hash)
        self.assertEqual((fp["hashed"], fp["first_hash"]), (1, "Himg0.jpg"))
        det2 = ad_detail.normalise_detail(node("1002"))         # new ad, same image, different text lineage-free
        ad_detail.fingerprint_creatives(self.conn, "1002", det2, fetch=fake_hash)
        ad_detail.fingerprint_creatives(self.conn, "1002", det2, fetch=fake_hash)   # cached, no re-download
        self.assertEqual(len(calls), 2)
        n = ad_detail.creative_lineage(self.conn, self.sid, TODAY)
        self.assertEqual(n, 1)
        r = self.conn.execute("SELECT lineage_of, lineage_via, lineage_similarity FROM meta_ads WHERE ad_id = '1002'").fetchone()
        self.assertEqual(tuple(r), ("1003", "creative", 1.0))
        # a failing download is recorded, not raised
        def bad(url, session=None):
            raise OSError("boom")
        fp = ad_detail.fingerprint_creatives(self.conn, "1001", ad_detail.normalise_detail(node("1001", images=2)), fetch=bad)
        self.assertEqual(fp["failed"], 2)

    def test_selection_priorities_and_cap(self):
        self.conn.execute("INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at, dedupe_key) VALUES (?,?,?,?,?,?,?)",
                          (TODAY, self.sid, "a", 8, "ad 1003 (BioRoot): reach/day 1 -> 2", "x", "8|1003"))
        self.conn.execute("UPDATE meta_ads SET delivery_status = 'off', detail_fetched_date = ? WHERE ad_id = '1001'", (d(1),))
        sel = ad_detail.select_for_detail(self.conn, self.sid, TODAY, cap=10)
        self.assertEqual([x["ad_id"] for x in sel], ["1003", "1002"])   # flagged first, then the new ad; the off ad waits a week
        self.assertEqual(sel[0]["priority"], 0)
        self.assertEqual(ad_detail.flagged_ad_ids(self.conn, self.sid), {"1003"})
        self.conn.execute("UPDATE meta_ads SET lineage_of = '1003' WHERE ad_id = '1002'")
        self.conn.execute("INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at, dedupe_key) VALUES (?,?,?,?,?,?,?)",
                          (TODAY, self.sid, "a", 7, "1 new ad", "x", "7|BioRoot|1003"))
        self.assertEqual(ad_detail.flagged_ad_ids(self.conn, self.sid), {"1003", "1002"})
        self.assertEqual(len(ad_detail.select_for_detail(self.conn, self.sid, TODAY, cap=1)), 1)

    def test_dead_browser_is_relaunched_and_context_recycled(self):
        class FakePage:
            def __init__(self, ctx): self.ctx, self.closed = ctx, False
            def is_closed(self): return self.closed
        class FakeCtx:
            def __init__(self, br): self.br, self.pages = br, []
            def new_page(self):
                p = FakePage(self); self.pages.append(p); return p
            def route(self, *a): pass
            def close(self): pass
        class FakeBrowser:
            def __init__(self): self.connected, self.contexts = True, []
            def is_connected(self): return self.connected
            def new_context(self, **kw):
                c = FakeCtx(self); self.contexts.append(c); return c
        class Handle:
            def __init__(self): self.browsers = [FakeBrowser()]
            def get(self):
                if not self.browsers[-1].connected:
                    self.browsers.append(FakeBrowser())
                return self.browsers[-1]
        handle = Handle()
        seen = []

        def fetch(browser, ad_id, page=None, dismiss=True):
            seen.append((ad_id, dismiss))
            if ad_id == "1002" and len(seen) == 1:      # the browser dies on the first ad
                page.closed = True
                handle.browsers[-1].connected = False
                return None, "error:TargetClosedError"
            return ad_detail.normalise_detail(node(ad_id)), "ok"
        old_pages, old_light = config.META_DETAIL_CONTEXT_PAGES, config.META_DETAIL_LIGHT
        config.META_DETAIL_CONTEXT_PAGES, config.META_DETAIL_LIGHT = 2, False
        try:
            real = ad_detail.fetch_ad_detail
            ad_detail.fetch_ad_detail = fetch      # so fetch_store_details treats the pass as live
            c = ad_detail.fetch_store_details(self.conn, handle, self.sid, TODAY, cap=10, fetch=fetch,
                                              hash_fetch=lambda u, s=None: {"sha256": "h", "bytes": 1, "scope": "full"}, wait=lambda: None)
        finally:
            ad_detail.fetch_ad_detail = real
            config.META_DETAIL_CONTEXT_PAGES, config.META_DETAIL_LIGHT = old_pages, old_light
        self.assertEqual((c["ok"], c["errors"], c["relaunches"]), (3, 0, 1))
        self.assertEqual(len(handle.browsers), 2)                         # relaunched once
        self.assertEqual([a for a, _ in seen], ["1002", "1002", "1001", "1003"])   # retried once after the relaunch
        self.assertGreaterEqual(c["context_recycles"], 1)                 # 2 pages per context

    def test_fetch_store_details_stops_on_login_wall(self):
        seq = iter([(ad_detail.normalise_detail(node("1002")), "ok"), (None, "login-wall"), (ad_detail.normalise_detail(node("1001")), "ok")])
        c = ad_detail.fetch_store_details(self.conn, None, self.sid, TODAY, cap=10, fetch=lambda b, a: next(seq),
                                          hash_fetch=lambda u, s=None: {"sha256": "h", "bytes": 1, "scope": "full"}, wait=lambda: None)
        self.assertEqual((c["ok"], c["login_wall"]), (1, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM meta_ad_detail_daily WHERE source = 'detail'").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
