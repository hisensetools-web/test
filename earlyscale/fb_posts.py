"""Meta layer part 2 (B): engagement on Sponsored posts, captured by hand, counted logged-out.

Dark ads are unpublished Page posts; their reactions / comments / shares only exist on the post
itself. This module does NOT drive a logged-in Facebook account. Instead:

  fb-capture     you saw a Sponsored post in your own feed: paste its permalink (and optionally the
                 page, text and counts, or the JSON the bookmarklet in tools/fb_capture_bookmarklet.js
                 copies to the clipboard). Stored in fb_posts, deduped on post_id.
  fb-engagement  daily: open every known permalink logged-out (fresh browser context, no profile)
                 and record the public counts; gated / removed permalinks are marked and skipped.
                 Derives comment_delta_1d and engagement_per_day_7d.
  match_posts    join captured posts to Ad Library ads on page + normalised primary text
                 (similarity >= 0.8) or a matching creative fingerprint; the matched ad row gets
                 post_id / permalink and its counts, so engagement_per_day flows into Signals via
                 the existing product join.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

from . import config, meta_ads

log = logging.getLogger("earlyscale.fb_posts")

POST_ID_RES = [
    re.compile(r"/posts/(?:pfbid)?([A-Za-z0-9]+)"),                 # /{page}/posts/{id} or /posts/pfbid...
    re.compile(r"/(?:videos|reels|photos)/(?:[^/]+/)?(\d{6,})"),
    re.compile(r"/permalink\.php\?.*?story_fbid=([A-Za-z0-9]+)"),
    re.compile(r"[?&](?:story_fbid|fbid|v)=([A-Za-z0-9]+)"),
]
PAGE_ID_RES = [re.compile(r"facebook\.com/(\d{6,})/"), re.compile(r"[?&]id=(\d{6,})")]
UTM_RE = re.compile(r"^(utm_|fbclid$|ref$|__cft__|__tn__|mibextid$|rdid$|share_url$)")


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def clean_permalink(url: str) -> str:
    """Drop tracking params, force https://www.facebook.com."""
    u = urlparse(url.strip())
    qs = {k: v for k, v in parse_qs(u.query).items() if not UTM_RE.match(k)}
    q = "&".join(f"{k}={v[0]}" for k, v in sorted(qs.items()))
    fb = u.netloc.endswith("facebook.com")
    host = "www.facebook.com" if fb else u.netloc
    scheme = "https" if fb else (u.scheme or "https")
    return f"{scheme}://{host}{u.path}" + (f"?{q}" if q else "")


def post_id_from_url(url: str) -> str | None:
    for rx in POST_ID_RES:
        m = rx.search(url)
        if m:
            return m.group(1)
    return None


def page_id_from_url(url: str) -> str | None:
    for rx in PAGE_ID_RES:
        m = rx.search(url)
        if m:
            return m.group(1)
    return None


def parse_capture(obj: dict | str) -> dict:
    """A capture is a permalink or a dict {permalink|url, page_name, page_id, primary_text|text,
    headline, landing_url, image_url, reactions, comments, shares}. Returns a normalised record."""
    if isinstance(obj, str):
        obj = {"permalink": obj}
    url = obj.get("permalink") or ""
    if not url and not obj.get("post_id"):
        url = obj.get("url") or ""
    if not url and not obj.get("post_id"):
        raise ValueError("capture has no permalink")
    link = clean_permalink(url) if url else ""
    pid = obj.get("post_id") or post_id_from_url(link)
    if not pid:
        raise ValueError(f"cannot find a post id in {link}")
    if not link:
        link = f"feed://{pid}"   # observer saw the post but Facebook had not filled in its permalink yet
    counts = {}
    for k in ("reactions", "comments", "shares"):
        v = obj.get(k)
        if v is None or v == "":
            continue
        counts[k] = meta_ads._to_int(str(v)) if not isinstance(v, int) else v
    return {"post_id": str(pid), "permalink": link, "page_id": obj.get("page_id") or page_id_from_url(link),
            "page_name": obj.get("page_name") or obj.get("page"), "primary_text": obj.get("primary_text") or obj.get("text"),
            "headline": obj.get("headline"), "landing_url": obj.get("landing_url") or obj.get("link"),
            "image_url": obj.get("image_url"), "counts": counts}


def parse_captures_text(text: str) -> list[dict]:
    """Clipboard / file content: a JSON object, a JSON array, JSON lines, or bare URLs one per line."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
        return [parse_capture(x) for x in items]
    except ValueError:
        pass
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(parse_capture(json.loads(line) if line.startswith("{") else line))
        except ValueError as e:
            log.warning("skipping line: %s", e)
    return out


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record_capture(conn: sqlite3.Connection, cap: dict, today: str, source: str = "manual",
                   store_id: int | None = None) -> bool:
    """Insert or refresh a captured post; today's counts (if given) become a fb_posts_daily row. Returns True if new."""
    now = _utcnow()
    new = conn.execute("SELECT 1 FROM fb_posts WHERE post_id = ?", (cap["post_id"],)).fetchone() is None
    conn.execute(
        """INSERT INTO fb_posts (post_id, page_id, page_name, permalink, primary_text, headline, landing_url, image_url, image_hash, source, captured_at, store_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(post_id) DO UPDATE SET page_id = COALESCE(excluded.page_id, page_id), page_name = COALESCE(excluded.page_name, page_name),
             primary_text = COALESCE(excluded.primary_text, primary_text), headline = COALESCE(excluded.headline, headline),
             landing_url = COALESCE(excluded.landing_url, landing_url), image_url = COALESCE(excluded.image_url, image_url),
             image_hash = COALESCE(excluded.image_hash, image_hash), store_id = COALESCE(excluded.store_id, store_id)""",
        (cap["post_id"], cap.get("page_id"), cap.get("page_name"), cap["permalink"], cap.get("primary_text"), cap.get("headline"),
         cap.get("landing_url"), cap.get("image_url"), cap.get("image_hash"), source, now, store_id))
    if cap.get("counts"):
        c = cap["counts"]
        conn.execute("""INSERT OR REPLACE INTO fb_posts_daily (snapshot_date, post_id, reactions, comments, shares, status, fetched_at)
                        VALUES (?,?,?,?,?,'capture',?)""",
                     (today, cap["post_id"], c.get("reactions"), c.get("comments"), c.get("shares"), now))
        compute_engagement(conn, cap["post_id"], today)
    conn.commit()
    return new


# ---------------------------------------------------------------- daily counts (logged-out)

def fetch_counts(browser, url: str) -> tuple[dict, str]:
    """Public post page in a fresh, logged-out context. status ok | gated | removed | no-counts | error:<type>."""
    counts, status = meta_ads.fetch_post_counts(browser, url)
    if status == "login-wall":
        return counts, "gated"
    return counts, status


def compute_engagement(conn: sqlite3.Connection, post_id: str, today: str) -> None:
    rows = conn.execute(
        """SELECT snapshot_date, reactions, comments, shares FROM fb_posts_daily WHERE post_id = ? AND snapshot_date <= ?
           AND (reactions IS NOT NULL OR comments IS NOT NULL OR shares IS NOT NULL) ORDER BY snapshot_date DESC LIMIT 8""",
        (post_id, today)).fetchall()
    if not rows or rows[0]["snapshot_date"] != today:
        return
    cdelta = None
    if len(rows) >= 2 and rows[0]["comments"] is not None and rows[1]["comments"] is not None:
        cdelta = rows[0]["comments"] - rows[1]["comments"]

    def eng(r):
        return (r["reactions"] or 0) + (r["comments"] or 0) + (r["shares"] or 0)
    per_day = None
    t = date.fromisoformat(today)
    window = [r for r in rows if (t - date.fromisoformat(r["snapshot_date"])).days <= 7]
    if len(window) >= 2:
        days = (t - date.fromisoformat(window[-1]["snapshot_date"])).days
        if days > 0:
            per_day = round((eng(window[0]) - eng(window[-1])) / days, 2)
    conn.execute("UPDATE fb_posts_daily SET comment_delta_1d = ?, engagement_per_day_7d = ? WHERE snapshot_date = ? AND post_id = ?",
                 (cdelta, per_day, today, post_id))


def refresh_engagement(conn: sqlite3.Connection, browser, today: str, max_posts: int | None = None,
                       fetch=fetch_counts, wait=None) -> dict:
    """Re-fetch every known permalink not yet read today. Gated / removed posts are marked and skipped next time."""
    max_posts = config.FB_POSTS_MAX if max_posts is None else max_posts
    wait = wait or (lambda: meta_ads._wait(3, 7))
    rows = conn.execute(
        """SELECT p.post_id, p.permalink, p.status FROM fb_posts p
           WHERE COALESCE(p.status, '') NOT IN ('removed', 'gated') AND p.permalink LIKE 'http%'
             AND NOT EXISTS (SELECT 1 FROM fb_posts_daily d WHERE d.post_id = p.post_id AND d.snapshot_date = ? AND d.status IN ('ok', 'capture'))
           ORDER BY p.captured_at""", (today,)).fetchall()
    counts = {"candidates": len(rows), "fetched": 0, "ok": 0, "gated": 0, "removed": 0, "no_counts": 0, "errors": 0}
    now = _utcnow()
    for i, r in enumerate(rows[:max_posts]):
        c, status = fetch(browser, r["permalink"])
        counts["fetched"] += 1
        key = {"ok": "ok", "gated": "gated", "removed": "removed", "no-counts": "no_counts"}.get(status, "errors")
        counts[key] += 1
        conn.execute("""INSERT OR REPLACE INTO fb_posts_daily (snapshot_date, post_id, reactions, comments, shares, status, fetched_at)
                        VALUES (?,?,?,?,?,?,?)""",
                     (today, r["post_id"], c.get("reactions"), c.get("comments"), c.get("shares"), status, now))
        if status in ("gated", "removed"):
            conn.execute("UPDATE fb_posts SET status = ?, note = ? WHERE post_id = ?", (status, f"{status} on {today}", r["post_id"]))
        elif status == "ok":
            conn.execute("UPDATE fb_posts SET status = 'ok' WHERE post_id = ?", (r["post_id"],))
            compute_engagement(conn, r["post_id"], today)
        conn.commit()
        if i < min(len(rows), max_posts) - 1:
            wait()
    propagate_to_ads(conn, today)
    return counts


# ---------------------------------------------------------------- join to Ad Library ads

def match_posts(conn: sqlite3.Connection, threshold: float = 0.8) -> int:
    """Attach unmatched posts to ads: same page (page_id, else normalised page name) and text
    similarity >= threshold, or an identical creative hash. Returns the number newly matched."""
    from . import ad_metrics
    posts = [dict(r) for r in conn.execute("SELECT * FROM fb_posts WHERE ad_id IS NULL")]
    if not posts:
        return 0
    ads = [dict(r) for r in conn.execute(
        """SELECT ad_id, store_id, page_id, page_name, primary_text, headline, creative_hash, ad_start_date, first_seen_date
           FROM meta_ads WHERE COALESCE(page_ignored, 0) = 0""")]
    n = 0
    for p in posts:
        cands = [a for a in ads if (p.get("page_id") and a["page_id"] == p["page_id"])
                 or (p.get("page_name") and _norm(a["page_name"]) == _norm(p["page_name"]))]
        if not cands:
            continue
        best, via, score = None, None, 0.0
        if p.get("image_hash"):
            for a in cands:
                if a["creative_hash"] and a["creative_hash"] == p["image_hash"]:
                    best, via, score = a, "creative", 1.0
                    break
        if best is None and p.get("primary_text"):
            for a in cands:
                s_ = ad_metrics.text_similarity(_norm(p["primary_text"]), _norm(f"{a['primary_text'] or ''}"))
                if s_ > score:
                    best, score = a, s_
            via = "text"
            if score < threshold:
                best = None
        if best is None:
            continue
        conn.execute("UPDATE fb_posts SET ad_id = ?, match_via = ?, match_score = ?, store_id = COALESCE(store_id, ?) WHERE post_id = ?",
                     (best["ad_id"], via, round(score, 3), best["store_id"], p["post_id"]))
        conn.execute("UPDATE meta_ads SET post_id = COALESCE(post_id, ?), post_permalink = COALESCE(post_permalink, ?) WHERE ad_id = ?",
                     (p["post_id"], p["permalink"], best["ad_id"]))   # first matched post stays the ad's post
        n += 1
    conn.commit()
    return n


def propagate_to_ads(conn: sqlite3.Connection, today: str) -> int:
    """Copy today's post counts onto the matched ad's meta_ads_daily row so the existing engagement
    metrics (engagement_per_day -> Signals) and rule 5 see them."""
    from . import ad_metrics
    match_posts(conn)
    n = 0
    for r in conn.execute(
        """SELECT p.ad_id, a.store_id, d.reactions, d.comments, d.shares FROM fb_posts p
           JOIN fb_posts_daily d ON d.post_id = p.post_id AND d.snapshot_date = ?
           JOIN meta_ads a ON a.ad_id = p.ad_id WHERE p.ad_id IS NOT NULL AND d.status IN ('ok', 'capture')""", (today,)).fetchall():
        conn.execute("""INSERT INTO meta_ads_daily (snapshot_date, ad_id, store_id, is_active, fetched_at) VALUES (?,?,?,1,?)
                        ON CONFLICT(snapshot_date, ad_id) DO NOTHING""", (today, r["ad_id"], r["store_id"], _utcnow()))
        conn.execute("""UPDATE meta_ads_daily SET reactions = ?, comments = ?, shares = ?, post_status = 'feed'
                        WHERE snapshot_date = ? AND ad_id = ?""", (r["reactions"], r["comments"], r["shares"], today, r["ad_id"]))
        eng = ad_metrics._engagement({"reactions": r["reactions"], "comments": r["comments"], "shares": r["shares"]})
        per_day = ad_metrics._engagement_per_day(conn, r["ad_id"], today)
        conn.execute("UPDATE meta_ads_daily SET engagement = ?, engagement_per_day = ? WHERE snapshot_date = ? AND ad_id = ?",
                     (eng, per_day, today, r["ad_id"]))
        ad_metrics.compute_reach_metrics(conn, r["ad_id"], today)
        n += 1
    conn.commit()
    return n


def hash_image(url: str, session=None) -> str | None:
    try:
        from . import ad_detail
        return ad_detail._hash_url(url, session)["sha256"]
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- local listener for the browser observer

def make_listener(conn_or_factory, today_fn, on_capture=None):
    """HTTP handler: POST /capture with a JSON capture or array of captures (from tools/fb_observer),
    GET /health. Bound to 127.0.0.1 only. `conn_or_factory` is a connection (single-threaded server)
    or a zero-arg callable returning one (opened lazily in the serving thread)."""
    from http.server import BaseHTTPRequestHandler
    state = {"conn": None if callable(conn_or_factory) else conn_or_factory}

    def get_conn():
        if state["conn"] is None:
            state["conn"] = conn_or_factory()
        return state["conn"]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body: dict):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self._send(204, {})

        def do_GET(self):
            n = get_conn().execute("SELECT COUNT(*) FROM fb_posts").fetchone()[0]
            self._send(200, {"ok": True, "posts": n})

        def do_POST(self):
            if urlparse(self.path).path != "/capture":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            try:
                caps = parse_captures_text(body)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            today = today_fn()
            conn = get_conn()
            new = 0
            for c in caps:
                try:
                    if record_capture(conn, c, today, source="observer"):
                        new += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("capture %s failed: %s", c.get("post_id"), e)
            matched = match_posts(conn)
            propagate_to_ads(conn, today)
            if on_capture:
                on_capture(len(caps), new, matched)
            self._send(200, {"received": len(caps), "new": new, "matched": matched})

    return H
