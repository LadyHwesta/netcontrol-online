"""
NetControl Online — evacuation zone data sources (issue #27)

Pulls real evacuation zone polygons from external government GIS APIs, so
operators pick from an authoritative, current list instead of hand-typing
zone names -- and so a future incident-reporting feature (issue #28) has
real geometry to match station positions against.

A small registry (SOURCES below), not a generic config-driven GIS client:
today there's exactly one adapter (California's data.ca.gov evacuation
aggregation layer, a public ArcGIS FeatureServer, no API key). Adding a
new state later means writing one fetch_* function (its own field-name
mapping baked in, since state GIS schemas vary) and registering it here --
deliberately not building a generic "configure any ArcGIS layer" system
for a single currently-known source.

Deliberately live/on-demand, not a cron sync like gmrs_sync.py: the
California feed is "fully updated every 5 minutes" during an active
incident, and nets that track evac zones are, by Net.is_ares, already in
an active-incident context -- a nightly sync would be stale exactly when
it matters. See routers/evac_zones.py's POST /nets/{id}/evac-zone-sync,
which calls sync_net_evac_zones() directly from the request handler and
lets exceptions propagate (this is a deliberate, visible admin action,
not a best-effort background side effect like net_repository.py's pushes
-- the admin should see a real error if the sync actually fails).
"""

from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import EvacZoneBoundary, Net

# Verified live against the real service while planning this feature --
# public, no API key, standard ArcGIS FeatureServer query interface.
CA_FEATURE_SERVER_QUERY_URL = (
    "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/"
    "CA_EVACUATIONS_CalOESHosted_view/FeatureServer/0/query"
)


class UnsupportedSourceError(Exception):
    """Raised when a net's Net.state doesn't match any registered source."""


def _parse_source_date(value) -> Optional[datetime]:
    """ArcGIS date fields come back as either epoch-milliseconds (raw
    f=json) or an ISO string (observed with f=geojson, which is what this
    module requests) -- handle both defensively since this is purely
    informational and shouldn't ever block a sync on a parse failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


async def fetch_data_ca_gov(county: Optional[str]) -> list[dict]:
    """Queries California's statewide evacuation zone layer, optionally
    filtered to one county (so a net only pulls its own area's zones
    rather than every active zone in the state). Returns a list of
    normalized dicts: external_id, name, county, status, geometry,
    source_updated_at. Raises on any HTTP/network failure -- the caller
    (sync_net_evac_zones) doesn't catch these, letting them surface as a
    real error to the admin who triggered the sync."""
    where = "1=1"
    if county:
        # The source's COUNTY field is the bare county name with no
        # "County" suffix (e.g. "SAN LUIS OBISPO", "SONOMA") -- but
        # Net.region's own established placeholder ("Snohomish County",
        # predating this feature -- it's also shown in the public
        # directory/Net Repository listing, where the suffix reads
        # naturally) actively invites typing it WITH "County" on the end.
        # Left as an exact match against that, a region of "Sonoma
        # County" would silently match nothing, every time, for every
        # county -- so a trailing "county" is stripped before matching.
        normalized = county.strip().upper()
        if normalized.endswith(" COUNTY"):
            normalized = normalized[: -len(" COUNTY")].strip()
        safe_county = normalized.replace("'", "''")
        where = f"COUNTY='{safe_county}'"

    params = {
        "where": where,
        "outFields": "COUNTY,ZONE_NAME,ZONE_ID,STATUS,EVENT_TYPE,EDIT_DATE",
        "f": "geojson",
        "returnGeometry": "true",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(CA_FEATURE_SERVER_QUERY_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    zones = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        geometry = feature.get("geometry")
        external_id = props.get("ZONE_ID")
        # ZONE_NAME is frequently null in the real feed (confirmed live) --
        # ZONE_ID is the one field that's always populated, so it's the
        # only thing worth requiring here.
        if not geometry or not external_id:
            continue
        zones.append({
            "external_id": external_id,
            "name": props.get("ZONE_NAME"),
            "county": props.get("COUNTY"),
            "status": props.get("STATUS"),
            "geometry": geometry,
            "source_updated_at": _parse_source_date(props.get("EDIT_DATE")),
        })
    return zones


# Registry -- add a new state by writing a fetch_* function above (or in a
# new module imported here) with this same signature, and one entry below.
SOURCES = {
    "data_ca_gov": fetch_data_ca_gov,
}

# Which Net.state values map to which source. Net.state is free text (the
# net edit form's placeholder is "WA", but nothing enforces a 2-letter
# code), so this is a small alias list, matched case-insensitively --
# not a strict ISO-3166 lookup.
STATE_ALIASES = {
    "data_ca_gov": {"CA", "CALIFORNIA"},
}


def select_source_for_state(state: Optional[str]) -> Optional[str]:
    if not state:
        return None
    normalized = state.strip().upper()
    for source, aliases in STATE_ALIASES.items():
        if normalized in aliases:
            return source
    return None


async def sync_net_evac_zones(net: Net, db: AsyncSession) -> int:
    """Fetches the current zone set for this net's state/region and
    replaces every existing EvacZoneBoundary row for (net.id, source) in
    one transaction -- the source's active zone set is small (tens of
    rows for a county), so a full delete-and-reinsert is simpler than
    diffing individual zone lifecycle (a retired/merged zone just
    disappears on the next sync). Raises UnsupportedSourceError if
    net.state doesn't map to a registered source; lets any fetch
    failure (network error, non-2xx response) propagate as-is."""
    source = select_source_for_state(net.state)
    if not source:
        raise UnsupportedSourceError(net.state)

    fetch = SOURCES[source]
    zones = await fetch(net.region)

    await db.execute(delete(EvacZoneBoundary).filter(
        EvacZoneBoundary.net_id == net.id, EvacZoneBoundary.source == source,
    ))
    for zone in zones:
        db.add(EvacZoneBoundary(
            net_id=net.id, source=source,
            external_id=zone["external_id"], name=zone["name"],
            county=zone["county"], status=zone["status"],
            geometry=zone["geometry"], source_updated_at=zone["source_updated_at"],
        ))
    await db.commit()
    return len(zones)
