// ============================================================
// HISTORY
// ============================================================
async function loadHistory(netId) {
  // Populate the net selector with all known nets
  const sel = document.getElementById('history-net-select');
  sel.innerHTML = nets.map(n =>
    `<option value="${n.id}" ${n.id === netId ? 'selected' : ''}>${esc(n.name)}</option>`
  ).join('');

  currentNetId = netId;
  document.getElementById('history-search').value = '';
  document.getElementById('history-filter').value = 'all';
  document.getElementById('history-min-checkins').value = '';
  document.getElementById('history-date-from').value = '';
  document.getElementById('history-date-to').value = '';
  document.getElementById('history-csv-preset').value = 'full';
  switchHistoryTemplate('roster', /*skipRender=*/true);

  const net = nets.find(n => n.id === netId);
  historyNetIsAres = net ? !!net.is_ares : false;
  document.getElementById('history-th-zone').style.display = historyNetIsAres ? '' : 'none';

  const zoneFilterSel = document.getElementById('history-zone-filter');
  zoneFilterSel.innerHTML = `<option value="">${t('All Zones')}</option>`;
  zoneFilterSel.value = '';
  zoneFilterSel.style.display = historyNetIsAres ? '' : 'none';
  historyZones = {};
  if (historyNetIsAres) {
    try {
      const zones = await apiFetch(`/nets/${netId}/evac-zones`);
      zones.forEach(z => { historyZones[z.callsign] = z.zone; });
      const distinctZones = [...new Set(Object.values(historyZones))].sort();
      zoneFilterSel.innerHTML += distinctZones.map(z => `<option value="${esc(z)}">${esc(z)}</option>`).join('');
    } catch {}
  }

  try {
    const sessions = await apiFetch(`/nets/${netId}/sessions`);
    historySessionCount = sessions.filter(s => s.ended_at).length;
  } catch { historySessionCount = 0; }

  try {
    historyData = await apiFetch(`/nets/${netId}/history?limit=1000`);
  } catch { historyData = []; }
  filterHistory();
}

function onHistoryNetChange() {
  const netId = parseInt(document.getElementById('history-net-select').value);
  if (netId) loadHistory(netId);
}

const historyLookupCache = {};  // separate cache for history view

// ------------------------------------------------------------
// Template picker -- switches which of the three views below is shown
// ------------------------------------------------------------
function switchHistoryTemplate(tpl, skipRender) {
  currentHistoryTemplate = tpl;
  ['roster', 'summary', 'printable'].forEach(t => {
    const btn = document.getElementById('history-tpl-' + t);
    if (btn) btn.classList.toggle('active', t === tpl);
  });
  if (!skipRender) filterHistory();
}

function renderHistory(rows) {
  historyFilteredRows = rows;
  const empty = document.getElementById('history-empty');
  ['roster', 'summary', 'printable'].forEach(t => {
    const el = document.getElementById('history-view-' + t);
    if (el) el.style.display = (t === currentHistoryTemplate && rows.length) ? '' : 'none';
  });
  empty.style.display = rows.length ? 'none' : '';
  if (!rows.length) return;

  if (currentHistoryTemplate === 'summary') renderHistorySummary(rows);
  else if (currentHistoryTemplate === 'printable') renderHistoryPrintable(rows);
  else renderHistoryRoster(rows);
}

// ------------------------------------------------------------
// ROSTER template -- the original per-callsign table
// ------------------------------------------------------------
function renderHistoryRoster(rows) {
  const tbody = document.getElementById('history-tbody');
  tbody.innerHTML = rows.map(r => {
    const cached = historyLookupCache[r.callsign];
    const licenseCell = cached
      ? buildLicensePills(cached)
      : `<button class="btn btn-ghost btn-sm" onclick="lookupHistoryRow('${esc(r.callsign)}')" title="${t('Look up FCC license data')}">🔍</button>`;
    const lastNetBadge = r.checked_in_last_session
      ? `<span class="badge badge-green">✓</span>`
      : `<span class="badge badge-gray">—</span>`;
    const recent2wBadge = r.recent_checkins > 0
      ? `<span class="badge badge-green">${r.recent_checkins}</span>`
      : `<span class="text-muted" style="font-size:11px">—</span>`;
    const recent4wBadge = r.recent_4w_checkins > 0
      ? `<span class="badge badge-blue">${r.recent_4w_checkins}</span>`
      : `<span class="text-muted" style="font-size:11px">—</span>`;
    const zoneCell = historyNetIsAres ? `<td>${esc(historyZones[r.callsign] || '—')}</td>` : '';
    return `<tr id="hist-row-${esc(r.callsign)}">
      <td><span class="callsign">${esc(r.callsign)}</span></td>
      <td id="hist-name-${esc(r.callsign)}">
        <span class="hist-name-display">${esc(r.name || '—')}</span>
        <button type="button" title="${t('Set preferred name / remark for this station')}"
          onclick="toggleHistoryRemarkEditor(this, '${esc(r.callsign)}')"
          style="background:none;border:none;color:var(--lc-orange);cursor:pointer;font-size:11px;padding:0 2px;opacity:0.7">✏️</button>
      </td>
      <td id="hist-lic-${esc(r.callsign)}">${licenseCell}</td>
      ${zoneCell}
      <td style="text-align:center">${lastNetBadge}</td>
      <td style="text-align:center">${recent2wBadge}</td>
      <td style="text-align:center">${recent4wBadge}</td>
      <td style="text-align:center"><span class="badge badge-orange">${r.total_checkins}</span></td>
      <td class="text-muted" style="font-size:11px">${fmt(r.last_checkin)}</td>
    </tr>`;
  }).join('');
}

function buildLicensePills(result) {
  if (result.status !== 'found') return `<span class="lookup-notfound" style="font-size:11px">${t('Not found')}</span>`;
  const parts = [];
  if (result.license_class) parts.push(`<span class="lookup-pill lookup-pill-class">${esc(result.license_class)}</span>`);
  if (result.state)         parts.push(`<span class="lookup-pill lookup-pill-state">${esc(result.state)}</span>`);
  if (result.grid)          parts.push(`<span class="lookup-pill lookup-pill-grid">${esc(result.grid)}</span>`);
  return parts.join(' ') || '<span class="text-muted" style="font-size:11px">—</span>';
}

async function lookupHistoryRow(callsign) {
  const licEl = document.getElementById(`hist-lic-${callsign}`);
  const nameEl = document.getElementById(`hist-name-${callsign}`)?.querySelector('.hist-name-display');
  if (!licEl) return;
  licEl.innerHTML = '<span class="lookup-spinner"></span>';
  try {
    const result = await apiFetch(`/callsign/${encodeURIComponent(callsign)}/lookup`);
    historyLookupCache[callsign] = result;
    licEl.innerHTML = buildLicensePills(result);
    if (result.status === 'found' && result.name && nameEl && nameEl.textContent === '—') {
      nameEl.textContent = result.name;
    }
  } catch {
    licEl.innerHTML = `<span class="lookup-notfound" style="font-size:11px">${t('Error')}</span>`;
  }
}

// Inline preferred name / remark editor for a History row — lets an operator
// set a preferred name after the net has closed, not just live during check-in.
async function toggleHistoryRemarkEditor(btn, callsign) {
  const cell = document.getElementById(`hist-name-${callsign}`);
  const existing = cell.querySelector('.hist-remark-editor');
  if (existing) { existing.remove(); return; }

  const current = await loadStationRemarks(callsign);
  const editor = document.createElement('div');
  editor.className = 'hist-remark-editor';
  editor.style.cssText = 'display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap';
  editor.innerHTML = `
    <input class="form-control hist-pref-input" style="width:120px;font-size:12px"
      placeholder="${t('Preferred name')}" value="${esc((current && current.preferred_name) || '')}" />
    <input class="form-control hist-remark-input" style="width:140px;font-size:12px"
      placeholder="${t('Notes')}" value="${esc((current && current.remark) || '')}" />
    <button class="btn btn-primary btn-sm" type="button">${t('Save')}</button>
    <button class="btn btn-ghost btn-sm" type="button">✕</button>`;
  const prefInput = editor.querySelector('.hist-pref-input');
  const remarkInput = editor.querySelector('.hist-remark-input');
  const [saveBtn, cancelBtn] = editor.querySelectorAll('button');
  const doSave = async () => {
    try {
      await saveStationRemark(callsign, remarkInput.value, prefInput.value);
      toast(t('Saved'), 'success');
      // Refresh the underlying data without resetting the active search/filter.
      historyData = await apiFetch(`/nets/${currentNetId}/history?limit=1000`).catch(() => historyData);
      filterHistory();
    } catch (e) { toast(e.message, 'error'); }
  };
  saveBtn.onclick = doSave;
  cancelBtn.onclick = () => editor.remove();
  [prefInput, remarkInput].forEach(el => el.addEventListener('keydown', e => { if (e.key === 'Enter') doSave(); }));
  cell.appendChild(editor);
  prefInput.focus();
}

async function lookupAllHistory() {
  for (const r of historyData) {
    if (!historyLookupCache[r.callsign]) {
      await lookupHistoryRow(r.callsign);
      await new Promise(res => setTimeout(res, 150)); // small delay between requests
    }
  }
}

// ------------------------------------------------------------
// SUMMARY STATS template -- aggregate cards + a compact attendance table
// ------------------------------------------------------------
function renderHistorySummary(rows) {
  const uniqueOperators = rows.length;
  const totalCheckins = rows.reduce((sum, r) => sum + r.total_checkins, 0);
  const avgPerOperator = uniqueOperators ? (totalCheckins / uniqueOperators).toFixed(1) : '0';

  const card = (label, value) => `
    <div class="card" style="padding:12px;text-align:center">
      <div style="font-size:22px;font-weight:700;color:var(--lc-orange)">${value}</div>
      <div style="font-size:11px;color:var(--text-muted);letter-spacing:.04em;margin-top:2px">${label}</div>
    </div>`;
  document.getElementById('history-summary-cards').innerHTML =
    card(t('OPERATORS SHOWN'), uniqueOperators) +
    card(t('COMBINED CHECK-INS'), totalCheckins) +
    card(t('AVG. PER OPERATOR'), avgPerOperator) +
    card(t('SESSIONS LOGGED'), historySessionCount);

  const tbody = document.getElementById('history-summary-tbody');
  tbody.innerHTML = rows.map(r => {
    const attendance = historySessionCount > 0
      ? `${Math.min(100, Math.round((r.total_checkins / historySessionCount) * 100))}%`
      : '—';
    return `<tr>
      <td><span class="callsign">${esc(r.callsign)}</span></td>
      <td>${esc(r.name || '—')}</td>
      <td style="text-align:center"><span class="badge badge-orange">${r.total_checkins}</span></td>
      <td style="text-align:center">${attendance}</td>
    </tr>`;
  }).join('');
}

// ------------------------------------------------------------
// PRINTABLE REPORT template -- clean layout for window.print()
// ------------------------------------------------------------
function renderHistoryPrintable(rows) {
  const net = nets.find(n => n.id === currentNetId);
  const netName = net ? net.name : t('Net');
  const generated = new Date().toLocaleString();

  const td = 'border:1px solid #ccc;padding:3px 6px';
  const zoneTh = historyNetIsAres ? `<th style="border:1px solid #999;padding:4px 6px;text-align:left">${t('Zone')}</th>` : '';
  const rowsHtml = rows.map(r => {
    const cached = historyLookupCache[r.callsign];
    const license = cached && cached.status === 'found'
      ? [cached.license_class, cached.state].filter(Boolean).join(' / ')
      : '';
    const zoneTd = historyNetIsAres ? `<td style="${td}">${esc(historyZones[r.callsign] || '')}</td>` : '';
    return `<tr>
      <td style="${td}"><strong>${esc(r.callsign)}</strong></td>
      <td style="${td}">${esc(r.name || '')}</td>
      <td style="${td}">${esc(license)}</td>
      ${zoneTd}
      <td style="${td};text-align:center">${r.total_checkins}</td>
      <td style="${td}">${r.last_checkin ? fmt(r.last_checkin) : ''}</td>
    </tr>`;
  }).join('');

  document.getElementById('history-print-area').innerHTML = `
    <div style="font-family:Arial,sans-serif;color:#000;padding:10px">
      <h1 style="font-size:18px;margin:0 0 4px">${esc(netName)} — ${t('Checkin History Report')}</h1>
      <p style="font-size:11px;color:#555;margin:0 0 16px">${t('Generated')} ${esc(generated)} &middot; ${tn(rows.length, '{n} operator', '{n} operators')}</p>
      <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead>
          <tr style="background:#eee">
            <th style="border:1px solid #999;padding:4px 6px;text-align:left">${t('Callsign')}</th>
            <th style="border:1px solid #999;padding:4px 6px;text-align:left">${t('Name')}</th>
            <th style="border:1px solid #999;padding:4px 6px;text-align:left">${t('License')}</th>
            ${zoneTh}
            <th style="border:1px solid #999;padding:4px 6px;text-align:left">${t('Total Check-ins')}</th>
            <th style="border:1px solid #999;padding:4px 6px;text-align:left">${t('Last Check-in')}</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>`;
}

// ------------------------------------------------------------
// Filtering -- combines the text search, preset dropdown, and the newer
// min-checkins / date-range / zone filters, then hands off to whichever
// template is currently active.
// ------------------------------------------------------------
function filterHistory() {
  const q      = document.getElementById('history-search').value.toLowerCase();
  const filter = document.getElementById('history-filter').value;
  const minCheckins = parseInt(document.getElementById('history-min-checkins').value) || 0;
  const dateFrom = document.getElementById('history-date-from').value;
  const dateTo = document.getElementById('history-date-to').value;
  const zoneFilter = document.getElementById('history-zone-filter').value;

  let rows = historyData;

  // Apply dropdown filter
  switch (filter) {
    case 'last_net':    rows = rows.filter(r => r.checked_in_last_session); break;
    case 'missed_last': rows = rows.filter(r => !r.checked_in_last_session); break;
    case 'active_2w':   rows = rows.filter(r => r.recent_checkins > 0); break;
    case 'regular_4w':  rows = rows.filter(r => r.recent_4w_checkins >= 2); break;
    case 'frequent_4w': rows = rows.filter(r => r.recent_4w_checkins >= 3); break;
    case 'inactive_4w': rows = rows.filter(r => r.recent_4w_checkins === 0); break;
  }

  // Apply text search on top of dropdown filter
  if (q) {
    rows = rows.filter(r =>
      r.callsign.toLowerCase().includes(q) || (r.name || '').toLowerCase().includes(q)
    );
  }

  if (minCheckins > 0) {
    rows = rows.filter(r => r.total_checkins >= minCheckins);
  }

  if (dateFrom) {
    const from = new Date(dateFrom);
    rows = rows.filter(r => r.last_checkin && new Date(r.last_checkin) >= from);
  }
  if (dateTo) {
    const to = new Date(dateTo);
    to.setHours(23, 59, 59, 999);
    rows = rows.filter(r => r.last_checkin && new Date(r.last_checkin) <= to);
  }

  if (zoneFilter) {
    rows = rows.filter(r => (historyZones[r.callsign] || '') === zoneFilter);
  }

  renderHistory(rows);
}

function downloadHistoryCSV() {
  const rows = historyFilteredRows.length ? historyFilteredRows : historyData;
  if (!rows.length) return toast(t('No history to download'), 'error');
  const net = nets.find(n => n.id === currentNetId);
  const netName = net ? net.name : t('net');
  const preset = document.getElementById('history-csv-preset').value;

  let header, csvRows;
  if (preset === 'minimal') {
    header = [t('Callsign'), t('Name'), t('Total Check-ins')];
    csvRows = rows.map(r => [r.callsign, r.name || '', r.total_checkins]);
  } else if (preset === 'license') {
    header = [t('Callsign'), t('Name'), t('License Class'), t('State'), t('Grid')];
    csvRows = rows.map(r => {
      const cached = historyLookupCache[r.callsign];
      return [r.callsign, r.name || '', cached?.license_class || '', cached?.state || '', cached?.grid || ''];
    });
  } else {
    header = [t('Callsign'), t('Name'), t('License Class'), t('State'), t('Grid'), t('Past 14 Days'), t('Total Check-ins'), t('Last Check-in')];
    if (historyNetIsAres) header.push(t('Zone'));
    csvRows = rows.map(r => {
      const cached = historyLookupCache[r.callsign];
      const row = [
        r.callsign,
        r.name || '',
        cached?.license_class || '',
        cached?.state || '',
        cached?.grid || '',
        r.recent_checkins,
        r.total_checkins,
        r.last_checkin ? new Date(r.last_checkin).toISOString() : '',
      ];
      if (historyNetIsAres) row.push(historyZones[r.callsign] || '');
      return row;
    });
  }

  const csv = [header, ...csvRows].map(row =>
    row.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')
  ).join('\r\n');

  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `history_${netName.replace(/\s+/g, '_')}_${preset}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function historyBack() {
  if (currentNetId) {
    openNet(currentNetId);
  } else {
    showView('nets');
  }
}

// ============================================================
// CSV EXPORT
// ============================================================
function exportSession() {
  if (!currentSessionId) return;
  window.open(`${API}/sessions/${currentSessionId}/export?token=${token}`, '_blank');
  // Workaround: fetch with auth header and trigger download
  apiFetch(`/sessions/${currentSessionId}/export`).catch(() => {});
  triggerDownload(`${API}/sessions/${currentSessionId}/export`);
}

function exportNet() {
  if (!currentNetId) return;
  triggerDownload(`${API}/nets/${currentNetId}/export`);
}
