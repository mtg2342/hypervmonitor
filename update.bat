@echo off
title Hyper-V Monitor - Update
cd /d "%~dp0"

echo ============================================
echo   Hyper-V Monitor - Pulling Updates
echo ============================================
echo.

:: Check admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Administrator privileges required.
    echo Right-click update.bat and select "Run as administrator"
    pause
    exit /b 1
)

:: Check git
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: git is not installed or not on PATH.
    echo Install Git for Windows from https://git-scm.com/download/win
    pause
    exit /b 1
)

:: Stop any running instance (python processes that own app.py)
echo Stopping any running instance...
for /f "tokens=2" %%i in ('tasklist /v /fi "imagename eq python.exe" /fo csv ^| findstr /i "app.py"') do (
    taskkill /f /pid %%i >nul 2>&1
)
:: Also kill any python.exe running from this directory as fallback
wmic process where "name='python.exe' and CommandLine like '%%app.py%%'" delete >nul 2>&1
timeout /t 2 /nobreak >nul

:: Show what will change
echo.
echo Fetching latest changes from GitHub...
git fetch origin
if %errorlevel% neq 0 (
    echo ERROR: git fetch failed. Check network and credentials.
    pause
    exit /b 1
)

echo.
echo Changes incoming:
git log --oneline HEAD..origin/main 2>nul
if %errorlevel% neq 0 (
    git log --oneline HEAD..origin/master 2>nul
)
echo.

:: Pull (the database is gitignored so it survives)
echo Applying updates...
git pull --ff-only
if %errorlevel% neq 0 (
    echo.
    echo ERROR: git pull failed. This could mean:
    echo   - Local changes conflict with remote (commit/stash or revert them)
    echo   - The branch has diverged
    echo.
    pause
    exit /b 1
)

:: Update Python deps if requirements changed
if exist requirements.txt (
    echo.
    echo Updating Python dependencies...
    pip install -r requirements.txt --quiet 2>nul
)

echo.
echo ============================================
echo   Update complete. Database preserved.
echo   Starting Hyper-V Monitor...
echo ============================================
echo.

:: Restart the app
start "Hyper-V Monitor" cmd /c "start.bat"

echo.
echo Hyper-V Monitor restarted. You can close this window.
timeout /t 5 /nobreak >nul
