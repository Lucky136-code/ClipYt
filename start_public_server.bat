@echo off
title ClipYt Public Server (Laptop Host)
cd /d "%~dp0"

echo ========================================================
echo   Launching ClipYt Public Server from your Laptop...
echo ========================================================
echo.
python start_public_server.py
pause
