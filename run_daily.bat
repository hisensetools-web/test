@echo off
REM Daily tracker run. Registered in Task Scheduler by register_task.ps1 (two tasks):
REM   run_daily.bat        morning: Shopify snapshot + stock probe + Sheets sync (about 15 min)
REM   run_daily.bat meta   night:   Meta Ad Library pass for the whole watchlist within
REM                                 META_NIGHT_MINUTES (default 600) or until META_STOP_AT (08:30), then a Sheets sync
REM Appends stdout+stderr to logs\run_YYYY-MM-DD.log (or logs\meta_YYYY-MM-DD.log).
REM tracker.py reads .env itself (SHEETS_WEBHOOK_URL, META_STORES, ...).
setlocal EnableDelayedExpansion
cd /d "%~dp0"
if not exist logs mkdir logs
set MODE=%~1
if not defined MODE set MODE=day
if not defined META_NIGHT_MINUTES set META_NIGHT_MINUTES=600
REM the night pass stops at this local time whatever the budget, so it never runs into the 09:00 morning task
if not defined META_STOP_AT set META_STOP_AT=08:30

REM Locale-independent date via PowerShell (%DATE% format varies by region).
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set LOG=logs\run_%TODAY%.log
if /i "%MODE%"=="meta" set LOG=logs\meta_%TODAY%.log

REM Pick a Python that actually runs and is 3.11 or newer. Each candidate is probed by
REM executing it; a missing launcher, a missing 3.x version, or the Microsoft Store
REM "python" stub all fail the probe and are skipped.
REM Order: project venv, py launcher (any installed 3.x), python, python3.
set "PY="
REM (relative, unquoted on purpose: a leading quote inside for /f below would be stripped by cmd)
if exist ".venv\Scripts\python.exe" call :try .venv\Scripts\python.exe
if not defined PY call :try py -3
if not defined PY call :try python
if not defined PY call :try python3
if not defined PY (
  echo ==== %DATE% %TIME% ERROR: no Python 3.11+ found - tried .venv\Scripts\python.exe, py -3, python, python3 >> "%LOG%"
  echo No Python 3.11+ found. See "%LOG%".
  endlocal & exit /b 9009
)
for /f %%v in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set PYVER=%%v

echo ==== %DATE% %TIME% start %MODE% (%PY% = Python %PYVER%) >> "%LOG%"
REM A night start that Task Scheduler missed (laptop asleep at 22:00) is re-run "as soon as possible", which can be
REM the next morning, on top of the 09:00 task. The night pass is 8 h of browser time: skip it between 07:00 and
REM 20:00 and let tonight's start do it.
for /f %%h in ('powershell -NoProfile -Command "(Get-Date).Hour"') do set HOUR=%%h
if /i "%MODE%"=="meta" if %HOUR% GEQ 7 if %HOUR% LSS 20 (
  echo ==== %DATE% %TIME% night start missed and caught up at %HOUR%:00 - skipped, tonight's start will run it >> "%LOG%"
  endlocal & exit /b 0
)
if /i "%MODE%"=="meta" (
  %PY% tracker.py ads --max-minutes %META_NIGHT_MINUTES% >> "%LOG%" 2>&1
  set RC=!ERRORLEVEL!
  %PY% tracker.py radar >> "%LOG%" 2>&1
  %PY% tracker.py sync-sheets >> "%LOG%" 2>&1
) else (
  %PY% tracker.py run --no-ads >> "%LOG%" 2>&1
  set RC=!ERRORLEVEL!
)
echo ==== %DATE% %TIME% exit code %RC% >> "%LOG%"
REM The diagnostics report (versions, task status, database counts, per-store status, log errors) goes to the
REM Google Doc "EarlyScale Diag" so problems can be read there instead of pasting logs.
%PY% tracker.py diag --note "after run_daily.bat %MODE% (exit code %RC%)" >> "%LOG%" 2>&1
endlocal & exit /b %RC%

:try
REM Usage: call :try <command...>. Sets PY if the command runs and reports Python >= 3.11.
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
goto :eof
