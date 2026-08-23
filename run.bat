@echo off
title Telegram Group Leaver Pro - By Kero309x
color 0b

echo ===================================================
echo     Telegram Group ^& Channel Leaver Pro (v2.5)
echo     Engineered by @Kero309x (GitHub)
echo ===================================================
echo.

:: Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    color 0c
    echo [ERROR] Python is not found in system PATH!
    echo Please install Python 3.8+ and make sure to check "Add Python to PATH".
    echo.
    pause
    exit /b
)

:: Install required dependencies
echo [*] Checking and verifying dependencies...
pip install -r requirements.txt >nul 2>&1

:: Open browser automatically after 2 seconds
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"

:: Start the Flask Web Application
echo [+] Starting Flask Web Server on http://localhost:5000 ...
echo [!] Keep this terminal window open while using the tool. Press Ctrl+C to stop.
echo.
python app.py

pause
