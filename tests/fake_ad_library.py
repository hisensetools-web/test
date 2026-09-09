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
                                          body: 'doc_id=123&variables=' + encodeURIComponent(JSON.stringify({cursor: batch, sort: (location.search.match(/sort_data\\[mode\\]=([a-z_]+)/) || [null, null])[1]}))});
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


def make_handler(batches: int, block: bool, landing: str | None = None, handles: list[str] | None = None, port: int = 8095,
                 day: int = 1, likes: int = 12000, stop_ads: tuple[str, ...] = (), base_date: str | None = None,
                 same_order: bool = False):
    """day / likes / stop_ads drive the single-ad page: end_date = base_date + (day - 1) except for ads in
    stop_ads (which freeze at day 1); page_like_count grows with day. base_date defaults to today (UTC)."""
    base = json.loads(FIX.read_text())
    from datetime import date as _date, datetime as _dt, timezone as _tz
    bd = _date.fromisoformat(base_date) if base_date else _dt.now(_tz.utc).date()
    day0 = int((bd - _date(1970, 1, 1)).total_seconds())

    def end_date_for(ad_id: str) -> int:
        d = 1 if ad_id in stop_ads else day
        return day0 + (d - 1) * 86400

    def page_likes() -> int:
        return likes + (day - 1) * 150
    if landing:
        # point every ad at the given store origin: products/<handle> for the first two ads, /pages/av1 for the third
        res = base["data"]["ad_library_main"]["search_results_connection"]["edges"][0]["node"]["collated_results"]
        hs = handles or ["ceylon-cinnamon-capsules-tt", "ceylon-cinnamon-capsules"]
        res[0]["snapshot"]["link_url"] = f"{landing}/products/{hs[0]}?utm_source=fb"
        res[1]["snapshot"]["link_url"] = f"{landing}/pages/av1"
        res[2]["snapshot"]["link_url"] = None
        res[2]["snapshot"]["cards"][0]["link_url"] = f"{landing}/products/{hs[-1]}"
        # boosted post lives on this same fake host so the browser can open it
        res[1]["snapshot"]["root_reshared_post"]["url"] = f"http://127.0.0.1:{port}/55501/posts/987654321012345"

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
            qs = parse_qs(u.query)
            if u.path.startswith("/ads/library") and qs.get("id"):
                # the single-ad page: the record sits in the HTML in a data-sjs block under
                # deeplink_ad_archive_result.deeplink_ad_archive; no graphql call, no post id.
                if block:
                    return self._send(200, BLOCKED.encode(), "text/html")
                ad_id = qs["id"][0]
                res = base["data"]["ad_library_main"]["search_results_connection"]["edges"][0]["node"]["collated_results"]
                node = None
                for r in res:
                    off = int(ad_id) - int(r["ad_archive_id"])
                    if off >= 0 and off % 100 == 0:
                        node = copy.deepcopy(r)
                        node["ad_archive_id"] = ad_id
                        break
                if node is None:
                    return self._send(200, b"<html><head><title>Ad Library</title></head><body>No ads</body></html>", "text/html")
                node["ad_id"] = None
                node["end_date"] = end_date_for(ad_id)
                node["page_like_count"] = page_likes()
                node["page_profile_uri"] = f"https://www.facebook.com/{node['page_id']}/"
                node["page_categories"] = ["Health/beauty", "Vitamins/supplements"]
                node["snapshot"]["images"] = [{"original_image_url": f"http://127.0.0.1:{port}/creative/{int(ad_id) % 3}.jpg",
                                               "resized_image_url": f"http://127.0.0.1:{port}/creative/{int(ad_id) % 3}_r.jpg"}]
                node["snapshot"]["page_like_count"] = node["page_like_count"]
                doc = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [["RelayPrefetchedStreamCache", "next", [],
                       ["adp_AdLibraryMobileFocusedStateProviderQueryRelayPreloader", {"__bbox": {"result": {"data": {
                           "deeplink_ad_archive_result": {"deeplink_ad_archive": node}}}}}]]]}}]]]}
                html = ('<!DOCTYPE html><html><head><title>Ad Library</title></head><body><div id="root"></div>'
                        '<script type="application/json" data-content-len="1" data-sjs>' + json.dumps(doc) + '</script>'
                        '<script type="application/json" data-sjs>{"define":[]}</script></body></html>')
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if u.path.startswith("/creative/"):
                n = u.path.rsplit("/", 1)[-1].split("_")[0].split(".")[0]
                body = (b"FAKEJPEG-" + n.encode()) * 64
                return self._send(200, body, "image/jpeg")
            if "/posts/" in u.path:   # a "public post" page with embedded counts
                body = ('<html><head><title>Post</title></head><body><script>{"comment_count":{"total_count":57},'
                        '"reaction_count":{"count":1203},"share_count":{"count":9}}</script></body></html>')
                return self._send(200, body.encode(), "text/html")
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
            sort = None
            try:
                v = json.loads(parse_qs(body).get("variables", ["{}"])[0])
                cursor, sort = v.get("cursor", 0), v.get("sort")
            except ValueError:
                pass
            doc = copy.deepcopy(base)
            results = doc["data"]["ad_library_main"]["search_results_connection"]["edges"][0]["node"]["collated_results"]
            for i, r in enumerate(results):
                r["ad_archive_id"] = str(int(r["ad_archive_id"]) + 100 * cursor)
                # the 'Low impression count' badge: Meta's exact key is confirmed with `ads-fields`; the fake uses a
                # plausible boolean so the whole chain (payload -> daily row -> delivering metrics) is exercised
                r["is_low_impressions"] = (i % 3 == 2)
            if sort == "total_impressions" and not same_order:
                results.reverse()          # "Impressions: high to low": a different order from newest-first
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
    ap.add_argument("--landing", help="store origin to point landing URLs at, e.g. http://127.0.0.1:8011")
    ap.add_argument("--handles", nargs="+", help="product handles to use in landing URLs")
    ap.add_argument("--day", type=int, default=1, help="simulated day: single-ad end_date and page likes advance with it")
    ap.add_argument("--likes", type=int, default=12000)
    ap.add_argument("--stop-ads", nargs="*", default=(), help="ad ids whose end_date stops advancing (switched off)")
    ap.add_argument("--base-date", help="ISO date that day 1's end_date maps to (default: today)")
    ap.add_argument("--same-order", action="store_true", help="the impressions sort returns the newest-first order (not informative)")
    a = ap.parse_args(argv)
    srv = HTTPServer(("127.0.0.1", a.port), make_handler(a.batches, a.block, a.landing, a.handles, a.port, a.day, a.likes,
                                                         tuple(a.stop_ads), a.base_date, a.same_order))
    print(f"fake Ad Library on http://127.0.0.1:{a.port}/ads/library/ batches={a.batches} block={a.block}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
