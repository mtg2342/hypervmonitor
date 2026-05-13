@echo off
title Hyper-V Monitor - First-Time Setup
echo ============================================
echo   Hyper-V Monitor - First-Time Host Setup
echo ============================================
echo.
echo This script is for setting up the host machine for the first time.
echo It will clone the repository and install Python dependencies.
echo.
echo Prerequisites:
echo   - Python 3.10+ installed and on PATH ^(python.org^)
echo   - Git for Windows installed ^(git-scm.com^)
echo   - Administrator privileges
echo.

:: Check admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Administrator privileges required.
    echo Right-click first-time-setup.bat and select "Run as administrator"
    pause
    exit /b 1
)

:: Check git
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: git is not installed. Install from https://git-scm.com/download/win
    pause
    exit /b 1
)

:: Check python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: Default install location
set "INSTALL_DIR=%~dp0"

echo.
echo Install location: %INSTALL_DIR%
echo.
set /p CONFIRM="Continue with setup here? (Y/N): "
if /i not "%CONFIRM%"=="Y" (
    echo Cancelled.
    pause
    exit /b 1
)

cd /d "%INSTALL_DIR%"

:: If this script is being run inside an already-cloned repo, just install deps
if exist ".git" (
    echo.
    echo Existing git repo detected. Skipping clone.
    echo Pulling latest...
    git pull
) else (
    echo.
    echo Cloning repository...
    git clone https://github.com/mtg2342/hypervmonitor.git temp_clone
    if %errorlevel% neq 0 (
        echo ERROR: git clone failed. Check network and repo URL.
        pause
        exit /b 1
    )
    :: Move contents up one level
    xcopy /e /h /y temp_clone\* .\ >nul
    rmdir /s /q temp_clone
)

echo.
echo Installing Python dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo WARNING: pip install reported errors. The app may still work if Flask is already installed.
)

echo.
echo ============================================
echo   Setup complete.
echo.
echo   To start:        Right-click start.bat   -^> Run as administrator
echo   To update later: Right-click update.bat  -^> Run as administrator
echo ============================================
echo.
pause
