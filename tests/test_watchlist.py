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
