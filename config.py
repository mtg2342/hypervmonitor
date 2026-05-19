import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hyperv_monitor.db")

POLL_INTERVAL = 30          # seconds between metric collections
VHD_POLL_MULTIPLE = 10      # collect VHD info every N polls (10 * 30s = 5 min)
SYSINFO_POLL_MULTIPLE = 10  # system info (uptime, OS) every 5 min
EVENTLOG_POLL_MULTIPLE = 4  # event log scan every 2 min
UPDATES_POLL_MULTIPLE = 120 # pending updates check every 1 hour (slow operation)
SECURITY_POLL_MULTIPLE = 20 # security status every 10 min
VEEAM_POLL_MULTIPLE    = 20 # Veeam backup status every 10 min
ROLLUP_POLL_MULTIPLE = 120  # roll up hourly/daily aggregates every 1 hour
PURGE_CHECK_MULTIPLE = 120  # run purge every N polls (120 * 30s = 1 hour)

EVENTLOG_LOOKBACK_HOURS = 24    # how far back to scan event logs
EVENTLOG_MAX_EVENTS = 50        # cap events per scan
RDP_LOOKBACK_DAYS = 30          # scan RDP logon events from this far back
RDP_MAX_EVENTS = 500            # cap RDP login events per scan

# Retention — set any value to 0 to disable purging for that category
# entirely (data kept indefinitely). The dashboard ships defaulting to
# permanent storage so historical trends are never lost.
#
# Approximate storage growth at 5 VMs, 30s polling:
#   Raw vm_metrics: ~3.8 GB / year
#   Hourly:         ~7 MB  / year
#   Daily:          ~300 KB/ year
#   Events:         a few MB per year (sparse, only high-severity)
#   Alerts/RDP:     tiny
# SQLite copes happily with tens of GB. If storage ever becomes a
# concern, set a positive number of hours/days here to bring the cap
# back.
RAW_RETENTION_HOURS    = 0   # 0 = keep all raw 30-second samples forever
HOURLY_RETENTION_DAYS  = 0   # 0 = keep all hourly aggregates forever
DAILY_RETENTION_DAYS   = 0   # 0 = keep all daily aggregates forever
EVENTS_RETENTION_DAYS  = 0   # 0 = keep all event-log entries forever
ALERTS_RETENTION_DAYS  = 0   # 0 = keep all alert history forever
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
