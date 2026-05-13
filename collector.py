import subprocess
import json
import time
import threading
import logging
from db import get_connection, purge_old_data, rollup_aggregates
from config import (
    POLL_INTERVAL, VHD_POLL_MULTIPLE, PURGE_CHECK_MULTIPLE,
    SYSINFO_POLL_MULTIPLE, EVENTLOG_POLL_MULTIPLE, UPDATES_POLL_MULTIPLE,
    ROLLUP_POLL_MULTIPLE, SECURITY_POLL_MULTIPLE,
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
        # Step 1: VM basic info via Get-VM. Heartbeat is wrapped in try/catch
        # because $_.Heartbeat can throw for VMs without integration services.
        script_vm = (
            "Get-VM | Select-Object Name, State, CPUUsage, MemoryAssigned, MemoryDemand, "
            "@{N='UptimeSec';E={try{if($_.Uptime){$_.Uptime.TotalSeconds}else{0}}catch{0}}}, "
            "@{N='Heartbeat';E={"
            "try{"
            "  if ($_.State -ne 'Running') {'N/A'}"
            "  else { "
            "    $h = $_.Heartbeat; "
            "    if ($h -ne $null) { $h.ToString() } else { 'NoIntegration' }"
            "  }"
            "} catch { 'Unknown' }"
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
            state_val = vm.get("State")
            if isinstance(state_val, int):
                state_map = {2: "Running", 3: "Off", 6: "Saved", 9: "Paused"}
                state = state_map.get(state_val, str(state_val))
            else:
                state = str(state_val) if state_val else "Unknown"

            vm_data[name] = {
                "state": state,
                "cpu_usage":      vm.get("CPUUsage"),
                "mem_assigned":   vm.get("MemoryAssigned"),
                "mem_demand":     vm.get("MemoryDemand"),
                "uptime_sec":     vm.get("UptimeSec"),
                "heartbeat":      vm.get("Heartbeat"),
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
                    disk_read_bps, disk_write_bps)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts, name, d["state"],
                    d["cpu_usage"], d["mem_assigned"], d["mem_demand"],
                    d["uptime_sec"], d["heartbeat"],
                    d["net_sent_bps"] or None,
                    d["net_recv_bps"] or None,
                    d["disk_read_bps"] or None,
                    d["disk_write_bps"] or None,
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

        conn.execute(
            """INSERT INTO host_metrics
               (ts, cpu_pct, mem_total, mem_avail, mem_pct, disk_read_bps, disk_write_bps)
               VALUES (?,?,?,?,?,?,?)""",
            (
                ts,
                vals.get("cpu"),
                mem_total,
                vals.get("mem_avail"),
                vals.get("mem_pct"),
                vals.get("disk_r"),
                vals.get("disk_w"),
            ),
        )

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

        bitlocker = data.get("BitLocker", "")
        if bitlocker.startswith("Off"):
            findings.append({
                "severity": "medium",
                "title": "BitLocker is not enabled on any volume",
                "detail": "Encrypting the system drive protects data if the host disks are removed.",
            })

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
