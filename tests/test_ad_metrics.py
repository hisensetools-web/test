"""Part B increment 2: landing join, concepts, lineage, daily metrics, alerts, Signals join."""
import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import requests

from earlyscale import ad_metrics, config, db, meta_ads, sheets

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"
FIX = Path(__file__).parent / "fixtures"


class LandingParseTests(unittest.TestCase):
    def test_handle_from_url(self):
        self.assertEqual(ad_metrics.handle_from_url("https://x.com/products/ceylon-cinnamon-tt?utm=1"), ("ceylon-cinnamon-tt", None))
        self.assertEqual(ad_metrics.handle_from_url("https://x.com/collections/all/products/Foo-Bar/"), ("foo-bar", None))
        self.assertEqual(ad_metrics.handle_from_url("https://x.com/pages/av1"), (None, "av1"))
        self.assertEqual(ad_metrics.handle_from_url("https://x.com/"), (None, None))
        self.assertEqual(ad_metrics.handle_from_url(None), (None, None))

    def test_handles_from_html_ranks_buy_buttons_first(self):
        html = ('<a href="/products/other-thing">x</a><a href="/products/hero-google">buy</a>'
                '<form action="/cart/add"><input name="id" value="4400000000001"></form>'
                '<script>var p = {"handle":"hero-google"};</script>')
        ranked = ad_metrics.handles_from_html(html, {4400000000001: "hero"})
        self.assertEqual(ranked[0][0], "hero")            # variant id -> handle, weight 2
        self.assertEqual(ranked[1][0], "hero-google")     # link + json = 2 as well, alphabetical after
        self.assertIn(("other-thing", 1), ranked)

    def test_match_handle_variants(self):
        known = {"ceylon-cinnamon", "ceylon-cinnamon-google", "oregano-oil"}
        self.assertEqual(ad_metrics.match_handle("ceylon-cinnamon", known), "ceylon-cinnamon")
        self.assertEqual(ad_metrics.match_handle("ceylon-cinnamon-google", known), "ceylon-cinnamon-google")
        self.assertEqual(ad_metrics.match_handle("ceylon-cinnamon-google-2", known), "ceylon-cinnamon-google")  # longest prefix
        self.assertEqual(ad_metrics.match_handle("oregano", known), "oregano-oil")                             # known extends it
        self.assertIsNone(ad_metrics.match_handle("zebra", known))
        self.assertIsNone(ad_metrics.match_handle(None, known))

    def test_same_store(self):
        self.assertTrue(ad_metrics.same_store("www.biorootlabs.com", "biorootlabs.com"))
        self.assertTrue(ad_metrics.same_store("shop.biorootlabs.com", "https://biorootlabs.com/"))
        self.assertFalse(ad_metrics.same_store("biorootlabs.co", "biorootlabs.com"))
        self.assertFalse(ad_metrics.same_store(None, "biorootlabs.com"))


class ConceptTests(unittest.TestCase):
    def test_chain_within_one_day_same_page_and_landing(self):
        ads = [
            {"ad_id": "a", "page": "P", "landing": "https://x/products/h", "launch": "2026-09-01"},
            {"ad_id": "b", "page": "P", "landing": "https://x/products/h", "launch": "2026-09-02"},
            {"ad_id": "c", "page": "P", "landing": "https://x/products/h", "launch": "2026-09-03"},  # chains via b
            {"ad_id": "d", "page": "P", "landing": "https://x/products/h", "launch": "2026-09-06"},  # gap -> new concept
            {"ad_id": "e", "page": "Q", "landing": "https://x/products/h", "launch": "2026-09-01"},  # other page
            {"ad_id": "f", "page": "P", "landing": "https://x/pages/av1", "launch": "2026-09-01"},   # other landing
            {"ad_id": "g", "page": "P", "landing": "https://x/products/h", "launch": None},          # no date -> alone
        ]
        c = ad_metrics.cluster_concepts(ads)
        self.assertEqual(c["a"], c["b"])
        self.assertEqual(c["b"], c["c"])
        self.assertNotEqual(c["a"], c["d"])
        self.assertNotEqual(c["a"], c["e"])
        self.assertNotEqual(c["a"], c["f"])
        self.assertEqual(len(set(c.values())), 5)


class LineageTests(unittest.TestCase):
    def test_similarity_and_lineage(self):
        base = "I'm a doctor with chronic insomnia. I refused sleeping pills for 2 years. Then I found this."
        near = "I'm a doctor with chronic insomnia. I refused sleeping pills for 2 years. Then I found magnesium."
        far = "Six words from my doctor cost me years. Let pain be your guide."
        self.assertGreater(ad_metrics.text_similarity(base, near), 0.7)
        self.assertLess(ad_metrics.text_similarity(base, far), 0.2)
        new = [{"ad_id": "n1", "page": "P", "text": near}, {"ad_id": "n2", "page": "P", "text": far},
               {"ad_id": "n3", "page": "Q", "text": near}]
        old = [{"ad_id": "o1", "page": "P", "text": base}]
        lin = ad_metrics.find_lineage(new, old)
        self.assertEqual(set(lin), {"n1"})
        self.assertEqual(lin["n1"][0], "o1")
        self.assertGreater(lin["n1"][1], 0.7)


def _ad(ad_id, page, start, landing, text, headline="H", active=1):
    return {"ad_id": ad_id, "page_id": None, "page_name": page, "start_date": start, "end_date": None,
            "is_active": active, "primary_text": text, "headline": headline, "landing_url": landing,
            "landing_domain": "supp.com" if landing else None, "caption": None, "cta": None,
            "creative_type": "image", "asset_url": None, "platforms": None, "collation_count": 1,
            "eu_total_reach": None, "reactions": None, "comments": None, "shares": None,
            "fingerprint": "f", "raw_json": "{}"}


def _prod(pid, handle, title, pos=None):
    return {"product_id": pid, "handle": handle, "title": title, "vendor": "", "product_type": None, "tags": [],
            "created_at": "2026-08-01T00:00:00Z", "published_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z", "variant_count": 1, "sold_out_variants": 0, "min_price": 39.0,
            "max_price": 39.0, "collection_position": pos,
            "variants": [{"variant_id": pid * 10, "title": "x", "sku": None, "price": 39.0, "compare_at_price": None,
                          "available": True}]}


class ProcessStoreTests(unittest.TestCase):
    TODAY = "2026-09-06"

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "supp.com", "Supp")
        db.write_product_snapshot(self.conn, self.sid, self.TODAY, [
            _prod(1, "ceylon-cinnamon", "Ceylon Cinnamon", pos=0),
            _prod(2, "ceylon-cinnamon-google", "Ceylon Cinnamon", pos=3),
            _prod(3, "magnesium-glycinate", "Magnesium", pos=1),
        ])
        long_copy = "You've heard turmeric helps with inflammation. But you've probably never heard this about fillers."
        self.ads = [
            _ad("1", "Supp", "2026-08-10", "https://supp.com/products/ceylon-cinnamon-google?utm=1", long_copy),
            _ad("2", "Supp", "2026-08-11", "https://supp.com/products/ceylon-cinnamon-google", long_copy + " Extra."),
            _ad("3", "Supp", "2026-09-06", "https://supp.com/products/ceylon-cinnamon-google", long_copy + " Today."),  # new + lineage
            _ad("4", "Persona", "2026-09-01", "https://supp.com/pages/av1", "Six words from my doctor cost me years."),
            _ad("5", "Supp", "2026-09-05", "https://supp.com/products/nope-not-a-product", "Different copy entirely here."),
            _ad("6", "Supp", "2026-07-01", "https://supp.com/products/magnesium-glycinate", "Sleep story", active=0),
        ]

    def _fake_session(self):
        """av1 advertorial -> HTML with a buy form; anything else -> 404 (like a hidden product)."""
        session = mock.Mock(spec=requests.Session)

        def get(url, **kw):
            r = requests.Response()
            r.encoding = "utf-8"
            r.url = url
            if "/pages/av1" in url:
                r.status_code = 200
                r._content = (b'<html><a href="/products/magnesium-glycinate-2">buy</a>'
                              b'<form action="/cart/add"><input type="hidden" name="id" value="30"></form></html>')
            else:
                r.status_code = 404
                r._content = b"not found"
            return r
        session.get.side_effect = get
        return session

    def test_process_store_end_to_end(self):
        # first scrape of the store: every ad is first-seen today. Lineage must still only pick
        # ad 3 (Meta start date today) as new, never the old-start ads pairing with each other.
        meta_ads.record_scrape(self.conn, self.sid, self.TODAY, self.ads, "Supp")
        session = self._fake_session()
        m = ad_metrics.process_store(self.conn, self.sid, "supp.com", self.TODAY, session)
        self.assertEqual(m["ads"], 6)
        rows = {r["ad_id"]: dict(r) for r in self.conn.execute("SELECT * FROM meta_ads")}
        self.assertEqual(rows["1"]["product_handle"], "ceylon-cinnamon-google")
        self.assertEqual(rows["1"]["landing_resolved_via"], "url")
        self.assertEqual(rows["4"]["page_handle"], "av1")
        self.assertEqual(rows["4"]["product_handle"], "magnesium-glycinate")   # variant id 30 -> product 3, via fetch
        self.assertEqual(rows["4"]["landing_resolved_via"], "page-fetch")
        self.assertIsNone(rows["5"]["product_handle"])
        self.assertEqual(rows["5"]["landing_resolved_via"], "url-unmatched")
        self.assertEqual(session.get.call_count, 3)   # nope.json probe, av1 once (cached), nope page once
        # concepts: 1+2 chain (08-10, 08-11), 3 alone (09-06), 4 alone, 5 alone, 6 alone
        self.assertEqual(rows["1"]["concept_id"], rows["2"]["concept_id"])
        self.assertNotEqual(rows["1"]["concept_id"], rows["3"]["concept_id"])
        concepts = self.conn.execute("SELECT * FROM meta_concepts_daily ORDER BY ads_ever DESC").fetchall()
        self.assertEqual(concepts[0]["ads_ever"], 2)
        self.assertEqual(concepts[0]["survival"], 1.0)
        self.assertEqual(concepts[0]["days_running"], 27)
        dead = next(c for c in concepts if c["product_handle"] == "magnesium-glycinate" and c["page_name"] == "Supp")
        self.assertEqual((dead["ads_active"], dead["survival"]), (0, 0.0))
        # lineage: ad 3 (new today) ~ ad 1 (27 days)
        self.assertEqual(rows["3"]["lineage_of"], "1")
        self.assertGreater(rows["3"]["lineage_similarity"], 0.7)
        self.assertIsNone(rows["5"]["lineage_of"])
        self.assertIsNone(rows["1"]["lineage_of"])     # old ads never get lineage on first scrape
        self.assertIsNone(rows["2"]["lineage_of"])
        # daily metrics
        d3 = self.conn.execute("SELECT days_running, engagement, engagement_per_day FROM meta_ads_daily WHERE ad_id='3'").fetchone()
        self.assertEqual(tuple(d3), (0, None, None))
        # alerts: rule 7 fires for ad 3 (parent 27d >= 20)
        alerts = ad_metrics.run_alerts(self.conn, self.sid, "supp.com", self.TODAY)
        self.assertEqual([a["rule"] for a in alerts], [7])
        self.assertIn("1 new ad on Supp re-use copy from ad 1 running 27d", alerts[0]["detail"])
        self.assertIn("e.g. 3", alerts[0]["detail"])
        self.assertEqual(alerts[0]["handle"], "ceylon-cinnamon-google")
        self.assertEqual(ad_metrics.run_alerts(self.conn, self.sid, "supp.com", self.TODAY), [])   # idempotent
        # raw advertised handle is kept even when it resolved elsewhere
        self.assertEqual(rows["5"]["landing_handle"], "nope-not-a-product")
        self.assertEqual(rows["1"]["landing_handle"], "ceylon-cinnamon-google")
        self.assertEqual(rows["4"]["landing_handle"], None)
        # Signals join
        sig = ad_metrics.meta_for_signals(self.conn, self.sid, self.TODAY)
        self.assertEqual(sig["ceylon-cinnamon-google"]["ads_pointing_here"], 3)
        self.assertEqual(sig["ceylon-cinnamon-google"]["days_running_max"], 27)
        self.assertIn("2/2 concepts alive", sig["ceylon-cinnamon-google"]["concept_status"])
        self.assertEqual(sig["magnesium-glycinate"]["ads_pointing_here"], 1)      # ad 4 via advertorial
        rows = {r[2]: r for r in sheets.signals_rows(self.conn)}
        H = sheets.SIGNALS_HEADERS
        r = rows["ceylon-cinnamon-google"]
        self.assertEqual([r[H.index("ads_pointing_here")], r[H.index("engagement_per_day")], r[H.index("days_running_max")]], [3, "", 27])
        self.assertEqual(rows["ceylon-cinnamon"][11], "")
        # markdown
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = ad_metrics.write_alerts_markdown(self.conn, self.TODAY, Path(d) / "a.md")
            self.assertIn("rule 7", p.read_text())

    def test_rule6_concept_survives_while_page_thins_out(self):
        week_ago = (date.fromisoformat(self.TODAY) - timedelta(days=7)).isoformat()
        copy = ["one copy here", "two copy here", "three copy here", "four copy here"]
        ads_then = [_ad("a", "P", "2026-08-01", "https://supp.com/products/ceylon-cinnamon", copy[0]),
                    _ad("b", "P", "2026-08-01", "https://supp.com/products/ceylon-cinnamon", copy[1]),
                    _ad("c", "P", "2026-08-15", "https://supp.com/products/ceylon-cinnamon-google", copy[2]),
                    _ad("d", "P", "2026-08-20", "https://supp.com/pages/x", copy[3])]
        meta_ads.record_scrape(self.conn, self.sid, week_ago, ads_then, "P")
        ad_metrics.process_store(self.conn, self.sid, "supp.com", week_ago, None, fetch_landings=False)
        # today: concept a+b still fully alive; c and d gone
        meta_ads.record_scrape(self.conn, self.sid, self.TODAY, ads_then[:2], "P")
        ad_metrics.process_store(self.conn, self.sid, "supp.com", self.TODAY, None, fetch_landings=False)
        alerts = ad_metrics.run_alerts(self.conn, self.sid, "supp.com", self.TODAY)
        self.assertEqual([a["rule"] for a in alerts], [6])
        self.assertIn("2/2 ads alive 36d", alerts[0]["detail"])
        self.assertIn("3 -> 1", alerts[0]["detail"])

    def test_rule5_engagement_doubling(self):
        ads = [_ad("e", "P", "2026-08-20", "https://supp.com/products/ceylon-cinnamon", "copy")]
        for d, eng in (("2026-08-29", 100), ("2026-08-30", 110), ("2026-09-05", 200), ("2026-09-06", 260)):
            a = dict(ads[0], reactions=eng, comments=0, shares=0)
            meta_ads.record_scrape(self.conn, self.sid, d, [a], "P")
            ad_metrics.process_store(self.conn, self.sid, "supp.com", d, None, fetch_landings=False)
        epd = {r["snapshot_date"]: r["engagement_per_day"] for r in
               self.conn.execute("SELECT snapshot_date, engagement_per_day FROM meta_ads_daily WHERE ad_id='e'")}
        self.assertEqual(epd["2026-08-30"], 10.0)        # (110-100)/1
        self.assertEqual(epd["2026-09-06"], 20.0)        # (260-100)/8 days window
        alerts = ad_metrics.run_alerts(self.conn, self.sid, "supp.com", "2026-09-06")
        self.assertEqual([a["rule"] for a in alerts], [5])


if __name__ == "__main__":
    unittest.main()


class PageRelevanceTests(unittest.TestCase):
    def test_pages_that_never_land_on_store_are_ignored(self):
        ads = [
            {"page_name": "Brand", "landing_url": "https://supp.com/products/x", "landing_domain": "supp.com", "product_handle": "x"},
            {"page_name": "Brand", "landing_url": "https://other.com/p", "landing_domain": "other.com", "product_handle": None},
            {"page_name": "Persona", "landing_url": "https://supp.com/pages/av1", "landing_domain": "supp.com", "product_handle": None},
            {"page_name": "Funnel", "landing_url": "https://getsupp.co/go", "landing_domain": "getsupp.co", "product_handle": "x"},
            {"page_name": "Stranger", "landing_url": "https://pureveenarg.com/products/z", "landing_domain": "pureveenarg.com", "product_handle": None},
            {"page_name": "Stranger", "landing_url": "https://glowfem.com/", "landing_domain": "glowfem.com", "product_handle": None},
            {"page_name": "NoUrls", "landing_url": None, "landing_domain": None, "product_handle": None},
        ]
        rel = ad_metrics.page_relevance(ads, "supp.com")
        self.assertEqual(rel["Brand"]["ignored"], 0)
        self.assertEqual(rel["Persona"]["ignored"], 0)
        self.assertEqual(rel["Funnel"]["ignored"], 0)        # resolved through a redirect domain
        self.assertEqual(rel["Stranger"]["ignored"], 1)
        self.assertEqual(rel["NoUrls"]["ignored"], 0)        # nothing to judge by -> keep
        self.assertEqual((rel["Brand"]["ads"], rel["Brand"]["with_url"], rel["Brand"]["on_store"]), (2, 2, 1))

    def test_ignored_page_excluded_from_concepts_alerts_and_signals(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        db.write_product_snapshot(conn, sid, "2026-09-06", [_prod(1, "hero", "Hero", pos=0)])
        copy = "You've heard turmeric helps with inflammation but never this about fillers and dosing."
        ads = [_ad("a", "Brand", "2026-08-01", "https://supp.com/products/hero", copy),
               _ad("b", "Brand", "2026-09-06", "https://supp.com/products/hero", copy + " Now."),
               _ad("c", "Stranger", "2026-08-01", "https://pureveenarg.com/products/z", copy),
               _ad("d", "Stranger", "2026-09-06", "https://pureveenarg.com/products/z", copy + " Now.")]
        for a in ads[2:]:
            a["landing_domain"] = "pureveenarg.com"
        meta_ads.record_scrape(conn, sid, "2026-09-06", ads, "supp.com")
        m = ad_metrics.process_store(conn, sid, "supp.com", "2026-09-06", None, fetch_landings=False)
        self.assertEqual(m["ignored"], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta_concepts_daily").fetchone()[0], 2)   # only Brand's
        self.assertIsNone(conn.execute("SELECT concept_id FROM meta_ads WHERE ad_id='c'").fetchone()[0])
        alerts = ad_metrics.run_alerts(conn, sid, "supp.com", "2026-09-06")
        self.assertEqual(len(alerts), 1)
        self.assertIn("on Brand", alerts[0]["detail"])
        pages = {r["page_name"]: r["ignored"] for r in conn.execute("SELECT page_name, ignored FROM meta_pages_daily")}
        self.assertEqual(pages, {"Brand": 0, "Stranger": 1})
        self.assertEqual(ad_metrics.meta_for_signals(conn, sid, "2026-09-06")["hero"]["ads_pointing_here"], 2)

    def test_rule7_groups_many_children_into_one_alert(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        db.write_product_snapshot(conn, sid, "2026-09-06", [_prod(1, "hero", "Hero")])
        copy = "Clinically dosed turmeric with no filler, you've probably never heard this before today."
        ads = [_ad("parent", "Brand", "2026-07-20", "https://supp.com/products/hero", copy)]
        ads += [_ad(f"kid{i}", "Brand", "2026-09-05", "https://supp.com/products/hero", copy + f" v{i}") for i in range(12)]
        meta_ads.record_scrape(conn, sid, "2026-09-06", ads, "supp.com")
        ad_metrics.process_store(conn, sid, "supp.com", "2026-09-06", None, fetch_landings=False)
        alerts = ad_metrics.run_alerts(conn, sid, "supp.com", "2026-09-06")
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["detail"].startswith("12 new ads on Brand re-use copy from ad parent running 48d"))


class TransitiveLandingTests(unittest.TestCase):
    def test_advertorial_linking_to_unlisted_offer_page_resolves_one_hop(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        db.write_product_snapshot(conn, sid, "2026-09-06", [_prod(1, "turmeric-1000mg", "Turmeric")])
        session = mock.Mock(spec=requests.Session)
        fetched = []

        def get(url, **kw):
            fetched.append(url)
            r = requests.Response(); r.encoding = "utf-8"; r.url = url; r.status_code = 200
            if "/pages/li10" in url:      # advertorial links only to the unlisted offer page
                r._content = b'<a href="/products/turmeric-1-000mg-o2">Buy</a>'
            elif "/products/turmeric-1-000mg-o2" in url:   # unlisted page names the listed product
                r._content = b'<script>{"handle":"turmeric-1000mg"}</script>'
            else:
                r.status_code = 404; r._content = b""
            return r
        session.get.side_effect = get
        ads = [_ad("1", "Persona", "2026-09-01", "https://supp.com/pages/li10", "story one about turmeric"),
               _ad("2", "Persona", "2026-09-02", "https://supp.com/pages/li10?utm=x", "story two about turmeric")]
        meta_ads.record_scrape(conn, sid, "2026-09-06", ads, "q")
        m = ad_metrics.process_store(conn, sid, "supp.com", "2026-09-06", session)
        self.assertEqual(m["resolved"], 2)
        rows = {r["ad_id"]: dict(r) for r in conn.execute("SELECT * FROM meta_ads")}
        self.assertEqual(rows["1"]["product_handle"], "turmeric-1000mg")
        self.assertEqual(rows["1"]["page_handle"], "li10")
        self.assertEqual(fetched, ["https://supp.com/pages/li10", "https://supp.com/products/turmeric-1-000mg-o2"])  # cached
        lp = {r["url"]: dict(r) for r in conn.execute("SELECT * FROM landing_pages")}
        self.assertIn("via:turmeric-1-000mg-o2", lp["https://supp.com/pages/li10"]["candidates"])
        self.assertEqual(lp["https://supp.com/products/turmeric-1-000mg-o2"]["product_handle"], "turmeric-1000mg")

    def test_legacy_per_ad_alert_rows_are_replaced(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        conn.execute("INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at) VALUES "
                     "('2026-09-06', ?, 'x', 7, 'old per-ad row', 'now')", (sid,))
        conn.execute("INSERT INTO alerts (snapshot_date, store_id, product_handle, rule, detail, created_at) VALUES "
                     "('2026-09-06', ?, 'x', 1, 'shopify rule, untouched', 'now')", (sid,))
        ad_metrics.run_alerts(conn, sid, "supp.com", "2026-09-06")
        left = [r[0] for r in conn.execute("SELECT detail FROM alerts")]
        self.assertEqual(left, ["shopify rule, untouched"])


class UnlistedProductTests(unittest.TestCase):
    PRODUCT_JSON = json.dumps({"product": {
        "id": 15551846285652, "title": "Turmeric Curcumin Capsules (1,000mg)", "handle": "turmeric-1-000mg-o2",
        "published_at": "2026-06-27T16:52:29+01:00", "created_at": "2026-06-27T16:52:29+01:00",
        "updated_at": "2026-09-06T10:45:54+01:00", "vendor": "Bioroot Labs", "product_type": "Dietary Supplements",
        "tags": "", "variants": [{"id": 56260941545812, "title": "Default Title", "price": "19.00",
                                  "compare_at_price": "38.00", "sku": "TU-001", "available": True}]}})

    def _conn(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        db.write_product_snapshot(conn, sid, "2026-09-06", [_prod(1, "turmeric-1000mg", "Turmeric Curcumin Capsules (1,000mg)", pos=0)])
        return conn, sid

    def _session(self, log):
        session = mock.Mock(spec=requests.Session)

        def get(url, **kw):
            log.append(url)
            r = requests.Response(); r.encoding = "utf-8"; r.url = url
            if url.endswith("/products/turmeric-1-000mg-o2.json"):
                r.status_code = 200; r.headers["Content-Type"] = "application/json"; r._content = self.PRODUCT_JSON.encode()
            elif "/pages/li10" in url:
                r.status_code = 200; r._content = b'<a href="/products/turmeric-1-000mg-o2">Buy</a>'
            else:
                r.status_code = 404; r._content = b""
            return r
        session.get.side_effect = get
        return session

    def test_unlisted_advertised_product_gets_its_own_row_and_ads(self):
        conn, sid = self._conn()
        ads = [_ad("1", "Brand", "2026-09-01", "https://supp.com/products/turmeric-1-000mg-o2?utm=1", "offer copy one"),
               _ad("2", "Brand", "2026-09-01", "https://supp.com/products/turmeric-1-000mg-o2", "offer copy two"),
               _ad("3", "Brand", "2026-08-01", "https://supp.com/products/turmeric-1000mg", "base copy")]
        meta_ads.record_scrape(conn, sid, "2026-09-06", ads, "q")
        log = []
        m = ad_metrics.process_store(conn, sid, "supp.com", "2026-09-06", self._session(log))
        self.assertEqual(m["unlisted"], 1)
        self.assertEqual(log, ["https://supp.com/products/turmeric-1-000mg-o2.json"])   # one probe, no page fetches
        rows = {r["ad_id"]: dict(r) for r in conn.execute("SELECT * FROM meta_ads")}
        self.assertEqual(rows["1"]["product_handle"], "turmeric-1-000mg-o2")      # not folded into the base product
        self.assertEqual(rows["1"]["landing_resolved_via"], "url")
        self.assertEqual(rows["3"]["product_handle"], "turmeric-1000mg")
        pd = {r["handle"]: dict(r) for r in conn.execute("SELECT * FROM products_daily WHERE snapshot_date='2026-09-06'")}
        self.assertEqual(pd["turmeric-1-000mg-o2"]["unlisted"], 1)
        self.assertEqual(pd["turmeric-1-000mg-o2"]["min_price"], 19.0)
        self.assertEqual(pd["turmeric-1-000mg-o2"]["product_id"], 15551846285652)
        self.assertEqual(pd["turmeric-1000mg"]["unlisted"], 0)
        # Signals: own row, tagged unlisted, same family as the listed product (same title), ads attributed to it
        sig = {r[2]: r for r in sheets.signals_rows(conn)}
        self.assertEqual(sig["turmeric-1-000mg-o2"][3], "variant+unlisted")
        self.assertEqual(sig["turmeric-1-000mg-o2"][1], sig["turmeric-1000mg"][1])
        self.assertEqual(sig["turmeric-1-000mg-o2"][11], 2)
        self.assertEqual(sig["turmeric-1000mg"][11], 1)
        # a normal Shopify snapshot for the same day must not wipe the unlisted row
        db.write_product_snapshot(conn, sid, "2026-09-06", [_prod(1, "turmeric-1000mg", "Turmeric Curcumin Capsules (1,000mg)")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM products_daily WHERE unlisted = 1").fetchone()[0], 1)
        # next day: cached (no refetch), carried into the new snapshot day
        meta_ads.record_scrape(conn, sid, "2026-09-07", ads, "q")
        log.clear()
        ad_metrics.process_store(conn, sid, "supp.com", "2026-09-07", self._session(log))
        self.assertEqual(log, [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM products_daily WHERE unlisted = 1 AND snapshot_date='2026-09-07'").fetchone()[0], 1)

    def test_cached_unresolved_page_rematches_after_unlisted_discovery(self):
        conn, sid = self._conn()
        # day 1: only the advertorial is known; it links to a handle nobody knows yet -> unresolved, cached
        ads = [_ad("a", "Persona", "2026-09-01", "https://supp.com/pages/li10", "advertorial copy")]
        meta_ads.record_scrape(conn, sid, "2026-09-06", ads, "q")
        session = mock.Mock(spec=requests.Session)

        def get_day1(url, **kw):
            r = requests.Response(); r.encoding = "utf-8"; r.url = url
            if "/pages/li10" in url:
                r.status_code = 200; r._content = b'<a href="/products/turmeric-1-000mg-o2">Buy</a>'
            else:
                r.status_code = 404; r._content = b""
            return r
        session.get.side_effect = get_day1
        ad_metrics.process_store(conn, sid, "supp.com", "2026-09-06", session)
        self.assertIsNone(conn.execute("SELECT product_handle FROM meta_ads WHERE ad_id='a'").fetchone()[0])
        # same day, a product ad appears whose .json now resolves; li10 must re-match from its cached candidates
        ads.append(_ad("b", "Brand", "2026-09-05", "https://supp.com/products/turmeric-1-000mg-o2", "offer copy"))
        meta_ads.record_scrape(conn, sid, "2026-09-06", ads, "q")
        log = []
        ad_metrics.process_store(conn, sid, "supp.com", "2026-09-06", self._session(log))
        self.assertEqual(conn.execute("SELECT product_handle FROM meta_ads WHERE ad_id='a'").fetchone()[0], "turmeric-1-000mg-o2")
        self.assertNotIn("https://supp.com/pages/li10", log)   # served from cache, re-matched


class RedirectedHandleTests(unittest.TestCase):
    def test_old_handle_redirecting_to_listed_product_is_not_recorded_as_unlisted(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "supp.com")
        db.write_product_snapshot(conn, sid, "2026-09-06", [_prod(1, "turmeric-1000mg", "Turmeric", pos=0)])
        session = mock.Mock(spec=requests.Session)

        def get(url, **kw):
            r = requests.Response(); r.encoding = "utf-8"
            if url.endswith("/products/turmeric.json"):     # store redirects old handle -> current product json
                r.status_code = 200; r.url = "https://supp.com/products/turmeric-1000mg.json"
                r.headers["Content-Type"] = "application/json"
                r._content = json.dumps({"product": {"id": 1, "handle": "turmeric-1000mg", "title": "Turmeric",
                                                     "variants": [{"id": 10, "price": "39.00", "available": True}]}}).encode()
            else:
                r.status_code = 404; r.url = url; r._content = b""
            return r
        session.get.side_effect = get
        ads = [_ad("1", "Brand", "2026-09-01", "https://supp.com/products/turmeric", "old handle copy")]
        meta_ads.record_scrape(conn, sid, "2026-09-06", ads, "q")
        m = ad_metrics.process_store(conn, sid, "supp.com", "2026-09-06", session)
        self.assertEqual(m["unlisted"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM products_daily WHERE unlisted = 1").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT product_handle FROM meta_ads WHERE ad_id='1'").fetchone()[0], "turmeric-1000mg")
        lp = conn.execute("SELECT candidates FROM landing_pages WHERE url LIKE '%turmeric.json'").fetchone()[0]
        self.assertEqual(lp, "redirect:turmeric-1000mg")


class EncodedHandleTests(unittest.TestCase):
    def test_percent_encoded_handles_decode_and_match(self):
        self.assertEqual(ad_metrics.handle_from_url("https://uk.avalaine.com/products/nervana-%C2%AE-magnesium-patches?x=1"),
                         ("nervana-®-magnesium-patches", None))
        self.assertEqual(ad_metrics.handle_from_url("https://lumiqour.com/products/turkey-tail-probiotic%e2%84%a2.json"),
                         ("turkey-tail-probiotic™", None))
        known = {"nervana-®-magnesium-patches", "turkey-tail-probiotic™"}
        self.assertEqual(ad_metrics.match_handle("nervana-®-magnesium-patches", known), "nervana-®-magnesium-patches")
        html = '<a href="/products/shipping-protection-1">x</a><a href="/products/turkey-tail-probiotic%E2%84%A2">buy</a>'
        ranked = ad_metrics.handles_from_html(html)
        self.assertEqual(ranked[0][0], "turkey-tail-probiotic™")     # junk widget sorted last
        self.assertEqual(ranked[-1][0], "shipping-protection-1")

    def test_shipping_protection_never_wins_page_resolution(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "uk.avalaine.com")
        db.write_product_snapshot(conn, sid, "2026-09-06", [_prod(1, "shipping-protection-1", "Shipping Protection"),
                                                             _prod(2, "nervana-®-magnesium-patches", "Nervana Patches")])
        session = mock.Mock(spec=requests.Session)

        def get(url, **kw):
            r = requests.Response(); r.encoding = "utf-8"; r.url = url; r.status_code = 200
            r._content = (b'<a href="/products/shipping-protection-1">p</a><a href="/products/shipping-protection-1">p</a>'
                          b'<a href="/products/nervana-%C2%AE-magnesium-patches">buy</a>')
            return r
        session.get.side_effect = get
        ads = [_ad("1", "Susan", "2026-09-01", "https://uk.avalaine.com/pages/patches-story", "story copy here")]
        ads[0]["landing_domain"] = "uk.avalaine.com"
        meta_ads.record_scrape(conn, sid, "2026-09-06", ads, "q")
        ad_metrics.process_store(conn, sid, "uk.avalaine.com", "2026-09-06", session)
        self.assertEqual(conn.execute("SELECT product_handle FROM meta_ads WHERE ad_id='1'").fetchone()[0],
                         "nervana-®-magnesium-patches")

    def test_payload_shape_reports_null_fields(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        n = {"ad_archive_id": "1", "page_name": "P", "snapshot": {"body": {"text": "t"}, "link_url": None},
             "aaa_info": {"eu_total_reach": None}, "reach_estimate": None}
        a = meta_ads.normalise_ad(n)
        meta_ads.record_scrape(conn, sid, "2026-09-06", [a], "q")
        shape = {k: (c, z) for k, c, z in ad_metrics.payload_shape(conn, "2026-09-06")}
        self.assertEqual(shape["aaa_info.eu_total_reach"], (1, 1))
        self.assertEqual(shape["reach_estimate"], (1, 1))
        self.assertEqual(shape["snapshot.link_url"], (1, 1))
        self.assertIn("aaa_info.eu_total_reach=null", a["reach_keys"])


class MetaPassPlanningTests(unittest.TestCase):
    """Which stores the Meta pass takes, in what order, and the per-run landing-fetch cap."""

    def test_scope_and_rotation(self):
        from earlyscale import cli, db, meta_ads
        conn = db.connect(":memory:")
        stores = [{"store_domain": d} for d in ("a.com", "b.com", "c.com", "https://d.com")]
        ids = {d: db.upsert_store(conn, d) for d in ("a.com", "b.com", "c.com", "https://d.com")}
        meta_ads.record_page_run(conn, ids["a.com"], "2026-09-06", "a", "ok", "", 5, 1, 1.0)
        meta_ads.record_page_run(conn, ids["b.com"], "2026-09-04", "b", "ok", "", 5, 1, 1.0)
        meta_ads.record_page_run(conn, ids["c.com"], "2026-09-05", "c", "blocked", "", 0, 0, 1.0)   # not a success
        order = [s["store_domain"] for s in cli.plan_meta_stores(conn, stores, scope=[])]
        self.assertEqual(order, ["c.com", "https://d.com", "b.com", "a.com"])     # never scraped first, then oldest
        self.assertEqual([s["store_domain"] for s in cli.plan_meta_stores(conn, stores, scope=["d.com", "A.com"])],
                         ["https://d.com", "a.com"])
        self.assertEqual([s["store_domain"] for s in cli.plan_meta_stores(conn, stores, only={"b.com"}, scope=["a.com"])], ["b.com"])

    def test_landing_fetch_budget_per_store(self):
        from earlyscale import config, db
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        calls = []

        class S:
            def get(self, url, **kw):
                calls.append(url)
                r = requests.Response(); r.status_code = 200; r.url = url; r._content = b"<html></html>"
                return r
        old = config.META_MAX_LANDING_FETCH
        config.META_MAX_LANDING_FETCH = 2
        try:
            cache = {}
            for i in range(5):
                ad_metrics._resolve_url(conn, f"https://adv.example/{i}", "x.com", set(), {}, S(), cache, "2026-09-07", 0)
        finally:
            config.META_MAX_LANDING_FETCH = old
        self.assertEqual(len(calls), 2)
        self.assertEqual(sum(1 for h in cache.values() if isinstance(h, dict) and h.get("status") is not None), 2)


class LandingKindTests(unittest.TestCase):
    def test_buckets(self):
        st = "biorootlabs.com"
        cases = [
            ({"product_handle": "x", "landing_url": "https://biorootlabs.com/products/x"}, "product"),
            ({"landing_url": "https://biorootlabs.com/products/x-o2", "landing_domain": "biorootlabs.com"}, "unlisted-product"),
            ({"landing_url": "https://biorootlabs.com/pages/story", "landing_domain": "biorootlabs.com"}, "advertorial"),
            ({"landing_url": "https://www.biorootlabs.com/", "landing_domain": "www.biorootlabs.com"}, "homepage"),
            ({"landing_url": "https://biorootlabs.com/collections/all", "landing_domain": "biorootlabs.com"}, "collection"),
            ({"landing_url": "https://biorootlabs.com/blogs/news/x", "landing_domain": "biorootlabs.com"}, "other-store-path"),
            ({"landing_url": "https://healthnews.example/advertorial", "landing_domain": "healthnews.example"}, "external"),
            ({"landing_url": None}, "no-url"),
            ({"landing_url": "https://other.example/", "page_ignored": 1}, "page-ignored"),
        ]
        for ad, want in cases:
            with self.subTest(want=want):
                self.assertEqual(ad_metrics.landing_kind(ad, st), want)

    def test_breakdown_and_summary(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "biorootlabs.com")
        rows = [("1", "x", "https://biorootlabs.com/products/x", "biorootlabs.com", 0),
                ("2", None, "https://biorootlabs.com/", "biorootlabs.com", 0),
                ("3", None, "https://news.example/a", "news.example", 0),
                ("4", None, "https://news.example/b", "news.example", 1)]
        for aid, ph, url, dom, ign in rows:
            conn.execute("""INSERT INTO meta_ads (ad_id, store_id, first_seen_date, last_seen_date, product_handle, landing_url, landing_domain, page_ignored)
                            VALUES (?,?,?,?,?,?,?,?)""", (aid, sid, "2026-09-07", "2026-09-07", ph, url, dom, ign))
            conn.execute("INSERT INTO meta_ads_daily (snapshot_date, ad_id, store_id, is_active, fetched_at) VALUES ('2026-09-07', ?, ?, 1, 'x')", (aid, sid))
        b = ad_metrics.landing_breakdown(conn, sid, "biorootlabs.com", "2026-09-07")
        self.assertEqual((b["active"], b["to_products"], b["products"], b["homepage"], b["external"], b["page-ignored"]), (4, 1, 1, 1, 1, 1))
        self.assertEqual(ad_metrics.breakdown_summary(b), "homepage 1, external 1, page-ignored 1")
        empty = ad_metrics.landing_breakdown(conn, db.upsert_store(conn, "none.com"), "none.com", "2026-09-07")
        self.assertIsNone(empty["snapshot"])


class AdVelocityTests(unittest.TestCase):
    """ads launched this week vs last (Meta start date), per store and per product; rule 2."""

    def _seed(self, conn, sid, rows):
        for aid, start, handle, active in rows:
            conn.execute("""INSERT INTO meta_ads (ad_id, store_id, page_name, ad_start_date, first_seen_date, last_seen_date, product_handle)
                            VALUES (?,?,?,?,?,?,?)""", (aid, sid, "P", start, "2026-09-01", "2026-09-07", handle))
            conn.execute("INSERT INTO meta_ads_daily (snapshot_date, ad_id, store_id, is_active, fetched_at) VALUES ('2026-09-07', ?, ?, ?, 'x')",
                         (aid, sid, active))
        conn.commit()

    def test_store_and_product_velocity(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        # this week: 6 launches (4 on handle a, 2 on b); last week: 2 (both on a); older: 1
        rows = [(f"n{i}", "2026-09-0" + str(3 + i % 4), "a" if i < 4 else "b", 1) for i in range(6)]
        rows += [("p1", "2026-08-27", "a", 1), ("p2", "2026-08-30", "a", 0), ("o1", "2026-08-01", "b", 1)]
        self._seed(conn, sid, rows)
        v = ad_metrics.ad_velocity(conn, sid, "2026-09-07")
        self.assertEqual((v["new_ads_7d"], v["new_ads_prev_7d"], v["ad_velocity_wow"]), (6, 2, 3.0))
        self.assertEqual(ad_metrics.ad_velocity(conn, sid, "2026-09-07", "b")["ad_velocity_wow"], float("inf"))   # 2 vs 0
        m = ad_metrics.meta_for_signals(conn, sid, "2026-09-07")
        self.assertEqual((m["a"]["ads_launched_7d"], m["a"]["ads_launched_prev_7d"], m["a"]["ad_velocity_wow"]), (4, 2, 2.0))
        self.assertEqual((m["b"]["ads_launched_7d"], m["b"]["ad_velocity_wow"], m["b"]["ads_pointing_here"]), (2, "new", 3))
        # rule 2 fires once for the store (6 >= 3 launches, x3.0 >= 2), and not again the same week
        found = ad_metrics.run_alerts(conn, sid, "x.com", "2026-09-07")
        self.assertIn(2, [f["rule"] for f in found])
        self.assertEqual([f["rule"] for f in ad_metrics.run_alerts(conn, sid, "x.com", "2026-09-07")], [])

    def test_signals_sort_by_launches_then_pointing_then_age(self):
        from earlyscale import sheets, shopify
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        def prod(pid, handle, days):
            from datetime import datetime, timedelta, timezone
            ts = (datetime(2026, 9, 7, tzinfo=timezone.utc) - timedelta(days=days)).isoformat()
            return {"product_id": pid, "handle": handle, "title": handle, "vendor": "", "product_type": None, "tags": [],
                    "created_at": ts, "published_at": ts, "updated_at": ts, "variant_count": 1, "sold_out_variants": 0,
                    "min_price": 10, "max_price": 10, "collection_position": None, "variants": []}
        db.write_product_snapshot(conn, sid, "2026-09-07", [prod(1, "quiet-young", 2), prod(2, "launching", 40), prod(3, "pointed", 20)])
        self._seed(conn, sid, [("l1", "2026-09-05", "launching", 1), ("l2", "2026-09-06", "launching", 1), ("o1", "2026-07-01", "pointed", 1)])
        rows = sheets.signals_rows(conn, "2026-09-07")
        self.assertEqual([r[2] for r in rows], ["launching", "pointed", "quiet-young"])
        i = sheets.SIGNALS_HEADERS.index("ads_launched_7d")
        self.assertEqual(rows[0][i:i + 3], [2, 0, "new"])
