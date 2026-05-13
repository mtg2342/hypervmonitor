import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hyperv_monitor.db")

POLL_INTERVAL = 30          # seconds between metric collections
VHD_POLL_MULTIPLE = 10      # collect VHD info every N polls (10 * 30s = 5 min)
SYSINFO_POLL_MULTIPLE = 10  # system info (uptime, OS) every 5 min
EVENTLOG_POLL_MULTIPLE = 4  # event log scan every 2 min
UPDATES_POLL_MULTIPLE = 120 # pending updates check every 1 hour (slow operation)
PURGE_CHECK_MULTIPLE = 120  # run purge every N polls (120 * 30s = 1 hour)

EVENTLOG_LOOKBACK_HOURS = 24   # how far back to scan event logs
EVENTLOG_MAX_EVENTS = 50       # cap events per scan

RETENTION_HOURS = 168       # keep raw data for 7 days
ALERTS_RETENTION_DAYS = 90
VACUUM_THRESHOLD = 10000    # run VACUUM after deleting this many rows

FLASK_HOST = "127.0.0.1"
FLASK_PORT = 5000

# Downsampling buckets (seconds)
DOWNSAMPLE = {
    "1h":  None,    # raw data
    "6h":  None,    # raw data
    "24h": 120,     # 2-minute buckets
    "7d":  600,     # 10-minute buckets
}

RANGE_SECONDS = {
    "1h":  3600,
    "6h":  21600,
    "24h": 86400,
    "7d":  604800,
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
