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

    def test_marketing_site_on_apex_falls_through_to_shop_subdomain(self):
        """pipitea.com is a marketing site (HTML 404 on /products.json); the storefront is shop.pipitea.com."""
        html = _json_resp(404, b"<!DOCTYPE html><html><body>Not found</body></html>", ctype="text/html",
                          url="https://pipitea.com/products.json?limit=1")
        www_html = _json_resp(200, b"<html><body>marketing</body></html>", ctype="text/html",
                              url="https://www.pipitea.com/products.json?limit=1")
        session = mock.Mock()
        session.get.side_effect = [html, www_html, _json_resp(url="https://shop.pipitea.com/products.json?limit=1")]
        self.assertEqual(shopify.resolve_base_url(session, "pipitea.com"), "https://shop.pipitea.com")
        called = [c.args[0] for c in session.get.call_args_list]
        self.assertEqual(called, ["https://pipitea.com/products.json", "https://www.pipitea.com/products.json",
                                  "https://shop.pipitea.com/products.json"])

    def test_no_storefront_anywhere_returns_first_answering_host(self):
        session = mock.Mock()
        session.get.side_effect = [
            _json_resp(404, b"<html>no</html>", ctype="text/html", url="https://example.com/products.json?limit=1"),
            requests.ConnectionError("no www"),
            requests.ConnectionError("no shop"),
        ]
        # the caller then fetches https://example.com/products.json and reports the real error (404 / HTML)
        self.assertEqual(shopify.resolve_base_url(session, "example.com"), "https://example.com")
        self.assertEqual(session.get.call_count, 3)

    def test_shop_variant_only_for_bare_domains(self):
        self.assertEqual(shopify._shop_variant("https://pipitea.com"), "https://shop.pipitea.com")
        self.assertIsNone(shopify._shop_variant("https://shop.pipitea.com"))
        self.assertIsNone(shopify._shop_variant("https://www.pipitea.com"))
        self.assertEqual(shopify._shop_variant("https://brand.co.uk"), "https://shop.brand.co.uk")
        self.assertIsNone(shopify._shop_variant("http://127.0.0.1:8001"))

    def test_keeps_apex_when_it_answers(self):
        session = mock.Mock()
        session.get.return_value = _resp("https://example.com/products.json?limit=1")
        self.assertEqual(shopify.resolve_base_url(session, "example.com"), "https://example.com")


def _json_resp(status=200, body=b'{"products": []}', ctype="application/json; charset=utf-8",
               url="https://x/products.json"):
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = body
    r.headers["Content-Type"] = ctype
    r.encoding = "utf-8"
    return r


class HeaderTests(unittest.TestCase):
    def test_session_defaults_are_browser_ua_with_json_accept(self):
        s = shopify.make_session()
        self.assertIn("Mozilla/5.0", s.headers["User-Agent"])
        self.assertNotIn("compatible;", s.headers["User-Agent"])
        self.assertEqual(s.headers["Accept"], "application/json")
        self.assertIn("en-US", s.headers["Accept-Language"])

    def test_products_json_request_sends_accept_application_json(self):
        session = mock.Mock()
        session.get.return_value = _json_resp()
        shopify.get_json(session, "https://x/products.json", params={"limit": 250, "page": 1})
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.get.call_args.kwargs["headers"]["Accept"], "application/json")
        self.assertEqual(session.get.call_args.kwargs["params"], {"limit": 250, "page": 1})

    def test_resolve_probe_sends_accept_application_json(self):
        session = mock.Mock()
        session.get.return_value = _json_resp(url="https://example.com/products.json?limit=1")
        shopify.resolve_base_url(session, "example.com")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Accept"], "application/json")

    def test_406_retried_once_with_wildcard_accept(self):
        session = mock.Mock()
        session.get.side_effect = [_json_resp(406, b'{"error":"Not Acceptable"}'), _json_resp()]
        with mock.patch("earlyscale.shopify.time.sleep") as sleep:
            data = shopify.get_json(session, "https://x/products.json")
        self.assertEqual(data, {"products": []})
        accepts = [c.kwargs["headers"]["Accept"] for c in session.get.call_args_list]
        self.assertEqual(accepts, ["application/json", "*/*"])
        sleep.assert_not_called()

    def test_406_twice_fails_fast(self):
        session = mock.Mock()
        session.get.return_value = _json_resp(406, b'{"error":"Not Acceptable"}')
        with mock.patch("earlyscale.shopify.time.sleep") as sleep:
            with self.assertRaises(shopify.StoreFetchError) as cm:
                shopify.get_json(session, "https://x/products.json")
        self.assertIn("406", str(cm.exception))
        self.assertEqual(session.get.call_count, 2)  # json accept + wildcard, no backoff retries
        sleep.assert_not_called()


class NonJsonBodyTests(unittest.TestCase):
    def test_html_body_raises_clear_error_without_retry(self):
        session = mock.Mock()
        session.get.return_value = _json_resp(
            200, b"<!DOCTYPE html><html><head><title>Store</title></head><body></body></html>",
            ctype="text/html; charset=utf-8")
        with mock.patch("earlyscale.shopify.time.sleep") as sleep:
            with self.assertRaises(shopify.StoreFetchError) as cm:
                shopify.get_json(session, "https://x/products.json")
        self.assertIn("got HTML, not JSON", str(cm.exception))
        self.assertIn("https://x/products.json", str(cm.exception))
        self.assertEqual(session.get.call_count, 1)
        sleep.assert_not_called()

    def test_html_body_with_json_content_type_is_still_caught(self):
        r = _json_resp(200, b"\n  <html><body>challenge</body></html>", ctype="application/json")
        with self.assertRaises(shopify.StoreFetchError) as cm:
            shopify.parse_json_response(r)
        self.assertIn("got HTML, not JSON", str(cm.exception))

    def test_garbage_body_raises_clear_error_not_traceback(self):
        r = _json_resp(200, b"Expecting nothing here", ctype="application/json")
        with self.assertRaises(shopify.StoreFetchError) as cm:
            shopify.parse_json_response(r)
        self.assertIn("not valid JSON", str(cm.exception))
        self.assertIn("Expecting nothing", str(cm.exception))

    def test_valid_json_passes(self):
        self.assertEqual(shopify.parse_json_response(_json_resp())["products"], [])
        # Shopify sometimes omits charset / uses text/javascript; body decides.
        self.assertEqual(shopify.parse_json_response(_json_resp(ctype="text/javascript"))["products"], [])


if __name__ == "__main__":
    unittest.main()
