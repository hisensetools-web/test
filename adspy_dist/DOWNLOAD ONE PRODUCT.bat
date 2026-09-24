@echo off
setlocal
cd /d "%~dp0"
title Adspy video downloader - one product
echo Type part of the product name exactly as it is on the sheet (for example: skull  or  ghostface)
echo.
set /p NAME=Product name: 
if "%NAME%"=="" (
    echo Nothing typed, nothing downloaded.
    pause
    exit /b 0
)
echo.
python adspy.py download --only "%NAME%"
echo.
echo Finished. You can close this window.
pause
