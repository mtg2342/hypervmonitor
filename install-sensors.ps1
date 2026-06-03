# install-sensors.ps1
# Downloads and installs LibreHardwareMonitor (LHM) so the Hyper-V Monitor
# dashboard can read GPU and motherboard temperatures via its WMI provider.
#
# Triggered by the "Install LibreHardwareMonitor" button in Settings →
# Sensor Sources, or runnable manually from a PowerShell admin prompt.
#
# What this does:
#   1. Skips if LHM is already installed at C:\Program Files\LibreHardwareMonitor
#   2. Calls the GitHub Releases API for the newest LHM release ZIP
#   3. Downloads and extracts to Program Files
#   4. Registers a "LibreHardwareMonitorAutoStart" scheduled task that
#      launches it at logon, minimized to the tray
#   5. Launches it now so the WMI namespace gets populated immediately
#
# No third-party dependencies — everything uses built-in PowerShell cmdlets.

$ErrorActionPreference = 'Continue'

$installPath = 'C:\Program Files\LibreHardwareMonitor'
$taskName    = 'LibreHardwareMonitorAutoStart'
$exe         = Join-Path $installPath 'LibreHardwareMonitor.exe'

function Log($msg) {
    Write-Host "[install-sensors] $msg"
}

Log "=== LibreHardwareMonitor installer starting ==="

# ── Step 1: already installed? ───────────────────────────────────────────────
if (Test-Path $exe) {
    Log "LHM already installed at $installPath, skipping download."
} else {
    # ── Step 2: query latest release ─────────────────────────────────────────
    Log "Fetching latest release info from GitHub..."
    try {
        $headers = @{ 'User-Agent' = 'HyperVMonitor-installer' }
        $release = Invoke-RestMethod `
            -Uri 'https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest' `
            -UseBasicParsing -Headers $headers -TimeoutSec 30
        $asset = $release.assets |
            Where-Object { $_.name -match '\.zip$' -and $_.name -notmatch 'source' } |
            Select-Object -First 1
        if (-not $asset) { throw 'No suitable .zip asset found in the latest release' }
        Log ("Latest release: {0} ({1})" -f $release.tag_name, $asset.name)
    } catch {
        Log ("ERROR querying GitHub: {0}" -f $_.Exception.Message)
        exit 1
    }

    # ── Step 3: download ────────────────────────────────────────────────────
    $tempZip = Join-Path $env:TEMP 'LibreHardwareMonitor-install.zip'
    Log "Downloading $($asset.browser_download_url) ..."
    try {
        if (Test-Path $tempZip) { Remove-Item $tempZip -Force }
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tempZip `
            -UseBasicParsing -TimeoutSec 120 -Headers $headers
        $sz = (Get-Item $tempZip).Length
        Log ("Downloaded {0:N1} MB" -f ($sz / 1MB))
    } catch {
        Log ("ERROR downloading: {0}" -f $_.Exception.Message)
        exit 1
    }

    # ── Step 4: extract ─────────────────────────────────────────────────────
    Log "Extracting to $installPath ..."
    try {
        if (-not (Test-Path $installPath)) {
            New-Item -Path $installPath -ItemType Directory -Force | Out-Null
        }
        Expand-Archive -Path $tempZip -DestinationPath $installPath -Force
        Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
        Log "Extracted successfully."
    } catch {
        Log ("ERROR extracting: {0}" -f $_.Exception.Message)
        exit 1
    }

    if (-not (Test-Path $exe)) {
        Log "ERROR: LibreHardwareMonitor.exe not found at $exe after extracting."
        Log "The release zip may have changed its layout. Check the archive contents."
        exit 1
    }
}

# ── Step 5: scheduled task for auto-start at logon ──────────────────────────
Log "Registering auto-start scheduled task '$taskName' ..."
try {
    $action = New-ScheduledTaskAction -Execute $exe -Argument '/MinTrayIcon'
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $action `
        -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Log "Scheduled task created."
} catch {
    Log ("WARNING: could not register scheduled task: {0}" -f $_.Exception.Message)
}

# ── Step 6: start LHM now so its WMI provider is live ───────────────────────
Log "Starting LibreHardwareMonitor ..."
try {
    Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Start-Process -FilePath $exe -ArgumentList '/MinTrayIcon' -WindowStyle Hidden
    Start-Sleep -Seconds 3
    Log "LHM is running."
} catch {
    Log ("ERROR launching LHM: {0}" -f $_.Exception.Message)
    exit 1
}

Log "=== Done. ==="
Log "Sensors should appear in the dashboard within ~30 seconds."
Log "If GPU/motherboard temps still don't appear, right-click the LHM tray icon"
Log "and ensure Options -> WMI is ticked."
exit 0
