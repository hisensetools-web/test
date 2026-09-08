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
| 22:00 daily | Meta Ad Library, least recently scraped stores first, up to 8 h, then Radar (triage; sweeps on Sundays, up to 4 h), then Sheets sync | ad columns on Signals, concepts, lineage, delivery, page likes, Pages tab, Candidates tab, new stores on the watchlist |

The machine must be on and logged in at those times (the tracker keeps Windows awake while a pass runs; a closed lid still sleeps it). Logs: `logs\run_YYYY-MM-DD.log` and `logs\meta_YYYY-MM-DD.log`.

## 2b. Getting the data into the Google Sheet

You normally do nothing: both scheduled tasks end with a sync, and so does `python tracker.py run`.
The sync only happens if `SHEETS_WEBHOOK_URL=...` is in `.env`. To push by hand at any time:

```powershell
python tracker.py sync-sheets                # rewrite every tab from the database, then check row counts
python tracker.py sync-sheets --verify-only  # check only, send nothing
```

Every sync ends with a "sheet vs database" table; every row should say `yes`.

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
python tracker.py radar-report                # hook phrases by yield, radar totals, recent searches, Candidates
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
dir logs                                               # which days actually ran (no file = the task did not run that day)
schtasks /Query /TN "ShopifyTracker Daily" /V /FO LIST | findstr /C:"Last Run Time" /C:"Last Result" /C:"Next Run Time"
schtasks /Query /TN "ShopifyTracker Meta"  /V /FO LIST | findstr /C:"Last Run Time" /C:"Last Result" /C:"Next Run Time"
```

`Cannot find path 'logs\run_<today>.log'` means the morning task has not run yet today: either it is not
09:00 yet, or the laptop was asleep / off / on the lock screen at 09:00. The 22:00 task names its log by
the day it *started*, so last night is `meta_<yesterday>.log`. `register_task.ps1` (re-run it once after
`git pull`) tells Windows to run a missed start as soon as the machine is awake and to wake it from sleep
for the start; a closed lid or a shut-down laptop still cannot run anything. "Last Result: 0" = ran fine.

## 5. Running a pass by hand (only when you do not want to wait for the schedule)

```powershell
python tracker.py run                                  # morning pass now (~15 min)
python tracker.py ads --only x.com y.com               # Meta for specific stores (~6 min each)
python tracker.py ads --max-minutes 60                 # Meta for the watchlist, capped at an hour
python tracker.py inventory --only x.com               # stock probe for specific stores
python tracker.py radar --sweep                        # weekly discovery sweeps now (~3-4 h), then triage; a re-run continues where it stopped
python tracker.py radar                                # triage new / parked domains only (~30 min)
```

## 6. Watchlist

```powershell
python tracker.py add-store x.com                      # add a store (finds its Facebook page)
python tracker.py add-store pipitea.com                # use the apex domain even when the shop is on shop.pipitea.com (found automatically)
python tracker.py remove-store x.com y.com             # drop stores (history in the DB is kept)
```

The 17 domains that fail with 404 every run are not Shopify storefronts; removing them saves time.

Stores found by Radar are added automatically with the note `radar: <source> <date>` and show `store_badge=NEW` on Signals for 14 days. To add stores from an ad-spy export or by hand without triage:

```powershell
python tracker.py radar-add x.com https://y.com/products/z   # straight onto the watchlist (source=manual)
```

or drop a `.txt` / `.csv` into `radar\imports\` (one domain per line, or a CSV with a domain/website/url column); the night run imports it and moves the file to `radar\imports\done\`.

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
| `INVENTORY_STORES=a.com,b.com` | which stores get the stock probe (`INVENTORY=1` = all); stores whose products.json says `inventory_management=shopify` are probed anyway |
| `META_MAX_SCROLLS=40` / `META_DETAIL_MAX=30` | how deep each store's Meta scrape goes (~6 min per store at these) |
| `RADAR_MAX_MINUTES=240` | cap for one radar run (sweeps + triage); leftovers continue next time |
| `RADAR_MAX_AGE_DAYS=180` / `RADAR_MIN_ACTIVE_ADS=10` | promotion thresholds: (age <= 180 d OR a product <= 30 d old with >= 3 ads) AND >= 10 active ads |
| `RADAR_FUNNEL_MIN_ADS=3` | a non-Shopify lander needs this many sweep ads to be tracked as a funnel; fewer = discarded (1 keeps everything) |
| `RADAR_MAX_TRIAGE=40` | Ad Library searches per run for candidates that could promote; the rest wait for the next night |
| `RADAR_MAX_ADS_PER_QUERY=500` / `RADAR_COUNTRY=US` | how deep each hook / copycat search goes |
| `RADAR_SWEEP_WEEKDAY=6` | which weekday the sweeps run (6 = Sunday) |

## 9. Radar (finding new stores)

Runs on its own inside the 22:00 task. What you do:

- **Sunday night / Monday morning:** open the Candidates tab and `python tracker.py radar-report`.
  The hook table says per phrase how many domains it found and how many were promoted; delete phrases
  with a `delete` verdict from `radar\hooks.txt` after two sweeps, add siblings of the top producers.
- **Any day:** type `Y` in the `promote` column of a Candidates row to force it onto the watchlist
  (picked up by the next sync + radar run). Rows are re-checked daily and promote themselves the day
  they cross the thresholds; `type=funnel` rows are non-Shopify landers whose ads are tracked anyway.
- **Signals tab:** `store_badge=NEW` marks stores Radar added in the last 14 days.
- **Reading a Candidates row:** `active_ads` is every ad radar has seen landing there; while `searched_at`
  is empty that is only what the sweeps happened to catch. Young stores and stores with a hot new product
  get an Ad Library search of their pages within the next nights (40 per night), then `active_ads` is real.
