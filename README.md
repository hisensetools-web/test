# Shopify Early-Scaling Tracker

Detects Shopify products in their first weeks of paid-social scaling by snapshotting
leading signals daily and alerting on week-over-week deltas. Full spec: [CLAUDE.md](CLAUDE.md).

**Status: build step 1 of 6** — `products.json` fetcher, SQLite schema, daily snapshot.
Deltas/report (step 2), Meta Ad Library (step 3), landing-URL join (4), alerts (5) and
cron docs (6) are not built yet.

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional; nothing is required for step 1
python tracker.py init-db     # creates data/tracker.db
```

## Daily run

```bash
python tracker.py run                 # snapshot every store in watchlist.csv for today
python tracker.py run --only gymshark.com
python tracker.py run --date 2026-09-05   # backfill/replace a specific day
python tracker.py status              # recent runs + per-store summary
```

Start running this daily as soon as it works for you. Week-over-week metrics need
at least 7–14 days of history before they mean anything.

`watchlist.csv` ships with three test stores (gymshark, allbirds, colourpop). Add more with:

```bash
python tracker.py add-store somestore.com            # tries to find facebook.com/<page> in the storefront HTML
python tracker.py add-store somestore.com --meta-page SomeStore --notes "found via ad"
```

## What gets stored (`data/tracker.db`)

| table | one row per | notes |
|---|---|---|
| `stores` | store | domain + Meta page name/id |
| `products_daily` | (day, store, product) | handle, title, type, tags (JSON), created/published/updated, variant_count, sold_out_variants, min/max price, `collection_position` (index in `/collections/all`, often best-selling order) |
| `variants_daily` | (day, store, variant) | price, compare_at_price, available |
| `runs`, `store_runs` | run / (run, store) | status, error text, products seen, pages, duration |
| `ads_daily`, `alerts` | — | created now, populated from steps 3 and 5 |

History is append-only across days. Re-running the same `--date` replaces only that day
for that store, so a crashed or partial run can be repeated safely.

## Looking at the data

No `sqlite3` CLI? `python -c "import sqlite3; ..."` works the same.

```sql
-- per-day totals
SELECT snapshot_date, COUNT(*) products, SUM(sold_out_variants) sold_out
FROM products_daily GROUP BY snapshot_date;

-- products published in the last 7 days
SELECT store_id, handle, published_at FROM products_daily
WHERE snapshot_date = date('now') AND published_at >= datetime('now', '-7 days');

-- variants whose price changed vs yesterday
SELECT t.store_id, t.product_id, y.price AS yesterday, t.price AS today
FROM variants_daily t JOIN variants_daily y
  ON y.store_id = t.store_id AND y.variant_id = t.variant_id
 AND y.snapshot_date = date(t.snapshot_date, '-1 day')
WHERE t.snapshot_date = date('now') AND t.price != y.price;
```

## Behaviour on failure

- Every request has a connect/read timeout and up to 3 retries with exponential backoff
  on connection errors, timeouts, HTTP 429/430 (Shopify rate limit) and 5xx.
- 401/403 (password-protected), 404 (not Shopify) and HTML responses fail fast, no retry.
- A failing store is logged in `store_runs.error` and the run continues with the next store.
- If `/collections/all/products.json` fails but `/products.json` works, the snapshot is still
  written with `collection_position = NULL`.
- Pagination stops on an empty page, a short page, a repeated page (some themes ignore
  `page=`), or after 60 pages.
- A configurable pause (`REQUEST_DELAY_S`, default 1s) sits between requests to the same store.

## Tests and offline end-to-end

```bash
python -m unittest discover -s tests -v
```

`tests/mock_store.py` is a fake Shopify storefront with real pagination semantics, so the
whole pipeline can be exercised without network access:

```bash
python -m tests.mock_store --port 8001 --products 320 --seed 1 &
python -m tests.mock_store --port 8002 --products 40  --seed 2 --fail-first 430 &   # tests retry
python -m tests.mock_store --port 8003 --products 610 --seed 3 &
python tracker.py run --watchlist tests/watchlist.mock.csv --date 2026-09-05
# restart the mocks with --mutate for a "day 2" with sold-outs, price cuts and 2 new products
python tracker.py run --watchlist tests/watchlist.mock.csv
python tracker.py status
```

## Layout

```
tracker.py              CLI entry point
earlyscale/cli.py       commands: init-db, add-store, run, status
earlyscale/shopify.py   HTTP fetch (retry/backoff/pagination) + pure normaliser + Meta page discovery
earlyscale/db.py        schema + snapshot writers
earlyscale/watchlist.py watchlist.csv I/O
earlyscale/config.py    paths, .env loader, tunables
tests/                  unit tests, fixture JSON, mock store server
```
