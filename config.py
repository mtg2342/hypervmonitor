import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hyperv_monitor.db")

POLL_INTERVAL = 30          # seconds between metric collections
VHD_POLL_MULTIPLE = 10      # collect VHD info every N polls (10 * 30s = 5 min)
SYSINFO_POLL_MULTIPLE = 10  # system info (uptime, OS) every 5 min
EVENTLOG_POLL_MULTIPLE = 4  # event log scan every 2 min
UPDATES_POLL_MULTIPLE = 120 # pending updates check every 1 hour (slow operation)
ROLLUP_POLL_MULTIPLE = 120  # roll up hourly/daily aggregates every 1 hour
PURGE_CHECK_MULTIPLE = 120  # run purge every N polls (120 * 30s = 1 hour)

EVENTLOG_LOOKBACK_HOURS = 24   # how far back to scan event logs
EVENTLOG_MAX_EVENTS = 50       # cap events per scan

# Retention (raw → hourly → daily rollup chain)
RAW_RETENTION_HOURS    = 48     # keep raw 30-second samples for 2 days
HOURLY_RETENTION_DAYS  = 30     # keep hourly aggregates for 30 days
DAILY_RETENTION_DAYS   = 120    # keep daily aggregates for ~4 months
EVENTS_RETENTION_DAYS  = 30     # keep raw event log entries 30 days
ALERTS_RETENTION_DAYS  = 3650   # effectively permanent (~10 years) for the Alert History tab
VACUUM_THRESHOLD       = 10000  # run VACUUM after deleting this many rows

FLASK_HOST = "127.0.0.1"
FLASK_PORT = 5000

# Range → which table to query
RANGE_SOURCE = {
    "1h":  ("raw",    None),       # raw, no downsample
    "6h":  ("raw",    None),       # raw, no downsample
    "24h": ("raw",    300),        # raw with 5-min buckets
    "7d":  ("hourly", None),       # hourly aggregates
    "30d": ("hourly", None),       # hourly aggregates
    "4m":  ("daily",  None),       # daily aggregates (~120 days)
}

RANGE_SECONDS = {
    "1h":  3600,
    "6h":  21600,
    "24h": 86400,
    "7d":  604800,
    "30d": 2592000,
    "4m":  10368000,    # ~120 days
}

ALERT_THRESHOLDS = {
    "host_cpu_warning":    85,
    "host_cpu_critical":   95,
    "host_mem_warning":    85,
    "host_mem_critical":   95,
    "host_disk_warning":   85,
    "host_disk_critical":  95,
    "vm_cpu_warning":      90,
    "vm_cpu_critical":     98,
    "vm_mem_warning":      90,
    "vm_mem_critical":     95,
}

SUSTAINED_POLLS = 3
