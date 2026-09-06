"""Google Sheets sync via an Apps Script web app (sheets/Code.gs).

The client side is deliberately simple: build rows from SQLite, cut them into
JSON chunks under SHEETS_CHUNK_BYTES, POST each chunk, follow the 302 that Apps
Script answers POSTs with, retry on 5xx/network errors, and surface Apps Script's
own {"ok": false, "error": ...} replies as clear exceptions.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from urllib.parse import urljoin

import requests

from . import config, deltas

log = logging.getLogger("earlyscale.sheets")

STORES_HEADERS = ["store", "meta page", "last status", "products", "sold-out variants", "new products 7d",
                  "updated products 7d", "sold-out delta", "price changes", "change score", "last snapshot date"]
PRODUCTS_HEADERS = ["date", "store", "handle", "title", "published_at", "updated_at", "price",
                    "available variants", "total variants", "collection position"]
ALERTS_HEADERS = ["date", "store", "handle", "rule", "detail", "created_at"]

TAB_ORDER = ("stores", "products", "alerts")
TAB_NAMES = {"stores": "Stores", "products": "Products", "alerts": "Alerts"}
TAB_MODES = {"stores": "replace", "products": "append", "alerts": "append"}


class SheetsSyncError(Exception):
    """Fatal sync problem (bad URL, deployment not public, Apps Script error)."""


class _Retryable(SheetsSyncError):
    pass


# ---------------------------------------------------------------- rows from SQLite

def stores_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    by_id = {d.store_id: d for d in deltas.all_store_deltas(conn, as_of)}
    rows = []
    for s in conn.execute("SELECT id, store_domain, meta_page_name FROM stores ORDER BY store_domain"):
        last = conn.execute(
            "SELECT status, error FROM store_runs WHERE store_id = ? ORDER BY run_id DESC LIMIT 1", (s["id"],)
        ).fetchone()
        if last is None:
            status = "never run"
        elif last["status"] == "ok":
            status = "ok"
        else:
            status = "error: " + (last["error"] or "")[:120]
        d = by_id.get(s["id"])
        if d is None:
            rows.append([s["store_domain"], s["meta_page_name"] or "", status, "", "", "", "", "", "", "", ""])
            continue
        rows.append([
            d.store_domain, s["meta_page_name"] or "", status, d.products, d.sold_out_variants,
            d.new_products_7d, d.updated_products_7d,
            "" if d.sold_out_variants_delta is None else d.sold_out_variants_delta,
            "" if d.price_changes is None else d.price_changes,
            d.change_score, d.snapshot_date,
        ])
    rows.sort(key=lambda r: (-(r[9] if isinstance(r[9], int) else -1), r[0]))
    return rows


def products_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    rows = []
    for s in conn.execute("SELECT id, store_domain FROM stores ORDER BY store_domain"):
        dates = deltas.snapshot_dates(conn, s["id"], as_of)
        if not dates:
            continue
        for p in conn.execute(
            """SELECT snapshot_date, handle, title, published_at, updated_at, min_price, variant_count,
                      sold_out_variants, collection_position
               FROM products_daily WHERE store_id = ? AND snapshot_date = ? ORDER BY handle""",
            (s["id"], dates[0]),
        ):
            rows.append([
                p["snapshot_date"], s["store_domain"], p["handle"], p["title"] or "",
                p["published_at"] or "", p["updated_at"] or "",
                "" if p["min_price"] is None else p["min_price"],
                p["variant_count"] - p["sold_out_variants"], p["variant_count"],
                "" if p["collection_position"] is None else p["collection_position"],
            ])
    return rows


def alerts_rows(conn: sqlite3.Connection, as_of: str | None = None) -> list[list]:
    if as_of is None:
        row = conn.execute("SELECT MAX(snapshot_date) FROM alerts").fetchone()
        as_of = row[0] if row else None
        if as_of is None:
            return []
    return [
        [a["snapshot_date"], a["store_domain"], a["product_handle"] or "", a["rule"], a["detail"] or "",
         a["created_at"]]
        for a in conn.execute(
            """SELECT a.snapshot_date, s.store_domain, a.product_handle, a.rule, a.detail, a.created_at
               FROM alerts a JOIN stores s ON s.id = a.store_id WHERE a.snapshot_date = ? ORDER BY a.id""",
            (as_of,))
    ]


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


def post_payload(session: requests.Session, url: str, payload: dict, retries: int = 3) -> dict:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session.post(url, data=body, headers={"Content-Type": "application/json"},
                             timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
            hops = 0
            while r.status_code in (301, 302, 303, 307, 308) and hops < 5:
                # Apps Script answers every POST with a 302 to a one-time googleusercontent URL
                # that serves the script's response; that hop must be a GET.
                loc = urljoin(r.url, r.headers.get("Location", ""))
                r = session.get(loc, timeout=config.SHEETS_TIMEOUT, allow_redirects=False)
                hops += 1
            if r.status_code >= 500:
                raise _Retryable(f"HTTP {r.status_code} from Apps Script")
            if r.status_code != 200:
                raise SheetsSyncError(f"HTTP {r.status_code} from {r.url}: {r.text.strip()[:200]!r}")
            text = r.text.strip()
            if not text.startswith("{"):
                raise SheetsSyncError(_explain_non_json(text, r.status_code))
            data = json.loads(text)
            if not data.get("ok"):
                raise SheetsSyncError(f"Apps Script reported an error: {data.get('error')}")
            return data
        except (_Retryable, requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        if attempt < retries:
            wait = (2 ** attempt) * 2 + random.uniform(0, 1)
            log.warning("sheets: retry %d/%d after %s (sleep %.1fs)", attempt + 1, retries, last_err, wait)
            time.sleep(wait)
    raise SheetsSyncError(f"giving up after {retries + 1} attempts: {last_err}")


# ---------------------------------------------------------------- orchestration

def build_plan(conn: sqlite3.Connection, tabs=TAB_ORDER, as_of: str | None = None) -> list[dict]:
    builders = {"stores": stores_rows, "products": products_rows, "alerts": alerts_rows}
    plan = []
    for tab in TAB_ORDER:
        if tab not in tabs:
            continue
        rows = builders[tab](conn, as_of)
        plan.append({"tab": TAB_NAMES[tab], "mode": TAB_MODES[tab], "rows": rows, "chunks": chunk_rows(rows)})
    return plan


def sync(conn: sqlite3.Connection, url: str, tabs=TAB_ORDER, as_of: str | None = None,
         session: requests.Session | None = None, dry_run: bool = False) -> list[dict]:
    """POST every tab. Returns one summary dict per tab: tab, rows, chunks, written, skipped."""
    if not url and not dry_run:
        raise SheetsSyncError("SHEETS_WEBHOOK_URL is not set (put it in .env)")
    session = session or requests.Session()
    summaries = []
    for item in build_plan(conn, tabs, as_of):
        n = len(item["chunks"])
        written = skipped = 0
        for i, chunk in enumerate(item["chunks"], start=1):
            payload = {"tab": item["tab"], "mode": item["mode"], "chunk": i, "chunks": n, "rows": chunk}
            size = _size(payload)
            if dry_run:
                log.info("sheets: [dry-run] %s chunk %d/%d: %d rows, %d bytes", item["tab"], i, n, len(chunk), size)
                continue
            resp = post_payload(session, url, payload)
            written += int(resp.get("written", 0))
            skipped += int(resp.get("skipped", 0))
            log.info("sheets: %s chunk %d/%d: %d rows -> written %s skipped %s (%d bytes)", item["tab"], i, n,
                     len(chunk), resp.get("written"), resp.get("skipped"), size)
        summaries.append({"tab": item["tab"], "rows": len(item["rows"]), "chunks": n,
                          "written": written, "skipped": skipped})
    return summaries
