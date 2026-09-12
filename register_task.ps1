# Registers two Task Scheduler tasks:
#   "ShopifyTracker Daily"  run_daily.bat        at -Time     (09:00): catalogues + shop ids + Sheets, ~10 min
#   "ShopifyTracker Meta"   run_daily.bat meta   at -MetaTime (22:00): Meta Ad Library for the watchlist, up to 8 h
# Run from PowerShell in the project folder:   .\register_task.ps1
#                                  or, e.g.:   .\register_task.ps1 -Time 07:30 -MetaTime 23:00
#                                              .\register_task.ps1 -NoMeta      (morning task only)
# Re-running replaces the existing task (/F), so changing the time is just running it again.
# Verify:   schtasks /Query /TN "ShopifyTracker Daily" /V /FO LIST
# Test:     schtasks /Run /TN "ShopifyTracker Daily"
# Remove:   schtasks /Delete /TN "ShopifyTracker Daily" /F
param([string]$Time = "09:00", [string]$MetaTime = "22:00", [switch]$NoMeta)
$ErrorActionPreference = "Stop"
if ($Time -notmatch '^\d{2}:\d{2}$') { throw "Time must be HH:MM (24h), e.g. 09:00" }
if ($MetaTime -notmatch '^\d{2}:\d{2}$') { throw "MetaTime must be HH:MM (24h), e.g. 22:00" }
$bat = Join-Path $PSScriptRoot "run_daily.bat"
if (-not (Test-Path $bat)) { throw "run_daily.bat not found next to this script" }

# Exposed as an env var so the schtasks line below can be passed verbatim (--% stops
# PowerShell's own argument parsing, which mangles embedded quotes differently in 5.1 vs 7).
# The tasks run through run_hidden.vbs: no console window, so a run cannot be killed by closing one.
$vbs = Join-Path $PSScriptRoot "run_hidden.vbs"
$env:TRACKER_BAT = $bat
$env:TRACKER_VBS = $vbs
$env:TRACKER_TIME = $Time
Write-Host "Running: schtasks /Create /TN ""ShopifyTracker Daily"" /TR ""wscript.exe \""$vbs\"""" /SC DAILY /ST $Time /F"
schtasks --% /Create /TN "ShopifyTracker Daily" /TR "wscript.exe \"%TRACKER_VBS%\"" /SC DAILY /ST %TRACKER_TIME% /F
if ($LASTEXITCODE -ne 0) { throw "schtasks failed with exit code $LASTEXITCODE" }
if ($NoMeta) {
  schtasks /Delete /TN "ShopifyTracker Meta" /F 2>$null | Out-Null
  Write-Host "Meta night task not registered (-NoMeta); `run` will only include Meta if you run it by hand with --ads."
} else {
  $env:TRACKER_META_TIME = $MetaTime
  Write-Host "Running: schtasks /Create /TN ""ShopifyTracker Meta"" /TR ""wscript.exe \""$vbs\"" meta"" /SC DAILY /ST $MetaTime /F"
  schtasks --% /Create /TN "ShopifyTracker Meta" /TR "wscript.exe \"%TRACKER_VBS%\" meta" /SC DAILY /ST %TRACKER_META_TIME% /F
  if ($LASTEXITCODE -ne 0) { throw "schtasks (meta) failed with exit code $LASTEXITCODE" }
}

# Missed-start behaviour. schtasks /Create cannot set these, so they are applied afterwards:
#   StartWhenAvailable  run a missed 09:00 / 22:00 start as soon as the laptop is awake again
#   WakeToRun           wake the machine from sleep for the start (not from a closed lid / shutdown)
#   battery flags       do not skip or kill the run because the laptop is unplugged
#   ExecutionTimeLimit  kill a stuck run after 14 h (the night task can legitimately take 12 h)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Hours 14)
$names = @("ShopifyTracker Daily")
if (-not $NoMeta) { $names += "ShopifyTracker Meta" }
foreach ($n in $names) {
  try {
    Set-ScheduledTask -TaskName $n -Settings $settings | Out-Null
    Write-Host "Applied to '$n': run when missed, wake to run, allowed on battery."
  } catch {
    Write-Warning "Could not set missed-start options on '$n' ($($_.Exception.Message)). Open Task Scheduler > task > Settings and tick 'Run task as soon as possible after a scheduled start is missed' and Conditions > 'Wake the computer to run this task'."
  }
}

Write-Host ""
Write-Host "Registered. Verify with:   schtasks /Query /TN ""ShopifyTracker Daily"" /V /FO LIST   (and ""ShopifyTracker Meta"")"
Write-Host "Run it now to test with:   schtasks /Run /TN ""ShopifyTracker Daily""   (then check logs\)"
Write-Host "NOTE: without /RU and /RP the task only runs while you are logged on. To run when logged"
Write-Host "      off, open the task's Properties in Task Scheduler and choose 'Run whether user is"
Write-Host "      logged on or not', or re-register adding /RU <user> /RP <password>."
