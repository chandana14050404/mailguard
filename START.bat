@echo off
title MailGuard - Keep This Window Open
color 0A
cls

echo.
echo  ============================================
echo    MailGuard - Email + WhatsApp Automation
echo    Keep this window OPEN while using the app
echo  ============================================
echo.

cd /d "%~dp0"

python --version
if errorlevel 1 (
    echo.
    echo  ERROR: Python not found!
    echo  Download from: https://python.org/downloads
    echo  Tick "Add Python to PATH" when installing.
    echo.
    pause
    exit /b 1
)

echo.
echo  Starting MailGuard...
echo  Browser will open automatically.
echo  If it doesn't, go to: http://localhost:5000
echo.

python app.py

echo.
echo  ============================================
echo  MailGuard stopped. See any error above.
echo  ============================================
echo.
pause
