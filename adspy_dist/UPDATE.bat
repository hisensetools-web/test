@echo off
setlocal
cd /d "%~dp0"
title Adspy video downloader - update
echo Updating the downloader packages (TikTok and Instagram change often; run this when downloads start failing).
echo.
python -m pip install --quiet --upgrade -r requirements-adspy.txt
python adspy.py check
echo.
pause
