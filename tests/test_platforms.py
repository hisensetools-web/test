"""Catalogue adapters for non-Shopify platforms: parsers, detection, each adapter against a mock store, dates,
and the daily pass end to end."""
import json
import threading
import unittest
from http.server import HTTPServer
from unittest import mock

from earlyscale import config, db, platforms, sheets, shopify
from earlyscale.cli import run_products_pass
from tests import mock_platforms

TODAY = "2026-09-08"


def serve(platform):
    srv = HTTPServer(("127.0.0.1", 0), mock_platforms.make_handler(platform))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class PureParserTests(unittest.TestCase):
    def test_product_page_json_ld(self):
        html = ('<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Glow Serum",'
                '"sku":"GS1","brand":{"@type":"Brand","name":"Acme"},"offers":[{"@type":"Offer","price":"39.00","availability":"https://schema.org/InStock"},'
                '{"@type":"Offer","price":"99.00","sku":"GS3","availability":"http://schema.org/OutOfStock"}]}</script></head></html>')
        f = platforms.parse_product_page(html, "/product/glow-serum/")
        self.assertEqual((f["title"], f["brand"], f["path"]), ("Glow Serum", "Acme", "/product/glow-serum"))
        self.assertEqual([(v["price"], v["available"]) for v in f["variants"]], [("39.00", True), ("99.00", False)])

    def test_product_page_opengraph_only(self):
        html = '<meta property="og:type" content="product"><meta property="og:title" content="Tea &amp; Co Sampler"><meta property="product:price:amount" content="19.5"><meta property="product:availability" content="instock">'
        f = platforms.parse_product_page(html, "/p/tea-sampler")
        self.assertEqual((f["title"], f["variants"][0]["price"], f["variants"][0]["available"]), ("Tea & Co Sampler", "19.5", True))

    def test_non_product_page_is_none(self):
        self.assertIsNone(platforms.parse_product_page("<html><body>About us</body></html>", "/about"))

    def test_sitemap_parsing_and_product_url_filter(self):
        idx = '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://x.com/a.xml</loc></sitemap></sitemapindex>'
        subs, urls = platforms.parse_sitemap(idx)
        self.assertEqual((subs, urls), (["https://x.com/a.xml"], []))
        um = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://x.com/product/a</loc><lastmod>2026-09-01</lastmod></url><url><loc>https://x.com/collections/all</loc></url></urlset>'
        subs, urls = platforms.parse_sitemap(um)
        self.assertEqual(urls, [("https://x.com/product/a", "2026-09-01"), ("https://x.com/collections/all", None)])
        self.assertTrue(platforms.is_product_url("https://x.com/product/a"))
        self.assertTrue(platforms.is_product_url("https://x.com/shop/p/glow"))
        self.assertTrue(platforms.is_product_url("https://x.com/en-us/products/glow"))
        self.assertFalse(platforms.is_product_url("https://x.com/collections/all"))
        self.assertFalse(platforms.is_product_url("https://x.com/blog/post"))
        self.assertTrue(platforms.is_product_url("https://x.com/glow-serum.html", "sitemap-products.xml"))   # bare paths count inside a product sitemap
        self.assertFalse(platforms.is_product_url("https://x.com/glow-serum.html", "sitemap.xml"))

    def test_make_product_ids_are_stable_and_dates_normalised(self):
        from datetime import datetime, timezone
        ms = int(datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        a = platforms.make_product("/product/glow/", "Glow", [{"price": "10", "sku": "A"}], created_at=ms)
        b = platforms.make_product("/product/glow", "Glow", [{"price": "10", "sku": "A"}])
        self.assertEqual((a["product_id"], a["handle"], a["url_path"]), (b["product_id"], "glow", "/product/glow"))
        self.assertEqual(a["variants"][0]["variant_id"], b["variants"][0]["variant_id"])
        self.assertEqual(a["created_at"], "2026-09-08T12:00:00+00:00")
        self.assertEqual(platforms._iso("2026-09-01 10:00:00"), "2026-09-01T10:00:00+00:00")
        self.assertEqual(platforms._iso("Mon, 07 Sep 2026 10:00:00 +0000"), "2026-09-07T10:00:00+00:00")
        self.assertIsNone(platforms._iso("soon"))


class DetectAndAdapterTests(unittest.TestCase):
    def _run(self, platform):
        srv, base = serve(platform)
        try:
            session = shopify.make_session()
            info = platforms.detect(session, base)
            cat = platforms.fetch_catalogue(base, session, platform_hint=info, today=TODAY)
            return info, cat, srv.RequestHandlerClass.stats
        finally:
            srv.shutdown()

    def test_woocommerce(self):
        info, cat, _ = self._run("woocommerce")
        self.assertEqual((info["platform"], cat.platform), ("woocommerce", "woocommerce"))
        by = {p["handle"]: p for p in cat.products}
        self.assertEqual(sorted(by), ["magnesium-glycinate", "tart-cherry-sleep-gummies", "wormwood-tincture"])
        p = by["tart-cherry-sleep-gummies"]
        self.assertEqual((p["url_path"], p["min_price"], p["variant_count"], p["sold_out_variants"]), ("/product/tart-cherry-sleep-gummies", 34.0, 1, 0))
        self.assertEqual(p["created_at"][:10], "2026-08-27")                         # wp/v2 date_gmt
        self.assertEqual(by["wormwood-tincture"]["sold_out_variants"], 1)

    def test_squarespace_with_stock(self):
        info, cat, _ = self._run("squarespace")
        self.assertEqual(cat.platform, "squarespace")
        by = {p["handle"]: p for p in cat.products}
        v = by["tart-cherry-sleep-gummies"]["variants"][0]
        self.assertEqual((v["stock"], v["inventory_management"], v["price"], v["available"]), (120, "shopify", 34.0, True))
        self.assertIsNone(by["magnesium-glycinate"]["variants"][0]["stock"])          # unlimited
        self.assertEqual(by["wormwood-tincture"]["sold_out_variants"], 1)
        self.assertEqual(by["tart-cherry-sleep-gummies"]["created_at"][:10], "2026-08-27")

    def test_magento_graphql(self):
        info, cat, _ = self._run("magento")
        self.assertEqual(cat.platform, "magento")
        by = {p["handle"]: p for p in cat.products}
        self.assertEqual(by["wormwood-tincture"]["url_path"], "/wormwood-tincture.html")
        self.assertEqual(by["wormwood-tincture"]["variants"][0]["available"], False)
        self.assertEqual(by["tart-cherry-sleep-gummies"]["variants"][0]["stock"], 120)
        self.assertEqual(by["tart-cherry-sleep-gummies"]["created_at"][:10], "2026-08-27")

    def test_generic_sitemap_json_ld_with_budget_and_cache(self):
        srv, base = serve("generic")
        try:
            conn = db.connect(":memory:")
            sid = db.upsert_store(conn, base)
            session = shopify.make_session()
            with mock.patch.object(config, "CATALOGUE_MAX_PAGES", 2):
                cat = platforms.fetch_catalogue(base, session, conn, sid, TODAY)
            self.assertEqual((cat.platform, cat.partial, len(cat.products)), ("generic", True, 3))
            read = [p for p in cat.products if p["variant_count"]]
            self.assertEqual(len(read), 2)                                              # budget: two pages read, one placeholder
            self.assertEqual(srv.RequestHandlerClass.stats["product_pages"], 2)
            placeholder = next(p for p in cat.products if not p["variant_count"])
            self.assertTrue(placeholder["title"])                                       # slug-derived title, ads can attach
            # next run reads the remaining page and keeps the cached two (no refetch within CATALOGUE_REFRESH_DAYS)
            with mock.patch.object(config, "CATALOGUE_MAX_PAGES", 2):
                cat2 = platforms.fetch_catalogue(base, session, conn, sid, "2026-09-09")
            self.assertEqual((cat2.partial, srv.RequestHandlerClass.stats["product_pages"]), (False, 3))
            by = {p["handle"]: p for p in cat2.products}
            self.assertEqual((by["wormwood-tincture"]["vendor"], by["wormwood-tincture"]["sold_out_variants"]), ("FakeBrand", 1))
            self.assertEqual(conn.execute("SELECT platform FROM stores WHERE id = ?", (sid,)).fetchone()[0], "generic")
        finally:
            srv.shutdown()

    def test_bigcommerce_dates_from_rss(self):
        info, cat, _ = self._run("bigcommerce")
        self.assertEqual((info["platform"], cat.platform), ("bigcommerce", "bigcommerce"))
        by = {p["handle"]: p for p in cat.products}
        self.assertEqual(by["tart-cherry-sleep-gummies"]["created_at"][:10], "2026-08-27")

    def test_headless_shopify_uses_the_myshopify_origin(self):
        srv, base = serve("headless")
        try:
            session = shopify.make_session()
            info = platforms.detect(session, base)
            self.assertEqual((info["platform"], info["myshopify"]), ("shopify_headless", "fakebrand.myshopify.com"))
            fake_raw = [{"id": 1, "handle": "glow", "title": "Glow", "created_at": "2026-08-01T00:00:00Z", "published_at": "2026-08-01T00:00:00Z",
                         "updated_at": "2026-09-01T00:00:00Z", "variants": [{"id": 11, "title": "d", "price": "20.00", "available": True}]}]
            with mock.patch.object(shopify, "fetch_store", lambda dom, s: (fake_raw, 1)) as _:
                cat = platforms.fetch_catalogue(base, session, platform_hint=info, today=TODAY)
            self.assertEqual((cat.platform, cat.products[0]["handle"], cat.extra["myshopify"]), ("shopify_headless", "glow", "fakebrand.myshopify.com"))
        finally:
            srv.shutdown()


class FillDatesTests(unittest.TestCase):
    def test_first_seen_becomes_created_only_after_the_first_snapshot(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "x.com")
        day1 = [platforms.make_product("/p/a", "A", [{"price": 1}]), platforms.make_product("/p/b", "B", [{"price": 1}])]
        platforms.fill_dates(conn, sid, day1, "2026-09-01")
        self.assertEqual([p["created_at"] for p in day1], [None, None])                # present on the first snapshot: unknown age
        db.write_product_snapshot(conn, sid, "2026-09-01", day1)
        day5 = [platforms.make_product("/p/a", "A", [{"price": 1}]), platforms.make_product("/p/c", "C", [{"price": 1}])]
        platforms.fill_dates(conn, sid, day5, "2026-09-05")
        by = {p["handle"]: p for p in day5}
        self.assertIsNone(by["a"]["created_at"])
        self.assertEqual((by["c"]["created_at"], by["c"]["published_at"]), ("2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"))
        dated = [platforms.make_product("/p/d", "D", [{"price": 1}], created_at="2026-01-01T00:00:00+00:00")]
        platforms.fill_dates(conn, sid, dated, "2026-09-05")
        self.assertEqual(dated[0]["created_at"], "2026-01-01T00:00:00+00:00")          # a platform date is kept


class DailyPassTests(unittest.TestCase):
    def test_run_products_pass_over_three_platforms(self):
        servers = [serve(p) for p in ("woocommerce", "squarespace", "generic")]
        try:
            conn = db.connect(":memory:")
            stores = [{"store_domain": base} for _, base in servers]
            with mock.patch.object(config, "CATALOGUE_MAX_PAGES", 10):
                ok, failed = run_products_pass(conn, stores, TODAY)
            self.assertEqual((ok, failed), (3, 0))
            plats = {r[0]: r[1] for r in conn.execute("SELECT store_domain, platform FROM stores")}
            self.assertEqual(sorted(plats.values()), ["generic", "squarespace", "woocommerce"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products_daily WHERE snapshot_date = ?", (TODAY,)).fetchone()[0], 9)
            generic_base = servers[2][1]
            gen = conn.execute("SELECT created_at FROM products_daily p JOIN stores s ON s.id = p.store_id WHERE s.store_domain = ?", (generic_base,)).fetchall()
            self.assertTrue(all(r[0] is None for r in gen))          # no dates on the platform, first snapshot: unknown age, not "today"
            woo = conn.execute("SELECT created_at FROM products_daily p JOIN stores s ON s.id = p.store_id WHERE s.store_domain = ?", (servers[0][1],)).fetchall()
            self.assertTrue(all(r[0] for r in woo))                   # WooCommerce dates come through
            with mock.patch.object(sheets, "watched_store_ids", lambda c: None):
                srows = sheets.stores_rows(conn)
            self.assertEqual(sorted(r[sheets.STORES_HEADERS.index("products")] for r in srows), [3, 3, 3])
        finally:
            for srv, _ in servers:
                srv.shutdown()


if __name__ == "__main__":
    unittest.main()


class WafStoreTests(unittest.TestCase):
    """A storefront WAF that answers 406 to an explicit JSON Accept (olavita.co) is still a Shopify store: the probe
    must retry with the browser Accept like shopify.fetch does, not fall through to the headless / sitemap adapters."""

    def test_406_to_json_accept_is_retried_and_detected_as_shopify(self):
        from http.server import BaseHTTPRequestHandler
        from earlyscale import config, platforms, shopify

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                accept = self.headers.get("Accept", "")
                if self.path.startswith("/products.json"):
                    if accept.startswith(config.JSON_ACCEPT):
                        body, status, ctype = b"Not Acceptable", 406, "text/plain"
                    else:
                        body, status, ctype = b'{"products": [{"id": 1, "handle": "x", "title": "X", "variants": []}]}', 200, "application/json"
                else:
                    body, status, ctype = b'<html><script>Shopify.shop = "waf-1.myshopify.com"</script></html>', 200, "text/html"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            info = platforms.detect(shopify.make_session(), f"http://127.0.0.1:{srv.server_port}", quiet=True)
        finally:
            srv.shutdown()
        self.assertEqual((info["platform"], info["evidence"]), ("shopify", "products.json"))
