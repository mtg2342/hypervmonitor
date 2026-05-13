function renderAlerts(alerts) {
    const banner = document.getElementById('alertsBanner');
    const list = document.getElementById('alertsList');
    if (!alerts || alerts.length === 0) {
        banner.style.display = 'none';
        return;
    }
    banner.style.display = 'block';
    list.innerHTML = alerts.map(a => {
        const time = new Date(a.ts_raised * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const iconClass = a.severity === 'critical' ? 'critical' : 'warning';
        const icon = a.severity === 'critical' ? '!!' : '!';
        return `
            <div class="alert-item">
                <span class="alert-icon ${iconClass}">[${icon}]</span>
                <span class="alert-msg">${escapeHtml(a.message)}</span>
                <span class="alert-time">${time}</span>
                <button class="alert-dismiss" onclick="dismissAlert(${a.id})">dismiss</button>
            </div>
        `;
    }).join('');
}

function dismissAlert(id) {
    fetch('/api/alerts/' + id + '/dismiss', { method: 'POST' })
        .then(() => fetchAlerts());
}

function fetchAlerts() {
    fetch('/api/alerts/active')
        .then(r => r.json())
        .then(data => renderAlerts(data))
        .catch(() => {});
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
