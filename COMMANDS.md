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
pencil > New version). The sheet now has two tabs: **Winners** and **Stores**. The old tabs (Signals, Early,
Families, Categories, Pages, Ads, Candidates, Products, Alerts) are no longer written; delete them by hand when you like.

`python tracker.py sync-sheets` refuses to write until the new `Code.gs` is deployed (it would put values under the
wrong headers otherwise) and tells you so.

## 1b. A whole day by hand

Same order as the scheduled tasks, one at a time in one PowerShell window:

```powershell
python tracker.py run --no-ads                         # 1. catalogues: id, handle, title, dates of every product + shop id (~10 min)
python tracker.py ads --max-minutes 480                # 2. Meta pass: every ad of each store, badge read per card (~3 min/store)
python tracker.py sync-sheets                          # 3. push Winners / Stores and verify the row counts
python tracker.py report                               # 4. the Winners tab in the terminal
```

Shorter versions:

```powershell
python tracker.py run --only x.com y.com               # catalogues for a few stores
python tracker.py ads --only x.com y.com               # Meta pass for a few stores
python tracker.py ads --max-minutes 120                # Meta pass, stop after 2 h (the rest continue next time)
```

Nothing is lost if you stop a command with Ctrl+C: every pass writes as it goes, and the next run continues from what is stored.

## 2. Runs by itself (nothing to do)

| when | task | what you get |
|---|---|---|
| 09:00 daily | catalogues + shop ids, Sheets sync | Stores tab refreshed, product families and ages current; ~10 min |
| 22:00 daily | Meta Ad Library (least recently scraped stores first, until 08:30), then Sheets sync | Winners tab (delivering per landing URL, week over week) |

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

You only need domains. The night pass searches each domain in the Ad Library, which returns every Facebook page
advertising it (the whitepage personas included); no page name is needed and none is looked up.

```powershell
python tracker.py add-store a.com b.com c.com          # add stores (as many as you like on one line)
python tracker.py add-store --file domains.txt         # one domain per line (a CSV's first column works too)
python tracker.py remove-store x.com y.com             # drop stores (history in the DB is kept)
python tracker.py prune-dead                           # list domains that never returned a catalogue or an ad
python tracker.py prune-dead --apply                   # ...and remove them from the watchlist
python tracker.py restore-stores --apply               # put removed stores back (all of them, or name domains after --apply)
python tracker.py pages elivorahealth.com              # which pages advertise this domain (the same search the night pass runs)
python tracker.py pages elivorahealth.com --set 2      # pin the store to page 2 only (rarely wanted; the domain search covers every page)
python tracker.py ads --only a.com b.com               # scrape the new stores now instead of waiting for 22:00
```

Editing `watchlist.csv` by hand works the same way: one domain per line under `store_domain`, the other columns empty.
Use the apex domain (`brand.com`, not `www.brand.com`). Keep one entry per advertiser: two entries sharing one
Facebook page (luma.viture.com and viture.com) each get the same ads.

## 6. Settings (`.env`)

| line | effect |
|---|---|
| `SHEETS_WEBHOOK_URL=...` | your Apps Script /exec URL; without it there is no sync |
| `META_ADS=1` | `run` includes the Meta pass when run by hand (the night task does it anyway) |
| `META_STORES=a.com,b.com` | limit the Meta pass to these stores (default: all, least recently scraped first) |
| `META_MAX_SCROLLS=20` | how deep each store's Meta scrape goes (~350 newest ads, ~3 min per store) |
