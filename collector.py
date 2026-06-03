import subprocess
import json
import time
import threading
import logging
from db import get_connection, purge_old_data, rollup_aggregates
from alerts import is_enabled
from config import (
    POLL_INTERVAL, VHD_POLL_MULTIPLE, PURGE_CHECK_MULTIPLE,
    SYSINFO_POLL_MULTIPLE, EVENTLOG_POLL_MULTIPLE, UPDATES_POLL_MULTIPLE,
    ROLLUP_POLL_MULTIPLE, SECURITY_POLL_MULTIPLE, VEEAM_POLL_MULTIPLE,
    RDP_LOOKBACK_DAYS, RDP_MAX_EVENTS,
    EVENTLOG_LOOKBACK_HOURS, EVENTLOG_MAX_EVENTS,
)

logger = logging.getLogger(__name__)


def ps_json(script, timeout=15):
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("PowerShell timed out after %ds", timeout)
        return None
    if result.returncode != 0:
        logger.warning("PowerShell error: %s", result.stderr[:500])
        return None
    stdout = result.stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("JSON parse error: %s", stdout[:500])
        return None


def _ensure_list(data):
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    return data


def _match_vm_to_instance(instance, vm_lc_to_orig):
    """Map a Hyper-V counter InstanceName to a VM name.

    Counter instance names vary by Hyper-V version. Typical forms:
      - 'VMName_<AdapterGUID>'           (Virtual Network Adapter)
      - 'VMName Network Adapter'         (Virtual Network Adapter)
      - 'VMName-<DiskName>'              (Virtual Storage Device, dynamic VHDX)
      - 'VMName_<GUID>'                  (Virtual Storage Device)
      - '<GUID>'                         (raw GUID — unmappable)

    Strategy: longest VM-name prefix or substring match against the instance.
    """
    if not instance:
        return None
    inst = instance.lower()
    best = None
    best_len = 0
    for vm_lc, vm_orig in vm_lc_to_orig.items():
        if vm_lc and (inst.startswith(vm_lc) or vm_lc in inst):
            if len(vm_lc) > best_len:
                best = vm_orig
                best_len = len(vm_lc)
    return best


class MetricCollector:
    def __init__(self, db_path=None):
        self.db_path = db_path
        self._prev_net = {}
        self._poll_count = 0
        self._stop_event = threading.Event()
        self.last_temp_diagnostics = {"sensors": [], "diagnostics": {}, "error": None}

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._collect_once()
            except Exception:
                logger.exception("Collection cycle failed")
            self._stop_event.wait(POLL_INTERVAL)

    def stop(self):
        self._stop_event.set()

    def _collect_once(self):
        self._poll_count += 1
        ts = time.time()
        conn = get_connection(self.db_path)
        try:
            self._collect_vm_and_network(conn, ts)
            self._collect_host_counters(conn, ts)
            self._collect_volumes(conn, ts)
            if self._poll_count == 1 or self._poll_count % SYSINFO_POLL_MULTIPLE == 0:
                self._collect_system_info(conn, ts)
            if self._poll_count == 1 or self._poll_count % EVENTLOG_POLL_MULTIPLE == 0:
                self._collect_event_logs(conn, ts)
            if self._poll_count == 1 or self._poll_count % SECURITY_POLL_MULTIPLE == 0:
                self._collect_security(conn, ts)
                self._collect_rdp_logins(conn, ts)
            if self._poll_count == 1 or self._poll_count % VEEAM_POLL_MULTIPLE == 0:
                if is_enabled(conn, "veeam_enabled"):
                    self._collect_veeam(conn, ts)
                if is_enabled(conn, "windowsbackup_enabled"):
                    self._collect_windows_backup(conn, ts)
            if self._poll_count % VHD_POLL_MULTIPLE == 0:
                self._collect_vhd(conn, ts)
            if self._poll_count == 1 or self._poll_count % UPDATES_POLL_MULTIPLE == 0:
                self._collect_pending_updates(conn, ts)
            if self._poll_count % ROLLUP_POLL_MULTIPLE == 0:
                rollup_aggregates(conn)
            if self._poll_count % PURGE_CHECK_MULTIPLE == 0:
                purge_old_data(conn)
            conn.commit()
        finally:
            conn.close()
        logger.debug("Poll #%d completed in %.1fs", self._poll_count, time.time() - ts)

    def _collect_vm_and_network(self, conn, ts):
        # Step 1: VM basic info via Get-VM.
        # - State is converted to string explicitly with "$($_.State)" because the
        #   raw enum value can break the string comparison on some Hyper-V versions.
        # - Heartbeat is wrapped in try/catch because $_.Heartbeat can throw for
        #   VMs without integration services; we capture as a real string.
        # - IPs come from Get-VMNetworkAdapter | Select IPAddresses, joined IPv4-only.
        script_vm = (
            "Get-VM | Select-Object Name, "
            "@{N='StateStr';E={\"$($_.State)\"}}, "
            "CPUUsage, MemoryAssigned, MemoryDemand, "
            "@{N='UptimeSec';E={try{if($_.Uptime){$_.Uptime.TotalSeconds}else{0}}catch{0}}}, "
            "@{N='Heartbeat';E={"
            "try{"
            "  $stateName = \"$($_.State)\"; "
            "  if ($stateName -ne 'Running') { 'N/A' } "
            "  else { "
            "    $h = $_.Heartbeat; "
            "    if ($h -ne $null -and \"$h\" -ne '') { \"$h\" } else { 'NoContact' } "
            "  } "
            "} catch { 'Unknown' } "
            "}}, "
            "@{N='IPs';E={"
            "try{"
            "  $stateName = \"$($_.State)\"; "
            "  if ($stateName -ne 'Running') { '' } "
            "  else { "
            "    $vm = $_; "
            "    $ips = @(); "
            "    foreach ($a in (Get-VMNetworkAdapter -VM $vm -ErrorAction SilentlyContinue)) { "
            "      foreach ($ip in $a.IPAddresses) { "
            "        if ($ip -and $ip -notmatch ':' -and $ip -ne '169.254.0.0') { $ips += $ip } "
            "      } "
            "    } "
            "    ($ips | Select-Object -Unique) -join ',' "
            "  } "
            "} catch { '' } "
            "}} | ConvertTo-Json -Compress -Depth 3"
        )
        vms = _ensure_list(ps_json(script_vm))
        if not vms:
            return

        # Build per-VM record map keyed by lowercase name for counter mapping
        vm_data = {}
        vm_lc_to_orig = {}
        for vm in vms:
            if not isinstance(vm, dict) or not vm.get("Name"):
                continue
            name = vm["Name"]
            state = vm.get("StateStr") or "Unknown"

            vm_data[name] = {
                "state": state,
                "cpu_usage":      vm.get("CPUUsage"),
                "mem_assigned":   vm.get("MemoryAssigned"),
                "mem_demand":     vm.get("MemoryDemand"),
                "uptime_sec":     vm.get("UptimeSec"),
                "heartbeat":      vm.get("Heartbeat"),
                "ip_addresses":   vm.get("IPs") or "",
                "net_sent_bps":   0.0,
                "net_recv_bps":   0.0,
                "disk_read_bps":  0.0,
                "disk_write_bps": 0.0,
            }
            vm_lc_to_orig[name.lower()] = name

        # Step 2: All VM throughput counters in one Get-Counter call
        script_perf = (
            "try { "
            "Get-Counter "
            "'\\Hyper-V Virtual Network Adapter(*)\\Bytes Sent/sec',"
            "'\\Hyper-V Virtual Network Adapter(*)\\Bytes Received/sec',"
            "'\\Hyper-V Virtual Storage Device(*)\\Read Bytes/sec',"
            "'\\Hyper-V Virtual Storage Device(*)\\Write Bytes/sec' "
            "-SampleInterval 1 -MaxSamples 1 -ErrorAction Stop | "
            "ForEach-Object { $_.CounterSamples } | "
            "Select-Object Path, InstanceName, CookedValue | "
            "ConvertTo-Json -Compress -Depth 3 "
            "} catch { '[]' }"
        )
        samples = _ensure_list(ps_json(script_perf))

        for s in samples:
            instance = (s.get("InstanceName") or "").strip()
            path     = (s.get("Path") or "").lower()
            value    = s.get("CookedValue", 0) or 0
            vm_name  = _match_vm_to_instance(instance, vm_lc_to_orig)
            if not vm_name or vm_name not in vm_data:
                continue
            if "bytes sent/sec" in path:
                vm_data[vm_name]["net_sent_bps"] += value
            elif "bytes received/sec" in path:
                vm_data[vm_name]["net_recv_bps"] += value
            elif "read bytes/sec" in path:
                vm_data[vm_name]["disk_read_bps"] += value
            elif "write bytes/sec" in path:
                vm_data[vm_name]["disk_write_bps"] += value

        # Step 3: Insert
        for name, d in vm_data.items():
            conn.execute(
                """INSERT INTO vm_metrics
                   (ts, vm_name, state, cpu_usage, mem_assigned, mem_demand,
                    uptime_sec, heartbeat, net_sent_bps, net_recv_bps,
                    disk_read_bps, disk_write_bps, ip_addresses)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts, name, d["state"],
                    d["cpu_usage"], d["mem_assigned"], d["mem_demand"],
                    d["uptime_sec"], d["heartbeat"],
                    d["net_sent_bps"] or None,
                    d["net_recv_bps"] or None,
                    d["disk_read_bps"] or None,
                    d["disk_write_bps"] or None,
                    d["ip_addresses"] or None,
                ),
            )

    def _collect_host_counters(self, conn, ts):
        script = (
            "Get-Counter "
            "'\\Processor(_Total)\\% Processor Time',"
            "'\\Memory\\Available Bytes',"
            "'\\Memory\\% Committed Bytes In Use',"
            "'\\PhysicalDisk(_Total)\\Disk Read Bytes/sec',"
            "'\\PhysicalDisk(_Total)\\Disk Write Bytes/sec' "
            "-SampleInterval 1 -MaxSamples 1 | "
            "ForEach-Object { $_.CounterSamples | Select-Object Path, CookedValue } | "
            "ConvertTo-Json -Compress"
        )
        data = _ensure_list(ps_json(script))
        if not data:
            return

        vals = {}
        for sample in data:
            path = (sample.get("Path") or "").lower()
            val = sample.get("CookedValue")
            if "% processor time" in path:
                vals["cpu"] = val
            elif "available bytes" in path:
                vals["mem_avail"] = val
            elif "% committed bytes" in path:
                vals["mem_pct"] = val
            elif "disk read bytes" in path:
                vals["disk_r"] = val
            elif "disk write bytes" in path:
                vals["disk_w"] = val

        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            mem_status = ctypes.c_ulonglong()
            kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(mem_status))
            mem_total = mem_status.value * 1024
        except Exception:
            mem_total = None

        sensors = self._collect_all_temperatures()  # list[dict]
        cpu_max  = self._max_of_type(sensors, "cpu")
        disk_max = self._max_of_type(sensors, "disk")
        gpu_max  = self._max_of_type(sensors, "gpu")

        # Save the live per-sensor breakdown to host_sensors_now for the dashboard
        try:
            conn.execute(
                """UPDATE host_sensors_now
                   SET updated_ts=?, sensors_json=? WHERE id=1""",
                (ts, json.dumps(sensors) if sensors else None),
            )
        except Exception:
            pass

        conn.execute(
            """INSERT INTO host_metrics
               (ts, cpu_pct, mem_total, mem_avail, mem_pct,
                disk_read_bps, disk_write_bps,
                cpu_temp_c, disk_temp_c, gpu_temp_c)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                ts,
                vals.get("cpu"),
                mem_total,
                vals.get("mem_avail"),
                vals.get("mem_pct"),
                vals.get("disk_r"),
                vals.get("disk_w"),
                cpu_max,
                disk_max,
                gpu_max,
            ),
        )

    @staticmethod
    def _max_of_type(sensors, t):
        """Return the highest reading among sensors of a given type, or None."""
        vals = [s.get("c") for s in sensors
                if isinstance(s, dict) and s.get("t") == t and s.get("c") is not None]
        try:
            vals = [float(v) for v in vals]
        except (TypeError, ValueError):
            return None
        return max(vals) if vals else None

    # ── Temperature scan script (encoded so shell quoting can't break it) ───
    # Returns a JSON object: { sensors: [...], diagnostics: {...} }
    # `diagnostics` records per-source: tried, count, error.
    _TEMP_SCRIPT = r"""
$ErrorActionPreference = 'Continue'
$sensors = @()
$diag = @{
    acpi  = @{ tried = $false; count = 0; error = $null }
    smart = @{ tried = $false; count = 0; error = $null; disks = 0 }
    lhm   = @{ tried = $false; count = 0; error = $null }
    ohm   = @{ tried = $false; count = 0; error = $null }
}

function Test-Plausible($v) {
    if ($null -eq $v) { return $false }
    try { $d = [double]$v } catch { return $false }
    return ($d -gt 10 -and $d -lt 120)
}

# ── 1. CPU via ACPI thermal zones ──────────────────────────────────────────
$diag.acpi.tried = $true
try {
    $i = 0
    foreach ($z in (Get-CimInstance -Namespace 'root\WMI' -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop)) {
        try {
            $c = [double](($z.CurrentTemperature - 2732) / 10.0)
            if (Test-Plausible $c) {
                $sensors += @{ t = 'cpu'; n = "Thermal Zone $i"; c = [Math]::Round($c, 1); s = 'ACPI' }
                $diag.acpi.count++
            }
        } catch {}
        $i++
    }
} catch {
    $diag.acpi.error = $_.Exception.Message
}

# ── 2. Disk temperatures via SMART (Get-StorageReliabilityCounter) ─────────
$diag.smart.tried = $true
try {
    $disks = @(Get-PhysicalDisk -ErrorAction Stop)
    $diag.smart.disks = $disks.Count
    foreach ($d in $disks) {
        try {
            $rc = $d | Get-StorageReliabilityCounter -ErrorAction Stop
            if ($rc -and $rc.Temperature -ne $null) {
                $c = [double]$rc.Temperature
                if (Test-Plausible $c) {
                    $name = if ($d.FriendlyName) { $d.FriendlyName } else { "Disk $($d.DeviceId)" }
                    $sensors += @{ t = 'disk'; n = $name; c = [Math]::Round($c, 1); s = 'SMART' }
                    $diag.smart.count++
                }
            }
        } catch {}
    }
} catch {
    $diag.smart.error = $_.Exception.Message
}

# ── 3. LibreHardwareMonitor (CPU, disk, GPU, motherboard) ──────────────────
$diag.lhm.tried = $true
try {
    $temps = Get-CimInstance -Namespace 'root\LibreHardwareMonitor' -ClassName Sensor -ErrorAction Stop |
             Where-Object { $_.SensorType -eq 'Temperature' }
    foreach ($s in $temps) {
        try {
            if (-not (Test-Plausible $s.Value)) { continue }
            $parent = "$($s.Parent)"
            $sname  = "$($s.Name)"
            $type   = 'motherboard'
            if     ($parent -match '(?i)cpu|amdcpu|intelcpu')           { $type = 'cpu' }
            elseif ($parent -match '(?i)gpu|nvidia|amd|geforce|radeon') { $type = 'gpu' }
            elseif ($parent -match '(?i)hdd|ssd|nvme|disk')             { $type = 'disk' }
            elseif ($sname  -match '(?i)cpu|core|package')              { $type = 'cpu' }
            elseif ($sname  -match '(?i)gpu|graphics')                  { $type = 'gpu' }
            elseif ($sname  -match '(?i)hdd|ssd|nvme|drive')            { $type = 'disk' }

            $already = $false
            foreach ($e in $sensors) {
                if ($e.t -eq $type -and $e.n -eq $sname) { $already = $true; break }
            }
            if (-not $already) {
                $sensors += @{ t = $type; n = $sname; c = [Math]::Round([double]$s.Value, 1); s = 'LHM' }
                $diag.lhm.count++
            }
        } catch {}
    }
} catch {
    $diag.lhm.error = $_.Exception.Message
}

# ── 4. OpenHardwareMonitor (legacy) ────────────────────────────────────────
$diag.ohm.tried = $true
try {
    $temps = Get-CimInstance -Namespace 'root\OpenHardwareMonitor' -ClassName Sensor -ErrorAction Stop |
             Where-Object { $_.SensorType -eq 'Temperature' }
    foreach ($s in $temps) {
        try {
            if (-not (Test-Plausible $s.Value)) { continue }
            $parent = "$($s.Parent)"
            $sname  = "$($s.Name)"
            $type   = 'motherboard'
            if     ($parent -match '(?i)cpu')      { $type = 'cpu' }
            elseif ($parent -match '(?i)gpu')      { $type = 'gpu' }
            elseif ($parent -match '(?i)hdd|ssd')  { $type = 'disk' }
            elseif ($sname  -match '(?i)cpu|core') { $type = 'cpu' }
            elseif ($sname  -match '(?i)gpu')      { $type = 'gpu' }
            elseif ($sname  -match '(?i)hdd|ssd')  { $type = 'disk' }

            $already = $false
            foreach ($e in $sensors) {
                if ($e.t -eq $type -and $e.n -eq $sname) { $already = $true; break }
            }
            if (-not $already) {
                $sensors += @{ t = $type; n = $sname; c = [Math]::Round([double]$s.Value, 1); s = 'OHM' }
                $diag.ohm.count++
            }
        } catch {}
    }
} catch {
    $diag.ohm.error = $_.Exception.Message
}

@{ sensors = $sensors; diagnostics = $diag } | ConvertTo-Json -Compress -Depth 4
"""

    def _run_temp_script(self, timeout=25):
        """Run the temperature scan via -EncodedCommand for safety, return the
        parsed dict OR (None, error_message) for diagnostic purposes."""
        try:
            import base64
            encoded = base64.b64encode(self._TEMP_SCRIPT.encode("utf-16-le")).decode("ascii")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None, "PowerShell timed out"
        except Exception as e:
            return None, str(e)
        if result.returncode != 0:
            return None, (result.stderr or "")[:500] or f"exit code {result.returncode}"
        stdout = (result.stdout or "").strip()
        if not stdout:
            return None, "empty stdout"
        try:
            return json.loads(stdout), None
        except json.JSONDecodeError as e:
            return None, f"JSON parse error: {e}; raw output (truncated): {stdout[:300]}"

    def _collect_all_temperatures(self):
        """Gather every temperature sensor we can find on the host.

        Returns a list of {t: type, n: name, c: celsius, s: source} dicts:
          t = 'cpu' | 'disk' | 'gpu' | 'motherboard'
          n = friendly sensor name
          c = current temperature in Celsius
          s = which provider gave us the reading

        Stores the most recent diagnostics dict on self.last_temp_diagnostics
        so /api/sensors/debug can surface it.

        Plausible-range filter (10–120 °C) applied to every reading so faulty
        sensors don't poison the data.
        """
        data, err = self._run_temp_script()
        if err and not data:
            self.last_temp_diagnostics = {"error": err, "sensors": [], "diagnostics": {}}
            logger.warning("Temperature scan failed: %s", err[:200])
            return []
        sensors = []
        diag = {}
        if isinstance(data, dict):
            raw_sensors = data.get("sensors") or []
            if isinstance(raw_sensors, dict):
                raw_sensors = [raw_sensors]
            sensors = [s for s in raw_sensors if isinstance(s, dict)]
            diag = data.get("diagnostics") or {}
        elif isinstance(data, list):
            # Older script format — just a sensor list
            sensors = [s for s in data if isinstance(s, dict)]

        self.last_temp_diagnostics = {
            "sensors": sensors,
            "diagnostics": diag,
            "error": None,
        }

        # Summarise in the log so it shows up in the running console
        if sensors:
            by_type = {}
            for s in sensors:
                by_type[s.get("t", "?")] = by_type.get(s.get("t", "?"), 0) + 1
            summary = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
            logger.info("Temperature scan: %d sensors (%s)", len(sensors), summary)
        else:
            # No sensors — explain what was tried
            parts = []
            for src in ("acpi", "smart", "lhm", "ohm"):
                d = diag.get(src) or {}
                if d.get("tried"):
                    if d.get("error"):
                        parts.append(f"{src}=ERR({(d.get('error') or '')[:80]})")
                    else:
                        parts.append(f"{src}=0")
                else:
                    parts.append(f"{src}=skipped")
            logger.info("Temperature scan: 0 sensors (%s)", "; ".join(parts))

        return sensors

    def _collect_volumes(self, conn, ts):
        script = (
            "Get-Volume | Where-Object { $_.DriveLetter -ne $null -and $_.Size -gt 0 } | "
            "Select-Object DriveLetter, FileSystemLabel, "
            "@{N='Total';E={$_.Size}}, @{N='Free';E={$_.SizeRemaining}} | "
            "ConvertTo-Json -Compress"
        )
        data = _ensure_list(ps_json(script))
        for vol in data:
            drive = vol.get("DriveLetter", "")
            total = vol.get("Total", 0) or 0
            free = vol.get("Free", 0) or 0
            pct = ((total - free) / total * 100) if total > 0 else 0
            conn.execute(
                "INSERT INTO host_volumes (ts, drive, label, total, free, pct_used) VALUES (?,?,?,?,?,?)",
                (ts, str(drive), vol.get("FileSystemLabel", ""), total, free, round(pct, 1)),
            )

    def _collect_vhd(self, conn, ts):
        script = (
            "Get-VM | ForEach-Object { $vmName = $_.Name; "
            "$_.HardDrives | ForEach-Object { "
            "$vhd = Get-VHD -Path $_.Path -ErrorAction SilentlyContinue; "
            "if ($vhd) { [PSCustomObject]@{ "
            "VMName=$vmName; Path=$_.Path; "
            "VhdType=$vhd.VhdType.ToString(); "
            "FileSize=$vhd.FileSize; MaxSize=$vhd.Size } } } } | "
            "ConvertTo-Json -Compress"
        )
        data = _ensure_list(ps_json(script, timeout=30))
        for vhd in data:
            fs = vhd.get("FileSize", 0) or 0
            ms = vhd.get("MaxSize", 0) or 0
            pct = (fs / ms * 100) if ms > 0 else 0
            conn.execute(
                """INSERT INTO vhd_info (ts, vm_name, vhd_path, vhd_type, file_size, max_size, pct_used)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    ts,
                    vhd.get("VMName", ""),
                    vhd.get("Path", ""),
                    vhd.get("VhdType", ""),
                    fs, ms, round(pct, 1),
                ),
            )

    def _collect_system_info(self, conn, ts):
        script = (
            "$os = Get-CimInstance Win32_OperatingSystem; "
            "$cs = Get-CimInstance Win32_ComputerSystem; "
            "$boot = $os.LastBootUpTime; "
            "$bootEpoch = [int][DateTimeOffset]::new($boot).ToUnixTimeSeconds(); "
            "$uptimeSec = ((Get-Date) - $boot).TotalSeconds; "
            "[PSCustomObject]@{ "
            "OsName = $os.Caption; "
            "OsVersion = $os.Version; "
            "HostName = $cs.Name; "
            "LastBootEpoch = $bootEpoch; "
            "UptimeSec = $uptimeSec "
            "} | ConvertTo-Json -Compress"
        )
        data = ps_json(script)
        if not data or not isinstance(data, dict):
            return

        boot_epoch = data.get("LastBootEpoch")
        conn.execute(
            """UPDATE system_info SET
                ts=?, os_name=?, os_version=?, host_name=?,
                last_boot_ts=?, uptime_sec=? WHERE id=1""",
            (
                ts,
                data.get("OsName"),
                data.get("OsVersion"),
                data.get("HostName"),
                boot_epoch,
                data.get("UptimeSec"),
            ),
        )

        if boot_epoch:
            conn.execute(
                "INSERT OR IGNORE INTO reboot_history (ts_boot) VALUES (?)",
                (boot_epoch,),
            )

        self._scan_reboot_history(conn)

    def _scan_reboot_history(self, conn):
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM reboot_history"
        ).fetchone()[0]
        if existing_count >= 10:
            return

        script = (
            "Get-WinEvent -FilterHashtable @{LogName='System'; Id=6005,6006,6008,1074,41} "
            "-MaxEvents 30 -ErrorAction SilentlyContinue | "
            "Select-Object @{N='TsEpoch';E={[int][DateTimeOffset]::new($_.TimeCreated).ToUnixTimeSeconds()}}, "
            "Id, @{N='Msg';E={if($_.Message){$_.Message.Substring(0,[Math]::Min(120,$_.Message.Length))}else{''}}} | "
            "ConvertTo-Json -Compress"
        )
        data = _ensure_list(ps_json(script, timeout=30))
        for ev in data:
            ts_boot = ev.get("TsEpoch")
            event_id = ev.get("Id")
            reason_map = {
                6005: "Event log service started (boot)",
                6006: "Event log service stopped (shutdown)",
                6008: "Unexpected shutdown",
                1074: "User-initiated shutdown/restart",
                41:   "Kernel power critical (unexpected)",
            }
            reason = reason_map.get(event_id, "")
            if ts_boot:
                conn.execute(
                    "INSERT OR IGNORE INTO reboot_history (ts_boot, reason) VALUES (?,?)",
                    (ts_boot, reason),
                )

    def _collect_event_logs(self, conn, ts):
        script = (
            "$hours = " + str(EVENTLOG_LOOKBACK_HOURS) + "; "
            "$max = " + str(EVENTLOG_MAX_EVENTS) + "; "
            "$start = (Get-Date).AddHours(-$hours); "
            "$events = @(); "
            "foreach ($log in @('System','Application')) { "
            "  try { "
            "    $events += Get-WinEvent -FilterHashtable @{LogName=$log; Level=1,2; StartTime=$start} "
            "      -MaxEvents $max -ErrorAction SilentlyContinue "
            "  } catch {} "
            "} "
            "$events | Select-Object "
            "@{N='TsEpoch';E={[int][DateTimeOffset]::new($_.TimeCreated).ToUnixTimeSeconds()}}, "
            "@{N='LogName';E={$_.LogName}}, "
            "@{N='Source';E={$_.ProviderName}}, "
            "@{N='Id';E={$_.Id}}, "
            "@{N='Level';E={$_.Level}}, "
            "@{N='LevelName';E={$_.LevelDisplayName}}, "
            "@{N='Msg';E={if($_.Message){($_.Message -replace '\\r?\\n',' ').Substring(0,[Math]::Min(300,$_.Message.Length))}else{''}}} | "
            "ConvertTo-Json -Compress -Depth 3"
        )
        data = _ensure_list(ps_json(script, timeout=30))
        inserted = 0
        for ev in data:
            ts_event = ev.get("TsEpoch")
            if not ts_event:
                continue
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO system_events
                       (ts_event, log_name, source, event_id, level, level_name, message)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        ts_event,
                        ev.get("LogName", ""),
                        ev.get("Source", ""),
                        ev.get("Id", 0),
                        ev.get("Level", 0),
                        ev.get("LevelName", ""),
                        ev.get("Msg", ""),
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
            except Exception:
                logger.exception("Failed to insert event")
        if inserted:
            logger.info("Collected %d new system events", inserted)

    def _collect_pending_updates(self, conn, ts):
        script = (
            "try { "
            "$session = New-Object -ComObject Microsoft.Update.Session; "
            "$searcher = $session.CreateUpdateSearcher(); "
            "$searcher.ServerSelection = 1; "
            "$result = $searcher.Search('IsInstalled=0 and IsHidden=0'); "
            "$updates = @(); "
            "foreach ($u in $result.Updates) { "
            "  $kb = ''; "
            "  if ($u.KBArticleIDs.Count -gt 0) { $kb = 'KB' + $u.KBArticleIDs[0] } "
            "  $sev = if ($u.MsrcSeverity) { $u.MsrcSeverity } else { 'Unspecified' }; "
            "  $updates += [PSCustomObject]@{ Title=$u.Title; Severity=$sev; KB=$kb } "
            "} "
            "$updates | ConvertTo-Json -Compress -Depth 3 "
            "} catch { '[]' }"
        )
        data = _ensure_list(ps_json(script, timeout=120))

        conn.execute("DELETE FROM pending_updates")
        for upd in data:
            title = upd.get("Title", "")
            if not title:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO pending_updates (ts_seen, title, severity, kb) VALUES (?,?,?,?)",
                    (ts, title, upd.get("Severity", ""), upd.get("KB", "")),
                )
            except Exception:
                pass

        conn.execute(
            "UPDATE system_info SET updates_pending=?, updates_ts=? WHERE id=1",
            (len(data), ts),
        )
        logger.info("Found %d pending Windows updates", len(data))

    # ── Security ─────────────────────────────────────────────────────────────

    def _collect_security(self, conn, ts):
        """Collect Windows security posture: firewall, Defender, BitLocker, UAC."""
        # Note: all statements separated by semicolons because powershell.exe -Command
        # treats the script as a single line. Missing semicolons cause parser errors.
        script = (
            "$result = @{}; "
            "try { "
            "  $fw = Get-NetFirewallProfile -ErrorAction Stop; "
            "  foreach ($p in $fw) { $result[\"Firewall_$($p.Name)\"] = [int]$p.Enabled }; "
            "} catch { $result['Firewall_Error'] = $_.Exception.Message }; "
            "try { "
            "  $d = Get-MpComputerStatus -ErrorAction Stop; "
            "  $result['Defender_Realtime'] = [int]$d.RealTimeProtectionEnabled; "
            "  $result['Defender_AVEnabled'] = [int]$d.AntivirusEnabled; "
            "  $result['Defender_EngineVersion'] = if ($d.AMEngineVersion) { $d.AMEngineVersion.ToString() } else { '' }; "
            "  if ($d.AntivirusSignatureLastUpdated) { "
            "    $result['Defender_SigDate'] = ((Get-Date) - $d.AntivirusSignatureLastUpdated).TotalDays "
            "  } else { $result['Defender_SigDate'] = -1 }; "
            "} catch { $result['Defender_Error'] = $_.Exception.Message }; "
            "try { "
            "  $bl = @(Get-BitLockerVolume -ErrorAction Stop); "
            "  $on  = @($bl | Where-Object { $_.ProtectionStatus -eq 'On' }).Count; "
            "  $off = @($bl | Where-Object { $_.ProtectionStatus -eq 'Off' }).Count; "
            "  if ($bl.Count -eq 0) { $result['BitLocker'] = 'None' } "
            "  elseif ($off -eq 0)   { $result['BitLocker'] = \"On ($on vols)\" } "
            "  elseif ($on -eq 0)    { $result['BitLocker'] = \"Off ($off vols)\" } "
            "  else                  { $result['BitLocker'] = \"Mixed ($on on / $off off)\" }; "
            "} catch { $result['BitLocker'] = 'NotAvailable' }; "
            "try { "
            "  $uac = (Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' -Name EnableLUA -ErrorAction Stop).EnableLUA; "
            "  $result['UAC'] = [int]$uac; "
            "} catch { $result['UAC'] = -1 }; "
            "try { "
            "  $admins = @(Get-LocalGroupMember -Group 'Administrators' -ErrorAction Stop); "
            "  $result['AdminCount'] = $admins.Count; "
            "} catch { $result['AdminCount'] = -1 }; "
            "try { "
            "  $cut = (Get-Date).AddHours(-24); "
            "  $failed = @(Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4625; StartTime=$cut} -ErrorAction SilentlyContinue); "
            "  $result['FailedLogins24h'] = $failed.Count; "
            "} catch { $result['FailedLogins24h'] = -1 }; "
            "try { "
            "  $cut = (Get-Date).AddHours(-24); "
            "  $rdp = @(Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4624; StartTime=$cut} -ErrorAction SilentlyContinue | "
            "          Where-Object { $_.Properties[8].Value -eq 10 }); "
            "  $result['RdpSuccess24h'] = $rdp.Count; "
            "} catch { $result['RdpSuccess24h'] = -1 }; "
            "try { "
            "  $reasons = @(); "
            "  $comReboot = $null; "
            # Authoritative check: ask Windows itself via the Windows Update
            # COM API. This is what Windows Update Agent uses internally and
            # avoids false positives from stale registry keys that didn't get
            # cleaned up after a previous reboot.
            "  try { "
            "    $sysInfo = New-Object -ComObject Microsoft.Update.SystemInfo; "
            "    $comReboot = [bool]$sysInfo.RebootRequired "
            "  } catch {}; "
            # Registry indicators — used both as reasons (when COM agrees a
            # reboot is needed) and as a fallback when the COM call fails.
            "  if (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending') { $reasons += 'Component Based Servicing' }; "
            "  if (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired') { $reasons += 'Windows Update' }; "
            "  if (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\ServerManager\\CurrentRebootAttempts') { $reasons += 'Server Manager' }; "
            "  if ($comReboot -eq $true) { "
            "    $result['PendingReboot'] = 1; "
            "    if ($reasons.Count -eq 0) { $reasons = @('Windows Update Agent') } "
            "  } elseif ($comReboot -eq $false) { "
            # Windows says no reboot needed — trust it over stale registry keys
            "    $result['PendingReboot'] = 0; "
            "    $reasons = @() "
            "  } else { "
            # COM call failed — fall back to registry-only detection
            "    $result['PendingReboot'] = [int]($reasons.Count -gt 0) "
            "  }; "
            "  $result['RebootReasons'] = ($reasons -join ', '); "
            "} catch { $result['PendingReboot'] = 0; $result['RebootReasons'] = '' }; "
            "$result | ConvertTo-Json -Compress -Depth 3"
        )
        data = ps_json(script, timeout=45)
        if not isinstance(data, dict):
            return

        firewall_domain  = data.get("Firewall_Domain")
        firewall_private = data.get("Firewall_Private")
        firewall_public  = data.get("Firewall_Public")
        defender_rt      = data.get("Defender_Realtime")
        defender_av      = data.get("Defender_AVEnabled")
        defender_eng     = data.get("Defender_EngineVersion")
        defender_sigage  = data.get("Defender_SigDate")
        if defender_sigage == -1:
            defender_sigage = None
        bitlocker        = data.get("BitLocker", "Unknown")
        uac              = data.get("UAC", -1)
        admin_count      = data.get("AdminCount", -1)
        failed_24h       = data.get("FailedLogins24h", -1)
        rdp_24h          = data.get("RdpSuccess24h", -1)
        pending_reboot   = data.get("PendingReboot", 0)
        reboot_reasons   = data.get("RebootReasons", "")

        findings = self._evaluate_security_findings(data)
        findings_json = json.dumps(findings)

        conn.execute(
            """UPDATE security_status SET
                ts=?,
                firewall_domain=?, firewall_private=?, firewall_public=?,
                defender_realtime=?, defender_antivirus_enabled=?,
                defender_signature_age_days=?, defender_engine_version=?,
                bitlocker_status=?, uac_enabled=?,
                failed_logins_24h=?, rdp_success_24h=?, admin_count=?,
                pending_reboot=?, reboot_reasons=?,
                findings_json=?
               WHERE id=1""",
            (
                ts,
                firewall_domain, firewall_private, firewall_public,
                defender_rt, defender_av,
                defender_sigage, defender_eng,
                bitlocker, uac if uac != -1 else None,
                failed_24h if failed_24h != -1 else None,
                rdp_24h if rdp_24h != -1 else None,
                admin_count if admin_count != -1 else None,
                int(pending_reboot or 0), reboot_reasons or "",
                findings_json,
            ),
        )
        logger.info(
            "Security: firewall(D/P/Pub)=%s/%s/%s, defender_rt=%s, bitlocker=%s, failed_24h=%s, rdp_24h=%s, findings=%d",
            firewall_domain, firewall_private, firewall_public,
            defender_rt, bitlocker, failed_24h, rdp_24h, len(findings),
        )

    def _evaluate_security_findings(self, data):
        """Turn raw security data into a list of human-readable findings."""
        findings = []
        for prof in ("Domain", "Private", "Public"):
            val = data.get(f"Firewall_{prof}")
            if val == 0:
                findings.append({
                    "severity": "high",
                    "title": f"{prof} Firewall profile is disabled",
                    "detail": "Re-enable in Windows Security > Firewall & network protection.",
                })

        if data.get("Defender_Realtime") == 0:
            findings.append({
                "severity": "high",
                "title": "Microsoft Defender real-time protection is off",
                "detail": "Run: Set-MpPreference -DisableRealtimeMonitoring $false",
            })
        sig_age = data.get("Defender_SigDate")
        if isinstance(sig_age, (int, float)) and sig_age > 7:
            findings.append({
                "severity": "medium",
                "title": f"Defender signatures are {sig_age:.1f} days old",
                "detail": "Update signatures: Update-MpSignature  (or via Windows Update)",
            })

        uac = data.get("UAC")
        if uac == 0:
            findings.append({
                "severity": "high",
                "title": "UAC (User Account Control) is disabled",
                "detail": "Set EnableLUA=1 in HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System and reboot.",
            })

        # BitLocker status is collected for the Security tile but is not
        # surfaced as a finding — Hyper-V hosts often legitimately run without
        # BitLocker (locked physical access, full-disk encryption handled at
        # a different layer, etc.). Status remains visible on the tile.

        admin_count = data.get("AdminCount", -1)
        if isinstance(admin_count, int) and admin_count > 3:
            findings.append({
                "severity": "info",
                "title": f"{admin_count} accounts are in the local Administrators group",
                "detail": "Review: Get-LocalGroupMember -Group Administrators",
            })

        failed_24h = data.get("FailedLogins24h", -1)
        if isinstance(failed_24h, int):
            if failed_24h >= 50:
                findings.append({
                    "severity": "high",
                    "title": f"{failed_24h} failed login attempts in the last 24h",
                    "detail": "Possible brute-force activity. Review event ID 4625 in the Security log.",
                })
            elif failed_24h >= 10:
                findings.append({
                    "severity": "medium",
                    "title": f"{failed_24h} failed login attempts in the last 24h",
                    "detail": "Worth a quick look — review event ID 4625 in the Security log.",
                })

        if data.get("PendingReboot"):
            findings.append({
                "severity": "medium",
                "title": "System has a pending reboot",
                "detail": "Reason: " + (data.get("RebootReasons") or "unknown") +
                          ". Until you reboot, some updates and security patches are not active.",
            })

        if not findings:
            findings.append({
                "severity": "ok",
                "title": "No security issues detected",
                "detail": "Firewall, Defender, UAC, and login activity all look normal.",
            })
        return findings

    def _collect_rdp_logins(self, conn, ts):
        """Scan Security event log for RDP logon events (4624/4625 with LogonType 10)."""
        script = (
            f"$cut = (Get-Date).AddDays(-{RDP_LOOKBACK_DAYS}); "
            "$results = @(); "
            "foreach ($id in @(4624, 4625)) { "
            "  try { "
            "    $events = Get-WinEvent -FilterHashtable @{LogName='Security'; Id=$id; StartTime=$cut} "
            f"      -MaxEvents {RDP_MAX_EVENTS} -ErrorAction SilentlyContinue; "
            "    foreach ($e in $events) { "
            "      try { "
            "        $lt = $e.Properties[8].Value; "
            "        if ($lt -ne 10) { continue }; "
            "        $results += [PSCustomObject]@{ "
            "          TsEpoch     = [int][DateTimeOffset]::new($e.TimeCreated).ToUnixTimeSeconds(); "
            "          Username    = $e.Properties[5].Value; "
            "          Domain      = $e.Properties[6].Value; "
            "          SourceIP    = $e.Properties[18].Value; "
            "          Workstation = $e.Properties[11].Value; "
            "          LogonType   = $lt; "
            "          Success     = if ($e.Id -eq 4624) { 1 } else { 0 } "
            "        } "
            "      } catch { continue } "
            "    } "
            "  } catch { } "
            "} "
            "$results | ConvertTo-Json -Compress -Depth 3"
        )
        events = _ensure_list(ps_json(script, timeout=60))
        inserted = 0
        for e in events:
            if not isinstance(e, dict):
                continue
            ts_event = e.get("TsEpoch")
            if not ts_event:
                continue
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO rdp_logins
                       (ts_event, username, domain, source_ip, workstation, logon_type, success)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        ts_event,
                        e.get("Username", ""),
                        e.get("Domain", ""),
                        e.get("SourceIP", ""),
                        e.get("Workstation", ""),
                        e.get("LogonType", 10),
                        1 if e.get("Success") else 0,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
            except Exception:
                logger.exception("Failed to insert RDP login event")
        if inserted:
            logger.info("Collected %d new RDP login events", inserted)

    # ── Veeam Backup & Replication ───────────────────────────────────────────

    def _collect_veeam(self, conn, ts):
        """Scan Veeam B&R job history.

        Detection cascade — try each in order, record which one worked:
          1. Default module name (PSModulePath includes Veeam path)
          2. Registry's recorded CorePath
          3. Two known install locations
          4. Recursive filesystem search under Program Files\\Veeam
          5. Legacy PSSnapin

        On the local Veeam B&R server, Connect-VBRServer -Server 'localhost' is
        attempted after module load to ensure the cmdlets have a session.
        Veeam 12.x auto-connects; older versions don't.

        Whatever the outcome, a row is written to veeam_status describing what
        happened, so the UI can show a useful diagnostic.
        """
        script = (
            "$loaded = $null; "
            "$loadErrors = @(); "
            "$ourErr = $null; "

            # Method 1: default module name
            "try { Import-Module Veeam.Backup.PowerShell -ErrorAction Stop -DisableNameChecking; "
            "  $loaded = 'Veeam.Backup.PowerShell (auto)'; } "
            "catch { $loadErrors += \"auto-import: $($_.Exception.Message)\" }; "

            # Method 2: registry-recorded install path
            "if (-not $loaded) { "
            "  try { "
            "    $reg = Get-ItemProperty 'HKLM:\\SOFTWARE\\Veeam\\Veeam Backup and Replication' -ErrorAction Stop; "
            "    if ($reg.CorePath) { "
            "      $p = Join-Path $reg.CorePath 'Veeam.Backup.PowerShell\\Veeam.Backup.PowerShell.psd1'; "
            "      if (Test-Path $p) { Import-Module $p -ErrorAction Stop -DisableNameChecking; $loaded = \"registry: $p\" } "
            "    } "
            "  } catch { $loadErrors += \"registry: $($_.Exception.Message)\" } "
            "}; "

            # Method 3: known install paths
            "if (-not $loaded) { "
            "  foreach ($p in @("
            "    'C:\\Program Files\\Veeam\\Backup and Replication\\Console\\Veeam.Backup.PowerShell\\Veeam.Backup.PowerShell.psd1',"
            "    'C:\\Program Files\\Veeam\\Backup and Replication\\Backup\\Veeam.Backup.PowerShell\\Veeam.Backup.PowerShell.psd1'"
            "  )) { "
            "    if (Test-Path $p) { "
            "      try { Import-Module $p -ErrorAction Stop -DisableNameChecking; $loaded = \"known-path: $p\"; break } "
            "      catch { $loadErrors += \"known-path '$p': $($_.Exception.Message)\" } "
            "    } "
            "  } "
            "}; "

            # Method 4: filesystem recursive search under Program Files
            "if (-not $loaded) { "
            "  try { "
            "    $found = Get-ChildItem -Path 'C:\\Program Files\\Veeam' -Filter 'Veeam.Backup.PowerShell.psd1' "
            "             -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1; "
            "    if ($found) { "
            "      Import-Module $found.FullName -ErrorAction Stop -DisableNameChecking; "
            "      $loaded = \"fs-search: $($found.FullName)\" "
            "    } "
            "  } catch { $loadErrors += \"fs-search: $($_.Exception.Message)\" } "
            "}; "

            # Method 5: legacy PSSnapin
            "if (-not $loaded) { "
            "  try { Add-PSSnapin VeeamPSSnapIn -ErrorAction Stop; $loaded = 'VeeamPSSnapIn (legacy)' } "
            "  catch { $loadErrors += \"snapin: $($_.Exception.Message)\" } "
            "}; "

            "if (-not $loaded) { "
            "  Write-Output (@{Status='not_loaded'; Loaded=$null; Error=($loadErrors -join '; '); Jobs=@()} | ConvertTo-Json -Compress -Depth 3); "
            "  exit "
            "}; "

            # Module loaded — try Connect-VBRServer (idempotent, ignore failures)
            "try { Connect-VBRServer -Server 'localhost' -ErrorAction SilentlyContinue | Out-Null } catch {}; "

            # Now fetch jobs
            "try { "
            "  $jobs = @(); "
            "  try { $jobs += @(Get-VBRJob -ErrorAction SilentlyContinue) } catch {}; "
            "  try { $jobs += @(Get-VBRComputerBackupJob -ErrorAction SilentlyContinue) } catch {}; "
            "  $out = @(); "
            "  foreach ($j in $jobs) { "
            "    if (-not $j) { continue }; "
            "    $name = if ($j.Name) { $j.Name } else { 'unnamed' }; "
            "    $type = if ($j.JobType) { \"$($j.JobType)\" } elseif ($j.Type) { \"$($j.Type)\" } else { '' }; "
            "    $enabled = $true; "
            "    try { if ($j.PSObject.Properties.Match('ScheduleEnabled')) { $enabled = [bool]$j.ScheduleEnabled } } catch {}; "
            "    $session = $null; "
            "    try { $session = $j.FindLastSession() } catch {}; "
            "    if (-not $session) { "
            "      try { $session = (Get-VBRBackupSession -Job $j -ErrorAction SilentlyContinue | Sort-Object EndTime -Descending | Select-Object -First 1) } catch {} "
            "    }; "
            "    if ($session) { "
            "      $startT = if ($session.CreationTime) { [int][DateTimeOffset]::new($session.CreationTime).ToUnixTimeSeconds() } else { 0 }; "
            "      $endT   = if ($session.EndTime -and $session.EndTime.Year -gt 1) { [int][DateTimeOffset]::new($session.EndTime).ToUnixTimeSeconds() } else { 0 }; "
            "      $result = if ($session.Result) { \"$($session.Result)\" } else { 'None' }; "
            "      $state  = if ($session.State)  { \"$($session.State)\"  } else { '' }; "
            "      $dur = if ($endT -gt 0 -and $startT -gt 0) { $endT - $startT } else { 0 }; "
            "      $out += [PSCustomObject]@{ Name=$name; Type=$type; Result=$result; State=$state; "
            "        StartTs=$startT; EndTs=$endT; DurationSec=$dur; ScheduleEnabled=[int]$enabled } "
            "    } else { "
            "      $out += [PSCustomObject]@{ Name=$name; Type=$type; Result='NeverRan'; State=''; "
            "        StartTs=0; EndTs=0; DurationSec=0; ScheduleEnabled=[int]$enabled } "
            "    } "
            "  }; "
            "  $status = if ($out.Count -eq 0) { 'no_jobs' } else { 'ok' }; "
            "  Write-Output (@{Status=$status; Loaded=$loaded; Error=$null; Jobs=$out} | ConvertTo-Json -Compress -Depth 4) "
            "} catch { "
            "  Write-Output (@{Status='error'; Loaded=$loaded; Error=$_.Exception.Message; Jobs=@()} | ConvertTo-Json -Compress -Depth 3) "
            "}"
        )

        data = ps_json(script, timeout=120)
        if not isinstance(data, dict):
            # Total parse failure — record as error so the UI can show it
            conn.execute(
                """UPDATE veeam_status SET last_check_ts=?, status=?, module_used=?, error_message=?, jobs_count=?
                   WHERE id=1""",
                (ts, "error", None, "Could not parse PowerShell output", 0),
            )
            logger.warning("Veeam: PowerShell returned non-dict / parse error")
            return

        status = data.get("Status", "error")
        loaded = data.get("Loaded")
        err    = data.get("Error")
        jobs   = _ensure_list(data.get("Jobs"))

        # ── Event-log fallback ──────────────────────────────────────────────
        # When the B&R cmdlets either aren't installed (Veeam Agent free
        # edition has no PowerShell module, just GUI + CLI) or returned no
        # jobs, scrape the Windows Event Log. Both the Agent and B&R write
        # backup completion events to the "Veeam Backup" / "Veeam Agent" logs.
        if status in ("not_loaded", "no_jobs", "error") or len(jobs) == 0:
            log_jobs, log_diag = self._collect_veeam_eventlog(ts)
            if log_jobs:
                jobs = log_jobs
                if loaded:
                    loaded = loaded + " + EventLog"
                else:
                    loaded = "Windows Event Log"
                status = "ok"
                err = None
            else:
                # Append event-log diagnostic to whatever the cmdlet path said
                if log_diag:
                    err = ((err or "") + " | EventLog: " + log_diag).strip(" |")

        # Persist status row
        conn.execute(
            """UPDATE veeam_status SET last_check_ts=?, status=?, module_used=?, error_message=?, jobs_count=?
               WHERE id=1""",
            (ts, status, loaded, err, len(jobs)),
        )

        # Persist jobs (only if any)
        names_seen = set()
        for j in jobs:
            if not isinstance(j, dict) or not j.get("Name"):
                continue
            name = j["Name"]
            names_seen.add(name)
            conn.execute(
                """INSERT INTO veeam_backups
                   (job_name, job_type, last_result, last_state, last_start_ts,
                    last_end_ts, duration_sec, schedule_enabled, seen_ts)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_name) DO UPDATE SET
                     job_type         = excluded.job_type,
                     last_result      = excluded.last_result,
                     last_state       = excluded.last_state,
                     last_start_ts    = excluded.last_start_ts,
                     last_end_ts      = excluded.last_end_ts,
                     duration_sec     = excluded.duration_sec,
                     schedule_enabled = excluded.schedule_enabled,
                     seen_ts          = excluded.seen_ts""",
                (
                    name, j.get("Type", ""),
                    j.get("Result", ""), j.get("State", ""),
                    j.get("StartTs") or None, j.get("EndTs") or None,
                    j.get("DurationSec") or None,
                    int(j.get("ScheduleEnabled") or 0),
                    ts,
                ),
            )

        # Clean up jobs that disappeared more than 7 days ago
        conn.execute(
            "DELETE FROM veeam_backups WHERE seen_ts < ?",
            (ts - 7 * 86400,),
        )

        if status == "ok":
            logger.info("Veeam: %d job(s) tracked via %s", len(names_seen), loaded)
        elif status == "no_jobs":
            logger.info("Veeam: module loaded (%s) but no jobs found", loaded)
        elif status == "not_loaded":
            logger.warning("Veeam: PowerShell module not detected. Errors: %s",
                           (err or "")[:300])
        else:
            logger.warning("Veeam: %s (loaded=%s): %s", status, loaded, (err or "")[:300])

    def _collect_veeam_eventlog(self, ts):
        """Universal Veeam backup-status fallback that reads the Windows Event
        Log. Works for Veeam Agent for Windows (free) AND B&R, because both
        write session completion events to a 'Veeam Backup' / 'Veeam Agent' /
        Application log channel.

        Detection strategy:
          1. Only consider events that look like a SESSION-END summary
             (event IDs 41/110/111/190/191/192/193/194, or message contains
             completed/finished/failed terminal keywords).
          2. Determine result from the MESSAGE TEXT first — message wording is
             more reliable than event ID across Veeam product/version variants.
             Fall back to event ID only when the message is ambiguous.
          3. Keep the most recent terminal event per job.

        Returns (list_of_job_dicts, diagnostic_string).
        """
        script = (
            "$results = @(); "
            "$diagParts = @(); "
            "$candidateLogs = @('Veeam Backup', 'Veeam Agent', 'Veeam Endpoint Backup', 'Application'); "
            "$cutoff = (Get-Date).AddDays(-30); "
            "foreach ($logName in $candidateLogs) { "
            "  try { "
            "    $evs = Get-WinEvent -FilterHashtable @{LogName=$logName; StartTime=$cutoff} "
            "           -MaxEvents 2000 -ErrorAction Stop | "
            "           Where-Object { $_.ProviderName -like '*Veeam*' }; "
            "    if ($evs) { $diagParts += \"$logName : $($evs.Count) Veeam event(s)\"; $results += $evs } "
            "  } catch { "
            "    $diagParts += \"$logName : $($_.Exception.Message.Split([Environment]::NewLine)[0])\" "
            "  } "
            "}; "
            "if ($results.Count -eq 0) { "
            "  Write-Output (@{Jobs=@(); Diag=($diagParts -join '; ')} | ConvertTo-Json -Compress -Depth 3); "
            "  exit "
            "}; "
            "$terminalIds = @(41, 110, 111, 190, 191, 192, 193, 194); "
            "$jobMap = @{}; "
            "foreach ($e in $results) { "
            "  $msg = $e.Message; "
            "  if (-not $msg) { continue }; "

            # Skip events that aren't a session-end summary.
            # Either it's a known terminal event ID, or it explicitly contains
            # one of the terminal keywords ("completed", "finished", "failed").
            "  $isTerminal = $false; "
            "  if ($terminalIds -contains $e.Id) { $isTerminal = $true } "
            "  elseif ($msg -match '(?i)\\b(?:completed|finished|failed)\\b') { $isTerminal = $true }; "
            "  if (-not $isTerminal) { continue }; "

            # Extract job name — try multiple patterns
            "  $jobName = $null; "
            "  if     ($msg -match \"[Jj]ob\\s+['""\\u2019\\u201D](.+?)['""\\u2019\\u201D]\")     { $jobName = $matches[1] } "
            "  elseif ($msg -match \"[Jj]ob\\s+\\[(.+?)\\]\")                                   { $jobName = $matches[1] } "
            "  elseif ($msg -match \"[Pp]olicy\\s+['""\\u2019\\u201D](.+?)['""\\u2019\\u201D]\")  { $jobName = $matches[1] } "
            "  elseif ($msg -match \"['""\\u2019\\u201D](.+?)['""\\u2019\\u201D]\\s+(?:backup|job|policy)\") { $jobName = $matches[1] } "
            "  elseif ($msg -match \"[Bb]ackup\\s+(?:of\\s+|job\\s+)?['""]?([^'""\\s].{1,80}?)['""]?\\s+(?:has\\s+(?:been\\s+)?)?(?:completed|finished|failed|succeeded)\") { $jobName = $matches[1].Trim() } "
            "  if (-not $jobName) { continue }; "

            # MESSAGE-BASED result detection — checked first, before event ID,
            # because Veeam Agent often emits multiple events per session and
            # only the message text reliably indicates the actual outcome.
            "  $result = $null; "
            "  if     ($msg -match '(?i)successfully\\s+(?:finished|completed)') { $result = 'Success' } "
            "  elseif ($msg -match '(?i)(?:finished|completed)\\s+successfully')  { $result = 'Success' } "
            "  elseif ($msg -match '(?i)(?:finished|completed)\\s+with\\s+warning') { $result = 'Warning' } "
            "  elseif ($msg -match '(?i)(?:has\\s+failed|backup\\s+failed|with\\s+errors?|\\bjob\\s+failed)') { $result = 'Failed' } "

            # Event-ID fallback only if message text was ambiguous
            "  if (-not $result) { "
            "    if     ($e.Id -in @(110, 111))                       { $result = 'Success' } "
            "    elseif ($e.Id -in @(190))                             { $result = 'Warning' } "
            "    elseif ($e.Id -in @(191, 192, 193, 194))             { $result = 'Failed' } "
            "  }; "
            "  if (-not $result) { continue }; "

            # Keep the most recent terminal event for each job
            "  if (-not $jobMap.ContainsKey($jobName) -or $e.TimeCreated -gt $jobMap[$jobName].TimeRaw) { "
            "    $jobMap[$jobName] = @{ "
            "      Name = $jobName; "
            "      Result = $result; "
            "      TimeRaw = $e.TimeCreated; "
            "      EndTs = [int][DateTimeOffset]::new($e.TimeCreated).ToUnixTimeSeconds(); "
            "      Source = $e.ProviderName; "
            "      EventId = $e.Id "
            "    } "
            "  } "
            "}; "
            "$out = @(); "
            "foreach ($k in $jobMap.Keys) { "
            "  $j = $jobMap[$k]; "
            "  $out += [PSCustomObject]@{ "
            "    Name = $j.Name; "
            "    Type = if ($j.Source -like '*Endpoint*') { 'Agent' } else { 'Backup' }; "
            "    Result = $j.Result; "
            "    State = 'Stopped'; "
            "    StartTs = 0; "
            "    EndTs = $j.EndTs; "
            "    DurationSec = 0; "
            "    ScheduleEnabled = 1 "
            "  } "
            "}; "
            "Write-Output (@{Jobs=$out; Diag=($diagParts -join '; ')} | ConvertTo-Json -Compress -Depth 4)"
        )
        data = ps_json(script, timeout=60)
        if not isinstance(data, dict):
            return [], "EventLog scrape returned no parseable output"
        jobs = _ensure_list(data.get("Jobs"))
        diag = data.get("Diag", "") or ""
        if jobs:
            logger.info("Veeam: event-log fallback found %d job(s) (%s)", len(jobs), diag[:200])
        return jobs, diag

    # ── Windows Server Backup ────────────────────────────────────────────────

    def _collect_windows_backup(self, conn, ts):
        """Scan Windows Server Backup state.

        Strategy:
          1. Try Get-WBSummary (cleanest, most info) via WindowsServerBackup module.
          2. Fall back to scanning the Microsoft-Windows-Backup event log.
        """
        script = (
            "$out = @{Status='unknown'; FeatureInstalled=0; Source=''; Error=$null}; "
            # Method 1: WindowsServerBackup module
            "try { "
            "  Import-Module WindowsServerBackup -ErrorAction Stop; "
            "  $out.FeatureInstalled = 1; "
            "  try { "
            "    $sum = Get-WBSummary -ErrorAction Stop; "
            "    if ($sum.LastBackupTime -and $sum.LastBackupTime.Year -gt 1) { "
            "      $out.LastBackupTs = [int][DateTimeOffset]::new($sum.LastBackupTime).ToUnixTimeSeconds() "
            "    } else { $out.LastBackupTs = 0 }; "
            "    if ($sum.LastSuccessfulBackupTime -and $sum.LastSuccessfulBackupTime.Year -gt 1) { "
            "      $out.LastSuccessTs = [int][DateTimeOffset]::new($sum.LastSuccessfulBackupTime).ToUnixTimeSeconds() "
            "    } else { $out.LastSuccessTs = 0 }; "
            "    if ($sum.NextBackupTime -and $sum.NextBackupTime.Year -gt 1) { "
            "      $out.NextBackupTs = [int][DateTimeOffset]::new($sum.NextBackupTime).ToUnixTimeSeconds() "
            "    } else { $out.NextBackupTs = 0 }; "
            "    $out.Versions = [int]$sum.NumberOfVersions; "
            "    $out.TargetLabel = if ($sum.LastSuccessfulBackupTargetLabel) { \"$($sum.LastSuccessfulBackupTargetLabel)\" } else { '' }; "
            "    $out.LastResultHR = [int]$sum.LastBackupResultHR; "
            "    if ($sum.LastBackupResultHR -eq 0) { $out.LastResult = 'Success' } "
            "    elseif ($out.LastBackupTs -eq 0)    { $out.LastResult = 'NeverRan' } "
            "    else                                  { $out.LastResult = 'Failed' }; "
            "    $out.Status = 'ok'; $out.Source = 'WindowsServerBackup module' "
            "  } catch { $out.Status = 'error'; $out.Error = $_.Exception.Message } "
            "} catch { "
            # Method 2: Event log fallback
            "  $out.FeatureInstalled = 0; "
            "  try { "
            "    $evs = Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Backup'; StartTime=(Get-Date).AddDays(-60)} "
            "           -MaxEvents 200 -ErrorAction Stop; "
            "    if ($evs) { "
            "      $lastAny     = $evs | Sort-Object TimeCreated -Descending | Select-Object -First 1; "
            "      $lastSuccess = $evs | Where-Object { $_.Id -eq 4 } | Sort-Object TimeCreated -Descending | Select-Object -First 1; "
            "      $out.LastBackupTs  = [int][DateTimeOffset]::new($lastAny.TimeCreated).ToUnixTimeSeconds(); "
            "      $out.LastSuccessTs = if ($lastSuccess) { [int][DateTimeOffset]::new($lastSuccess.TimeCreated).ToUnixTimeSeconds() } else { 0 }; "
            "      switch ($lastAny.Id) { "
            "        4       { $out.LastResult = 'Success' } "
            "        5       { $out.LastResult = 'Failed' } "
            "        14      { $out.LastResult = 'Warning' } "
            "        17      { $out.LastResult = 'InProgress' } "
            "        default { $out.LastResult = \"Event $($lastAny.Id)\" } "
            "      }; "
            "      $out.Status = 'eventlog'; $out.Source = 'Event Log (Microsoft-Windows-Backup)' "
            "    } else { "
            "      $out.Status = 'no_data'; $out.Source = 'Event Log (no entries)' "
            "    } "
            "  } catch { "
            "    $out.Status = 'not_installed'; $out.Error = $_.Exception.Message "
            "  } "
            "}; "
            "$out | ConvertTo-Json -Compress -Depth 3"
        )
        data = ps_json(script, timeout=45)
        if not isinstance(data, dict):
            conn.execute(
                """UPDATE windows_backup_status SET
                    last_check_ts=?, status=?, source=?, feature_installed=?,
                    error_message=? WHERE id=1""",
                (ts, "error", None, 0, "Could not parse PowerShell output"),
            )
            return

        conn.execute(
            """UPDATE windows_backup_status SET
                last_check_ts=?, status=?, source=?, feature_installed=?,
                last_backup_ts=?, last_success_ts=?, next_backup_ts=?,
                last_result=?, last_result_hr=?, versions=?, target_label=?,
                error_message=? WHERE id=1""",
            (
                ts,
                data.get("Status") or "unknown",
                data.get("Source") or "",
                int(data.get("FeatureInstalled") or 0),
                data.get("LastBackupTs") or None,
                data.get("LastSuccessTs") or None,
                data.get("NextBackupTs") or None,
                data.get("LastResult") or None,
                data.get("LastResultHR"),
                data.get("Versions") or None,
                data.get("TargetLabel") or None,
                data.get("Error") or None,
            ),
        )

        status = data.get("Status", "unknown")
        if status in ("ok", "eventlog"):
            logger.info(
                "Windows Backup: %s via %s, last=%s, result=%s",
                status, data.get("Source"),
                data.get("LastBackupTs"), data.get("LastResult"),
            )
        elif status == "not_installed":
            logger.info("Windows Backup: feature not installed")
        else:
            logger.warning("Windows Backup: %s — %s", status, (data.get("Error") or "")[:200])
