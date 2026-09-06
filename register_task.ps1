# Registers run_daily.bat in Windows Task Scheduler, daily at 09:00 (or -Time HH:MM).
# Run from PowerShell in the project folder:   .\register_task.ps1
#                                  or, e.g.:   .\register_task.ps1 -Time 07:30
# Re-running replaces the existing task (/F), so changing the time is just running it again.
# Verify:   schtasks /Query /TN "ShopifyTracker Daily" /V /FO LIST
# Test:     schtasks /Run /TN "ShopifyTracker Daily"
# Remove:   schtasks /Delete /TN "ShopifyTracker Daily" /F
param([string]$Time = "09:00")
$ErrorActionPreference = "Stop"
if ($Time -notmatch '^\d{2}:\d{2}$') { throw "Time must be HH:MM (24h), e.g. 09:00" }
$bat = Join-Path $PSScriptRoot "run_daily.bat"
if (-not (Test-Path $bat)) { throw "run_daily.bat not found next to this script" }

# Exposed as an env var so the schtasks line below can be passed verbatim (--% stops
# PowerShell's own argument parsing, which mangles embedded quotes differently in 5.1 vs 7).
$env:TRACKER_BAT = $bat
$env:TRACKER_TIME = $Time
Write-Host "Running: schtasks /Create /TN ""ShopifyTracker Daily"" /TR ""\""$bat\"""" /SC DAILY /ST $Time /F"
schtasks --% /Create /TN "ShopifyTracker Daily" /TR "\"%TRACKER_BAT%\"" /SC DAILY /ST %TRACKER_TIME% /F
if ($LASTEXITCODE -ne 0) { throw "schtasks failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "Registered. Verify with:   schtasks /Query /TN ""ShopifyTracker Daily"" /V /FO LIST"
Write-Host "Run it now to test with:   schtasks /Run /TN ""ShopifyTracker Daily""   (then check logs\)"
Write-Host "NOTE: without /RU and /RP the task only runs while you are logged on. To run when logged"
Write-Host "      off, open the task's Properties in Task Scheduler and choose 'Run whether user is"
Write-Host "      logged on or not', or re-register adding /RU <user> /RP <password>."
