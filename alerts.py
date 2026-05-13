import time
import logging
from config import ALERT_THRESHOLDS, SUSTAINED_POLLS

logger = logging.getLogger(__name__)


# Keys allowed to be overridden via the Settings tab + their (min, max) ranges
SETTABLE_THRESHOLDS = {
    "host_cpu_warning":   (1, 100),
    "host_cpu_critical":  (1, 100),
    "host_mem_warning":   (1, 100),
    "host_mem_critical":  (1, 100),
    "host_disk_warning":  (1, 100),
    "host_disk_critical": (1, 100),
    "vm_cpu_warning":     (1, 100),
    "vm_cpu_critical":    (1, 100),
    "vm_mem_warning":     (1, 100),
    "vm_mem_critical":    (1, 100),
}
SETTABLE_INTS = {
    "sustained_polls":    (1, 20),
}


def get_settings(conn):
    """Return effective settings = defaults overlaid with rows in app_settings."""
    settings = dict(ALERT_THRESHOLDS)
    settings["sustained_polls"] = SUSTAINED_POLLS
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    for r in rows:
        k = r["key"]
        v = r["value"]
        try:
            if k in SETTABLE_INTS:
                settings[k] = int(v)
            elif k in SETTABLE_THRESHOLDS or k in settings:
                settings[k] = float(v)
        except (TypeError, ValueError):
            pass
    return settings


def get_default_settings():
    """Return the bundled defaults (read-only — for the UI to display a Reset button)."""
    d = dict(ALERT_THRESHOLDS)
    d["sustained_polls"] = SUSTAINED_POLLS
    return d


def save_settings(conn, updates):
    """Persist a dict of {key: value} updates. Validates against SETTABLE_*.

    Returns (saved_count, errors_list).
    """
    saved = 0
    errors = []
    now = time.time()
    defaults = get_default_settings()

    for key, raw in updates.items():
        try:
            if key in SETTABLE_INTS:
                val = int(raw)
                lo, hi = SETTABLE_INTS[key]
            elif key in SETTABLE_THRESHOLDS:
                val = float(raw)
                lo, hi = SETTABLE_THRESHOLDS[key]
            else:
                errors.append(f"unknown setting '{key}'")
                continue
            if val < lo or val > hi:
                errors.append(f"{key}={val} is outside range [{lo},{hi}]")
                continue
        except (TypeError, ValueError):
            errors.append(f"{key}: not a number")
            continue

        # If value equals the default, delete the row (keep DB clean)
        if key in defaults and val == defaults[key]:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (key, str(val), now),
            )
        saved += 1

    # Cross-field sanity: critical >= warning. Don't fail save — just warn.
    eff = get_settings(conn)
    for prefix in ("host_cpu", "host_mem", "host_disk", "vm_cpu", "vm_mem"):
        w = eff.get(f"{prefix}_warning")
        c = eff.get(f"{prefix}_critical")
        if w is not None and c is not None and c < w:
            errors.append(f"warning: {prefix}_critical ({c}) is below {prefix}_warning ({w})")

    conn.commit()
    return saved, errors


def evaluate_alerts(conn, ts):
    s = get_settings(conn)
    _check_host_cpu(conn, ts, s)
    _check_host_mem(conn, ts, s)
    _check_disk_space(conn, ts, s)
    _check_vm_cpu(conn, ts, s)
    _check_vm_mem(conn, ts, s)
    _check_vm_heartbeat(conn, ts)
    _check_pending_reboot(conn, ts)
    _check_veeam_backups(conn, ts)


def _check_pending_reboot(conn, ts):
    row = conn.execute(
        "SELECT pending_reboot, reboot_reasons FROM security_status WHERE id=1"
    ).fetchone()
    if not row:
        return
    if row["pending_reboot"]:
        reasons = row["reboot_reasons"] or "unknown"
        _raise_alert(conn, ts, "warning", "host", "pending_reboot",
                      f"Host has a pending reboot ({reasons})", 1)
    else:
        _clear_alert(conn, ts, "host", "pending_reboot")


def _check_veeam_backups(conn, ts):
    """Raise an alert for any backup job whose last result is Failed."""
    rows = conn.execute(
        "SELECT job_name, last_result, last_end_ts FROM veeam_backups"
    ).fetchall()
    seen_jobs = set()
    for r in rows:
        job = r["job_name"]
        seen_jobs.add(job)
        target = "veeam:" + job
        result = (r["last_result"] or "").lower()
        if result == "failed":
            _raise_alert(conn, ts, "critical", target, "backup",
                          f"Veeam job '{job}' failed", 0)
        elif result == "warning":
            _raise_alert(conn, ts, "warning", target, "backup",
                          f"Veeam job '{job}' completed with warnings", 0)
        else:
            _clear_alert(conn, ts, target, "backup")


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


def _check_host_cpu(conn, ts, s):
    sp = s["sustained_polls"]
    values = _get_recent_values(conn, "host_metrics", "cpu_pct", "1=1", (), sp)
    level = _check_sustained(values, s["host_cpu_warning"], s["host_cpu_critical"], sp)
    if level:
        _raise_alert(conn, ts, level, "host", "cpu",
                      f"Host CPU at {values[0]:.0f}% (sustained)", values[0])
    else:
        _clear_alert(conn, ts, "host", "cpu")


def _check_host_mem(conn, ts, s):
    values = _get_recent_values(conn, "host_metrics", "mem_pct", "1=1", (), 1)
    if not values:
        return
    val = values[0]
    if val >= s["host_mem_critical"]:
        _raise_alert(conn, ts, "critical", "host", "memory",
                      f"Host memory at {val:.0f}%", val)
    elif val >= s["host_mem_warning"]:
        _raise_alert(conn, ts, "warning", "host", "memory",
                      f"Host memory at {val:.0f}%", val)
    else:
        _clear_alert(conn, ts, "host", "memory")


def _check_disk_space(conn, ts, s):
    rows = conn.execute(
        "SELECT drive, pct_used FROM host_volumes WHERE ts = (SELECT MAX(ts) FROM host_volumes)"
    ).fetchall()
    for row in rows:
        drive = row["drive"]
        pct = row["pct_used"]
        target = f"host:{drive}:"
        if pct >= s["host_disk_critical"]:
            _raise_alert(conn, ts, "critical", target, "disk_space",
                          f"Drive {drive}: is {pct:.0f}% full", pct)
        elif pct >= s["host_disk_warning"]:
            _raise_alert(conn, ts, "warning", target, "disk_space",
                          f"Drive {drive}: is {pct:.0f}% full", pct)
        else:
            _clear_alert(conn, ts, target, "disk_space")


def _check_vm_cpu(conn, ts, s):
    sp = s["sustained_polls"]
    vms = conn.execute(
        "SELECT DISTINCT vm_name FROM vm_metrics ORDER BY vm_name"
    ).fetchall()
    for vm_row in vms:
        vm = vm_row["vm_name"]
        values = _get_recent_values(
            conn, "vm_metrics", "cpu_usage",
            "vm_name=?", (vm,), sp,
        )
        level = _check_sustained(values, s["vm_cpu_warning"], s["vm_cpu_critical"], sp)
        if level:
            _raise_alert(conn, ts, level, vm, "cpu",
                          f"{vm} CPU at {values[0]:.0f}% (sustained)", values[0])
        else:
            _clear_alert(conn, ts, vm, "cpu")


def _check_vm_mem(conn, ts, s):
    rows = conn.execute(
        """SELECT vm_name, mem_assigned, mem_demand FROM vm_metrics
           WHERE ts = (SELECT MAX(ts) FROM vm_metrics) AND mem_assigned > 0"""
    ).fetchall()
    for row in rows:
        vm = row["vm_name"]
        assigned = row["mem_assigned"]
        demand = row["mem_demand"] or 0
        pct = (demand / assigned * 100) if assigned > 0 else 0
        if pct >= s["vm_mem_critical"]:
            _raise_alert(conn, ts, "critical", vm, "memory",
                          f"{vm} memory demand at {pct:.0f}% of assigned", pct)
        elif pct >= s["vm_mem_warning"]:
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
