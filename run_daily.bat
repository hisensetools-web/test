@echo off
REM Daily tracker run. Registered in Task Scheduler by register_task.ps1.
REM Appends stdout+stderr to logs\run_YYYY-MM-DD.log.
REM tracker.py reads .env itself, so if SHEETS_WEBHOOK_URL is set there the run ends with a
REM Google Sheets sync automatically (exit code 3 = run fine, sync failed).
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs

REM Locale-independent date via PowerShell (%DATE% format varies by region).
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set LOG=logs\run_%TODAY%.log

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

echo ==== %DATE% %TIME% start (%PY% = Python %PYVER%) >> "%LOG%"
%PY% tracker.py run >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo ==== %DATE% %TIME% exit code %RC% >> "%LOG%"
endlocal & exit /b %RC%

:try
REM Usage: call :try <command...>. Sets PY if the command runs and reports Python >= 3.11.
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
goto :eof
