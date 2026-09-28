@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title vidDownloader - one-time setup
echo ==========================================================
echo   vidDownloader - one-time setup
echo ==========================================================
echo.

rem --- 1. Python (the Microsoft Store stub named python.exe does not count) ---
python -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo Python is not installed yet. Installing it now, this takes a minute...
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --silent
    echo.
    echo Python is installed. This window must be closed for Windows to notice it.
    echo   1. Close this window.
    echo   2. Double-click setup.bat once more.
    echo.
    pause
    exit /b 0
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo Python  : %%v

rem --- 2. ffmpeg (merges best video+audio, removes metadata) ---
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg is not installed yet. Installing it now...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements --silent
    echo.
    echo ffmpeg is installed. This window must be closed for Windows to notice it.
    echo   1. Close this window.
    echo   2. Double-click setup.bat once more.
    echo.
    pause
    exit /b 0
)
echo ffmpeg  : found

rem --- 3. the Python packages the tool uses ---
echo Installing the downloader packages (yt-dlp, requests)...
python -m pip install --quiet --upgrade pip >nul 2>&1
python -m pip install --quiet --upgrade -r requirements-vidDownloader.txt
if errorlevel 1 (
    echo.
    echo The packages could not be installed. Check the internet connection and run setup.bat again.
    pause
    exit /b 1
)
echo packages: installed

rem --- 4. settings file (created once, never overwritten) ---
if not exist ".env" (
    > .env echo # vidDownloader settings. One KEY=VALUE per line, lines starting with # are ignored.
    >> .env echo # Instagram links need a logged-in browser. Log into instagram.com in Firefox once, then leave this as is.
    >> .env echo VIDDL_COOKIES_FROM_BROWSER=firefox
    echo settings: .env created
) else (
    echo settings: .env kept
)
findstr /B /C:"VIDDL_CLICKUP_TOKEN=pk_" .env >nul 2>&1
if errorlevel 1 (
    echo.
    echo The products come from ClickUp. You need your personal API token, one time:
    echo   ClickUp ^> your avatar bottom-left ^> Settings ^> Apps ^> API Token ^> Generate / Copy
    echo It starts with pk_ . Paste it here and press Enter ^(right-click pastes in this window^).
    set /p CU_TOKEN=ClickUp token: 
    if not "!CU_TOKEN!"=="" (
        >> .env echo VIDDL_CLICKUP_TOKEN=!CU_TOKEN!
        echo settings: ClickUp token saved to .env
    ) else (
        echo settings: no token given; the tool falls back to the Google Sheet until you run setup.bat again
    )
)

rem --- 5. prove it works ---
echo.
echo Checking the tool and the Google Sheet...
echo.
python vidDownloader.py check
echo.
echo ==========================================================
echo   Setup finished. From now on double-click DOWNLOAD.bat
echo ==========================================================
pause
