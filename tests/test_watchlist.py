"""watchlist.csv domain normalisation: whatever is pasted (URL, www., upper case) becomes the bare store domain."""
import unittest

from earlyscale.watchlist import normalise_domain


class NormaliseDomainTests(unittest.TestCase):
    def test_urls_become_bare_domains(self):
        self.assertEqual(normalise_domain("https://jevawell.com/products/kgg"), "jevawell.com")
        self.assertEqual(normalise_domain("HTTPS://WWW.Pipitea.com/"), "pipitea.com")
        self.assertEqual(normalise_domain("shop.pipitea.com"), "shop.pipitea.com")
        self.assertEqual(normalise_domain("example.com?utm=1"), "example.com")

    def test_local_mock_origin_keeps_scheme_and_port(self):
        self.assertEqual(normalise_domain("http://127.0.0.1:8001/products.json"), "http://127.0.0.1:8001")
        self.assertEqual(normalise_domain("http://example.com/x"), "example.com")


if __name__ == "__main__":
    unittest.main()


class StoresTxtTests(unittest.TestCase):
    def test_txt_round_trip_and_migration_from_csv(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from earlyscale import config
        from earlyscale.watchlist import append_to_watchlist, read_watchlist, remove_from_watchlist, update_watchlist_entry
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "stores.txt"
            p.write_text("# my stores\nhttps://www.One.com/x\ntwo.com   page=123456789012   # persona pinned\n\nthree.com # new\none.com\n", encoding="utf-8")
            rows = read_watchlist(p)
            self.assertEqual([(r["store_domain"], r["meta_page_id"], r["notes"]) for r in rows],
                             [("one.com", "", ""), ("two.com", "123456789012", "persona pinned"), ("three.com", "", "new")])
            self.assertTrue(append_to_watchlist({"store_domain": "Four.com", "notes": "added"}, p))
            self.assertFalse(append_to_watchlist({"store_domain": "two.com"}, p))
            self.assertEqual(remove_from_watchlist(["three.com", "nope.com"], p), (["three.com"], ["nope.com"]))
            self.assertTrue(update_watchlist_entry("one.com", p, meta_page_id="999999999999"))
            text = p.read_text(encoding="utf-8")
            self.assertIn("# my stores", text)                                   # comments survive rewrites
            self.assertIn("two.com   page=123456789012   # persona pinned", text)   # untouched lines stay as typed
            self.assertIn("four.com   # added", text)
            self.assertNotIn("three.com", text)
            self.assertEqual([(r["store_domain"], r["meta_page_id"]) for r in read_watchlist(p)],
                             [("one.com", "999999999999"), ("two.com", "123456789012"), ("four.com", "")])
            # first run on a machine that only has watchlist.csv: stores.txt is created from it
            new = Path(d) / "sub" / "stores.txt"
            new.parent.mkdir()
            (new.parent / "watchlist.csv").write_text("store_domain,meta_page_name,meta_page_id,notes\nold.com,Old Page,555555555555,kept\nolder.com,,,\n", encoding="utf-8")
            with mock.patch.object(config, "WATCHLIST_PATH", new):
                rows = read_watchlist()
            self.assertTrue(new.exists())
            self.assertEqual([(r["store_domain"], r["meta_page_id"], r["notes"]) for r in rows], [("old.com", "555555555555", "kept"), ("older.com", "", "")])

