"""SQLite schema, migration from the old layout, and the snapshot writers.

History is never overwritten: every table keyed by snapshot_date is append-only across days.
Re-running the same day replaces that day's rows for the store (idempotent).

Six data points, nothing else (see CLAUDE.md):
  ads / ads_daily      per ad: id, page, landing URL (raw + normalised path), first_seen, still_active, low_impressions
  stores               shop_id + myshopify handle from the storefront HTML
  products_daily       per product: id, handle, title, created_at, published_at
  url_daily            derived per landing path per day (winners.py)
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger("earlyscale.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id              INTEGER PRIMARY KEY,
    store_domain    TEXT NOT NULL UNIQUE,
    meta_page_name  TEXT,
    meta_page_id    TEXT,
    notes           TEXT,
    added_at        TEXT NOT NULL,
    shop_id         INTEGER,                -- Shopify shop id from the storefront HTML (never changes)
    myshopify       TEXT,                   -- x.myshopify.com
    shop_id_source  TEXT,
    shop_id_checked_at TEXT,
    shop_id_error   TEXT,
    store_created_est TEXT,                 -- from calibration/shop_ids.csv (store_age.py)
    store_created_method TEXT,
    platform        TEXT,                   -- shopify | shopify_headless | woocommerce | ... (platforms.py)
    platform_base   TEXT,
    platform_checked_at TEXT,
    platform_note   TEXT
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

-- Every product of every store, once per day: id, handle, title and the two dates. Nothing else.
CREATE TABLE IF NOT EXISTS products_daily (
    snapshot_date   TEXT NOT NULL,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    product_id      INTEGER NOT NULL,
    handle          TEXT NOT NULL,
    title           TEXT,
    created_at      TEXT,
    published_at    TEXT,
    updated_at      TEXT,
    url_path        TEXT,                   -- /products/<handle> on Shopify; the product page path elsewhere
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, store_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_products_daily_handle ON products_daily(store_id, handle, snapshot_date);
-- The day this tracker first saw a product URL: created_at for platforms that do not publish dates.
CREATE TABLE IF NOT EXISTS product_first_seen (
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    url_path        TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    PRIMARY KEY (store_id, url_path)
);
-- Product pages read by the generic (sitemap + JSON-LD) adapter, refreshed every CATALOGUE_REFRESH_DAYS.
CREATE TABLE IF NOT EXISTS product_pages (
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    url_path        TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_fetched    TEXT,
    status          INTEGER,
    lastmod         TEXT,
    json            TEXT,
    PRIMARY KEY (store_id, url_path)
);

-- One row per ad ever seen for a store. The landing URL is the key to everything: no product resolution.
CREATE TABLE IF NOT EXISTS ads (
    ad_id           TEXT NOT NULL,          -- ad_archive_id
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    page_id         TEXT,
    page_name       TEXT,
    landing_url     TEXT,                   -- raw, as shown on the card
    landing_path    TEXT,                   -- normalised: host + path, lower-case, no query string, no trailing slash
    first_seen      TEXT,                   -- the Ad Library's start date
    first_scraped   TEXT NOT NULL,          -- our first snapshot containing it
    last_scraped    TEXT NOT NULL,
    primary_text    TEXT,
    PRIMARY KEY (ad_id, store_id)
);
CREATE INDEX IF NOT EXISTS idx_ads_store_path ON ads(store_id, landing_path);

-- One row per ad per scrape day. still_active = the ad was present in that day's active search;
-- low_impressions = the 'Low impression count' badge on the card: 1 / 0, NULL only when the card never rendered
-- (such an ad is never counted as delivering, and the sheet never shows a '?').
CREATE TABLE IF NOT EXISTS ads_daily (
    snapshot_date   TEXT NOT NULL,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    ad_id           TEXT NOT NULL,
    still_active    INTEGER NOT NULL,
    low_impressions INTEGER,
    position        INTEGER,                -- order in the search results
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, store_id, ad_id)
);
CREATE INDEX IF NOT EXISTS idx_ads_daily_store ON ads_daily(store_id, snapshot_date);

-- Derived per landing path per day (winners.py rewrites a store's rows for the day after each scrape).
CREATE TABLE IF NOT EXISTS url_daily (
    snapshot_date       TEXT NOT NULL,
    store_id            INTEGER NOT NULL REFERENCES stores(id),
    landing_path        TEXT NOT NULL,
    delivering          INTEGER NOT NULL,   -- active ads on the URL without the badge
    delivering_7d_ago   INTEGER,            -- NULL = no scrape 6-8 days earlier
    proven_days         INTEGER,            -- age of the oldest still-delivering ad on the URL
    pages               INTEGER NOT NULL,   -- distinct page_ids with a delivering ad
    pages_new_7d        INTEGER NOT NULL,   -- pages whose first delivering ad on the URL is < 7 days old
    top_page            TEXT,
    family_key          TEXT,               -- only for /products/ paths
    family_created      TEXT,               -- oldest created_at in the family
    PRIMARY KEY (snapshot_date, store_id, landing_path)
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

CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
"""

# Tables of the previous layout that carry nothing the six data points need. Dropped by the migration.
OBSOLETE_TABLES = ("variants_daily", "meta_ads_daily", "meta_ads", "meta_concepts_daily", "meta_pages_daily", "landing_pages",
                   "meta_ad_detail_daily", "meta_page_likes_daily", "meta_creatives", "fb_posts_daily", "fb_posts", "rank_checks",
                   "hero_variants", "inventory_daily", "alerts", "products_daily_old",
                   "radar_domains", "radar_ads", "radar_ad_hits", "radar_runs")

# Columns added to a table after it was first created (ALTER is idempotent via the column check).
MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "stores": [("shop_id", "INTEGER"), ("myshopify", "TEXT"), ("shop_id_source", "TEXT"), ("shop_id_checked_at", "TEXT"),
               ("shop_id_error", "TEXT"), ("store_created_est", "TEXT"), ("store_created_method", "TEXT"),
               ("platform", "TEXT"), ("platform_base", "TEXT"), ("platform_checked_at", "TEXT"), ("platform_note", "TEXT")],
}

# One-off steps, applied once each (recorded in schema_migrations) and part of the schema stamp. The names of the
# steps of the previous layout are kept so a database that already ran them is not asked to run them again.
MIGRATION_STEPS: list[tuple[str, str]] = [
    ("2026-09-10-clear-non-informative-ranks", ""),
    ("2026-09-10-refetch-ambiguous-landers", ""),
    ("2026-09-10-landing-resolver-version", ""),
    ("2026-09-13-six-data-points", "six_data_points"),      # python step, see _migrate_six_data_points
    ("2026-09-13-drop-radar", "drop_radar"),
]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path is not None else config.DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)   # wait for another tracker command instead of "database is locked"
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    stamp = schema_stamp()
    if conn.execute("PRAGMA user_version").fetchone()[0] != stamp:
        _setup(conn, stamp)
    return conn


def schema_stamp() -> int:
    """Checksum of the schema + migration list: the database carries it in user_version once set up, so a
    connection whose code version matches does no write at all on open (read-only commands never take a lock)."""
    return zlib.crc32(SCHEMA.encode() + repr(MIGRATION_COLUMNS).encode() + repr([n for n, _ in MIGRATION_STEPS]).encode()) & 0x7FFFFFFF


def _setup(conn: sqlite3.Connection, stamp: int, attempts: int = 6) -> None:
    """Create / migrate the schema once per code version, inside one write transaction. If another tracker command
    (a running ads pass, the scheduled task) holds the database, wait and retry instead of failing on open."""
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA foreign_keys=OFF")     # dropping the old tables must not trip on their references
            conn.execute("BEGIN IMMEDIATE")
            vacuum = _migrate(conn)
            conn.execute(f"PRAGMA user_version = {int(stamp)}")
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
            if vacuum:
                _vacuum(conn)
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == attempts - 1:
                raise
            conn.rollback()
            wait = 5 + 5 * attempt
            log.warning("database is in use by another tracker command; waiting %ds before retrying the schema check (%d/%d)",
                        wait, attempt + 1, attempts - 1)
            time.sleep(wait)


def _vacuum(conn: sqlite3.Connection) -> None:
    """Give the space of the dropped tables back to the file system (outside any transaction; may take minutes)."""
    try:
        before = conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
        log.info("vacuuming the database after dropping the old tables (%.0f MB; this can take a few minutes) ...", before / 1e6)
        conn.execute("VACUUM")
        after = conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
        log.info("vacuum done: %.0f MB -> %.0f MB", before / 1e6, after / 1e6)
    except sqlite3.OperationalError as e:
        log.warning("vacuum skipped (%s); the file keeps its old size until the next successful vacuum", e)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _migrate(conn: sqlite3.Connection) -> bool:
    """Bring any database (fresh, or from the previous layout) to the current schema. Returns True when tables
    were dropped and a VACUUM is worth running."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    dropped = False
    # products_daily from the old layout carries many more columns: rebuild it minimal before CREATE TABLE IF NOT EXISTS
    if _table_exists(conn, "products_daily") and "variant_count" in _columns(conn, "products_daily"):
        _rebuild_products_daily(conn)
        dropped = True
    for stmt in re.sub(r"--[^\n]*", "", SCHEMA).split(";"):
        if stmt.strip():
            conn.execute(stmt)
    for table, cols in MIGRATION_COLUMNS.items():
        have = _columns(conn, table)
        for name, typ in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    for name, step in MIGRATION_STEPS:
        if name in done:
            continue
        if step == "six_data_points":
            dropped = _migrate_six_data_points(conn) or dropped
        elif step == "drop_radar":
            for t in ("radar_domains", "radar_ads", "radar_ad_hits", "radar_runs"):
                if _table_exists(conn, t):
                    conn.execute(f"DROP TABLE {t}")
                    dropped = True
            for idx in ("idx_radar_ads_domain", "idx_radar_hits_source", "idx_radar_hits_domain"):
                conn.execute(f"DROP INDEX IF EXISTS {idx}")
        conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)", (name, utcnow_iso()))
    return dropped


def _rebuild_products_daily(conn: sqlite3.Connection) -> None:
    """The old products_daily (variants, prices, tags, collection position) becomes the minimal one; unlisted rows
    (products invented from ad URLs) are not carried over: only products.json is a data point."""
    log.info("migrating products_daily to the minimal layout (id, handle, title, dates) ...")
    have = _columns(conn, "products_daily")
    unlisted = "COALESCE(unlisted, 0) = 0" if "unlisted" in have else "1 = 1"
    url_path = "url_path" if "url_path" in have else "'/products/' || handle"
    conn.execute("ALTER TABLE products_daily RENAME TO products_daily_old")
    conn.execute("DROP INDEX IF EXISTS idx_products_daily_handle")
    conn.execute("""CREATE TABLE products_daily (
        snapshot_date TEXT NOT NULL, store_id INTEGER NOT NULL REFERENCES stores(id), product_id INTEGER NOT NULL,
        handle TEXT NOT NULL, title TEXT, created_at TEXT, published_at TEXT, updated_at TEXT, url_path TEXT,
        fetched_at TEXT NOT NULL, PRIMARY KEY (snapshot_date, store_id, product_id))""")
    conn.execute(f"""INSERT OR IGNORE INTO products_daily (snapshot_date, store_id, product_id, handle, title, created_at, published_at,
                       updated_at, url_path, fetched_at)
                     SELECT snapshot_date, store_id, product_id, handle, title, created_at, published_at, updated_at,
                            COALESCE({url_path}, '/products/' || handle), fetched_at
                     FROM products_daily_old WHERE {unlisted}""")
    conn.execute("DROP TABLE products_daily_old")


def _migrate_six_data_points(conn: sqlite3.Connection) -> bool:
    """Copy the ads of the previous layout (meta_ads / meta_ads_daily) into ads / ads_daily, then drop every table
    that carried a deleted signal. Idempotent: runs once, recorded in schema_migrations."""
    from .winners import normalise_landing_path
    dropped = False
    if _table_exists(conn, "meta_ads") and _table_exists(conn, "meta_ads_daily"):
        n_ads = conn.execute("SELECT COUNT(*) FROM meta_ads").fetchone()[0]
        log.info("migrating %d ads from the previous layout ...", n_ads)
        rows = conn.execute("""SELECT ad_id, store_id, page_id, page_name, ad_start_date, first_seen_date, last_seen_date, landing_url,
                                      substr(COALESCE(primary_text, ''), 1, 500) AS primary_text FROM meta_ads""").fetchall()
        conn.executemany("""INSERT OR IGNORE INTO ads (ad_id, store_id, page_id, page_name, landing_url, landing_path, first_seen,
                                                       first_scraped, last_scraped, primary_text) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         [(r["ad_id"], r["store_id"], r["page_id"], r["page_name"], r["landing_url"], normalise_landing_path(r["landing_url"]),
                           r["ad_start_date"] or r["first_seen_date"], r["first_seen_date"], r["last_seen_date"], r["primary_text"] or None)
                          for r in rows])
        have = _columns(conn, "meta_ads_daily")
        low = "low_impressions" if "low_impressions" in have else "NULL"
        conn.execute(f"""INSERT OR IGNORE INTO ads_daily (snapshot_date, store_id, ad_id, still_active, low_impressions, position, fetched_at)
                         SELECT snapshot_date, store_id, ad_id, is_active, {low}, position, fetched_at FROM meta_ads_daily""")
    for t in OBSOLETE_TABLES:
        if _table_exists(conn, t):
            conn.execute(f"DROP TABLE {t}")
            dropped = True
    for idx in ("idx_meta_ads_store", "idx_meta_ads_handle", "idx_meta_ads_daily_store", "idx_page_likes_page", "idx_meta_creatives_hash",
                "idx_radar_ads_domain", "idx_radar_hits_source", "idx_radar_hits_domain"):
        conn.execute(f"DROP INDEX IF EXISTS {idx}")
    # url_daily for the days already in the database, so the sheet shows history right after the update
    if _table_exists(conn, "ads_daily"):
        from .winners import rebuild_url_daily
        rebuild_url_daily(conn)
    return dropped


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
    cur = conn.execute("INSERT INTO runs (snapshot_date, started_at, stores_total) VALUES (?,?,?)",
                       (snapshot_date, utcnow_iso(), stores_total))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, ok: int, failed: int) -> None:
    conn.execute("UPDATE runs SET finished_at = ?, stores_ok = ?, stores_failed = ? WHERE id = ?", (utcnow_iso(), ok, failed, run_id))
    conn.commit()


def record_store_run(conn: sqlite3.Connection, run_id: int, store_id: int, snapshot_date: str,
                     status: str, error: str | None, products_seen: int, pages_fetched: int, duration_s: float) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO store_runs (run_id, store_id, snapshot_date, status, error, products_seen, pages_fetched, duration_s)
           VALUES (?,?,?,?,?,?,?,?)""",
        (run_id, store_id, snapshot_date, status, error, products_seen, pages_fetched, round(duration_s, 2)))
    conn.commit()


# ---------------------------------------------------------------- snapshots

def write_product_snapshot(conn: sqlite3.Connection, store_id: int, snapshot_date: str,
                           products: list[dict], fetched_at: str | None = None) -> int:
    """Write one day's catalogue for a store: product id, handle, title, created_at, published_at (+ updated_at and
    the page path). `products` are normalised records (shopify.normalise_products / platforms.make_product);
    extra keys are ignored. Same-day re-runs replace that day's rows only."""
    fetched_at = fetched_at or utcnow_iso()
    with conn:  # single transaction: a crash mid-write leaves no partial day
        conn.execute("DELETE FROM products_daily WHERE snapshot_date = ? AND store_id = ?", (snapshot_date, store_id))
        conn.executemany(
            """INSERT INTO products_daily (snapshot_date, store_id, product_id, handle, title, created_at, published_at, updated_at,
                                           url_path, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(snapshot_date, store_id, p["product_id"], p["handle"], p.get("title"), p.get("created_at"), p.get("published_at"),
              p.get("updated_at"), p.get("url_path") or f"/products/{p['handle']}", fetched_at) for p in products])
    return len(products)


def latest_products(conn: sqlite3.Connection, store_id: int, as_of: str | None = None) -> tuple[str | None, list[dict]]:
    """(snapshot_date, products) of the store's latest catalogue on or before as_of."""
    d = conn.execute("SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ? AND snapshot_date <= ?",
                     (store_id, as_of or "9999")).fetchone()[0]
    if not d:
        return None, []
    rows = conn.execute("""SELECT product_id, handle, title, created_at, published_at, updated_at, url_path
                           FROM products_daily WHERE store_id = ? AND snapshot_date = ? ORDER BY handle""", (store_id, d))
    return d, [dict(r) for r in rows]
