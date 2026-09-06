# Shopify Early-Scaling Tracker

## Goal
Detect Shopify products in their first 2–4 weeks of paid-social scaling, before traffic tools (SimilarWeb, TrendTrack) can see them. Do this by measuring *leading* signals daily and alerting on week-over-week deltas.

## Signals to collect (once per day per store)

### A. Shopify store side (free, no auth)
- `GET https://{store}/products.json?limit=250` (paginate with `page=` until empty)
  - Store every product: id, handle, title, created_at, published_at, updated_at, variants (id, price, available), tags, product_type
- `GET https://{store}/collections/all/products.json?limit=250`
  - Record collection sort position (stores often sort by best-selling)
- Derived metrics:
  - `new_products_7d`: products with published_at in last 7 days
  - `updated_products_7d`: products with updated_at in last 7 days
  - `sold_out_variants_delta`: change in count of `available: false` vs yesterday
  - `price_changes`: variants whose price differs from yesterday

### B. Meta Ad Library side
- For each store's Meta page name (and/or domain), collect:
  - `active_ad_count`
  - list of ad IDs with `first_shown` / start date
  - for ads with EU delivery: reach and spend range
- Prefer the official Ad Library API (`https://graph.facebook.com/v*/ads_archive`) when a token is present — set `META_ACCESS_TOKEN` in `.env`. Fields: `id, ad_delivery_start_time, ad_delivery_stop_time, page_name, eu_total_reach, spend, impressions, ad_creative_link_captions, ad_snapshot_url`.
- Fallback: Playwright headless scrape of `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&q={page}&search_type=page`. Handle infinite scroll, rate-limit politely (random 3–8s waits), rotate user agents, and never run more than 1 concurrent browser.
- Derived metrics:
  - `new_ads_7d`: ads whose start date is within last 7 days
  - `ad_velocity_wow`: new_ads_7d / new_ads_prev_7d
  - `creatives_per_product_url`: group ads by landing-page product handle; count distinct ads per product
  - `eu_reach_wow`: sum of EU reach this week vs last week

### C. Cross-reference
- Join ad landing URLs to products.json handles → produces a per-product record: `first_ad_date, ad_count, eu_reach, published_at, price, sold_out_variants`
- `days_since_first_ad` and `days_since_published` are the key "how early are we" fields.

## Alert rules (start simple, tune later)
Flag a product when ANY of:
1. `published_at` within 14 days AND ≥ 5 ads point at it
2. `ad_velocity_wow` for the store ≥ 2.0 AND store had ≥ 3 new ads this week
3. `eu_reach_wow` ≥ 1.5 for a single product's ads
4. A product that had 0 ads last week has ≥ 8 this week

Output alerts to `alerts/YYYY-MM-DD.md` and optionally post to a Slack/Discord webhook (`ALERT_WEBHOOK_URL` in `.env`).

## Storage
- SQLite at `data/tracker.db`. Tables: `stores`, `products_daily`, `ads_daily`, `alerts`.
- Never overwrite history; every run appends a dated snapshot. Deltas are computed from the DB, not from memory.

## Watchlist
- `watchlist.csv` with columns: `store_domain, meta_page_name, meta_page_id (optional), notes`
- Provide a `python tracker.py add-store <domain>` command that tries to auto-discover the Meta page name from the store's footer social links.

## Scheduling
- `python tracker.py run` does one full pass.
- Document how to schedule daily via cron (Linux/Mac) or Task Scheduler (Windows). Target run time: under 30 min for 100 stores.

## Reporting
- `python tracker.py report` prints a table sorted by `ad_velocity_wow` desc, with the top flagged products, their `days_since_first_ad`, and store.
- `python tracker.py product <handle>` shows the full time series for one product.

## Engineering rules
- Python 3.11, `requests`, `playwright`, `sqlite3`, `rich` for tables. Keep dependencies minimal.
- Every network call has a timeout and retry with backoff. A failing store must not abort the run; log it and continue.
- Write tests for the delta/alert logic using fixture JSON — it's the part most likely to have silent bugs.
- Respect robots and rate limits. This is read-only public data; do not attempt logins or checkout manipulation.

## Build order
1. products.json fetcher + SQLite schema + daily snapshot for 3 test stores
2. Delta calculations and `report` command
3. Meta Ad Library API path (if token) → then Playwright fallback
4. Landing-URL → product join
5. Alert rules + webhook
6. Cron docs + README
