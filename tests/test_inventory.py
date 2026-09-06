"""Inventory-delta sales tracking: hero selection, 422 parsing, theme fallback, fallback chain,
units-sold maths (restocks excluded, gaps handled), alerts 9-11 and the Signals columns."""
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from earlyscale import config, db, inventory, sheets, signals

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"

TODAY = "2026-09-06"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def prod(pid, handle, title, days_ago=100, pos=None, price=39.95, ptype=None, tags=(), variants=1, available=True):
    return {"product_id": pid, "handle": handle, "title": title, "vendor": "", "product_type": ptype, "tags": list(tags),
            "created_at": ts(days_ago), "published_at": ts(days_ago), "updated_at": ts(days_ago),
            "variant_count": variants, "sold_out_variants": 0, "min_price": price, "max_price": price,
            "collection_position": pos, "variants": [
                {"variant_id": pid * 10 + i, "title": "Default Title" if variants == 1 else f"v{i}", "sku": None,
                 "price": price, "compare_at_price": None, "available": available} for i in range(variants)]}


NORES = lambda domain: "https://" + domain   # noqa: E731 - no network in unit tests


def d(days_ago):
    return (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()


class HeroSelectionTests(unittest.TestCase):
    def test_top_rank_plus_new_minus_widgets(self):
        ps = [prod(i, f"old-{i}", f"Old {i}", days_ago=200, pos=i) for i in range(1, 21)]   # 20 ranked, old
        ps += [prod(50, "fresh-drop", "Fresh Drop", days_ago=3, pos=40),                     # new, badly ranked
               prod(51, "shipping-protection", "Shipping Protection", days_ago=1, pos=1, price=1.98),
               prod(52, "gift-card", "Gift Card", days_ago=2, pos=2),
               prod(53, "vip-membership", "VIP Membership", days_ago=2, pos=3),
               prod(54, "guide", "Recipe Guide", days_ago=2, ptype="Digital Download"),
               prod(55, "free-sample", "Free Sample", days_ago=2, price=0),
               prod(56, "starter-kit", "Starter Kit", days_ago=5, pos=4, variants=2)]
        heroes = inventory.select_heroes(ps, date.fromisoformat(TODAY), top_n=15, new_days=30, max_variants=100)
        handles = [h["handle"] for h in heroes]
        for bad in ("shipping-protection", "gift-card", "vip-membership", "guide", "free-sample"):
            self.assertNotIn(bad, handles)
        self.assertEqual(handles[:3], ["starter-kit", "starter-kit", "fresh-drop"])   # new first, by rank
        self.assertEqual(heroes[0]["role"], "new")
        self.assertTrue(heroes[0]["bundle_like"])
        self.assertFalse(heroes[2]["bundle_like"])
        ranked = [h for h in heroes if h["role"] == "rank"]
        self.assertEqual(len(ranked), 15 - 4)   # top 15 positions minus the 4 excluded / new ones occupying 1-4
        self.assertNotIn("old-16", handles)

    def test_max_variants_cap(self):
        ps = [prod(i, f"p-{i}", f"P {i}", days_ago=2, variants=5) for i in range(20)]
        self.assertEqual(len(inventory.select_heroes(ps, date.fromisoformat(TODAY), max_variants=7)), 7)


class ParseTests(unittest.TestCase):
    def test_cart_error_messages(self):
        cases = [
            ('{"status":422,"message":"Cart Error","description":"You can only add 43 Ceylon Cinnamon to the cart."}', ("count", 43)),
            ('{"status":422,"message":"Cart Error","description":"Only 1,204 items were added to your cart due to availability."}', ("count", 1204)),
            ('{"status":422,"message":"Cart Error","description":"All 12 Oregano Oil are in your cart."}', ("count", 12)),
            ('{"status":422,"message":"Cart Error","description":"The product \'Sea Moss\' is already sold out."}', ("sold_out", 0)),
            ('{"status":422,"description":"Out of stock"}', ("sold_out", 0)),
            ("<html>Access denied</html>", ("unknown", None)),
            ('{"status":422,"message":"Cart Error","description":"Invalid request"}', ("unknown", None)),
        ]
        for text, want in cases:
            with self.subTest(text=text[:40]):
                self.assertEqual(inventory.parse_cart_error(text), want)

    def test_theme_inventory(self):
        html = ('<script id="ProductJson" type="application/json">{"variants":[{"id":111,"title":"S","price":2995,'
                '"inventory_quantity":7,"inventory_management":"shopify"},{"id":222,"title":"M","inventory_quantity":-3}]}</script>')
        self.assertEqual(inventory.theme_inventory_from_html(html, 111), 7)
        self.assertEqual(inventory.theme_inventory_from_html(html, 222), -3)
        self.assertIsNone(inventory.theme_inventory_from_html(html, 333))
        self.assertIsNone(inventory.theme_inventory_from_html('{"variants":[{"id":111,"price":2995}]}', 111))
        self.assertEqual(inventory.theme_inventory_from_html('<div data-variant-id="444" data-inventory-quantity="19">', 444), 19)

    def test_component_ids(self):
        html = '<div data-variant-id="70000000000001"></div><script>[{"variant_id": 70000000000002}, {"id": 70000000000009}]</script>'
        got = inventory.component_variant_ids(html, own_variant_ids={70000000000009},
                                              known_variant_ids={70000000000001, 70000000000002, 70000000000009, 5})
        self.assertEqual(got, [70000000000001, 70000000000002])


class SalesMathTests(unittest.TestCase):
    def test_drop_is_sales_rise_is_restock(self):
        r = [(d(2), 100), (d(1), 90), (d(0), 84)]
        s = inventory.sales_from_readings(r, TODAY)
        self.assertEqual(s["units_sold_1d"], 6)
        self.assertEqual(s["restock_units"], 0)
        self.assertEqual(s["prev_date"], d(1))
        s = inventory.sales_from_readings([(d(1), 10), (d(0), 160)], TODAY)
        self.assertEqual(s["units_sold_1d"], 0)
        self.assertEqual(s["restock_units"], 150)

    def test_gap_uses_previous_available_reading(self):
        r = [(d(3), 100), (d(2), None), (d(1), None), (d(0), 70)]
        s = inventory.sales_from_readings(r, TODAY)
        self.assertEqual(s["units_sold_1d"], 30)
        self.assertEqual(s["prev_date"], d(3))
        self.assertEqual(s["units_per_day_7d"], 10.0)   # 30 units over a 3-day span

    def test_weekly_windows_and_wow_exclude_restocks(self):
        # prior week: 2/day; this week: 6/day with a restock in the middle that must not count as negative sales
        r = [(d(14), 200), (d(13), 198), (d(12), 196), (d(11), 194), (d(10), 192), (d(9), 190), (d(8), 188), (d(7), 186),
             (d(6), 180), (d(5), 174), (d(4), 168), (d(3), 300), (d(2), 294), (d(1), 288), (d(0), 282)]
        s = inventory.sales_from_readings(r, TODAY)
        self.assertEqual(s["units_per_day_prev_7d"], 2.0)
        self.assertEqual(s["units_per_day_7d"], 6.0)   # restock interval dropped from units and days
        self.assertEqual(s["units_per_day_wow"], 3.0)
        self.assertEqual(s["restock_units"], 0)
        self.assertEqual(inventory.sales_from_readings(r[:-3], d(3))["restock_units"], 132)

    def test_too_few_readings(self):
        self.assertIsNone(inventory.sales_from_readings([(d(0), 5)], TODAY)["units_sold_1d"])
        self.assertIsNone(inventory.sales_from_readings([(d(1), 5), (d(0), None)], TODAY)["units_sold_1d"])


def seed_store(conn, products, day=TODAY):
    sid = db.upsert_store(conn, "shop.example.com")
    db.write_product_snapshot(conn, sid, day, products)
    conn.commit()
    return sid


class FakeProbe:
    """Scripted /cart/add.js answers per variant id, and a log of what was asked."""
    def __init__(self, answers, pages=None):
        self.answers, self.pages, self.calls, self.page_calls = answers, pages or {}, [], []

    def probe(self, base, vid, handle=None):
        self.calls.append(vid)
        a = self.answers.get(vid, {"status": "untracked", "stock": None, "message": "add ok", "http": 200})
        return dict(a)

    def page(self, base, handle):
        self.page_calls.append(handle)
        return self.pages.get(handle, "")


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def test_fallback_chain_and_ads_only_is_sticky(self):
        ps = [prod(1, "hero-a", "Hero A", days_ago=5, pos=1),        # 422 count
              prod(2, "hero-b", "Hero B", days_ago=6, pos=2),        # sold out
              prod(3, "hero-c", "Hero C", days_ago=7, pos=3),        # add ok, theme shows inventory
              prod(4, "hero-d", "Hero D", days_ago=8, pos=4),        # add ok, nothing on page -> ads_only
              prod(5, "hero-e", "Hero E", days_ago=9, pos=5)]        # blocked
        sid = seed_store(self.conn, ps)
        fake = FakeProbe({10: {"status": "count", "stock": 43, "message": "only add 43", "http": 422},
                          20: {"status": "sold_out", "stock": 0, "message": "sold out", "http": 422},
                          50: {"status": "blocked", "stock": None, "message": "HTTP 403", "http": 403}},
                         pages={"hero-c": '{"id": 30, "inventory_quantity": 12}', "hero-d": "<html>nothing</html>"})
        counts = inventory.probe_store(self.conn, sid, "shop.example.com", TODAY, probe=fake.probe, page_fetch=fake.page, pause=lambda: None, resolve=NORES)
        self.assertEqual({k: counts[k] for k in ("cart_probe", "theme_inventory", "ads_only", "blocked")},
                         {"cart_probe": 2, "theme_inventory": 1, "ads_only": 1, "blocked": 1})
        self.assertEqual(fake.page_calls, ["hero-c", "hero-d"])   # page only fetched when the cart probe was inconclusive
        rows = {r["variant_id"]: dict(r) for r in self.conn.execute("SELECT * FROM inventory_daily WHERE snapshot_date = ?", (TODAY,))}
        self.assertEqual((rows[10]["stock_level"], rows[10]["signal_source"]), (43, "cart_probe"))
        self.assertEqual((rows[20]["stock_level"], rows[20]["signal_source"]), (0, "cart_probe"))
        self.assertEqual((rows[30]["stock_level"], rows[30]["signal_source"]), (12, "theme_inventory"))
        self.assertEqual((rows[40]["stock_level"], rows[40]["signal_source"]), (None, "ads_only"))
        self.assertEqual((rows[50]["stock_level"], rows[50]["signal_source"]), (None, "blocked"))
        hv = {r["variant_id"]: dict(r) for r in self.conn.execute("SELECT * FROM hero_variants")}
        self.assertEqual(hv[40]["inventory_tracked"], 0)
        self.assertIsNone(hv[50]["last_probe_date"])   # blocked = not probed, retried tomorrow
        self.assertEqual(hv[50]["consecutive_failures"], 1)
        # same day again: nothing re-probed
        fake.calls.clear()
        counts = inventory.probe_store(self.conn, sid, "shop.example.com", TODAY, probe=fake.probe, page_fetch=fake.page, pause=lambda: None, resolve=NORES)
        self.assertEqual(fake.calls, [50])   # only the blocked one is retried
        self.assertEqual(counts["skipped"], 4)
        # next day: ads_only variant is not probed again, others are
        tomorrow = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()
        db.write_product_snapshot(self.conn, sid, tomorrow, ps)
        fake.calls.clear()
        fake.answers[10]["stock"] = 40
        inventory.probe_store(self.conn, sid, "shop.example.com", tomorrow, probe=fake.probe, page_fetch=fake.page, pause=lambda: None, resolve=NORES)
        self.assertEqual(sorted(fake.calls), [10, 20, 30, 50])
        r = self.conn.execute("SELECT * FROM inventory_daily WHERE variant_id = 10 AND snapshot_date = ?", (tomorrow,)).fetchone()
        self.assertEqual((r["units_sold_1d"], r["prev_reading_date"]), (3, TODAY))
        r = self.conn.execute("SELECT signal_source FROM inventory_daily WHERE variant_id = 40 AND snapshot_date = ?", (tomorrow,)).fetchone()
        self.assertEqual(r["signal_source"], "ads_only")

    def test_store_gives_up_after_consecutive_blocks(self):
        ps = [prod(i, f"p-{i}", f"P {i}", days_ago=100, pos=i) for i in range(1, 11)]
        sid = seed_store(self.conn, ps)
        fake = FakeProbe({i * 10: {"status": "blocked", "stock": None, "message": "HTTP 430", "http": 430} for i in range(1, 11)})
        counts = inventory.probe_store(self.conn, sid, "shop.example.com", TODAY, probe=fake.probe, page_fetch=fake.page, pause=lambda: None, resolve=NORES)
        self.assertEqual(counts["blocked"], 10)
        self.assertEqual(len(fake.calls), config.INVENTORY_MAX_BLOCKED)   # the rest were recorded without a request
        msgs = [r[0] for r in self.conn.execute("SELECT raw_message FROM inventory_daily WHERE signal_source = 'blocked'")]
        self.assertEqual(sum("not probed" in m for m in msgs), 10 - config.INVENTORY_MAX_BLOCKED)
        # a success in between resets the streak
        fake = FakeProbe({10: {"status": "blocked", "stock": None, "message": "x", "http": 430},
                          20: {"status": "count", "stock": 5, "message": "", "http": 422},
                          30: {"status": "blocked", "stock": None, "message": "x", "http": 430},
                          40: {"status": "blocked", "stock": None, "message": "x", "http": 430},
                          50: {"status": "count", "stock": 7, "message": "", "http": 422}})
        conn = db.connect(":memory:")
        sid = seed_store(conn, ps[:5])
        counts = inventory.probe_store(conn, sid, "shop.example.com", TODAY, probe=fake.probe, page_fetch=fake.page, pause=lambda: None, resolve=NORES)
        self.assertEqual((counts["cart_probe"], counts["blocked"]), (2, 3))

    def test_throttle_waits_retry_after_then_retries_once(self):
        ps = [prod(i, f"p-{i}", f"P {i}", days_ago=100, pos=i) for i in range(1, 4)]
        sid = seed_store(self.conn, ps)
        script = {10: [{"status": "throttled", "stock": None, "message": "429", "http": 429, "retry_after": 45},
                       {"status": "count", "stock": 9, "message": "", "http": 422}],
                  20: [{"status": "count", "stock": 4, "message": "", "http": 422}],
                  30: [{"status": "throttled", "stock": None, "message": "429", "http": 429, "retry_after": None},
                       {"status": "throttled", "stock": None, "message": "429", "http": 429, "retry_after": None}]}
        slept = []

        def probe(base, vid, handle=None):
            return dict(script[vid].pop(0))
        counts = inventory.probe_store(self.conn, sid, "shop.example.com", TODAY, probe=probe, page_fetch=lambda b, h: "",
                                       pause=lambda: None, resolve=NORES, sleep=slept.append)
        self.assertEqual(slept, [45, config.INVENTORY_THROTTLE_WAIT])
        self.assertEqual((counts["cart_probe"], counts["blocked"]), (2, 1))
        r = self.conn.execute("SELECT stock_level FROM inventory_daily WHERE variant_id = 10").fetchone()
        self.assertEqual(r[0], 9)

    def test_bundle_components_are_probed(self):
        ps = [prod(1, "cinnamon", "Ceylon Cinnamon", days_ago=100, pos=1),
              prod(2, "oregano", "Oregano Oil", days_ago=100, pos=2),
              prod(3, "starter-bundle", "Starter Bundle", days_ago=2, pos=9)]
        sid = seed_store(self.conn, ps)
        fake = FakeProbe({10: {"status": "count", "stock": 50, "message": "", "http": 422},
                          20: {"status": "count", "stock": 60, "message": "", "http": 422}},
                         pages={"starter-bundle": '<div data-variant-id="10"></div><script>[{"variant_id": 20}]</script>'})
        counts = inventory.probe_store(self.conn, sid, "shop.example.com", TODAY, probe=fake.probe, page_fetch=fake.page, pause=lambda: None, resolve=NORES)
        self.assertEqual(counts["components"], 0)   # both components were already heroes (ranked 1 and 2)
        # bundle whose components are NOT heroes themselves
        ps2 = [prod(i, f"filler-{i}", f"Filler {i}", days_ago=100, pos=i) for i in range(1, 16)] + \
              [prod(30, "starter-bundle", "Starter Bundle", days_ago=2, pos=40),
               prod(31, "cinnamon", "Ceylon Cinnamon", days_ago=100, pos=41)]
        conn = db.connect(":memory:")
        sid = seed_store(conn, ps2)
        fake = FakeProbe({310: {"status": "count", "stock": 77, "message": "", "http": 422}},
                         pages={"starter-bundle": '<div data-variant-id="310"></div>'})
        counts = inventory.probe_store(conn, sid, "shop.example.com", TODAY, probe=fake.probe, page_fetch=fake.page, pause=lambda: None, resolve=NORES)
        self.assertEqual(counts["components"], 1)
        self.assertIn(310, fake.calls)
        r = conn.execute("SELECT role, signal_source FROM hero_variants WHERE variant_id = 310").fetchone()
        self.assertEqual((r["role"], r["signal_source"]), ("bundle_component", "cart_probe"))


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def _readings(self, sid, vid, pid, series):
        for days_ago, stock in series:
            inventory.record_reading(self.conn, sid, d(days_ago), vid, pid, stock, "cart_probe", "")
        for days_ago in sorted({x for x, _ in series}, reverse=True):
            inventory.compute_sales(self.conn, sid, d(days_ago))

    def test_rules_9_10_11(self):
        ps = [prod(1, "rocket", "Rocket", days_ago=12, pos=1),
              prod(2, "steady", "Steady", days_ago=300, pos=2),
              prod(3, "newcomer", "Newcomer", days_ago=200, pos=12),   # outside the store's top 10
              prod(4, "gone", "Gone", days_ago=9, pos=4)]
        sid = seed_store(self.conn, ps)
        for p in ps:
            self.conn.execute("INSERT INTO hero_variants (store_id, variant_id, product_id, handle, role, inventory_tracked, signal_source, selected_at) VALUES (?,?,?,?,?,1,'cart_probe','2026-09-01')",
                              (sid, p["product_id"] * 10, p["product_id"], p["handle"], "rank"))
        # rocket: 3/day last week -> 8/day this week (x2.67), 12 days old -> rule 9
        self._readings(sid, 10, 1, [(i, 500 - (3 * (14 - i) if i >= 7 else 21 + 8 * (7 - i))) for i in range(14, -1, -1)])
        # steady: 60/day both weeks, in last week's top 10 -> nothing
        self._readings(sid, 20, 2, [(i, 5000 - 60 * (14 - i)) for i in range(14, -1, -1)])
        # newcomer: 1/day last week -> 55/day this week, old product, ranked 12th -> rule 10 (not rule 9, > 30 days)
        # steady sells 60/day too but sits at rank 2 -> no rule 10
        self._readings(sid, 30, 3, [(i, 2000 - (1 * (14 - i) if i >= 7 else 7 + 55 * (7 - i))) for i in range(14, -1, -1)])
        # gone: 20 -> 0 within 14 days of publish -> rule 11
        self._readings(sid, 40, 4, [(1, 20), (0, 0)])
        found = inventory.run_alerts(self.conn, sid, "shop.example.com", TODAY)
        got = {(f["rule"], f["handle"]) for f in found}
        self.assertEqual(got, {(9, "rocket"), (10, "newcomer"), (11, "gone")})
        self.assertIn("x2.67", next(f["detail"] for f in found if f["rule"] == 9))
        # idempotent, and a re-run the next day does not repeat rule 11 (one-off) nor 9/10 (weekly)
        self.assertEqual(inventory.run_alerts(self.conn, sid, "shop.example.com", TODAY), [])
        tomorrow = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()
        self.assertEqual(inventory.run_alerts(self.conn, sid, "shop.example.com", tomorrow), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 3)
        rows = sheets.alerts_rows(self.conn, TODAY)
        self.assertEqual(sorted(r[3] for r in rows), [9, 10, 11])

    def test_signals_columns(self):
        ps = [prod(1, "rocket", "Rocket", days_ago=12, pos=1), prod(2, "dark", "Dark", days_ago=3, pos=2)]
        sid = seed_store(self.conn, ps)
        self.conn.execute("INSERT INTO hero_variants (store_id, variant_id, product_id, handle, role, inventory_tracked, signal_source, selected_at) VALUES (?,?,?,?,?,1,'cart_probe','2026-09-01')",
                          (sid, 10, 1, "rocket", "rank"))
        self.conn.execute("INSERT INTO hero_variants (store_id, variant_id, product_id, handle, role, inventory_tracked, signal_source, selected_at) VALUES (?,?,?,?,?,0,'ads_only','2026-09-01')",
                          (sid, 20, 2, "dark", "new"))
        self._readings(sid, 10, 1, [(1, 100), (0, 91)])
        inventory.record_reading(self.conn, sid, TODAY, 20, 2, None, "ads_only", "")
        inv = inventory.inventory_for_signals(self.conn, sid, TODAY)
        self.assertEqual(inv["rocket"], {"signal_source": "cart_probe", "inventory_tracked": "Y", "stock_level": 91,
                                         "units_sold_1d": 9, "units_per_day_7d": 9.0, "units_per_day_wow": None})
        self.assertEqual(inv["dark"]["signal_source"], "ads_only")
        self.assertEqual(inv["dark"]["inventory_tracked"], "N")
        rows = sheets.signals_rows(self.conn, TODAY)
        by = {r[2]: r for r in rows}
        i = sheets.SIGNALS_HEADERS.index("signal_source")
        self.assertEqual(by["rocket"][i:i + 6], ["cart_probe", "Y", 91, 9, 9.0, ""])
        self.assertEqual(by["dark"][i:i + 6], ["ads_only", "N", "", "", "", ""])

    def test_signals_sort_young_by_wow(self):
        ps = [prod(1, "old-fast", "Old Fast", days_ago=200, pos=1), prod(2, "young-slow", "Young Slow", days_ago=5, pos=2),
              prod(3, "young-fast", "Young Fast", days_ago=9, pos=3), prod(4, "young-none", "Young None", days_ago=2, pos=4)]
        sid = seed_store(self.conn, ps)
        for p in ps:
            self.conn.execute("INSERT INTO hero_variants (store_id, variant_id, product_id, handle, role, inventory_tracked, signal_source, selected_at) VALUES (?,?,?,?,?,1,'cart_probe','2026-09-01')",
                              (sid, p["product_id"] * 10, p["product_id"], p["handle"], "rank"))
        for vid, pid, rate2 in ((10, 1, 30), (20, 2, 3), (30, 3, 9)):
            self._readings(sid, vid, pid, [(i, 5000 - (2 * (14 - i) if i >= 7 else 14 + rate2 * (7 - i))) for i in range(14, -1, -1)])
        rows = sheets.signals_rows(self.conn, TODAY)
        self.assertEqual([r[2] for r in rows], ["young-fast", "young-slow", "young-none", "old-fast"])


class ProbeHttpTests(unittest.TestCase):
    """probe_variant against a local fake /cart/add.js (no real store is touched)."""
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import HTTPServer
        from tests import mock_store
        now = datetime.now(timezone.utc)
        products = mock_store.build_supplement_catalog(10, 5, now)
        cls.stock = mock_store.stock_model(products, 5, 1)
        cls.handler = mock_store.make_handler(products, products, None, stock=cls.stock)
        cls.srv = HTTPServer(("127.0.0.1", 0), cls.handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.products = products
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _vid(self, mode):
        return next(v for v, st in self.stock.items() if st["mode"] == mode and st["stock"] > 0)

    def test_count_untracked_not_found_and_never_checkout(self):
        vid = self._vid("cart")
        r = inventory.probe_variant(self.base, vid, self.stock[vid]["handle"])
        self.assertEqual((r["status"], r["stock"], r["http"]), ("count", self.stock[vid]["stock"], 422))
        vid = self._vid("none")
        r = inventory.probe_variant(self.base, vid)
        self.assertEqual(r["status"], "untracked")
        self.assertEqual(inventory.probe_variant(self.base, 123)["status"], "not_found")
        st = self.handler.stats()
        self.assertEqual(st["checkout_hits"], 0)
        self.assertGreaterEqual(st["cart_clears"], 1)   # the successful add was cleared

    def test_theme_page(self):
        vid = self._vid("theme")
        html = inventory.fetch_product_page(self.base, self.stock[vid]["handle"])
        self.assertEqual(inventory.theme_inventory_from_html(html, vid), self.stock[vid]["stock"])

    def test_throttled_status_and_one_session_per_store(self):
        from http.server import HTTPServer
        import threading
        from tests import mock_store
        h = mock_store.make_handler(self.products, self.products, None, stock=self.stock, throttle_after=1)
        srv = HTTPServer(("127.0.0.1", 0), h)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            sess = inventory.fresh_session(base)
            vid = self._vid("cart")
            self.assertEqual(inventory.probe_variant(base, vid, session=sess)["status"], "count")
            r = inventory.probe_variant(base, vid, session=sess)
            self.assertEqual((r["status"], r["retry_after"]), ("throttled", 1))
            self.assertIn("title=Throttled", r["message"])
            # probe_store on a fresh DB: waits Retry-After and retries; the store still gets classified
            conn = db.connect(":memory:")
            sid = db.upsert_store(conn, base)
            from earlyscale import shopify
            products = shopify.normalise_products(self.products[:3])
            db.write_product_snapshot(conn, sid, TODAY, products)
            counts = inventory.probe_store(conn, sid, base, TODAY, pause=lambda: None, resolve=lambda d: base, sleep=lambda s: None)
            self.assertGreaterEqual(counts["cart_probe"] + counts["theme_inventory"] + counts["ads_only"], 1)
            st = h.stats()
            self.assertEqual(st["checkout_hits"], 0)
        finally:
            srv.shutdown()

    def test_blocked(self):
        from http.server import HTTPServer
        import threading
        from tests import mock_store
        srv = HTTPServer(("127.0.0.1", 0), mock_store.make_handler(self.products, self.products, None, stock=self.stock, cart_status=403))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = inventory.probe_variant(f"http://127.0.0.1:{srv.server_address[1]}", self._vid("cart"))
            self.assertEqual(r["status"], "blocked")
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
