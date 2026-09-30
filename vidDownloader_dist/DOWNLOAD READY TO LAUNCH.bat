@echo off
setlocal
cd /d "%~dp0"
title vidDownloader - ready to launch
echo Downloading the videos of every product whose ClickUp status is "ready to launch".
echo Already downloaded videos are skipped. Leave this window open.
echo.
python vidDownloader.py download --status "ready to launch"
echo.
echo ==========================================================
echo   Finished. The videos are in the vidDownloader_output folder,
echo   one folder per product. You can close this window.
echo ==========================================================
pause
