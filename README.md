# Shopify Early-Scaling Tracker

Detects Shopify products in their first weeks of paid-social scaling by snapshotting
leading signals daily and alerting on week-over-week deltas. Full spec: [CLAUDE.md](CLAUDE.md).

**Status: build steps 1–2 of 6, Sheets sync, Part A, Part B** — `products.json` fetcher, SQLite schema, daily snapshot,
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
| `score` | — | `new 7d + |Δ sold-out| + price Δ + +handles + -handles`, the sort key. `upd 7d` is excluded on purpose: inventory apps touch every product daily, so it equals the catalogue size on most stores |

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
| **Signals** | one per product (latest snapshot) | overwritten every sync; products under 30 days old first, by `units_per_day_wow` desc, then everything else by age. Columns: store, product family, handle, channel tag (from handle suffix: google, tiktok, taboola, fb, otp, sub, coc, vip, retired, variant), days_since_published, published_at, price, sold_out, collection_rank (1 = top of /collections/all), collection_rank_delta_7d (positive = climbed vs the snapshot 7+ days ago), variants_of_family_published_7d, then the Meta columns filled by Part B and the six inventory columns (`signal_source`, `inventory_tracked`, `stock_level`, `units_sold_1d`, `units_per_day_7d`, `units_per_day_wow`) filled by the stock probe |
| **Families** | one per product family per store | overwritten; a family = handles sharing a base name or normalised title. Handle count, newest/oldest published_at, launches in 7/14/30 days, best rank, handle list. Sorted by 7-day launches |
| **Categories** | one per keyword category | overwritten; categories are title unigrams/bigrams shared by 2+ stores (nothing hardcoded), with store/family counts and newest publish date. Families with no shared keyword fall into `(uncategorised)` |
| **Stores** | one per store in watchlist.csv | fully overwritten every sync, sorted by change score |
| **Products** | one per product per snapshot date | appended; duplicates (same date + store + handle) are skipped, so syncing twice is safe. Kept at the end of the tab bar with the two variant-count columns hidden |
| **Alerts** | one per alert | appended; duplicates (date + store + handle + rule + detail) skipped |

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

## Meta Ad Library (Part B)

`python tracker.py ads` opens the public Ad Library in headless Chromium, one browser, one
page at a time, with random 3-8 s pauses between scrolls and a rotating desktop
User-Agent. Instead of scraping the obfuscated page markup it captures the GraphQL
responses the page itself loads while scrolling; those carry structured ad records
(archive id, page, start date, active flag, primary text, headline, landing URL,
creative type and asset, EU reach). A recursive extractor finds ad records wherever Meta
nests them, so wrapper changes don't break it. Expect to maintain this anyway.

**Setup once**

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

**Try one page first** (search is by `meta_page_name` from watchlist.csv when set,
`meta_page_id` if you have it, else by store domain as a keyword search):

```bash
python tracker.py ads --only somestore.com          # add --headed to watch the browser
python tracker.py ads-report --store somestore.com  # per-page status + raw ad rows
```

`ads-report` shows, per ad: id, page, start date, days running, active Y/N, creative
type, headline, primary text, landing URL, fingerprint (hash of creative asset + text),
EU reach. Send that table back before enabling more stores.

**What happens after each page is scraped** (also runnable on its own with
`python tracker.py ads-metrics`, no browser needed):

1. **Landing URL to product.** `/products/<handle>` on the store's domain matches
   products.json directly (channel-suffixed handles match exactly; unknown suffixes fall back
   to the longest known prefix). An advertised handle that is a live product page but absent
   from products.json (offer / subscription variants are often published this way) is fetched
   as `/products/<handle>.json` and recorded in `products_daily` with `unlisted = 1`, so it
   gets its own Signals row (channel tag ends in `unlisted`), joins its family by title, and
   the ads pointing at it are counted against it rather than folded into the listed product. `/pages/<x>` advertorials and other URLs are fetched once a
   week and their buy links, `/cart/add` variant ids and embedded product JSON are read; the
   best-supported known handle wins. Advertorials with no product link keep `page_handle`
   and a blank product. Fetch results are cached in `landing_pages`.
2. **Concepts.** Same page + same landing URL (sans query) + launch dates chaining within
   1 day = one concept. Per day: ads ever, ads active, survival = active / ever, days running.
3. **Lineage.** An ad whose Meta start date is within 7 days, whose headline+text is more
   than 70% similar (word-trigram Jaccard) to an ad 14+ days old on the same page, is linked
   to that ad.
   **Page relevance.** A domain keyword search also returns unrelated advertisers. Any page
   none of whose ads land on the store (or resolve to a product) is marked ignored in
   `meta_pages_daily`; its ads stay in the database but are left out of concepts, lineage,
   alerts and the Signals counts. `ads-report` lists ignored pages.
4. **Per-ad daily metrics.** `days_running`, and `engagement`, `engagement_delta`,
   `engagement_per_day` (7-day average) when engagement counts exist, which the Ad Library
   does not provide, so these stay empty.
5. **Alerts** (rules 5-7, appended to the `alerts` table, `alerts/YYYY-MM-DD.md`, and the
   Alerts tab on the next sync):
   - 5: `engagement_per_day` at least 2x its value a week earlier on a single ad.
   - 6: a concept with every ad still active after 14+ days while the page's number of
     active concepts fell versus a week earlier.
   - 8: EU reach slope doubled week over week on one ad (see below).
   - 7: new ads with lineage to an ad running 20+ days, one alert per (page, parent ad)
     with the count, on the day they are first seen. A store launching 40 copies of one
     proven ad yields one line.
6. **Signals tab Meta columns:** `ads_pointing_here` (active ads resolved to that handle),
   `engagement_per_day` (average, empty without engagement data), `days_running_max`,
   `concept_status` (e.g. `2/3 concepts alive, intact 27d`).

`ads-report` prints per-page status, products ranked by active ads pointing at them (with
how they were resolved), the raw advertised handles (channel-suffixed handles that are not
in products.json show "no" and which product they were resolved to), the landing pages
that were fetched and what handles were found on them (unresolved first, so you can see
why an advertorial did not map), concepts, lineage grouped by parent ad, and today's
alerts; `--raw` adds the per-ad rows.

**Reach curve and boosted-post comments (Part B, increment 3)**

- Every ad's exact EU/EEA reach (`eu_total_reach`), exact UK reach when the country
  breakdown carries a GB row, and any reach *range* (lower/upper bound) are stored per ad per
  day. `reach_delta_1d` is the day-over-day change; `reach_slope_7d` is reach per day over the
  last 7 days and `reach_slope_prev_7d` the 7 days before. Reach is cumulative in the
  library, so the slope is the closest public proxy for daily budget, for any advertiser that
  also delivers to the EU.
- Rule 8: an ad's 7-day reach slope is at least 2x its prior-7-day slope (total reach at
  least `META_REACH_MIN`, default 1000). Needs 14 days of snapshots.
- Ads whose payload exposes an underlying Page or Instagram post get `engagement_type =
  boosted` and `post_url`; the post is opened once a day in the browser (up to
  `META_MAX_POSTS` per store) to read comment / reaction / share counts, from which
  `comment_delta_1d` follows. Everything else is `dark`: blank is expected, not an error.
  `post_status` records ok / login-wall / no-counts per day.
- `python tracker.py ads-coverage` shows per store how measurable the ads are: % with exact
  EU reach, % UK exact, % range only, % boosted, % of boosted posts whose counts were read,
  % dark, plus which reach-related keys the raw payloads carried (so the parser can be
  adjusted if Meta renames them). `ads-metrics --posts` re-derives everything from the stored
  payloads and fetches posts without re-scraping.
- Signals tab gains `eu_reach_slope_7d` and `comment_delta_1d` (summed over the active ads
  pointing at each product).

**Tables:** `meta_ads` (one row per ad ever seen, first/last seen dates, texts, links,
copy fingerprint, product/page handle, concept id, lineage), `meta_ads_daily` (one row per
ad per day; an ad that stops appearing in the active search gets an `is_active = 0` row
that day, which is how disappearance is tracked), `meta_concepts_daily`, `landing_pages`,
`meta_page_runs` (status per page per run: ok / blocked / error).

**Daily run:** the Shopify pass never depends on Meta. Set `META_ADS=1` in `.env` (or pass
`--ads`) and `python tracker.py run` scrapes after the Shopify pass, before the Sheets
sync; a blocked or failing page is logged in `meta_page_runs` and the run continues.
If Meta serves a login wall the pass stops for the day rather than hammering it.

**Known limits**

- Reactions / comments / shares are not exposed by the Ad Library. The columns exist and
  stay empty unless a payload happens to carry them; the engagement-based metrics in the
  spec will need another source.
- The GraphQL payload shape is based on the library's current responses and was verified
  offline against a local stand-in (`tests/fake_ad_library.py`), not against Meta from the
  build environment. The first real run is the real test.
- The concept, lineage and alert rules need a few days of snapshots before they say much:
  rule 6 compares against a week earlier, rule 7 needs an ad older than 20 days on record.
- If Playwright cannot download its browser, point `META_CHROMIUM_PATH` in `.env` at an
  installed Chromium/Chrome binary.

## Store age from the Shopify shop ID

Shopify assigns shop IDs sequentially, so the number orders stores by creation date. Every
storefront's HTML carries it, and it never changes:

- `<meta name="shopify-digital-wallet" content="/SHOP_ID/digital_wallets/dialog">` (preferred)
- `"shopId":SHOP_ID` in the analytics / Trekkie config or the `shopify-features` JSON (fallback)
- `Shopify.shop = "handle.myshopify.com"` for the myshopify handle

The daily run fetches the homepage once per store, stores `shop_id`, `myshopify` and which
source it came from, and retries only while the ID is missing. A store with no shop ID in its
HTML is named in a warning at the end of the run and in `shop-ids` / `store-age` output so you can
check it by hand.

**Calibration.** `calibration/shop_ids.csv` (`shop_id,created_date`, other columns ignored) holds
stores whose creation date you verified, for example in the Koala Inspector extension. Five or
six rows spread over a few years is enough. Every store's `store_created_est` is interpolated
linearly between its two nearest calibration rows, or extrapolated with the end segment's slope
outside the range; `store_age_days` is the distance to the report date. Estimates are recomputed on
every run, so editing the CSV takes effect on the next run (or `python tracker.py store-age`).

```bash
python tracker.py shop-ids                # fetch ids for every watchlist store (once), show the table
python tracker.py shop-ids --refresh      # re-fetch even if stored
python tracker.py store-age               # stores newest first with method + calibration neighbours
```

Sheets: the Stores tab gains `shop_id, myshopify, store_created_est, store_age_days`, the Signals
tab gains `store_created_est, store_age_days`, and a **Store Age** tab lists stores newest first
with the method and the two calibration points each estimate came from.

## Meta layer part 2: delivery, page likes, creatives, and feed-post engagement

### A. Single-ad Ad Library pages (public, no login)

`facebook.com/ads/library/?id={ad_archive_id}` embeds the ad record in the HTML (`data-sjs`
script blocks, under `deeplink_ad_archive_result.deeplink_ad_archive`). There is no post id in
it and the tracker does not look for one. The daily `ads` pass (and `python tracker.py ads-detail`)
opens these pages headlessly for up to `META_DETAIL_MAX` (60) ads per store per day, most
valuable first: ads named by an alert, ads started in the last 14 days, one ad per page (for the
page-likes reading), then the rest; an ad already switched off is re-read weekly. The search
results payload also carries `end_date` and `page_like_count`, so every scraped ad gets a free
`list` reading each day and the single-ad page adds a `detail` reading (which wins).

| field | stored as | derived |
|---|---|---|
| `end_date` | `meta_ad_detail_daily.end_date`, `meta_ads.last_delivered` | `delivery_status` = on while it advances; **off** when it has not moved for 2+ days (or lies 2+ days back), with `switched_off_date` = the last day it delivered. Concept survival (`ads_delivering / ads_ever`, rule 6, Signals `concept_status`) uses delivery whenever a concept has it, search presence otherwise |
| `page_like_count` | `meta_page_likes_daily` (one row per page per day) | `likes_delta_1d`, `likes_slope_7d`, `likes_slope_prev_7d`; **rule 12** fires when the 7-day slope is at least twice the prior week's (and the prior week was at least `META_LIKES_MIN_SLOPE` likes/day) |
| `snapshot.images[].original_image_url`, video URLs | `meta_creatives` (sha256 of the file, videos capped at `META_CREATIVE_MAX_BYTES`) and `meta_ads.creative_hash` | lineage by creative: a later ad on the same page with the same hash gets `lineage_of` the earliest one, `lineage_via = creative`, so image-only iterations are caught alongside the text-similarity lineage |
| `page_profile_uri` id, `page_categories` | `meta_ads.page_profile_id`, `page_categories` (JSON) | shown in the reports |

```bash
python tracker.py ads-detail --only biorootlabs.com        # fetch now (default cap 60 per store)
python tracker.py ads-detail --ads 1234567890123 ...       # one ad, prints what was parsed
python tracker.py ads-detail-report --store biorootlabs.com --days 3
```

Single-ad pages are opened in a *light* browser context (images, media, fonts, stylesheets and
external scripts are aborted: the record is in the server-rendered HTML; `META_DETAIL_LIGHT=0`
turns that off), the context is recycled every `META_DETAIL_CONTEXT_PAGES` (20) pages, and a
Chromium that dies mid-pass is relaunched and the ad retried once, so one heavy page cannot
take the remaining stores down with it.

`ads-detail-report` lists, for the ads named by that store's alerts (or `--ads`), the
`end_date` read on each day (`L` = list payload), delivery status, last delivered and off-since
dates, and lineage; then `page_like_count` per page per day with the deltas and slopes; then
coverage (ads read, delivering, off, creatives hashed, lineage by creative).

### B. Engagement on Sponsored posts: captured by hand, counted logged-out

Dark ads carry reactions / comments / shares only as feed posts. The tracker does **not** drive
a logged-in Facebook account for this (automating an account, throwaway or not, inside a
logged-in session is exactly what Meta's terms forbid and litigate). Instead:

1. **You capture.** When you see a Sponsored post from a watchlist brand in your own feed, open
   it by hand (click its timestamp so the permalink is in the address bar) and either run
   `python tracker.py fb-capture <permalink> --page "Brand" --text "the ad copy" --store brand.com`
   or click the bookmarklet in `tools/fb_capture_bookmarklet.js` (it copies a JSON capture with the
   permalink, page, selected text and visible counts to the clipboard) and run
   `python tracker.py fb-capture --paste`. `--file` takes JSON lines or one URL per line.
   Posts are deduped on `post_id`.
2. **The tracker counts.** `python tracker.py fb-engagement` (and the daily `run`, once any post
   is captured) opens every known permalink in a fresh, logged-out browser context and records
   the public reaction / comment / share counts in `fb_posts_daily`, deriving `comment_delta_1d`
   and `engagement_per_day_7d`. A permalink behind a login wall is marked `gated`, a dead one
   `removed`; both are skipped afterwards.
3. **Join.** A post is matched to an Ad Library ad on the same page (id, else name) when the
   normalised primary text is at least 0.8 similar (word trigrams) or the creative hash matches.
   The ad row gets `post_id` / `post_permalink`, its `meta_ads_daily` row gets the counts, and
   `engagement_per_day` reaches the Signals tab through the existing product join (rule 5 sees it too).

**Hands-free capture: the browser observer.** `tools/fb_observer/` is a small Chrome extension
(Load unpacked at `chrome://extensions` with Developer mode on). It never scrolls, clicks or
navigates: while *you* browse Facebook normally in that browser, it notices every Sponsored post
Facebook renders, reads it off the page (page, permalink, copy, landing URL, visible counts) and
sends it to `python tracker.py fb-listen` (a listener on 127.0.0.1:8765). If the listener is not
running the captures wait in the extension and are sent the next time it is, or you export them
from the extension popup and run `fb-capture --file captures.json`. From then on the daily run
counts each permalink logged-out. `python tracker.py fb-bait` opens the watchlist stores' hero
product pages in your default browser so you can add to cart by hand, which is what makes those
brands' ads appear in your feed within a day or two.

```bash
python tracker.py fb-listen        # leave running while you browse; Ctrl+C when done
python tracker.py fb-bait          # open hero product pages to seed retargeting (add to cart yourself)
python tracker.py fb-report        # captured posts, matches, count history
```

The observer's extraction code is exercised by `tests/test_fb_observer.py`, which loads
`content.js` into real Chromium against a fake feed and checks both ways Facebook marks an ad, the
timestamp link whose href only appears on hover, the `l.facebook.com` landing redirect, the counts,
posts scrolled in after load, and that nothing is captured twice. Facebook's markup still changes
without notice, so the popup shows the last capture it made: that is the thing to paste when a
field comes back empty.

Each capture's creative is downloaded once and hashed, which groups repeats of the same creative
across sessions. It rarely matches an Ad Library `creative_hash` outright, because Facebook
re-encodes a different rendition for the feed, so the post-to-ad join is carried by text
similarity in practice.

**Daily run:** with `META_ADS=1` the scheduled `run` now does: Shopify pass -> stock probe -> Ad
Library search scrape -> single-ad pages (A) -> boosted-post counts -> captured-post counts (B)
-> Sheets sync. Nothing in `run_daily.bat` changes. Budget: the single-ad pages add roughly
`META_DETAIL_MAX` x 4 s per store per day.

## Inventory-delta sales tracking (store-side demand signal)

Traffic tools see a store weeks late and the Ad Library gives no reach for US/UK advertisers,
so the demand signal is read off the store itself: how fast its stock goes down.

**Hero variants.** For every store in `INVENTORY_STORES` the daily run selects the top 15
products of `/collections/all` (best-selling order) plus anything published in the last
30 days, and skips shipping protection / package protection, warranties, insurance, gift
cards, memberships, ebooks / digital downloads and $0 items. Every variant of a hero product
is a hero variant, capped at `INVENTORY_MAX_VARIANTS` (40) per store per day, newest
products first. A hidden bundle product in `products.json` is treated like any other
product; when a bundle's own stock is not enforced at the cart and its page lists other
products' variant ids, those components are probed too (`role = bundle_component`).

**Probe.** Once per day per hero variant: `POST /cart/add.js` with quantity 9999 from a
fresh session with browser headers. Shopify answers `422 "You can only add N ... to the
cart"` when inventory is tracked, which is the stock level; `"sold out"` is 0. A `200`
means the add went through (inventory not enforced), and the orphaned cart is cleared with
`/cart/clear.js`. Probes are spaced by a random 5-15 s pause. The tracker never opens
checkout and never creates an order (the mock store's `/checkout` returns 403 and a test
asserts it is never hit).

One session (one cart) per store: Shopify throttles an IP that keeps creating carts, so the
product page is opened once per handle and every probe of that store reuses the session.
A 429 waits `Retry-After` (else `INVENTORY_THROTTLE_WAIT`, 90 s) and retries once; a second
429 ends the store for the day and its remaining variants are retried tomorrow. The probe
pass runs right after the Shopify pass, before the Meta pass fetches landing pages.

**Fallback chain**, recorded per reading in `signal_source`:

| rung | when | what is stored |
|---|---|---|
| `cart_probe` | 422 with a count or a sold-out message | `stock_level` from the message |
| `theme_inventory` | add succeeded, but the product page JSON carries `inventory_quantity` for the variant | that number |
| `ads_only` | neither | `inventory_tracked = 0`; the variant is not probed again, it is only followed via ads |
| `blocked` | 401/403/429/430/503, redirect or captcha | no reading; retried tomorrow (`consecutive_failures` counts) |

**Derived numbers** (`inventory_daily`): `units_sold_1d` = previous available reading minus
today's, floored at 0 and compared against the previous *available* reading rather than
calendar yesterday, so a blocked day does not lose the sales. A rise is a restock: logged in
`restock_units`, excluded from sales, and the interval containing it is dropped from the
units/day maths. `units_per_day_7d` and `units_per_day_prev_7d` are units per measured day
over the last 7 and the 7 before; `units_per_day_wow` is their ratio. The Signals tab sums
these over a product's hero variants.

**Alerts** (rules 9-11, written to `alerts/YYYY-MM-DD.md` and the Alerts tab):

- 9: published < 30 days ago, `units_per_day_wow` >= 2.0 and `units_per_day_7d` >= 5
- 10: >= 50 units/day and not in the store's top 10 (`collection_position <= 10`) in the snapshot a week ago
- 11: stock hit 0 within 14 days of publish

**Commands**

```bash
python tracker.py inventory                      # probe INVENTORY_STORES now (any date: --date)
python tracker.py inventory --only a.com b.com   # probe specific stores regardless of .env
python tracker.py inventory-report [--store a.com] [--days 14] [--raw]
```

`inventory-probe <store>` does one live probe and prints the HTTP status, the response
headers that identify a bot wall (`cf-mitigated`, `x-shopify-stage`, redirects) and the body,
so a store whose readings come back `blocked` can be diagnosed from one paste. After
`INVENTORY_MAX_BLOCKED` (3) consecutive blocks the rest of that store's variants are recorded
as blocked without another request, and the store is retried the next day.

`inventory-report` shows, per store, how many hero variants landed on each rung of the
chain, then one row per hero variant with the raw `stock_level` reading for every day
(`ads` = not tracked, `blk` = blocked, `-` = no reading), and the inventory alerts.

**Rollout:** set `INVENTORY_STORES=neuro-bella.com,tryorgatics.com,metabolae.com` in `.env`
and the scheduled `python tracker.py run` probes those three after the Shopify pass, before
the Meta pass and the Sheets sync. `INVENTORY=1` (or `run --inventory`) probes every watchlist store; do that
only after the readings from the first stores look right. `--no-inventory` skips the pass.
Budget: about 40 probes x 10 s = 7 minutes per store per day.

## Scheduling (Windows)

`run_daily.bat` probes, in order, `.venv\Scripts\python.exe`, `py -3`, `python` and `python3`,
and uses the first one that actually runs and reports Python 3.11 or newer (so a machine
with only 3.14 and no `py` launcher works, and the Microsoft Store `python` stub is skipped).
It then runs `tracker.py run` and appends stdout+stderr to `logs\run_YYYY-MM-DD.log`, with
the chosen interpreter and version in the start line. If nothing qualifies it logs an error
and exits with code 9009.
Register it in Task Scheduler (09:00 daily by default; `-Time HH:MM` for another time, re-run to change it) from PowerShell in the project folder:

```powershell
.\register_task.ps1
```

which runs exactly:

```
schtasks /Create /TN "ShopifyTracker Daily" /TR "\"C:\path\to\run_daily.bat\"" /SC DAILY /ST 09:00 /F
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

Linux/macOS cron equivalent (09:00 daily):

```
0 9 * * * cd /path/to/tracker && .venv/bin/python tracker.py run >> logs/run_$(date +\%F).log 2>&1
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
| `stores` | store | domain, Meta page name/id, `shop_id`, `myshopify`, `store_created_est` (+ method) |
| `products_daily` | (day, store, product) | handle, title, type, tags (JSON), created/published/updated, variant_count, sold_out_variants, min/max price, `collection_position` (index in `/collections/all`, often best-selling order) |
| `variants_daily` | (day, store, variant) | price, compare_at_price, available |
| `runs`, `store_runs` | run / (run, store) | status, error text, products seen, pages, duration |
| `hero_variants` | (store, variant) | which variants are probed, role (new / rank / bundle_component), current rung, `inventory_tracked`, last probe date |
| `inventory_daily` | (day, variant) | `stock_level`, `signal_source`, raw probe message, `units_sold_1d`, `restock_units`, `units_per_day_7d`, `units_per_day_prev_7d`, `units_per_day_wow` |
| `meta_ad_detail_daily`, `meta_page_likes_daily`, `meta_creatives` | (day, ad) / (day, page) / (ad, creative) | single-ad page readings, page likes with slopes, creative hashes |
| `fb_posts`, `fb_posts_daily` | post / (day, post) | captured Sponsored posts, their match to an ad, daily public counts |
| `alerts` | alert | rules 1-4 (deltas), 5-8 (Meta), 9-11 (inventory), 12 (page likes) |

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
register_task.ps1       registers run_daily.bat in Task Scheduler (09:00 daily, -Time to change)
earlyscale/cli.py       commands: init-db, add-store, remove-store, run, sync-sheets, report, product, status
earlyscale/sheets.py    Google Sheets sync client (rows from SQLite, chunking, 302 + retry handling)
sheets/Code.gs          Apps Script web app to paste into the Sheet's script editor
earlyscale/shopify.py   HTTP fetch (host resolution, retry/backoff, pagination) + pure normaliser + Meta page discovery
earlyscale/deltas.py    pure delta calculations (7d counts, sold-out/price/handle deltas) + DB loaders
earlyscale/meta_ads.py  Ad Library scraper (Playwright + GraphQL capture), parser, SQLite recording
earlyscale/ad_metrics.py landing-URL join, concepts, lineage, daily ad metrics, alert rules 5-8, Signals join
earlyscale/ad_detail.py  single-ad Ad Library pages: delivery (end_date), page likes, creative fingerprints, rule 12
earlyscale/fb_posts.py   captured Sponsored posts, logged-out counts, join to ads
earlyscale/store_age.py  Shopify shop id + myshopify from the storefront HTML, calibration interpolation, Store Age tab
earlyscale/inventory.py  hero-variant selection, /cart/add.js stock probe + theme fallback, units-sold maths, alert rules 9-11
earlyscale/db.py        schema + snapshot writers
earlyscale/watchlist.py watchlist.csv I/O
earlyscale/config.py    paths, .env loader, tunables
tests/                  unit tests, fixture JSON, mock store server, fake Apps Script runtime (Node),
                        fake Ad Library page for the browser loop
```
