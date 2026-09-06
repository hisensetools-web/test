"""Part A signal logic: channel tags, families, categories, and the tab row builders."""
import unittest
from datetime import datetime, timedelta, timezone

from pathlib import Path

from earlyscale import config, db, signals, sheets

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def ts(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def prod(pid, handle, title, days_ago=100, pos=None, price=39.95, sold_out=0, variants=1):
    return {"product_id": pid, "handle": handle, "title": title, "vendor": "", "product_type": None, "tags": [],
            "created_at": ts(days_ago), "published_at": ts(days_ago), "updated_at": ts(days_ago),
            "variant_count": variants, "sold_out_variants": sold_out, "min_price": price, "max_price": price,
            "collection_position": pos, "variants": [
                {"variant_id": pid * 10 + i, "title": "x", "sku": None, "price": price, "compare_at_price": None,
                 "available": i >= sold_out} for i in range(variants)]}


class ChannelTagTests(unittest.TestCase):
    def test_suffixes(self):
        cases = {
            "ceylon-cinnamon": ("ceylon-cinnamon", ""),
            "ceylon-cinnamon-google": ("ceylon-cinnamon", "google"),
            "ceylon-cinnamon-tt": ("ceylon-cinnamon", "tiktok"),
            "ceylon-cinnamon-tiktok-2": ("ceylon-cinnamon", "tiktok+variant"),
            "ceylon-cinnamon-taboola": ("ceylon-cinnamon", "taboola"),
            "ceylon-cinnamon-fb": ("ceylon-cinnamon", "fb"),
            "ceylon-cinnamon-otp-sub": ("ceylon-cinnamon", "otp+sub"),
            "ceylon-cinnamon-cc": ("ceylon-cinnamon", "coc"),
            "ceylon-cinnamon-coc-vip": ("ceylon-cinnamon", "coc+vip"),
            "ceylon-cinnamon-prev": ("ceylon-cinnamon", "retired"),
            "ceylon-cinnamon-old": ("ceylon-cinnamon", "retired"),
            "ceylon-cinnamon-copy": ("ceylon-cinnamon", "variant"),
            "copy-of-ceylon-cinnamon": ("ceylon-cinnamon", "variant"),
            "copy-of-ceylon-cinnamon-google": ("ceylon-cinnamon", "variant+google"),
            "ceylon-cinnamon-v2": ("ceylon-cinnamon", "variant"),
            "google": ("google", ""),          # a single token is never stripped to nothing
            "old-spice-deodorant": ("old-spice-deodorant", ""),   # tag words only count at the end
        }
        for handle, (base, tag) in cases.items():
            with self.subTest(handle=handle):
                self.assertEqual(signals.split_handle(handle)[0], base)
                self.assertEqual(signals.channel_tag(handle), tag)


class FamilyTests(unittest.TestCase):
    def test_families_by_base_handle_and_title(self):
        ps = [
            prod(1, "ceylon-cinnamon-capsules", "Ceylon Cinnamon Capsules"),
            prod(2, "ceylon-cinnamon-capsules-google", "Ceylon Cinnamon Capsules"),
            prod(3, "ceylon-cinnamon-capsules-tt", "Ceylon Cinnamon Capsules - TikTok"),
            prod(4, "cc-caps", "Ceylon Cinnamon Capsules"),          # different handle, same title -> same family
            prod(5, "oregano-oil", "Oregano Oil Drops"),
            prod(6, "oregano-oil-2", "Oregano Oil Drops (copy)"),
            prod(7, "beef-organ", "Beef Organ Complex"),
        ]
        fam = signals.assign_families(ps)
        self.assertEqual(len({fam[i] for i in (1, 2, 3, 4)}), 1)
        self.assertEqual(fam[1], "ceylon-cinnamon-capsules")
        self.assertEqual(fam[5], fam[6])
        self.assertEqual(fam[5], "oregano-oil")
        self.assertEqual(fam[7], "beef-organ")
        self.assertEqual(len(set(fam.values())), 3)

    def test_normalise_title_strips_noise(self):
        self.assertEqual(signals.normalise_title("Copy of Ceylon Cinnamon - TikTok (2)"), "ceylon cinnamon")
        self.assertEqual(signals.normalise_title("Lion's Mane Capsules"), "lions mane capsules")


class CategoryTests(unittest.TestCase):
    def test_keywords_shared_across_stores_become_categories(self):
        fams = [
            {"store": "a.com", "family": "ceylon-cinnamon", "title": "Ceylon Cinnamon Capsules"},
            {"store": "b.com", "family": "cinnamon-caps", "title": "Organic Ceylon Cinnamon Gummies"},
            {"store": "c.com", "family": "ceylon-x", "title": "Ceylon Cinnamon Extract Premium"},
            {"store": "a.com", "family": "oregano-oil", "title": "Oregano Oil Drops"},
            {"store": "b.com", "family": "oregano", "title": "Wild Oregano Oil"},
            {"store": "c.com", "family": "lymph", "title": "Lymphatic Drainage Support"},
            {"store": "a.com", "family": "one-off", "title": "Zebra Stripes Widget"},
        ]
        cat = signals.derive_categories(fams)
        self.assertEqual(cat[("a.com", "ceylon-cinnamon")], "ceylon cinnamon")
        self.assertEqual(cat[("b.com", "cinnamon-caps")], "ceylon cinnamon")
        self.assertEqual(cat[("c.com", "ceylon-x")], "ceylon cinnamon")
        self.assertEqual(cat[("a.com", "oregano-oil")], "oregano oil")
        self.assertEqual(cat[("b.com", "oregano")], "oregano oil")
        self.assertEqual(cat[("c.com", "lymph")], "")          # only one store -> no category
        self.assertEqual(cat[("a.com", "one-off")], "")
        # "capsules"/"organic"/"premium" never win: they are stopwords
        self.assertNotIn("capsules", cat.values())

    def test_single_store_falls_back_to_family_support(self):
        fams = [{"store": "a.com", "family": f"tart-cherry-{i}", "title": "Tart Cherry Gummies"} for i in range(2)]
        self.assertEqual(signals.derive_categories(fams)[("a.com", "tart-cherry-0")], "tart cherry")


class RowBuilderTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.sid = db.upsert_store(self.conn, "supp.com")
        old = [prod(1, "ceylon-cinnamon", "Ceylon Cinnamon", days_ago=40, pos=5),
               prod(2, "ceylon-cinnamon-google", "Ceylon Cinnamon", days_ago=12, pos=0),
               prod(3, "oregano-oil", "Oregano Oil", days_ago=200, pos=1)]
        db.write_product_snapshot(self.conn, self.sid, "2026-08-29", old)  # 8 days before as_of
        today = [prod(1, "ceylon-cinnamon", "Ceylon Cinnamon", days_ago=40, pos=1),
                 prod(2, "ceylon-cinnamon-google", "Ceylon Cinnamon", days_ago=12, pos=0),
                 prod(4, "ceylon-cinnamon-tt", "Ceylon Cinnamon", days_ago=2, pos=3, sold_out=1),
                 prod(5, "ceylon-cinnamon-fb", "Ceylon Cinnamon", days_ago=0, pos=None),
                 prod(3, "oregano-oil", "Oregano Oil", days_ago=200, pos=2)]
        db.write_product_snapshot(self.conn, self.sid, "2026-09-06", today)

    def test_signals_rows(self):
        rows = sheets.signals_rows(self.conn)
        self.assertEqual([r[2] for r in rows], ["ceylon-cinnamon-fb", "ceylon-cinnamon-tt", "ceylon-cinnamon-google",
                                                "ceylon-cinnamon", "oregano-oil"])  # days_since_published asc
        by = {r[2]: r for r in rows}
        self.assertEqual(len(by["ceylon-cinnamon"]), len(sheets.SIGNALS_HEADERS))
        self.assertEqual(by["ceylon-cinnamon-tt"][3], "tiktok")
        self.assertEqual(by["ceylon-cinnamon-tt"][4], 2)
        self.assertEqual(by["ceylon-cinnamon-tt"][7], "Y")
        self.assertEqual(by["ceylon-cinnamon"][7], "N")
        self.assertEqual(by["ceylon-cinnamon"][8], 2)          # rank is 1-based
        self.assertEqual(by["ceylon-cinnamon"][9], 4)          # was rank 6 a week ago -> climbed 4
        self.assertEqual(by["oregano-oil"][9], -1)             # slipped one place
        self.assertEqual(by["ceylon-cinnamon-fb"][8], "")      # no collection position
        self.assertEqual(by["ceylon-cinnamon-fb"][9], "")
        self.assertEqual(by["ceylon-cinnamon-tt"][9], "")      # didn't exist a week ago
        self.assertEqual(by["ceylon-cinnamon"][10], 2)         # family launched 2 handles this week
        self.assertEqual(by["oregano-oil"][10], 0)
        self.assertEqual(by["ceylon-cinnamon"][11:], [""] * 12)  # Meta + inventory columns empty

    def test_families_and_categories_rows(self):
        fams = sheets.families_rows(self.conn)
        self.assertEqual(fams[0][1], "ceylon-cinnamon")
        self.assertEqual(fams[0][3], 4)
        self.assertEqual(fams[0][6:9], [2, 3, 3])
        self.assertEqual(fams[0][9], 1)
        self.assertIn("ceylon-cinnamon-tt", fams[0][10])
        self.assertEqual(fams[1][1], "oregano-oil")
        cats = sheets.categories_rows(self.conn)
        self.assertEqual([c[0] for c in cats], ["(uncategorised)"])   # one store, no repeated keywords
        self.assertEqual(cats[0][2], 2)

    def test_no_history_for_rank_delta_gives_blank(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        db.write_product_snapshot(conn, sid, "2026-09-06", [prod(1, "a", "A", pos=0)])
        self.assertEqual(sheets.signals_rows(conn)[0][9], "")


if __name__ == "__main__":
    unittest.main()
