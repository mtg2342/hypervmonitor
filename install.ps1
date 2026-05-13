# Hyper-V Monitor — One-line installer
# Usage from any PowerShell window:
#   iex (irm https://raw.githubusercontent.com/mtg2342/hypervmonitor/main/install.ps1)

$ErrorActionPreference = 'Stop'
$REPO         = 'https://github.com/mtg2342/hypervmonitor.git'
$RAW_INSTALLER = 'https://raw.githubusercontent.com/mtg2342/hypervmonitor/main/install.ps1'
$DEFAULT_PATH = 'C:\hypervmonitor'

function Write-Section($text) {
    Write-Host ''
    Write-Host '================================================' -ForegroundColor Cyan
    Write-Host (" $text") -ForegroundColor Cyan
    Write-Host '================================================' -ForegroundColor Cyan
    Write-Host ''
}

function Test-Command($name) {
    try { Get-Command $name -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

# ── Self-elevate if not running as admin ─────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'Administrator privileges required. Relaunching elevated...' -ForegroundColor Yellow
    $args = "-NoExit -NoProfile -ExecutionPolicy Bypass -Command `"iex (irm $RAW_INSTALLER)`""
    Start-Process powershell -Verb RunAs -ArgumentList $args
    return
}

Write-Section 'Hyper-V Monitor — Installer'

# ── Prereq: Git ──────────────────────────────────────────────────────────────
if (-not (Test-Command git)) {
    Write-Host 'Git is not installed.' -ForegroundColor Red
    if (Test-Command winget) {
        $r = Read-Host 'Install Git via winget? (Y/n)'
        if ($r -ne 'n') {
            winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
            Refresh-Path
        }
    } else {
        Write-Host 'Install from https://git-scm.com/download/win and re-run this installer.' -ForegroundColor Yellow
        Read-Host 'Press Enter to exit'; return
    }
    if (-not (Test-Command git)) {
        Write-Host 'Git still not on PATH. Open a new PowerShell window and re-run the installer.' -ForegroundColor Red
        Read-Host 'Press Enter to exit'; return
    }
}
Write-Host ('Git:    ' + (git --version)) -ForegroundColor Green

# ── Prereq: Python ───────────────────────────────────────────────────────────
if (-not (Test-Command python)) {
    Write-Host 'Python is not installed.' -ForegroundColor Red
    if (Test-Command winget) {
        $r = Read-Host 'Install Python 3.12 via winget? (Y/n)'
        if ($r -ne 'n') {
            winget install --id Python.Python.3.12 -e --source winget --accept-source-agreements --accept-package-agreements
            Refresh-Path
        }
    } else {
        Write-Host "Install Python 3.10+ from https://python.org/downloads (check 'Add to PATH') and re-run." -ForegroundColor Yellow
        Read-Host 'Press Enter to exit'; return
    }
    if (-not (Test-Command python)) {
        Write-Host 'Python still not on PATH. Open a new PowerShell window and re-run the installer.' -ForegroundColor Red
        Read-Host 'Press Enter to exit'; return
    }
}
Write-Host ('Python: ' + (python --version 2>&1)) -ForegroundColor Green

# ── Install location ─────────────────────────────────────────────────────────
Write-Host ''
$path = Read-Host "Install location [$DEFAULT_PATH]"
if ([string]::IsNullOrWhiteSpace($path)) { $path = $DEFAULT_PATH }
$path = $path.TrimEnd('\')

# ── Clone or pull ────────────────────────────────────────────────────────────
if (Test-Path "$path\.git") {
    Write-Host ''
    Write-Host "Existing checkout at $path detected — pulling latest..." -ForegroundColor Yellow
    Push-Location $path
    git pull --ff-only
    Pop-Location
} elseif (Test-Path $path) {
    Write-Host ''
    Write-Host "Folder exists at $path but is not a git checkout." -ForegroundColor Red
    $r = Read-Host 'Delete and re-clone? (y/N)'
    if ($r -ne 'y') { Write-Host 'Aborted.'; return }
    Remove-Item -Recurse -Force $path
    git clone $REPO $path
} else {
    Write-Host ''
    Write-Host "Cloning $REPO -> $path..." -ForegroundColor Cyan
    git clone $REPO $path
}

# ── Install Python deps ──────────────────────────────────────────────────────
Write-Host ''
Write-Host 'Installing Python dependencies (Flask)...' -ForegroundColor Cyan
Push-Location $path
python -m pip install -r requirements.txt --quiet
Pop-Location

Write-Section 'Installation complete'

Write-Host "Installed at:    $path" -ForegroundColor Cyan
Write-Host "Dashboard URL:   http://127.0.0.1:5000"
Write-Host ""
Write-Host "To start:        $path\start.bat   (right-click -> Run as administrator)"
Write-Host "To update:       $path\update.bat  (right-click -> Run as administrator)"
Write-Host ""

# ── Optional: auto-start at login via Task Scheduler ─────────────────────────
$r = Read-Host 'Set up auto-start at login? (Y/n)'
if ($r -ne 'n') {
    $taskName = 'HyperVMonitor'
    $action   = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$path\start.bat`""
    $trigger  = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "Scheduled task '$taskName' created. It will start at next login." -ForegroundColor Green
}

# ── Optional: start now ──────────────────────────────────────────────────────
Write-Host ''
$r = Read-Host 'Start the dashboard now? (Y/n)'
if ($r -ne 'n') {
    Write-Host 'Launching...' -ForegroundColor Cyan
    Start-Process -FilePath "$path\start.bat" -WorkingDirectory $path
    Start-Sleep -Seconds 4
    Start-Process 'http://127.0.0.1:5000'
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
