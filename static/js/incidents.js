// ============================================================
// INCIDENTS (issue #28)
// ============================================================
let incidentNets = [];
let currentIncidentNetId = null;
let currentIncidentZoneBoundaries = [];   // this net's synced EvacZoneBoundary rows
let currentIncidents = [];
let editingIncidentId = null;   // set while the form is editing an existing incident, else creating
let openIncidentId = null;      // the incident currently shown in the detail card
let openIncidentStations = [];

const INCIDENT_STATION_STATUSES = ['not_contacted', 'attempted', 'contacted', 'confirmed_safe', 'needs_assistance'];
const INCIDENT_STATION_STATUS_LABELS = {
  not_contacted: 'Not Contacted',
  attempted: 'Attempted',
  contacted: 'Contacted',
  confirmed_safe: 'Confirmed Safe',
  needs_assistance: 'Needs Assistance',
};
const INCIDENT_MATCH_REASON_LABELS = {
  zone_report: 'Zone report',
  position: 'Position',
  manual: 'Manual',
};

async function loadIncidentNets() {
  try {
    incidentNets = await apiFetch('/nets');
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  const sel = document.getElementById('incident-net-select');
  sel.innerHTML = incidentNets.map(n => `<option value="${n.id}">${esc(n.name)}</option>`).join('');
  if (incidentNets.length) {
    currentIncidentNetId = incidentNets[0].id;
    sel.value = currentIncidentNetId;
    await onIncidentNetChange();
  } else {
    document.getElementById('incident-list-section').style.display = 'none';
  }
}

async function onIncidentNetChange() {
  currentIncidentNetId = parseInt(document.getElementById('incident-net-select').value, 10);
  hideIncidentDetail();
  hideIncidentForm();
  document.getElementById('incident-list-section').style.display = '';
  await Promise.all([loadIncidentZoneBoundaries(), loadIncidents()]);
}

async function loadIncidentZoneBoundaries() {
  try {
    currentIncidentZoneBoundaries = await apiFetch(`/nets/${currentIncidentNetId}/evac-zone-boundaries`);
  } catch {
    currentIncidentZoneBoundaries = [];
  }
}

async function loadIncidents() {
  const listEl = document.getElementById('incident-list');
  try {
    currentIncidents = await apiFetch(`/nets/${currentIncidentNetId}/incidents`);
  } catch (e) {
    listEl.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${esc(e.message)}</p>`;
    return;
  }
  if (!currentIncidents.length) {
    listEl.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${t('No incidents recorded for this net yet.')}</p>`;
    return;
  }
  listEl.innerHTML = currentIncidents.map(i => `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--lc-border);cursor:pointer" onclick="openIncident(${i.id})">
      <div>
        <strong>${esc(i.title)}</strong>
        <span style="font-size:11px;color:${i.status === 'active' ? 'var(--lc-red)' : 'var(--text-muted)'};margin-left:8px">${esc(i.status.toUpperCase())}</span>
      </div>
      <span style="font-size:12px;color:var(--text-muted)">${tn(i.station_count, '{n} station', '{n} stations')}</span>
    </div>
  `).join('');
}

// ── Create/edit form ──────────────────────────────────
function showNewIncidentForm() {
  editingIncidentId = null;
  hideIncidentDetail();
  document.getElementById('incident-form-heading').textContent = t('NEW INCIDENT');
  document.getElementById('incident-title').value = '';
  document.getElementById('incident-description').value = '';
  renderIncidentZoneCheckboxes([]);
  document.getElementById('incident-form-card').style.display = '';
}

function renderIncidentZoneCheckboxes(selectedIds) {
  const el = document.getElementById('incident-zone-checkboxes');
  const searchInput = document.getElementById('incident-zone-search');
  if (searchInput) searchInput.value = '';
  document.getElementById('incident-zone-no-results').style.display = 'none';

  if (!currentIncidentZoneBoundaries.length) {
    el.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${t('No synced zones for this net yet.')}</p>`;
    updateIncidentZoneSelectedCount();
    return;
  }
  // data-search carries name/county/status pre-lowercased so filtering
  // (below) is a plain substring check per keystroke, no re-render --
  // a checked box that scrolls out of a filtered view stays checked,
  // since the row is only hidden (display:none), never removed from the DOM.
  el.innerHTML = currentIncidentZoneBoundaries.map(z => {
    const label = z.name || z.external_id;
    const searchText = `${label} ${z.county || ''} ${z.status || ''}`.toLowerCase();
    return `
    <label class="incident-zone-row" data-search="${esc(searchText)}" style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:13px;cursor:pointer">
      <input type="checkbox" class="incident-zone-checkbox" value="${z.id}" ${selectedIds.includes(z.id) ? 'checked' : ''} onchange="updateIncidentZoneSelectedCount()" style="width:auto" />
      <span>${esc(label)}${z.status ? ` — ${esc(z.status)}` : ''}</span>
    </label>`;
  }).join('');
  updateIncidentZoneSelectedCount();
}

function filterIncidentZoneCheckboxes(query) {
  const q = query.trim().toLowerCase();
  let anyVisible = false;
  document.querySelectorAll('.incident-zone-row').forEach(row => {
    const matches = !q || row.dataset.search.includes(q);
    row.style.display = matches ? 'flex' : 'none';
    if (matches) anyVisible = true;
  });
  document.getElementById('incident-zone-no-results').style.display = (q && !anyVisible) ? '' : 'none';
}

function updateIncidentZoneSelectedCount() {
  const el = document.getElementById('incident-zone-selected-count');
  if (!el) return;
  const n = document.querySelectorAll('.incident-zone-checkbox:checked').length;
  el.textContent = n ? tn(n, '{n} zone selected', '{n} zones selected') : '';
}

function cancelIncidentForm() {
  hideIncidentForm();
}

function hideIncidentForm() {
  document.getElementById('incident-form-card').style.display = 'none';
}

async function saveIncident() {
  const title = document.getElementById('incident-title').value.trim();
  if (!title) return toast(t('Incident title is required'), 'error');
  const description = document.getElementById('incident-description').value.trim() || null;
  const evac_zone_boundary_ids = Array.from(document.querySelectorAll('.incident-zone-checkbox:checked')).map(cb => parseInt(cb.value, 10));
  try {
    if (editingIncidentId) {
      await apiFetch(`/incidents/${editingIncidentId}`, { method: 'PATCH', body: JSON.stringify({ title, description, evac_zone_boundary_ids }) });
      toast(t('Incident updated'), 'success');
    } else {
      await apiFetch(`/nets/${currentIncidentNetId}/incidents`, { method: 'POST', body: JSON.stringify({ title, description, evac_zone_boundary_ids }) });
      toast(t('Incident created'), 'success');
    }
    hideIncidentForm();
    await loadIncidents();
  } catch (e) { toast(e.message, 'error'); }
}

// ── Detail view ────────────────────────────────────────
async function openIncident(id) {
  hideIncidentForm();
  openIncidentId = id;
  let incident = currentIncidents.find(i => i.id === id);
  if (!incident) incident = await apiFetch(`/incidents/${id}`);

  document.getElementById('incident-detail-title').textContent = incident.title;
  document.getElementById('incident-detail-description').textContent = incident.description || '';
  const statusBtn = document.getElementById('incident-status-btn');
  statusBtn.textContent = incident.status === 'active' ? t('Mark Resolved') : t('Reopen');
  document.getElementById('incident-detail-card').style.display = '';

  // Reuse evac-zone-map.js as-is for this incident's selected zone boundaries
  const zones = currentIncidentZoneBoundaries.filter(z => incident.zone_ids.includes(z.id));
  const mapContainer = document.getElementById('incident-map-container');
  if (zones.length) {
    mapContainer.style.display = '';
    initEvacZoneMap('incident-map-container', zones);
    setTimeout(() => invalidateEvacZoneMapSize('incident-map-container'), 0);
  } else {
    mapContainer.style.display = 'none';
  }

  document.getElementById('incident-scan-status').textContent = '';
  await loadIncidentStations();
}

function hideIncidentDetail() {
  openIncidentId = null;
  document.getElementById('incident-detail-card').style.display = 'none';
}

function editIncident() {
  if (!openIncidentId) return;
  const incident = currentIncidents.find(i => i.id === openIncidentId);
  if (!incident) return;
  editingIncidentId = incident.id;
  document.getElementById('incident-form-heading').textContent = t('EDIT INCIDENT');
  document.getElementById('incident-title').value = incident.title;
  document.getElementById('incident-description').value = incident.description || '';
  renderIncidentZoneCheckboxes(incident.zone_ids);
  hideIncidentDetail();
  document.getElementById('incident-form-card').style.display = '';
}

async function toggleIncidentStatus() {
  if (!openIncidentId) return;
  const incident = currentIncidents.find(i => i.id === openIncidentId);
  if (!incident) return;
  const newStatus = incident.status === 'active' ? 'resolved' : 'active';
  try {
    await apiFetch(`/incidents/${openIncidentId}`, { method: 'PATCH', body: JSON.stringify({ status: newStatus }) });
    toast(newStatus === 'resolved' ? t('Incident marked resolved') : t('Incident reopened'), 'success');
    const reopenId = openIncidentId;
    await loadIncidents();
    await openIncident(reopenId);
  } catch (e) { toast(e.message, 'error'); }
}

async function deleteIncidentConfirm() {
  if (!openIncidentId) return;
  if (!confirm(t("Delete this incident? This also removes its station list. This can't be undone."))) return;
  try {
    await apiFetch(`/incidents/${openIncidentId}`, { method: 'DELETE' });
    toast(t('Incident deleted'), 'success');
    hideIncidentDetail();
    await loadIncidents();
  } catch (e) { toast(e.message, 'error'); }
}

async function scanIncidentNow() {
  if (!openIncidentId) return;
  const btn = document.getElementById('incident-scan-btn');
  btn.disabled = true;
  try {
    const result = await apiFetch(`/incidents/${openIncidentId}/scan`, { method: 'POST' });
    document.getElementById('incident-scan-status').textContent = tn(result.added, 'Added {n} station', 'Added {n} stations');
    await loadIncidentStations();
    await loadIncidents();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

// ── Station roster ─────────────────────────────────────
async function loadIncidentStations() {
  const el = document.getElementById('incident-stations-list');
  try {
    openIncidentStations = await apiFetch(`/incidents/${openIncidentId}/stations`);
  } catch (e) {
    el.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${esc(e.message)}</p>`;
    return;
  }
  renderIncidentStations();
}

function renderIncidentStations() {
  const el = document.getElementById('incident-stations-list');
  if (!openIncidentStations.length) {
    el.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${t('No stations yet — scan or add one manually.')}</p>`;
    return;
  }
  el.innerHTML = openIncidentStations.map(s => `
    <div style="padding:10px 0;border-bottom:1px solid var(--lc-border)">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
        <div>
          <span style="font-weight:700;font-family:monospace">${esc(s.callsign)}</span>
          ${s.name ? ` — ${esc(s.name)}` : ''}
          <span style="font-size:11px;color:var(--text-muted);margin-left:8px">${esc(t(INCIDENT_MATCH_REASON_LABELS[s.match_reason] || s.match_reason))}</span>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <select class="form-control" style="font-size:12px;width:auto" onchange="updateIncidentStation(${s.id}, { status: this.value })">
            ${INCIDENT_STATION_STATUSES.map(st => `<option value="${st}" ${s.status === st ? 'selected' : ''}>${esc(t(INCIDENT_STATION_STATUS_LABELS[st]))}</option>`).join('')}
          </select>
          <button class="btn btn-ghost btn-sm" onclick="removeIncidentStation(${s.id})" title="${esc(t('Remove'))}">✕</button>
        </div>
      </div>
      <textarea class="form-control" style="margin-top:6px;font-size:12px" rows="1" placeholder="${esc(t('Notes about their situation…'))}"
        onchange="updateIncidentStation(${s.id}, { notes: this.value })">${esc(s.notes || '')}</textarea>
    </div>
  `).join('');
}

async function updateIncidentStation(stationId, patch) {
  try {
    await apiFetch(`/incidents/${openIncidentId}/stations/${stationId}`, { method: 'PATCH', body: JSON.stringify(patch) });
    const idx = openIncidentStations.findIndex(s => s.id === stationId);
    if (idx !== -1) Object.assign(openIncidentStations[idx], patch);
  } catch (e) {
    toast(e.message, 'error');
    await loadIncidentStations();
  }
}

async function addIncidentStation() {
  const callsign = document.getElementById('incident-add-callsign').value.trim();
  if (!callsign) return toast(t('Callsign is required'), 'error');
  const name = document.getElementById('incident-add-name').value.trim() || null;
  try {
    await apiFetch(`/incidents/${openIncidentId}/stations`, { method: 'POST', body: JSON.stringify({ callsign, name }) });
    document.getElementById('incident-add-callsign').value = '';
    document.getElementById('incident-add-name').value = '';
    await loadIncidentStations();
    await loadIncidents();
  } catch (e) { toast(e.message, 'error'); }
}

async function removeIncidentStation(stationId) {
  try {
    await apiFetch(`/incidents/${openIncidentId}/stations/${stationId}`, { method: 'DELETE' });
    await loadIncidentStations();
    await loadIncidents();
  } catch (e) { toast(e.message, 'error'); }
}
