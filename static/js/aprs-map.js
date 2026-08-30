// ============================================================
// APRS MAP — shared Leaflet init/update (issue #22)
// Used by both the authenticated live-session panel (sessions.js) and the
// public live page (public.html) -- one shared module so both stay in
// sync, backed by self-hosted Leaflet + OpenStreetMap tiles (no API key,
// no CDN -- see static/vendor/leaflet/).
// ============================================================
const _aprsMaps = {};         // containerId -> Leaflet map instance
const _aprsMarkerLayers = {}; // containerId -> L.layerGroup

// v1 shows only the latest known position per callsign -- no historical
// track/trail rendering (documented follow-up in the issue #22 plan).
function initAprsMap(containerId, positions) {
  if (typeof L === 'undefined') return null;  // Leaflet failed to load
  if (_aprsMaps[containerId]) {
    updateAprsMap(containerId, positions);
    return _aprsMaps[containerId];
  }

  const map = L.map(containerId, { scrollWheelZoom: false });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
  }).addTo(map);

  _aprsMaps[containerId] = map;
  _aprsMarkerLayers[containerId] = L.layerGroup().addTo(map);
  updateAprsMap(containerId, positions);
  return map;
}

function updateAprsMap(containerId, positions) {
  const map = _aprsMaps[containerId];
  if (!map) return;
  const layer = _aprsMarkerLayers[containerId];
  layer.clearLayers();

  const valid = (positions || []).filter(p => typeof p.lat === 'number' && typeof p.lon === 'number');
  if (valid.length === 0) {
    map.setView([39.8283, -98.5795], 3);  // no positions yet -- default continental-US-ish view
    return;
  }

  const bounds = [];
  valid.forEach(p => {
    const marker = L.marker([p.lat, p.lon]);
    const lines = [`<strong>${esc(p.callsign)}</strong>`];
    const details = [];
    if (p.course != null) details.push(`${p.course}°`);
    if (p.speed != null) details.push(`${p.speed} kt`);
    if (p.altitude != null) details.push(`${p.altitude} ft`);
    if (details.length) lines.push(details.join(' · '));
    if (p.comment) lines.push(esc(p.comment));
    if (p.heard_at) lines.push(`<span style="color:#888;font-size:11px">${esc(p.heard_at)}</span>`);
    marker.bindPopup(lines.join('<br>'));
    marker.addTo(layer);
    bounds.push([p.lat, p.lon]);
  });

  if (bounds.length === 1) {
    map.setView(bounds[0], 12);
  } else {
    map.fitBounds(bounds, { padding: [30, 30] });
  }
}

// Leaflet keeps internal size caches; a map created while its container was
// hidden (display:none) renders blank/mis-sized until told to re-measure.
function invalidateAprsMapSize(containerId) {
  const map = _aprsMaps[containerId];
  if (map) map.invalidateSize();
}
