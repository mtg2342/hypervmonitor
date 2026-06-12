# Quick standalone test of the DLL-direct LHM sensor read.
# Usage: powershell -ExecutionPolicy Bypass -File test-lhmlib.ps1 [-LhmDir <path>]
param([string]$LhmDir = 'C:\Program Files\LibreHardwareMonitor')

$dll = Join-Path $LhmDir 'LibreHardwareMonitorLib.dll'
if (-not (Test-Path $dll)) { Write-Host "DLL not found: $dll"; exit 1 }

$asm = [System.Reflection.Assembly]::LoadFrom($dll)
Write-Host "Loaded $($asm.GetName().Name) $($asm.GetName().Version) ($($asm.ImageRuntimeVersion))"

$compType = $asm.GetType('LibreHardwareMonitor.Hardware.Computer')
$comp = [Activator]::CreateInstance($compType)
$comp.IsCpuEnabled         = $true
$comp.IsGpuEnabled         = $true
$comp.IsMotherboardEnabled = $true
$comp.IsStorageEnabled     = $true
$comp.Open()

$found = 0
foreach ($hw in $comp.Hardware) {
    $hw.Update()
    Write-Host ("HW: {0} - {1}" -f $hw.HardwareType, $hw.Name)
    foreach ($s in $hw.Sensors) {
        if ("$($s.SensorType)" -eq 'Temperature' -and $null -ne $s.Value) {
            Write-Host ("  TEMP: {0} = {1}" -f $s.Name, $s.Value); $found++
        }
    }
    foreach ($sub in $hw.SubHardware) {
        $sub.Update()
        foreach ($s in $sub.Sensors) {
            if ("$($s.SensorType)" -eq 'Temperature' -and $null -ne $s.Value) {
                Write-Host ("  TEMP(sub): {0}/{1} = {2}" -f $sub.Name, $s.Name, $s.Value); $found++
            }
        }
    }
}
$comp.Close()
Write-Host "Total temp sensors with values: $found"
