"""Delta/report logic against fixture JSON: day 1 = products_page1.json, day 2 = products_day2.json.

Day 2 differences: cloud-hoodie S+M price 59->49 (2 variants), S sold out, L restocked (2 sold-out -> 2);
no-variants (103) removed; launch-drop (104) new and published on day 2.
"""
import json
import unittest
from datetime import date
from pathlib import Path

from earlyscale import db, deltas, shopify

FIX = Path(__file__).parent / "fixtures"
D1, D2 = "2026-09-05", "2026-09-06"


def load(name):
    return shopify.normalise_products(json.loads((FIX / name).read_text())["products"])


def variants(products):
    return [{"variant_id": v["variant_id"], "product_id": p["product_id"], "price": v["price"],
             "available": v["available"]} for p in products for v in p["variants"]]


class TimeWindowTests(unittest.TestCase):
    def test_within_days_handles_offsets_and_z(self):
        as_of = date(2026, 9, 6)
        self.assertTrue(deltas.within_days("2026-08-31T00:00:00+00:00", as_of, 7))   # window starts 08-31 00:00Z
        self.assertFalse(deltas.within_days("2026-08-30T23:59:59Z", as_of, 7))
        self.assertTrue(deltas.within_days("2026-09-06T19:00:00-04:00", as_of, 7))  # 09-06 23:00Z -> inside
        self.assertFalse(deltas.within_days("2026-09-07T00:00:00Z", as_of, 7))
        self.assertFalse(deltas.within_days(None, as_of, 7))
        self.assertFalse(deltas.within_days("garbage", as_of, 7))

    def test_within_days_local_offset_that_crosses_window_end(self):
        # -04:00 evening on 09-06 is 09-07 03:00Z, i.e. after end of the as_of day in UTC.
        self.assertFalse(deltas.within_days("2026-09-06T23:00:00-04:00", date(2026, 9, 6), 7))


class SingleDayTests(unittest.TestCase):
    def test_one_day_gives_7d_metrics_and_none_deltas(self):
        p1 = load("products_page1.json")
        d = deltas.compute_store_delta(1, "example.com", D1, p1, variants(p1))
        self.assertFalse(d.has_history)
        self.assertEqual(d.products, 3)
        self.assertEqual(d.new_products_7d, 1)      # cloud-hoodie published 08-30
        self.assertEqual(d.updated_products_7d, 2)  # cloud-hoodie 09-05, no-variants 09-01
        self.assertEqual(d.sold_out_variants, 2)
        self.assertIsNone(d.sold_out_variants_delta)
        self.assertIsNone(d.price_changes)
        self.assertIsNone(d.new_handles)
        self.assertEqual(d.changes, [])
        self.assertEqual(d.change_score, 1)   # new_products_7d only; updated_7d excluded


class TwoDayTests(unittest.TestCase):
    def setUp(self):
        self.p1, self.p2 = load("products_page1.json"), load("products_day2.json")
        self.d = deltas.compute_store_delta(1, "example.com", D2, self.p2, variants(self.p2),
                                            D1, self.p1, variants(self.p1))

    def test_counts(self):
        d = self.d
        self.assertTrue(d.has_history)
        self.assertEqual(d.prev_date, D1)
        self.assertEqual(d.new_products_7d, 1)          # launch-drop; cloud-hoodie (08-30) aged out of the window
        self.assertEqual(d.updated_products_7d, 2)      # cloud-hoodie, launch-drop (no-variants is gone)
        self.assertEqual(d.sold_out_variants, 2)
        self.assertEqual(d.sold_out_variants_delta, 0)  # 2 -> 2 overall even though variants flipped
        self.assertEqual(d.price_changes, 2)            # S and M of cloud-hoodie; L unchanged
        self.assertEqual(d.new_handles, 1)
        self.assertEqual(d.removed_handles, 1)
        self.assertEqual(d.change_score, 1 + 0 + 2 + 1 + 1)

    def test_product_changes(self):
        kinds = {(c.kind, c.handle) for c in self.d.changes}
        self.assertIn(("new_handle", "launch-drop"), kinds)
        self.assertIn(("removed", "no-variants"), kinds)
        self.assertIn(("price", "cloud-hoodie"), kinds)
        # sold-out count for cloud-hoodie is unchanged (2 -> 2) so no sold_out/restocked row for it
        self.assertNotIn(("sold_out", "cloud-hoodie"), kinds)
        price = next(c for c in self.d.changes if c.kind == "price")
        self.assertIn("59.00 -> 49.00 (-17%, 2 variant(s))", price.detail)

    def test_new_variant_is_not_a_price_change(self):
        # a variant that didn't exist yesterday must not count
        p2v = variants(self.p2)
        d = deltas.compute_store_delta(1, "x", D2, self.p2, p2v, D1, self.p1, [])
        self.assertEqual(d.price_changes, 0)


class DbRoundTripTests(unittest.TestCase):
    def test_from_db_uses_previous_available_snapshot_not_calendar_yesterday(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "example.com")
        db.write_product_snapshot(conn, sid, "2026-09-01", load("products_page1.json"))
        db.write_product_snapshot(conn, sid, D2, load("products_day2.json"))  # gap of 5 days
        d = deltas.store_delta_from_db(conn, sid, "example.com")
        self.assertEqual((d.snapshot_date, d.prev_date), (D2, "2026-09-01"))
        self.assertEqual(d.price_changes, 2)
        # as_of an earlier date -> single snapshot -> no history
        d1 = deltas.store_delta_from_db(conn, sid, "example.com", as_of="2026-09-01")
        self.assertFalse(d1.has_history)
        # sorting: a second store with no snapshots is skipped, not crashed on
        db.upsert_store(conn, "empty.com")
        rows = deltas.all_store_deltas(conn)
        self.assertEqual([r.store_domain for r in rows], ["example.com"])

    def test_product_history(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "example.com")
        db.write_product_snapshot(conn, sid, D1, load("products_page1.json"))
        db.write_product_snapshot(conn, sid, D2, load("products_day2.json"))
        h = deltas.product_history(conn, "cloud-hoodie")
        self.assertEqual([r["snapshot_date"] for r in h], [D1, D2])
        self.assertEqual([r["min_price"] for r in h], [59.0, 49.0])
        self.assertEqual(deltas.product_history(conn, "nope"), [])


if __name__ == "__main__":
    unittest.main()
