"""Google Sheets sync via an Apps Script web app (sheets/Code.gs).

Two tabs: Winners (one row per landing URL with >= 3 delivering ads) and Stores. Rows come from winners.py, are cut into JSON chunks under SHEETS_CHUNK_BYTES, POSTed one by one (following the
302 that Apps Script answers POSTs with, retrying on 5xx / network errors / Google's response-host hiccups), and
the tab counts are read back afterwards to verify the sheet holds what the database holds.
"""
from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from . import config, winners
from .watchlist import read_watchlist

log = logging.getLogger("earlyscale.sheets")

WINNERS_HEADERS = winners.WINNERS_HEADERS
STORES_HEADERS = winners.STORES_HEADERS
TAB_ORDER = ("winners", "stores")
TAB_NAMES = {"winners": "Winners", "stores": "Stores"}
TAB_MODES = {"winners": "replace", "stores": "replace"}

_watchlist_path = None   # set by set_watchlist_path(); None = default stores.txt


def set_watchlist_path(path) -> None:
    """Use an alternate watchlist.csv for the store filter (the CLI passes --watchlist through)."""
    global _watchlist_path
    _watchlist_path = path


def watched_store_ids(conn: sqlite3.Connection) -> set[int] | None:
    """Stores currently in stores.txt (None = no watchlist, use every store in the DB).
    Removed stores keep their history in SQLite but drop out of the sheet."""
    domains = {r["store_domain"] for r in read_watchlist(_watchlist_path)}
    if not domains:
        return None
    return {r["id"] for r in conn.execute("SELECT id, store_domain FROM stores") if r["store_domain"] in domains}


def _stores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    keep = watched_store_ids(conn)
    return [s for s in conn.execute("SELECT id, store_domain, meta_page_name, platform, shop_id, store_created_est FROM stores ORDER BY store_domain")
            if keep is None or s["id"] in keep]


class SheetsSyncError(Exception):
    """Fatal sync problem (bad URL, deployment not public, Apps Script error)."""


class _Retryable(SheetsSyncError):
    pass


# ---------------------------------------------------------------- rows from SQLite

def winners_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    return winners.winners_rows(conn, _stores(conn), as_of)


def stores_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    return winners.stores_rows(conn, _stores(conn), as_of)


# ---------------------------------------------------------------- chunking

def _size(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def chunk_rows(rows: list[list], max_bytes: int = config.SHEETS_CHUNK_BYTES) -> list[list[list]]:
    """Greedy split so each chunk's JSON stays under max_bytes (~200 bytes envelope reserved).
    A single oversized row still goes out alone. Zero rows -> one empty chunk."""
    if not rows:
        return [[]]
    budget = max(1000, max_bytes - 200)
    chunks: list[list[list]] = []
    cur: list[list] = []
    size = 2  # "[]"
    for r in rows:
        rsize = _size(r) + 1
        if cur and size + rsize > budget:
            chunks.append(cur)
            cur, size = [], 2
        cur.append(r)
        size += rsize
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- HTTP

def _explain_non_json(text: str, status: int) -> str:
    head = text.strip()[:160].replace("\n", " ")
    hint = ("Apps Script returned a web page instead of JSON. Usual causes: the deployment's "
            "'Who has access' is not 'Anyone', the URL is not the /exec URL, or you edited Code.gs "
            "without doing Deploy > Manage deployments > Edit > New version.")
    return f"{hint} (HTTP {status}; starts with: {head!r})"


def _echo_hiccup(r, hops: int) -> bool:
    """Google's script.googleusercontent.com response host sometimes answers the one-time redirect with its own
    generic 404 error page (the body carries 'ppConfig'). That is not the script's answer and goes away on retry."""
    if hops == 0 or "googleusercontent" not in urlparse(r.url).netloc:
        return False
    head = (r.text or "")[:2000]
    return r.status_code == 404 or ("ppConfig" in head and not head.lstrip().startswith("{"))


def tab_count(session: requests.Session, url: str, tab: str) -> int | None:
    """Data rows the sheet holds for one tab right now (None when the web app cannot be asked)."""
    try:
        counts = get_json(session, url, {"tabs": "1"}).get("tabs") or {}
    except (SheetsSyncError, ValueError, requests.RequestException):
        return None
    n = counts.get(tab)
    return int(n) if isinstance(n, (int, float)) else None


def post_payload(session: requests.Session, url: str, payload: dict, retries: int = 5, already_applied=None) -> dict:
    """POST one chunk and return the script's JSON reply. `already_applied()` is asked before a retry: Apps Script
    may have written the rows although its reply was lost (Google's response host hiccups), and re-sending a
    replace-mode chunk would duplicate them."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        if attempt and already_applied is not None:
            try:
                applied = already_applied()
            except Exception:  # noqa: BLE001
                applied = False
            if applied:
                n = len(payload.get("rows") or [])
                log.info("sheets: %s chunk %s was written although the reply was lost; not re-sending it",
                         payload.get("tab"), payload.get("chunk"))
                return {"ok": True, "written": n, "skipped": 0, "recovered": True}
        try:
            r = session.post(url, data=body, headers={"Content-Type": "application/json"},
                             timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
            hops = 0
            chain = []
            while r.status_code in (301, 302, 303, 307, 308) and hops < 10:
                # Apps Script answers every POST with a 302 to a one-time googleusercontent URL
                # that serves the script's response; that hop must be a GET.
                loc = urljoin(r.url, r.headers.get("Location", ""))
                chain.append(urlparse(loc).netloc)
                if "accounts.google" in loc:
                    raise SheetsSyncError("Apps Script redirected to a Google login page: the deployment's 'Who has access' must be "
                                          "'Anyone' (Deploy > Manage deployments > Edit), and the URL must be the /exec URL")
                r = session.get(loc, timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
                hops += 1
            if r.status_code in (301, 302, 303, 307, 308):
                raise _Retryable(f"Google's response host kept redirecting ({hops} hops via {', '.join(dict.fromkeys(chain))})")
            if r.status_code >= 500:
                raise _Retryable(f"HTTP {r.status_code} from Apps Script")
            if _echo_hiccup(r, hops):
                raise _Retryable(f"HTTP {r.status_code} error page from Google's response host (transient)")
            if r.status_code != 200:
                raise SheetsSyncError(f"HTTP {r.status_code} from {r.url}: {r.text.strip()[:200]!r}")
            text = r.text.strip()
            if text.lower() == "ok":
                # doGet's health-check text: Google served the deployment's GET reply instead of the POST's one-time response
                raise _Retryable("got the script's health-check text ('ok') instead of the POST reply (transient)")
            if not text.startswith("{"):
                raise SheetsSyncError(_explain_non_json(text, r.status_code))
            data = json.loads(text)
            if not data.get("ok"):
                raise SheetsSyncError(f"Apps Script reported an error: {data.get('error')}")
            return data
        except (_Retryable, requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        if attempt < retries:
            wait = min(60, (2 ** attempt) * 3) + random.uniform(0, 2)
            log.warning("sheets: retry %d/%d after %s (sleep %.1fs)", attempt + 1, retries, last_err, wait)
            time.sleep(wait)
    raise SheetsSyncError(f"giving up after {retries + 1} attempts: {last_err}")


def get_json(session: requests.Session, url: str, params: dict, _retry: int = 0) -> dict:
    """GET the web app (follows the same 302 hop as POST) and parse its JSON. A network failure (DNS down, no
    connection) is retried twice and then reported as a SheetsSyncError, never as a raw traceback."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            return _get_json_once(session, url, params, _retry)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            if attempt < 2:
                wait = 3 * (2 ** attempt)
                log.warning("sheets: cannot reach the web app (%s); retry %d/2 in %ds", type(e).__name__, attempt + 1, wait)
                time.sleep(wait)
    raise SheetsSyncError(f"cannot reach the Apps Script web app (network down or DNS failure?): {str(last)[:200]}")


def _get_json_once(session: requests.Session, url: str, params: dict, _retry: int = 0) -> dict:
    r = session.get(url, params=params, timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
    hops = 0
    chain = []
    while r.status_code in (301, 302, 303, 307, 308) and hops < 10:
        nxt = urljoin(r.url, r.headers.get("Location", ""))
        chain.append(urlparse(nxt).netloc)
        if "accounts.google" in nxt:
            raise SheetsSyncError("Apps Script redirected to a Google login page: the deployment's 'Who has access' must be "
                                  "'Anyone' (Deploy > Manage deployments > Edit), and the URL must be the /exec URL")
        r = session.get(nxt, timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
        hops += 1
    if _echo_hiccup(r, hops) and _retry < 3:
        time.sleep(5 * (_retry + 1))
        return _get_json_once(session, url, params, _retry=_retry + 1)
    text = r.text.strip()
    if r.status_code in (301, 302, 303, 307, 308):
        raise SheetsSyncError(f"Apps Script kept redirecting ({hops} hops via {', '.join(dict.fromkeys(chain))}); usually a temporary "
                              "Google hiccup, sometimes a response too large for a GET")
    if r.status_code != 200 or not text.startswith("{"):
        raise SheetsSyncError(_explain_non_json(text, r.status_code) if not text.startswith("{") else f"HTTP {r.status_code}")
    data = json.loads(text)
    if not data.get("ok"):
        raise SheetsSyncError(f"Apps Script reported an error: {data.get('error')}")
    return data


def verify(conn: sqlite3.Connection, url: str, as_of: str | None = None, session: requests.Session | None = None) -> dict:
    """Compare what the sheet holds with what the DB says it should: rows per tab.
    Returns {"tabs": [...], "problems": [...]}. Needs the doGet of the current Code.gs."""
    session = session or requests.Session()
    try:
        counts = get_json(session, url, {"tabs": "1"}).get("tabs") or {}
    except (SheetsSyncError, ValueError) as e:
        raise SheetsSyncError(f"the deployed Code.gs does not answer ?tabs=1 (re-paste sheets/Code.gs and deploy a new version): {e}") from e
    tabs, problems = [], []
    for item in build_plan(conn, TAB_ORDER, as_of):
        expected = len(item["rows"])
        have = counts.get(item["tab"])
        ok = have is not None and have == expected
        tabs.append({"tab": item["tab"], "mode": item["mode"], "expected": expected, "sheet": have, "ok": ok})
        if not ok:
            problems.append(f"{item['tab']}: sheet has {have} rows, DB has {expected}")
    return {"tabs": tabs, "problems": problems}


# ---------------------------------------------------------------- orchestration

def build_plan(conn: sqlite3.Connection, tabs=TAB_ORDER, as_of: str | None = None) -> list[dict]:
    builders = {"winners": winners_rows, "stores": stores_rows}
    plan = []
    for tab in TAB_ORDER:
        if tab not in tabs:
            continue
        t0 = time.monotonic()
        rows = builders[tab](conn, as_of)
        took = time.monotonic() - t0
        if took >= 5:
            log.info("sheets: built %s (%d rows) in %.0fs", TAB_NAMES[tab], len(rows), took)
        plan.append({"tab": TAB_NAMES[tab], "mode": TAB_MODES[tab], "rows": rows, "chunks": chunk_rows(rows)})
    return plan


def expected_headers() -> dict[str, list[str]]:
    return {"Winners": WINNERS_HEADERS, "Stores": STORES_HEADERS}


def check_deployed_headers(session: requests.Session, url: str, tabs=TAB_ORDER) -> list[str]:
    """Compare the headers the deployed Code.gs writes with the columns this code sends. Returns the names of
    tabs that differ (a stale deployment would put every value under the wrong column). An old deployment
    that does not report headers yields ['?'] so the caller can warn instead of writing blind."""
    try:
        data = get_json(session, url, {"tabs": "1"})
    except SheetsSyncError as e:
        raise SheetsSyncError(f"cannot read the deployed Code.gs ({e})") from e
    deployed = data.get("headers")
    if not isinstance(deployed, dict):
        return ["?"]
    want = expected_headers()
    return [TAB_NAMES[t] for t in tabs if deployed.get(TAB_NAMES[t]) != want[TAB_NAMES[t]]]


class SyncLock:
    """Two syncs at once interleave their chunks and a replace-mode tab ends up holding a random subset of rows.
    One sync at a time per database: a lock file next to it."""

    def __init__(self, path: Path | None = None):
        self.path = path or (config.DB_PATH.parent / "sync.lock")
        self.held = False

    def __enter__(self):
        try:
            if self.path.exists():
                age = time.time() - self.path.stat().st_mtime
                if age < 3 * 3600:
                    raise SheetsSyncError(f"another sync-sheets started {age / 60:.0f} min ago and has not finished ({self.path}); "
                                          "wait for it, or delete the file if that process is gone")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(f"{os.getpid()} {datetime.now().isoformat()}")
            self.held = True
        except OSError:
            self.held = False
        return self

    def __exit__(self, *a):
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


def sync(conn: sqlite3.Connection, url: str, tabs=TAB_ORDER, as_of: str | None = None,
         session: requests.Session | None = None, dry_run: bool = False, check_headers: bool = True) -> list[dict]:
    """POST every tab. Returns one summary dict per tab: tab, rows, chunks, written, skipped.
    Refuses to write when the deployed Code.gs carries different headers than this code (column shift)."""
    if not url and not dry_run:
        raise SheetsSyncError("SHEETS_WEBHOOK_URL is not set (put it in .env)")
    with SyncLock():
        return _sync(conn, url, tabs, as_of, session, dry_run, check_headers)


def _sync(conn, url, tabs, as_of, session, dry_run, check_headers) -> list[dict]:
    session = session or requests.Session()
    if url and not dry_run and check_headers:
        bad = check_deployed_headers(session, url, tabs)
        if bad == ["?"]:
            log.warning("sheets: the deployed Code.gs does not report its headers (old version); cannot check for a column shift. "
                        "Re-paste sheets/Code.gs and deploy a new version.")
        elif bad:
            raise SheetsSyncError(f"the deployed Code.gs writes different columns than this code for: {', '.join(bad)}. "
                                  "Every value would land under the wrong header. Paste sheets/Code.gs into the Apps Script editor "
                                  "and Deploy > Manage deployments > Edit > New version, then sync again.")
    summaries = []
    for item in build_plan(conn, tabs, as_of):
        n = len(item["chunks"])
        written = skipped = 0
        sent = 0   # rows the tab holds once every chunk so far has been applied (replace mode)
        for i, chunk in enumerate(item["chunks"], start=1):
            payload = {"tab": item["tab"], "mode": item["mode"], "chunk": i, "chunks": n, "rows": chunk}
            size = _size(payload)
            if dry_run:
                log.info("sheets: [dry-run] %s chunk %d/%d: %d rows, %d bytes", item["tab"], i, n, len(chunk), size)
                continue
            expected_after = sent + len(chunk)
            applied = (lambda tab=item["tab"], want=expected_after: tab_count(session, url, tab) == want)
            resp = post_payload(session, url, payload, already_applied=applied)
            sent += len(chunk)
            written += int(resp.get("written", 0))
            skipped += int(resp.get("skipped", 0))
            log.info("sheets: %s chunk %d/%d: %d rows -> written %s skipped %s (%d bytes)", item["tab"], i, n,
                     len(chunk), resp.get("written"), resp.get("skipped"), size)
        summaries.append({"tab": item["tab"], "rows": len(item["rows"]), "chunks": n, "written": written, "skipped": skipped})
    return summaries
