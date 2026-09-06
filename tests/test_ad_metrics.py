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
        self.assertEqual(session.get.call_count, 2)   # av1 fetched once (cache); nope-not-a-product fetched once
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
        self.assertIn("ad 3", alerts[0]["detail"])
        self.assertEqual(ad_metrics.run_alerts(self.conn, self.sid, "supp.com", self.TODAY), [])   # idempotent
        # Signals join
        sig = ad_metrics.meta_for_signals(self.conn, self.sid, self.TODAY)
        self.assertEqual(sig["ceylon-cinnamon-google"]["ads_pointing_here"], 3)
        self.assertEqual(sig["ceylon-cinnamon-google"]["days_running_max"], 27)
        self.assertIn("2/2 concepts alive", sig["ceylon-cinnamon-google"]["concept_status"])
        self.assertEqual(sig["magnesium-glycinate"]["ads_pointing_here"], 1)      # ad 4 via advertorial
        rows = {r[2]: r for r in sheets.signals_rows(self.conn)}
        self.assertEqual(rows["ceylon-cinnamon-google"][11:14], [3, "", 27])
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
