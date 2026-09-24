@echo off
setlocal
cd /d "%~dp0"
title Adspy video downloader - one-time setup
echo ==========================================================
echo   Adspy video downloader - one-time setup
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
python -m pip install --quiet --upgrade -r requirements-adspy.txt
if errorlevel 1 (
    echo.
    echo The packages could not be installed. Check the internet connection and run setup.bat again.
    pause
    exit /b 1
)
echo packages: installed

rem --- 4. settings file (created once, never overwritten) ---
if not exist ".env" (
    > .env echo # Adspy video downloader settings. One KEY=VALUE per line, lines starting with # are ignored.
    >> .env echo # Instagram links need a logged-in browser. Log into instagram.com in Firefox once, then leave this as is.
    >> .env echo ADSPY_COOKIES_FROM_BROWSER=firefox
    echo settings: .env created
) else (
    echo settings: .env kept
)

rem --- 5. prove it works ---
echo.
echo Checking the tool and the Google Sheet...
echo.
python adspy.py check
echo.
echo ==========================================================
echo   Setup finished. From now on double-click DOWNLOAD.bat
echo ==========================================================
pause
