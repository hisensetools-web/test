# Shopify Early-Scaling Tracker

Finds landing URLs whose paid-social delivery is growing week over week, before traffic tools can see them.
Six data points a day, one derived table, one sheet tab that matters. Spec: [CLAUDE.md](CLAUDE.md); the day-to-day
command sheet for Windows: [COMMANDS.md](COMMANDS.md).

## PDP cloner (`pdp.py`)

Separate from the tracker: give it a competitor product page and it produces everything needed to
launch our own version of that page. One folder per product under `pdp_output/<slug>/`:

| what | where | command |
|---|---|---|
| every image on the competitor page (gallery first, then page images, size suffixes stripped so you get the originals) | `competitor_imgs/` + `manifest.json` (source URL, alt text, kind) | `grab` |
| summary of the competitor page (title, price/compare-at, variants, copy, headings in order, bullets, FAQ, trust lines, reviews) | `product_summary.md` + `product_summary.json` | `grab` |
| new images rendered by Higgsfield from your prompt, with the competitor images as references | `<product name>_shopify_PDP_imgs/` + `generation_log.json` | `generate` |
| those images uploaded to a **draft** Shopify product | `shopify_upload.json` | `upload` (`shopify-check` first) |
| PDF build instructions for Fudge, merged from our reference PDP template + the summary | `<slug>_fudge_guide.pdf` (+ `.md`) | `guide` |

```bash
pip install -r requirements.txt            # adds beautifulsoup4, reportlab, anthropic, higgsfield-client
python pdp.py grab https://competitor.com/products/glow-neck-massager
python pdp.py generate glow-neck-massager --prompt "Studio shot on white, soft shadow, same product" --prompt "Lifestyle shot, woman on sofa using it"
python pdp.py upload glow-neck-massager                       # creates a DRAFT product with the competitor title
python pdp.py guide glow-neck-massager --template templates/pdp_template.md
python pdp.py run https://competitor.com/products/x --prompt "..." --template templates/pdp_template.md   # all four
python pdp.py list                                            # what has been grabbed / generated / uploaded
```

Keys in `.env` (see `.env.example`): `ANTHROPIC_API_KEY` (summary brief + guide text; without it you get the
raw-facts summary and a mechanical guide), Higgsfield credentials (below), Shopify app credentials (below),
`PDP_TEMPLATE` for the default template.

**Shopify app (for `upload`).** Since January 2026 custom apps are created in the Shopify Dev Dashboard and hand
you a Client ID + Client secret instead of a token; `pdp.py` mints the Admin API token itself (client credentials
grant, valid 24 h, cached in `data/shopify_token.json`). One-time setup, about five minutes:

1. Shopify admin > **Settings > Apps and sales channels > Develop apps > Build apps in Dev Dashboard** (or go to
   dev.shopify.com/dashboard and sign in with the same account). If it asks you to create an organization, do so;
   the store and the app must live in the same organization.
2. **Create app** > name it `pdp-uploader` (anything) > start from scratch / no template.
   The script has no web interface, so the URL fields are formalities: **App URL** =
   `https://shopify.dev/apps/default-app-home` (Shopify's own placeholder for apps without a UI), **Redirect URLs** =
   the same value if the form insists on one, **Embedded in Shopify admin** = off. Webhooks API version = the latest.
3. In the app: **Access** (or **Configuration > Access scopes**) > add `write_products` and `write_files` > save.
4. **Release** a version (top right; name optional).
5. **Home > Install app** > pick your store > **Install**.
6. **Settings** (left panel) > copy **Client ID** and **Client secret** into `.env` as `SHOPIFY_CLIENT_ID` /
   `SHOPIFY_CLIENT_SECRET`, plus `SHOPIFY_STORE=your-store.myshopify.com` (the myshopify domain, not the custom domain).
7. `python pdp.py shopify-check` prints the store name and the scopes the token carries, and says exactly which
   scope is missing if any (add it under Access, release again, reinstall).

An older admin-created app whose `shpat_...` token you still have keeps working: put it in `SHOPIFY_ADMIN_TOKEN`
and leave the client id/secret empty.

**Higgsfield: two backends.** `generate --backend cli` (or `HIGGSFIELD_BACKEND=cli`) drives the official
`higgsfield` CLI on your normal account: install it once with
`curl -fsSL https://raw.githubusercontent.com/higgsfield-ai/cli/main/install.sh | sh` and run `higgsfield auth login`.
This is the recommended route for product pages because `generate --photoshoot product_shot` (or `lifestyle_scene`,
`hero_banner`, `closeup_product_with_person`, `ad_creative_pack`, ...) uses Higgsfield's `product-photoshoot` command,
whose backend prompt enhancer is built for exactly this; without `--photoshoot` it runs `generate create <model>`
with every reference passed as `--image` (default model `nano_banana_2`, change with `HIGGSFIELD_CLI_MODEL` or `--model`).
`generate --backend api` uses the developer API on platform.higgsfield.ai with `HF_KEY=key:secret` from
cloud.higgsfield.ai; `HIGGSFIELD_MODEL` / `HIGGSFIELD_IMAGE_ARG` pick the model and the request field that carries the
reference-image URLs (default `bytedance/seedream/v4/edit` / `image_urls`). `python pdp.py hf-check [--backend cli|api]`
verifies the credentials without spending credits, and `generate --dry-run` prints the exact request or command.
Shopify pages are read through `/products/<handle>.json`; other platforms fall back
to HTML parsing and, when the page is JavaScript-rendered, a headless Chromium pass (`grab --browser` forces it).
Useful flags: `generate --ref path.jpg` (choose references by hand), `--num`, `--prompt-file` (blank-line separated
prompts), `upload --handle x` / `--product-id N` (attach to an existing product), `upload --with-description`,
`--dry-run` on `generate` and `upload`. A starter template is in `templates/pdp_template.example.md`.
Tests: `python -m unittest tests.test_pdpkit tests.test_pdpkit_shopify tests.test_pdpkit_higgsfield_cli`.

## What is collected

Once per day per watchlist store, and nothing else:

| data point | source | stored in |
|---|---|---|
| per ad: `ad_archive_id`, `page_name`, `page_id`, landing URL (raw and normalised to host + path without query string), `first_seen` (the Ad Library's start date), `still_active` (in today's active search), `low_impressions` (the "Low impression count" badge on the card, true/false) | Meta Ad Library, public page, headless Chromium | `ads`, `ads_daily` |
| per store: `shop_id` and myshopify handle | storefront HTML, once | `stores` |
| per product: `id`, `handle`, `title`, `created_at`, `published_at` | `products.json` (other platforms: their adapter) | `products_daily` |

There is no product resolution: the landing URL is the key. A `/pages/prostate` advertorial is a row of its own,
never attributed to a product by guessing which product the page links to.

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate     # Windows: py -3 -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env          # SHEETS_WEBHOOK_URL for the sheet; everything else optional
python tracker.py init-db     # creates data/tracker.db
```

`watchlist.csv` ships with three test stores. Add yours with `python tracker.py add-store somestore.com` (finds the
Facebook page link in the storefront footer; `find-page` searches the Ad Library when there is none) or drop a list
into `radar/imports/`.

**Updating from the previous version:** the first command after `git pull` converts `data/tracker.db` to the new
layout (ads and their daily rows are kept, the tables of the deleted signals are dropped, the file is compacted; a
few minutes for a 1.4 GB file, once). Then paste `sheets/Code.gs` again and deploy a new version.

## Daily run

```bash
python tracker.py run                    # catalogues + shop ids for every store, then a Sheets sync (~10 min for 150 stores)
python tracker.py ads --max-minutes 480  # Ad Library pass, least recently scraped store first, within the budget (~3 min per store)
python tracker.py radar                  # store discovery (Sundays: weekly sweeps, then triage)
python tracker.py sync-sheets            # Winners / Stores / Candidates -> the Google Sheet, with a row-count check
```

`run --ads` (or `META_ADS=1`) chains the Meta pass after the catalogues. Every pass writes as it goes; Ctrl+C keeps
what is done and the next run continues from the database (`ads` picks the stores it has not scraped for longest).

## The Winners tab

One row per landing URL with at least 3 delivering ads in the store's latest scrape, from `url_daily`:

| column | meaning |
|---|---|
| `store`, `landing_url` | the watchlist store and the normalised URL (host + path, lower-case, no query string) |
| `delivering` | active ads on the URL whose card shows no "Low impression count" badge. An ad whose card never rendered (badge unknown) is not delivering: a smaller number, never a wrong one |
| `delivering_7d_ago` | the same from the scrape 6-8 days earlier (blank without one) |
| `delivering_wow` | `delivering / delivering_7d_ago`; `new` when it was 0 a week ago; blank without history. The tab sorts on it (`new` first, blank last), then on `delivering` |
| `proven_days` | age of the oldest still-delivering ad on the URL |
| `pages`, `pages_new_7d` | distinct Facebook pages with a delivering ad on the URL; those whose first delivering ad here is under 7 days old |
| `top_page` | the page with the most delivering ads |
| `family_age_days` | only for `/products/<handle>` URLs: days since the oldest `created_at` in the product family. Blank for landers |
| `store_age_days` | from `shop_id` through `calibration/shop_ids.csv` (see below); blank without a calibration |
| `ads_as_of` | the scrape date the row comes from |

**Product families.** Merchants duplicate a product for every channel (`glow-serum`, `glow-serum-copy`,
`glow-serum-1`, `glow-serum-otp`, `glow-serum-es`). Handles on a store are grouped by product id where one product
carried several handles over time, else by the handle or the normalised title with those suffixes stripped; the
family's age is the oldest `created_at` in it, so a fresh duplicate of an old product does not read as a new product.

**Store age.** Shopify shop ids are sequential. `calibration/shop_ids.csv` (columns `shop_id,created_date`; not in
the repo) holds stores whose creation date is known; every store's date is interpolated from it and from the earliest
product `created_at` of the stores in the database (a store exists before its first product). Without the file the
column stays blank.

Terminal views of the same data:

```bash
python tracker.py report                                  # the Winners tab
python tracker.py url https://elivorahealth.com/pages/prostate   # one URL: delivering per day + the ads behind it, badge per ad
python tracker.py status                                  # per store: products, ads, delivering, winners, last status
python tracker.py rebuild                                 # recompute url_daily from the stored ads (no scraping)
```

## Google Sheets sync

Pushes the three tabs into a Google Sheet through a Google Apps Script **web app**. No Google Cloud project, no
service account, no API keys: you paste one file into the Apps Script editor and copy one URL back. The code you
paste is [`sheets/Code.gs`](sheets/Code.gs).

### One-time setup (about 5 minutes)

1. **Create the spreadsheet.** Go to <https://sheets.new>, name it e.g. *Shopify Tracker*.
2. **Open the script editor from inside that sheet.** Menu *Extensions* > *Apps Script*.
3. **Replace its contents.** Select everything in the editor (Ctrl+A), delete it, then paste the entire contents of
   `sheets/Code.gs` from this repo. Ctrl+S to save.
4. **Deploy it as a web app.** **Deploy** > **New deployment** > gear > **Web app**: *Execute as: Me*,
   *Who has access: Anyone*. Deploy, authorise (Advanced > Go to ... (unsafe) > Allow: it is your own script).
5. **Copy the Web app URL** (ends in `/exec`) into `.env` as `SHEETS_WEBHOOK_URL=...`. Anyone who has the URL can
   write to your sheet, so treat it like a password.
6. **Once, for the diagnostics doc:** in the editor's function dropdown pick `authorizeDiag`, click Run, accept the
   prompt, then Deploy > Manage deployments > pencil > New version.

### Test it

```bash
python tracker.py sync-sheets --dry-run     # shows what would be sent, sends nothing
python tracker.py sync-sheets               # sends it, then compares the sheet's row counts with the database
```

| tab | rows | behaviour |
|---|---|---|
| **Winners** | one per landing URL with >= 3 delivering ads | rewritten every sync; columns and sort as above |
| **Stores** | one per store in watchlist.csv | store, shop_id, store_age_days, products, ads_as_of, last error |
| **Candidates** | one per Radar domain (candidates first) | rewritten after the `promote` column has been read back |

Every sync first asks the deployed `Code.gs` which headers it writes and refuses to send rows when they differ
from this code's columns (a stale deployment would put every value under the wrong header); it ends by reading the
row counts back and exits 3 on a mismatch (a mismatched tab is pushed again once). One sync at a time per database
(`data/sync.lock`). A retried chunk is never sent twice by accident: when Google loses the reply to a POST, the sync
first asks the sheet how many rows the tab holds.

### After updating Code.gs

Paste the new file over the old one in the Apps Script editor, save, then Deploy > **Manage deployments** > pencil >
Version: **New version** > Deploy. The URL stays the same.

### If something goes wrong

- **"Apps Script returned a web page instead of JSON"**: the deployment is not set to *Anyone*, or the URL is not the `/exec` one.
- **You edited Code.gs and nothing changed**: a web app serves the *deployed version*; deploy a new version.
- **"the deployed Code.gs writes different columns"**: paste and deploy the current `Code.gs`, then sync again.
- Advanced: `SHEETS_CHUNK_BYTES` (default 40000) sets the payload size per POST.

## Meta Ad Library pass

`python tracker.py ads` opens the public Ad Library in headless Chromium, one browser, one store at a time, with a
random 3-8 s pause between scrolls and stores. It searches the store's Meta page name (`watchlist.csv`; the domain
when none is set) for active ads, scrolls `META_MAX_SCROLLS` (20, about 350 newest ads) times and captures the
GraphQL responses the page loads: ad id, page, start date, landing link. The "Low impression count" badge is not in
those payloads; it is read off every rendered result card (before each scroll and at the end), so an ad whose card
was never on screen keeps an unknown badge and is not counted as delivering.

Per store and day the pass writes `ads_daily` (still_active, low_impressions), marks ads of earlier days that did
not come back as `still_active = 0`, and derives that day's `url_daily` rows. Stores are taken least recently
scraped first within `--max-minutes` (the night task: `META_NIGHT_MINUTES` 600, hard stop at `META_STOP_AT` 08:30),
so a watchlist bigger than one night rotates. A login wall stops the pass (logged as `blocked`); a store whose page
crashes Chromium is logged and the next store gets a fresh browser.

The pass never logs in and never touches a store's cart or checkout. Two watchlist entries that share one Facebook
page (luma.viture.com and viture.com both advertising as "VITURE") each get the same ads recorded under their own
store; keep one domain per advertiser to avoid double rows on the Winners tab.

## Every storefront platform, not only Shopify

`run` detects what a domain runs on and reads its catalogue with the matching adapter: Shopify (`/products.json`),
headless Shopify (the `*.myshopify.com` origin behind a custom front), WooCommerce (Store API), Squarespace,
Magento (GraphQL), BigCommerce / Wix / anything with a product sitemap and JSON-LD (`CATALOGUE_MAX_PAGES` product
pages per run, cached in `product_pages`). Every adapter yields the same product record (id, handle, title, dates,
page path). Dates a platform does not publish are filled with the day this tracker first saw the product; products
already present on the store's first snapshot get no date (unknown age) rather than "today".

## Radar: finding stores before they are on the watchlist

`python tracker.py radar` discovers new Shopify stores from the Ad Library and decides, per store,
whether it goes straight onto the watchlist or waits on a **Candidates** tab. The night task runs it
after the Meta pass; on Sundays (`RADAR_SWEEP_WEEKDAY=6`) it also runs the weekly sweeps.

### Sources

1. **Hook sweeps** (`radar/hooks.txt`, one phrase per line, `#` comments). Each phrase is searched
   in the Ad Library (US, active ads, unordered keyword match, up to `RADAR_MAX_ADS_PER_QUERY=500`
   ads per phrase). Every ad is stored in `radar_ads` with its page name, page id, landing domain,
   body length and start date; the landing domain is what gets triaged.
2. **Copycat search**: for every watchlist store's hero products (the 3 newest by `created_at` plus
   anything published in the last 30 days) the distinctive words of the title, minus the store's
   own brand words (the domain label and any word that opens several titles), become an Ad Library query, so stores selling the same product show up. The
   same words go to a web search (DuckDuckGo HTML, `RADAR_WEB_SEARCH=1`, `RADAR_MAX_WEB_QUERIES`)
   and every result domain is kept for triage. Copycat queries rotate: `RADAR_MAX_COPYCAT_QUERIES`
   least-recently-searched ones per sweep.
3. **Manual import**: `python tracker.py radar-add <domain or url> ...`, or drop a `.txt`/`.csv`
   into `radar/imports/` (one domain per line, or any CSV with a domain / website / url / landing / link
   column, e.g. an ad-spy export). These skip triage and go straight onto the watchlist with
   `source=manual`; processed files move to `radar/imports/done/`.

### Triage (every new landing domain, then every Candidates row again daily)

A sweep of 80 phrases returns a few thousand distinct landing domains, so triage is split into a cheap
stage that runs on all of them and an expensive stage that runs only where it can change the verdict.

**Stage 1, HTTP only, `RADAR_CHECK_WORKERS` (6) domains at a time:**

1. Is it a storefront on any platform (`platforms.is_store`: Shopify, headless Shopify, WooCommerce,
   Squarespace, Magento, BigCommerce, Wix, or any site with a product sitemap and JSON-LD)? If not, the
   lander's outbound links (buy buttons, checkout, "shop now") are followed one hop; if one of them is a
   store, that store is the candidate (`store_domain`) and the lander is remembered in `lander_domain`.
   Still nothing: it is a **funnel**. With `RADAR_FUNNEL_MIN_ADS` (3) or more sweep ads pointing at it,
   it is parked as `type=funnel` and its ads, pages and landing URLs are tracked; with fewer it is
   `discarded` (news sites, app stores, one-off landers) and comes back automatically the day a later
   sweep brings its count to the threshold.
2. Store age: `store_age_days` from the earliest product `created_at` (or the shop-id calibration
   when one exists; it is optional). `store_first_created` and `store_created_est` are both shown.
3. From the ads radar has already seen: `active_ads` (a lower bound until the domain is searched),
   `ads_in_sweeps`, `pages`, `top page`, an example ad text, and `hot new product` = a product
   published in the last 30 days with 3 or more ads pointing at it.

**Stage 2, one Ad Library search per domain (minutes each), up to `RADAR_MAX_TRIAGE` (40) per run,
most sweep ads first:** the store's top pages (up to 3, by `page_id`) are searched for all their
active ads, or the domain as a keyword when no page is known. Only domains where the answer can change
the verdict get one: Shopify stores that are young (`<= RADAR_MAX_AGE_DAYS`) or have a hot new product
and are still under `RADAR_MIN_ACTIVE_ADS`, and funnels over the ad threshold. An old store without a
hot product is parked without a search (nothing it could return would promote it) and is searched
the week its catalogue refresh shows a new product. `searched_at` on the tab says when a domain was
last searched; a domain is not searched again within `RADAR_RESEARCH_DAYS` (7).

**Promote** when `(store_age_days <= RADAR_MAX_AGE_DAYS (180) OR hot new product) AND active_ads >= RADAR_MIN_ACTIVE_ADS (10)`.
Promotion appends the store to `watchlist.csv` with the note `radar: <source> <date>` (plus
`via <lander>` when it was reached through a funnel page) and it gets its first Shopify snapshot on
the next morning run.
Everything else is a **candidate**: every night its facts are recomputed (age moves, new sweep ads,
`promote` marks) and it is promoted the day it crosses; Shopify candidates get their catalogue re-read
every `RADAR_REFRESH_DAYS` (7) so a new product makes them eligible. To force one, type `Y` in the
`promote` column of the Candidates tab; the next `sync-sheets` reads the marks back and the next
radar run promotes those domains without waiting for a search.

There is no copy-length filter anywhere.

### Candidates tab

`domain, type (shopify / funnel), status (candidate / promoted), first_seen, store_age_days,
store_created_est, store_first_created, products, active_ads, ads_in_sweeps, searched_at, pages, top page,
example ad text (200 chars), hot new product, source (hook:<phrase> / copycat:<store>/<handle> /
web:<store>/<handle> / manual), lander_domain, last_checked, promote`. Candidates first (most active
ads first), then promoted rows. Discarded domains are kept in the database (`radar_domains`) but not shown.

### Budget, resume and schedule

One radar run is capped at `RADAR_MAX_MINUTES` (240); the sweeps stop `RADAR_TRIAGE_MINUTES` (60)
before that so classification and searches always get their turn. A phrase searched successfully in
the last `RADAR_RESWEEP_DAYS` (6) days is skipped, so a sweep that crashed, was interrupted or ran out
of budget continues where it stopped when you run it again (and finishes next Sunday otherwise).
Stage 1 checks every new domain; stage 2 searches are capped per run and the rest wait for the next
night. Every search waits the same random 3-8 s as the Meta scraper and uses one browser. A first
sweep of 80 phrases takes about 3.5 h at 500 ads per phrase; if it keeps running short, halve the
symptom group in `hooks.txt` first or raise the budget.

Radar shares `data/tracker.db` with the other commands. The connection waits up to 60 s for another
tracker command to finish writing and retries a write that still finds the database locked, so
running `run` or `sync-sheets` while a sweep is going is safe; a phrase whose ads could not be stored
is logged and searched again next time.

### Hook yield (keeping `hooks.txt` honest)

`radar-report` (and the end of every sweep) prints one row per phrase: sweeps run, ads seen, distinct
landing domains, how many of those were promoted, how many are parked Shopify candidates, how many
funnels, how many discarded, and a verdict. A phrase gets credit for every ad it returned, even when
another phrase returned the same ad first. `delete` = zero promotable stores across two or more sweeps; `add siblings` = the top
producers. Edit `radar/hooks.txt` accordingly; the next sweep picks up the new list.

## Diagnostics without pasting: the "EarlyScale Diag" doc

`python tracker.py diag` collects a read-only report and writes it to a Google Doc named **EarlyScale Diag** in the
Drive of the account that owns the sheet (through the same Apps Script web app, `mode=diag`; created on first use,
replaced on every push). Both scheduled tasks push it when they finish, so the doc always shows the latest state and
anyone with access to the Drive can read it instead of asking for log pastes. Contents: code version (commit,
branch, local changes), config, the two scheduled tasks' last run / result / next run, database counts (latest
snapshot dates, active ads, badge coverage, URL rows), one line per store (platform, shop id, last catalogue
status, latest ads snapshot with active / delivering / badge-unknown / winner counts, last error), the top 40
Winners rows, and for the last 4 log files every ERROR / WARNING / Traceback line plus the last 30 lines. Size-capped
by `OPS_DIAG_CHARS` (250k). `--print` shows it, `--no-push` only writes `logs/diag.txt`.

## Scheduling (Windows)

Two tasks, so the morning numbers are ready quickly and the slow Meta pass runs overnight:

| task | when | what | takes |
|---|---|---|---|
| ShopifyTracker Daily | 09:00 | `run_daily.bat`: catalogues + shop ids, Sheets sync, diag | about 10 min for 150 stores |
| ShopifyTracker Meta | 22:00 | `run_daily.bat meta`: Ad Library pass until `META_NIGHT_MINUTES` (600) or 08:30, then `radar`, then a Sheets sync, diag | about 3 min per store |

Register both from PowerShell in the project folder (re-run to change the times):

```powershell
.\register_task.ps1                      # or: .\register_task.ps1 -Time 07:30 -MetaTime 23:00
```

The tasks run through `run_hidden.vbs` (no console window, so closing a window cannot kill a run) and are set to
run a missed start as soon as the machine is awake, to wake it from sleep for the start, and to run on battery.
`run_daily.bat` picks the first Python 3.11+ it finds (`.venv`, `py -3`, `python`, `python3`), appends stdout+stderr
to `logs\run_YYYY-MM-DD.log` / `logs\meta_YYYY-MM-DD.log`, and skips a night start that Task Scheduler catches up
between 07:00 and 20:00. Verify / test / remove:

```
schtasks /Query /TN "ShopifyTracker Daily" /V /FO LIST
schtasks /Run   /TN "ShopifyTracker Daily"
schtasks /Delete /TN "ShopifyTracker Daily" /F
```

Without `/RU` and `/RP` the task only fires while you are logged on. **The laptop must not sleep during the night
pass**: `KeepAwake` asks Windows not to sleep while a pass runs, but a closed lid or a battery power plan overrides
it (Settings > Power & battery > sleep when plugged in: Never; lid: Do nothing). Time lost to sleep is given back to
the budget and the 08:30 stop still holds.

### Running it on an always-on Linux box

The code is the same; only the scheduler differs:

```bash
sudo apt install -y git python3.11 python3.11-venv
git clone <your repo url> tracker && cd tracker
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
# copy .env, watchlist.csv, calibration/ and data/tracker.db from the laptop to keep the history
mkdir -p logs && crontab -e
```

```
0 9  * * * cd /home/you/tracker && .venv/bin/python tracker.py run --no-ads            >> logs/run_$(date +\%F).log  2>&1
0 22 * * * cd /home/you/tracker && .venv/bin/python tracker.py ads --max-minutes 600   >> logs/meta_$(date +\%F).log 2>&1
30 8 * * * cd /home/you/tracker && .venv/bin/python tracker.py radar                   >> logs/meta_$(date +\%F).log 2>&1 && .venv/bin/python tracker.py sync-sheets >> logs/meta_$(date +\%F).log 2>&1
```

From a datacenter IP Facebook shows login walls more often than from a home connection, and only one machine should
run the passes (move `data/tracker.db` once and delete the Windows tasks).

## What gets stored (`data/tracker.db`)

| table | one row per | notes |
|---|---|---|
| `stores` | watchlist store | Meta page, platform, `shop_id`, myshopify handle, `store_created_est` |
| `products_daily` | product per day | id, handle, title, created_at, published_at, updated_at, url_path |
| `ads` | ad per store | page, raw + normalised landing URL, first_seen (Ad Library start date), first/last scraped |
| `ads_daily` | ad per scrape day | still_active, low_impressions (1 / 0 / NULL unknown), position |
| `url_daily` | landing path per store per scrape day | delivering, delivering_7d_ago, proven_days, pages, pages_new_7d, top_page, family |
| `meta_page_runs`, `runs`, `store_runs` | pass / store run | status, errors, durations |
| `radar_domains`, `radar_ads`, `radar_ad_hits`, `radar_runs` | Radar | see the Radar section |

History is never overwritten; re-running a date replaces that date's rows for the store. `python tracker.py rebuild`
recomputes `url_daily` from `ads` / `ads_daily` at any time.

## Behaviour on failure

- Requests carry a Chrome User-Agent and `Accept: application/json`; a store that answers 406 to that gets one retry
  with `Accept: */*`. HTML or non-JSON bodies fail fast with a descriptive error in `store_runs.error`.
- Before paginating, the host is resolved once (`GET /products.json?limit=1`, redirects followed); if the apex host is
  unreachable or serves a marketing site, `www.` and then `shop.` are tried. Keep the apex domain in the watchlist.
- Every request has a connect/read timeout and up to 3 retries with exponential backoff on connection errors,
  timeouts, HTTP 429/430 and 5xx. 401/403/406/404 fail fast. A failing store is logged and the run continues.
- Pagination stops on an empty, short or repeated page, or after 60 pages. `REQUEST_DELAY_S` (1 s) sits between
  requests to the same store.
- **The Sheets web app is unreachable** (DNS failure, no network): retried twice, then `sheets sync failed: cannot
  reach the Apps Script web app`, exit 3. The data is in the database; the next sync pushes it.
- **`database is locked`**: another tracker command holds `data/tracker.db` for writing (a Meta pass, the scheduled
  task, a python that never exited, a SQLite viewer, OneDrive syncing the folder). Opening the database does no write
  when the schema is current, so read-only commands work while a pass runs; when a schema update is needed and the
  file is busy, connect waits and retries. `python tracker.py db-check` tries a 2-second write lock and lists the
  processes that could hold it.
- Scheduled runs go through `run_hidden.vbs`; a Chromium crash mid-pass is followed by a fresh browser for the next
  store; a missed 22:00 start caught up in daytime is skipped so the night pass never runs on top of the morning task.

## Tests and offline end-to-end

```bash
python -m unittest discover -s tests -v
bash tests/replay.sh                        # Linux / macOS / WSL, needs node: two scheduled days against the fakes
```

The replay runs the morning `run --no-ads`, the night `ads`, `radar` and `sync-sheets` for 2026-09-03 and then
2026-09-10 (mutated catalogues, ads a week older) against mock Shopify stores (`tests/mock_store.py`), a fake Ad
Library page with the badge on some cards (`tests/fake_ad_library.py`) and the real `Code.gs` inside
`tests/fake_gas.js`, then prints the sheet-vs-database table and the day-2 Winners. Every step must exit 0 and no log
may contain a traceback. `tests/test_winners.py` covers the derived numbers with fixture data;
`tests/test_db_snapshot.py` covers the migration from the previous database layout.

## Layout

```
tracker.py              CLI entry point
run_daily.bat           Windows daily runner (day / meta modes; logs to logs\)
run_hidden.vbs          runs run_daily.bat without a console window (used by the scheduled tasks)
register_task.ps1       registers the two Task Scheduler tasks
earlyscale/cli.py       commands: run, ads, radar, sync-sheets, report, url, status, diag, rebuild, watchlist helpers
earlyscale/db.py        schema, migration from the previous layout, snapshot writers
earlyscale/winners.py   landing path normalisation, product families, url_daily, the Winners / Stores rows
earlyscale/meta_ads.py  Ad Library scraper (Playwright + GraphQL capture + card badges), recording
earlyscale/shopify.py   HTTP fetch (host resolution, retry/backoff, pagination) + normaliser + Meta page discovery
earlyscale/platforms.py catalogue adapters for the other storefront platforms
earlyscale/store_age.py shop id extraction and the store age estimate (calibration/shop_ids.csv)
earlyscale/radar.py     store discovery: hook/copycat/web sweeps, triage, Candidates tab, watchlist promotion, hook yield
earlyscale/sheets.py    Google Sheets sync client (rows, chunking, 302 + retry handling, verify, lock)
earlyscale/ops.py       the diagnostics report
earlyscale/watchlist.py watchlist.csv I/O
earlyscale/config.py    paths, .env loader, tunables
sheets/Code.gs          Apps Script web app to paste into the Sheet's script editor
radar/hooks.txt         hook phrases for the weekly sweep; radar/imports/ for manual domain lists
tests/                  unit tests, fixture JSON, mock store server, fake Apps Script runtime (node), fake Ad Library page, replay.sh
```
