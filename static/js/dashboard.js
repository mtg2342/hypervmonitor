let currentRange = '1h';
let currentTab = 'cpu';
let currentView = 'dashboard';

// Alert History state
let alertFilter = { status: 'all', severity: 'all' };
let alertPage = { limit: 50, offset: 0, total: 0 };
let alertRefreshTimer = null;

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

    // View switching
    document.getElementById('mainNav').addEventListener('click', e => {
        const btn = e.target.closest('.nav-btn');
        if (!btn) return;
        switchView(btn.dataset.view);
    });

    // Filter pills (alert history + RDP)
    document.querySelectorAll('.filter-pills').forEach(group => {
        group.addEventListener('click', e => {
            const pill = e.target.closest('.pill');
            if (!pill) return;
            group.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            const filterKey = group.dataset.filter;
            const value = pill.dataset.value;
            if (filterKey === 'rdp') {
                rdpFilter = value;
                fetchRdpLogins();
            } else {
                alertFilter[filterKey] = value;
                alertPage.offset = 0;
                fetchAlertHistory();
            }
        });
    });

    // Pagination
    document.getElementById('alertPrev').addEventListener('click', () => {
        if (alertPage.offset > 0) {
            alertPage.offset = Math.max(0, alertPage.offset - alertPage.limit);
            fetchAlertHistory();
        }
    });
    document.getElementById('alertNext').addEventListener('click', () => {
        if (alertPage.offset + alertPage.limit < alertPage.total) {
            alertPage.offset += alertPage.limit;
            fetchAlertHistory();
        }
    });

    fetchLiveData();
    fetchAlerts();
    fetchSystemInfo();
    fetchSystemEvents();
    fetchBandwidth();
    fetchSecurityForReboot();
    fetchVeeam();
    refreshCharts();

    setInterval(fetchLiveData, 5000);
    setInterval(fetchAlerts, 10000);
    setInterval(fetchSystemInfo, 30000);
    setInterval(fetchSystemEvents, 60000);
    setInterval(fetchBandwidth, 300000);
    setInterval(fetchSecurityForReboot, 60000);   // refresh banner periodically
    setInterval(fetchVeeam, 300000);              // 5 min — Veeam status moves slowly
    setInterval(refreshCharts, 60000);
});


// ── View switching ──────────────────────────────────────────────────────────

function switchView(name) {
    currentView = name;
    document.querySelectorAll('.nav-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === name);
    });
    document.querySelectorAll('.view').forEach(v => {
        v.classList.toggle('active', v.id === 'view-' + name);
    });
    // Range buttons only make sense on the Dashboard view
    document.getElementById('rangeButtons').style.display = (name === 'dashboard') ? 'flex' : 'none';

    if (name === 'alerts') {
        fetchAlertHistory();
        if (!alertRefreshTimer) {
            alertRefreshTimer = setInterval(fetchAlertHistory, 15000);
        }
    } else if (alertRefreshTimer) {
        clearInterval(alertRefreshTimer);
        alertRefreshTimer = null;
    }

    if (name === 'security') {
        fetchSecurityStatus();
        fetchRdpLogins();
    }
    if (name === 'settings') {
        fetchSettings();
        fetchUpdateInfo();
        bindUpdateHandlers();
    }
}


// ── Settings ────────────────────────────────────────────────────────────────

let settingsBaseline = {};   // server-side effective values when the form was last loaded
let settingsDefaults = {};
let settingsBound = false;

function fetchSettings() {
    fetch('/api/settings')
        .then(r => r.json())
        .then(d => {
            settingsBaseline = Object.assign({}, d.effective);
            settingsDefaults = Object.assign({}, d.defaults);
            populateSettingsForm(d.effective);
            updateDerivedLabels();
            bindSettingsHandlers();
            updateSettingsDirty();
            setSettingsStatus('Loaded.', 'ok');
        })
        .catch(() => setSettingsStatus('Failed to load settings.', 'err'));
}

function populateSettingsForm(eff) {
    document.querySelectorAll('[data-setting]').forEach(input => {
        const key = input.dataset.setting;
        if (eff[key] != null) input.value = eff[key];
        input.classList.remove('changed', 'invalid');
    });
}

function updateDerivedLabels() {
    document.querySelectorAll('[data-show]').forEach(el => {
        const key = el.dataset.show;
        const input = document.querySelector(`[data-setting="${key}"]`);
        el.textContent = input ? input.value : '';
    });
    document.querySelectorAll('[data-show-mult]').forEach(el => {
        const key = el.dataset.showMult;
        const mult = parseInt(el.dataset.mult, 10) || 1;
        const input = document.querySelector(`[data-setting="${key}"]`);
        const v = input ? parseInt(input.value, 10) : NaN;
        el.textContent = isFinite(v) ? (v * mult) : '?';
    });
}

function bindSettingsHandlers() {
    if (settingsBound) return;
    settingsBound = true;

    document.querySelectorAll('[data-setting]').forEach(input => {
        input.addEventListener('input', () => {
            updateDerivedLabels();
            updateSettingsDirty();
        });
    });

    document.getElementById('settingsSave').addEventListener('click', saveSettings);
    document.getElementById('settingsReset').addEventListener('click', resetSettings);
}

function updateSettingsDirty() {
    let dirty = false;
    let invalid = false;
    document.querySelectorAll('[data-setting]').forEach(input => {
        const key = input.dataset.setting;
        const raw = input.value;
        const v = Number(raw);
        const min = Number(input.min);
        const max = Number(input.max);
        const isInvalid = raw === '' || !isFinite(v) || v < min || v > max;
        input.classList.toggle('invalid', isInvalid);
        if (isInvalid) invalid = true;
        const baseline = settingsBaseline[key];
        if (!isInvalid && baseline != null && Number(baseline) !== v) {
            input.classList.add('changed');
            dirty = true;
        } else {
            input.classList.remove('changed');
        }
    });

    // Cross-field: critical >= warning
    const pairs = ['host_cpu','host_mem','host_disk','vm_cpu','vm_mem'];
    let crossWarn = '';
    for (const p of pairs) {
        const w = Number(document.querySelector(`[data-setting="${p}_warning"]`)?.value);
        const c = Number(document.querySelector(`[data-setting="${p}_critical"]`)?.value);
        if (isFinite(w) && isFinite(c) && c < w) {
            crossWarn = `${p.replace('_',' ')}: critical (${c}) is below warning (${w})`;
            break;
        }
    }

    const btn = document.getElementById('settingsSave');
    btn.disabled = !dirty || invalid;

    if (invalid) {
        setSettingsStatus('Some values are out of range.', 'err');
    } else if (crossWarn) {
        setSettingsStatus('Heads up: ' + crossWarn, 'warn');
    } else if (dirty) {
        setSettingsStatus('Unsaved changes.', 'warn');
    } else {
        setSettingsStatus('', '');
    }
}

function collectSettings() {
    const out = {};
    document.querySelectorAll('[data-setting]').forEach(input => {
        const key = input.dataset.setting;
        out[key] = input.value;
    });
    return out;
}

function saveSettings() {
    const btn = document.getElementById('settingsSave');
    btn.disabled = true;
    setSettingsStatus('Saving…', 'warn');
    fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: collectSettings() }),
    })
    .then(r => r.json())
    .then(d => {
        if (!d.ok) {
            setSettingsStatus('Save failed.', 'err');
            return;
        }
        settingsBaseline = Object.assign({}, d.effective);
        populateSettingsForm(d.effective);
        updateDerivedLabels();
        updateSettingsDirty();
        if (d.errors && d.errors.length) {
            setSettingsStatus('Saved with warnings: ' + d.errors[0], 'warn');
        } else {
            setSettingsStatus('Saved. New thresholds active within 30 s.', 'ok');
        }
    })
    .catch(() => setSettingsStatus('Save failed.', 'err'));
}

function resetSettings() {
    if (!confirm('Reset all alert thresholds to their defaults?')) return;
    fetch('/api/settings/reset', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            if (!d.ok) {
                setSettingsStatus('Reset failed.', 'err');
                return;
            }
            settingsBaseline = Object.assign({}, d.effective);
            populateSettingsForm(d.effective);
            updateDerivedLabels();
            updateSettingsDirty();
            setSettingsStatus('Defaults restored.', 'ok');
        })
        .catch(() => setSettingsStatus('Reset failed.', 'err'));
}

function setSettingsStatus(msg, level) {
    const el = document.getElementById('settingsStatus');
    if (!el) return;
    el.textContent = msg;
    el.className = 'settings-status ' + (level || '');
}


// ── Updates ─────────────────────────────────────────────────────────────────

let updateBound = false;
let currentCommit = '';

function fetchUpdateInfo() {
    fetch('/api/update/info')
        .then(r => r.json())
        .then(d => {
            if (!d.ok) {
                document.getElementById('updateCurrent').textContent = 'Not a git checkout';
                setUpdateStatus('This install was not deployed via deploy.ps1, so in-app updates are disabled.', 'warn');
                document.getElementById('updateCheckBtn').disabled = true;
                return;
            }
            currentCommit = d.full;
            document.getElementById('updateCurrent').textContent = d.short;
            document.getElementById('updateSubject').textContent = d.subject || '';
            document.getElementById('updateDate').textContent =
                (d.relative ? d.relative : '') +
                (d.date ? ' · ' + new Date(d.date).toLocaleString() : '');
        })
        .catch(() => {});

    fetchAutoUpdateStatus();
}

function fetchAutoUpdateStatus() {
    fetch('/api/autoupdate/status')
        .then(r => r.json())
        .then(d => {
            const toggle = document.getElementById('autoUpdateToggle');
            if (!toggle) return;
            toggle.checked = !!d.enabled;
            setAutoUpdateStatus(d.enabled ? 'Auto-update enabled.' : 'Auto-update disabled.', d.enabled ? 'ok' : 'warn');
        })
        .catch(() => {});
}

function bindUpdateHandlers() {
    if (updateBound) return;
    updateBound = true;
    document.getElementById('updateCheckBtn').addEventListener('click', updateCheck);
    document.getElementById('updateApplyBtn').addEventListener('click', updateApply);
    const toggle = document.getElementById('autoUpdateToggle');
    if (toggle) toggle.addEventListener('change', onAutoUpdateToggle);
}

function onAutoUpdateToggle(e) {
    const toggle = e.target;
    const wantOn = toggle.checked;
    toggle.disabled = true;
    setAutoUpdateStatus(wantOn ? 'Enabling…' : 'Disabling…', 'warn');
    fetch(wantOn ? '/api/autoupdate/enable' : '/api/autoupdate/disable', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            toggle.disabled = false;
            if (!d.ok) {
                toggle.checked = !wantOn; // revert
                setAutoUpdateStatus('Failed: ' + (d.error || 'unknown'), 'err');
                return;
            }
            toggle.checked = !!d.enabled;
            setAutoUpdateStatus(d.enabled ? 'Auto-update enabled.' : 'Auto-update disabled.', d.enabled ? 'ok' : 'warn');
        })
        .catch(() => {
            toggle.disabled = false;
            toggle.checked = !wantOn;
            setAutoUpdateStatus('Failed to update setting.', 'err');
        });
}

function setAutoUpdateStatus(msg, level) {
    const el = document.getElementById('autoUpdateStatus');
    if (!el) return;
    el.textContent = msg;
    el.className = 'settings-status ' + (level || '');
}

function updateCheck() {
    const btn = document.getElementById('updateCheckBtn');
    btn.disabled = true;
    setUpdateStatus('Checking GitHub…', 'warn');
    fetch('/api/update/check', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            btn.disabled = false;
            if (!d.ok) {
                setUpdateStatus('Check failed: ' + (d.error || 'unknown error'), 'err');
                return;
            }
            const changesBox = document.getElementById('updateChanges');
            const applyBtn   = document.getElementById('updateApplyBtn');
            const list = document.getElementById('updateCommits');
            if (d.up_to_date) {
                setUpdateStatus('You are running the latest version.', 'ok');
                changesBox.style.display = 'none';
                applyBtn.style.display = 'none';
                return;
            }
            setUpdateStatus(
                d.count + (d.count === 1 ? ' new commit' : ' new commits') + ' available.',
                'warn'
            );
            list.innerHTML = d.commits.map(c => `
                <li>
                    <span class="c-hash">${escapeHtml(c.hash)}</span>
                    <span class="c-subject">${escapeHtml(c.subject)}</span>
                    <span class="c-when">${escapeHtml(c.relative)}</span>
                </li>
            `).join('');
            changesBox.style.display = 'block';
            applyBtn.style.display = 'inline-flex';
        })
        .catch(() => {
            btn.disabled = false;
            setUpdateStatus('Check failed.', 'err');
        });
}

function updateApply() {
    if (!confirm('Apply update now? The dashboard will stop for about 10–15 seconds and reload automatically.')) return;

    document.getElementById('updateCheckBtn').disabled = true;
    document.getElementById('updateApplyBtn').disabled = true;
    setUpdateStatus('', '');
    document.getElementById('updateProgress').style.display = 'block';
    document.getElementById('updateChanges').style.display = 'none';

    fetch('/api/update/apply', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            if (!d.ok) {
                document.getElementById('updateProgress').style.display = 'none';
                document.getElementById('updateCheckBtn').disabled = false;
                document.getElementById('updateApplyBtn').disabled = false;
                setUpdateStatus('Failed to start update: ' + (d.error || ''), 'err');
                return;
            }
            // Start polling for the server to come back on a different commit
            pollForRestart();
        })
        .catch(() => {
            // The server may have already started shutting down — fall through to poll
            pollForRestart();
        });
}

function pollForRestart() {
    const detail = document.getElementById('updateProgressDetail');
    const startedAt = Date.now();
    let serverDied = false;

    const tick = () => {
        const elapsed = (Date.now() - startedAt) / 1000;
        if (elapsed > 120) {
            detail.textContent = 'Update is taking longer than expected. Refresh the page manually.';
            return;
        }
        if (!serverDied) {
            detail.textContent = `Stopping the dashboard… (${Math.floor(elapsed)}s)`;
        } else {
            detail.textContent = `Waiting for the new version to come online… (${Math.floor(elapsed)}s)`;
        }

        fetch('/api/update/info', { cache: 'no-store' })
            .then(r => r.ok ? r.json() : null)
            .then(d => {
                if (!d || !d.ok) {
                    serverDied = true;
                    setTimeout(tick, 1500);
                    return;
                }
                // If we already saw the server die and the commit hash has changed, we're done
                if (serverDied && d.full && d.full !== currentCommit) {
                    detail.textContent = 'Updated. Reloading…';
                    setTimeout(() => location.reload(), 500);
                    return;
                }
                setTimeout(tick, 1500);
            })
            .catch(() => {
                serverDied = true;
                setTimeout(tick, 1500);
            });
    };

    tick();
}

function setUpdateStatus(msg, level) {
    const el = document.getElementById('updateStatus');
    if (!el) return;
    el.textContent = msg;
    el.className = 'update-status ' + (level || '');
}


// ── Alert History ───────────────────────────────────────────────────────────

// ── Pending Reboot banner ───────────────────────────────────────────────────

function fetchSecurityForReboot() {
    fetch('/api/security/status')
        .then(r => r.json())
        .then(d => {
            const banner = document.getElementById('rebootBanner');
            if (!banner) return;
            if (d && d.pending_reboot) {
                banner.style.display = 'flex';
                document.getElementById('rebootDetail').textContent =
                    (d.reboot_reasons || 'unknown') + ' — reboot the host to apply pending changes.';
            } else {
                banner.style.display = 'none';
            }
        })
        .catch(() => {});
}


// ── Veeam Backups ───────────────────────────────────────────────────────────

function fetchVeeam() {
    fetch('/api/veeam/backups')
        .then(r => r.json())
        .then(d => renderVeeam(d))
        .catch(() => {});
}

function renderVeeam(d) {
    const section = document.getElementById('veeamSection');
    if (!section) return;
    if (!d || !d.jobs || d.jobs.length === 0) {
        section.style.display = 'none';
        return;
    }
    section.style.display = 'block';

    document.getElementById('veeamCount').textContent = d.total;

    const c = d.counts || {};
    const pills = [];
    const order = ['success', 'warning', 'failed', 'running', 'never'];
    const labels = { success: 'Success', warning: 'Warning', failed: 'Failed', running: 'Running', never: 'Never Ran' };
    for (const k of order) {
        if ((c[k] || 0) > 0) {
            pills.push(`<span class="veeam-summary-pill ${k}"><span class="dot"></span>${c[k]} ${labels[k]}</span>`);
        }
    }
    document.getElementById('veeamSummary').innerHTML = pills.join('');

    const list = document.getElementById('veeamJobs');
    list.innerHTML = d.jobs.map(j => {
        const result = (j.last_result || 'never').toLowerCase();
        const cls = ['success', 'warning', 'failed', 'running'].includes(result) ? result : 'never';
        const label = (j.last_result && j.last_result !== 'None' && j.last_result !== 'NeverRan')
            ? j.last_result.toUpperCase()
            : 'NEVER RAN';
        const when = j.last_end_ts ? formatRelativeTime(j.last_end_ts) : '—';
        const dur  = j.duration_sec ? formatDuration(j.duration_sec) : '—';
        return `
            <div class="veeam-job">
                <span class="veeam-job-name">
                    ${escapeHtml(j.job_name)}
                    ${j.job_type ? `<span class="veeam-type">${escapeHtml(j.job_type)}</span>` : ''}
                </span>
                <span class="veeam-job-result ${cls}">${label}</span>
                <span class="veeam-job-when" title="${j.last_end_ts ? new Date(j.last_end_ts * 1000).toLocaleString() : ''}">${when}</span>
                <span class="veeam-job-dur">${dur}</span>
            </div>
        `;
    }).join('');
}


// ── Bandwidth (30-day VM traffic) ───────────────────────────────────────────

let bandwidthByVm = {};      // {vm_name: {sent_bytes, recv_bytes, total_bytes}}

function fetchBandwidth() {
    fetch('/api/vms/bandwidth?days=30')
        .then(r => r.json())
        .then(d => renderBandwidth(d))
        .catch(() => {});
}

function renderBandwidth(d) {
    if (!d || !d.vms) return;

    // Cache for VM card footers
    bandwidthByVm = {};
    d.vms.forEach(v => { bandwidthByVm[v.vm_name] = v; });

    // Update existing VM cards' footers
    document.querySelectorAll('.vm-card').forEach(card => {
        const id = card.id;
        if (!id || !id.startsWith('vm-')) return;
        applyVmFooter(card);
    });

    // Top-line summary on dashboard
    const lbl = document.getElementById('bwTotalLabel');
    if (lbl) lbl.textContent = formatGB(d.total_bytes);
    setText('bwTotal', formatGB(d.total_bytes));
    setText('bwSent',  formatGB(d.total_sent_bytes));
    setText('bwRecv',  formatGB(d.total_recv_bytes));
    setText('bwTopVm', d.vms.length ? `${d.vms[0].vm_name} (${formatGB(d.vms[0].total_bytes)})` : '—');

    const bars = document.getElementById('bandwidthBars');
    if (!bars) return;
    if (d.vms.length === 0) {
        bars.innerHTML = '<div class="no-events">No VM traffic recorded in the last 30 days.</div>';
        return;
    }
    const maxTotal = d.vms[0].total_bytes || 1;
    bars.innerHTML = d.vms.map(v => {
        const total = v.total_bytes || 0;
        const sentPct = (v.sent_bytes / maxTotal) * 100;
        const recvPct = (v.recv_bytes / maxTotal) * 100;
        return `
            <div class="bw-row">
                <span class="bw-row-name">${escapeHtml(v.vm_name)}</span>
                <div class="bw-row-bar-bg">
                    <div class="bw-row-bar-sent" style="width:${sentPct}%"
                         title="Sent: ${formatGB(v.sent_bytes)}"></div>
                    <div class="bw-row-bar-recv" style="width:${recvPct}%"
                         title="Received: ${formatGB(v.recv_bytes)}"></div>
                </div>
                <span class="bw-row-total">
                    ${formatGB(total)}
                    <span class="bw-up" title="sent">↑${formatGBShort(v.sent_bytes)}</span>
                    <span class="bw-down" title="received">↓${formatGBShort(v.recv_bytes)}</span>
                </span>
            </div>
        `;
    }).join('');
}

function applyVmFooter(card) {
    // VM name is stored as the card id suffix; but cssId strips chars. Use the displayed name instead.
    const nameEl = card.querySelector('.vm-name');
    const vmName = nameEl ? nameEl.textContent : '';
    const d = bandwidthByVm[vmName];
    let footer = card.querySelector('.vm-footer');
    if (!d) {
        if (footer) footer.remove();
        return;
    }
    if (!footer) {
        footer = document.createElement('div');
        footer.className = 'vm-footer';
        card.appendChild(footer);
    }
    footer.innerHTML = `
        <span class="vm-footer-label">30d Network</span>
        <span class="vm-footer-value">
            ${formatGB(d.total_bytes)}
            <span class="bw-up">↑${formatGBShort(d.sent_bytes)}</span>
            <span class="bw-down">↓${formatGBShort(d.recv_bytes)}</span>
        </span>
    `;
}

function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

function formatGB(bytes) {
    if (bytes == null || !isFinite(bytes)) return '—';
    const gb = bytes / (1024 ** 3);
    if (gb >= 1000) return (gb / 1000).toFixed(2) + ' TB';
    if (gb >= 10)   return gb.toFixed(1) + ' GB';
    if (gb >= 1)    return gb.toFixed(2) + ' GB';
    const mb = bytes / (1024 ** 2);
    if (mb >= 1)    return mb.toFixed(0) + ' MB';
    const kb = bytes / 1024;
    if (kb >= 1)    return kb.toFixed(0) + ' KB';
    return Math.round(bytes) + ' B';
}

function formatGBShort(bytes) {
    if (bytes == null || !isFinite(bytes)) return '—';
    const gb = bytes / (1024 ** 3);
    if (gb >= 1000) return (gb / 1000).toFixed(1) + 'T';
    if (gb >= 10)   return gb.toFixed(0) + 'G';
    if (gb >= 1)    return gb.toFixed(1) + 'G';
    const mb = bytes / (1024 ** 2);
    if (mb >= 1)    return mb.toFixed(0) + 'M';
    const kb = bytes / 1024;
    if (kb >= 1)    return kb.toFixed(0) + 'K';
    return Math.round(bytes) + 'B';
}


// ── Alert History ───────────────────────────────────────────────────────────

function fetchAlertHistory() {
    const p = new URLSearchParams({
        status:   alertFilter.status,
        severity: alertFilter.severity,
        limit:    alertPage.limit,
        offset:   alertPage.offset,
    });
    fetch('/api/alerts/history?' + p)
        .then(r => r.json())
        .then(data => renderAlertHistory(data))
        .catch(() => {});
}

function renderAlertHistory(data) {
    const tbody = document.getElementById('alertHistoryBody');
    const total = data.total || 0;
    const rows  = data.rows || [];

    alertPage.total = total;
    document.getElementById('alertHistoryTotal').textContent = total;

    if (rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="no-events">No alerts match the current filters</td></tr>';
        document.getElementById('alertSummary').textContent = '0 of 0';
        document.getElementById('alertPagination').style.display = 'none';
        return;
    }

    tbody.innerHTML = rows.map(a => {
        const isActive = a.ts_cleared == null;
        const statusCls = isActive ? 'active' : 'cleared';
        const sevCls    = (a.severity || '').toLowerCase();
        const raised    = formatDateTimeFull(a.ts_raised);
        const cleared   = isActive ? '—' : formatDateTimeFull(a.ts_cleared);
        const duration  = isActive
            ? formatDuration((Date.now() / 1000) - a.ts_raised) + ' ongoing'
            : formatDuration(a.ts_cleared - a.ts_raised);

        return `
            <tr>
                <td class="col-status"><span class="status-pill ${statusCls}">${isActive ? 'Active' : 'Cleared'}</span></td>
                <td class="col-sev"><span class="sev-pill ${sevCls}">${escapeHtml(a.severity || '')}</span></td>
                <td class="col-target">${escapeHtml(a.target || '')}</td>
                <td class="col-msg">${escapeHtml(a.message || '')}</td>
                <td class="col-raised">${raised}</td>
                <td class="col-cleared">${cleared}</td>
                <td class="col-dur">${duration}</td>
            </tr>
        `;
    }).join('');

    const start = alertPage.offset + 1;
    const end   = Math.min(alertPage.offset + rows.length, total);
    document.getElementById('alertSummary').textContent = `Showing ${start}–${end} of ${total}`;

    const pag = document.getElementById('alertPagination');
    if (total > alertPage.limit) {
        pag.style.display = 'flex';
        document.getElementById('alertPrev').disabled = alertPage.offset <= 0;
        document.getElementById('alertNext').disabled = alertPage.offset + alertPage.limit >= total;
        const page = Math.floor(alertPage.offset / alertPage.limit) + 1;
        const last = Math.ceil(total / alertPage.limit);
        document.getElementById('alertPageInfo').textContent = `Page ${page} of ${last}`;
    } else {
        pag.style.display = 'none';
    }
}

// ── Security view ───────────────────────────────────────────────────────────

let rdpFilter = 'all';

function setSecValue(id, label, level, subText) {
    const el = document.getElementById(id);
    const sub = document.getElementById(id + 'Sub');
    if (el) {
        el.textContent = label;
        el.className = 'sec-value ' + (level ? 'is-' + level : 'is-unknown');
    }
    if (sub && subText !== undefined) sub.textContent = subText;
}

function fetchSecurityStatus() {
    fetch('/api/security/status')
        .then(r => r.json())
        .then(d => renderSecurityStatus(d))
        .catch(() => {});
}

function renderSecurityStatus(d) {
    if (!d || !d.ts) {
        ['secFirewall','secDefender','secBitlocker','secUac','secFailed','secUpdates']
            .forEach(id => setSecValue(id, 'Scanning…', null, ''));
        return;
    }

    // Firewall
    const fw = [
        ['Domain',  d.firewall_domain],
        ['Private', d.firewall_private],
        ['Public',  d.firewall_public],
    ];
    const onCount = fw.filter(([, v]) => v === 1).length;
    const offProfs = fw.filter(([, v]) => v === 0).map(([n]) => n);
    let fwLabel, fwLevel, fwSub;
    if (onCount === 3)      { fwLabel = 'All Enabled';        fwLevel = 'ok';   fwSub = 'Domain / Private / Public'; }
    else if (onCount === 0) { fwLabel = 'All Disabled';       fwLevel = 'crit'; fwSub = 'All three profiles are off'; }
    else                    { fwLabel = `${onCount} of 3 On`; fwLevel = 'warn'; fwSub = 'Off: ' + offProfs.join(', '); }
    setSecValue('secFirewall', fwLabel, fwLevel, fwSub);

    // Defender
    if (d.defender_realtime === 1) {
        const age = d.defender_signature_age_days;
        let level = 'ok', sub = '';
        if (age == null) sub = 'engine ' + (d.defender_engine_version || '');
        else if (age > 7) { level = 'warn'; sub = `signatures ${age.toFixed(1)}d old`; }
        else              { sub = `signatures ${age.toFixed(1)}d old`; }
        setSecValue('secDefender', 'Real-time On', level, sub);
    } else if (d.defender_realtime === 0) {
        setSecValue('secDefender', 'Off',  'crit', 'Real-time protection disabled');
    } else {
        setSecValue('secDefender', 'Unknown', 'unknown', '');
    }

    // BitLocker
    const bl = d.bitlocker_status || '';
    let blLevel = 'unknown';
    if (bl.startsWith('On'))           blLevel = 'ok';
    else if (bl.startsWith('Off'))     blLevel = 'warn';
    else if (bl.startsWith('Mixed'))   blLevel = 'warn';
    else if (bl === 'None')            blLevel = 'info';
    setSecValue('secBitlocker', bl || '--', blLevel, '');

    // UAC
    if (d.uac_enabled === 1)      setSecValue('secUac', 'Enabled',  'ok',   'EnableLUA = 1');
    else if (d.uac_enabled === 0) setSecValue('secUac', 'Disabled', 'crit', 'EnableLUA = 0');
    else                           setSecValue('secUac', 'Unknown',  'unknown', '');

    // Failed logins
    const fl = d.failed_logins_24h;
    if (fl == null) {
        setSecValue('secFailed', '—', 'unknown', '');
    } else if (fl >= 50) {
        setSecValue('secFailed', String(fl), 'crit', 'Possible brute-force');
    } else if (fl >= 10) {
        setSecValue('secFailed', String(fl), 'warn', 'Elevated activity');
    } else {
        setSecValue('secFailed', String(fl), 'ok',   'Normal');
    }

    // Pending updates
    const up = d.updates_pending;
    if (up == null)        setSecValue('secUpdates', '—',         'unknown', '');
    else if (up === 0)     setSecValue('secUpdates', 'None',      'ok',      'System is up to date');
    else                    setSecValue('secUpdates', String(up),  'warn',    'Run Windows Update');

    renderFindings(d.findings || []);
}

function renderFindings(findings) {
    const list = document.getElementById('findingsList');
    document.getElementById('findingsCount').textContent = findings.length;
    if (!findings.length) {
        list.innerHTML = '<div class="no-events">No findings</div>';
        return;
    }
    const order = { high: 0, medium: 1, info: 2, ok: 3 };
    findings.sort((a, b) => (order[a.severity] ?? 9) - (order[b.severity] ?? 9));
    list.innerHTML = findings.map(f => `
        <div class="finding">
            <span class="finding-sev ${escapeHtml(f.severity || 'info')}">${escapeHtml((f.severity || 'info').toUpperCase())}</span>
            <div class="finding-body">
                <div class="finding-title">${escapeHtml(f.title || '')}</div>
                <div class="finding-detail">${escapeHtml(f.detail || '')}</div>
            </div>
        </div>
    `).join('');
}

function fetchRdpLogins() {
    const p = new URLSearchParams({ status: rdpFilter, limit: 200 });
    fetch('/api/security/rdp?' + p)
        .then(r => r.json())
        .then(d => renderRdpLogins(d))
        .catch(() => {});
}

function renderRdpLogins(data) {
    const body = document.getElementById('rdpLoginBody');
    const total = data.total || 0;
    const rows = data.rows || [];
    document.getElementById('rdpCount').textContent = total;

    if (rows.length === 0) {
        body.innerHTML = '<tr><td colspan="5" class="no-events">No RDP login activity in the lookback window</td></tr>';
        document.getElementById('rdpSummary').textContent = '0 of 0';
        return;
    }

    body.innerHTML = rows.map(r => {
        const success = r.success === 1;
        const cls = success ? 'cleared' : 'active';   // reuse pill colors
        const label = success ? 'SUCCESS' : 'FAILED';
        const sevCls = success ? 'ok' : 'high';
        return `
            <tr>
                <td><span class="finding-sev ${sevCls}">${label}</span></td>
                <td class="col-raised">${formatDateTimeFull(r.ts_event)}</td>
                <td class="col-target">${escapeHtml((r.domain ? r.domain + '\\\\' : '') + (r.username || ''))}</td>
                <td>${escapeHtml(r.source_ip || '—')}</td>
                <td>${escapeHtml(r.workstation || '—')}</td>
            </tr>
        `;
    }).join('');
    document.getElementById('rdpSummary').textContent = `Showing 1–${rows.length} of ${total}`;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function formatDateTimeFull(epoch) {
    const d = new Date(epoch * 1000);
    return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: '2-digit' }) +
           ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatDuration(seconds) {
    if (seconds == null || seconds < 0) return '—';
    const s = Math.floor(seconds);
    if (s < 60)    return s + 's';
    if (s < 3600)  return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
    return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
}


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

    // Memory: also show "X.X GB free / Y GB total"
    const memSub = document.getElementById('hostMemSub');
    if (memSub) {
        if (m.mem_total && m.mem_avail != null) {
            const freeGB  = (m.mem_avail / (1024 ** 3)).toFixed(1);
            const totalGB = (m.mem_total / (1024 ** 3)).toFixed(0);
            memSub.textContent = `${freeGB} GB free of ${totalGB} GB`;
        } else if (m.mem_avail != null) {
            const freeGB = (m.mem_avail / (1024 ** 3)).toFixed(1);
            memSub.textContent = `${freeGB} GB free`;
        } else {
            memSub.textContent = '';
        }
    }

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
        const labelText = v.label && v.label.trim()
            ? `${v.drive}: <span class="vol-name">${escapeHtml(v.label)}</span>`
            : `${v.drive}:`;
        return `
            <div class="volume-row">
                <span class="volume-label">${labelText}</span>
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
    const navBadge = document.getElementById('navVmCount');
    const count = vms ? vms.length : 0;
    if (countEl) countEl.textContent = count;
    if (navBadge) {
        if (count > 0) {
            navBadge.textContent = count;
            navBadge.style.display = 'inline-flex';
        } else {
            navBadge.style.display = 'none';
        }
    }

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
        applyVmFooter(card);
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
            <div class="vm-header-left">
                <span class="vm-name">${escapeHtml(vm.vm_name)}</span>
                <span class="vm-ip-wrap" data-field="ips"></span>
            </div>
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
    // Heartbeat states explained:
    //   OkApplicationsHealthy/Unknown -> green   (integration services healthy)
    //   N/A                            -> dim    (VM not running)
    //   NoContact/Disabled/LostComm    -> amber  (ISs not running in guest)
    //   anything else                  -> red    (real problem)
    if (hb === 'N/A')                                    hbEl.classList.add('text-dim');
    else if (hb.includes('Ok'))                          hbEl.classList.add('text-green');
    else if (/^(NoContact|Disabled|LostComm|NoIntegration)/i.test(hb)) hbEl.classList.add('text-orange');
    else                                                  hbEl.classList.add('text-red');

    // IPs (internal addresses from Hyper-V integration services)
    const ipWrap = card.querySelector('[data-field="ips"]');
    if (ipWrap) {
        ipWrap.innerHTML = '';
        const ips = (vm.ip_addresses || '').split(',').map(s => s.trim()).filter(Boolean);
        const shown = ips.slice(0, 2);
        for (const ip of shown) {
            const chip = document.createElement('span');
            chip.className = 'vm-ip';
            chip.textContent = ip;
            ipWrap.appendChild(chip);
        }
        if (ips.length > shown.length) {
            const more = document.createElement('span');
            more.className = 'vm-ip-more';
            more.textContent = `+${ips.length - shown.length}`;
            more.title = ips.slice(shown.length).join(', ');
            ipWrap.appendChild(more);
        }
    }

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
