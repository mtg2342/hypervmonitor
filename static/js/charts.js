const CHART_COLORS = [
    '#5b9dff', '#3ddc97', '#f5b14a', '#ff6b6b',
    '#a78bfa', '#56cfe1', '#ec98c5', '#fcd34d',
];

const CHART_DEFAULTS = {
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: {
            labels: {
                color: '#9197ac',
                font: { size: 11, family: 'Inter, system-ui, sans-serif' },
                boxWidth: 12,
                boxHeight: 12,
                padding: 12,
                usePointStyle: true,
                pointStyle: 'circle',
            }
        },
        tooltip: {
            backgroundColor: 'rgba(17, 21, 31, 0.96)',
            borderColor: '#1f2535',
            borderWidth: 1,
            titleColor: '#e6e9f2',
            bodyColor: '#9197ac',
            padding: 10,
            cornerRadius: 6,
            titleFont: { family: 'Inter, system-ui, sans-serif', size: 12, weight: '600' },
            bodyFont: { family: 'Inter, system-ui, sans-serif', size: 11 },
            displayColors: true,
            boxPadding: 5,
        },
    },
    scales: {
        x: {
            ticks: {
                color: '#5d6378',
                font: { size: 10, family: 'Inter, system-ui, sans-serif' },
                maxTicksLimit: 10,
            },
            grid: { color: 'rgba(255,255,255,0.04)', drawBorder: false },
            border: { display: false },
        },
        y: {
            ticks: {
                color: '#5d6378',
                font: { size: 10, family: 'Inter, system-ui, sans-serif' },
            },
            grid: { color: 'rgba(255,255,255,0.04)', drawBorder: false },
            border: { display: false },
            beginAtZero: true,
        },
    },
    elements: {
        line: { borderJoinStyle: 'round', borderCapStyle: 'round' },
    },
};

function hexToRgba(hex, alpha) {
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

function destroyDetailChart() {
    if (detailChart) {
        detailChart.destroy();
        detailChart = null;
    }
}

function formatTime(epoch) {
    const d = new Date(epoch * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function buildDetailChart(tab, historyData, vmHistoryData) {
    destroyDetailChart();
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
                borderColor: '#ffffff',
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
                backgroundColor: CHART_COLORS[ci % CHART_COLORS.length].replace(')', ', 0.15)').replace('#', 'rgba('),
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
                borderColor: '#ffffff',
                borderWidth: 1.5,
                borderDash: [4, 4],
                pointRadius: 0, tension: 0.3, fill: false,
            });
            datasets.push({
                label: 'Host Write',
                data: historyData.map(r => bytesToMBps(r.disk_write_bps)),
                borderColor: '#888888',
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
                                { label: 'Free', data: freeData, backgroundColor: '#2a2e3f' },
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
