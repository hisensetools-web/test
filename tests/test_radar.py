"""Radar: query building, domain normalisation, lander following, triage decision, imports, the run, the tab."""
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from earlyscale import config, db, radar, sheets

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"
TODAY = "2026-09-13"   # a Sunday


def d(n):
    return (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()


def prod(pid, handle, title, days, pos=None):
    ts = d(days) + "T00:00:00Z"
    return {"product_id": pid, "handle": handle, "title": title, "vendor": "Acme", "product_type": None, "tags": [],
            "created_at": ts, "published_at": ts, "updated_at": ts, "variant_count": 1, "sold_out_variants": 0,
            "min_price": 10, "max_price": 10, "collection_position": pos, "variants": []}


class PureTests(unittest.TestCase):
    def test_distinctive_words_strips_brand_and_noise(self):
        bw = radar.brand_words_for("biorootlabs.com", "BioRoot Labs")
        self.assertEqual(radar.distinctive_words("BioRoot Ceylon Cinnamon Softgels 7200mg with MCT Oil - 60 Capsules", bw), "ceylon cinnamon mct")
        self.assertEqual(radar.distinctive_words("Premium Formula", bw), "")

    def test_junk_landing_domains_are_dropped(self):
        for junk in ("https://fonts.googleapis.com/css", "salest.php", "https://stcdn.leadconnectorhq.com/x", "image.jpg", "https://d1abc.cloudfront.net/p"):
            self.assertIsNone(radar.normalise_landing_domain(junk), junk)
        self.assertEqual(radar.normalise_landing_domain("https://learn.algo-retail.com/x"), "learn.algo-retail.com")
        self.assertEqual(radar.normalise_landing_domain("http://127.0.0.1:8282/products/x"), "http://127.0.0.1:8282")

    def test_landing_domain(self):
        self.assertEqual(radar.normalise_landing_domain("https://www.Shop.com/products/x?utm=1"), "shop.com")
        self.assertEqual(radar.normalise_landing_domain("shop.com"), "shop.com")
        self.assertIsNone(radar.normalise_landing_domain("https://www.facebook.com/x"))
        self.assertIsNone(radar.normalise_landing_domain("https://l.instagram.com/?u=x"))
        self.assertIsNone(radar.normalise_landing_domain("not a url"))

    def test_outbound_shop_domains_prefers_cta_links(self):
        html = '<a href="https://news.example/about">about</a><a href="https://brand.com/products/x?ref=adv">Buy</a>' \
               '<a href="https://brand.com/collections/all">shop</a><a href="https://other.com/">x</a>'
        self.assertEqual(radar.outbound_shop_domains(html, "news.example"), ["brand.com", "other.com"])

    def test_decide(self):
        old_age, old_min = config.RADAR_MAX_AGE_DAYS, config.RADAR_MIN_ACTIVE_ADS
        config.RADAR_MAX_AGE_DAYS, config.RADAR_MIN_ACTIVE_ADS = 180, 10
        try:
            self.assertTrue(radar.decide(90, 10, None))
            self.assertFalse(radar.decide(90, 9, None))
            self.assertFalse(radar.decide(400, 50, None))
            self.assertTrue(radar.decide(400, 50, "hot-handle"))
            self.assertFalse(radar.decide(None, 50, None))
        finally:
            config.RADAR_MAX_AGE_DAYS, config.RADAR_MIN_ACTIVE_ADS = old_age, old_min

    def test_parse_import_text(self):
        self.assertEqual(radar.parse_import_text("brand.com\nhttps://www.two.com/products/x\n# c\n\n"), ["brand.com", "two.com"])
        csv_text = "Brand,Website,Ads\nA,https://a.com/,12\nB,b.com,3\n"
        self.assertEqual(radar.parse_import_text(csv_text), ["a.com", "b.com"])
        self.assertEqual(radar.parse_import_text("name,notes\nx,y\n"), [])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wl = self.tmp / "watchlist.csv"
        self.wl.write_text("store_domain,meta_page_name,meta_page_id,notes\nexisting.com,,,\n", encoding="utf-8")
        self.conn = db.connect(":memory:")
        sid = db.upsert_store(self.conn, "existing.com")
        db.write_product_snapshot(self.conn, sid, d(1), [prod(1, "hero-cream", "Acme Retinol Night Cream", 100, 0), prod(2, "old", "Old Thing", 400, 1)])
        self.conn.commit()
        # the fake Ad Library: ads per query
        self.ads = {
            "I'm a doctor": [self._ad("h1", "Young Page", "https://young.com/products/glow-serum", "doctor text " * 5),
                             self._ad("h2", "Young Page", "https://young.com/products/glow-serum", "x"),
                             self._ad("h3", "News Site", "https://news.example/story-1", "advertorial")],
            "retinol night cream": [self._ad("c1", "Copy Cat", "https://oldstore.com/products/retinol", "copy")],
            "young.com": [self._ad(f"y{i}", "Young Page" if i < 8 else "Second Page", "https://young.com/products/glow-serum", "t") for i in range(12)],
            "brand.com": [self._ad(f"b{i}", "Brand Page", "https://brand.com/products/x", "t") for i in range(15)],
            "oldstore.com": [self._ad(f"o{i}", "Old Page", "https://oldstore.com/products/retinol", "t") for i in range(20)],
            "news.example": [],
        }
        self.searched = []
        self.checked = []

    def _ad(self, aid, page, url, text):
        return {"ad_id": aid, "page_id": page.lower().replace(" ", ""), "page_name": page, "landing_url": url,
                "landing_domain": url.split("/")[2], "primary_text": text, "start_date": d(3), "is_active": True}

    PAGE_OF = {"youngpage": "young.com", "secondpage": "young.com", "newssite": "brand.com", "oldpage": "oldstore.com"}

    def search(self, browser, q, max_ads=None, search_type="keyword_unordered", page_id=None):
        key = self.PAGE_OF.get(page_id, page_id) if page_id else q
        self.searched.append(key)
        return list(self.ads.get(key, []))

    def check(self, domain, session=None):
        self.checked.append(domain)
        if domain == "young.com":
            return True, [prod(10, "glow-serum", "Glow Serum", 20, 0)]
        if domain == "brand.com":
            return True, [prod(20, "x", "X", 300, 0)]
        if domain == "oldstore.com":
            return True, [prod(30, "retinol", "Retinol", 500, 0), prod(31, "fresh", "Fresh", 10, 1)]
        return False, []

    def identity(self, domain, session=None):
        return {"shop_id": None, "myshopify": None, "source": None, "http": 200, "error": "no shop id in HTML"}

    def test_full_run_promotes_parks_and_follows_landers(self):
        old = radar.HOOKS_PATH
        hooks = self.tmp / "hooks.txt"
        hooks.write_text("# seed\nI'm a doctor\n", encoding="utf-8")
        radar.HOOKS_PATH = hooks
        fetches = []

        def fetch(session, url):
            fetches.append(url)
            return url, '<a href="https://brand.com/products/x">Get it</a>', 200
        try:
            out = radar.run_radar(self.conn, None, TODAY, do_sweep=True, search=self.search, check=self.check, identity=self.identity,
                                  web_search=lambda q, s: ["oldstore.com"], watchlist_path=self.wl, wait=lambda: None, fetch=fetch, workers=2)
        finally:
            radar.HOOKS_PATH = old
        self.assertEqual(out["sweep"]["queries"], 1)
        self.assertIn("retinol night cream", self.searched)                       # copycat query from the hero product (brand word stripped)
        rows = {r["domain"]: dict(r) for r in self.conn.execute("SELECT * FROM radar_domains")}
        # young.com: 20-day-old store (first product), 2 sweep ads -> eligible -> searched by its page -> 12 more ads -> promoted
        self.assertEqual((rows["young.com"]["status"], rows["young.com"]["type"], rows["young.com"]["active_ads"]), ("promoted", "shopify", 14))   # 12 from the page search + 2 hook ads
        self.assertEqual((rows["young.com"]["pages"], rows["young.com"]["ads_in_sweeps"], rows["young.com"]["searched_at"]), (2, 2, TODAY))
        self.assertIn("young.com", self.searched)                                  # page search resolved through the fake's page map
        self.assertNotIn("brand.com", self.searched)                               # old store, no hot product: no search spent on it
        self.assertEqual(rows["young.com"]["store_first_created"], d(20))
        # news.example is a lander whose CTA goes to brand.com (old store, 15 ads, no hot product) -> candidate
        self.assertEqual((rows["news.example"]["type"], rows["news.example"]["lander_domain"], rows["news.example"]["status"], rows["news.example"]["store_domain"]),
                         ("shopify", "news.example", "candidate", "brand.com"))
        self.assertGreaterEqual(fetches and len(fetches), 1)
        # oldstore.com (web search): old store, but 'fresh' published 10 days ago... only 'retinol' has ads -> candidate
        self.assertEqual(rows["oldstore.com"]["status"], "candidate")
        self.assertEqual((out["found"], out["shopify"], out["promoted"], out["parked"], out["discarded"], out["searched"]), (3, 3, 1, 2, 0, 1))
        wl = self.wl.read_text(encoding="utf-8")
        self.assertIn("young.com", wl)
        self.assertIn("radar: hook:I'm a doctor", wl)
        # the tab
        tab = radar.candidates_rows(self.conn)
        self.assertEqual(len(tab[0]), len(radar.CANDIDATES_HEADERS))
        self.assertEqual([r[0] for r in tab][:2], sorted([r[0] for r in tab][:2], key=lambda x: -rows[x]["active_ads"]))
        # promote=Y read back from the sheet -> promoted on the next (daily) re-triage
        sheet_rows = [r[:-1] + ["Y"] if r[0] == "oldstore.com" else r for r in tab]
        self.assertEqual(radar.apply_promote_marks(self.conn, sheet_rows), 1)
        out2 = radar.run_radar(self.conn, None, d(-1), do_sweep=False, search=self.search, check=self.check, identity=self.identity,
                               web_search=None, watchlist_path=self.wl, wait=lambda: None)
        self.assertEqual((out2["promoted_later"], out2["retriaged"], out2["searched"]), (1, 2, 0))   # promote=Y needs no search
        self.assertEqual(self.conn.execute("SELECT status FROM radar_domains WHERE domain = 'oldstore.com'").fetchone()[0], "promoted")
        self.assertIn("oldstore.com", self.wl.read_text(encoding="utf-8"))

    def _funnel_ads(self, n):
        for i in range(n):
            self.conn.execute("""INSERT OR IGNORE INTO radar_ads (ad_id, query, source, page_id, page_name, landing_url, landing_domain, body_len, body_snippet, start_date, first_seen, last_seen, is_active)
                                 VALUES (?, 'q', 'hook:q', 'p', 'Funnel Page', 'https://funnel.example/offer', 'funnel.example', 50, 'offer', ?, ?, ?, 1)""", (f"f{i}", d(2), TODAY, TODAY))
            self.conn.execute("INSERT OR IGNORE INTO radar_ad_hits (ad_id, source, query, landing_domain, seen) VALUES (?, 'hook:q', 'q', 'funnel.example', ?)", (f"f{i}", TODAY))
        self.conn.commit()

    def test_funnel_is_parked_and_tracked(self):
        self._funnel_ads(3)
        rec = radar.triage_domain(self.conn, None, "funnel.example", "hook:q", TODAY, search=self.search, check=self.check, identity=self.identity,
                                  follow=lambda c, dom, s: (None, []), watchlist_path=self.wl)
        self.assertEqual((rec["type"], rec["status"], rec["active_ads"], rec["pages"], rec["top_page"], rec["ads_in_sweeps"]),
                         ("funnel", "candidate", 3, 1, "Funnel Page", 3))
        self.assertEqual(rec["searched_at"], TODAY)                                # 3+ sweep ads: its page was searched
        self.assertNotIn("funnel.example", self.wl.read_text(encoding="utf-8"))

    def test_non_shopify_with_few_ads_is_discarded_then_revived(self):
        self._funnel_ads(1)
        rec = radar.triage_domain(self.conn, None, "funnel.example", "hook:q", TODAY, search=self.search, check=self.check, identity=self.identity,
                                  follow=lambda c, dom, s: (None, []), watchlist_path=self.wl)
        self.assertEqual((rec["status"], rec.get("searched_at")), ("discarded", None))
        self.assertNotIn("funnel.example", [r[0] for r in radar.new_domains(self.conn, self.wl)])
        self._funnel_ads(3)                                                          # a later sweep brings more ads
        self.assertIn("funnel.example", [r[0] for r in radar.new_domains(self.conn, self.wl)])

    def test_sweep_resumes_and_survives_a_locked_database(self):
        old = radar.HOOKS_PATH
        calls = {"n": 0}
        real = radar.record_ads

        def flaky(conn, ads, query, source, today):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(conn, ads, query, source, today)
        radar.record_ads = flaky
        try:
            out = radar.sweep(self.conn, None, [("I'm a doctor", "hook:I'm a doctor"), ("retinol night cream", "hook:retinol night cream")], TODAY,
                              search=self.search, wait=lambda: None)
        finally:
            radar.record_ads = real
            radar.HOOKS_PATH = old
        self.assertEqual((out["queries"], out["ads"]), (2, 1))                       # first query lost its ads but the sweep went on
        notes = {r[0]: r[1] for r in self.conn.execute("SELECT query, note FROM radar_runs")}
        self.assertIn("db error", notes["I'm a doctor"])
        self.assertEqual(notes["retinol night cream"], "")
        # a re-run within RADAR_RESWEEP_DAYS skips the phrase that succeeded and repeats the one that failed
        self.searched.clear()
        out2 = radar.sweep(self.conn, None, [("I'm a doctor", "hook:I'm a doctor"), ("retinol night cream", "hook:retinol night cream")], TODAY,
                           search=self.search, wait=lambda: None, skip_days=6)
        self.assertEqual((out2["skipped"], self.searched), (1, ["I'm a doctor"]))

    def test_every_phrase_gets_credit_for_a_shared_ad(self):
        radar.record_ads(self.conn, self.ads["I'm a doctor"], "after 60", "hook:after 60", TODAY)
        radar.record_ads(self.conn, self.ads["I'm a doctor"], "over 60", "hook:over 60", TODAY)
        old = radar.HOOKS_PATH
        hooks = self.tmp / "hooks.txt"
        hooks.write_text("after 60\nover 60\n", encoding="utf-8")
        radar.HOOKS_PATH = hooks
        try:
            y = {r["hook"]: r for r in radar.hook_yield(self.conn)}
        finally:
            radar.HOOKS_PATH = old
        self.assertEqual((y["after 60"]["ads"], y["over 60"]["ads"], y["over 60"]["domains"]), (3, 3, 2))

    def test_manual_import_folder(self):
        folder = self.tmp / "imports"
        folder.mkdir()
        (folder / "a.txt").write_text("manual-one.com\nhttps://www.manual-two.com/products/x\n", encoding="utf-8")
        (folder / "hookd.csv").write_text("Brand,Website\nX,https://manual-three.com/\n", encoding="utf-8")
        out = radar.import_folder(self.conn, TODAY, folder, watchlist_path=self.wl)
        self.assertEqual((out["files"], sorted(out["added"])), (2, ["manual-one.com", "manual-three.com", "manual-two.com"]))
        self.assertTrue((folder / "done" / f"{TODAY}_a.txt").exists())
        wl = self.wl.read_text(encoding="utf-8")
        self.assertIn("manual-two.com", wl)
        self.assertIn("radar: manual:a.txt", wl)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM radar_domains WHERE status = 'watchlist'").fetchone()[0], 3)
        self.assertEqual(radar.new_domains(self.conn, self.wl), [])


if __name__ == "__main__":
    unittest.main()
