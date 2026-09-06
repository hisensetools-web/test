@echo off
REM Daily tracker run. Registered in Task Scheduler by register_task.ps1.
REM Appends stdout+stderr to logs\run_YYYY-MM-DD.log.
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs

REM Locale-independent date via PowerShell (%DATE% format varies by region).
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set LOG=logs\run_%TODAY%.log

REM Pick the Python: project venv first, then the py launcher, then whatever python is on PATH.
set PY=python
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
) else (
  where py >nul 2>&1 && set PY=py -3.11
)

echo ==== %DATE% %TIME% start (%PY%) >> "%LOG%"
%PY% tracker.py run >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo ==== %DATE% %TIME% exit code %RC% >> "%LOG%"
endlocal & exit /b %RC%
