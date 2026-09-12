#!/usr/bin/env bash
# Two-day offline replay of the scheduled sequence (morning: run --no-ads; night: ads, sync-sheets) against
# the fakes: mock Shopify stores, the fake Ad Library (Playwright + local Chromium) and the real Code.gs inside
# tests/fake_gas.js (node). Day 1 = 2026-09-03, day 2 = 2026-09-10 (mutated catalogues, ads a week older), so the
# week-over-week columns of the Winners tab are exercised. Linux / macOS / WSL.
#
#   bash tests/replay.sh            # ~5 min; prints each step's exit code, then the sheet-vs-database table
#   META_CHROMIUM_PATH=/path/to/chrome bash tests/replay.sh   # if Playwright's Chromium is not installed
set -u
cd "$(dirname "$0")/.."
WORK=${REPLAY_DIR:-$(mktemp -d)}; mkdir -p "$WORK"
export TRACKER_DB=$WORK/tracker.db SHEETS_WEBHOOK_URL=http://127.0.0.1:8090/exec META_AD_LIBRARY_BASE=http://127.0.0.1:8095/ads/library/
export META_WAIT_MIN=0.2 META_WAIT_MAX=0.5
WL=tests/watchlist.mock.csv
PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT

start_stores() {   # $1 = day
  local MUT=""; [ "$1" = 2 ] && MUT="--mutate --day 2"
  python -m tests.mock_store --port 8001 --products 320 --seed 1 $MUT >/dev/null 2>&1 & PIDS+=($!)
  python -m tests.mock_store --port 8002 --products 40  --seed 2 --fail-first 430 $MUT >/dev/null 2>&1 & PIDS+=($!)
  python -m tests.mock_store --port 8003 --products 610 --seed 3 $MUT >/dev/null 2>&1 & PIDS+=($!)
  python -m tests.mock_store --port 8004 --products 55  --seed 4 --require-browser $MUT >/dev/null 2>&1 & PIDS+=($!)
  python -m tests.mock_store --port 8005 --products 1   --seed 5 --redirect-to http://127.0.0.1:8006 >/dev/null 2>&1 & PIDS+=($!)
  python -m tests.mock_store --port 8006 --products 130 --seed 5 $MUT >/dev/null 2>&1 & PIDS+=($!)
  sleep 2
  local HANDLES ADDAY=1; [ "$1" = 2 ] && ADDAY=8
  HANDLES=$(curl -s "http://127.0.0.1:8001/products.json?limit=6" | python -c "import json,sys; print(' '.join(p['handle'] for p in json.load(sys.stdin)['products']))")
  python -m tests.fake_ad_library --port 8095 --batches 3 --landing http://127.0.0.1:8001 --handles $HANDLES --day $ADDAY --base-date 2026-09-03 --same-order \
    --landing-map MockOne=http://127.0.0.1:8001 MockTwo=http://127.0.0.1:8002 MockThree=http://127.0.0.1:8003 MockWaf=http://127.0.0.1:8004 MockApex=http://127.0.0.1:8006 \
    > "$WORK/adlib_day$1.log" 2>&1 & PIDS+=($!)
  sleep 1
}
stop_stores() { cleanup; PIDS=(); }

node tests/fake_gas.js --port 8090 > "$WORK/gas.log" 2>&1 & GAS=$!
sleep 1
FAIL=0
for DAY in 1 2; do
  DATE=2026-09-03; [ "$DAY" = 2 ] && DATE=2026-09-10
  start_stores $DAY
  for step in "run --no-ads" "ads --max-minutes 15" "sync-sheets"; do
    name=$(echo "$step" | cut -d' ' -f1)
    python tracker.py $step --watchlist $WL --date $DATE > "$WORK/day${DAY}_$name.log" 2>&1; rc=$?
    printf 'day %d  %-32s exit %d\n' "$DAY" "$step" "$rc"
    [ "$rc" != 0 ] && FAIL=1
  done
  stop_stores
done
kill $GAS 2>/dev/null
echo "--- day 2 sync verification:"; grep -A7 "sheet vs database" "$WORK/day2_sync-sheets.log" | tail -6
echo "--- day 2 winners:"; TRACKER_DB=$WORK/tracker.db python tracker.py report --date 2026-09-10 2>/dev/null | head -20
echo "--- tracebacks / errors in any log:"; grep -l "Traceback\|ERROR" "$WORK"/day*.log || echo none
echo "logs in $WORK"
exit $FAIL
