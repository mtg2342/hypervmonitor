import time
import threading
import logging
from flask import Flask, jsonify, request, render_template
from db import init_db, get_connection
from collector import MetricCollector
from alerts import evaluate_alerts
from config import FLASK_HOST, FLASK_PORT, RANGE_SECONDS, RANGE_SOURCE, DB_PATH

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
