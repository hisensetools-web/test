"""pdpkit unit tests: image discovery / URL normalisation, page facts, summary, guide PDF, Higgsfield helpers."""
import json
import tempfile
import unittest
from pathlib import Path

from pdpkit import config, guide, higgsfield, scrape, summary

FIX = Path(__file__).parent / "fixtures_pdp"
BASE = "https://dropstore.example/products/glow-neck-massager?variant=11"


class UrlTests(unittest.TestCase):
    def test_shopify_product_json_url(self):
        self.assertEqual(scrape.shopify_product_url(BASE), "https://dropstore.example/products/glow-neck-massager.json")
        self.assertEqual(scrape.shopify_product_url("https://x.com/collections/all/products/foo"), "https://x.com/products/foo.json")
        self.assertIsNone(scrape.shopify_product_url("https://x.com/pages/about"))

    def test_size_suffix_stripped_and_query_dropped(self):
        n = scrape.normalise_image_url
        self.assertEqual(n("//d.example/cdn/shop/files/a_600x600.jpg?v=1", BASE), "https://d.example/cdn/shop/files/a.jpg")
        self.assertEqual(n("/cdn/shop/files/a_1024x.webp", BASE), "https://dropstore.example/cdn/shop/files/a.webp")
        self.assertEqual(n("https://d.example/f/a_600x600_crop_center@2x.png", BASE), "https://d.example/f/a.png")
        self.assertEqual(n("https://d.example/f/plain.jpg", BASE), "https://d.example/f/plain.jpg")

    def test_skips_logos_icons_svg(self):
        self.assertFalse(scrape._is_image_url("https://d.example/cdn/shop/files/logo.png"))
        self.assertFalse(scrape._is_image_url("https://d.example/img/x.svg"))
        self.assertTrue(scrape._is_image_url("https://d.example/cdn/shop/files/hero.jpg"))


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.html = (FIX / "pdp.html").read_text()
        self.pj = json.loads((FIX / "product.json").read_text())["product"]

    def test_image_refs_gallery_first_dedup_lazy_srcset_background_inline(self):
        refs = scrape.extract_image_refs(self.html, BASE, self.pj)
        urls = [r.url for r in refs]
        self.assertEqual(urls[:2], ["https://dropstore.example/cdn/shop/files/glow-main.jpg", "https://dropstore.example/cdn/shop/files/glow-side.jpg"])
        self.assertEqual([r.kind for r in refs[:2]], ["gallery", "gallery"])
        self.assertIn("https://dropstore.example/cdn/shop/files/lifestyle-1.jpg", urls)      # data-src, protocol-relative
        self.assertIn("https://dropstore.example/cdn/shop/files/benefit-1.webp", urls)       # srcset, both sizes collapse to one
        self.assertIn("https://dropstore.example/cdn/shop/files/hero-bg.jpg", urls)          # css background
        self.assertIn("https://dropstore.example/cdn/shop/files/inline-json.jpg", urls)      # raw-source sweep
        self.assertNotIn("https://dropstore.example/cdn/shop/files/logo.png", urls)
        self.assertNotIn("https://dropstore.example/cdn/shop/files/payment-icons.png", urls)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(refs[0].alt, "Glow Neck Massager front")

    def test_image_refs_without_product_json_uses_og_and_ldjson(self):
        refs = scrape.extract_image_refs(self.html, BASE, None)
        self.assertEqual(refs[0].url, "https://dropstore.example/cdn/shop/files/glow-main.jpg")
        self.assertEqual(refs[0].kind, "gallery")

    def test_page_data_from_shopify_json(self):
        pd = scrape.extract_page_data(self.html, BASE, self.pj)
        self.assertEqual(pd.platform, "shopify")
        self.assertEqual(pd.title, "Glow Neck Massager")
        self.assertEqual(pd.handle, "glow-neck-massager")
        self.assertEqual(pd.price, "49.00")
        self.assertEqual(pd.compare_at_price, "89.00")
        self.assertEqual(pd.currency, "USD")
        self.assertEqual(pd.rating, "4.8")
        self.assertEqual(pd.review_count, "1243")
        self.assertEqual(len(pd.variants), 2)
        self.assertEqual(pd.options[0]["values"], ["Black", "White"])
        self.assertIn("10 minutes", pd.description_text)
        self.assertIn("4 massage modes and 15 intensity levels", pd.bullets)
        self.assertNotIn("Short", pd.bullets)
        self.assertEqual(pd.faqs[0]["q"], "Does it work with a big neck?")
        self.assertTrue(any("money-back" in t for t in pd.trust_lines))
        self.assertIn("Add to cart", pd.cta_texts)
        self.assertTrue(any("neck pain is gone" in r for r in pd.review_snippets))
        self.assertIn("Why 1,243 customers love it", pd.headings)

    def test_page_data_without_json_falls_back_to_ldjson_and_h1(self):
        pd = scrape.extract_page_data(self.html, BASE, None)
        self.assertEqual(pd.title, "Glow Neck Massager")
        self.assertEqual(pd.price, "49.00")
        self.assertEqual(pd.vendor, "Dropstore")
        self.assertEqual(pd.handle, "glow-neck-massager")

    def test_summary_markdown_and_json(self):
        pd = scrape.extract_page_data(self.html, BASE, self.pj)
        pd.images = scrape.extract_image_refs(self.html, BASE, self.pj)
        with tempfile.TemporaryDirectory() as d:
            path = summary.write_summary(pd, Path(d), manifest=[{"kind": "gallery"}, {"kind": "page"}], use_claude=False)
            md = path.read_text()
            self.assertTrue(md.startswith("# Glow Neck Massager"))
            self.assertIn("compare at 89.00", md)
            self.assertIn("## FAQ", md)
            self.assertIn("| Black | 49.00 | 89.00 | True |", md)
            facts = json.loads((Path(d) / "product_summary.json").read_text())
            self.assertEqual(facts["title"], "Glow Neck Massager")


class GuideTests(unittest.TestCase):
    def test_markdown_to_pdf_and_mechanical_guide(self):
        md = guide.mechanical_guide("Glow", "## Hero\n- headline\n\n| a | b |\n|---|---|\n| 1 | 2 |", "# Glow\n**bold** text", ["gen_01.jpg"])
        self.assertIn("gen_01.jpg", md)
        with tempfile.TemporaryDirectory() as d:
            pdf = guide.markdown_to_pdf(md, Path(d) / "g.pdf", "t")
            self.assertGreater(pdf.stat().st_size, 1000)
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))

    def test_read_template_md_html(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.md"; p.write_text("# T\nrule")
            self.assertIn("rule", guide.read_template(p))
            h = Path(d) / "t.html"; h.write_text("<h1>T</h1><p>rule</p>")
            self.assertIn("rule", guide.read_template(h))


class HiggsfieldTests(unittest.TestCase):
    def test_pick_references_gallery_first(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("page_01.jpg", "gallery_02.jpg", "gallery_01.jpg", "manifest.json"):
                (Path(d) / n).write_bytes(b"x")
            refs = higgsfield.pick_references(Path(d), max_refs=2)
            self.assertEqual([p.name for p in refs], ["gallery_01.jpg", "gallery_02.jpg"])

    def test_build_arguments_and_result_urls(self):
        args = higgsfield.build_arguments("studio shot", ["https://u/1.jpg"], num_images=2)
        self.assertEqual(args["prompt"], "studio shot")
        self.assertEqual(args[config.HIGGSFIELD_IMAGE_ARG], ["https://u/1.jpg"])
        self.assertEqual(args["num_images"], 2)
        self.assertEqual(higgsfield.result_image_urls({"images": [{"url": "https://a"}, {"url": "https://a"}], "image": {"url": "https://b"}}), ["https://a", "https://b"])

    def test_explain_error_classifies_by_status_not_text(self):
        import httpx
        from higgsfield_client.exceptions import HiggsfieldClientError

        def client_error(code, body):
            req = httpx.Request("POST", "https://platform.higgsfield.ai/x")
            resp = httpx.Response(code, request=req, text=body)
            err = HiggsfieldClientError(body)
            err.__cause__ = httpx.HTTPStatusError("x", request=req, response=resp)
            return err

        self.assertIn("network", higgsfield.explain_error(httpx.ProxyError("403 Forbidden"), "m"))
        self.assertIn("cloud.higgsfield.ai", higgsfield.explain_error(client_error(401, "Unauthorized"), "m"))
        self.assertIn("HIGGSFIELD_MODEL", higgsfield.explain_error(client_error(404, "Not Found"), "some/model"))
        self.assertIn("HIGGSFIELD_IMAGE_ARG", higgsfield.explain_error(client_error(422, "image_urls field required"), "m"))
        self.assertIn("credits", higgsfield.explain_error(client_error(402, "Insufficient balance"), "m"))

    def test_upload_bytes_tries_header_shapes_and_keeps_url_exact(self):
        import http.server, threading
        seen = []

        class Bucket(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_PUT(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                seen.append((self.path, self.headers.get("Content-Type"), body))
                ok = self.headers.get("Content-Type") is None and "X-Sig=a%2Fb%2Bc" in self.path
                self.send_response(200 if ok else 403)
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bucket)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            signed = f"http://127.0.0.1:{srv.server_port}/bucket/obj.png?X-Sig=a%2Fb%2Bc&X-Expires=60"

            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {"public_url": "https://cdn.example/obj.png", "upload_url": signed}

            class FakeTransport:
                def request(self, method, url, **kw): return FakeResp()

            class FakeClient:
                _transport = FakeTransport()

            url = higgsfield.upload_bytes(FakeClient(), b"png-bytes", "image/png")
            self.assertEqual(url, "https://cdn.example/obj.png")
            self.assertEqual([c for _, c, _ in seen], ["image/png", "image/png", None])   # with extras, plain, then no Content-Type
            self.assertTrue(all(p.endswith("?X-Sig=a%2Fb%2Bc&X-Expires=60") for p, _, _ in seen))   # no re-quoting
            self.assertEqual(seen[0][2], b"png-bytes")
        finally:
            srv.shutdown()

    def test_upload_forwards_headers_and_tagging_from_response(self):
        seen = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"public_url": "p", "upload_url": "u?X-Amz-SignedHeaders=content-type%3Bhost%3Bx-amz-tagging",
                                    "headers": {"x-amz-tagging": "ttl=7d"}}

        class FakeClient:
            class _transport:
                @staticmethod
                def request(method, url, **kw): return FakeResp()

        pub, up, extra = higgsfield.request_upload_url(FakeClient(), "image/png")
        self.assertEqual(extra, {"x-amz-tagging": "ttl=7d"})
        self.assertEqual(higgsfield._signed_headers(up), ["content-type", "host", "x-amz-tagging"])

    def test_upload_error_is_explained(self):
        msg = higgsfield.explain_error(higgsfield.UploadError("refused"), "m")
        self.assertIn("accepted the key", msg)

    def test_generated_dir_name(self):
        self.assertEqual(config.generated_dir_name("Glow Neck Massager!"), "glow-neck-massager_shopify_PDP_imgs")


if __name__ == "__main__":
    unittest.main()
