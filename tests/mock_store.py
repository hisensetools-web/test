"""A fake Shopify storefront for offline end-to-end runs.

Serves /products.json and /collections/all/products.json with real Shopify pagination
semantics (limit=250, page=N, empty page past the end). The catalog is deterministic
per --seed, so two runs on different --date values produce comparable snapshots, and
--mutate applies a small set of day-2 changes (sold-outs, price changes, a new product)
so delta logic (build step 2) has something to chew on.

    python -m tests.mock_store --port 8001 --products 320 --seed 1
    python tracker.py run --watchlist tests/watchlist.mock.csv
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

WORDS = ["Cloud", "Ridge", "Nova", "Sol", "Pulse", "Aero", "Terra", "Lumen", "Halo", "Ember",
         "Drift", "Vale", "Onyx", "Fern", "Atlas", "Coral", "Slate", "Bloom", "Tide", "Zen"]
TYPES = ["Hoodie", "Leggings", "Tee", "Serum", "Bottle", "Wallet", "Sneaker", "Palette", "Cap", "Bag"]
SIZES = ["XS", "S", "M", "L", "XL"]


def build_catalog(n: int, seed: int, now: datetime) -> list[dict]:
    rng = random.Random(seed)
    products = []
    for i in range(n):
        pid = 7_000_000_000_000 + seed * 1_000_000 + i
        name = f"{rng.choice(WORDS)} {rng.choice(TYPES)} {i}"
        handle = name.lower().replace(" ", "-")
        # Most products are old; ~8% published in the last 14 days so "new product" logic has material.
        age_days = rng.choice([rng.randint(0, 13)] * 8 + [rng.randint(14, 900)] * 92)
        created = now - timedelta(days=age_days, hours=rng.randint(0, 23))
        updated = created + timedelta(days=rng.randint(0, max(0, age_days)))
        nvar = rng.choice([1, 1, 2, 3, 5])
        base_price = rng.choice([19.0, 24.0, 29.0, 39.0, 49.0, 69.0, 89.0]) + rng.choice([0, 0.5, 0.95, 0.99])
        variants = []
        for j in range(nvar):
            variants.append({
                "id": pid * 10 + j,
                "title": SIZES[j] if nvar > 1 else "Default Title",
                "sku": f"SKU-{seed}-{i}-{j}",
                "price": f"{base_price:.2f}",
                "compare_at_price": f"{base_price * 1.25:.2f}" if rng.random() < 0.3 else None,
                "available": rng.random() > 0.15,
                "position": j + 1,
                "product_id": pid,
            })
        products.append({
            "id": pid,
            "title": name,
            "handle": handle,
            "body_html": f"<p>{name}</p>",
            "published_at": created.isoformat(),
            "created_at": created.isoformat(),
            "updated_at": updated.isoformat(),
            "vendor": f"MockStore{seed}",
            "product_type": name.split()[1],
            "tags": rng.sample(["new", "bestseller", "sale", "core", "limited"], k=rng.randint(0, 3)),
            "variants": variants,
            "images": [],
            "options": [],
        })
    return products


def mutate(products: list[dict], seed: int, now: datetime) -> list[dict]:
    """Day-2 changes: 5% of variants flip to sold out, 3% of products change price, 2 new products."""
    rng = random.Random(seed + 999)
    for p in products:
        for v in p["variants"]:
            if v["available"] and rng.random() < 0.05:
                v["available"] = False
        if rng.random() < 0.03:
            for v in p["variants"]:
                v["price"] = f"{float(v['price']) * 0.8:.2f}"
            p["updated_at"] = now.isoformat()
    for k in range(2):
        pid = 7_999_000_000_000 + seed * 1000 + k
        products.insert(0, {
            "id": pid, "title": f"Launch Drop {k}", "handle": f"launch-drop-{k}", "body_html": "",
            "published_at": now.isoformat(), "created_at": now.isoformat(), "updated_at": now.isoformat(),
            "vendor": f"MockStore{seed}", "product_type": "Drop", "tags": ["new"],
            "variants": [{"id": pid * 10, "title": "Default Title", "sku": None, "price": "44.00",
                          "compare_at_price": None, "available": True, "position": 1, "product_id": pid}],
            "images": [], "options": [],
        })
    return products


def make_handler(products: list[dict], collection: list[dict], delay_first_page_status: int | None,
                 require_browser: bool = False, redirect_to: str | None = None,
                 html_unless_json_accept: bool = False):
    state = {"first_products_call": True, "hits": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send_json(self, status: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            state["hits"] += 1
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if redirect_to:  # behave like an apex host that 301s everything to www
                self.send_response(301)
                self.send_header("Location", redirect_to + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ua = self.headers.get("User-Agent", "")
            accept = self.headers.get("Accept", "")
            if html_unless_json_accept and u.path.endswith(".json"):
                # Real Shopify behaviour that caused the 2026-09 regression: an HTML-first
                # Accept header gets the storefront page back with a 200.
                if "application/json" not in accept and not accept.startswith("*/*"):
                    html = b"<!DOCTYPE html><html><head><title>Store</title></head><body>storefront</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                    return
            if require_browser:  # olavita.co-style WAF: 406 unless browser UA, and rejects explicit JSON Accept
                if "Mozilla/" not in ua or "compatible;" in ua or accept.strip() == "application/json":
                    self._send_json(406, {"error": "Not Acceptable"})
                    return
            limit = min(int(qs.get("limit", ["30"])[0]), 250)
            page = int(qs.get("page", ["1"])[0])
            if u.path == "/products.json":
                src = products
                if delay_first_page_status and state["first_products_call"]:
                    state["first_products_call"] = False
                    self._send_json(delay_first_page_status, {"errors": "simulated transient error"})
                    return
            elif u.path == "/collections/all/products.json":
                src = collection
            elif u.path == "/":
                html = b'<html><body><footer><a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>' \
                       b'<a href="https://www.facebook.com/MockStorePage">fb</a></footer></body></html>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return
            else:
                self.send_response(404)
                self.end_headers()
                return
            start = (page - 1) * limit
            self._send_json(200, {"products": src[start:start + limit]})

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--products", type=int, default=320)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--mutate", action="store_true", help="apply day-2 changes to the catalog")
    ap.add_argument("--fail-first", type=int, default=None, metavar="STATUS",
                    help="answer the first /products.json request with this status (e.g. 430) to test retry")
    ap.add_argument("--require-browser", action="store_true",
                    help="answer 406 unless User-Agent/Accept look like a real browser")
    ap.add_argument("--html-unless-json-accept", action="store_true",
                    help="answer .json URLs with the storefront HTML (200) unless Accept asks for JSON")
    ap.add_argument("--redirect-to", metavar="ORIGIN",
                    help="301 every request to ORIGIN (simulates apex -> www redirect)")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    products = build_catalog(args.products, args.seed, now)
    if args.mutate:
        products = mutate(products, args.seed, now)
    # "best-selling" collection order: deterministic shuffle so position differs from catalog order
    collection = list(products)
    random.Random(args.seed + 7).shuffle(collection)
    srv = HTTPServer(("127.0.0.1", args.port), make_handler(products, collection, args.fail_first,
                                                             args.require_browser, args.redirect_to,
                                                             args.html_unless_json_accept))
    print(f"mock store on http://127.0.0.1:{args.port} products={len(products)} seed={args.seed} mutate={args.mutate}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
