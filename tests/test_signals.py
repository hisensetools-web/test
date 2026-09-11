"""Part A signal logic: channel tags, families, categories, and the tab row builders."""
import unittest
from datetime import datetime, timedelta, timezone

from pathlib import Path

from earlyscale import config, db, signals, sheets

config.WATCHLIST_PATH = Path(__file__).parent / "fixtures" / "empty_watchlist.csv"

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def ts(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def prod(pid, handle, title, days_ago=100, pos=None, price=39.95, sold_out=0, variants=1, created_days_ago=None):
    return {"product_id": pid, "handle": handle, "title": title, "vendor": "", "product_type": None, "tags": [],
            "created_at": ts(days_ago if created_days_ago is None else created_days_ago), "published_at": ts(days_ago), "updated_at": ts(days_ago),
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
        H = sheets.SIGNALS_HEADERS
        c = H.index
        self.assertEqual([r[2] for r in rows], ["ceylon-cinnamon-fb", "ceylon-cinnamon-tt", "ceylon-cinnamon-google",
                                                "ceylon-cinnamon", "oregano-oil"])  # days_since_created asc
        by = {r[2]: r for r in rows}
        self.assertEqual(len(by["ceylon-cinnamon"]), len(H))
        self.assertEqual(by["ceylon-cinnamon-tt"][c("channel tag")], "tiktok")
        self.assertEqual(by["ceylon-cinnamon-tt"][c("days_since_published")], 2)
        self.assertEqual(by["ceylon-cinnamon-tt"][c("days_since_created")], 2)
        self.assertEqual(by["ceylon-cinnamon-tt"][c("relaunch")], "")
        self.assertEqual(by["ceylon-cinnamon-tt"][c("sold_out")], "Y")
        self.assertEqual(by["ceylon-cinnamon"][c("sold_out")], "N")
        self.assertEqual(by["ceylon-cinnamon"][c("collection_rank")], 2)          # rank is 1-based
        self.assertEqual(by["ceylon-cinnamon"][c("collection_rank_delta_7d")], 4)  # was rank 6 a week ago -> climbed 4
        self.assertEqual(by["oregano-oil"][c("collection_rank_delta_7d")], -1)     # slipped one place
        self.assertEqual(by["ceylon-cinnamon-fb"][c("collection_rank")], "")       # no collection position
        self.assertEqual(by["ceylon-cinnamon-fb"][c("collection_rank_delta_7d")], "")
        self.assertEqual(by["ceylon-cinnamon-tt"][c("collection_rank_delta_7d")], "")   # didn't exist a week ago
        self.assertEqual(by["ceylon-cinnamon"][c("variants_of_family_published_7d")], 2)   # family launched 2 handles this week
        self.assertEqual(by["oregano-oil"][c("variants_of_family_published_7d")], 0)
        self.assertEqual(by["ceylon-cinnamon"][c("ads_pointing_here"):], [""] * (len(H) - c("ads_pointing_here")))  # Meta + velocity + pages + inventory + badge empty

    def test_relaunch_uses_created_at_for_freshness(self):
        """Holior's wormwood: created 5 months ago, (re)published 26 days ago -> old product, flagged relaunch, ranked old."""
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "holior.com")
        db.write_product_snapshot(conn, sid, "2026-09-06", [prod(1, "wormwood", "Wormwood", days_ago=26, created_days_ago=155, pos=0),
                                                     prod(2, "fresh-drop", "Fresh Drop", days_ago=40, pos=1),
                                                     prod(3, "tweaked", "Tweaked", days_ago=10, created_days_ago=30, pos=2)])
        rows = sheets.signals_rows(conn)
        H = sheets.SIGNALS_HEADERS
        by = {r[2]: r for r in rows}
        self.assertEqual((by["wormwood"][H.index("days_since_published")], by["wormwood"][H.index("days_since_created")], by["wormwood"][H.index("relaunch")]),
                         (26, 155, "relaunch"))
        self.assertEqual(by["wormwood"][H.index("created_at")][:10], "2026-04-04")
        self.assertEqual(by["tweaked"][H.index("relaunch")], "")            # 20 days apart: within the 30-day gap
        self.assertEqual([r[2] for r in rows], ["tweaked", "fresh-drop", "wormwood"])   # youngest by created_at first

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
        self.assertEqual(sheets.signals_rows(conn)[0][sheets.SIGNALS_HEADERS.index("collection_rank_delta_7d")], "")


if __name__ == "__main__":
    unittest.main()


class CategoriesUndatedTests(unittest.TestCase):
    def test_a_category_with_no_dated_family_sorts_without_crashing(self):
        # 2026-09-11: the morning sync died in the sort key ("<" between str and int) once new stores arrived whose
        # catalogue carries no published dates
        from earlyscale import signals
        fams = [["a.com", "gummy", "Gummy", 1, "", "", 0, 0, 0, 1, "gummy"],
                ["b.com", "gummy", "Gummy", 1, "2026-09-01T00:00:00Z", "", 1, 1, 1, 1, "gummy"],
                ["c.com", "zzz-thing", "Thing", 1, "", "", 0, 0, 0, 1, "zzz-thing"]]
        rows = signals.categories_rows(fams)
        self.assertTrue(rows)
        self.assertTrue(all(isinstance(r[3], str) for r in rows))
