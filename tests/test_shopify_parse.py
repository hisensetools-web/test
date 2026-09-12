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
        self.products = shopify.normalise_products(raw)
        self.by_handle = {p["handle"]: p for p in self.products}

    def test_malformed_record_is_skipped_not_fatal(self):
        self.assertEqual(sorted(self.by_handle), ["cloud-hoodie", "no-variants", "ridge-wallet"])

    def test_only_identity_and_dates_are_kept(self):
        p = self.by_handle["cloud-hoodie"]
        self.assertEqual((p["product_id"], p["title"], p["url_path"], p["variant_count"]), (101, "Cloud Hoodie", "/products/cloud-hoodie", 3))
        self.assertEqual(p["tags"], ["new", "core"])
        self.assertTrue(p["created_at"].startswith("20"))
        for gone in ("variants", "min_price", "sold_out_variants", "collection_position"):
            self.assertNotIn(gone, p)

    def test_string_tags_and_empty_type(self):
        p = self.by_handle["ridge-wallet"]
        self.assertEqual(p["tags"], ["leather", "bestseller"])
        self.assertIsNone(p["product_type"])

    def test_product_without_variants(self):
        p = self.by_handle["no-variants"]
        self.assertEqual(p["variant_count"], 0)
        self.assertIsNone(p["published_at"])


class UrlAndDiscoveryTests(unittest.TestCase):
    def test_base_url_forms(self):
        self.assertEqual(shopify.base_url("example.com"), "https://example.com")
        self.assertEqual(shopify.base_url("https://www.example.com/"), "https://www.example.com")
        self.assertEqual(shopify.base_url("http://127.0.0.1:8001"), "http://127.0.0.1:8001")


if __name__ == "__main__":
    unittest.main()


class WatchlistTests(unittest.TestCase):
    def test_normalise_domain_keeps_explicit_http(self):
        from earlyscale.watchlist import normalise_domain
        self.assertEqual(normalise_domain("https://Example.com/"), "example.com")
        self.assertEqual(normalise_domain("example.com"), "example.com")
        self.assertEqual(normalise_domain("http://127.0.0.1:8001/"), "http://127.0.0.1:8001")
