"""Dev-only: seed the local DB with synthetic history so chart ranges can be
tested without a real Hyper-V host. Not used in production.

Usage: python seed_test_data.py
"""
import math
import random
import time

import db


def main():
    db.init_db()
    conn = db.get_connection()
    now = time.time()
    rng = random.Random(42)

    def cpu_wave(t):
        # daily sinus + noise, 10..70%
        return 40 + 30 * math.sin(t / 86400 * 2 * math.pi) * 0.7 + rng.uniform(-8, 8)

    # ── raw: last 26 hours at 30s ──────────────────────────────────────
    print("seeding raw (26h @ 30s)...")
    t = now - 26 * 3600
    host_rows, vm_rows = [], []
    while t < now:
        c = max(1, min(99, cpu_wave(t)))
        host_rows.append((t, c, 64 * 2**30, int(20 * 2**30 + 5 * 2**30 * math.sin(t / 7200)),
                          55 + rng.uniform(-5, 5), rng.uniform(0, 8e6), rng.uniform(0, 4e6),
                          45 + rng.uniform(-3, 8), 38 + rng.uniform(-2, 4), None))
        for vm in ("VM-ALPHA", "VM-BETA"):
            bump = 10 if vm == "VM-BETA" else 0
            vm_rows.append((t, vm, "Running", max(1, min(99, cpu_wave(t + 9999) + bump)),
                            8 * 2**30, int(5 * 2**30 + 2**30 * math.sin(t / 3600)),
                            123456, "Ok", rng.uniform(0, 2e6), rng.uniform(0, 6e6),
                            rng.uniform(0, 3e6), rng.uniform(0, 1e6), '["10.0.0.5"]'))
        t += 30
    conn.executemany(
        """INSERT INTO host_metrics
           (ts, cpu_pct, mem_total, mem_avail, mem_pct, disk_read_bps,
            disk_write_bps, cpu_temp_c, disk_temp_c, gpu_temp_c)
           VALUES (?,?,?,?,?,?,?,?,?,?)""", host_rows)
    conn.executemany(
        """INSERT INTO vm_metrics
           (ts, vm_name, state, cpu_usage, mem_assigned, mem_demand,
            uptime_sec, heartbeat, net_sent_bps, net_recv_bps,
            disk_read_bps, disk_write_bps, ip_addresses)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", vm_rows)

    # ── hourly: last 35 days ───────────────────────────────────────────
    print("seeding hourly (35d)...")
    start_h = (int(now // 3600) - 35 * 24) * 3600
    h_host, h_vm = [], []
    for b in range(start_h, int(now // 3600) * 3600, 3600):
        c = max(1, min(99, cpu_wave(b)))
        h_host.append((b, c, c + 10, 50, 65, 4e6, 2e6, 46, 55, 38, 41, None, None, 120))
        for vm in ("VM-ALPHA", "VM-BETA"):
            h_vm.append((b, vm, c * 0.6, c * 0.9, 8 * 2**30, 5 * 2**30, 6 * 2**30,
                         1e6, 3e6, 2e6, 5e5, 120))
    conn.executemany(
        """INSERT OR IGNORE INTO host_metrics_hourly
           (bucket_ts, cpu_pct_avg, cpu_pct_max, mem_pct_avg, mem_pct_max,
            disk_read_avg, disk_write_avg, cpu_temp_avg, cpu_temp_max,
            disk_temp_avg, disk_temp_max, gpu_temp_avg, gpu_temp_max, samples)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", h_host)
    conn.executemany(
        """INSERT OR IGNORE INTO vm_metrics_hourly
           (bucket_ts, vm_name, cpu_usage_avg, cpu_usage_max, mem_assigned_avg,
            mem_demand_avg, mem_demand_max, net_sent_avg, net_recv_avg,
            disk_read_avg, disk_write_avg, samples)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", h_vm)

    # ── daily: last 130 days ───────────────────────────────────────────
    print("seeding daily (130d)...")
    start_d = (int(now // 86400) - 130) * 86400
    d_host, d_vm = [], []
    for b in range(start_d, int(now // 86400) * 86400, 86400):
        c = max(1, min(99, cpu_wave(b)))
        d_host.append((b, c, c + 20, 50, 70, 4e6, 2e6, 46, 60, 38, 44, None, None, 2880))
        for vm in ("VM-ALPHA", "VM-BETA"):
            d_vm.append((b, vm, c * 0.6, c * 0.95, 8 * 2**30, 5 * 2**30, 6.5 * 2**30,
                         1e6, 3e6, 2e6, 5e5, 2880))
    conn.executemany(
        """INSERT OR IGNORE INTO host_metrics_daily
           (bucket_ts, cpu_pct_avg, cpu_pct_max, mem_pct_avg, mem_pct_max,
            disk_read_avg, disk_write_avg, cpu_temp_avg, cpu_temp_max,
            disk_temp_avg, disk_temp_max, gpu_temp_avg, gpu_temp_max, samples)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", d_host)
    conn.executemany(
        """INSERT OR IGNORE INTO vm_metrics_daily
           (bucket_ts, vm_name, cpu_usage_avg, cpu_usage_max, mem_assigned_avg,
            mem_demand_avg, mem_demand_max, net_sent_avg, net_recv_avg,
            disk_read_avg, disk_write_avg, samples)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", d_vm)

    # one volumes snapshot so the dashboard isn't empty
    conn.execute(
        """INSERT INTO host_volumes (ts, drive, label, total, free, pct_used)
           VALUES (?,?,?,?,?,?)""",
        (now, "C:", "System", 500 * 2**30, 200 * 2**30, 60.0))

    conn.commit()
    for tbl in ("host_metrics", "vm_metrics", "host_metrics_hourly",
                "vm_metrics_hourly", "host_metrics_daily", "vm_metrics_daily"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"{tbl}: {n} rows")
    conn.close()


if __name__ == "__main__":
    main()
