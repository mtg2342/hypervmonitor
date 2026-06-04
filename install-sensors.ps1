# install-sensors.ps1
# Downloads and installs LibreHardwareMonitor (LHM) so the Hyper-V Monitor
# dashboard can read GPU and motherboard temperatures via its WMI provider.
#
# Triggered by the "Install LibreHardwareMonitor" button in Settings ->
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
# No third-party dependencies -- everything uses built-in PowerShell cmdlets.

$ErrorActionPreference = 'Continue'

$installPath = 'C:\Program Files\LibreHardwareMonitor'
$taskName    = 'LibreHardwareMonitorAutoStart'
$exe         = Join-Path $installPath 'LibreHardwareMonitor.exe'

function Log($msg) {
    Write-Host "[install-sensors] $msg"
}

Log "=== LibreHardwareMonitor installer starting ==="

# -- Step 0: .NET Desktop Runtime check --------------------------------------
# LHM 0.9+ is built against .NET 8 (Windows Forms -- needs the *Desktop*
# Runtime, not just the base .NET runtime). Older Windows Server installs
# almost never have this, so check first and install if missing -- otherwise
# launching LHM later just throws ".NET Runtime: You must install or update
# .NET to run this application."
function Test-DotNetDesktop8Plus {
    # Method A: dotnet --list-runtimes (cleanest when dotnet.exe is on PATH)
    try {
        $out = & dotnet --list-runtimes 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            foreach ($line in $out) {
                if ($line -match '^Microsoft\.WindowsDesktop\.App\s+([89]|1\d)\.') {
                    return $true
                }
            }
        }
    } catch {}
    # Method B: registry -- Desktop Runtime writes its version here
    foreach ($reg in @(
        'HKLM:\SOFTWARE\dotnet\Setup\InstalledVersions\x64\sharedfx\Microsoft.WindowsDesktop.App',
        'HKLM:\SOFTWARE\WOW6432Node\dotnet\Setup\InstalledVersions\x64\sharedfx\Microsoft.WindowsDesktop.App'
    )) {
        try {
            if (Test-Path $reg) {
                $values = (Get-ItemProperty $reg).PSObject.Properties.Name
                foreach ($v in $values) {
                    if ($v -match '^([89]|1\d)\.') { return $true }
                }
            }
        } catch {}
    }
    # Method C: filesystem -- shared host folder for installed Desktop Runtimes
    $sharedHost = 'C:\Program Files\dotnet\shared\Microsoft.WindowsDesktop.App'
    if (Test-Path $sharedHost) {
        foreach ($d in Get-ChildItem $sharedHost -Directory -ErrorAction SilentlyContinue) {
            if ($d.Name -match '^([89]|1\d)\.') { return $true }
        }
    }
    return $false
}

if (-not (Test-DotNetDesktop8Plus)) {
    Log ".NET 8+ Desktop Runtime not detected -- installing now."
    $installedDotNet = $false

    # Method A: winget (Windows Server 2022/2025 + Windows 11)
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Log "Trying winget install Microsoft.DotNet.DesktopRuntime.8 ..."
        try {
            $p = Start-Process winget -ArgumentList @(
                'install', '--id', 'Microsoft.DotNet.DesktopRuntime.8',
                '-e', '--silent',
                '--accept-source-agreements', '--accept-package-agreements'
            ) -Wait -PassThru -NoNewWindow
            if ($p.ExitCode -eq 0 -or $p.ExitCode -eq -1978335189) {
                $installedDotNet = $true
                Log "winget install succeeded."
            } else {
                Log ("winget exit code {0}, will try direct download." -f $p.ExitCode)
            }
        } catch {
            Log ("winget threw: {0}" -f $_.Exception.Message)
        }
    } else {
        Log "winget not present, will try direct download."
    }

    # Method B: download the official installer from Microsoft
    if (-not $installedDotNet) {
        # aka.ms link redirects to the latest .NET 8 Desktop Runtime build
        $dnUrl = 'https://aka.ms/dotnet/8.0/windowsdesktop-runtime-win-x64.exe'
        $dnExe = Join-Path $env:TEMP 'dotnet-desktop-runtime-installer.exe'
        $dnLog = Join-Path $env:TEMP 'dotnet-desktop-runtime-installer.log'
        try {
            Log "Downloading $dnUrl ..."
            Invoke-WebRequest -Uri $dnUrl -OutFile $dnExe -UseBasicParsing -TimeoutSec 300
            $sz = (Get-Item $dnExe).Length
            Log ("Downloaded {0:N1} MB, running silent install ..." -f ($sz / 1MB))
            # /log tells the bootstrapper to write a verbose log we can inspect
            # if the install fails -- far more useful than just an exit code.
            $p = Start-Process -FilePath $dnExe `
                -ArgumentList '/install', '/quiet', '/norestart', '/log', $dnLog `
                -Wait -PassThru
            Remove-Item $dnExe -Force -ErrorAction SilentlyContinue
            # Microsoft installer exit codes: 0 = success, 3010 = success+reboot
            if ($p.ExitCode -eq 0 -or $p.ExitCode -eq 3010) {
                $installedDotNet = $true
                Log ".NET Desktop Runtime installed via direct download."
                if ($p.ExitCode -eq 3010) {
                    Log "NOTE: installer signalled a reboot is desirable but not required for LHM."
                }
            } else {
                Log ("ERROR: .NET installer exit code {0}" -f $p.ExitCode)
                # Surface the bottom of the installer log so the user sees the
                # real failure reason instead of just an opaque exit code.
                if (Test-Path $dnLog) {
                    Log "Last 25 lines of .NET installer log:"
                    try {
                        Get-Content -Path $dnLog -Tail 25 -ErrorAction Stop |
                            ForEach-Object { Log ("  | {0}" -f $_) }
                    } catch {
                        Log ("  (couldn't read log: {0})" -f $_.Exception.Message)
                    }
                }
            }
        } catch {
            Log ("ERROR downloading/installing .NET: {0}" -f $_.Exception.Message)
        }
    }

    if (-not $installedDotNet) {
        Log "FATAL: could not install .NET 8 Desktop Runtime automatically."
        Log "  Please install it manually from:"
        Log "    https://dotnet.microsoft.com/en-us/download/dotnet/8.0"
        Log "  Then re-run this installer."
        exit 1
    }

    # Verify
    if (-not (Test-DotNetDesktop8Plus)) {
        Log "WARNING: .NET installer reported success but runtime still not detected."
        Log "  LHM may fail to launch. A reboot might be required."
    } else {
        Log ".NET 8 Desktop Runtime is now installed."
    }
} else {
    Log ".NET 8+ Desktop Runtime already present -- good."
}

# -- Step 1: already installed? -----------------------------------------------
if (Test-Path $exe) {
    Log "LHM already installed at $installPath, skipping download."
} else {
    # -- Step 2: query latest release -----------------------------------------
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

    # -- Step 3: download ----------------------------------------------------
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

    # -- Step 4: extract -----------------------------------------------------
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

# -- Step 4.5: enable LHM's WMI provider via config patching ----------------
# LHM stores its settings in a file it writes itself on graceful shutdown.
# The file name + location have varied between LHM versions, so we don't
# know a priori where it lives. Strategy:
#   1. Boot LHM cleanly so it writes its default config wherever it likes
#   2. Send it a close message and wait for the config to be flushed to disk
#   3. Search common locations for the config file
#   4. Patch mainForm.PluginWmiEnabled (and a few sibling keys) to true
#   5. Restart LHM -- it'll read the patched config and activate the WMI plugin
# This works for both old "PersistentSettings in install dir" and newer
# "settings in AppData" layouts.

function Find-LHMConfigFiles {
    $candidates = @(
        (Join-Path $installPath 'LibreHardwareMonitor.config'),
        (Join-Path $installPath 'LibreHardwareMonitor.exe.config'),
        (Join-Path $env:APPDATA      'LibreHardwareMonitor\LibreHardwareMonitor.config'),
        (Join-Path $env:LOCALAPPDATA 'LibreHardwareMonitor\LibreHardwareMonitor.config'),
        (Join-Path $env:USERPROFILE  '.LibreHardwareMonitor\LibreHardwareMonitor.config')
    )
    $found = @()
    foreach ($p in $candidates) {
        if (Test-Path $p) { $found += $p }
    }
    return ,$found  # comma keeps it a single-element array when only one match
}

function Patch-LHMConfig($cfgPath) {
    # Read existing XML, ensure /configuration/appSettings exists, then set
    # mainForm.PluginWmiEnabled=true and a handful of related keys. The list
    # is intentionally over-inclusive: LHM ignores keys it doesn't know.
    # NOTE: hashtable keys here are case-insensitive in PowerShell -- don't
    # add both "WmiProvider" and "wmiProvider", they collide.
    $desiredKeys = @{
        'mainForm.PluginWmiEnabled'           = 'true'
        'mainForm.WmiEnabled'                 = 'true'
        'WmiProvider'                         = 'true'
        'mainForm.MinimizeToTray'             = 'true'
        'mainForm.MinimizeOnClose'            = 'true'
        'mainForm.PluginCpuEnabled'           = 'true'
        'mainForm.PluginGpuEnabled'           = 'true'
        'mainForm.PluginMotherboardEnabled'   = 'true'
        'mainForm.PluginStorageEnabled'       = 'true'
        'mainForm.PluginNetworkEnabled'       = 'true'
    }
    try {
        $doc = New-Object System.Xml.XmlDocument
        if (Test-Path $cfgPath) {
            $doc.Load($cfgPath)
        }
        if (-not $doc.DocumentElement) {
            $decl = $doc.CreateXmlDeclaration('1.0', 'utf-8', $null)
            $doc.AppendChild($decl) | Out-Null
            $root = $doc.CreateElement('configuration')
            $doc.AppendChild($root) | Out-Null
        }
        $root = $doc.SelectSingleNode('/configuration')
        if (-not $root) {
            $root = $doc.CreateElement('configuration')
            $doc.AppendChild($root) | Out-Null
        }
        $appSettings = $root.SelectSingleNode('appSettings')
        if (-not $appSettings) {
            $appSettings = $doc.CreateElement('appSettings')
            $root.AppendChild($appSettings) | Out-Null
        }
        foreach ($key in $desiredKeys.Keys) {
            $existing = $appSettings.SelectSingleNode("add[@key='$key']")
            if ($existing) {
                $existing.SetAttribute('value', $desiredKeys[$key])
            } else {
                $node = $doc.CreateElement('add')
                $node.SetAttribute('key', $key)
                $node.SetAttribute('value', $desiredKeys[$key])
                $appSettings.AppendChild($node) | Out-Null
            }
        }
        $doc.Save($cfgPath)
        return $true
    } catch {
        Log ("  WARNING: couldn't patch {0}: {1}" -f $cfgPath, $_.Exception.Message)
        return $false
    }
}

# Phase A: kill any running LHM and boot a fresh instance briefly so it
# writes its default config in whatever location this version uses.
Log "Configuring LHM WMI: phase A (let LHM write its default config) ..."
try {
    Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Start-Process -FilePath $exe -ArgumentList '/MinTrayIcon' -WindowStyle Hidden
    Start-Sleep -Seconds 6
    # Ask LHM to close gracefully so it flushes config to disk. CloseMainWindow
    # is cleaner than Stop-Process and gives LHM a chance to save settings.
    $procs = Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        try { $p.CloseMainWindow() | Out-Null } catch {}
    }
    Start-Sleep -Seconds 3
    # Belt and braces: force-kill anything still hanging around
    Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
} catch {
    Log ("  WARNING in phase A: {0}" -f $_.Exception.Message)
}

# Phase B: locate the config file LHM actually wrote, and patch it
Log "Configuring LHM WMI: phase B (patch config) ..."
$cfgFiles = Find-LHMConfigFiles
if ($cfgFiles.Count -eq 0) {
    Log "  No existing LHM config found -- writing a fresh one in the install dir."
    $cfgFiles = @( (Join-Path $installPath 'LibreHardwareMonitor.config') )
}
foreach ($cfg in $cfgFiles) {
    if (Patch-LHMConfig $cfg) {
        Log ("  Patched {0}" -f $cfg)
    }
}

# -- Step 5: scheduled task for auto-start at logon --------------------------
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

# -- Step 6: start LHM now so its WMI provider is live -----------------------
Log "Starting LibreHardwareMonitor ..."
$lhmAlive = $false
try {
    Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Start-Process -FilePath $exe -ArgumentList '/MinTrayIcon' -WindowStyle Hidden
    # Wait long enough for the .NET runtime to load and (if it's going to)
    # die on a missing-runtime error. 4 seconds is enough in practice.
    Start-Sleep -Seconds 4

    # Verify the process is still alive -- this catches the silent-death case
    # where .NET wasn't actually installed despite our checks above.
    $proc = Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue
    if ($proc) {
        $lhmAlive = $true
        Log ("LHM is running (PID {0})." -f $proc.Id)
    } else {
        Log "ERROR: LHM exited immediately after launch."
        Log "  This almost always means the .NET 8 Desktop Runtime is still missing."
        Log "  To install manually, run as Administrator:"
        Log "    `$exe = `"`$env:TEMP\dotnet8-desktop.exe`""
        Log "    Invoke-WebRequest 'https://aka.ms/dotnet/8.0/windowsdesktop-runtime-win-x64.exe' -OutFile `$exe"
        Log "    Start-Process `$exe -ArgumentList '/install','/quiet','/norestart' -Wait"
        Log "  Then click 'Start LHM' in the dashboard."
    }
} catch {
    Log ("ERROR launching LHM: {0}" -f $_.Exception.Message)
}

if (-not $lhmAlive) {
    Log "=== Done with errors. ==="
    exit 1
}

# -- Step 7: verify WMI namespace is now live --------------------------------
# This is the actual success criterion -- LHM running doesn't guarantee the
# WMI provider plugin loaded. Confirm the namespace responds with sensors.
Log "Verifying LHM WMI namespace ..."
Start-Sleep -Seconds 4
$wmiOk = $false
try {
    $probe = Get-CimInstance -Namespace 'root\LibreHardwareMonitor' `
        -ClassName Sensor -ErrorAction Stop | Select-Object -First 1
    if ($probe) {
        $wmiOk = $true
        $count = (Get-CimInstance -Namespace 'root\LibreHardwareMonitor' `
            -ClassName Sensor -ErrorAction SilentlyContinue | Measure-Object).Count
        Log ("LHM WMI namespace is live with {0} sensor(s)." -f $count)
    }
} catch {
    Log ("  WMI probe error: {0}" -f $_.Exception.Message)
}

if ($wmiOk) {
    Log "=== Done. ==="
    Log "Sensors should appear in the dashboard within ~30 seconds."
    exit 0
}

# -- Manual fallback message -------------------------------------------------
# Auto-patch failed for this LHM version. Walk the user through the one-click
# manual enable.
Log ""
Log "============================================================="
Log "  LHM is running but its WMI provider is still OFF."
Log ""
Log "  This version of LHM stores the WMI setting somewhere our"
Log "  config patcher didn't reach. Please enable it manually -- it"
Log "  takes 5 seconds and only has to be done ONCE:"
Log ""
Log "    1. In the Windows system tray (bottom-right), click the ^"
Log "       arrow to show hidden icons."
Log "    2. Find the LibreHardwareMonitor icon (small green chip)."
Log "    3. Right-click it -> Options -> click WMI so it gets a tick."
Log "    4. Click Refresh in the dashboard Sensor Sources panel."
Log ""
Log "  LHM remembers this across restarts, so the auto-start task"
Log "  will keep it on from now on."
Log "============================================================="
# Exit 0 because everything that WE control succeeded -- the install + launch
# are fine, only the user-side tray-icon click remains.
exit 0
