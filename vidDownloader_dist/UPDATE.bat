@echo off
setlocal
cd /d "%~dp0"
title vidDownloader - update
echo Updating the downloader packages (TikTok and Instagram change often; run this when downloads start failing).
echo.
python -m pip install --quiet --upgrade -r requirements-vidDownloader.txt
python vidDownloader.py check
echo.
pause
