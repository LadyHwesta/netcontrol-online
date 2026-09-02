// ============================================================
// EVACUATION ZONE MAP — shared Leaflet init/update (issue #27)
// Renders synced EvacZoneBoundary polygons (GeoJSON) color-coded by
// status. Same self-hosted Leaflet + OpenStreetMap tile setup as
// aprs-map.js (static/vendor/leaflet/, no API key, no CDN), kept in its
// own module since this renders zone polygons rather than station points
// -- and is meant to be reused as-is by a future public incident map
// (issue #28), the way aprs-map.js is already shared between the
// authenticated session panel and public.html.
// ============================================================
const _evacZoneMaps = {};    // containerId -> Leaflet map instance
const _evacZoneLayers = {};  // containerId -> L.geoJSON layer

function _evacZoneColor(status) {
  const s = (status || '').toLowerCase();
  if (s.includes('order')) return '#ff3b3b';
  if (s.includes('warning')) return '#ff9900';
  if (s.includes('shelter')) return '#a855f7';
  return '#888';
}

function initEvacZoneMap(containerId, boundaries) {
  if (typeof L === 'undefined') return null;  // Leaflet failed to load
  if (_evacZoneMaps[containerId]) {
    updateEvacZoneMap(containerId, boundaries);
    return _evacZoneMaps[containerId];
  }

  const map = L.map(containerId, { scrollWheelZoom: false });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
  }).addTo(map);

  _evacZoneMaps[containerId] = map;
  updateEvacZoneMap(containerId, boundaries);
  return map;
}

function updateEvacZoneMap(containerId, boundaries) {
  const map = _evacZoneMaps[containerId];
  if (!map) return;

  if (_evacZoneLayers[containerId]) {
    map.removeLayer(_evacZoneLayers[containerId]);
    delete _evacZoneLayers[containerId];
  }

  const valid = (boundaries || []).filter(b => b.geometry);
  if (valid.length === 0) {
    map.setView([39.8283, -98.5795], 3);
    return;
  }

  const layer = L.geoJSON(valid.map(b => ({
    type: 'Feature',
    properties: { name: b.name || b.external_id, status: b.status, county: b.county },
    geometry: b.geometry,
  })), {
    style: f => ({ color: _evacZoneColor(f.properties.status), weight: 2, fillOpacity: 0.25 }),
    onEachFeature: (f, l) => {
      const lines = [`<strong>${esc(f.properties.name || '')}</strong>`];
      if (f.properties.status) lines.push(esc(f.properties.status));
      if (f.properties.county) lines.push(esc(f.properties.county));
      l.bindPopup(lines.join('<br>'));
    },
  }).addTo(map);

  _evacZoneLayers[containerId] = layer;
  const bounds = layer.getBounds();
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [30, 30] });
}

// Leaflet keeps internal size caches; a map created while its container was
// hidden (display:none) renders blank/mis-sized until told to re-measure.
function invalidateEvacZoneMapSize(containerId) {
  const map = _evacZoneMaps[containerId];
  if (map) map.invalidateSize();
}
