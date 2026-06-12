function _cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

let CHART_COLORS = [];
let CHART_DEFAULTS = {};
// Theme-aware colours for datasets that aren't in the VM palette. The host
// line was previously hardcoded '#ffffff', which is invisible on the light
// theme's white card background.
let CHART_HOST_LINE   = '#ffffff';
let CHART_HOST_LINE_2 = '#888888';
let CHART_FREE_BG     = '#2a2e3f';

function refreshChartDefaults() {
    const text2 = _cssVar('--text-2') || '#9197ac';
    const text3 = _cssVar('--text-3') || '#5d6378';
    const grid  = _cssVar('--grid')   || 'rgba(255,255,255,0.04)';
    const cardBg = _cssVar('--bg-elev-1') || 'rgba(17, 21, 31, 0.96)';
    const cardBorder = _cssVar('--border') || '#1f2535';
    const textPrimary = _cssVar('--text') || '#e6e9f2';
    const accent = _cssVar('--accent') || '#5b9dff';
    const green  = _cssVar('--green')  || '#3ddc97';
    const amber  = _cssVar('--amber')  || '#f5b14a';
    const red    = _cssVar('--red')    || '#ff6b6b';
    const purple = _cssVar('--purple') || '#a78bfa';

    CHART_COLORS = [accent, green, amber, red, purple, '#56cfe1', '#ec98c5', '#fcd34d'];

    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    CHART_HOST_LINE   = isLight ? '#475569' : '#ffffff';
    CHART_HOST_LINE_2 = isLight ? '#94a3b8' : '#888888';
    CHART_FREE_BG     = isLight ? '#e2e8f0' : '#2a2e3f';

    CHART_DEFAULTS = {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: {
                labels: {
                    color: text2,
                    font: { size: 11, family: 'Inter, system-ui, sans-serif' },
                    boxWidth: 12,
                    boxHeight: 12,
                    padding: 12,
                    usePointStyle: true,
                    pointStyle: 'circle',
                }
            },
            tooltip: {
                backgroundColor: cardBg,
                borderColor: cardBorder,
                borderWidth: 1,
                titleColor: textPrimary,
                bodyColor: text2,
                padding: 10,
                cornerRadius: 6,
                titleFont: { family: 'Inter, system-ui, sans-serif', size: 12, weight: '600' },
                bodyFont:  { family: 'Inter, system-ui, sans-serif', size: 11 },
                displayColors: true,
                boxPadding: 5,
            },
        },
        scales: {
            x: {
                ticks: { color: text3, font: { size: 10, family: 'Inter, system-ui, sans-serif' }, maxTicksLimit: 10 },
                grid:  { color: grid, drawBorder: false },
                border: { display: false },
            },
            y: {
                ticks: { color: text3, font: { size: 10, family: 'Inter, system-ui, sans-serif' } },
                grid:  { color: grid, drawBorder: false },
                border: { display: false },
                beginAtZero: true,
            },
        },
        elements: { line: { borderJoinStyle: 'round', borderCapStyle: 'round' } },
    };
}

// Initialize on script load
refreshChartDefaults();

function hexToRgba(hex, alpha) {
    // Colours sourced from CSS vars may not be hex — pass those through
    // untouched rather than producing an invalid colour string (Chart.js
    // renders invalid colours as opaque black).
    if (!hex || hex[0] !== '#' || hex.length < 7) return hex;
    const h = hex.replace('#', '');
    const r = parseInt(h.substring(0, 2), 16);
    const g = parseInt(h.substring(2, 4), 16);
    const b = parseInt(h.substring(4, 6), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

function createSparkline(canvasId, maxPoints, color) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    const c = color || '#5b9dff';
    const data = [];
    const labels = [];
    const chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                data: data,
                borderColor: c,
                borderWidth: 1.5,
                pointRadius: 0,
                fill: true,
                backgroundColor: hexToRgba(c, 0.14),
                tension: 0.35,
            }],
        },
        options: {
            responsive: false,
            animation: false,
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
            scales: {
                x: { display: false },
                y: { display: false, min: 0 },
            },
            elements: { line: { borderJoinStyle: 'round', borderCapStyle: 'round' } },
        },
    });

    return {
        chart,
        push(val) {
            data.push(val);
            labels.push('');
            if (data.length > (maxPoints || 30)) {
                data.shift();
                labels.shift();
            }
            chart.update('none');
        },
        setColor(color) {
            chart.data.datasets[0].borderColor = color;
            chart.data.datasets[0].backgroundColor = hexToRgba(color, 0.14);
        },
        setMax(max) {
            chart.options.scales.y.max = max;
        },
    };
}

function createVmSparkCanvas(id) {
    const canvas = document.createElement('canvas');
    canvas.id = id;
    canvas.width = 100;
    canvas.height = 28;
    canvas.className = 'vm-metric-spark';
    return canvas;
}

let detailChart = null;
let _detailChartRange = '1h';

function destroyDetailChart() {
    if (detailChart) {
        detailChart.destroy();
        detailChart = null;
    }
}

function formatTime(epoch) {
    const d = new Date(epoch * 1000);
    const r = _detailChartRange;
    if (r === '7d') {
        return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit' });
    }
    if (r === '30d' || r === '4m') {
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    }
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function showDetailChartMessage(text, sub) {
    destroyDetailChart();
    const container = document.querySelector('.chart-container');
    if (!container) return;
    // Stash an overlay above the canvas; clear it on next chart build.
    const canvas = document.getElementById('detailChart');
    if (canvas) canvas.style.display = 'none';
    let overlay = container.querySelector('.detail-chart-empty');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.className = 'detail-chart-empty';
        container.appendChild(overlay);
    }
    overlay.innerHTML =
        '<div class="detail-chart-empty-title">' + text + '</div>' +
        (sub ? '<div class="detail-chart-empty-sub">' + sub + '</div>' : '');
}

function clearDetailChartMessage() {
    const container = document.querySelector('.chart-container');
    if (!container) return;
    const overlay = container.querySelector('.detail-chart-empty');
    if (overlay) overlay.remove();
    const canvas = document.getElementById('detailChart');
    if (canvas) canvas.style.display = '';
}

function buildDetailChart(tab, historyData, vmHistoryData, rangeKey) {
    if (rangeKey) _detailChartRange = rangeKey;
    destroyDetailChart();
    clearDetailChartMessage();

    // Empty-data fallback so the chart never silently shows nothing.
    const hostEmpty = !historyData || historyData.length === 0;
    const vmsEmpty  = !vmHistoryData || vmHistoryData.length === 0;
    const needsHost = (tab === 'cpu');
    const needsVm   = (tab !== 'cpu' && tab !== 'storage');
    if (tab !== 'storage' && (
        (needsHost && needsVm && hostEmpty && vmsEmpty) ||
        (needsHost && !needsVm && hostEmpty) ||
        (!needsHost && needsVm && vmsEmpty)
    )) {
        const longRange = (rangeKey === '7d' || rangeKey === '30d' || rangeKey === '4m');
        if (longRange) {
            showDetailChartMessage(
                'No aggregated data yet for this range',
                rangeKey === '4m'
                    ? 'Daily roll-ups populate after the host has been up for at least one full day. Check back tomorrow, or pick a shorter range.'
                    : 'Hourly roll-ups run once an hour. If you just started the dashboard, give it ~60 minutes to fill, or pick a shorter range (1H / 6H / 24H).'
            );
        } else if (needsVm && vmsEmpty) {
            showDetailChartMessage(
                'No VM data',
                'This tab plots per-VM metrics. Either there are no running VMs on the host, or the Hyper-V Platform feature isn’t installed.'
            );
        } else {
            showDetailChartMessage('No data available', '');
        }
        return;
    }

    const ctx = document.getElementById('detailChart');
    if (!ctx) return;

    const labels = [];
    const datasets = [];

    if (tab === 'cpu') {
        if (historyData.length > 0) {
            labels.push(...historyData.map(r => formatTime(r.ts)));
            datasets.push({
                label: 'Host',
                data: historyData.map(r => r.cpu_pct),
                borderColor: CHART_HOST_LINE,
                borderWidth: 2,
                borderDash: [4, 4],
                pointRadius: 0,
                tension: 0.3,
                fill: false,
            });
        }
        const vmGroups = groupByVm(vmHistoryData);
        let ci = 0;
        for (const [vm, rows] of Object.entries(vmGroups)) {
            if (labels.length === 0) labels.push(...rows.map(r => formatTime(r.ts)));
            datasets.push({
                label: vm,
                data: rows.map(r => r.cpu_usage),
                borderColor: CHART_COLORS[ci % CHART_COLORS.length],
                borderWidth: 1.5,
                pointRadius: 0,
                tension: 0.3,
                fill: false,
            });
            ci++;
        }
        detailChart = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                ...CHART_DEFAULTS,
                scales: {
                    ...CHART_DEFAULTS.scales,
                    y: { ...CHART_DEFAULTS.scales.y, max: 100, title: { display: true, text: 'CPU %', color: '#5a5e72' } },
                },
            },
        });
    } else if (tab === 'memory') {
        const vmGroups = groupByVm(vmHistoryData);
        let ci = 0;
        for (const [vm, rows] of Object.entries(vmGroups)) {
            if (labels.length === 0) labels.push(...rows.map(r => formatTime(r.ts)));
            datasets.push({
                label: vm + ' (assigned)',
                data: rows.map(r => bytesToGB(r.mem_assigned)),
                borderColor: CHART_COLORS[ci % CHART_COLORS.length],
                backgroundColor: hexToRgba(CHART_COLORS[ci % CHART_COLORS.length], 0.15),
                borderWidth: 1.5,
                pointRadius: 0,
                tension: 0.3,
                fill: true,
            });
            datasets.push({
                label: vm + ' (demand)',
                data: rows.map(r => bytesToGB(r.mem_demand)),
                borderColor: CHART_COLORS[ci % CHART_COLORS.length],
                borderWidth: 1,
                borderDash: [3, 3],
                pointRadius: 0,
                tension: 0.3,
                fill: false,
            });
            ci++;
        }
        detailChart = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                ...CHART_DEFAULTS,
                scales: {
                    ...CHART_DEFAULTS.scales,
                    y: { ...CHART_DEFAULTS.scales.y, title: { display: true, text: 'GB', color: '#5a5e72' } },
                },
            },
        });
    } else if (tab === 'network') {
        const vmGroups = groupByVm(vmHistoryData);
        let ci = 0;
        for (const [vm, rows] of Object.entries(vmGroups)) {
            if (labels.length === 0) labels.push(...rows.map(r => formatTime(r.ts)));
            datasets.push({
                label: vm + ' sent',
                data: rows.map(r => bytesToMBps(r.net_sent_bps)),
                borderColor: CHART_COLORS[ci % CHART_COLORS.length],
                borderWidth: 1.5,
                pointRadius: 0,
                tension: 0.3,
                fill: false,
            });
            datasets.push({
                label: vm + ' recv',
                data: rows.map(r => bytesToMBps(r.net_recv_bps)),
                borderColor: CHART_COLORS[ci % CHART_COLORS.length],
                borderWidth: 1,
                borderDash: [3, 3],
                pointRadius: 0,
                tension: 0.3,
                fill: false,
            });
            ci++;
        }
        detailChart = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                ...CHART_DEFAULTS,
                scales: {
                    ...CHART_DEFAULTS.scales,
                    y: { ...CHART_DEFAULTS.scales.y, title: { display: true, text: 'MB/s', color: '#5a5e72' } },
                },
            },
        });
    } else if (tab === 'diskio') {
        if (historyData.length > 0) {
            labels.push(...historyData.map(r => formatTime(r.ts)));
            datasets.push({
                label: 'Host Read',
                data: historyData.map(r => bytesToMBps(r.disk_read_bps)),
                borderColor: CHART_HOST_LINE,
                borderWidth: 1.5,
                borderDash: [4, 4],
                pointRadius: 0, tension: 0.3, fill: false,
            });
            datasets.push({
                label: 'Host Write',
                data: historyData.map(r => bytesToMBps(r.disk_write_bps)),
                borderColor: CHART_HOST_LINE_2,
                borderWidth: 1.5,
                borderDash: [4, 4],
                pointRadius: 0, tension: 0.3, fill: false,
            });
        }
        const vmGroups = groupByVm(vmHistoryData);
        let ci = 0;
        for (const [vm, rows] of Object.entries(vmGroups)) {
            if (labels.length === 0) labels.push(...rows.map(r => formatTime(r.ts)));
            datasets.push({
                label: vm + ' read',
                data: rows.map(r => bytesToMBps(r.disk_read_bps)),
                borderColor: CHART_COLORS[ci % CHART_COLORS.length],
                borderWidth: 1.5,
                pointRadius: 0, tension: 0.3, fill: false,
            });
            datasets.push({
                label: vm + ' write',
                data: rows.map(r => bytesToMBps(r.disk_write_bps)),
                borderColor: CHART_COLORS[ci % CHART_COLORS.length],
                borderWidth: 1,
                borderDash: [3, 3],
                pointRadius: 0, tension: 0.3, fill: false,
            });
            ci++;
        }
        detailChart = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                ...CHART_DEFAULTS,
                scales: {
                    ...CHART_DEFAULTS.scales,
                    y: { ...CHART_DEFAULTS.scales.y, title: { display: true, text: 'MB/s', color: '#5a5e72' } },
                },
            },
        });
    } else if (tab === 'storage') {
        buildStorageChart(ctx);
    }
}

function buildStorageChart(ctx) {
    fetch('/api/vhd/current')
        .then(r => r.json())
        .then(vhds => {
            fetch('/api/host/current')
                .then(r => r.json())
                .then(host => {
                    const labels = [];
                    const usedData = [];
                    const freeData = [];

                    if (host.volumes) {
                        for (const v of host.volumes) {
                            labels.push(v.drive + ':' + (v.label && v.label.trim() ? ' ' + v.label : ''));
                            usedData.push(bytesToGB(v.total - v.free));
                            freeData.push(bytesToGB(v.free));
                        }
                    }
                    for (const vhd of vhds) {
                        labels.push(vhd.vm_name + ' VHD');
                        usedData.push(bytesToGB(vhd.file_size));
                        freeData.push(bytesToGB(vhd.max_size - vhd.file_size));
                    }

                    destroyDetailChart();
                    detailChart = new Chart(ctx, {
                        type: 'bar',
                        data: {
                            labels,
                            datasets: [
                                { label: 'Used', data: usedData, backgroundColor: '#4c9aff' },
                                { label: 'Free', data: freeData, backgroundColor: CHART_FREE_BG },
                            ],
                        },
                        options: {
                            ...CHART_DEFAULTS,
                            indexAxis: 'y',
                            scales: {
                                x: { ...CHART_DEFAULTS.scales.x, stacked: true, title: { display: true, text: 'GB', color: '#5a5e72' } },
                                y: { ...CHART_DEFAULTS.scales.y, stacked: true, beginAtZero: undefined },
                            },
                            plugins: {
                                ...CHART_DEFAULTS.plugins,
                                tooltip: {
                                    ...CHART_DEFAULTS.plugins.tooltip,
                                    callbacks: {
                                        label: function(ctx) {
                                            return ctx.dataset.label + ': ' + ctx.parsed.x.toFixed(1) + ' GB';
                                        }
                                    }
                                }
                            }
                        },
                    });
                });
        });
}

function groupByVm(data) {
    const groups = {};
    for (const row of data) {
        const vm = row.vm_name;
        if (!groups[vm]) groups[vm] = [];
        groups[vm].push(row);
    }
    return groups;
}

function bytesToGB(b) {
    if (b == null) return 0;
    return b / (1024 * 1024 * 1024);
}

function bytesToMBps(b) {
    if (b == null) return 0;
    return b / (1024 * 1024);
}

function formatBytes(b) {
    if (b == null) return '--';
    if (b >= 1e12) return (b / 1e12).toFixed(1) + ' TB';
    if (b >= 1e9) return (b / 1e9).toFixed(1) + ' GB';
    if (b >= 1e6) return (b / 1e6).toFixed(1) + ' MB';
    if (b >= 1e3) return (b / 1e3).toFixed(0) + ' KB';
    return b + ' B';
}

function formatRate(bps) {
    if (bps == null) return '--';
    if (bps >= 1e9) return (bps / 1e9).toFixed(1) + ' GB/s';
    if (bps >= 1e6) return (bps / 1e6).toFixed(1) + ' MB/s';
    if (bps >= 1e3) return (bps / 1e3).toFixed(0) + ' KB/s';
    return bps.toFixed(0) + ' B/s';
}
