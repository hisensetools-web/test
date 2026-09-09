"""In-process fake storefronts on platforms other than Shopify, for tests and the offline e2e.

  make_handler("woocommerce")   homepage with a WooCommerce marker; /wp-json/wc/store/v1/products (+ wp/v2/product dates)
  make_handler("squarespace")   homepage with Static.SQUARESPACE_CONTEXT; /shop?format=json with items, addedOn, qtyInStock
  make_handler("generic")       plain homepage; robots.txt -> sitemap index -> product sitemap; product pages with JSON-LD
  make_handler("magento")       homepage with Magento_ marker; POST /graphql products query
  make_handler("headless")      homepage with cdn.shopify.com assets and <shop>.myshopify.com (the catalogue lives elsewhere)

Run one by hand:  python -m tests.mock_platforms --platform woocommerce --port 8301
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _ts(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).replace(tzinfo=None).isoformat()


PRODUCTS = [  # slug, title, price, in_stock, stock, created_days_ago
    ("tart-cherry-sleep-gummies", "Tart Cherry Sleep Gummies", 34.0, True, 120, 12),
    ("magnesium-glycinate", "Magnesium Glycinate", 29.0, True, None, 200),
    ("wormwood-tincture", "Wormwood Tincture", 49.0, False, 0, 40),
]


def make_handler(platform: str, myshopify: str = "fakebrand"):
    stats = {"hits": 0, "product_pages": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, status, body, ctype="text/html; charset=utf-8", headers=None):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _origin(self):
            return f"http://{self.headers.get('Host')}"

        def do_GET(self):
            stats["hits"] += 1
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            path = u.path
            if path == "/products.json" or path == "/collections/all/products.json":
                return self._send(404, "<html>not found</html>")
            if path == "/":
                return self._send(200, self._home())
            if platform == "woocommerce":
                if path == "/wp-json/wc/store/v1/products":
                    page = int(qs.get("page", ["1"])[0])
                    if page > 1:
                        return self._send(200, "[]", "application/json")
                    items = []
                    for i, (slug, title, price, in_stock, stock, _) in enumerate(PRODUCTS):
                        items.append({"id": 100 + i, "name": title, "slug": slug, "permalink": f"{self._origin()}/product/{slug}/",
                                      "type": "simple", "prices": {"price": str(int(price * 100)), "regular_price": str(int(price * 100)),
                                                                   "sale_price": str(int(price * 100)), "currency_minor_unit": 2},
                                      "is_in_stock": in_stock, "low_stock_remaining": stock if stock and stock < 10 else None,
                                      "variations": [], "categories": [{"name": "Supplements"}], "tags": []})
                    return self._send(200, json.dumps(items), "application/json", {"X-WP-TotalPages": "1"})
                if path == "/wp-json/wp/v2/product":
                    return self._send(200, json.dumps([{"id": 100 + i, "slug": p[0], "date_gmt": _ts(p[5]), "modified_gmt": _ts(1)}
                                                       for i, p in enumerate(PRODUCTS)]), "application/json")
            if platform == "squarespace" and path in ("/shop", "/store") and qs.get("format") == ["json"]:
                if path == "/store":
                    return self._send(404, "nope")
                items = []
                for i, (slug, title, price, in_stock, stock, days) in enumerate(PRODUCTS):
                    added = int((NOW - timedelta(days=days)).timestamp() * 1000)
                    items.append({"id": f"sq{i}", "title": title, "urlId": slug, "fullUrl": f"/shop/p/{slug}", "addedOn": added,
                                  "publishOn": added, "updatedOn": added + 3600000,
                                  "variants": [{"id": f"v{i}", "sku": f"SKU-{i}", "priceMoney": {"value": f"{price:.2f}", "currency": "USD"},
                                                "stock": {"unlimited": stock is None, "qtyInStock": stock or 0}, "attributes": {"Size": "One"}}]})
                return self._send(200, json.dumps({"collection": {"id": "c1"}, "items": items, "pagination": {"nextPage": False}}), "application/json")
            if platform in ("generic", "bigcommerce"):
                if path == "/robots.txt":
                    return self._send(200, f"User-agent: *\nSitemap: {self._origin()}/sitemap.xml\n", "text/plain")
                if path == "/sitemap.xml":
                    return self._send(200, f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                           f'<sitemap><loc>{self._origin()}/sitemap-pages.xml</loc></sitemap>'
                                           f'<sitemap><loc>{self._origin()}/sitemap-products.xml</loc></sitemap></sitemapindex>', "application/xml")
                if path == "/sitemap-pages.xml":
                    return self._send(200, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                           f'<url><loc>{self._origin()}/about</loc></url><url><loc>{self._origin()}/collections/all</loc></url></urlset>', "application/xml")
                if path == "/sitemap-products.xml":
                    urls = "".join(f"<url><loc>{self._origin()}/shop/{p[0]}</loc><lastmod>{_ts(1)[:10]}</lastmod></url>" for p in PRODUCTS)
                    return self._send(200, f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', "application/xml")
                if path == "/rss.php" and platform == "bigcommerce":
                    items = "".join(f"<item><title>{p[1]}</title><link>{self._origin()}/shop/{p[0]}</link>"
                                    f"<pubDate>{(NOW - timedelta(days=p[5])).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>" for p in PRODUCTS)
                    return self._send(200, f"<rss><channel>{items}</channel></rss>", "application/rss+xml")
                if path.startswith("/shop/"):
                    slug = path[len("/shop/"):].strip("/")
                    for s, title, price, in_stock, stock, _ in PRODUCTS:
                        if s == slug:
                            stats["product_pages"] += 1
                            ld = {"@context": "https://schema.org", "@type": "Product", "name": title, "sku": f"G-{slug}",
                                  "brand": {"@type": "Brand", "name": "FakeBrand"},
                                  "offers": {"@type": "Offer", "price": f"{price:.2f}", "priceCurrency": "USD",
                                             "availability": "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock"}}
                            return self._send(200, f'<html><head><title>{title}</title><meta property="og:type" content="product">'
                                                   f'<script type="application/ld+json">{json.dumps(ld)}</script></head><body><h1>{title}</h1></body></html>')
                    return self._send(404, "<html>no</html>")
            if platform == "magento":
                pass
            return self._send(404, "<html>not found</html>")

        def do_POST(self):
            stats["hits"] += 1
            if platform == "magento" and urlparse(self.path).path == "/graphql":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                page = (body.get("variables") or {}).get("page", 1)
                items = [] if page > 1 else [
                    {"sku": f"M-{i}", "name": title, "url_key": slug, "url_suffix": ".html", "created_at": (NOW - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
                     "updated_at": _ts(1), "stock_status": "IN_STOCK" if in_stock else "OUT_OF_STOCK", "only_x_left_in_stock": stock if stock and stock < 200 else None,
                     "price_range": {"minimum_price": {"final_price": {"value": price}, "regular_price": {"value": price}}, "maximum_price": {"final_price": {"value": price}}},
                     "variants": []} for i, (slug, title, price, in_stock, stock, days) in enumerate(PRODUCTS)]
                return self._send(200, json.dumps({"data": {"products": {"total_count": len(PRODUCTS), "items": items}}}), "application/json")
            return self._send(404, "nope")

        def _home(self):
            if platform == "woocommerce":
                return '<html><head><link rel="stylesheet" href="/wp-content/plugins/woocommerce/assets/css/woocommerce.css"></head><body class="woocommerce-page">shop</body></html>'
            if platform == "squarespace":
                return '<html><head><script>Static.SQUARESPACE_CONTEXT = {"collectionId": "c1"};</script><link href="https://static1.squarespace.com/x.css"></head><body>shop</body></html>'
            if platform == "magento":
                return '<html><head><script src="/static/version1712/frontend/Magento/luma/en_US/requirejs/require.js"></script></head><body>Magento_Theme</body></html>'
            if platform == "headless":
                return f'<html><head><link href="https://cdn.shopify.com/s/files/1/0001/0002/t/1/assets/theme.css"><script>window.Shopify = {{shop: "{myshopify}.myshopify.com"}}</script></head><body>headless</body></html>'
            if platform == "bigcommerce":
                return '<html><head><script src="https://cdn11.bigcommerce.com/s-abc/stencil/1/js/theme-bundle.main.js"></script></head><body>shop</body></html>'
            return '<html><head><title>Fake brand</title></head><body><a href="/shop/tart-cherry-sleep-gummies">shop</a></body></html>'

    H.stats = stats
    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="generic", choices=["woocommerce", "squarespace", "generic", "magento", "headless", "bigcommerce"])
    ap.add_argument("--port", type=int, default=8301)
    a = ap.parse_args()
    srv = HTTPServer(("127.0.0.1", a.port), make_handler(a.platform))
    print(f"mock {a.platform} store on http://127.0.0.1:{a.port}")
    srv.serve_forever()
