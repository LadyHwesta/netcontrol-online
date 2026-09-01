// ============================================================
// APRS MAP — shared Leaflet init/update (issue #22)
// Used by both the authenticated live-session panel (sessions.js) and the
// public live page (public.html) -- one shared module so both stay in
// sync, backed by self-hosted Leaflet + OpenStreetMap tiles (no API key,
// no CDN -- see static/vendor/leaflet/).
// ============================================================
const _aprsMaps = {};         // containerId -> Leaflet map instance
const _aprsMarkerLayers = {}; // containerId -> L.layerGroup
const _aprsFiAttributionAdded = {}; // containerId -> bool

// This module is shared with public.html (issue #22), which is a self-
// contained page with no i18n.js loaded at all (see TECH_DEBT.md) -- a bare
// t() call here would throw ReferenceError there. Falls back to identity so
// this file works untranslated on the public page and translated wherever
// i18n.js is actually loaded (the authenticated session panel).
const _t = typeof t === 'function' ? t : (s => s);

// aprs.fi's terms require crediting them as the data source with a link
// back, wherever their data is displayed -- https://aprs.fi/page/api. Added
// to the same Leaflet attribution control as the OSM credit (bottom-right
// corner), so it's a real visible link, not just text colored to blend in.
const APRS_FI_ATTRIBUTION = () => `${_t('Position data via')} <a href="https://aprs.fi" target="_blank" rel="noopener">aprs.fi</a>`;

// v1 shows only the latest known position per callsign -- no historical
// track/trail rendering (documented follow-up in the issue #22 plan).
// sourceType: the net's configured APRS source ("aprs_fi" | "relay" | null/
// undefined for manual-only) -- only "aprs_fi" owes the credit above.
function initAprsMap(containerId, positions, sourceType) {
  if (typeof L === 'undefined') return null;  // Leaflet failed to load
  if (_aprsMaps[containerId]) {
    updateAprsMap(containerId, positions, sourceType);
    return _aprsMaps[containerId];
  }

  const map = L.map(containerId, { scrollWheelZoom: false });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
  }).addTo(map);

  _aprsMaps[containerId] = map;
  _aprsMarkerLayers[containerId] = L.layerGroup().addTo(map);
  updateAprsMap(containerId, positions, sourceType);
  return map;
}

function updateAprsMap(containerId, positions, sourceType) {
  const map = _aprsMaps[containerId];
  if (!map) return;

  const wantsAttribution = sourceType === 'aprs_fi';
  if (wantsAttribution && !_aprsFiAttributionAdded[containerId]) {
    map.attributionControl.addAttribution(APRS_FI_ATTRIBUTION());
    _aprsFiAttributionAdded[containerId] = true;
  } else if (!wantsAttribution && _aprsFiAttributionAdded[containerId]) {
    map.attributionControl.removeAttribution(APRS_FI_ATTRIBUTION());
    _aprsFiAttributionAdded[containerId] = false;
  }

  const layer = _aprsMarkerLayers[containerId];
  layer.clearLayers();

  const valid = (positions || []).filter(p => typeof p.lat === 'number' && typeof p.lon === 'number');
  if (valid.length === 0) {
    map.setView([39.8283, -98.5795], 3);  // no positions yet -- default continental-US-ish view
    return;
  }

  const bounds = [];
  valid.forEach(p => {
    // Manual positions (issue follow-up) get a distinct color so they read
    // as self-reported, not live APRS tracking -- L.marker has no built-in
    // color option, so a manual pin uses a small circle marker instead.
    const marker = p.source === 'manual'
      ? L.circleMarker([p.lat, p.lon], { radius: 8, color: '#fff', weight: 2, fillColor: '#ff9900', fillOpacity: 1 })
      : L.marker([p.lat, p.lon]);
    const lines = [`<strong>${esc(p.callsign)}</strong>`];
    const details = [];
    if (p.course != null) details.push(`${p.course}°`);
    if (p.speed != null) details.push(`${p.speed} kt`);
    if (p.altitude != null) details.push(`${p.altitude} ft`);
    if (details.length) lines.push(details.join(' · '));
    if (p.comment) lines.push(esc(p.comment));
    if (p.source === 'manual') lines.push(`<span style="color:#ff9900;font-size:11px">📍 ${_t('Manually reported')}</span>`);
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
