@echo off
title Hyper-V Monitor
cd /d "%~dp0"

:: Check for admin privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ============================================
    echo   ERROR: Administrator privileges required
    echo ============================================
    echo.
    echo Hyper-V cmdlets need admin access to read VM metrics.
    echo Right-click start.bat and select "Run as administrator"
    echo.
    pause
    exit /b 1
)

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: Install dependencies
echo Installing dependencies...
pip install -r requirements.txt --quiet 2>nul

echo.
echo ============================================
echo   Hyper-V Monitor
echo   http://127.0.0.1:5000
echo ============================================
echo.
echo Press Ctrl+C to stop.
echo.

python app.py
pause
