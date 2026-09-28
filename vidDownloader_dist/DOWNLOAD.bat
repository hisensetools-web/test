@echo off
setlocal
cd /d "%~dp0"
title vidDownloader
echo Downloading every product video listed in ClickUp. Already downloaded videos are skipped.
echo Leave this window open; it closes nothing and tells you when it is done.
echo.
python vidDownloader.py download
echo.
echo ==========================================================
echo   Finished. The videos are in the vidDownloader_output folder,
echo   one folder per product. You can close this window.
echo ==========================================================
pause
