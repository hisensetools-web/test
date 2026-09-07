# Command sheet

All commands run in PowerShell from `C:\Users\top2\Desktop\test`. Most days you run nothing:
two scheduled tasks do the work and the Google Sheet updates itself.

## 1. Set up once (do these now, in this order)

```powershell
git pull                      # get the latest code (do this whenever I say "pushed")
.\register_task.ps1           # registers the 09:00 morning task and the 22:00 Meta task
```

Then:

1. Open `.env`. Remove the `META_STORES=...` line (or put `#` in front) so the night task covers every store.
2. Paste `sheets\Code.gs` into your Apps Script project and deploy a new version. Needed whenever a tab gains columns; the sync tells you if the deployed script is stale.
3. Optional, for engagement on ads: in Chrome open `chrome://extensions`, turn on Developer mode, Load unpacked, choose `tools\fb_observer`.

## 2. Runs by itself (nothing to do)

| when | task | what you get |
|---|---|---|
| 09:00 daily | Shopify snapshot, stock probe, Sheets sync | Signals, Families, Categories, Stores, Products tabs refreshed; ~15 min |
| 22:00 daily | Meta Ad Library, least recently scraped stores first, up to 8 h, then Sheets sync | ad columns on Signals, concepts, lineage, delivery, page likes |

The machine must be on and logged in at those times. Logs: `logs\run_YYYY-MM-DD.log` and `logs\meta_YYYY-MM-DD.log`.

## 3. Looking at the data

The Google Sheet is the main view. In the terminal:

```powershell
python tracker.py status                      # what is in the database
python tracker.py report                      # stores by how much changed, product-level changes
python tracker.py product <handle>            # one product's history
python tracker.py ads-report                  # per store: ads, where they land, products by ads, concepts, alerts
python tracker.py ads-report --store x.com    # one store
python tracker.py ads-detail-report --days 7  # delivery (end_date) per day, page likes, coverage
python tracker.py inventory-report --raw      # stock readings per hero variant, fallback rung per store
python tracker.py fb-report                   # captured Sponsored posts, matches, reactions over time
```

Alerts also land in `alerts\YYYY-MM-DD.md` and the Alerts tab.

## 4. When something looks wrong

```powershell
Get-Content logs\run_2026-09-08.log -Tail 40           # did the morning run happen, what failed
Get-Content logs\meta_2026-09-08.log -Tail 40          # same for the night run
python tracker.py sync-sheets --verify-only            # does the sheet hold what the DB holds (per tab, per store)
python tracker.py sync-sheets                          # push everything to the sheet now
python tracker.py inventory-probe x.com                # one live cart probe with status, headers, body
python tracker.py ads-detail --ads <ad_id>             # one single-ad page, prints what was parsed
schtasks /Query /TN "ShopifyTracker Daily" /V /FO LIST # is the task registered, when did it last run
```

## 5. Running a pass by hand (only when you do not want to wait for the schedule)

```powershell
python tracker.py run                                  # morning pass now (~15 min)
python tracker.py ads --only x.com y.com               # Meta for specific stores (~6 min each)
python tracker.py ads --max-minutes 60                 # Meta for the watchlist, capped at an hour
python tracker.py inventory --only x.com               # stock probe for specific stores
```

## 6. Watchlist

```powershell
python tracker.py add-store x.com                      # add a store (finds its Facebook page)
python tracker.py remove-store x.com y.com             # drop stores (history in the DB is kept)
```

The 17 domains that fail with 404 every run are not Shopify storefronts; removing them saves time.

## 7. Engagement on ads (optional, only if you installed the extension)

```powershell
python tracker.py fb-bait                              # opens the stores' hero products in your browser; add to cart by hand
python tracker.py fb-listen                            # leave running while you browse Facebook; Sponsored posts are captured
python tracker.py fb-capture <permalink> --page "Brand" --text "the ad copy"   # capture one post by hand
```

Captured posts are re-counted by the morning run automatically.

## 8. Settings (`.env`)

| line | effect |
|---|---|
| `SHEETS_WEBHOOK_URL=...` | your Apps Script /exec URL; without it there is no sync |
| `META_ADS=1` | `run` includes the Meta pass when run by hand (the night task does it anyway) |
| `META_STORES=a.com,b.com` | limit the Meta pass to these stores (default: all, rotating) |
| `INVENTORY_STORES=a.com,b.com` | which stores get the stock probe (`INVENTORY=1` = all) |
| `META_MAX_SCROLLS=40` / `META_DETAIL_MAX=30` | how deep each store's Meta scrape goes (~6 min per store at these) |
