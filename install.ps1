# Hyper-V Monitor — One-line installer
#
# PUBLIC repo usage:
#   iex (irm https://raw.githubusercontent.com/mtg2342/hypervmonitor/main/install.ps1)
#
# PRIVATE repo usage (with a fine-scoped read-only GitHub PAT):
#   $env:GH_TOKEN='github_pat_xxxxxxxx'
#   iex (irm -Headers @{Authorization="Bearer $env:GH_TOKEN"} https://raw.githubusercontent.com/mtg2342/hypervmonitor/main/install.ps1)
#
# The token (if provided) is used for git clone and is stored only in the
# clone's .git/config so update.bat can pull subsequent updates without
# re-supplying it. The token is NEVER written to any tracked file.

$ErrorActionPreference = 'Stop'
$REPO_OWNER     = 'mtg2342'
$REPO_NAME      = 'hypervmonitor'
$REPO_URL       = "https://github.com/$REPO_OWNER/$REPO_NAME.git"
$RAW_INSTALLER  = "https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/install.ps1"
$DEFAULT_PATH   = 'C:\hypervmonitor'
$TOKEN          = $env:GH_TOKEN   # empty if public repo

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

# Detects whether `python` actually runs (not the Microsoft Store stub that
# only opens a Store page and returns "Python was not found...").
function Test-PythonWorks {
    if (-not (Test-Command python)) { return $false }
    try {
        $out = & python --version 2>&1 | Out-String
    } catch { return $false }
    if ($LASTEXITCODE -ne 0)        { return $false }
    if ($out -match 'was not found') { return $false }   # MS Store stub message
    if ($out -match '^\s*Python \d') { return $true }
    return $false
}

# Returns $true if anonymous (no-auth) access to the repo works — i.e. it's public.
function Test-RepoPublic {
    try {
        $r = Invoke-WebRequest -Uri $RAW_INSTALLER -Method Head -UseBasicParsing `
                               -ErrorAction Stop -TimeoutSec 10
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

# ── Self-elevate, preserving the token across the UAC boundary ───────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'Administrator privileges required. Relaunching elevated...' -ForegroundColor Yellow
    if ($TOKEN) {
        $cmd = "`$env:GH_TOKEN='$TOKEN'; iex (irm -Headers @{Authorization=\`"Bearer `$env:GH_TOKEN\`"} $RAW_INSTALLER)"
    } else {
        $cmd = "iex (irm $RAW_INSTALLER)"
    }
    Start-Process powershell -Verb RunAs -ArgumentList "-NoExit -NoProfile -ExecutionPolicy Bypass -Command `"$cmd`""
    return
}

Write-Section 'Hyper-V Monitor — Installer'

# If the repo is publicly accessible, ignore any leftover GH_TOKEN env var so we
# don't embed a stale (potentially revoked) token in the clone's .git/config.
if (Test-RepoPublic) {
    if ($TOKEN) {
        Write-Host 'Repo is public — ignoring GH_TOKEN and using anonymous access.' -ForegroundColor DarkGray
        $TOKEN = $null
        Remove-Item Env:\GH_TOKEN -ErrorAction SilentlyContinue
    } else {
        Write-Host 'Auth:   none (public-repo mode)' -ForegroundColor DarkGray
    }
} elseif ($TOKEN) {
    $masked = $TOKEN.Substring(0, [Math]::Min(12, $TOKEN.Length)) + '…(redacted)'
    Write-Host "Auth:   using GH_TOKEN ($masked)" -ForegroundColor DarkGray
} else {
    Write-Host 'Repo is not publicly accessible and no GH_TOKEN is set.' -ForegroundColor Red
    Write-Host 'Either make the repo public, or set $env:GH_TOKEN before running this script.' -ForegroundColor Yellow
    Read-Host 'Press Enter to exit'; return
}

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
        Write-Host 'Install from https://git-scm.com/download/win and re-run.' -ForegroundColor Yellow
        Read-Host 'Press Enter to exit'; return
    }
    if (-not (Test-Command git)) {
        Write-Host 'Git still not on PATH. Open a new PowerShell window and re-run.' -ForegroundColor Red
        Read-Host 'Press Enter to exit'; return
    }
}
Write-Host ('Git:    ' + (git --version)) -ForegroundColor Green

# ── Prereq: Python ───────────────────────────────────────────────────────────
# Note: a `python` on PATH may be the Windows App Execution Alias stub that only
# opens the Microsoft Store. Test-PythonWorks distinguishes that from a real install.
if (-not (Test-PythonWorks)) {
    if (Test-Command python) {
        Write-Host 'Detected the Windows "App Execution Alias" stub for python (it opens the Microsoft Store).' -ForegroundColor Yellow
        Write-Host 'Will install real Python via winget. After install, you may need to disable the stub at:' -ForegroundColor Yellow
        Write-Host '  Settings -> Apps -> Advanced app settings -> App execution aliases  (turn off both python entries)' -ForegroundColor Yellow
    } else {
        Write-Host 'Python is not installed.' -ForegroundColor Red
    }
    if (Test-Command winget) {
        $r = Read-Host 'Install Python 3.12 via winget? (Y/n)'
        if ($r -ne 'n') {
            winget install --id Python.Python.3.12 -e --source winget --accept-source-agreements --accept-package-agreements
            Refresh-Path
        }
    } else {
        Write-Host "Install Python 3.10+ from https://python.org/downloads ('Add to PATH' checked) and re-run." -ForegroundColor Yellow
        Read-Host 'Press Enter to exit'; return
    }
    if (-not (Test-PythonWorks)) {
        Write-Host '' -ForegroundColor Red
        Write-Host 'Python is installed but still not callable as `python` from this shell.' -ForegroundColor Red
        Write-Host 'Most common cause: the Microsoft Store app-execution-alias is shadowing real Python.' -ForegroundColor Yellow
        Write-Host 'Fix: Settings -> Apps -> Advanced app settings -> App execution aliases -> turn OFF' -ForegroundColor Yellow
        Write-Host '     both "App Installer python.exe" and "App Installer python3.exe", then open a NEW' -ForegroundColor Yellow
        Write-Host '     PowerShell window and re-run the installer.' -ForegroundColor Yellow
        Read-Host 'Press Enter to exit'; return
    }
}
Write-Host ('Python: ' + ((& python --version 2>&1) -replace '\r?\n','')) -ForegroundColor Green

# ── Install location ─────────────────────────────────────────────────────────
Write-Host ''
$path = Read-Host "Install location [$DEFAULT_PATH]"
if ([string]::IsNullOrWhiteSpace($path)) { $path = $DEFAULT_PATH }
$path = $path.TrimEnd('\')

# Clone URL — embed token only if we have one, so update.bat's `git pull` reuses it
if ($TOKEN) {
    $cloneUrl = "https://x-access-token:$TOKEN@github.com/$REPO_OWNER/$REPO_NAME.git"
} else {
    $cloneUrl = $REPO_URL
}

# ── Clone or pull ────────────────────────────────────────────────────────────
if (Test-Path "$path\.git") {
    Write-Host ''
    Write-Host "Existing checkout at $path — pulling latest..." -ForegroundColor Yellow
    Push-Location $path
    # Update remote URL in case token changed
    git remote set-url origin $cloneUrl
    git pull --ff-only
    Pop-Location
} elseif (Test-Path $path) {
    Write-Host ''
    Write-Host "Folder exists at $path but is not a git checkout." -ForegroundColor Red
    $r = Read-Host 'Delete and re-clone? (y/N)'
    if ($r -ne 'y') { Write-Host 'Aborted.'; return }
    Remove-Item -Recurse -Force $path
    git clone $cloneUrl $path
} else {
    Write-Host ''
    Write-Host "Cloning to $path..." -ForegroundColor Cyan
    git clone $cloneUrl $path
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
if ($TOKEN) {
    Write-Host ""
    Write-Host "Note: the GitHub PAT is stored in $path\.git\config so update.bat" -ForegroundColor DarkGray
    Write-Host "      can pull without re-supplying it. Keep that file protected." -ForegroundColor DarkGray
}
Write-Host ""

# ── Optional: auto-start at login via Task Scheduler ─────────────────────────
$r = Read-Host 'Set up auto-start at login? (Y/n)'
if ($r -ne 'n') {
    $taskName  = 'HyperVMonitor'
    $action    = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$path\start.bat`""
    $trigger   = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "Scheduled task '$taskName' created. Starts at next login." -ForegroundColor Green
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
