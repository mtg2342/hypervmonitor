import sqlite3
import time
import logging
from config import (
    DB_PATH, VACUUM_THRESHOLD,
    RAW_RETENTION_HOURS, HOURLY_RETENTION_DAYS, DAILY_RETENTION_DAYS,
    EVENTS_RETENTION_DAYS, ALERTS_RETENTION_DAYS,
)

logger = logging.getLogger(__name__)


def get_connection(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path=None):
    conn = get_connection(db_path)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS host_metrics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL NOT NULL,
            cpu_pct         REAL,
            mem_total       INTEGER,
            mem_avail       INTEGER,
            mem_pct         REAL,
            disk_read_bps   REAL,
            disk_write_bps  REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_host_ts ON host_metrics(ts)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS host_volumes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            drive       TEXT NOT NULL,
            label       TEXT,
            total       INTEGER,
            free        INTEGER,
            pct_used    REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_vol_ts ON host_volumes(ts)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS vm_metrics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL NOT NULL,
            vm_name         TEXT NOT NULL,
            state           TEXT,
            cpu_usage       REAL,
            mem_assigned    INTEGER,
            mem_demand      INTEGER,
            uptime_sec      REAL,
            heartbeat       TEXT,
            net_sent_bps    REAL,
            net_recv_bps    REAL,
            disk_read_bps   REAL,
            disk_write_bps  REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_vm_ts ON vm_metrics(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vm_name_ts ON vm_metrics(vm_name, ts)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS vhd_info (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            vm_name     TEXT NOT NULL,
            vhd_path    TEXT NOT NULL,
            vhd_type    TEXT,
            file_size   INTEGER,
            max_size    INTEGER,
            pct_used    REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_vhd_ts ON vhd_info(ts)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_raised   REAL NOT NULL,
            ts_cleared  REAL,
            severity    TEXT NOT NULL,
            target      TEXT NOT NULL,
            metric      TEXT NOT NULL,
            message     TEXT NOT NULL,
            value       REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(ts_cleared)")

    # ── Hourly + Daily rollups for long-term history ─────────────────
    for suffix in ("hourly", "daily"):
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS host_metrics_{suffix} (
                bucket_ts       INTEGER PRIMARY KEY,
                cpu_pct_avg     REAL,
                cpu_pct_max     REAL,
                mem_pct_avg     REAL,
                mem_pct_max     REAL,
                disk_read_avg   REAL,
                disk_write_avg  REAL,
                samples         INTEGER
            )
        """)
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS vm_metrics_{suffix} (
                bucket_ts        INTEGER NOT NULL,
                vm_name          TEXT NOT NULL,
                cpu_usage_avg    REAL,
                cpu_usage_max    REAL,
                mem_assigned_avg REAL,
                mem_demand_avg   REAL,
                mem_demand_max   REAL,
                net_sent_avg     REAL,
                net_recv_avg     REAL,
                disk_read_avg    REAL,
                disk_write_avg   REAL,
                samples          INTEGER,
                PRIMARY KEY (bucket_ts, vm_name)
            )
        """)
        c.execute(f"CREATE INDEX IF NOT EXISTS idx_vmh_{suffix}_ts ON vm_metrics_{suffix}(bucket_ts)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS system_info (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            ts              REAL,
            os_name         TEXT,
            os_version      TEXT,
            host_name       TEXT,
            last_boot_ts    REAL,
            uptime_sec      REAL,
            updates_pending INTEGER,
            updates_ts      REAL
        )
    """)
    c.execute("INSERT OR IGNORE INTO system_info (id) VALUES (1)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS reboot_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_boot     REAL NOT NULL UNIQUE,
            reason      TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_reboot_ts ON reboot_history(ts_boot)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS system_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_event    REAL NOT NULL,
            log_name    TEXT NOT NULL,
            source      TEXT,
            event_id    INTEGER,
            level       INTEGER,
            level_name  TEXT,
            message     TEXT,
            UNIQUE(ts_event, log_name, source, event_id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON system_events(ts_event)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_updates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_seen     REAL NOT NULL,
            title       TEXT NOT NULL,
            severity    TEXT,
            kb          TEXT,
            UNIQUE(title)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS security_status (
            id                          INTEGER PRIMARY KEY CHECK (id = 1),
            ts                          REAL,
            firewall_domain             INTEGER,
            firewall_private            INTEGER,
            firewall_public             INTEGER,
            defender_realtime           INTEGER,
            defender_antivirus_enabled  INTEGER,
            defender_signature_age_days REAL,
            defender_engine_version     TEXT,
            bitlocker_status            TEXT,
            uac_enabled                 INTEGER,
            failed_logins_24h           INTEGER,
            rdp_success_24h             INTEGER,
            admin_count                 INTEGER,
            findings_json               TEXT
        )
    """)
    c.execute("INSERT OR IGNORE INTO security_status (id) VALUES (1)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS rdp_logins (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_event    REAL NOT NULL,
            username    TEXT,
            domain      TEXT,
            source_ip   TEXT,
            workstation TEXT,
            logon_type  INTEGER,
            success     INTEGER NOT NULL,
            UNIQUE(ts_event, username, source_ip, success)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_rdp_ts ON rdp_logins(ts_event)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_ts REAL
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", db_path or DB_PATH)


def purge_old_data(conn):
    now = time.time()
    raw_cutoff    = now - (RAW_RETENTION_HOURS * 3600)
    hourly_cutoff = now - (HOURLY_RETENTION_DAYS * 86400)
    daily_cutoff  = now - (DAILY_RETENTION_DAYS * 86400)
    events_cutoff = now - (EVENTS_RETENTION_DAYS * 86400)
    alert_cutoff  = now - (ALERTS_RETENTION_DAYS * 86400)
    total_deleted = 0

    for table in ("host_metrics", "host_volumes", "vm_metrics"):
        conn.execute(f"DELETE FROM {table} WHERE ts < ?", (raw_cutoff,))
        total_deleted += conn.execute("SELECT changes()").fetchone()[0]

    # VHD info changes slowly; keep 30 days raw
    conn.execute("DELETE FROM vhd_info WHERE ts < ?", (hourly_cutoff,))
    total_deleted += conn.execute("SELECT changes()").fetchone()[0]

    for table in ("host_metrics_hourly", "vm_metrics_hourly"):
        conn.execute(f"DELETE FROM {table} WHERE bucket_ts < ?", (hourly_cutoff,))
        total_deleted += conn.execute("SELECT changes()").fetchone()[0]

    for table in ("host_metrics_daily", "vm_metrics_daily"):
        conn.execute(f"DELETE FROM {table} WHERE bucket_ts < ?", (daily_cutoff,))
        total_deleted += conn.execute("SELECT changes()").fetchone()[0]

    conn.execute("DELETE FROM system_events WHERE ts_event < ?", (events_cutoff,))
    total_deleted += conn.execute("SELECT changes()").fetchone()[0]

    rdp_cutoff = now - (180 * 86400)  # keep 180 days of RDP logins
    conn.execute("DELETE FROM rdp_logins WHERE ts_event < ?", (rdp_cutoff,))
    total_deleted += conn.execute("SELECT changes()").fetchone()[0]

    conn.execute(
        "DELETE FROM alerts WHERE ts_cleared IS NOT NULL AND ts_cleared < ?",
        (alert_cutoff,),
    )
    total_deleted += conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()

    if total_deleted > VACUUM_THRESHOLD:
        conn.execute("VACUUM")
        logger.info("VACUUM after purging %d rows", total_deleted)
    elif total_deleted > 0:
        logger.info("Purged %d old rows", total_deleted)


def rollup_aggregates(conn):
    """Aggregate raw → hourly and hourly → daily for any complete buckets."""
    now = time.time()
    inserted_hourly = _rollup_hourly(conn, now)
    inserted_daily = _rollup_daily(conn, now)
    if inserted_hourly or inserted_daily:
        conn.commit()
        logger.info(
            "Rollup: %d hourly buckets, %d daily buckets created",
            inserted_hourly, inserted_daily,
        )


def _rollup_hourly(conn, now):
    """Bucket-size = 3600s. Roll up any completed hour not already aggregated."""
    completed_hour_floor = int(now // 3600) * 3600 - 3600
    last_done = conn.execute(
        "SELECT COALESCE(MAX(bucket_ts), 0) FROM host_metrics_hourly"
    ).fetchone()[0]
    start = max(last_done + 3600, 0)
    if start > completed_hour_floor:
        return 0

    rows = conn.execute(
        """SELECT CAST(ts / 3600 AS INTEGER) * 3600 AS bkt,
                  AVG(cpu_pct), MAX(cpu_pct),
                  AVG(mem_pct), MAX(mem_pct),
                  AVG(disk_read_bps), AVG(disk_write_bps),
                  COUNT(*)
           FROM host_metrics
           WHERE ts >= ? AND ts < ?
           GROUP BY bkt
           HAVING bkt <= ?
           ORDER BY bkt""",
        (start, completed_hour_floor + 3600, completed_hour_floor),
    ).fetchall()

    count = 0
    for r in rows:
        conn.execute(
            """INSERT OR IGNORE INTO host_metrics_hourly
               (bucket_ts, cpu_pct_avg, cpu_pct_max, mem_pct_avg, mem_pct_max,
                disk_read_avg, disk_write_avg, samples)
               VALUES (?,?,?,?,?,?,?,?)""",
            tuple(r),
        )
        count += conn.execute("SELECT changes()").fetchone()[0]

    vm_rows = conn.execute(
        """SELECT CAST(ts / 3600 AS INTEGER) * 3600 AS bkt, vm_name,
                  AVG(cpu_usage), MAX(cpu_usage),
                  AVG(mem_assigned),
                  AVG(mem_demand), MAX(mem_demand),
                  AVG(net_sent_bps), AVG(net_recv_bps),
                  AVG(disk_read_bps), AVG(disk_write_bps),
                  COUNT(*)
           FROM vm_metrics
           WHERE ts >= ? AND ts < ?
           GROUP BY bkt, vm_name
           HAVING bkt <= ?
           ORDER BY bkt""",
        (start, completed_hour_floor + 3600, completed_hour_floor),
    ).fetchall()

    for r in vm_rows:
        conn.execute(
            """INSERT OR IGNORE INTO vm_metrics_hourly
               (bucket_ts, vm_name, cpu_usage_avg, cpu_usage_max,
                mem_assigned_avg, mem_demand_avg, mem_demand_max,
                net_sent_avg, net_recv_avg,
                disk_read_avg, disk_write_avg, samples)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(r),
        )

    return count


def _rollup_daily(conn, now):
    """Bucket-size = 86400s (UTC day). Roll up completed days from hourly table."""
    today_floor = int(now // 86400) * 86400
    last_done = conn.execute(
        "SELECT COALESCE(MAX(bucket_ts), 0) FROM host_metrics_daily"
    ).fetchone()[0]
    start = max(last_done + 86400, 0)
    if start >= today_floor:
        return 0

    rows = conn.execute(
        """SELECT CAST(bucket_ts / 86400 AS INTEGER) * 86400 AS day,
                  AVG(cpu_pct_avg), MAX(cpu_pct_max),
                  AVG(mem_pct_avg), MAX(mem_pct_max),
                  AVG(disk_read_avg), AVG(disk_write_avg),
                  SUM(samples)
           FROM host_metrics_hourly
           WHERE bucket_ts >= ? AND bucket_ts < ?
           GROUP BY day
           ORDER BY day""",
        (start, today_floor),
    ).fetchall()

    count = 0
    for r in rows:
        conn.execute(
            """INSERT OR IGNORE INTO host_metrics_daily
               (bucket_ts, cpu_pct_avg, cpu_pct_max, mem_pct_avg, mem_pct_max,
                disk_read_avg, disk_write_avg, samples)
               VALUES (?,?,?,?,?,?,?,?)""",
            tuple(r),
        )
        count += conn.execute("SELECT changes()").fetchone()[0]

    vm_rows = conn.execute(
        """SELECT CAST(bucket_ts / 86400 AS INTEGER) * 86400 AS day, vm_name,
                  AVG(cpu_usage_avg), MAX(cpu_usage_max),
                  AVG(mem_assigned_avg),
                  AVG(mem_demand_avg), MAX(mem_demand_max),
                  AVG(net_sent_avg), AVG(net_recv_avg),
                  AVG(disk_read_avg), AVG(disk_write_avg),
                  SUM(samples)
           FROM vm_metrics_hourly
           WHERE bucket_ts >= ? AND bucket_ts < ?
           GROUP BY day, vm_name
           ORDER BY day""",
        (start, today_floor),
    ).fetchall()

    for r in vm_rows:
        conn.execute(
            """INSERT OR IGNORE INTO vm_metrics_daily
               (bucket_ts, vm_name, cpu_usage_avg, cpu_usage_max,
                mem_assigned_avg, mem_demand_avg, mem_demand_max,
                net_sent_avg, net_recv_avg,
                disk_read_avg, disk_write_avg, samples)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(r),
        )

    return count
