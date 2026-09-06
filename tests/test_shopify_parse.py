import json
import unittest
from pathlib import Path

from earlyscale import shopify

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


class NormaliseTests(unittest.TestCase):
    def setUp(self):
        raw = load("products_page1.json")["products"]
        coll = load("collection_all.json")["products"]
        positions = {p["id"]: i for i, p in enumerate(coll)}
        self.products = shopify.normalise_products(raw, positions)
        self.by_handle = {p["handle"]: p for p in self.products}

    def test_malformed_record_is_skipped_not_fatal(self):
        self.assertEqual(sorted(self.by_handle), ["cloud-hoodie", "no-variants", "ridge-wallet"])

    def test_variant_aggregates(self):
        p = self.by_handle["cloud-hoodie"]
        self.assertEqual(p["variant_count"], 3)
        self.assertEqual(p["sold_out_variants"], 2)
        self.assertEqual(p["min_price"], 59.0)
        self.assertEqual(p["max_price"], 64.0)
        self.assertEqual(p["tags"], ["new", "core"])
        self.assertEqual(p["collection_position"], 1)
        v = {v["variant_id"]: v for v in p["variants"]}
        self.assertEqual(v[1012]["compare_at_price"], 79.0)
        self.assertIsNone(v[1013]["sku"])  # empty string sku -> NULL
        self.assertFalse(v[1013]["available"])

    def test_string_tags_and_empty_type(self):
        p = self.by_handle["ridge-wallet"]
        self.assertEqual(p["tags"], ["leather", "bestseller"])
        self.assertIsNone(p["product_type"])
        self.assertEqual(p["collection_position"], 0)

    def test_product_without_variants(self):
        p = self.by_handle["no-variants"]
        self.assertEqual(p["variant_count"], 0)
        self.assertEqual(p["sold_out_variants"], 0)
        self.assertIsNone(p["min_price"])
        self.assertIsNone(p["published_at"])
        self.assertIsNone(p["collection_position"])


class UrlAndDiscoveryTests(unittest.TestCase):
    def test_base_url_forms(self):
        self.assertEqual(shopify.base_url("example.com"), "https://example.com")
        self.assertEqual(shopify.base_url("https://www.example.com/"), "https://www.example.com")
        self.assertEqual(shopify.base_url("http://127.0.0.1:8001"), "http://127.0.0.1:8001")

    def test_extract_meta_page_skips_share_links(self):
        html = ('<a href="https://www.facebook.com/sharer/sharer.php?u=x">s</a>'
                '<a href="https://facebook.com/plugins/like.php">l</a>'
                '<a href="https://www.facebook.com/AcmeOfficial/">fb</a>')
        self.assertEqual(shopify.extract_meta_page(html), "AcmeOfficial")
        self.assertIsNone(shopify.extract_meta_page("<p>no links</p>"))


if __name__ == "__main__":
    unittest.main()


class WatchlistTests(unittest.TestCase):
    def test_normalise_domain_keeps_explicit_http(self):
        from earlyscale.watchlist import normalise_domain
        self.assertEqual(normalise_domain("https://Example.com/"), "example.com")
        self.assertEqual(normalise_domain("example.com"), "example.com")
        self.assertEqual(normalise_domain("http://127.0.0.1:8001/"), "http://127.0.0.1:8001")
