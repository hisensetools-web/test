"""A local stand-in for the Meta Ad Library, for exercising the Playwright loop offline.

Serves an HTML page that, like the real thing, loads its first batch of ads through an
XHR to /api/graphql/ and loads the next batch when scrolled to the bottom. Payloads use
the same shape as tests/fixtures/ad_library_graphql.json (ids renumbered per batch).

    python -m tests.fake_ad_library --port 8095 [--batches 3] [--block]
    open http://127.0.0.1:8095/ads/library/?q=x
"""
from __future__ import annotations

import argparse
import copy
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIX = Path(__file__).parent / "fixtures" / "ad_library_graphql.json"

PAGE = """<!DOCTYPE html><html><head><title>Ad Library</title>
<style>body{margin:0;font-family:sans-serif} .ad{height:420px;border:1px solid #ccc;margin:12px;padding:8px}</style>
</head><body>
<h1>Results for "%(q)s"</h1>
<div id="list"></div>
<script>
let batch = 0, done = false, loading = false;
async function load() {
  if (done || loading) return; loading = true;
  const r = await fetch('/api/graphql/', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                                          body: 'doc_id=123&variables=' + encodeURIComponent(JSON.stringify({cursor: batch}))});
  const text = await r.text();
  const first = JSON.parse(text.split('\\n')[0]);
  const ads = [];
  (function walk(o){ if (o && typeof o === 'object') { if (o.ad_archive_id) ads.push(o); else Object.values(o).forEach(walk); } })(first);
  for (const a of ads) { const d = document.createElement('div'); d.className = 'ad'; d.textContent = a.ad_archive_id + ' ' + (a.snapshot.body && a.snapshot.body.text || ''); document.getElementById('list').appendChild(d); }
  batch += 1; if (!first.data.ad_library_main.search_results_connection.page_info.has_next_page) done = true;
  loading = false;
}
window.addEventListener('scroll', () => { if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 50) load(); });
load();
</script></body></html>"""

BLOCKED = "<html><head><title>Log in to Facebook</title></head><body><h1>You must log in to continue.</h1></body></html>"


def make_handler(batches: int, block: bool):
    base = json.loads(FIX.read_text())

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path.startswith("/ads/library"):
                if block:
                    return self._send(200, BLOCKED.encode(), "text/html")
                q = parse_qs(u.query).get("q", ["?"])[0]
                return self._send(200, (PAGE % {"q": q}).encode(), "text/html")
            self._send(404, b"nope", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode() if n else ""
            if u.path != "/api/graphql/":
                return self._send(404, b"nope", "text/plain")
            cursor = 0
            try:
                cursor = json.loads(parse_qs(body).get("variables", ["{}"])[0]).get("cursor", 0)
            except ValueError:
                pass
            doc = copy.deepcopy(base)
            results = doc["data"]["ad_library_main"]["search_results_connection"]["edges"][0]["node"]["collated_results"]
            for r in results:
                r["ad_archive_id"] = str(int(r["ad_archive_id"]) + 100 * cursor)
            doc["data"]["ad_library_main"]["search_results_connection"]["page_info"]["has_next_page"] = cursor + 1 < batches
            # Facebook-style multi-document body: main payload, then a deferred payload line.
            text = json.dumps(doc) + "\n" + json.dumps({"label": "deferred", "data": {}}) + "\n"
            self._send(200, text.encode(), "application/json; charset=utf-8")

    return H


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--batches", type=int, default=3)
    ap.add_argument("--block", action="store_true", help="serve a login wall instead of results")
    a = ap.parse_args(argv)
    srv = HTTPServer(("127.0.0.1", a.port), make_handler(a.batches, a.block))
    print(f"fake Ad Library on http://127.0.0.1:{a.port}/ads/library/ batches={a.batches} block={a.block}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
