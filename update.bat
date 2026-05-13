@echo off
title Hyper-V Monitor - Update
echo ============================================
echo   Hyper-V Monitor - Update
echo ============================================
echo.
echo Re-running the unified deploy script. This will:
echo   - Pull the latest commits from GitHub
echo   - Stop the running app
echo   - Update dependencies if needed
echo   - Restart the app
echo.
echo Your collected history and alerts are preserved.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "iex (irm https://raw.githubusercontent.com/mtg2342/hypervmonitor/main/deploy.ps1)"
