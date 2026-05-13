import subprocess
import json
import time
import threading
import logging
from db import get_connection, purge_old_data
from config import (
    POLL_INTERVAL, VHD_POLL_MULTIPLE, PURGE_CHECK_MULTIPLE,
    SYSINFO_POLL_MULTIPLE, EVENTLOG_POLL_MULTIPLE, UPDATES_POLL_MULTIPLE,
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
            if self._poll_count % VHD_POLL_MULTIPLE == 0:
                self._collect_vhd(conn, ts)
            if self._poll_count == 1 or self._poll_count % UPDATES_POLL_MULTIPLE == 0:
                self._collect_pending_updates(conn, ts)
            if self._poll_count % PURGE_CHECK_MULTIPLE == 0:
                purge_old_data(conn)
            conn.commit()
        finally:
            conn.close()
        logger.debug("Poll #%d completed in %.1fs", self._poll_count, time.time() - ts)

    def _collect_vm_and_network(self, conn, ts):
        script = (
            "$vms = Get-VM | Select-Object Name, State, CPUUsage, MemoryAssigned, "
            "MemoryDemand, @{N='UptimeSec';E={$_.Uptime.TotalSeconds}}, "
            "@{N='Heartbeat';E={if($_.Heartbeat){$_.Heartbeat.ToString()}else{'N/A'}}}\n"
            "$adapters = Get-VMNetworkAdapter -VM (Get-VM) -ErrorAction SilentlyContinue | "
            "Select-Object VMName, Name, "
            "@{N='SentBytes';E={if($_.BytesSent){$_.BytesSent}else{0}}}, "
            "@{N='RecvBytes';E={if($_.BytesReceived){$_.BytesReceived}else{0}}}\n"
            "@{VMs=$vms; Adapters=$adapters} | ConvertTo-Json -Depth 3 -Compress"
        )
        data = ps_json(script)
        if not data:
            return

        vms = _ensure_list(data.get("VMs"))
        adapters = _ensure_list(data.get("Adapters"))

        adapter_by_vm = {}
        for a in adapters:
            vm_name = a.get("VMName", "")
            key = (vm_name, a.get("Name", "default"))
            sent = a.get("SentBytes", 0) or 0
            recv = a.get("RecvBytes", 0) or 0

            sent_bps = None
            recv_bps = None
            if key in self._prev_net:
                prev_sent, prev_recv, prev_ts = self._prev_net[key]
                elapsed = ts - prev_ts
                if elapsed > 0:
                    d_sent = sent - prev_sent
                    d_recv = recv - prev_recv
                    if d_sent < 0:
                        d_sent = 0
                    if d_recv < 0:
                        d_recv = 0
                    sent_bps = d_sent / elapsed
                    recv_bps = d_recv / elapsed

            self._prev_net[key] = (sent, recv, ts)

            if vm_name not in adapter_by_vm:
                adapter_by_vm[vm_name] = {"sent_bps": 0, "recv_bps": 0}
            if sent_bps is not None:
                adapter_by_vm[vm_name]["sent_bps"] += sent_bps
                adapter_by_vm[vm_name]["recv_bps"] += recv_bps

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

            net = adapter_by_vm.get(name, {})
            conn.execute(
                """INSERT INTO vm_metrics
                   (ts, vm_name, state, cpu_usage, mem_assigned, mem_demand,
                    uptime_sec, heartbeat, net_sent_bps, net_recv_bps,
                    disk_read_bps, disk_write_bps)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts, name, state,
                    vm.get("CPUUsage"),
                    vm.get("MemoryAssigned"),
                    vm.get("MemoryDemand"),
                    vm.get("UptimeSec"),
                    vm.get("Heartbeat"),
                    net.get("sent_bps"),
                    net.get("recv_bps"),
                    None, None,
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

        script_disk_io = (
            "try { Get-Counter "
            "'\\Hyper-V Virtual Storage Device(*)\\Read Bytes/sec',"
            "'\\Hyper-V Virtual Storage Device(*)\\Write Bytes/sec' "
            "-SampleInterval 1 -MaxSamples 1 -ErrorAction Stop | "
            "ForEach-Object { $_.CounterSamples | Select-Object Path, InstanceName, CookedValue } | "
            "ConvertTo-Json -Compress } catch { '[]' }"
        )
        disk_data = _ensure_list(ps_json(script_disk_io))
        if not disk_data:
            return

        vm_disk = {}
        for sample in disk_data:
            instance = sample.get("InstanceName", "")
            path = (sample.get("Path") or "").lower()
            val = sample.get("CookedValue", 0)
            vm_name = instance.split(":")[0] if ":" in instance else instance
            if not vm_name or vm_name == "_total":
                continue
            if vm_name not in vm_disk:
                vm_disk[vm_name] = {"read": 0, "write": 0}
            if "read bytes" in path:
                vm_disk[vm_name]["read"] += val
            elif "write bytes" in path:
                vm_disk[vm_name]["write"] += val

        for vm_name, io in vm_disk.items():
            conn.execute(
                """UPDATE vm_metrics SET disk_read_bps=?, disk_write_bps=?
                   WHERE ts=? AND vm_name=?""",
                (io["read"], io["write"], ts, vm_name),
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
