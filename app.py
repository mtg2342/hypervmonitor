import os
import time
import threading
import subprocess
import logging
from flask import Flask, jsonify, request, render_template
from db import init_db, get_connection
from collector import MetricCollector
from alerts import evaluate_alerts, get_settings, get_default_settings, save_settings
from config import FLASK_HOST, FLASK_PORT, RANGE_SECONDS, RANGE_SOURCE, DB_PATH

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DEPLOY = "https://raw.githubusercontent.com/mtg2342/hypervmonitor/main/deploy.ps1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", template_folder="templates")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ts_cutoff(range_key):
    return time.time() - RANGE_SECONDS.get(range_key, 3600)


def _rows_to_dicts(rows):
    return [dict(r) for r in rows]


def _range_source(range_key):
    return RANGE_SOURCE.get(range_key, ("raw", None))


def _query_host_history(range_key):
    """Return [{ts, cpu_pct, mem_pct, disk_read_bps, disk_write_bps}, ...]."""
    cutoff = _ts_cutoff(range_key)
    source, bucket = _range_source(range_key)

    conn = get_connection()
    try:
        if source == "raw":
            if bucket:
                sql = f"""
                    SELECT CAST(ts / {bucket} AS INTEGER) * {bucket} AS ts,
                           AVG(cpu_pct) AS cpu_pct,
                           AVG(mem_pct) AS mem_pct,
                           AVG(disk_read_bps) AS disk_read_bps,
                           AVG(disk_write_bps) AS disk_write_bps
                    FROM host_metrics WHERE ts > ?
                    GROUP BY CAST(ts / {bucket} AS INTEGER) ORDER BY ts
                """
            else:
                sql = """
                    SELECT ts, cpu_pct, mem_pct, disk_read_bps, disk_write_bps
                    FROM host_metrics WHERE ts > ? ORDER BY ts
                """
            rows = conn.execute(sql, (cutoff,)).fetchall()
        else:
            table = "host_metrics_hourly" if source == "hourly" else "host_metrics_daily"
            sql = f"""
                SELECT bucket_ts AS ts,
                       cpu_pct_avg AS cpu_pct,
                       mem_pct_avg AS mem_pct,
                       disk_read_avg AS disk_read_bps,
                       disk_write_avg AS disk_write_bps
                FROM {table} WHERE bucket_ts > ? ORDER BY bucket_ts
            """
            rows = conn.execute(sql, (cutoff,)).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def _query_vms_history(range_key, vm_name=None):
    cutoff = _ts_cutoff(range_key)
    source, bucket = _range_source(range_key)

    conn = get_connection()
    try:
        if source == "raw":
            base_cols = ("cpu_usage", "mem_assigned", "mem_demand",
                         "net_sent_bps", "net_recv_bps",
                         "disk_read_bps", "disk_write_bps")
            if bucket:
                avg_cols = ", ".join(f"AVG({c}) AS {c}" for c in base_cols)
                where = "WHERE ts > ?" + (" AND vm_name = ?" if vm_name else "")
                params = (cutoff,) + ((vm_name,) if vm_name else ())
                sql = f"""
                    SELECT CAST(ts / {bucket} AS INTEGER) * {bucket} AS ts,
                           vm_name, {avg_cols}
                    FROM vm_metrics {where}
                    GROUP BY vm_name, CAST(ts / {bucket} AS INTEGER)
                    ORDER BY ts
                """
                rows = conn.execute(sql, params).fetchall()
            else:
                col_list = ", ".join(base_cols)
                where = "WHERE ts > ?" + (" AND vm_name = ?" if vm_name else "")
                params = (cutoff,) + ((vm_name,) if vm_name else ())
                sql = f"SELECT ts, vm_name, {col_list} FROM vm_metrics {where} ORDER BY ts"
                rows = conn.execute(sql, params).fetchall()
        else:
            table = "vm_metrics_hourly" if source == "hourly" else "vm_metrics_daily"
            where = "WHERE bucket_ts > ?" + (" AND vm_name = ?" if vm_name else "")
            params = (cutoff,) + ((vm_name,) if vm_name else ())
            sql = f"""
                SELECT bucket_ts AS ts, vm_name,
                       cpu_usage_avg AS cpu_usage,
                       mem_assigned_avg AS mem_assigned,
                       mem_demand_avg AS mem_demand,
                       net_sent_avg AS net_sent_bps,
                       net_recv_avg AS net_recv_bps,
                       disk_read_avg AS disk_read_bps,
                       disk_write_avg AS disk_write_bps
                FROM {table} {where} ORDER BY bucket_ts
            """
            rows = conn.execute(sql, params).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/host/current")
def host_current():
    conn = get_connection()
    try:
        metrics = conn.execute(
            "SELECT * FROM host_metrics ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        volumes = conn.execute(
            "SELECT * FROM host_volumes WHERE ts = (SELECT MAX(ts) FROM host_volumes)"
        ).fetchall()
        return jsonify({
            "metrics": dict(metrics) if metrics else None,
            "volumes": _rows_to_dicts(volumes),
        })
    finally:
        conn.close()


@app.route("/api/host/history")
def host_history():
    range_key = request.args.get("range", "1h")
    return jsonify(_query_host_history(range_key))


@app.route("/api/vms/current")
def vms_current():
    conn = get_connection()
    try:
        max_ts = conn.execute("SELECT MAX(ts) FROM vm_metrics").fetchone()[0]
        if max_ts is None:
            return jsonify([])
        vms = conn.execute(
            "SELECT * FROM vm_metrics WHERE ts = ?", (max_ts,)
        ).fetchall()
        return jsonify(_rows_to_dicts(vms))
    finally:
        conn.close()


@app.route("/api/vms/history")
def vms_history():
    range_key = request.args.get("range", "1h")
    vm_name = request.args.get("vm")
    return jsonify(_query_vms_history(range_key, vm_name))


@app.route("/api/veeam/backups")
def veeam_backups():
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT job_name, job_type, last_result, last_state, last_start_ts,
                      last_end_ts, duration_sec, schedule_enabled, seen_ts
               FROM veeam_backups
               ORDER BY COALESCE(last_end_ts, 0) DESC, job_name"""
        ).fetchall()
        jobs = _rows_to_dicts(rows)

        # Summary counts for the dashboard badge
        counts = {"success": 0, "warning": 0, "failed": 0, "never": 0, "running": 0, "other": 0}
        for j in jobs:
            r = (j.get("last_result") or "").lower()
            if   r == "success": counts["success"] += 1
            elif r == "warning": counts["warning"] += 1
            elif r == "failed":  counts["failed"]  += 1
            elif r == "running": counts["running"] += 1
            elif r in ("none", "neverran", ""): counts["never"] += 1
            else:                 counts["other"]   += 1

        status_row = conn.execute("SELECT * FROM veeam_status WHERE id=1").fetchone()
        status = dict(status_row) if status_row else {}

        return jsonify({
            "jobs": jobs,
            "counts": counts,
            "total": len(jobs),
            "status": status,
        })
    finally:
        conn.close()


@app.route("/api/veeam/refresh", methods=["POST"])
def veeam_refresh():
    """Trigger an immediate Veeam re-poll from the dashboard refresh button."""
    if app.config.get("_collector_instance"):
        try:
            import time as _time
            conn = get_connection()
            try:
                app.config["_collector_instance"]._collect_veeam(conn, _time.time())
                conn.commit()
            finally:
                conn.close()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": False, "error": "Collector not running"})


# ── Auto-update toggle ────────────────────────────────────────────────────────

AUTO_UPDATE_TASK = "HyperVMonitorAutoUpdate"


def _auto_update_task_exists():
    try:
        r = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", AUTO_UPDATE_TASK],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


@app.route("/api/autoupdate/status")
def autoupdate_status():
    return jsonify({"enabled": _auto_update_task_exists(), "task": AUTO_UPDATE_TASK})


@app.route("/api/autoupdate/enable", methods=["POST"])
def autoupdate_enable():
    """Create the daily 3:30 AM Task Scheduler entry that runs apply-update.ps1."""
    if _auto_update_task_exists():
        return jsonify({"ok": True, "enabled": True, "message": "Already enabled."})
    script_path = os.path.join(BASE_DIR, "apply-update.ps1")
    if not os.path.exists(script_path):
        return jsonify({"ok": False, "error": "apply-update.ps1 not found in install directory"})
    arg = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script_path}"'
    script = (
        f"$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '{arg}'; "
        "$trigger = New-ScheduledTaskTrigger -Daily -At 3:30am; "
        "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; "
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30); "
        f"Register-ScheduledTask -TaskName '{AUTO_UPDATE_TASK}' -Action $action -Trigger $trigger "
        "-Principal $principal -Settings $settings -Force | Out-Null; "
        "'ok'"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return jsonify({"ok": False, "error": (r.stderr or "schtask register failed")[:500]})
        return jsonify({"ok": True, "enabled": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/autoupdate/disable", methods=["POST"])
def autoupdate_disable():
    if not _auto_update_task_exists():
        return jsonify({"ok": True, "enabled": False, "message": "Already disabled."})
    try:
        r = subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", AUTO_UPDATE_TASK, "/F"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return jsonify({"ok": False, "error": (r.stderr or "schtask delete failed")[:500]})
        return jsonify({"ok": True, "enabled": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/vms/bandwidth")
def vms_bandwidth():
    """Per-VM network traffic totals over the last N days.

    Strategy: hourly aggregates cover everything from N days ago through the
    most recently completed hour. Raw 30-second samples fill in the current
    in-progress hour (raw data is kept for 48h, so this works for any N).
    Each hourly row contributes  avg_rate * 3600 bytes; each raw row
    contributes  rate * 30 bytes.
    """
    days = max(1, min(request.args.get("days", 30, type=int), 365))
    now = time.time()
    cutoff = now - (days * 86400)

    conn = get_connection()
    try:
        totals = {}

        # Hourly aggregates → bytes per hour
        hourly_rows = conn.execute(
            """SELECT vm_name,
                      SUM(COALESCE(net_sent_avg, 0)) * 3600 AS sent,
                      SUM(COALESCE(net_recv_avg, 0)) * 3600 AS recv
               FROM vm_metrics_hourly
               WHERE bucket_ts >= ?
               GROUP BY vm_name""",
            (cutoff,),
        ).fetchall()
        for r in hourly_rows:
            totals[r["vm_name"]] = {
                "sent_bytes": r["sent"] or 0,
                "recv_bytes": r["recv"] or 0,
            }

        # Raw data after the last completed hourly bucket (current partial hour)
        last_hour = conn.execute(
            "SELECT COALESCE(MAX(bucket_ts), 0) + 3600 FROM vm_metrics_hourly"
        ).fetchone()[0]
        raw_start = max(last_hour or 0, cutoff)
        raw_rows = conn.execute(
            """SELECT vm_name,
                      SUM(COALESCE(net_sent_bps, 0)) * 30 AS sent,
                      SUM(COALESCE(net_recv_bps, 0)) * 30 AS recv
               FROM vm_metrics
               WHERE ts >= ?
               GROUP BY vm_name""",
            (raw_start,),
        ).fetchall()
        for r in raw_rows:
            d = totals.setdefault(r["vm_name"], {"sent_bytes": 0, "recv_bytes": 0})
            d["sent_bytes"] += r["sent"] or 0
            d["recv_bytes"] += r["recv"] or 0

        vms = [
            {
                "vm_name":   name,
                "sent_bytes": v["sent_bytes"],
                "recv_bytes": v["recv_bytes"],
                "total_bytes": v["sent_bytes"] + v["recv_bytes"],
            }
            for name, v in totals.items()
        ]
        vms.sort(key=lambda x: x["total_bytes"], reverse=True)

        return jsonify({
            "days": days,
            "from_ts": cutoff,
            "total_bytes": sum(v["total_bytes"] for v in vms),
            "total_sent_bytes": sum(v["sent_bytes"] for v in vms),
            "total_recv_bytes": sum(v["recv_bytes"] for v in vms),
            "vms": vms,
        })
    finally:
        conn.close()


@app.route("/api/vhd/current")
def vhd_current():
    conn = get_connection()
    try:
        max_ts = conn.execute("SELECT MAX(ts) FROM vhd_info").fetchone()[0]
        if max_ts is None:
            return jsonify([])
        rows = conn.execute("SELECT * FROM vhd_info WHERE ts = ?", (max_ts,)).fetchall()
        return jsonify(_rows_to_dicts(rows))
    finally:
        conn.close()


@app.route("/api/system/info")
def system_info():
    conn = get_connection()
    try:
        info = conn.execute("SELECT * FROM system_info WHERE id=1").fetchone()
        reboots = conn.execute(
            "SELECT ts_boot, reason FROM reboot_history ORDER BY ts_boot DESC LIMIT 10"
        ).fetchall()
        return jsonify({
            "info": dict(info) if info else None,
            "reboots": _rows_to_dicts(reboots),
        })
    finally:
        conn.close()


@app.route("/api/system/events")
def system_events():
    limit = request.args.get("limit", 50, type=int)
    hours = request.args.get("hours", 24, type=int)
    cutoff = time.time() - (hours * 3600)
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM system_events
               WHERE ts_event > ? AND level IN (1, 2)
               ORDER BY ts_event DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return jsonify(_rows_to_dicts(rows))
    finally:
        conn.close()


@app.route("/api/system/updates")
def system_updates():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT title, severity, kb FROM pending_updates ORDER BY severity, title"
        ).fetchall()
        return jsonify(_rows_to_dicts(rows))
    finally:
        conn.close()


@app.route("/api/security/status")
def security_status():
    import json as _json
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM security_status WHERE id=1").fetchone()
        sysinfo = conn.execute(
            "SELECT updates_pending FROM system_info WHERE id=1"
        ).fetchone()
        d = dict(row) if row else {}
        try:
            d["findings"] = _json.loads(d.get("findings_json") or "[]")
        except Exception:
            d["findings"] = []
        d.pop("findings_json", None)
        d["updates_pending"] = (sysinfo["updates_pending"] if sysinfo else None)
        return jsonify(d)
    finally:
        conn.close()


@app.route("/api/settings", methods=["GET"])
def settings_get():
    conn = get_connection()
    try:
        eff = get_settings(conn)
        defaults = get_default_settings()
        overrides = {r["key"]: r["value"]
                     for r in conn.execute("SELECT key, value FROM app_settings").fetchall()}
        return jsonify({
            "effective": eff,
            "defaults":  defaults,
            "overrides": overrides,
        })
    finally:
        conn.close()


@app.route("/api/settings", methods=["POST"])
def settings_save():
    payload = request.get_json(silent=True) or {}
    updates = payload.get("settings") or {}
    if not isinstance(updates, dict):
        return jsonify({"ok": False, "error": "settings must be an object"}), 400
    conn = get_connection()
    try:
        saved, errors = save_settings(conn, updates)
        return jsonify({
            "ok": True,
            "saved": saved,
            "errors": errors,
            "effective": get_settings(conn),
        })
    finally:
        conn.close()


@app.route("/api/settings/reset", methods=["POST"])
def settings_reset():
    conn = get_connection()
    try:
        conn.execute("DELETE FROM app_settings")
        conn.commit()
        return jsonify({"ok": True, "effective": get_settings(conn)})
    finally:
        conn.close()


# ── Update self ──────────────────────────────────────────────────────────────

def _git(args, timeout=15):
    try:
        return subprocess.run(
            ["git", "-C", BASE_DIR, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None


@app.route("/api/update/info")
def update_info():
    """Return info about the currently-deployed commit."""
    r = _git(["log", "-1", "--format=%h%n%H%n%s%n%cI%n%cr"])
    if r is None or r.returncode != 0:
        return jsonify({"ok": False, "error": "Not a git checkout (or git not on PATH)."})
    parts = r.stdout.rstrip("\n").split("\n")
    return jsonify({
        "ok": True,
        "short":    parts[0] if len(parts) > 0 else "",
        "full":     parts[1] if len(parts) > 1 else "",
        "subject":  parts[2] if len(parts) > 2 else "",
        "date":     parts[3] if len(parts) > 3 else "",
        "relative": parts[4] if len(parts) > 4 else "",
    })


@app.route("/api/update/check", methods=["POST"])
def update_check():
    """git fetch and return any incoming commits."""
    r = _git(["fetch", "origin", "--quiet"], timeout=30)
    if r is None:
        return jsonify({"ok": False, "error": "git not available or timed out"})
    if r.returncode != 0:
        return jsonify({"ok": False, "error": (r.stderr or "git fetch failed")[:500]})

    log = _git(["log", "--format=%h|%s|%cr", "HEAD..origin/main"])
    if log is None or log.returncode != 0:
        return jsonify({"ok": False, "error": "git log failed"})

    commits = []
    for line in log.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0], "subject": parts[1], "relative": parts[2]})
    return jsonify({
        "ok": True,
        "up_to_date": len(commits) == 0,
        "count": len(commits),
        "commits": commits[:25],
    })


APPLY_UPDATE_TASK = "HyperVMonitorApplyUpdate"


def _apply_update_task_exists():
    try:
        r = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", APPLY_UPDATE_TASK],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _register_apply_update_task():
    """Idempotently create the HyperVMonitorApplyUpdate task.

    The task has a far-future trigger so it never fires on its own — we only
    invoke it via `schtasks /Run`. It launches apply-update.ps1 as SYSTEM,
    fully independent of the Python process that triggered it.
    """
    script_path = os.path.join(BASE_DIR, "apply-update.ps1")
    if not os.path.exists(script_path):
        return False, f"apply-update.ps1 not found at {script_path}"

    if _apply_update_task_exists():
        return True, "exists"

    arg = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script_path}"'
    register_script = (
        f"$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '{arg}'; "
        "$trigger = New-ScheduledTaskTrigger -Once -At '2099-12-31T23:59'; "
        "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; "
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30); "
        f"Register-ScheduledTask -TaskName '{APPLY_UPDATE_TASK}' -Action $action -Trigger $trigger "
        "-Principal $principal -Settings $settings -Force | Out-Null"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", register_script],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return False, (r.stderr or "Register-ScheduledTask failed")[:500]
        return True, "created"
    except Exception as e:
        return False, str(e)


@app.route("/api/update/apply", methods=["POST"])
def update_apply():
    """Run apply-update.ps1 via Task Scheduler so the spawned process survives
    after we kill ourselves during the update."""
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        return jsonify({"ok": False, "error": "Not a git checkout — re-run deploy.ps1 manually."})

    ok, msg = _register_apply_update_task()
    if not ok:
        return jsonify({"ok": False, "error": f"Could not prepare update task: {msg}"})

    try:
        r = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", APPLY_UPDATE_TASK],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return jsonify({"ok": False, "error": (r.stderr or "schtasks /Run failed")[:500]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    logger.info("Update apply triggered via Task Scheduler (%s)", APPLY_UPDATE_TASK)
    return jsonify({
        "ok": True,
        "message": "Update started via Task Scheduler. Dashboard will restart in ~10 seconds.",
        "task_action": msg,  # "created" or "exists"
    })


@app.route("/api/security/rdp")
def security_rdp():
    """RDP login history. Query: status=all|success|failed, limit, offset."""
    status = (request.args.get("status", "all") or "all").lower()
    limit  = min(request.args.get("limit", 200, type=int), 2000)
    offset = max(request.args.get("offset",  0, type=int), 0)

    where = []
    if status == "success":
        where.append("success = 1")
    elif status == "failed":
        where.append("success = 0")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM rdp_logins {where_sql} ORDER BY ts_event DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM rdp_logins {where_sql}"
        ).fetchone()[0]
        return jsonify({
            "total": total,
            "limit": limit,
            "offset": offset,
            "rows": _rows_to_dicts(rows),
        })
    finally:
        conn.close()


@app.route("/api/alerts/active")
def alerts_active():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE ts_cleared IS NULL ORDER BY ts_raised DESC"
        ).fetchall()
        return jsonify(_rows_to_dicts(rows))
    finally:
        conn.close()


@app.route("/api/alerts/<int:alert_id>/dismiss", methods=["POST"])
def alert_dismiss(alert_id):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE alerts SET ts_cleared = ? WHERE id = ? AND ts_cleared IS NULL",
            (time.time(), alert_id),
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/alerts/history")
def alerts_history():
    """Full alert log: active + cleared. Supports filtering and pagination.

    Query params:
        status   = 'all' (default), 'active', or 'cleared'
        severity = 'all' (default), 'warning', or 'critical'
        target   = exact match (optional)
        limit    = max rows (default 500, capped at 5000)
        offset   = pagination offset (default 0)
    """
    status   = (request.args.get("status",   "all") or "all").lower()
    severity = (request.args.get("severity", "all") or "all").lower()
    target   = request.args.get("target")
    limit    = min(request.args.get("limit",  500, type=int), 5000)
    offset   = max(request.args.get("offset",   0, type=int), 0)

    where = []
    params = []
    if status == "active":
        where.append("ts_cleared IS NULL")
    elif status == "cleared":
        where.append("ts_cleared IS NOT NULL")
    if severity in ("warning", "critical"):
        where.append("severity = ?")
        params.append(severity)
    if target:
        where.append("target = ?")
        params.append(target)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM alerts {where_sql} ORDER BY ts_raised DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM alerts {where_sql}", tuple(params)
        ).fetchone()[0]
        return jsonify({
            "total": total,
            "limit": limit,
            "offset": offset,
            "rows": _rows_to_dicts(rows),
        })
    finally:
        conn.close()


# ── Startup ──────────────────────────────────────────────────────────────────

def _start_collector():
    collector = MetricCollector(DB_PATH)
    app.config["_collector_instance"] = collector
    collector_thread = threading.Thread(target=collector.run, daemon=True, name="collector")
    collector_thread.start()
    logger.info("Metric collector started (polling every %ds)", 30)

    def _alert_loop():
        import time as _time
        while True:
            _time.sleep(30)
            try:
                conn = get_connection()
                evaluate_alerts(conn, _time.time())
                conn.commit()
                conn.close()
            except Exception:
                logger.exception("Alert evaluation failed")

    alert_thread = threading.Thread(target=_alert_loop, daemon=True, name="alerts")
    alert_thread.start()


def _quiet_werkzeug_request_logs():
    """The dev server logs every request; cut it down to warnings+."""
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


if __name__ == "__main__":
    init_db()
    _start_collector()
    _quiet_werkzeug_request_logs()
    logger.info("Starting Hyper-V Monitor at http://%s:%d", FLASK_HOST, FLASK_PORT)

    # Prefer waitress (production WSGI server, Windows-friendly, no warning spam).
    # Fall back to Flask's dev server if waitress isn't installed yet — keeps
    # the first run after a deps-upgrade working.
    try:
        from waitress import serve
        logger.info("Using waitress WSGI server")
        serve(
            app,
            host=FLASK_HOST,
            port=FLASK_PORT,
            threads=8,
            ident="Hyper-V Monitor",
            _quiet=True,
        )
    except ImportError:
        logger.warning("waitress not installed; falling back to Flask dev server "
                       "(run: pip install waitress)")
        app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, threaded=True)
