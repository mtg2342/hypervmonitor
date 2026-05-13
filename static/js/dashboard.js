let currentRange = '1h';
let currentTab = 'cpu';

const sparklines = {};
const vmSparklines = {};

// ── Initialization ──────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    sparklines.cpu    = createSparkline('sparkHostCpu',   30, '#5b9dff');
    sparklines.mem    = createSparkline('sparkHostMem',   30, '#3ddc97');
    sparklines.diskR  = createSparkline('sparkHostDiskR', 30, '#f5b14a');
    sparklines.diskW  = createSparkline('sparkHostDiskW', 30, '#a78bfa');

    if (sparklines.cpu) sparklines.cpu.setMax(100);
    if (sparklines.mem) sparklines.mem.setMax(100);

    document.getElementById('rangeButtons').addEventListener('click', e => {
        if (e.target.classList.contains('range-btn')) {
            document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            currentRange = e.target.dataset.range;
            refreshCharts();
        }
    });

    document.getElementById('chartTabs').addEventListener('click', e => {
        if (e.target.classList.contains('tab-btn')) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            currentTab = e.target.dataset.tab;
            refreshCharts();
        }
    });

    fetchLiveData();
    fetchAlerts();
    fetchSystemInfo();
    fetchSystemEvents();
    refreshCharts();

    setInterval(fetchLiveData, 5000);
    setInterval(fetchAlerts, 10000);
    setInterval(fetchSystemInfo, 30000);
    setInterval(fetchSystemEvents, 60000);
    setInterval(refreshCharts, 60000);
});


// ── System Info ─────────────────────────────────────────────────────────────

function fetchSystemInfo() {
    fetch('/api/system/info')
        .then(r => r.json())
        .then(data => updateSystemInfo(data.info, data.reboots))
        .catch(() => {});
}

function updateSystemInfo(info, reboots) {
    if (!info) return;

    document.getElementById('sysHost').textContent = info.host_name || '--';

    const osText = info.os_name
        ? info.os_name.replace('Microsoft ', '') + (info.os_version ? ' (' + info.os_version + ')' : '')
        : '--';
    document.getElementById('sysOs').textContent = osText;

    document.getElementById('sysUptime').textContent = info.uptime_sec
        ? formatUptime(info.uptime_sec)
        : '--';

    document.getElementById('sysLastBoot').textContent = info.last_boot_ts
        ? formatDateTime(info.last_boot_ts)
        : '--';

    const updatesEl = document.getElementById('sysUpdates');
    if (info.updates_pending == null) {
        updatesEl.textContent = 'Checking...';
        updatesEl.className = 'sysinfo-value text-dim';
    } else if (info.updates_pending === 0) {
        updatesEl.textContent = 'None';
        updatesEl.className = 'sysinfo-value text-green';
    } else {
        updatesEl.textContent = info.updates_pending + ' available';
        updatesEl.className = 'sysinfo-value has-updates';
    }

    const list = document.getElementById('rebootList');
    if (!reboots || reboots.length === 0) {
        list.innerHTML = '<li class="text-dim">No reboot history yet</li>';
    } else {
        list.innerHTML = reboots.map(r => `
            <li>
                <span class="reboot-time">${formatDateTime(r.ts_boot)}</span>
                <span class="reboot-reason">${escapeHtml(r.reason || 'boot')}</span>
            </li>
        `).join('');
    }
}


// ── System Events ───────────────────────────────────────────────────────────

function fetchSystemEvents() {
    fetch('/api/system/events?limit=50&hours=24')
        .then(r => r.json())
        .then(events => updateEventsList(events))
        .catch(() => {});
}

function updateEventsList(events) {
    const list = document.getElementById('eventsList');
    const count = document.getElementById('eventsCount');

    if (!events || events.length === 0) {
        list.innerHTML = '<div class="text-dim no-events">No critical or error events in the last 24 hours</div>';
        count.textContent = '';
        return;
    }

    count.textContent = '(' + events.length + ' in last 24h)';

    list.innerHTML = events.map(e => {
        const levelName = (e.level === 1) ? 'critical' : 'error';
        const time = formatRelativeTime(e.ts_event);
        return `
            <div class="event-item">
                <span class="event-level ${levelName}">${levelName}</span>
                <div class="event-body">
                    <div class="event-source">
                        ${escapeHtml(e.source || 'Unknown')}
                        <span class="event-id">[${e.log_name}#${e.event_id}]</span>
                    </div>
                    <div class="event-msg">${escapeHtml(e.message || '')}</div>
                </div>
                <span class="event-meta">${time}</span>
            </div>
        `;
    }).join('');
}


// ── Helpers ─────────────────────────────────────────────────────────────────

function formatDateTime(epoch) {
    const d = new Date(epoch * 1000);
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) +
        ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatRelativeTime(epoch) {
    const diff = Date.now() / 1000 - epoch;
    if (diff < 60) return Math.floor(diff) + 's ago';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
}


// ── Live Data ───────────────────────────────────────────────────────────────

function fetchLiveData() {
    Promise.all([
        fetch('/api/host/current').then(r => r.json()),
        fetch('/api/vms/current').then(r => r.json()),
    ])
    .then(([host, vms]) => {
        updateHostCards(host);
        updateVmGrid(vms);
        document.getElementById('lastPoll').textContent =
            'Last update: ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    })
    .catch(err => {
        document.getElementById('lastPoll').textContent = 'Connection error';
    });
}


// ── Host Cards ──────────────────────────────────────────────────────────────

function updateHostCards(host) {
    const m = host.metrics;
    if (!m) return;

    setKpiValue('hostCpuVal', m.cpu_pct, '%');
    setKpiValue('hostMemVal', m.mem_pct, '%');

    document.getElementById('hostDiskReadVal').innerHTML  = formatRateHtml(m.disk_read_bps);
    document.getElementById('hostDiskWriteVal').innerHTML = formatRateHtml(m.disk_write_bps);

    if (sparklines.cpu && m.cpu_pct != null)  sparklines.cpu.push(m.cpu_pct);
    if (sparklines.mem && m.mem_pct != null)  sparklines.mem.push(m.mem_pct);
    if (sparklines.diskR && m.disk_read_bps != null)
        sparklines.diskR.push(m.disk_read_bps / (1024 * 1024));
    if (sparklines.diskW && m.disk_write_bps != null)
        sparklines.diskW.push(m.disk_write_bps / (1024 * 1024));

    updateVolumes(host.volumes);
}

function setKpiValue(elementId, pct, suffix) {
    const el = document.getElementById(elementId);
    if (pct == null) {
        el.innerHTML = '--<span style="font-size:16px;color:var(--text-3)">' + (suffix || '') + '</span>';
        el.className = 'card-value';
        return;
    }
    const val = Math.round(pct);
    el.innerHTML = val + '<span style="font-size:16px;color:var(--text-3);margin-left:1px">' + suffix + '</span>';
    el.className = 'card-value';
    if (pct >= 95)      el.classList.add('is-crit');
    else if (pct >= 85) el.classList.add('is-warn');
    else if (pct >= 50) el.classList.add('text-blue');
    else                el.classList.add('is-ok');
}

function formatRateHtml(bps) {
    if (bps == null) return '--';
    let val, unit;
    if (bps >= 1e9)      { val = (bps / 1e9).toFixed(1); unit = 'GB/s'; }
    else if (bps >= 1e6) { val = (bps / 1e6).toFixed(1); unit = 'MB/s'; }
    else if (bps >= 1e3) { val = (bps / 1e3).toFixed(0); unit = 'KB/s'; }
    else                 { val = Math.round(bps); unit = 'B/s'; }
    return val + '<span style="font-size:14px;color:var(--text-3);margin-left:4px;font-weight:500">' + unit + '</span>';
}

function updateVolumes(volumes) {
    const container = document.getElementById('volumesList');
    if (!volumes || volumes.length === 0) {
        container.innerHTML = '<div class="text-dim">No data</div>';
        return;
    }
    container.innerHTML = volumes.map(v => {
        const pct = v.pct_used || 0;
        const fillClass = pct >= 95 ? 'crit' : pct >= 85 ? 'warn' : '';
        const usedGB = ((v.total - v.free) / 1e9).toFixed(0);
        const totalGB = (v.total / 1e9).toFixed(0);
        return `
            <div class="volume-row">
                <span class="volume-label">${v.drive}:</span>
                <div class="volume-bar-bg">
                    <div class="volume-bar-fill ${fillClass}" style="width:${pct}%"></div>
                </div>
                <span class="volume-text">${usedGB} / ${totalGB} GB (${pct.toFixed(0)}%)</span>
            </div>
        `;
    }).join('');
}


// ── VM Grid ─────────────────────────────────────────────────────────────────

function updateVmGrid(vms) {
    const grid = document.getElementById('vmGrid');
    const countEl = document.getElementById('vmCount');
    if (countEl) countEl.textContent = vms && vms.length ? vms.length : '';

    if (!vms || vms.length === 0) {
        if (grid.children.length === 0) {
            grid.innerHTML = '<div class="no-data">No VMs detected on this host</div>';
        }
        return;
    }

    for (const vm of vms) {
        let card = document.getElementById('vm-' + cssId(vm.vm_name));
        if (!card) {
            card = createVmCard(vm);
            grid.appendChild(card);
        }
        updateVmCard(card, vm);
    }

    const currentNames = new Set(vms.map(v => 'vm-' + cssId(v.vm_name)));
    for (const child of Array.from(grid.children)) {
        if (child.id && child.id.startsWith('vm-') && !currentNames.has(child.id)) {
            child.remove();
        }
    }
}

function createVmCard(vm) {
    const card = document.createElement('div');
    card.className = 'card vm-card';
    card.id = 'vm-' + cssId(vm.vm_name);
    card.innerHTML = `
        <div class="vm-header">
            <span class="vm-name">${escapeHtml(vm.vm_name)}</span>
            <span class="vm-state" data-field="state"></span>
        </div>
        <div class="vm-metrics">
            <div class="vm-metric">
                <span class="vm-metric-label">CPU</span>
                <span class="vm-metric-value" data-field="cpu"></span>
            </div>
            <div class="vm-metric">
                <span class="vm-metric-label">Memory</span>
                <span class="vm-metric-value" data-field="mem"></span>
            </div>
            <div class="vm-metric">
                <span class="vm-metric-label">Net In</span>
                <span class="vm-metric-value" data-field="netin"></span>
            </div>
            <div class="vm-metric">
                <span class="vm-metric-label">Net Out</span>
                <span class="vm-metric-value" data-field="netout"></span>
            </div>
            <div class="vm-metric">
                <span class="vm-metric-label">Disk Read</span>
                <span class="vm-metric-value" data-field="diskr"></span>
            </div>
            <div class="vm-metric">
                <span class="vm-metric-label">Disk Write</span>
                <span class="vm-metric-value" data-field="diskw"></span>
            </div>
            <div class="vm-metric">
                <span class="vm-metric-label">Heartbeat</span>
                <span class="vm-metric-value" data-field="hb"></span>
            </div>
            <div class="vm-metric">
                <span class="vm-metric-label">Uptime</span>
                <span class="vm-metric-value" data-field="uptime"></span>
            </div>
        </div>
    `;
    return card;
}

function updateVmCard(card, vm) {
    const stateEl = card.querySelector('[data-field="state"]');
    const state = (vm.state || 'Unknown').toLowerCase();
    stateEl.textContent = vm.state || 'Unknown';
    stateEl.className = 'vm-state ' + state;

    const cpuEl = card.querySelector('[data-field="cpu"]');
    cpuEl.textContent = vm.cpu_usage != null ? vm.cpu_usage.toFixed(0) + '%' : '--';
    cpuEl.className = 'vm-metric-value';
    if (vm.cpu_usage >= 90) cpuEl.classList.add('text-red');
    else if (vm.cpu_usage >= 70) cpuEl.classList.add('text-orange');

    const assigned = vm.mem_assigned || 0;
    const demand = vm.mem_demand || 0;
    card.querySelector('[data-field="mem"]').textContent =
        formatBytes(demand) + ' / ' + formatBytes(assigned);

    card.querySelector('[data-field="netin"]').textContent = formatRate(vm.net_recv_bps);
    card.querySelector('[data-field="netout"]').textContent = formatRate(vm.net_sent_bps);
    card.querySelector('[data-field="diskr"]').textContent = formatRate(vm.disk_read_bps);
    card.querySelector('[data-field="diskw"]').textContent = formatRate(vm.disk_write_bps);

    const hbEl = card.querySelector('[data-field="hb"]');
    const hb = vm.heartbeat || 'N/A';
    const shortHb = hb.replace('OkApplications', '').replace('Ok', 'OK');
    hbEl.textContent = shortHb;
    hbEl.className = 'vm-metric-value';
    if (hb.includes('Ok') || hb === 'N/A') hbEl.classList.add('text-green');
    else hbEl.classList.add('text-red');

    const uptimeSec = vm.uptime_sec || 0;
    card.querySelector('[data-field="uptime"]').textContent = formatUptime(uptimeSec);
}


// ── Detail Charts ───────────────────────────────────────────────────────────

function refreshCharts() {
    if (currentTab === 'storage') {
        buildDetailChart('storage', [], []);
        return;
    }

    Promise.all([
        fetch('/api/host/history?range=' + currentRange).then(r => r.json()),
        fetch('/api/vms/history?range=' + currentRange).then(r => r.json()),
    ])
    .then(([hostHistory, vmHistory]) => {
        buildDetailChart(currentTab, hostHistory, vmHistory);
    })
    .catch(() => {});
}


// ── Helpers ─────────────────────────────────────────────────────────────────

function cssId(name) {
    return name.replace(/[^a-zA-Z0-9-_]/g, '_');
}

function formatUptime(sec) {
    if (!sec || sec <= 0) return 'Off';
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
}
