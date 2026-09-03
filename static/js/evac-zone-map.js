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
const _evacZoneIds = {};     // containerId -> last-rendered boundary id fingerprint (issue follow-up, see updateEvacZoneMap)

function _evacZoneColor(status) {
  const s = (status || '').toLowerCase();
  if (s.includes('order')) return '#ff3b3b';
  if (s.includes('warning')) return '#ff9900';
  if (s.includes('shelter')) return '#a855f7';
  return '#888';
}

function initEvacZoneMap(containerId, boundaries, zoneCounts) {
  if (typeof L === 'undefined') return null;  // Leaflet failed to load
  if (_evacZoneMaps[containerId]) {
    updateEvacZoneMap(containerId, boundaries, zoneCounts);
    return _evacZoneMaps[containerId];
  }

  const map = L.map(containerId, { scrollWheelZoom: false });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
  }).addTo(map);

  _evacZoneMaps[containerId] = map;
  updateEvacZoneMap(containerId, boundaries, zoneCounts);
  return map;
}

// zoneCounts (issue follow-up, optional): {ZONE NAME -> checked-in station
// count}, keyed the same way as each feature's own `name` below (trimmed,
// uppercased) -- see checkins.js's computeZoneCheckinCounts(), which builds
// it from the live Zone Roster (Checkin.evac_zone, free-text matched same
// as incident_matching.py's own zone_report signal). A zone with a nonzero
// count gets a heavier border/fill and a permanent count badge, layered on
// top of (not replacing) the status color -- lets net control see at a
// glance which covered zones actually have someone reporting from them,
// not just which zones exist. Omitted entirely by the public incident map's
// own call site, which has no live check-in data to show.
function updateEvacZoneMap(containerId, boundaries, zoneCounts) {
  const map = _evacZoneMaps[containerId];
  if (!map) return;
  zoneCounts = zoneCounts || {};
  // Fit to bounds only when the actual SET of zones shown has changed --
  // a different net's boundaries (switching sessions within the same page
  // load), or a re-sync that added/removed zones (issue follow-up:
  // checkins.js now calls this on every check-in refresh too, to keep the
  // live-reporting badges current, and re-snapping an operator's own pan/
  // zoom back to the full view on every one of those, when the zones on
  // screen haven't actually changed, would be far more disruptive than the
  // occasional re-sync used to be).
  const idFingerprint = (boundaries || []).map(b => b.id).sort((a, b) => a - b).join(',');
  const shouldRefit = _evacZoneIds[containerId] !== idFingerprint;
  _evacZoneIds[containerId] = idFingerprint;

  if (_evacZoneLayers[containerId]) {
    map.removeLayer(_evacZoneLayers[containerId]);
    delete _evacZoneLayers[containerId];
  }

  const valid = (boundaries || []).filter(b => b.geometry);
  if (valid.length === 0) {
    map.setView([39.8283, -98.5795], 3);
    return;
  }

  const layer = L.geoJSON(valid.map(b => {
    const name = (b.name || '').trim() || b.external_id;
    const count = zoneCounts[(name || '').trim().toUpperCase()] || 0;
    return {
      type: 'Feature',
      properties: { name, status: b.status, county: b.county, count },
      geometry: b.geometry,
    };
  }), {
    style: f => ({
      color: _evacZoneColor(f.properties.status),
      weight: f.properties.count > 0 ? 4 : 2,
      fillOpacity: f.properties.count > 0 ? 0.55 : 0.25,
    }),
    onEachFeature: (f, l) => {
      const lines = [`<strong>${esc(f.properties.name || '')}</strong>`];
      if (f.properties.status) lines.push(esc(f.properties.status));
      if (f.properties.county) lines.push(esc(f.properties.county));
      if (f.properties.count > 0) lines.push(`📻 ${tn(f.properties.count, '{n} station reporting', '{n} stations reporting')}`);
      l.bindPopup(lines.join('<br>'));
      if (f.properties.count > 0) {
        l.bindTooltip(String(f.properties.count), {
          permanent: true, direction: 'center', className: 'evac-zone-count-badge',
        });
      }
    },
  }).addTo(map);

  _evacZoneLayers[containerId] = layer;
  if (shouldRefit) {
    const bounds = layer.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [30, 30] });
  }
}

// Leaflet keeps internal size caches; a map created while its container was
// hidden (display:none) renders blank/mis-sized until told to re-measure.
function invalidateEvacZoneMapSize(containerId) {
  const map = _evacZoneMaps[containerId];
  if (map) map.invalidateSize();
}
