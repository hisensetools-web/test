"""Host resolution (apex -> www) and request headers, without network."""
import unittest
from unittest import mock

import requests

from earlyscale import config, shopify


def _resp(url, history=()):
    r = requests.Response()
    r.status_code = 200
    r.url = url
    r.history = list(history)
    return r


class ResolveBaseUrlTests(unittest.TestCase):
    def test_follows_redirect_to_www_once(self):
        session = mock.Mock()
        session.get.return_value = _resp("https://www.tryterrastrike.com/products.json?limit=1", history=[object()])
        self.assertEqual(shopify.resolve_base_url(session, "tryterrastrike.com"), "https://www.tryterrastrike.com")
        self.assertEqual(session.get.call_count, 1)

    def test_falls_back_to_www_when_apex_unreachable(self):
        session = mock.Mock()
        session.get.side_effect = [requests.ConnectionError("apex dead"),
                                   _resp("https://www.tryterrastrike.com/products.json?limit=1")]
        self.assertEqual(shopify.resolve_base_url(session, "tryterrastrike.com"), "https://www.tryterrastrike.com")
        called = [c.args[0] for c in session.get.call_args_list]
        self.assertEqual(called, ["https://tryterrastrike.com/products.json",
                                  "https://www.tryterrastrike.com/products.json"])

    def test_no_www_fallback_for_www_hosts_or_ip(self):
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError("dead")
        with self.assertRaises(shopify.StoreFetchError):
            shopify.resolve_base_url(session, "www.example.com")
        self.assertEqual(session.get.call_count, 1)
        session.reset_mock()
        with self.assertRaises(shopify.StoreFetchError):
            shopify.resolve_base_url(session, "http://127.0.0.1:8001")
        self.assertEqual(session.get.call_count, 1)

    def test_keeps_apex_when_it_answers(self):
        session = mock.Mock()
        session.get.return_value = _resp("https://example.com/products.json?limit=1")
        self.assertEqual(shopify.resolve_base_url(session, "example.com"), "https://example.com")


class HeaderTests(unittest.TestCase):
    def test_session_presents_as_browser(self):
        s = shopify.make_session()
        self.assertIn("Mozilla/5.0", s.headers["User-Agent"])
        self.assertNotIn("compatible;", s.headers["User-Agent"])
        self.assertIn("text/html", s.headers["Accept"])
        self.assertEqual(s.headers["User-Agent"], config.USER_AGENT)

    def test_406_is_not_retried(self):
        session = mock.Mock()
        r = requests.Response(); r.status_code = 406; r.url = "https://x/products.json"
        session.get.return_value = r
        with mock.patch("earlyscale.shopify.time.sleep") as sleep:
            with self.assertRaises(shopify.StoreFetchError) as cm:
                shopify.get_json(session, "https://x/products.json")
        self.assertIn("406", str(cm.exception))
        self.assertEqual(session.get.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
