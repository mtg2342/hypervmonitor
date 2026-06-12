# install-sensors.ps1 -- one-click temperature sensor support
#
# Installs LibreHardwareMonitor (LHM) so the dashboard can read CPU / GPU /
# motherboard temperatures. Two things matter here:
#
#   1. We install the .NET FRAMEWORK (net472) build of LHM -- NOT the
#      .NET 8/10 build. The net472 build runs on the .NET Framework 4.8
#      that ships inside Windows 10/11 and Server 2019+, so there is no
#      runtime to download, and PowerShell 5.1 can load
#      LibreHardwareMonitorLib.dll directly.
#
#   2. The dashboard's collector reads sensors by loading that DLL in its
#      own PowerShell process (see collector.py). It does NOT depend on the
#      LHM GUI running, the tray icon, or the WMI provider plugin being
#      enabled. The GUI + WMI bits below are still set up because they're
#      nice to have, but they are no longer required for temperatures.
#
# Run as Administrator. Safe to re-run; it self-heals a wrong-framework
# install by replacing it with the net472 build.

$ErrorActionPreference = 'Continue'

$installPath = 'C:\Program Files\LibreHardwareMonitor'
$taskName    = 'LibreHardwareMonitorAutoStart'
$exe         = Join-Path $installPath 'LibreHardwareMonitor.exe'
$libDll      = Join-Path $installPath 'LibreHardwareMonitorLib.dll'

function Log($msg) {
    Write-Host "[install-sensors] $msg"
}

Log "=== LibreHardwareMonitor installer starting ==="

# -- Step 0: .NET Framework 4.7.2+ check --------------------------------------
# The net472 build needs .NET Framework 4.7.2 or later. Every supported
# Windows (10 1803+, 11, Server 2019+) ships 4.7.2+ built in, so this is a
# warn-only sanity check, not an installer.
try {
    $rel = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -ErrorAction Stop).Release
    if ($rel -ge 461808) {
        Log ".NET Framework 4.7.2+ present (release $rel) -- good."
    } else {
        Log "WARNING: .NET Framework looks older than 4.7.2 (release $rel)."
        Log "  LHM may not start. Install .NET Framework 4.8 from Windows Update."
    }
} catch {
    Log "WARNING: couldn't read .NET Framework version from the registry; continuing."
}

# -- Step 1: do we need to (re)install? ----------------------------------------
# Reinstall when:
#   - LHM isn't there at all
#   - LibreHardwareMonitorLib.dll is missing (partial/old install)
#   - a *.runtimeconfig.json exists  ->  that's the .NET 8/10 build, which
#     PowerShell 5.1 cannot load and which needs a runtime Windows doesn't
#     ship. Replace it with the net472 build.
$needInstall = $false
if (-not (Test-Path $exe)) {
    $needInstall = $true
    Log "LHM not installed yet."
} elseif (-not (Test-Path $libDll)) {
    $needInstall = $true
    Log "LHM install is incomplete (no LibreHardwareMonitorLib.dll) -- reinstalling."
} elseif (Get-ChildItem $installPath -Filter '*.runtimeconfig.json' -ErrorAction SilentlyContinue) {
    $needInstall = $true
    Log "Installed copy is the .NET 8/10 build -- replacing with the net472 build"
    Log "(runs on Windows' built-in .NET Framework, loadable by PowerShell)."
}

if ($needInstall) {
    # -- Step 2: query latest release ------------------------------------------
    Log "Fetching latest release info from GitHub..."
    try {
        $headers = @{ 'User-Agent' = 'HyperVMonitor-installer' }
        $release = Invoke-RestMethod `
            -Uri 'https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest' `
            -UseBasicParsing -Headers $headers -TimeoutSec 30
        $zips = @($release.assets | Where-Object {
            $_.name -match '\.zip$' -and $_.name -notmatch '(?i)source'
        })
        # Prefer the plain "LibreHardwareMonitor.zip" -- that's the net472
        # build. The modern-.NET build is named like
        # "LibreHardwareMonitor.NET.10.zip"; avoid it.
        $asset = $zips | Where-Object { $_.name -eq 'LibreHardwareMonitor.zip' } | Select-Object -First 1
        if (-not $asset) {
            $asset = $zips | Where-Object { $_.name -notmatch '(?i)\bnet\b|\.net\.?\d' } | Select-Object -First 1
        }
        if (-not $asset) { $asset = $zips | Select-Object -First 1 }
        if (-not $asset) { throw 'No suitable .zip asset found in the latest release' }
        Log ("Latest release: {0} ({1})" -f $release.tag_name, $asset.name)
    } catch {
        Log ("ERROR querying GitHub: {0}" -f $_.Exception.Message)
        exit 1
    }

    # -- Step 3: download -------------------------------------------------------
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

    # -- Step 4: extract --------------------------------------------------------
    Log "Extracting to $installPath ..."
    try {
        # Stop any running copy first so files aren't locked, and clear out
        # the old build so .NET-8 leftovers can't confuse anything.
        Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        if (Test-Path $installPath) {
            Remove-Item (Join-Path $installPath '*') -Recurse -Force -ErrorAction SilentlyContinue
        } else {
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
} else {
    Log "LHM net472 build already installed at $installPath."
}

# -- Step 4.5: enable LHM's WMI provider via config patching --------------------
# Optional nicety: with WMI on, other tools can read LHM sensors too, and the
# dashboard's WMI source picks them up without loading the DLL. The dashboard
# no longer REQUIRES this -- the collector loads LibreHardwareMonitorLib.dll
# directly -- so failures here are non-fatal.
#
# LHM stores its settings in a file it writes on graceful shutdown; the
# location has varied between versions. Strategy:
#   Phase A: boot LHM briefly so it writes its default config wherever it likes
#   Phase B: find + patch every config file, setting the WMI keys to true

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
    # the WMI keys plus a handful of related ones. The list is intentionally
    # over-inclusive: LHM ignores keys it doesn't know.
    # NOTE: hashtable keys are case-insensitive in PowerShell -- don't add
    # both "WmiProvider" and "wmiProvider", they collide.
    $desiredKeys = @{
        'mainForm.PluginWmiEnabled'           = 'true'
        'mainForm.WmiEnabled'                 = 'true'
        'WmiProvider'                         = 'true'
        'wmiProviderMenuItem'                 = 'true'
        'mainForm.MinimizeToTray'             = 'true'
        'mainForm.MinimizeOnClose'            = 'true'
        'minTrayMenuItem'                     = 'true'
        'minCloseMenuItem'                    = 'true'
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

# -- Step 5: scheduled task for auto-start at logon -----------------------------
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

# -- Step 6: start the LHM GUI (tray icon) --------------------------------------
# Nice to have, but NOT required for dashboard temperatures any more.
Log "Starting LibreHardwareMonitor (tray icon) ..."
try {
    Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Start-Process -FilePath $exe -ArgumentList '/MinTrayIcon' -WindowStyle Hidden
    Start-Sleep -Seconds 4
    $proc = Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue
    if ($proc) {
        Log ("LHM is running (PID {0})." -f $proc.Id)
    } else {
        Log "WARNING: the LHM GUI exited after launch. Dashboard temperatures"
        Log "  do NOT depend on it (the collector reads the sensor DLL directly),"
        Log "  but the tray icon won't be available."
    }
} catch {
    Log ("WARNING launching LHM GUI: {0}" -f $_.Exception.Message)
}

# -- Step 7: the success criterion -- can WE load the sensor DLL? ---------------
# This is exactly what the dashboard's collector does on every poll, so if it
# works here, temperatures will flow. Run in-process (this script runs under
# the same PowerShell the collector uses).
Log "Verifying sensor DLL loads in PowerShell ..."
$dllOk = $false
try {
    $asm = [System.Reflection.Assembly]::LoadFrom($libDll)
    $compType = $asm.GetType('LibreHardwareMonitor.Hardware.Computer')
    if ($compType) {
        $dllOk = $true
        Log ("LibreHardwareMonitorLib {0} loaded OK." -f $asm.GetName().Version)
    } else {
        Log "ERROR: DLL loaded but Computer type not found (unexpected layout)."
    }
} catch {
    Log ("ERROR: couldn't load LibreHardwareMonitorLib.dll: {0}" -f $_.Exception.Message)
    Log "  If this says it cannot load the assembly, the installed build may be"
    Log "  the .NET 8/10 one -- re-run this installer to replace it."
}

if ($dllOk) {
    Log "=== Done. ==="
    Log "Temperatures should appear in the dashboard within ~30 seconds"
    Log "(next collector poll). No tray icon or WMI setup is required."
    exit 0
} else {
    Log "=== Done with errors -- sensor DLL is not loadable. ==="
    exit 1
}
