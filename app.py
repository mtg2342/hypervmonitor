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


@app.route("/api/update/apply", methods=["POST"])
def update_apply():
    """Spawn a detached process that re-runs deploy.ps1 (stops us, pulls, restarts)."""
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        return jsonify({"ok": False, "error": "Not a git checkout — re-run deploy.ps1 manually."})

    # Set HVM_AUTO=1 so deploy.ps1 skips any interactive prompts.
    # Sleep first so this HTTP handler can respond before we get killed.
    ps_cmd = (
        "$env:HVM_AUTO=1; "
        "Start-Sleep -Seconds 3; "
        f"iex (irm '{RAW_DEPLOY}')"
    )
    try:
        DETACHED_PROCESS       = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-NonInteractive", "-Command", ps_cmd],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    logger.info("Update apply requested — restart imminent")
    return jsonify({"ok": True, "message": "Update started. Dashboard will restart in ~10 seconds."})


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


if __name__ == "__main__":
    init_db()
    _start_collector()
    logger.info("Starting Hyper-V Monitor at http://%s:%d", FLASK_HOST, FLASK_PORT)
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)
