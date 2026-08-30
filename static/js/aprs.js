// ============================================================
// APRS STATION MAP — net edit form config (issue #22)
// ============================================================
let currentAprsConfig = null;

function onAprsSourceChange() {
  const src = document.getElementById('aprs-source').value;
  document.getElementById('aprs-fi-fields').style.display    = src === 'aprs_fi' ? '' : 'none';
  document.getElementById('aprs-relay-fields').style.display = src === 'relay' ? '' : 'none';
}

async function loadAprsConfig(netId) {
  try {
    currentAprsConfig = await apiFetch(`/nets/${netId}/aprs/config`);
  } catch { currentAprsConfig = null; }
  const sec = document.getElementById('net-aprs-section');
  if (sec) sec.style.display = '';  // always show when editing own/editable ham net
  if (currentAprsConfig) {
    document.getElementById('aprs-source').value = currentAprsConfig.source_type;
    document.getElementById('aprs-fi-key').value = currentAprsConfig.aprs_fi_api_key || '';
    document.getElementById('aprs-filter').value = currentAprsConfig.filter_callsign || '';
  } else {
    document.getElementById('aprs-source').value = 'none';
    document.getElementById('aprs-fi-key').value = '';
    document.getElementById('aprs-filter').value = currentUser ? currentUser.callsign : '';
  }
  onAprsSourceChange();
}

// Called from saveNet() as part of the main net save — folded in from the
// start (not a separate "Save APRS Config" button) so a config change made
// alongside other net edits and saved via the one obvious Save button
// actually persists, avoiding the bug class the sharing fix addressed.
async function saveAprsConfigIfVisible(netId) {
  const sec = document.getElementById('net-aprs-section');
  if (!sec || sec.style.display === 'none') return;

  const src = document.getElementById('aprs-source').value;
  if (src === 'none') {
    if (currentAprsConfig) await apiFetch(`/nets/${netId}/aprs/config`, { method: 'DELETE' });
    currentAprsConfig = null;
    return;
  }
  const payload = {
    source_type: src,
    aprs_fi_api_key: document.getElementById('aprs-fi-key').value.trim() || null,
    filter_callsign: document.getElementById('aprs-filter').value.trim().toUpperCase() || null,
  };
  currentAprsConfig = await apiFetch(`/nets/${netId}/aprs/config`, { method: 'PUT', body: JSON.stringify(payload) });
}

function downloadAprsRelayScript() {
  if (!editNetId) return;
  triggerDownload(`/nets/${editNetId}/aprs/relay-script`);
  toast('aprs_relay.py downloaded — create an API token under 🪙 API Tokens and pass it with --token (or NT_TOKEN).');
}

// ============================================================
// APRS STATION MAP — authenticated live-session panel
// ============================================================
let aprsMapPanelOpen = false;
let aprsMapInterval = null;
let lastAprsPositions = [];

async function initAprsForSession(netId) {
  // GMRS nets 400 on GET /aprs/config (no APRS allocation) -- caught here
  // the same way DMR's initDmrForSession relies on its own config fetch
  // 400ing, so no separate net-type check is needed.
  let cfg = null;
  try { cfg = await apiFetch(`/nets/${netId}/aprs/config`); } catch { cfg = null; }
  const panel = document.getElementById('aprs-map-panel');
  if (cfg) {
    panel.style.display = '';
    startAprsMapPolling(netId);
  } else {
    panel.style.display = 'none';
    stopAprsMapPolling();
  }
}

function stopAprsMapPolling() {
  if (aprsMapInterval) { clearInterval(aprsMapInterval); aprsMapInterval = null; }
}

function startAprsMapPolling(netId) {
  stopAprsMapPolling();
  refreshAprsMap(netId);
  aprsMapInterval = setInterval(() => refreshAprsMap(netId), 30000);
}

async function refreshAprsMap(netIdArg) {
  const netId = netIdArg || currentNetId;
  try {
    lastAprsPositions = await apiFetch(`/nets/${netId}/aprs/positions`);
  } catch (e) {
    document.getElementById('aprs-map-last-refresh').textContent = `Error: ${e.message}`;
    return;
  }
  if (aprsMapPanelOpen) {
    initAprsMap('aprs-map-container', lastAprsPositions);
    invalidateAprsMapSize('aprs-map-container');
  }
  const now = new Date().toLocaleTimeString();
  document.getElementById('aprs-map-last-refresh').textContent = `Last refresh: ${now}`;
  const cnt = document.getElementById('aprs-map-count');
  if (lastAprsPositions.length > 0) { cnt.textContent = lastAprsPositions.length; cnt.style.display = ''; }
  else cnt.style.display = 'none';
}

function toggleAprsMapPanel() {
  aprsMapPanelOpen = !aprsMapPanelOpen;
  document.getElementById('aprs-map-body').style.display = aprsMapPanelOpen ? '' : 'none';
  document.getElementById('aprs-map-toggle-icon').textContent = aprsMapPanelOpen ? '▼' : '▶';
  if (aprsMapPanelOpen) {
    // Lazily init the map only once the panel is actually visible --
    // Leaflet needs a non-zero-size, visible container to measure
    // correctly, which a display:none body doesn't provide.
    initAprsMap('aprs-map-container', lastAprsPositions);
    setTimeout(() => invalidateAprsMapSize('aprs-map-container'), 0);
  }
}
