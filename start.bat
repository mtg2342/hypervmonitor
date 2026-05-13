@echo off
title Hyper-V Monitor
cd /d "%~dp0"

:: Admin check
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

:: Resolve a working Python command:
::   1) .python_path file (written by deploy.ps1) -- absolute path, foolproof
::   2) py launcher (Python for Windows launcher, ignores MS Store stub)
::   3) plain python (last resort)
set "PY="
if exist ".python_path" (
    for /f "usebackq delims=" %%i in (".python_path") do set "PY=%%i"
)
if not defined PY (
    where py >nul 2>&1
    if %errorlevel%==0 set "PY=py"
)
if not defined PY (
    where python >nul 2>&1
    if %errorlevel%==0 set "PY=python"
)
if not defined PY (
    echo ERROR: No Python found. Run deploy.ps1 to set things up:
    echo   iex ^(irm https://raw.githubusercontent.com/mtg2342/hypervmonitor/main/deploy.ps1^)
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Hyper-V Monitor
echo   http://127.0.0.1:5000
echo ============================================
echo.
echo Using Python: %PY%
echo Press Ctrl+C to stop.
echo.

"%PY%" app.py
pause
