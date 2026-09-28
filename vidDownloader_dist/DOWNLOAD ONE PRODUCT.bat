@echo off
setlocal
cd /d "%~dp0"
title vidDownloader - one product
echo Type part of the product name as it is in ClickUp (for example: skull  or  ghostface)
echo.
set /p NAME=Product name: 
if "%NAME%"=="" (
    echo Nothing typed, nothing downloaded.
    pause
    exit /b 0
)
echo.
python vidDownloader.py download --only "%NAME%"
echo.
echo Finished. You can close this window.
pause
