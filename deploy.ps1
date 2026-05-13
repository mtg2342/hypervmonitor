# Hyper-V Monitor — All-in-one deployment script
#
# One script for everything: fresh install, update, and restart.
# Idempotent — safe to run anytime.
#
#   iex (irm https://raw.githubusercontent.com/mtg2342/hypervmonitor/main/deploy.ps1)
#
# What it does, in order:
#   1. Self-elevates to Administrator (UAC prompt)
#   2. Cleans up leftover state (stale env vars)
#   3. Installs Git via winget if missing
#   4. Installs real Python via winget if missing — routes around the
#      Microsoft Store "App Execution Alias" stub automatically
#   5. Clones the repo (fresh install) OR pulls + stops the running app
#      and restarts it (update)
#   6. Records the real python.exe path so start.bat never has to
#      re-detect it
#   7. Installs / upgrades Flask
#   8. Offers to register a Task Scheduler entry for auto-start at login
#   9. Launches the dashboard at http://127.0.0.1:5000

$ErrorActionPreference = 'Stop'

# ── Constants ────────────────────────────────────────────────────────────────
$REPO_OWNER   = 'mtg2342'
$REPO_NAME    = 'hypervmonitor'
$REPO_URL     = "https://github.com/$REPO_OWNER/$REPO_NAME.git"
$RAW_SCRIPT   = "https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/deploy.ps1"
$DEFAULT_PATH = 'C:\hypervmonitor'
$TASK_NAME    = 'HyperVMonitor'

# When HVM_AUTO=1 the script is being invoked from the in-app "Apply Update"
# button or the nightly auto-update task. Skip prompts and default to safe choices.
$AUTO = ($env:HVM_AUTO -eq '1')

# ── Helpers ──────────────────────────────────────────────────────────────────
function Write-Section($text) {
    Write-Host ''
    Write-Host '================================================' -ForegroundColor Cyan
    Write-Host (" $text") -ForegroundColor Cyan
    Write-Host '================================================' -ForegroundColor Cyan
}

function Test-Command($name) {
    try { Get-Command $name -ErrorAction Stop | Out-Null; $true }
    catch { $false }
}

function Refresh-Path {
    $env:Path = ([Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                 [Environment]::GetEnvironmentVariable('Path','User'))
}

# Find a python.exe that actually runs — skipping the Microsoft Store stub
# at %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe which only opens the Store.
function Find-Python {
    # 1) Try the Python launcher `py` — it's the most reliable on Windows
    if (Test-Command py) {
        try {
            $v = & py --version 2>&1 | Out-String
            if ($v -match '^\s*Python \d') { return 'py' }
        } catch { }
    }
    # 2) Try every `python` on PATH except the WindowsApps stub
    $cands = @(Get-Command python -All -ErrorAction SilentlyContinue)
    foreach ($c in $cands) {
        if ($c.Source -like '*\WindowsApps\*') { continue }
        try {
            $v = & $c.Source --version 2>&1 | Out-String
            if ($v -match '^\s*Python \d') { return $c.Source }
        } catch { }
    }
    # 3) Look in standard install locations (winget / python.org)
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        'C:\Program Files\Python313\python.exe',
        'C:\Program Files\Python312\python.exe',
        'C:\Program Files\Python311\python.exe',
        'C:\Program Files\Python310\python.exe'
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Stop-RunningApp($installPath) {
    # Stop any python process whose command line references this install path
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$installPath*" } |
        ForEach-Object {
            Write-Host ("Stopping running python.exe (PID " + $_.ProcessId + ")...") -ForegroundColor DarkGray
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    Start-Sleep -Seconds 1
}

# ── Self-elevate ─────────────────────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'Administrator privileges required. Relaunching elevated...' -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-NoExit -NoProfile -ExecutionPolicy Bypass -Command `"iex (irm $RAW_SCRIPT)`""
    return
}

Write-Section 'Hyper-V Monitor — Deployment'

# Clean up any leftover token env var from prior private-repo experiments
if ($env:GH_TOKEN) {
    Write-Host 'Removing stale $env:GH_TOKEN (repo is public — not needed).' -ForegroundColor DarkGray
    Remove-Item Env:\GH_TOKEN -ErrorAction SilentlyContinue
}

# ── Git ──────────────────────────────────────────────────────────────────────
if (-not (Test-Command git)) {
    Write-Host 'Git is not installed.' -ForegroundColor Yellow
    if (Test-Command winget) {
        Write-Host 'Installing Git via winget...' -ForegroundColor Cyan
        winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
        Refresh-Path
    } else {
        Write-Host 'Install Git from https://git-scm.com/download/win then re-run.' -ForegroundColor Yellow
        Read-Host 'Press Enter to exit'; return
    }
    if (-not (Test-Command git)) {
        Write-Host 'Git installed but not yet on PATH. Open a NEW PowerShell window and re-run.' -ForegroundColor Red
        Read-Host 'Press Enter to exit'; return
    }
}
Write-Host ('Git:    ' + (git --version)) -ForegroundColor Green

# ── Python ───────────────────────────────────────────────────────────────────
$python = Find-Python
if (-not $python) {
    Write-Host 'Real Python not detected (Microsoft Store stub does not count).' -ForegroundColor Yellow
    if (Test-Command winget) {
        Write-Host 'Installing Python 3.12 via winget...' -ForegroundColor Cyan
        winget install --id Python.Python.3.12 -e --source winget --accept-source-agreements --accept-package-agreements
        Refresh-Path
        $python = Find-Python
    } else {
        Write-Host 'Install Python 3.10+ from https://python.org/downloads then re-run.' -ForegroundColor Yellow
        Read-Host 'Press Enter to exit'; return
    }
}
if (-not $python) {
    Write-Host '' -ForegroundColor Red
    Write-Host 'Python is installed but no working python.exe was found.' -ForegroundColor Red
    Write-Host 'Most common cause: PATH wasn''t refreshed. Close all PowerShell windows,' -ForegroundColor Yellow
    Write-Host 'open a NEW one as administrator, and re-run the one-liner.' -ForegroundColor Yellow
    Read-Host 'Press Enter to exit'; return
}
$pyVersion = (& $python --version 2>&1) -replace '\r?\n',''
Write-Host ("Python: $pyVersion ($python)") -ForegroundColor Green

# ── Install location ─────────────────────────────────────────────────────────
# If a checkout already exists somewhere else (e.g. the auto-update task is
# running from inside the install dir itself), prefer the script's own path.
if ((Test-Path "$PSScriptRoot\.git") -and (Test-Path "$PSScriptRoot\app.py")) {
    $path = $PSScriptRoot
} else {
    $path = $DEFAULT_PATH
}
if ((Test-Path $path) -and -not (Test-Path "$path\.git")) {
    Write-Host ''
    Write-Host "Folder $path exists but is not a git checkout." -ForegroundColor Yellow
    if ($AUTO) { Write-Host 'Auto mode: aborting to avoid data loss.' -ForegroundColor Red; return }
    $r = Read-Host 'Delete and re-clone? (y/N)'
    if ($r -ne 'y') { Write-Host 'Aborted.'; return }
    Remove-Item -Recurse -Force $path
}

# ── Clone or update ──────────────────────────────────────────────────────────
$isUpdate = Test-Path "$path\.git"
if ($isUpdate) {
    Write-Section 'Updating existing installation'
    Stop-RunningApp $path
    Push-Location $path
    # Drop any embedded token in the remote URL from earlier private-repo attempts
    git remote set-url origin $REPO_URL
    git fetch origin
    $incoming = (git log --oneline HEAD..origin/main) | Out-String
    if ($incoming.Trim()) {
        Write-Host 'Incoming changes:' -ForegroundColor Cyan
        Write-Host $incoming.Trim()
    } else {
        Write-Host 'Already up to date.' -ForegroundColor DarkGray
    }
    git pull --ff-only | Out-Null
    Pop-Location
} else {
    Write-Section 'Fresh installation'
    Write-Host "Cloning $REPO_URL -> $path..." -ForegroundColor Cyan
    git clone $REPO_URL $path
}

# ── Install / upgrade Flask ──────────────────────────────────────────────────
Write-Host ''
Write-Host 'Installing Python dependencies...' -ForegroundColor Cyan
Push-Location $path
& $python -m pip install -r requirements.txt --quiet --upgrade
Pop-Location

# ── Remember the real Python path so start.bat doesn't have to detect it ────
Set-Content -Path "$path\.python_path" -Value $python -Encoding ASCII -NoNewline

# ── Task Scheduler auto-start (only prompt if not already set up) ───────────
$existingTask = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if (-not $existingTask -and -not $AUTO) {
    Write-Host ''
    $r = Read-Host 'Set up auto-start at login? (Y/n)'
    if ($r -ne 'n') {
        $action    = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$path\start.bat`""
        $trigger   = New-ScheduledTaskTrigger -AtLogOn
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
        $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Write-Host "Scheduled task '$TASK_NAME' created. Starts at next login." -ForegroundColor Green
    }
}

# ── Optional: daily auto-update from GitHub ─────────────────────────────────
$AUTO_UPDATE_TASK = 'HyperVMonitorAutoUpdate'
$existingAuto = Get-ScheduledTask -TaskName $AUTO_UPDATE_TASK -ErrorAction SilentlyContinue
if (-not $existingAuto -and -not $AUTO) {
    Write-Host ''
    Write-Host 'Auto-update will check GitHub once a day at 3:30 AM and reapply if'
    Write-Host 'changes are available. Runs the same deploy.ps1 you just used.'
    $r = Read-Host 'Enable daily auto-update at 3:30 AM? (Y/n)'
    if ($r -ne 'n') {
        $autoCmd  = "`$env:HVM_AUTO=1; iex (irm $RAW_SCRIPT)"
        $autoArg  = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$autoCmd`""
        $autoAction    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $autoArg
        $autoTrigger   = New-ScheduledTaskTrigger -Daily -At 3:30am
        $autoPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $autoSettings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
        Register-ScheduledTask -TaskName $AUTO_UPDATE_TASK -Action $autoAction -Trigger $autoTrigger -Principal $autoPrincipal -Settings $autoSettings -Force | Out-Null
        Write-Host "Scheduled task '$AUTO_UPDATE_TASK' created. Runs daily at 3:30 AM as SYSTEM." -ForegroundColor Green
        Write-Host "To disable later: Unregister-ScheduledTask -TaskName '$AUTO_UPDATE_TASK' -Confirm:`$false" -ForegroundColor DarkGray
    }
}

Write-Section 'Done'
Write-Host "Installed at:  $path"
Write-Host "Dashboard URL: http://127.0.0.1:5000"
Write-Host ""
Write-Host "Re-run anytime to update:" -ForegroundColor DarkGray
Write-Host "  iex (irm $RAW_SCRIPT)" -ForegroundColor DarkGray
Write-Host ""

# ── Launch ───────────────────────────────────────────────────────────────────
Write-Host 'Starting Hyper-V Monitor...' -ForegroundColor Cyan
Start-Process -FilePath "$path\start.bat" -WorkingDirectory $path
Start-Sleep -Seconds 4

# Only open the browser when run interactively. The web-UI Apply Update and
# the nightly auto-update task don't need a new browser window.
if (-not $AUTO) {
    Start-Process 'http://127.0.0.1:5000'
}

Write-Host ''
Write-Host 'Dashboard ready.' -ForegroundColor Green
