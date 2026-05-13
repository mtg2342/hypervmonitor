import sqlite3
import time
import logging
from config import DB_PATH, RETENTION_HOURS, ALERTS_RETENTION_DAYS, VACUUM_THRESHOLD

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

    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", db_path or DB_PATH)


def purge_old_data(conn):
    cutoff = time.time() - (RETENTION_HOURS * 3600)
    alert_cutoff = time.time() - (ALERTS_RETENTION_DAYS * 86400)
    total_deleted = 0

    for table in ("host_metrics", "host_volumes", "vm_metrics", "vhd_info"):
        conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        total_deleted += conn.execute("SELECT changes()").fetchone()[0]

    event_cutoff = time.time() - (RETENTION_HOURS * 3600)
    conn.execute("DELETE FROM system_events WHERE ts_event < ?", (event_cutoff,))
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
