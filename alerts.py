import time
import logging
from config import ALERT_THRESHOLDS, SUSTAINED_POLLS

logger = logging.getLogger(__name__)


def evaluate_alerts(conn, ts):
    _check_host_cpu(conn, ts)
    _check_host_mem(conn, ts)
    _check_disk_space(conn, ts)
    _check_vm_cpu(conn, ts)
    _check_vm_mem(conn, ts)
    _check_vm_heartbeat(conn, ts)


def _raise_alert(conn, ts, severity, target, metric, message, value):
    existing = conn.execute(
        "SELECT id FROM alerts WHERE target=? AND metric=? AND ts_cleared IS NULL",
        (target, metric),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO alerts (ts_raised, severity, target, metric, message, value) VALUES (?,?,?,?,?,?)",
        (ts, severity, target, metric, message, value),
    )
    logger.info("Alert raised: [%s] %s - %s", severity, target, message)


def _clear_alert(conn, ts, target, metric):
    conn.execute(
        "UPDATE alerts SET ts_cleared=? WHERE target=? AND metric=? AND ts_cleared IS NULL",
        (ts, target, metric),
    )


def _get_recent_values(conn, table, column, where_clause, params, count):
    rows = conn.execute(
        f"SELECT {column} FROM {table} WHERE {where_clause} ORDER BY ts DESC LIMIT ?",
        (*params, count),
    ).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def _check_sustained(values, warning_thresh, critical_thresh, count):
    if len(values) < count:
        return None
    recent = values[:count]
    if all(v >= critical_thresh for v in recent):
        return "critical"
    if all(v >= warning_thresh for v in recent):
        return "warning"
    return None


def _check_host_cpu(conn, ts):
    values = _get_recent_values(conn, "host_metrics", "cpu_pct", "1=1", (), SUSTAINED_POLLS)
    level = _check_sustained(
        values,
        ALERT_THRESHOLDS["host_cpu_warning"],
        ALERT_THRESHOLDS["host_cpu_critical"],
        SUSTAINED_POLLS,
    )
    if level:
        _raise_alert(conn, ts, level, "host", "cpu",
                      f"Host CPU at {values[0]:.0f}% (sustained)", values[0])
    else:
        _clear_alert(conn, ts, "host", "cpu")


def _check_host_mem(conn, ts):
    values = _get_recent_values(conn, "host_metrics", "mem_pct", "1=1", (), 1)
    if not values:
        return
    val = values[0]
    if val >= ALERT_THRESHOLDS["host_mem_critical"]:
        _raise_alert(conn, ts, "critical", "host", "memory",
                      f"Host memory at {val:.0f}%", val)
    elif val >= ALERT_THRESHOLDS["host_mem_warning"]:
        _raise_alert(conn, ts, "warning", "host", "memory",
                      f"Host memory at {val:.0f}%", val)
    else:
        _clear_alert(conn, ts, "host", "memory")


def _check_disk_space(conn, ts):
    rows = conn.execute(
        "SELECT drive, pct_used FROM host_volumes WHERE ts = (SELECT MAX(ts) FROM host_volumes)"
    ).fetchall()
    for row in rows:
        drive = row["drive"]
        pct = row["pct_used"]
        target = f"host:{drive}:"
        if pct >= ALERT_THRESHOLDS["host_disk_critical"]:
            _raise_alert(conn, ts, "critical", target, "disk_space",
                          f"Drive {drive}: is {pct:.0f}% full", pct)
        elif pct >= ALERT_THRESHOLDS["host_disk_warning"]:
            _raise_alert(conn, ts, "warning", target, "disk_space",
                          f"Drive {drive}: is {pct:.0f}% full", pct)
        else:
            _clear_alert(conn, ts, target, "disk_space")


def _check_vm_cpu(conn, ts):
    vms = conn.execute(
        "SELECT DISTINCT vm_name FROM vm_metrics ORDER BY vm_name"
    ).fetchall()
    for vm_row in vms:
        vm = vm_row["vm_name"]
        values = _get_recent_values(
            conn, "vm_metrics", "cpu_usage",
            "vm_name=?", (vm,), SUSTAINED_POLLS,
        )
        level = _check_sustained(
            values,
            ALERT_THRESHOLDS["vm_cpu_warning"],
            ALERT_THRESHOLDS["vm_cpu_critical"],
            SUSTAINED_POLLS,
        )
        if level:
            _raise_alert(conn, ts, level, vm, "cpu",
                          f"{vm} CPU at {values[0]:.0f}% (sustained)", values[0])
        else:
            _clear_alert(conn, ts, vm, "cpu")


def _check_vm_mem(conn, ts):
    rows = conn.execute(
        """SELECT vm_name, mem_assigned, mem_demand FROM vm_metrics
           WHERE ts = (SELECT MAX(ts) FROM vm_metrics) AND mem_assigned > 0"""
    ).fetchall()
    for row in rows:
        vm = row["vm_name"]
        assigned = row["mem_assigned"]
        demand = row["mem_demand"] or 0
        pct = (demand / assigned * 100) if assigned > 0 else 0
        if pct >= ALERT_THRESHOLDS["vm_mem_critical"]:
            _raise_alert(conn, ts, "critical", vm, "memory",
                          f"{vm} memory demand at {pct:.0f}% of assigned", pct)
        elif pct >= ALERT_THRESHOLDS["vm_mem_warning"]:
            _raise_alert(conn, ts, "warning", vm, "memory",
                          f"{vm} memory demand at {pct:.0f}% of assigned", pct)
        else:
            _clear_alert(conn, ts, vm, "memory")


def _check_vm_heartbeat(conn, ts):
    rows = conn.execute(
        """SELECT vm_name, heartbeat, state FROM vm_metrics
           WHERE ts = (SELECT MAX(ts) FROM vm_metrics)"""
    ).fetchall()
    for row in rows:
        vm = row["vm_name"]
        hb = row["heartbeat"] or ""
        state = row["state"] or ""
        if state != "Running":
            _clear_alert(conn, ts, vm, "heartbeat")
            continue
        ok_states = ("OkApplicationsHealthy", "OkApplicationsUnknown", "Ok", "N/A")
        if hb not in ok_states and hb:
            _raise_alert(conn, ts, "critical", vm, "heartbeat",
                          f"{vm} heartbeat: {hb}", 0)
        else:
            _clear_alert(conn, ts, vm, "heartbeat")
