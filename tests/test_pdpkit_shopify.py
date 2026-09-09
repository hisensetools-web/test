"""Shopify upload flow against a fake Admin GraphQL + staged-upload endpoint (no network)."""
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

from pdpkit import shopify_admin

CALLS = []


class FakeShopify(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.path.startswith("/staged/"):
            CALLS.append(("staged", self.path, len(raw), self.headers.get("Content-Type", "")[:19]))
            return self._send(201, b"", "text/plain")
        body = json.loads(raw)
        q, v = body["query"], body.get("variables", {})
        CALLS.append(("gql", q.split("(")[0].split()[-1], v, self.headers.get("X-Shopify-Access-Token")))
        if "productByHandle" in q:
            return self._send(200, {"data": {"productByHandle": None}})
        if "productCreate(" in q:
            return self._send(200, {"data": {"productCreate": {"product": {"id": "gid://shopify/Product/777", "handle": "glow", "title": v["product"]["title"], "status": "DRAFT"}, "userErrors": []}}})
        if "stagedUploadsCreate" in q:
            fn = v["input"][0]["filename"]
            base = f"http://127.0.0.1:{self.server.server_port}"
            return self._send(200, {"data": {"stagedUploadsCreate": {"stagedTargets": [{
                "url": f"{base}/staged/{fn}", "resourceUrl": f"https://shopify-staged.example/tmp/{fn}",
                "parameters": [{"name": "key", "value": "tmp/" + fn}, {"name": "policy", "value": "abc"}]}], "userErrors": []}}})
        if "productCreateMedia" in q:
            return self._send(200, {"data": {"productCreateMedia": {"media": [{"alt": m["alt"], "status": "UPLOADED", "id": f"gid://shopify/MediaImage/{i}"} for i, m in enumerate(v["media"])], "mediaUserErrors": []}}})
        return self._send(400, {"errors": [{"message": "unexpected query"}]})


class UploadFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeShopify)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        CALLS.clear()
        self.admin = shopify_admin.ShopifyAdmin(store=f"127.0.0.1:{self.port}", token="shpat_test", version="2025-07")
        self.admin.endpoint = f"http://127.0.0.1:{self.port}/admin/api/2025-07/graphql.json"   # http for the fake

    def test_creates_draft_and_attaches_every_image(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            for n in ("gen_01.jpg", "gen_02.png", "notes.txt"):
                (folder / n).write_bytes(b"\x89PNG fake bytes " * 50)
            files = sorted(p for p in folder.iterdir() if p.suffix in (".jpg", ".png"))
            product = self.admin.find_product("glow") or self.admin.create_draft_product("Glow Neck Massager", "<p>x</p>", "Dropstore")
            media = self.admin.attach_images(product["id"], files, alt_prefix="Glow")
        self.assertEqual(product["status"], "DRAFT")
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0]["alt"], "Glow gen_01")
        kinds = [c[1] for c in CALLS if c[0] == "gql"]
        self.assertEqual(kinds, ["productByHandle", "productCreate", "stagedUploadsCreate", "stagedUploadsCreate", "productCreateMedia"])
        staged = [c for c in CALLS if c[0] == "staged"]
        self.assertEqual(len(staged), 2)
        self.assertTrue(all(c[3].startswith("multipart/form-data") for c in staged))
        self.assertEqual(CALLS[0][3], "shpat_test")
        create_media = [c for c in CALLS if c[0] == "gql" and c[1] == "productCreateMedia"][0][2]
        self.assertEqual(create_media["productId"], "gid://shopify/Product/777")
        self.assertEqual([m["originalSource"] for m in create_media["media"]], ["https://shopify-staged.example/tmp/gen_01.jpg", "https://shopify-staged.example/tmp/gen_02.png"])
        self.assertTrue(all(m["mediaContentType"] == "IMAGE" for m in create_media["media"]))

    def test_missing_credentials_is_a_clear_error(self):
        with self.assertRaises(SystemExit):
            shopify_admin.ShopifyAdmin(store="", token="")


if __name__ == "__main__":
    unittest.main()
