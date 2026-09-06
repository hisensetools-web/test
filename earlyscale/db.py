"""SQLite schema and snapshot writers.

History is never overwritten: every table keyed by snapshot_date is append-only
across days. Re-running the same day upserts that day's rows (idempotent).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id              INTEGER PRIMARY KEY,
    store_domain    TEXT NOT NULL UNIQUE,
    meta_page_name  TEXT,
    meta_page_id    TEXT,
    notes           TEXT,
    added_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY,
    snapshot_date   TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    stores_total    INTEGER,
    stores_ok       INTEGER,
    stores_failed   INTEGER
);

CREATE TABLE IF NOT EXISTS store_runs (
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    snapshot_date   TEXT NOT NULL,
    status          TEXT NOT NULL,          -- ok | error
    error           TEXT,
    products_seen   INTEGER,
    pages_fetched   INTEGER,
    duration_s      REAL,
    PRIMARY KEY (run_id, store_id)
);

CREATE TABLE IF NOT EXISTS products_daily (
    snapshot_date       TEXT NOT NULL,
    store_id            INTEGER NOT NULL REFERENCES stores(id),
    product_id          INTEGER NOT NULL,
    handle              TEXT NOT NULL,
    title               TEXT,
    vendor              TEXT,
    product_type        TEXT,
    tags                TEXT,               -- JSON array
    created_at          TEXT,
    published_at        TEXT,
    updated_at          TEXT,
    variant_count       INTEGER NOT NULL,
    sold_out_variants   INTEGER NOT NULL,
    min_price           REAL,
    max_price           REAL,
    collection_position INTEGER,            -- 0-based index in /collections/all, NULL if absent
    fetched_at          TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, store_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_products_daily_handle ON products_daily(store_id, handle, snapshot_date);

CREATE TABLE IF NOT EXISTS variants_daily (
    snapshot_date       TEXT NOT NULL,
    store_id            INTEGER NOT NULL REFERENCES stores(id),
    product_id          INTEGER NOT NULL,
    variant_id          INTEGER NOT NULL,
    title               TEXT,
    sku                 TEXT,
    price               REAL,
    compare_at_price    REAL,
    available           INTEGER NOT NULL,   -- 0/1
    PRIMARY KEY (snapshot_date, store_id, variant_id)
);

-- Meta Ad Library (Part B). One row per ad ever seen; texts/links refreshed on each sighting.
CREATE TABLE IF NOT EXISTS meta_ads (
    ad_id               TEXT PRIMARY KEY,   -- ad_archive_id
    store_id            INTEGER NOT NULL REFERENCES stores(id),
    page_id             TEXT,
    page_name           TEXT,
    ad_start_date       TEXT,               -- start date shown in the library (first_seen in the spec)
    ad_end_date         TEXT,
    first_seen_date     TEXT NOT NULL,      -- our first snapshot containing it
    last_seen_date      TEXT NOT NULL,
    primary_text        TEXT,
    headline            TEXT,
    landing_url         TEXT,
    landing_domain      TEXT,
    caption             TEXT,
    cta                 TEXT,
    creative_type       TEXT,               -- image | video | carousel | ...
    asset_url           TEXT,
    platforms           TEXT,
    fingerprint         TEXT,               -- sha1(asset url sans query + text)[:16]
    page_handle         TEXT,               -- /pages/<handle> advertorials (increment 2)
    product_handle      TEXT,               -- resolved product (increment 2)
    query               TEXT,
    raw_json            TEXT
);
CREATE INDEX IF NOT EXISTS idx_meta_ads_store ON meta_ads(store_id, last_seen_date);

-- One row per ad per snapshot day. is_active=0 rows are written when a previously seen
-- ad no longer appears in the active search (that is how disappearance is tracked).
CREATE TABLE IF NOT EXISTS meta_ads_daily (
    snapshot_date       TEXT NOT NULL,
    ad_id               TEXT NOT NULL REFERENCES meta_ads(ad_id),
    store_id            INTEGER NOT NULL REFERENCES stores(id),
    is_active           INTEGER NOT NULL,
    position            INTEGER,            -- order in the search results
    eu_total_reach      INTEGER,
    reactions           INTEGER,            -- Ad Library does not expose these; NULL unless present
    comments            INTEGER,
    shares              INTEGER,
    collation_count     INTEGER,
    fetched_at          TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, ad_id)
);

-- One row per concept per day (concept = page + landing URL + launch dates within 1 day).
CREATE TABLE IF NOT EXISTS meta_concepts_daily (
    snapshot_date   TEXT NOT NULL,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    concept_id      TEXT NOT NULL,
    page_name       TEXT,
    landing_url     TEXT,
    product_handle  TEXT,
    page_handle     TEXT,
    launch_date     TEXT,
    days_running    INTEGER,
    ads_ever        INTEGER,
    ads_active      INTEGER,
    survival        REAL,               -- ads_active / ads_ever
    PRIMARY KEY (snapshot_date, concept_id)
);

-- Per page per day: how much of a page's traffic lands on the store; pages that never do
-- are "ignored" (keyword search picked up an unrelated advertiser).
CREATE TABLE IF NOT EXISTS meta_pages_daily (
    snapshot_date   TEXT NOT NULL,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    page_name       TEXT NOT NULL,
    page_id         TEXT,
    ads             INTEGER NOT NULL,
    ads_with_url    INTEGER NOT NULL,
    on_store        INTEGER NOT NULL,   -- ads landing on the store domain or resolved to a product
    ignored         INTEGER NOT NULL,
    PRIMARY KEY (snapshot_date, store_id, page_name)
);

-- Fetched ad landing pages (advertorials, redirectors) and what product they point at.
CREATE TABLE IF NOT EXISTS landing_pages (
    url             TEXT PRIMARY KEY,   -- sans query string
    fetched_at      TEXT NOT NULL,
    status          INTEGER,
    final_url       TEXT,
    product_handle  TEXT,
    page_handle     TEXT,
    candidates      TEXT                -- handle:count,... found on the page
);

CREATE TABLE IF NOT EXISTS meta_page_runs (
    id              INTEGER PRIMARY KEY,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    snapshot_date   TEXT NOT NULL,
    query           TEXT,
    status          TEXT NOT NULL,          -- ok | blocked | error | skipped
    detail          TEXT,
    ads_found       INTEGER,
    scrolls         INTEGER,
    duration_s      REAL,
    ran_at          TEXT NOT NULL
);

-- Populated from build step 5.
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY,
    snapshot_date   TEXT NOT NULL,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    product_handle  TEXT,
    rule            INTEGER NOT NULL,
    detail          TEXT,
    created_at      TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path is not None else config.DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Small, idempotent schema fixes for databases created by earlier versions."""
    # ads_daily was a placeholder from build step 1; Part B replaced it with meta_ads*.
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ads_daily'").fetchone():
        if conn.execute("SELECT COUNT(*) FROM ads_daily").fetchone()[0] == 0:
            conn.execute("DROP TABLE ads_daily")
    # Part B increment 2 columns (ALTER is idempotent via the column check).
    wanted = {
        "meta_ads": [("concept_id", "TEXT"), ("lineage_of", "TEXT"), ("lineage_similarity", "REAL"),
                     ("landing_resolved_via", "TEXT"), ("landing_handle", "TEXT"), ("page_ignored", "INTEGER"),
                     ("post_url", "TEXT"), ("engagement_type", "TEXT"), ("reach_keys", "TEXT")],
        "alerts": [("dedupe_key", "TEXT")],
        "products_daily": [("unlisted", "INTEGER DEFAULT 0")],   # 1 = live product page not in products.json (found via ads)
        "meta_ads_daily": [("days_running", "INTEGER"), ("engagement", "INTEGER"), ("engagement_delta", "INTEGER"),
                           ("engagement_per_day", "REAL"),
                           # reach curve (EU exact, UK exact or range) and the comment curve
                           ("uk_reach", "INTEGER"), ("reach_range_lower", "INTEGER"), ("reach_range_upper", "INTEGER"),
                           ("reach_source", "TEXT"), ("reach_delta_1d", "INTEGER"), ("reach_slope_7d", "REAL"),
                           ("reach_slope_prev_7d", "REAL"), ("comment_delta_1d", "INTEGER"), ("post_status", "TEXT")],

    }
    for table, cols in wanted.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    conn.commit()


# ---------------------------------------------------------------- stores

def upsert_store(conn: sqlite3.Connection, domain: str, meta_page_name: str | None = None,
                 meta_page_id: str | None = None, notes: str | None = None) -> int:
    """Insert the store if new; update non-empty metadata if it exists. Returns store id."""
    row = conn.execute("SELECT id FROM stores WHERE store_domain = ?", (domain,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO stores (store_domain, meta_page_name, meta_page_id, notes, added_at) VALUES (?,?,?,?,?)",
            (domain, meta_page_name or None, meta_page_id or None, notes or None, utcnow_iso()),
        )
        return int(cur.lastrowid)
    conn.execute(
        """UPDATE stores SET meta_page_name = COALESCE(NULLIF(?, ''), meta_page_name),
                             meta_page_id   = COALESCE(NULLIF(?, ''), meta_page_id),
                             notes          = COALESCE(NULLIF(?, ''), notes)
           WHERE id = ?""",
        (meta_page_name or "", meta_page_id or "", notes or "", row["id"]),
    )
    return int(row["id"])


# ---------------------------------------------------------------- runs

def start_run(conn: sqlite3.Connection, snapshot_date: str, stores_total: int) -> int:
    cur = conn.execute(
        "INSERT INTO runs (snapshot_date, started_at, stores_total) VALUES (?,?,?)",
        (snapshot_date, utcnow_iso(), stores_total),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, ok: int, failed: int) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, stores_ok = ?, stores_failed = ? WHERE id = ?",
        (utcnow_iso(), ok, failed, run_id),
    )
    conn.commit()


def record_store_run(conn: sqlite3.Connection, run_id: int, store_id: int, snapshot_date: str,
                     status: str, error: str | None, products_seen: int, pages_fetched: int,
                     duration_s: float) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO store_runs
           (run_id, store_id, snapshot_date, status, error, products_seen, pages_fetched, duration_s)
           VALUES (?,?,?,?,?,?,?,?)""",
        (run_id, store_id, snapshot_date, status, error, products_seen, pages_fetched, round(duration_s, 2)),
    )
    conn.commit()


# ---------------------------------------------------------------- snapshots

def write_product_snapshot(conn: sqlite3.Connection, store_id: int, snapshot_date: str,
                           products: list[dict], fetched_at: str | None = None) -> int:
    """Write one day's snapshot for a store. `products` are normalised records from
    shopify.normalise_products(). Same-day re-runs replace that day's rows only."""
    fetched_at = fetched_at or utcnow_iso()
    with conn:  # single transaction: a crash mid-write leaves no partial day
        conn.execute("DELETE FROM products_daily WHERE snapshot_date = ? AND store_id = ? AND COALESCE(unlisted, 0) = 0",
                     (snapshot_date, store_id))
        conn.execute("""DELETE FROM variants_daily WHERE snapshot_date = ? AND store_id = ? AND product_id NOT IN
                        (SELECT product_id FROM products_daily WHERE snapshot_date = ? AND store_id = ? AND unlisted = 1)""",
                     (snapshot_date, store_id, snapshot_date, store_id))
        conn.executemany(
            """INSERT INTO products_daily
               (snapshot_date, store_id, product_id, handle, title, vendor, product_type, tags,
                created_at, published_at, updated_at, variant_count, sold_out_variants,
                min_price, max_price, collection_position, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (snapshot_date, store_id, p["product_id"], p["handle"], p["title"], p["vendor"],
                 p["product_type"], json.dumps(p["tags"]), p["created_at"], p["published_at"],
                 p["updated_at"], p["variant_count"], p["sold_out_variants"], p["min_price"],
                 p["max_price"], p["collection_position"], fetched_at)
                for p in products
            ],
        )
        # unlisted products discovered through ads are re-attached to the new day's snapshot
        # (write_product_snapshot only replaces the listed catalogue)
        conn.executemany(
            """INSERT INTO variants_daily
               (snapshot_date, store_id, product_id, variant_id, title, sku, price, compare_at_price, available)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (snapshot_date, store_id, p["product_id"], v["variant_id"], v["title"], v["sku"],
                 v["price"], v["compare_at_price"], 1 if v["available"] else 0)
                for p in products for v in p["variants"]
            ],
        )
    return len(products)


def write_unlisted_product(conn: sqlite3.Connection, store_id: int, snapshot_date: str, p: dict,
                           fetched_at: str | None = None) -> None:
    """Add one product that is live on the store but absent from products.json (found because
    ads point at it). Idempotent per (day, store, product)."""
    fetched_at = fetched_at or utcnow_iso()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO products_daily
               (snapshot_date, store_id, product_id, handle, title, vendor, product_type, tags,
                created_at, published_at, updated_at, variant_count, sold_out_variants,
                min_price, max_price, collection_position, fetched_at, unlisted)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (snapshot_date, store_id, p["product_id"], p["handle"], p["title"], p["vendor"], p["product_type"],
             json.dumps(p["tags"]), p["created_at"], p["published_at"], p["updated_at"], p["variant_count"],
             p["sold_out_variants"], p["min_price"], p["max_price"], None, fetched_at))
        conn.executemany(
            """INSERT OR REPLACE INTO variants_daily
               (snapshot_date, store_id, product_id, variant_id, title, sku, price, compare_at_price, available)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(snapshot_date, store_id, p["product_id"], v["variant_id"], v["title"], v["sku"], v["price"],
              v["compare_at_price"], 1 if v["available"] else 0) for v in p["variants"]])
