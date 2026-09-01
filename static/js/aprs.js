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
    document.getElementById('aprs-filter').value = currentAprsConfig.filter_callsign || '';
  } else {
    document.getElementById('aprs-source').value = 'none';
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
  } else {
    const payload = {
      source_type: src,
      filter_callsign: document.getElementById('aprs-filter').value.trim().toUpperCase() || null,
    };
    currentAprsConfig = await apiFetch(`/nets/${netId}/aprs/config`, { method: 'PUT', body: JSON.stringify(payload) });
  }

  // Default map view (issue follow-up) -- independent of source_type above;
  // works even with APRS fully disabled since manually-reported check-in
  // positions still use the same map.
  await saveAprsDefaultViewFields(netId);
}

// Reads whatever's currently in the three fields and PUTs it as part of the
// net form's own Save button flow above -- clears the default instead if
// any of the three is blank (the Clear button below just empties the
// fields; this is what actually persists that, next time Save is clicked).
async function saveAprsDefaultViewFields(netId) {
  const lat = parseFloat(document.getElementById('aprs-default-lat').value);
  const lon = parseFloat(document.getElementById('aprs-default-lon').value);
  const zoom = parseInt(document.getElementById('aprs-default-zoom').value);
  const complete = !isNaN(lat) && !isNaN(lon) && !isNaN(zoom);
  await apiFetch(`/nets/${netId}/aprs/default-view`, {
    method: 'PUT',
    body: JSON.stringify(complete ? { lat, lon, zoom } : { lat: null, lon: null, zoom: null }),
  });
}

function clearAprsDefaultViewFields() {
  document.getElementById('aprs-default-lat').value = '';
  document.getElementById('aprs-default-lon').value = '';
  document.getElementById('aprs-default-zoom').value = '';
}

// Live map panel's own button -- captures whatever the map is currently
// panned/zoomed to (no need to know or type coordinates) and saves it as
// this net's default view immediately, independent of the net edit form.
async function setAprsDefaultView() {
  const map = _aprsMaps['aprs-map-container'];
  if (!map) { toast(t('Open the map panel first.'), 'error'); return; }
  const center = map.getCenter();
  const zoom = map.getZoom();
  try {
    await apiFetch(`/nets/${currentNetId}/aprs/default-view`, {
      method: 'PUT',
      body: JSON.stringify({ lat: center.lat, lon: center.lng, zoom }),
    });
    // Keep the already-loaded net list in sync, so reopening the edit form
    // (which reads straight from `nets`, not a fresh fetch) shows the new value.
    const net = nets.find(n => n.id === currentNetId);
    if (net) { net.aprs_default_lat = center.lat; net.aprs_default_lon = center.lng; net.aprs_default_zoom = zoom; }
    toast(t('Default map view saved'), 'success');
  } catch (e) { toast(e.message, 'error'); }
}

function downloadAprsRelayScript() {
  if (!editNetId) return;
  triggerDownload(`/nets/${editNetId}/aprs/relay-script`);
  toast(t('aprs_relay.py downloaded — create an API token under 🪙 API Tokens and pass it with --token (or NT_TOKEN).'));
}

// ============================================================
// APRS STATION MAP — authenticated live-session panel
// ============================================================
let aprsMapPanelOpen = false;
let aprsMapInterval = null;
let lastAprsPositions = [];
let currentAprsSourceType = null;   // "aprs_fi" | "relay" | null -- drives the aprs.fi attribution on the map

// The net's configured default map view (issue follow-up), read from the
// already-loaded `nets` list rather than a separate fetch -- NetOut already
// carries these three fields. Returns null if the net has none set.
function _currentAprsDefaultView(netId) {
  const net = nets.find(n => n.id === netId);
  if (!net || net.aprs_default_zoom == null) return null;
  return { lat: net.aprs_default_lat, lon: net.aprs_default_lon, zoom: net.aprs_default_zoom };
}

async function initAprsForSession(netId) {
  // GMRS nets 400 on GET /aprs/config (no APRS allocation) -- caught here
  // the same way DMR's initDmrForSession relies on its own config fetch
  // 400ing, so no separate net-type check is needed. A ham net with no
  // AprsConfig at all still gets the panel (issue follow-up) -- manually-
  // reported positions work with zero APRS setup, so the fetch succeeding
  // (even with a null body) is what matters here, not whether cfg exists.
  let isHamNet = true;
  currentAprsSourceType = null;
  try {
    const cfg = await apiFetch(`/nets/${netId}/aprs/config`);
    currentAprsSourceType = cfg ? cfg.source_type : null;
  } catch {
    isHamNet = false;
  }
  const panel = document.getElementById('aprs-map-panel');
  if (isHamNet) {
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
    document.getElementById('aprs-map-last-refresh').textContent = `${t('Error:')} ${e.message}`;
    return;
  }
  if (aprsMapPanelOpen) {
    initAprsMap('aprs-map-container', lastAprsPositions, currentAprsSourceType, _currentAprsDefaultView(netId));
    invalidateAprsMapSize('aprs-map-container');
  }
  const now = new Date().toLocaleTimeString();
  document.getElementById('aprs-map-last-refresh').textContent = `${t('Last refresh:')} ${now}`;
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
    initAprsMap('aprs-map-container', lastAprsPositions, currentAprsSourceType, _currentAprsDefaultView(currentNetId));
    setTimeout(() => invalidateAprsMapSize('aprs-map-container'), 0);
  }
}
