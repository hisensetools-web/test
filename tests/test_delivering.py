"""'Low impression count' badge: extraction, storage per ad per day, delivering metrics, ranking, backfill, report."""
import argparse
import json
import unittest
from unittest import mock

from earlyscale import ad_metrics, cli, db, meta_ads, sheets
from tests.test_ad_metrics import _ad, _prod

TODAY = "2026-09-08"
WEEK_AGO = "2026-09-01"


class ExtractTests(unittest.TestCase):
    def test_boolean_key_anywhere(self):
        self.assertEqual(meta_ads.extract_low_impressions({"ad_archive_id": "1", "is_low_impressions": True}), (1, "is_low_impressions"))
        self.assertEqual(meta_ads.extract_low_impressions({"ad_archive_id": "1", "is_low_impressions": False}), (0, "is_low_impressions"))
        self.assertEqual(meta_ads.extract_low_impressions({"snapshot": {"impressions": {"low_impression_count": True}}}),
                         (1, "snapshot.impressions.low_impression_count"))

    def test_label_text_counts_as_present(self):
        self.assertEqual(meta_ads.extract_low_impressions({"snapshot": {"badges": [{"label": "Low impression count"}]}}),
                         (1, "snapshot.badges[0].label"))
        self.assertEqual(meta_ads.extract_low_impressions({"impression_label": "LOW_IMPRESSIONS"}), (1, "impression_label"))

    def test_absent_is_unknown(self):
        self.assertEqual(meta_ads.extract_low_impressions({"ad_archive_id": "1", "impressions_with_index": {"impressions_text": "<1K"}}), (None, None))

    def test_normalise_ad_carries_it(self):
        a = meta_ads.normalise_ad({"ad_archive_id": "9", "snapshot": {}, "is_low_impressions": True})
        self.assertEqual((a["low_impressions"], a["low_impressions_key"]), (1, "is_low_impressions"))


class CardBadgeTests(unittest.TestCase):
    def test_card_script_keeps_its_regex_escapes(self):
        # a non-raw string turned \s into a SyntaxWarning on Python 3.12+ and would drop the escape at runtime
        self.assertIn(r"Library ID:?\s*(\d{3,20})", meta_ads.CARD_BADGE_JS)

    def test_card_badges_override_payload_and_survive_a_second_scrape(self):
        ads = [{"ad_id": "1", "low_impressions": None}, {"ad_id": "2", "low_impressions": None}, {"ad_id": "3", "low_impressions": None}]
        n = meta_ads.apply_card_badges(ads, {"1": True, "2": False})
        self.assertEqual(n, 2)
        self.assertEqual([a["low_impressions"] for a in ads], [1, 0, None])
        self.assertEqual(ads[0]["low_impressions_key"], "card:Low impression count")
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        first = _ad("1", "B", "2026-09-01", "https://x.com/products/p", "c")
        first["low_impressions"] = 1
        meta_ads.record_scrape(conn, sid, TODAY, [first], "q")
        again = _ad("1", "B", "2026-09-01", "https://x.com/products/p", "c")     # e.g. the impressions-sorted pass: no card read
        meta_ads.record_scrape(conn, sid, TODAY, [again], "rank:q")
        self.assertEqual(conn.execute("SELECT low_impressions FROM meta_ads_daily WHERE ad_id = '1'").fetchone()[0], 1)


class DeliveringRuleTests(unittest.TestCase):
    def test_badge_beats_everything(self):
        self.assertFalse(ad_metrics.delivering({"is_active": 1, "delivery_status": "on", "low_impressions": 1}))
        self.assertTrue(ad_metrics.delivering({"is_active": 1, "low_impressions": 0}))
        self.assertTrue(ad_metrics.delivering({"is_active": 1, "low_impressions": None}))      # unknown = delivering
        self.assertFalse(ad_metrics.delivering({"is_active": 1, "delivery_status": "off", "low_impressions": None}))
        self.assertFalse(ad_metrics.delivering({"is_active": 0, "low_impressions": 0}))


def seed(conn, sid, day, ads):
    """Record a scrape day: ads is [(id, handle, badge)] -> all active, resolved to the handle."""
    recs = []
    for aid, handle, badge in ads:
        a = _ad(aid, "Brand", "2026-08-20", f"https://supp.com/products/{handle}", f"copy {aid}")
        a["low_impressions"] = badge
        a["low_impressions_key"] = "is_low_impressions" if badge is not None else None
        a["raw_json"] = json.dumps({"ad_archive_id": aid, "is_low_impressions": bool(badge)} if badge is not None else {"ad_archive_id": aid})
        recs.append(a)
    meta_ads.record_scrape(conn, sid, day, recs, "q")
    for aid, handle, _ in ads:
        conn.execute("UPDATE meta_ads SET product_handle = ?, concept_id = ? WHERE ad_id = ?", (handle, f"c-{handle}-{aid[-1]}", aid))
    conn.commit()
    ad_metrics.write_concept_rows(conn, sid, day)
    conn.commit()


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "supp.com")
        db.write_product_snapshot(self.conn, self.sid, TODAY, [_prod(1, "calming-diffuser", "Calming Diffuser", 0), _prod(2, "kombucha-gummy", "Kombucha Gummy", 1)])
        # a week ago: diffuser had 10 active ads, 4 delivering; gummy 3 active, all delivering
        seed(self.conn, self.sid, WEEK_AGO, [(f"d{i}", "calming-diffuser", 1 if i < 6 else 0) for i in range(10)] +
                                             [(f"g{i}", "kombucha-gummy", 0) for i in range(3)])
        # today: diffuser 15 active, 11 badged -> 4 delivering; gummy 8 active, 1 badged -> 7 delivering
        seed(self.conn, self.sid, TODAY, [(f"d{i}", "calming-diffuser", 1 if i < 11 else 0) for i in range(15)] +
                                          [(f"g{i}", "kombucha-gummy", 1 if i == 0 else 0) for i in range(8)])

    def test_daily_rows_hold_the_badge(self):
        rows = {r[0]: r[1] for r in self.conn.execute("SELECT ad_id, low_impressions FROM meta_ads_daily WHERE snapshot_date = ?", (TODAY,))}
        self.assertEqual((rows["d0"], rows["d14"], rows["g0"], rows["g5"]), (1, 0, 1, 0))
        self.assertEqual(self.conn.execute("SELECT low_impressions_key FROM meta_ads WHERE ad_id = 'd0'").fetchone()[0], "is_low_impressions")

    def test_delivering_metrics_per_product_and_store(self):
        m = ad_metrics.delivering_metrics(self.conn, self.sid, TODAY)
        d, g, st = m["calming-diffuser"], m["kombucha-gummy"], m["__store__"]
        self.assertEqual((d["ads_active"], d["ads_delivering"], d["ads_low_impressions"], d["ads_delivering_7d_ago"], d["delivering_velocity_wow"]),
                         (15, 4, 11, 4, 1.0))
        self.assertEqual((g["ads_active"], g["ads_delivering"], g["ads_delivering_7d_ago"], g["delivering_velocity_wow"]), (8, 7, 3, 2.33))
        self.assertEqual((st["ads_active"], st["ads_delivering"], st["ads_delivering_7d_ago"]), (23, 11, 7))
        self.assertEqual(d["concepts_delivering"], 4)                                   # one concept per ad id suffix... badged ones dead
        self.assertEqual(g["concepts_delivering"], 7)

    def test_concept_survival_counts_only_delivering_ads(self):
        rows = {r["concept_id"]: dict(r) for r in self.conn.execute("SELECT * FROM meta_concepts_daily WHERE snapshot_date = ?", (TODAY,))}
        self.assertEqual((rows["c-calming-diffuser-0"]["ads_active"], rows["c-calming-diffuser-0"]["ads_delivering"], rows["c-calming-diffuser-0"]["survival_source"]),
                         (2, 0, "badge"))                                              # d0 + d10 share the suffix-0 concept; both badged

    def test_signals_rank_on_delivering_then_trend(self):
        with mock.patch.object(sheets, "watched_store_ids", lambda c: None):
            rows = sheets.signals_rows(self.conn)
        H = sheets.SIGNALS_HEADERS
        self.assertEqual([r[H.index("handle")] for r in rows], ["kombucha-gummy", "calming-diffuser"])   # 7 delivering beats 15 active / 4 delivering
        r = {x[H.index("handle")]: x for x in rows}["calming-diffuser"]
        self.assertEqual((r[H.index("ads_pointing_here")], r[H.index("ads_delivering")], r[H.index("ads_low_impressions")],
                          r[H.index("ads_delivering_7d_ago")], r[H.index("delivering_velocity_wow")], r[H.index("concepts_delivering")]),
                         (15, 4, 11, 4, 1.0, 4))
        with mock.patch.object(sheets, "watched_store_ids", lambda c: None):
            srows = sheets.stores_rows(self.conn)
        SH = sheets.STORES_HEADERS
        self.assertEqual((srows[0][SH.index("ads_active")], srows[0][SH.index("ads_delivering")], srows[0][SH.index("ads_low_impressions")]), (23, 11, 12))

    def test_week_ago_without_badge_data_gives_blank_trend(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        db.write_product_snapshot(conn, sid, TODAY, [_prod(1, "p", "P", 0)])
        seed(conn, sid, WEEK_AGO, [(f"a{i}", "p", None) for i in range(5)])           # old scrape: no badge field at all
        seed(conn, sid, TODAY, [(f"a{i}", "p", 1 if i < 3 else 0) for i in range(5)])
        m = ad_metrics.delivering_metrics(conn, sid, TODAY)["p"]
        self.assertEqual((m["ads_delivering"], m["ads_delivering_7d_ago"], m["delivering_velocity_wow"]), (2, 5, ""))

    def test_backfill_from_stored_payloads(self):
        self.conn.execute("UPDATE meta_ads_daily SET low_impressions = NULL WHERE snapshot_date = ?", (TODAY,))
        self.conn.commit()
        bf = meta_ads.backfill_low_impressions(self.conn, self.sid)
        self.assertEqual((bf["updated"], bf["flagged"], bf["keys"]), (23, 12, {"is_low_impressions": 23}))
        self.assertEqual(self.conn.execute("SELECT low_impressions FROM meta_ads_daily WHERE ad_id = 'd0' AND snapshot_date = ?", (TODAY,)).fetchone()[0], 1)

    def test_payload_fields_diagnostic(self):
        f = meta_ads.payload_fields(self.conn, "impression", self.sid)
        self.assertIn("is_low_impressions", f)
        self.assertEqual(f["is_low_impressions"][0], 23)

    def test_delivering_report_runs(self):
        import io
        from rich.console import Console
        buf = io.StringIO()
        with mock.patch.object(cli, "console", Console(file=buf, width=160)):
            rc = cli.cmd_delivering_report(argparse.Namespace(db=None, handle=["calming-diffuser", "kombucha"], store=None, date=None, limit=5))
        self.assertEqual(rc, 1)   # db=None opens the default path: no matching products there
        with mock.patch.object(cli, "console", Console(file=buf, width=160)), mock.patch.object(db, "connect", lambda p=None: self.conn):
            rc = cli.cmd_delivering_report(argparse.Namespace(db=None, handle=["calming-diffuser", "kombucha"], store=None, date=None, limit=5))
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("calming-diffuser", out)
        self.assertIn("15", out)
        self.assertIn("LOW", out)


if __name__ == "__main__":
    unittest.main()
