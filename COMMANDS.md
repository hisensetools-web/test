# Command sheet

All commands run in PowerShell from `C:\Users\top2\Desktop\test`. Most days you run nothing:
two scheduled tasks do the work and the Google Sheet updates itself.

## 1. After this update (once)

```powershell
git pull
python tracker.py db-check        # the first command after the pull converts data/tracker.db to the new layout (a few minutes, once)
python tracker.py status          # shows the converted database: ads, delivering, winners per store
.\register_task.ps1               # only if you have not run it since the hidden-window change
```

Then paste `sheets\Code.gs` into your Apps Script project and deploy a new version (Deploy > Manage deployments >
pencil > New version). The sheet now has three tabs: **Winners**, **Stores**, **Candidates**. The old tabs (Signals,
Early, Families, Categories, Pages, Ads, Products, Alerts) are no longer written; delete them by hand when you like.

`python tracker.py sync-sheets` refuses to write until the new `Code.gs` is deployed (it would put values under the
wrong headers otherwise) and tells you so.

## 1b. A whole day by hand

Same order as the scheduled tasks, one at a time in one PowerShell window:

```powershell
python tracker.py run --no-ads                         # 1. catalogues: id, handle, title, dates of every product + shop id (~10 min)
python tracker.py ads --max-minutes 480                # 2. Meta pass: every ad of each store, badge read per card (~3 min/store)
python tracker.py radar                                # 3. new-domain pass (~30 min; Sundays it also sweeps, ~4 h)
python tracker.py sync-sheets                          # 4. push Winners / Stores / Candidates and verify the row counts
python tracker.py report                               # 5. the Winners tab in the terminal
```

Shorter versions:

```powershell
python tracker.py run --only x.com y.com               # catalogues for a few stores
python tracker.py ads --only x.com y.com               # Meta pass for a few stores
python tracker.py ads --max-minutes 120                # Meta pass, stop after 2 h (the rest continue next time)
python tracker.py radar --sweep                        # force the weekly discovery sweep now; a re-run continues where it stopped
python tracker.py radar --no-sweep                     # triage only, even on a Sunday
```

Nothing is lost if you stop a command with Ctrl+C: every pass writes as it goes, and the next run continues from what is stored.

## 2. Runs by itself (nothing to do)

| when | task | what you get |
|---|---|---|
| 09:00 daily | catalogues + shop ids, Sheets sync | Stores tab refreshed, product families and ages current; ~10 min |
| 22:00 daily | Meta Ad Library (least recently scraped stores first, until 08:30), then Radar, then Sheets sync | Winners tab (delivering per landing URL, week over week), Candidates tab, new stores on the watchlist |

The machine must be on and logged in at those times (the tracker keeps Windows awake while a pass runs; a closed
lid still sleeps it). Logs: `logs\run_YYYY-MM-DD.log` and `logs\meta_YYYY-MM-DD.log`. Both tasks end by writing
the diagnostics report to the Google Doc "EarlyScale Diag".

## 3. Reading the Winners tab

One row per landing URL (host + path, query string removed) with at least 3 delivering ads in the store's latest scrape.

| column | meaning |
|---|---|
| `delivering` | active ads on this URL whose card shows no "Low impression count" badge |
| `delivering_7d_ago` | the same number from the scrape 6-8 days earlier; blank = no scrape then |
| `delivering_wow` | delivering / delivering_7d_ago; `new` = 0 a week ago; the tab is sorted on it (`new` first, blank last), then on delivering |
| `proven_days` | age of the oldest still-delivering ad on the URL (start date from the Ad Library) |
| `pages` / `pages_new_7d` | distinct Facebook pages with a delivering ad here / those whose first delivering ad here is under 7 days old |
| `top_page` | the page with the most delivering ads |
| `family_age_days` | only for `/products/` URLs: days since the oldest `created_at` in the product family (duplicates like `-copy`, `-1`, `-otp` grouped). Blank for `/pages/` landers: the tracker does not guess what a lander sells |
| `store_age_days` | from the shop id and `calibration\shop_ids.csv`; blank without a calibration |
| `ads_as_of` | the scrape date the row comes from; rows from different nights are not the same day |

In the terminal:

```powershell
python tracker.py report                               # the Winners tab
python tracker.py report --store elivorahealth.com     # one store
python tracker.py url https://elivorahealth.com/pages/prostate   # one URL: delivering per day + the ads behind it (badge per ad)
python tracker.py status                               # per store: products, ads, delivering, winners, last status
python tracker.py radar-report                         # hook phrases by yield, radar totals, recent searches, Candidates
```

## 4. When something looks wrong

```powershell
python tracker.py diag                                 # writes the report to the Google Doc 'EarlyScale Diag' (+ logs\diag.txt); --print shows it
Get-Content logs\run_2026-09-13.log -Tail 40           # did the morning run happen, what failed
Get-Content logs\meta_2026-09-13.log -Tail 40          # same for the night run
python tracker.py sync-sheets --verify-only            # does the sheet hold what the DB holds (per tab)
python tracker.py sync-sheets                          # push everything to the sheet now
python tracker.py db-check                             # 'database is locked'? shows whether tracker.db is free and what could be holding it
python tracker.py rebuild                              # recompute the per-URL numbers from the stored ads (no scraping)
dir logs                                               # which days actually ran (no file = the task did not run that day)
schtasks /Query /TN "ShopifyTracker Daily" /V /FO LIST | findstr /C:"Last Run Time" /C:"Last Result" /C:"Next Run Time"
schtasks /Query /TN "ShopifyTracker Meta"  /V /FO LIST | findstr /C:"Last Run Time" /C:"Last Result" /C:"Next Run Time"
```

`Cannot find path 'logs\run_<today>.log'` means the morning task has not run yet today. The 22:00 task names its
log by the day it *started*, so last night is `meta_<yesterday>.log`. "Last Result: 0" = ran fine.

## 5. Watchlist

```powershell
python tracker.py add-store x.com                      # add a store (finds its Facebook page)
python tracker.py remove-store x.com y.com             # drop stores (history in the DB is kept)
python tracker.py prune-dead                           # list domains that never returned a catalogue or an ad
python tracker.py prune-dead --apply                   # ...and remove them from the watchlist
python tracker.py restore-stores --apply               # put removed stores back (all of them, or name domains after --apply)
python tracker.py find-page tryhappyharvest.com        # find the store's Facebook page (footer, then Ad Library search); shows candidates
python tracker.py find-page tryhappyharvest.com --set 1   # save candidate 1;  --query "Happy Harvest" to search another name
python tracker.py set-page getdovi.com --name "Dovi" --page-id 1234567890   # set it by hand
python tracker.py radar-add x.com https://y.com/products/z   # straight onto the watchlist (source=manual)
```

A store without a Meta page name is searched by its domain, which finds nothing for most brands: set the page for
every store whose `ads_as_of` stays empty on the Stores tab after a night pass. Keep one domain per advertiser: two
watchlist entries sharing one Facebook page (luma.viture.com and viture.com) each get the same ads.

Or drop a `.txt` / `.csv` into `radar\imports\` (one domain per line, or a CSV with a domain/website/url column); the
night run imports it and moves the file to `radar\imports\done\`.

## 6. Settings (`.env`)

| line | effect |
|---|---|
| `SHEETS_WEBHOOK_URL=...` | your Apps Script /exec URL; without it there is no sync |
| `META_ADS=1` | `run` includes the Meta pass when run by hand (the night task does it anyway) |
| `META_STORES=a.com,b.com` | limit the Meta pass to these stores (default: all, least recently scraped first) |
| `META_MAX_SCROLLS=20` | how deep each store's Meta scrape goes (~350 newest ads, ~3 min per store) |
| `RADAR_MAX_MINUTES=240` | cap for one radar run (sweeps + triage); leftovers continue next time |
| `RADAR_MAX_AGE_DAYS=180` / `RADAR_MIN_ACTIVE_ADS=10` | promotion thresholds: (age <= 180 d OR a product <= 30 d old with >= 3 ads) AND >= 10 active ads |
| `RADAR_SWEEP_WEEKDAY=6` | which weekday the sweeps run (6 = Sunday) |

## 7. Radar (finding new stores)

Runs on its own inside the 22:00 task. Sunday night / Monday morning: open the Candidates tab and
`python tracker.py radar-report`; delete hook phrases with a `delete` verdict from `radar\hooks.txt` after two sweeps,
add siblings of the top producers. Any day: type `Y` in the `promote` column of a Candidates row to force it onto the
watchlist (picked up by the next sync + radar run).
