# Hyper-V Monitor — Project Reference

A lightweight, local-only browser dashboard for monitoring a Windows 11 Hyper-V host and its VMs. Designed to add negligible load to a host whose primary job is running VMs, not running monitoring software.

---

## 1. Goal & Context

**Original request:** Design a lightweight monitoring app for a Windows 11 machine that runs Hyper-V with multiple VMs. The host OS is only used to manage the system but must not be lagged down. Need charts showing performance, storage, RAM/CPU usage per VM, network and disk I/O per VM, and flagging of potential problems. Local only — not exposed over the network.

**Follow-up additions:**
- Recent high-risk machine errors from Windows Event Log
- Reboot times / boot history
- System uptime
- Pending Windows Updates indicator
- This dashboard machine is itself a VM (not the Hyper-V host) — so on the development machine the VM grid will be empty, but the host-level metrics, system info, events, and updates all work.

**Target environment:** Windows 11 host with Hyper-V Platform installed and ~5 VMs running. The dashboard is opened in a local browser only — no network exposure.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Browser (localhost:5000)                                         │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Dashboard (HTML + CSS + JS + Chart.js)                    │  │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐               │  │
│  │  │Host CPU│ │Memory  │ │Disk I/O│ │Storage │               │  │
│  │  └────────┘ └────────┘ └────────┘ └────────┘               │  │
│  │  ┌────────────────┐ ┌────────────────────┐                 │  │
│  │  │ System Health  │ │ Recent High-Risk   │                 │  │
│  │  │ uptime/reboots │ │ Events (last 24h)  │                 │  │
│  │  └────────────────┘ └────────────────────┘                 │  │
│  │  VM Grid (one card per running VM)                         │  │
│  │  Detail Charts (CPU/Mem/Net/Disk/Storage tabs)             │  │
│  └────────────────────────────────────────────────────────────┘  │
└─────────────────────────────┬────────────────────────────────────┘
                              │ HTTP (127.0.0.1:5000)
┌─────────────────────────────▼────────────────────────────────────┐
│  Flask App (app.py) - REST API on 127.0.0.1 only                │
│  ┌─────────────────┐   ┌─────────────────┐                       │
│  │ Collector Thread │   │ Alert Thread    │                       │
│  │ (collector.py)   │   │ (alerts.py)     │                       │
│  └────────┬─────────┘   └────────┬────────┘                       │
│           │                       │                                │
│           ▼                       ▼                                │
│        ┌─────────────────────────────┐                            │
│        │   SQLite (WAL mode)         │                            │
│        │   hyperv_monitor.db         │                            │
│        └─────────────────────────────┘                            │
│           │                                                        │
│           ▼  PowerShell subprocess (every 30s)                    │
└───────────┬──────────────────────────────────────────────────────┘
            │
┌───────────▼──────────────────────────────────────────────────────┐
│   PowerShell + Hyper-V cmdlets + WMI/CIM                         │
│   Get-VM, Get-Counter, Get-Volume, Get-VHD,                      │
│   Get-WinEvent, Get-CimInstance, Microsoft.Update.Searcher       │
└──────────────────────────────────────────────────────────────────┘
```

**Stack rationale:**
- **Python (Flask)** — lightweight, ~20–30MB RAM, easy subprocess integration
- **PowerShell subprocess** for Hyper-V/Windows access — more reliable than the Python `wmi` library, easy to debug standalone
- **SQLite (WAL mode)** for history — zero config, allows concurrent reads while collector writes
- **Chart.js (local copy)** for visualisation — no CDN dependency, ~206 KB

**Resource footprint:** Expected <1% CPU and ~30 MB RAM. PowerShell processes are spawned briefly every 30 seconds; queries are bounded and indexed.

---

## 3. File Structure

```
C:\Users\User\Desktop\Projects\Hyper v monitor\
├── app.py                  Flask app, API endpoints, startup
├── collector.py             Background metric collection via PowerShell
├── alerts.py                Threshold-based alert evaluation
├── db.py                    SQLite schema, connection, retention purge
├── config.py                All tunable constants
├── requirements.txt         Python deps (flask)
├── start.bat                One-click launcher with admin check
├── update.bat               Pull latest from GitHub and restart (host)
├── first-time-setup.bat     Initial install helper for the host
├── .gitignore               Excludes the SQLite DB so history survives updates
├── PROJECT_REFERENCE.md     This file
├── hyperv_monitor.db        SQLite database (created on first run — gitignored)
├── static/
│   ├── css/
│   │   └── dashboard.css    Dark theme
│   └── js/
│       ├── chart.umd.min.js Chart.js library (local)
│       ├── dashboard.js     Main controller
│       ├── charts.js        Chart helpers, sparklines, detail charts
│       └── alerts.js        Alert banner rendering
└── templates/
    └── index.html           Single-page dashboard
```

---

## 4. Data Collected

### 4a. Hyper-V VM metrics (every 30 s)
| Metric | Source |
|---|---|
| VM state, CPU %, memory assigned, memory demand, uptime, heartbeat | `Get-VM` |
| Network sent / received (computed deltas) | `Get-VMNetworkAdapter` |
| Disk read / write bytes per second | `Get-Counter '\Hyper-V Virtual Storage Device(*)\...'` |
| VHD actual size vs max size | `Get-VHD` (every 5 minutes) |

### 4b. Host metrics (every 30 s)
| Metric | Source |
|---|---|
| Total CPU % | `Get-Counter '\Processor(_Total)\% Processor Time'` |
| Memory available / committed % | `Get-Counter \Memory\Available Bytes`, `\Memory\% Committed Bytes In Use` |
| Physical disk read / write bytes/sec | `Get-Counter '\PhysicalDisk(_Total)\...'` |
| Total RAM | Win32 `GetPhysicallyInstalledSystemMemory` |
| Volumes (drive letter, total, free, % used) | `Get-Volume` |

### 4c. System info (every 5 min)
| Metric | Source |
|---|---|
| OS name, OS version, hostname | `Get-CimInstance Win32_OperatingSystem`, `Win32_ComputerSystem` |
| Last boot time, uptime | Calculated from `LastBootUpTime` |
| Recent reboot history (up to 10) | `Get-WinEvent` event IDs 6005, 6006, 6008, 1074, 41 |

### 4d. High-risk event logs (every 2 min)
| Metric | Source |
|---|---|
| Critical (level 1) and Error (level 2) events from System & Application logs, last 24h | `Get-WinEvent -FilterHashtable @{LogName=...; Level=1,2; StartTime=...}` |

Events are deduplicated by `(ts_event, log_name, source, event_id)`.

### 4e. Pending Windows updates (every 1 hour)
Uses `Microsoft.Update.Session` COM object — slow (can take 30+ seconds) but accurate. Runs at startup and once an hour.

---

## 5. Database Schema (SQLite)

| Table | Purpose |
|---|---|
| `host_metrics` | Per-poll host CPU, RAM, disk I/O |
| `host_volumes` | Per-poll drive space per volume |
| `vm_metrics` | Per-poll metrics for each VM |
| `vhd_info` | VHD sizes (every 5 min) |
| `alerts` | Active and historical alerts, deduplicated by (target, metric) |
| `system_info` | Single-row table for current OS/uptime/updates state |
| `reboot_history` | Past boot events, deduplicated by ts_boot |
| `system_events` | Critical/error events, deduplicated by (ts, log, source, id) |
| `pending_updates` | Current pending updates with KB and MSRC severity |

**Indexes:** All time-series tables are indexed on `ts`. WAL mode (`PRAGMA journal_mode=WAL`) enables concurrent reads during collector writes.

### Retention & rollup chain

The host accumulates data over months. To keep the database small, raw 30-second samples are rolled up into hourly and daily aggregate tables:

| Table | Granularity | Retention |
|---|---|---|
| `host_metrics`, `vm_metrics` | 30 seconds (raw) | 48 hours |
| `host_metrics_hourly`, `vm_metrics_hourly` | 1 hour (avg + max) | 30 days |
| `host_metrics_daily`, `vm_metrics_daily` | 1 day (avg + max) | 120 days (~4 months) |
| `system_events` | per event | 30 days |
| `alerts` (cleared) | per alert | 90 days |

The rollup job (`rollup_aggregates()` in `db.py`) runs every hour. It looks for any completed hour/day not already aggregated, computes averages and maximums from the source table, and inserts into the rollup table. Raw rows older than 48h are then purged.

**Query routing** (in `app.py`):

| Range button | Source table |
|---|---|
| 1H, 6H | raw `host_metrics` / `vm_metrics` |
| 24H | raw, downsampled to 5-min buckets |
| 7D, 30D | `*_hourly` rollup |
| 4M | `*_daily` rollup |

VACUUM runs only when >10 000 rows are deleted in a single purge.

---

## 6. REST API

All endpoints return JSON, all bind to `127.0.0.1:5000`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Serves the dashboard HTML |
| GET | `/api/host/current` | Latest host metrics + volumes |
| GET | `/api/host/history?range=1h\|6h\|24h\|7d` | Host time-series (downsampled for long ranges) |
| GET | `/api/vms/current` | Latest snapshot for all VMs |
| GET | `/api/vms/history?range=...&vm=...` | Per-VM time-series |
| GET | `/api/vhd/current` | Latest VHD sizes |
| GET | `/api/system/info` | OS info, uptime, updates count, reboot history |
| GET | `/api/system/events?limit=50&hours=24` | Critical & error events |
| GET | `/api/system/updates` | List of pending updates with KB |
| GET | `/api/alerts/active` | Active (uncleared) alerts |
| GET | `/api/alerts/history?limit=100` | Past alerts |
| POST | `/api/alerts/<id>/dismiss` | Manually clear an alert |

**Downsampling:** ranges <=6h return raw rows; 24h uses 2-minute averages; 7d uses 10-minute averages. Implemented via `GROUP BY CAST(ts / bucket AS INTEGER)`.

---

## 7. Alert Engine

Defined in `alerts.py`. Runs once per poll cycle.

| Alert | Warning | Critical | Sustained |
|---|---|---|---|
| Host CPU | 85% | 95% | 3 consecutive polls (~90s) |
| Host Memory | 85% | 95% | instant |
| Disk space (per volume) | 85% used | 95% used | instant |
| VM CPU | 90% | 98% | 3 consecutive polls |
| VM Memory (demand/assigned) | 90% | 95% | instant |
| VM Heartbeat | non-OK | — | instant |

Deduplication key is `(target, metric)` — only one active alert per combination. Alerts auto-clear when conditions return to normal. Manual dismissal sets `ts_cleared = now()`.

---

## 8. Dashboard Layout

1. **Header**: Title + time range selector (1H/6H/24H/7D) + last update timestamp
2. **Host cards row**: CPU %, Memory %, Disk I/O, Storage (with sparklines & a per-volume progress bar)
3. **Alerts banner** (only visible when active alerts exist): one line per alert with dismiss button
4. **System Health card** + **Recent Events card** (side by side):
   - Health: host name, OS, uptime, last boot, pending updates count, recent reboot list
   - Events: list of critical/error events from System and Application logs (last 24h)
5. **VM grid**: One card per VM with state badge, CPU, RAM, network, disk I/O, heartbeat, uptime
6. **Detail Charts**: Tabs for CPU / Memory / Network / Disk I/O / Storage — line charts overlaying all VMs and host, with the time range selector controlling history depth

Frontend polling cadence:
- Live cards (host + VMs): every 5 seconds
- Alerts: every 10 seconds
- System info: every 30 seconds
- Events: every 60 seconds
- Detail charts: every 60 seconds or on range/tab change

---

## 8b. Updating from the Dev Machine (GitHub Workflow)

The host pulls updates from GitHub. Code changes are made on a separate dev machine (where Claude runs) and pushed to:

**Repo:** https://github.com/mtg2342/hypervmonitor.git

### Workflow

```
┌──────────────────────┐         ┌──────────────────────┐
│  Dev Machine          │  push   │   GitHub             │
│  (Claude edits here) │ ──────▶ │  mtg2342/            │
│                       │         │  hypervmonitor       │
└──────────────────────┘         └──────────────────────┘
                                            │
                                            │  pull (via update.bat)
                                            ▼
                                  ┌──────────────────────┐
                                  │  Hyper-V Host         │
                                  │  C:\hypervmonitor\   │
                                  └──────────────────────┘
```

### First-time host setup (one-line installer)

Open PowerShell (or Windows Terminal) and paste:

```powershell
iex (irm https://raw.githubusercontent.com/mtg2342/hypervmonitor/main/install.ps1)
```

The installer will:
- Self-elevate to Administrator
- Check for Git and Python — offer to install them via `winget` if missing
- Ask where to install (default `C:\hypervmonitor`)
- `git clone` the repo
- `pip install` Flask
- Offer to create a Task Scheduler entry that runs `start.bat` at login (auto-start)
- Offer to launch the dashboard and open `http://127.0.0.1:5000` in the browser

### First-time host setup (manual)

If you prefer not to run the installer:
1. Install Python 3.10+ (python.org, "Add to PATH" checked) and Git for Windows (git-scm.com)
2. Clone the repo: `git clone https://github.com/mtg2342/hypervmonitor.git C:\hypervmonitor`
3. Right-click `start.bat` → **Run as administrator**

### Updating the host

1. Right-click `update.bat` → **Run as administrator**
2. It will:
   - Stop any running `python app.py` process
   - `git fetch` and show the incoming commit messages
   - `git pull --ff-only` (fails safely if there are local changes that would conflict)
   - Re-install Python dependencies if `requirements.txt` changed
   - Restart `start.bat`

### History is never lost

`hyperv_monitor.db` (and its WAL/SHM sidecar files) are listed in `.gitignore`. Git's `pull` only touches tracked files — your collected history, alerts, reboot list, and event log records on the host are untouched by every update.

If you ever change the database schema, the collector calls `init_db()` at startup which uses `CREATE TABLE IF NOT EXISTS`, so new tables are added transparently. Adding new **columns** to existing tables would need a small migration step — flag this when it comes up.

### Dev-machine workflow

Whenever Claude edits code on the dev machine, the user asks Claude to commit and push. Each push produces commits visible at https://github.com/mtg2342/hypervmonitor/commits/main — providing a complete audit trail and easy rollback.

### Authentication

The host only needs to **pull**, which works without auth for a public repo or with cached credentials for a private one. The dev machine needs push access — use GitHub's Personal Access Token (PAT) or SSH key, configured once via `git config` or the Windows Credential Manager.

### Rolling back a bad update

If an update breaks something:

```cmd
cd <path-to-hypervmonitor>
git log --oneline -5            :: find the last good commit
git checkout <commit-sha>       :: detach to that revision
start.bat                       :: run from there
```

To resume tracking the latest later: `git checkout main && git pull`.

---

## 9. Running It

### One-time setup on the host
1. Install Python 3.10+ from python.org (check "Add to PATH")
2. Copy this entire folder to the host
3. Right-click `start.bat` → **Run as administrator** (required because Hyper-V cmdlets, event logs, and the Windows Update COM object all need elevation)

The batch file checks for admin, installs Flask if needed, and starts the server. Open `http://127.0.0.1:5000` in any browser.

### Auto-start on logon (optional)
Create a Task Scheduler task that runs `start.bat` at logon with "Run with highest privileges" checked. The task should be configured to run only when the user is logged on (so the browser can be opened).

### Stopping
Close the console window or press Ctrl+C. The collector thread exits when the Flask process ends.

---

## 10. Configuration

Edit `config.py` to tune behaviour:

```python
POLL_INTERVAL = 30              # base polling interval (seconds)
VHD_POLL_MULTIPLE = 10          # poll VHDs every 10 cycles (5 min)
SYSINFO_POLL_MULTIPLE = 10      # poll system info every 10 cycles (5 min)
EVENTLOG_POLL_MULTIPLE = 4      # scan event log every 4 cycles (2 min)
UPDATES_POLL_MULTIPLE = 120     # check pending updates every 120 cycles (1 hour)
PURGE_CHECK_MULTIPLE = 120      # run retention purge every hour
RETENTION_HOURS = 168           # keep 7 days of raw data
ALERTS_RETENTION_DAYS = 90
EVENTLOG_LOOKBACK_HOURS = 24
EVENTLOG_MAX_EVENTS = 50

ALERT_THRESHOLDS = { ... }       # see file for warning/critical thresholds
SUSTAINED_POLLS = 3              # CPU alerts require N consecutive polls
```

---

## 11. Known Limitations & Notes

1. **Hyper-V Platform required for VM metrics.** If this app is run on a machine that only has Hyper-V management tools (not the platform itself), `Get-VM` fails and the VM grid stays empty. The host metrics, system info, events, and updates all still work — this is the current development machine's state.
2. **Admin elevation required.** Hyper-V cmdlets and `Get-WinEvent` for the Security log both need elevation. The dashboard runs as a regular user but the underlying PowerShell calls need admin.
3. **Local-only by design.** Flask binds to `127.0.0.1`. To access from another machine you'd have to change `FLASK_HOST` in `config.py` *and* open the firewall — not recommended.
4. **Pending updates check is slow** (30+ seconds via COM). It runs in the collector thread, so it doesn't block API requests, but it can hold up other collections in the same cycle. That's why it runs only once per hour.
5. **No authentication.** Anyone with shell access to this machine can hit `127.0.0.1:5000`. Since the data is read-only and the alert dismiss endpoint is the only mutator, this is acceptable for a single-admin host.
6. **PowerShell argument quoting.** Calculated properties (`@{N='Name';E={...}}`) must use single quotes, not double, because subprocess strips inner double quotes. This was an early bug — fixed in `collector.py`.

---

## 12. Conversation History & Design Decisions

### Why Python + Flask (not Node, .NET, or pure PowerShell)?
- Considered: PowerShell-only (too painful to write a web UI), .NET/C# (heavier dev, overkill), Node.js (WMI integration is poor).
- Python wins for: simple subprocess interop, lightweight runtime, easy database integration, fast to develop. The `wmi` Python package was explicitly avoided because of threading/COM issues — shelling out to PowerShell is more reliable.

### Why subprocess PowerShell rather than a single long-running PowerShell process?
- Subprocess cost is ~300 ms per spawn; over 30 seconds that's negligible.
- Stateless processes are simpler — no need to handle PowerShell session recovery or memory leaks.

### Why SQLite over an in-memory store?
- Persistence across restarts so history is preserved.
- WAL mode handles concurrent reads/writes between the collector thread and Flask request threads with no extra locking code.

### Why poll every 30 seconds, not every second?
- 30 s is enough granularity for capacity planning and trend analysis on a host with 5 VMs.
- A 1-second poll would still be lightweight CPU-wise but would generate ~30× more rows for the same retention window.

### Why a dark theme?
- Easier on the eyes for server monitoring screens left open all day.
- Lower screen power draw on OLED monitors.

### Why one bundled PR-style change for this whole feature?
- Single user, single host, internal tooling — splitting into many small commits provides no review value.

### Reboot history detection — why 6005/6006/6008/1074/41?
- 6005: Event Log service started (proxy for boot)
- 6006: Event Log service stopped (proxy for clean shutdown)
- 6008: Last shutdown was unexpected
- 1074: A user or process initiated shutdown/restart (includes reason)
- 41: Kernel-Power critical — system rebooted without cleanly shutting down

These five together cover the vast majority of reboot causes worth distinguishing.

### Event log scope
- Only **Level 1 (Critical)** and **Level 2 (Error)** events are pulled, from the **System** and **Application** logs. The Security log is intentionally excluded — it's high-volume and dominated by audit noise rather than actionable health signals.

### Why query updates via COM rather than `Get-HotFix` or `Get-WUList`?
- `Get-HotFix` only shows installed updates, not pending ones.
- `Get-WUList` requires the third-party `PSWindowsUpdate` module, which we don't want to require.
- `Microsoft.Update.Session` is built into every Windows install and is the canonical source.

---

## 13. Verification Steps

After deploying to the actual Hyper-V host:

1. Run `start.bat` as admin → console should show "Database initialized" and "Starting Hyper-V Monitor at http://127.0.0.1:5000"
2. Open the browser → host CPU, Memory, Disk I/O, Storage cards should populate within 5 seconds
3. After 30 seconds, the VM grid should populate with one card per VM
4. After 1 minute, sparklines should start drawing
5. The System Health card should show OS name, uptime, last boot time, and "Checking..." for updates (then a count after ~1 minute)
6. The Recent Events card should populate with any Critical/Error events from the last 24 hours
7. Click each tab in the detail charts (CPU / Memory / Network / Disk I/O / Storage) — all should render
8. Click each time range (1H/6H/24H/7D) — charts should re-fetch and update
9. Task Manager: the Python process should sit at <30 MB RAM and <1% CPU between polls

---

## 14. Where to Look If Something Goes Wrong

| Symptom | Where to look |
|---|---|
| Dashboard loads but no data | Console output from `python app.py` for PowerShell errors |
| VM cards say "Waiting for VM data..." forever | Confirm Hyper-V Platform is installed: `Get-WindowsFeature -Name Hyper-V` or `Get-VM` directly in PowerShell |
| Events list stays empty | Try `Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2} -MaxEvents 5` in admin PowerShell |
| Pending updates count stays "Checking..." for >2 min | The COM update searcher can stall; check the console for the "Found N pending Windows updates" log line |
| Alerts not firing | `config.py` — check thresholds; alerts also require `SUSTAINED_POLLS` consecutive readings for CPU |
| Database growing large | `RETENTION_HOURS` controls how much history is kept. 7 days at 30 s × ~10 metrics ≈ 20 MB |

---

## 15. Future Enhancements (not implemented)

- WebSocket push instead of 5-second polling (low priority — current cost is negligible)
- Per-VM detail page with drill-down charts
- Export historical data to CSV
- Email/Slack alert delivery (requires SMTP config — currently the alert banner is the only notifier)
- Dark/light theme toggle (currently dark only)
- Per-process CPU/RAM on the host (would require `Get-Process` polling)
- Network adapter throughput on the host (currently only per-VM)
