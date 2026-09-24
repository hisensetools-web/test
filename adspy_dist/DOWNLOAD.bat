@echo off
setlocal
cd /d "%~dp0"
title Adspy video downloader
echo Downloading every Adspy video on the sheet. Already downloaded videos are skipped.
echo Leave this window open; it closes nothing and tells you when it is done.
echo.
python adspy.py download
echo.
echo ==========================================================
echo   Finished. The videos are in the adspy_output folder,
echo   one folder per product. You can close this window.
echo ==========================================================
pause
