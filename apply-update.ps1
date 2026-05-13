# apply-update.ps1
# Triggered by Task Scheduler (HyperVMonitorApplyUpdate task) when the user
# clicks "Apply Update" in Settings, and by the nightly auto-update task.
#
# This script must run completely independently of the Flask app process so
# it can stop and restart that process cleanly. Task Scheduler launches us
# as SYSTEM, which gives us full process isolation from the Python parent.
#
# Self-contained — no network dependency on raw.githubusercontent.com beyond
# what `git pull` itself does. Survives apply-update.ps1 being updated by the
# pull, because PowerShell loads the entire script into memory at start.

$ErrorActionPreference = 'Continue'

$installPath = $PSScriptRoot
$logFile     = Join-Path $installPath 'apply-update.log'

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Host $line
    try { Add-Content -Path $logFile -Value $line -ErrorAction Stop } catch {}
}

# Trim log if it grows past ~256KB so it can't fill the disk
try {
    if ((Test-Path $logFile) -and (Get-Item $logFile).Length -gt 262144) {
        Set-Content -Path $logFile -Value '' -ErrorAction SilentlyContinue
    }
} catch {}

Log ''
Log '============================================'
Log " Apply Update starting"
Log " Install path: $installPath"
Log '============================================'

if (-not (Test-Path "$installPath\app.py")) {
    Log "ERROR: app.py not found at $installPath. Aborting."
    exit 1
}

# ── Prevent two simultaneous apply-update runs ───────────────────────────────
$lockFile = Join-Path $installPath '.apply-update.lock'
if (Test-Path $lockFile) {
    $lockAge = (Get-Date) - (Get-Item $lockFile).LastWriteTime
    if ($lockAge.TotalMinutes -lt 5) {
        Log "Another apply-update.ps1 appears to be running (lock < 5 min old). Aborting this run."
        exit 0
    }
    Log "Stale lock file found (age $([int]$lockAge.TotalMinutes) min). Overwriting."
}
try { Set-Content -Path $lockFile -Value "$PID" -ErrorAction Stop } catch {}

try {

# ── Stop any running Hyper-V Monitor python processes ────────────────────────
Log 'Stopping any running Hyper-V Monitor python processes...'
$stoppedPy = 0
try {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$installPath*" } |
        ForEach-Object {
            Log "  stopping python.exe PID $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $stoppedPy++
        }
} catch {
    Log "  WARN: $_"
}
Log "Stopped $stoppedPy python process(es)."

# ── Stop cmd.exe consoles that were hosting start.bat ────────────────────────
# These windows sit at the `pause` line after python is killed and would
# otherwise accumulate every time an update runs. Filter on command lines
# that reference either start.bat or the install path.
Log 'Stopping cmd.exe consoles hosting start.bat...'
$stoppedCmd = 0
try {
    Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -like "*start.bat*" -or
                $_.CommandLine -like "*$installPath*"
            )
        } |
        ForEach-Object {
            Log "  stopping cmd.exe PID $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $stoppedCmd++
        }
} catch {
    Log "  WARN: $_"
}
Log "Stopped $stoppedCmd cmd.exe console(s)."

# ── Stop any conhost.exe sessions orphaned by the killed cmd.exe ────────────
# These are the actual console window hosts. Killing cmd.exe usually closes
# them, but stale ones occasionally linger.
try {
    Get-CimInstance Win32_Process -Filter "Name='conhost.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*start.bat*" } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
} catch {}

# Give SQLite a moment to flush and the OS to release file handles
Start-Sleep -Seconds 2

# ── git pull ────────────────────────────────────────────────────────────────
Log 'Fetching latest from GitHub...'
Push-Location $installPath
try {
    & git fetch origin 2>&1 | ForEach-Object { Log "  fetch: $_" }
    $pullOut = & git pull --ff-only 2>&1
    foreach ($line in $pullOut) { Log "  pull: $line" }
} catch {
    Log "ERROR during git pull: $_"
} finally {
    Pop-Location
}

# ── Update Python deps if requirements.txt changed ───────────────────────────
$python = $null
if (Test-Path "$installPath\.python_path") {
    $python = (Get-Content "$installPath\.python_path" -Raw -ErrorAction SilentlyContinue).Trim()
}
if (-not $python -or -not (Test-Path $python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $python = 'py' }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $python = 'python' }
}

if ($python) {
    Log "Installing Python dependencies using: $python"
    try {
        & $python -m pip install -r "$installPath\requirements.txt" --quiet --upgrade 2>&1 |
            ForEach-Object { if ($_) { Log "  pip: $_" } }
    } catch {
        Log "  WARN: pip install failed: $_"
    }
} else {
    Log 'WARNING: No python found, skipping dep update'
}

# ── Restart the app ──────────────────────────────────────────────────────────
Log 'Restarting Hyper-V Monitor...'
try {
    Start-Process -FilePath "$installPath\start.bat" -WorkingDirectory $installPath -WindowStyle Hidden
    Log 'start.bat launched.'
} catch {
    Log "ERROR launching start.bat: $_"
}

Log '============================================'
Log ' Apply Update complete'
Log '============================================'

} finally {
    # Always release the lock so future runs aren't blocked
    if (Test-Path $lockFile) {
        try { Remove-Item $lockFile -Force -ErrorAction Stop } catch {}
    }
}
