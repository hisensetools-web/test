# Shopify Early-Scaling Tracker

Detects Shopify products in their first weeks of paid-social scaling by snapshotting
leading signals daily and alerting on week-over-week deltas. Full spec: [CLAUDE.md](CLAUDE.md).

**Status: build steps 1–2 of 6** — `products.json` fetcher, SQLite schema, daily snapshot,
delta calculations and the `report` / `product` commands. Meta Ad Library (step 3),
landing-URL join (4), alerts (5) and the full cron/README pass (6) are not built yet.

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

## Report

```bash
python tracker.py report                 # stores sorted by change score, then product-level changes
python tracker.py report --date 2026-09-05 --top 20 --changes 100
python tracker.py product <handle>       # time series for one handle (add --store domain to narrow)
```

Per store the report shows:

| column | needs | meaning |
|---|---|---|
| `new 7d` | 1 day | products whose `published_at` is within the 7 days ending on the snapshot date (UTC) |
| `upd 7d` | 1 day | same, for `updated_at` |
| `sold-out` | 1 day | variants with `available: false` today |
| `Δ sold-out` | 2 days | change in that count vs the previous snapshot |
| `price Δ` | 2 days | variants whose price differs from the previous snapshot (new variants don't count) |
| `+handles` / `-handles` | 2 days | products that appeared / disappeared from products.json |
| `score` | — | `new 7d + upd 7d + |Δ sold-out| + price Δ + +handles + -handles`, the sort key |

"Previous snapshot" is the store's most recent earlier snapshot, not calendar yesterday, so
a missed day doesn't break the comparison. With one day of history the 2-day columns show
`n/a` and the `vs` column says `1 day`. The product changes table lists new handles,
sold-out / restocked products (with `(ALL)` when every variant is gone), price changes and
removed handles, ordered by store score.

## Google Sheets sync

Pushes the latest snapshot and the report numbers into a Google Sheet through a Google
Apps Script **web app**. No Google Cloud project, no service account, no API keys: you paste
one file into the Apps Script editor and copy one URL back. The code you paste is
[`sheets/Code.gs`](sheets/Code.gs).

### One-time setup (about 5 minutes)

1. **Create the spreadsheet.** Go to <https://sheets.new>, name it e.g. *Shopify Tracker*.
2. **Open the script editor from inside that sheet.** Menu *Extensions* > *Apps Script*.
   A new tab opens with a file called `Code.gs` containing an empty `myFunction`.
3. **Replace its contents.** Select everything in the editor (Ctrl+A), delete it, then
   paste the entire contents of `sheets/Code.gs` from this repo. Click the disk icon
   (or Ctrl+S) to save. Don't run anything.
4. **Deploy it as a web app.** Click the blue **Deploy** button (top right) > **New
   deployment**. Click the gear next to *Select type* and choose **Web app**. Fill in:
   - Description: anything, e.g. `tracker`
   - **Execute as: Me**
   - **Who has access: Anyone**  (this is what lets the tracker POST without logging in)

   Click **Deploy**. Google asks you to authorise: pick your account, click *Advanced* >
   *Go to ... (unsafe)* if it warns the app is unverified (it is your own script), then *Allow*.
5. **Copy the Web app URL.** It ends in `/exec`. Click *Copy*, then *Done*.
6. **Put the URL in `.env`.** In the project folder copy `.env.example` to `.env` if you
   have not already, and set:

   ```
   SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/AKfy.../exec
   ```

   `.env` is listed in `.gitignore`, so the URL never reaches GitHub. Anyone who has the
   URL can write to your sheet, so treat it like a password.

### Test it

```bash
python tracker.py sync-sheets --dry-run     # shows what would be sent, sends nothing
python tracker.py sync-sheets               # sends it
```

Open the /exec URL in a browser: it should show the word `ok`. After a sync the sheet has
three tabs, each with a bold frozen header row:

| tab | rows | behaviour |
|---|---|---|
| **Signals** | one per product (latest snapshot) | overwritten every sync; sorted by `days_since_published`. Columns: store, product family, handle, channel tag (from handle suffix: google, tiktok, taboola, fb, otp, sub, coc, vip, retired, variant), days_since_published, published_at, price, sold_out, collection_rank (1 = top of /collections/all), collection_rank_delta_7d (positive = climbed vs the snapshot 7+ days ago), variants_of_family_published_7d, then four Meta columns filled by Part B |
| **Families** | one per product family per store | overwritten; a family = handles sharing a base name or normalised title. Handle count, newest/oldest published_at, launches in 7/14/30 days, best rank, handle list. Sorted by 7-day launches |
| **Categories** | one per keyword category | overwritten; categories are title unigrams/bigrams shared by 2+ stores (nothing hardcoded), with store/family counts and newest publish date. Families with no shared keyword fall into `(uncategorised)` |
| **Stores** | one per store in watchlist.csv | fully overwritten every sync, sorted by change score |
| **Products** | one per product per snapshot date | appended; duplicates (same date + store + handle) are skipped, so syncing twice is safe. Kept at the end of the tab bar with the two variant-count columns hidden |
| **Alerts** | one per alert (build step 5) | appended; duplicates (date + store + handle + rule) skipped |

From then on `python tracker.py run` syncs automatically at the end whenever
`SHEETS_WEBHOOK_URL` is set (use `--no-sync` to skip). `run_daily.bat` needs no change: the
tracker reads `.env` itself. Exit code 3 means the run succeeded but the sync failed.

### After updating Code.gs

Whenever this repo's `sheets/Code.gs` changes (new tabs, new columns), paste the new file
over the old one in the Apps Script editor, save, then Deploy > **Manage deployments** >
pencil icon > Version: **New version** > Deploy. The URL stays the same. Tabs that already
exist keep their data; new tabs are created on the next sync.

### If something goes wrong

- **"Apps Script returned a web page instead of JSON"** - the deployment is not set to
  *Anyone*, or you pasted the wrong URL (it must be the `/exec` one).
- **You edited Code.gs and nothing changed** - a web app serves the *deployed version*.
  Deploy > *Manage deployments* > pencil icon > Version: *New version* > Deploy.
- **"unknown tab"** or another Apps Script error - the message comes straight from the
  script; check the paste was complete.
- **Sheet getting big** - Products grows by (products across all stores) rows per day.
  Google Sheets caps a file at 10 million cells (about 1 million Products rows). Delete
  old rows from the Products tab when needed; the SQLite database keeps full history.
- Advanced: `SHEETS_CHUNK_BYTES` (default 40000) sets the payload size per POST.

## Scheduling (Windows)

`run_daily.bat` probes, in order, `.venv\Scripts\python.exe`, `py -3`, `python` and `python3`,
and uses the first one that actually runs and reports Python 3.11 or newer (so a machine
with only 3.14 and no `py` launcher works, and the Microsoft Store `python` stub is skipped).
It then runs `tracker.py run` and appends stdout+stderr to `logs\run_YYYY-MM-DD.log`, with
the chosen interpreter and version in the start line. If nothing qualifies it logs an error
and exits with code 9009.
Register it in Task Scheduler for 06:00 daily from PowerShell in the project folder:

```powershell
.\register_task.ps1
```

which runs exactly:

```
schtasks /Create /TN "ShopifyTracker Daily" /TR "\"C:\path\to\run_daily.bat\"" /SC DAILY /ST 06:00 /F
```

Verify / test / remove:

```
schtasks /Query /TN "ShopifyTracker Daily" /V /FO LIST
schtasks /Run   /TN "ShopifyTracker Daily"
schtasks /Delete /TN "ShopifyTracker Daily" /F
```

Without `/RU` and `/RP` the task only fires while you are logged on. To run when logged off,
open the task's Properties in Task Scheduler and pick "Run whether user is logged on or not"
(or re-register with `/RU <user> /RP <password>`).

Linux/macOS cron equivalent (06:00 daily):

```
0 6 * * * cd /path/to/tracker && .venv/bin/python tracker.py run >> logs/run_$(date +\%F).log 2>&1
```

`watchlist.csv` ships with three test stores (gymshark, allbirds, colourpop). Add more with:

```bash
python tracker.py add-store somestore.com            # tries to find facebook.com/<page> in the storefront HTML
python tracker.py add-store somestore.com --meta-page SomeStore --notes "found via ad"
python tracker.py remove-store bad1.com bad2.com     # drops them from watchlist.csv, keeps DB history
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

- Requests carry a Chrome User-Agent and `Accept-Language` (some storefront WAFs answer 406
  to bot-looking agents, seen on olavita.co) and `Accept: application/json`. Do **not** send
  an HTML-first Accept: Shopify then serves the storefront HTML for `/products.json` with a
  200. If a store answers 406 to `application/json`, the request is repeated once with
  `Accept: */*`. Override the agent with `USER_AGENT` in `.env` if needed.
- An HTML or otherwise non-JSON body fails immediately with a descriptive
  `got HTML, not JSON from <url> (HTTP <status>, content-type <type>)` error in
  `store_runs.error`, never a JSON parse traceback.
- Before paginating, the host is resolved once: `GET /products.json?limit=1` following
  redirects, and the final origin (e.g. `https://www.store.com`) is reused for every later
  request. If the apex host is unreachable (connection error / timeout, seen on
  tryterrastrike.com) the `www.` variant is tried before giving up.
- Every request has a connect/read timeout and up to 3 retries with exponential backoff
  on connection errors, timeouts, HTTP 429/430 (Shopify rate limit) and 5xx.
- 401/403 (password-protected), 406 (WAF), 404 (not Shopify) and HTML responses fail fast, no retry.
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
python -m tests.mock_store --port 8004 --products 55  --seed 4 --require-browser &         # 406 to bot UA or explicit JSON Accept
python -m tests.mock_store --port 8007 --products 90  --seed 7 --html-unless-json-accept &  # HTML for .json unless Accept asks JSON
python -m tests.mock_store --port 8005 --products 1   --seed 5 --redirect-to http://127.0.0.1:8006 &   # apex -> "www"
python -m tests.mock_store --port 8006 --products 130 --seed 5 &
python tracker.py run --watchlist tests/watchlist.mock.csv --date 2026-09-05
# restart the mocks with --mutate for a "day 2" with sold-outs, price cuts and 2 new products
python tracker.py run --watchlist tests/watchlist.mock.csv
python tracker.py status
python tracker.py report
```

## Layout

```
tracker.py              CLI entry point
run_daily.bat           Windows daily runner (logs to logs\run_YYYY-MM-DD.log)
register_task.ps1       registers run_daily.bat in Task Scheduler (06:00 daily)
earlyscale/cli.py       commands: init-db, add-store, remove-store, run, sync-sheets, report, product, status
earlyscale/sheets.py    Google Sheets sync client (rows from SQLite, chunking, 302 + retry handling)
sheets/Code.gs          Apps Script web app to paste into the Sheet's script editor
earlyscale/shopify.py   HTTP fetch (host resolution, retry/backoff, pagination) + pure normaliser + Meta page discovery
earlyscale/deltas.py    pure delta calculations (7d counts, sold-out/price/handle deltas) + DB loaders
earlyscale/db.py        schema + snapshot writers
earlyscale/watchlist.py watchlist.csv I/O
earlyscale/config.py    paths, .env loader, tunables
tests/                  unit tests, fixture JSON, mock store server, fake Apps Script runtime (Node)
```
