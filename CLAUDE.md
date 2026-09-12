# Shopify Early-Scaling Tracker

## Goal
Find landing URLs whose paid-social delivery is growing week over week, before traffic tools can see them.
Six data points, collected daily; everything on the sheet is derived from them. Nothing else is collected.

## Data points (once per day per store)

### 1. Per ad (Meta Ad Library, Playwright scrape of the public page)
- `ad_archive_id`, `page_name`, `page_id`
- `landing_url`: raw, as shown on the card, and `landing_path`: normalised (host + path, lower-case, query string removed, no trailing slash)
- `first_seen`: the Ad Library's start date
- `still_active`: present in today's active-ads search
- `low_impressions`: the "Low impression count" badge read off the rendered card, true/false. Unknown (card never rendered)
  is stored as NULL and counts as NOT delivering. The sheet never shows a `?`.
- No product resolution of any kind. The landing URL is the key.

### 2. Per store
- `shop_id` and the myshopify handle from the storefront HTML (once; they never change)
- Every product's `id`, `handle`, `title`, `created_at`, `published_at` from `products.json` (other platforms: the adapter's equivalent)

### 3. Product family
- Handles on a store are grouped by product id where shared, else by the handle / normalised title with the duplicate
  suffixes stripped (`-copy`, `-1`, `-2`, `-cc`, `-otp`, `-sub`, `-es` and similar)
- `family_created` = oldest `created_at` in the family

## Deleted (code removed, not only the sync)
Concept clustering, lineage alerts, ads_launched velocity, impression sort / rank, collection rank, variant counts,
sold-out flags, review scraping, engagement, EU reach, the inventory / cart probe, feed-post capture, the Categories,
Families, Signals, Early, Pages, Ads, Products and Alerts tabs, and every alert rule that depended on them.

## Derived (per landing URL, per day; `url_daily`)
- `delivering`: active ads on the URL without the badge
- `delivering_7d_ago` (the scrape 6-8 days earlier; blank without one) and `delivering_wow = delivering / delivering_7d_ago`
  (`new` when it was 0 a week ago)
- `proven_days`: age of the oldest still-delivering ad on the URL
- `pages`: distinct page_ids with a delivering ad on the URL; `pages_new_7d`: pages whose first delivering ad on the URL is < 7 days old
- `top_page`: page name with the most delivering ads
- product family (only when the URL is a `/products/` path; blank for `/pages/` landers, never guessed) and `family_age_days`
- `store_age_days` from `shop_id` through `calibration/shop_ids.csv` (`shop_id,created_date`; not in the repo)

## Sheet (Google Apps Script web app, `sheets/Code.gs`)
- **Winners**: one row per landing URL with `delivering >= 3`. Columns in order: store, landing_url, delivering, delivering_7d_ago,
  delivering_wow, proven_days, pages, pages_new_7d, top_page, family_age_days, store_age_days, ads_as_of.
  Default sort: delivering_wow desc (`new` first, no history last), then delivering desc.
- **Stores**: store, shop_id, store_age_days, products, ads_as_of, last error.
- Nothing else.

## Storage
- SQLite at `data/tracker.db`: `stores`, `products_daily`, `ads`, `ads_daily`, `url_daily`, `meta_page_runs`, `runs`, `store_runs`,
  `schema_migrations`. History is never overwritten; every run appends a dated snapshot.
- A database from the previous layout is migrated on first open (ads copied, the rest, the Radar tables included, dropped, then VACUUM).

## Commands
`run` (catalogues + shop id), `ads` (Ad Library pass within a time budget, least recently scraped store first),
`sync-sheets`, `report` (the Winners tab in the terminal), `url <landing url>` (its time series), `status`, `diag`,
`rebuild` (recompute url_daily from the stored ads), and the watchlist helpers (`add-store`, `remove-store`, `find-page`,
`set-page`, `prune-dead`, `restore-stores`, `db-check`). Stores are added by hand; there is no store discovery.

## Engineering rules
- Python 3.11+, `requests`, `playwright`, `sqlite3`, `rich`. Keep dependencies minimal.
- Every network call has a timeout and retry with backoff. A failing store must not abort the run; log it and continue.
- Tests for the derived numbers use fixture data (`tests/test_winners.py`); `tests/replay.sh` replays two scheduled days offline.
- Respect robots and rate limits. Read-only public data; no logins, no checkout or cart manipulation.
