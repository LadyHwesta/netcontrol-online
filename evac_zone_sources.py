"""
NetControl Online — evacuation zone data sources (issue #27)

Pulls real evacuation zone polygons from external government GIS APIs, so
operators pick from an authoritative, current list instead of hand-typing
zone names -- and so a future incident-reporting feature (issue #28) has
real geometry to match station positions against.

A small registry (SOURCES below), not a generic config-driven GIS client:
adding a new source means writing one fetch_* function (its own field-name
mapping baked in, since every GIS schema varies) and registering it here --
deliberately not building a generic "configure any ArcGIS layer" system.

Two DIFFERENT kinds of source, both landing in the same EvacZoneBoundary
table (its (net_id, source, external_id) uniqueness was designed for
exactly this -- multiple sources coexisting per net, never colliding):

  - STATE-level, active-incidents-only (data_ca_gov): California's
    statewide aggregation layer only ever contains a zone while it's
    actively under some status (Order/Warning/...) -- confirmed live
    while building this (68 rows total statewide at the time, only 3
    distinct STATUS values ever present, never "Normal"). It goes empty
    the moment nothing's happening, which is correct for "what's active
    right now" but useless for picking a zone during a routine
    (non-activation) net.
  - COUNTY-level, full static catalog (issue follow-up -- e.g.
    sonoma_county_gov): many CA counties run their own "Know Your Zone"
    GIS system with EVERY predefined zone always present, most showing
    zone_status="Normal" outside an incident. This is what makes zone
    selection useful on an ordinary net, not just during an activation.
    Each county's service has its own URL and field names -- there's no
    statewide "full catalog" resource -- so this list only grows one
    hand-verified county at a time, same as adding a new state does.

Both kinds select by matching Net.state/Net.region respectively, and
sync_net_evac_zones() pulls from every source that matches (a net can get
rows from both a state-level and a county-level source at once).

Deliberately live/on-demand, not a cron sync like gmrs_sync.py: the
California statewide feed is "fully updated every 5 minutes" during an
active incident, and nets that track evac zones are, by Net.is_ares,
already in an active-incident context -- a nightly sync would be stale
exactly when it matters. See routers/evac_zones.py's POST
/nets/{id}/evac-zone-sync, which calls sync_net_evac_zones() directly
from the request handler and lets exceptions propagate (this is a
deliberate, visible admin action, not a best-effort background side
effect like net_repository.py's pushes -- the admin should see a real
error if the sync actually fails).
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
    """Raised when neither a net's Net.state nor Net.region matches any
    registered source."""


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
        # county -- so a trailing "county" is stripped before matching
        # (see _strip_county_suffix below).
        safe_county = _strip_county_suffix(county).replace("'", "''")
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


# Sonoma County's own full evacuation zone catalog (issue follow-up) --
# verified live: 332 zones covering every jurisdiction in the county, each
# carrying its own current zone_status (a coded domain that includes
# "Normal" -- confirmed all 332 show that value absent an active incident,
# proving this really is the complete catalog, not another active-only
# feed). Found via the item's ArcGIS Online listing ("Sonoma County
# Emergency Zones public"); no API key needed.
SONOMA_FEATURE_SERVER_QUERY_URL = (
    "https://services1.arcgis.com/P5Mv5GY5S66M8Z1Q/arcgis/rest/services/"
    "Sonoma_County_Evacuation_Areas_public/FeatureServer/0/query"
)


async def fetch_sonoma_county_gov(county: Optional[str]) -> list[dict]:
    """Sonoma County's full zone catalog -- unlike fetch_data_ca_gov, this
    service is already scoped to exactly one county, so there's no filter
    to apply; `county` is accepted only for a consistent registry
    signature. ZoneNumber (e.g. "SO-C01") is a stable per-zone code used
    as external_id; Summary is the short human-readable label ("Southeast
    City of Sonoma") used as name -- Description is a much longer prose
    boundary description, not stored here. No last-edited timestamp field
    exists on this layer (unlike data_ca_gov's EDIT_DATE)."""
    params = {
        "where": "1=1",
        "outFields": "Jurisdiction,ZoneNumber,zone_status,Summary",
        "f": "geojson",
        "returnGeometry": "true",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(SONOMA_FEATURE_SERVER_QUERY_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    zones = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        geometry = feature.get("geometry")
        external_id = props.get("ZoneNumber")
        if not geometry or not external_id:
            continue
        zones.append({
            "external_id": external_id,
            "name": props.get("Summary"),
            "county": props.get("Jurisdiction"),
            "status": props.get("zone_status"),
            "geometry": geometry,
            "source_updated_at": None,
        })
    return zones


# Registry -- add a new source by writing a fetch_* function above (or in
# a new module imported here) with this same signature, and one entry
# below (plus a STATE_ALIASES or COUNTY_ALIASES entry, matching whichever
# kind it is).
SOURCES = {
    "data_ca_gov": fetch_data_ca_gov,
    "sonoma_county_gov": fetch_sonoma_county_gov,
}

# STATE-level sources: which Net.state values map to which source.
# Net.state is free text (the net edit form's placeholder is "WA", but
# nothing enforces a 2-letter code), so this is a small alias list,
# matched case-insensitively -- not a strict ISO-3166 lookup.
STATE_ALIASES = {
    "data_ca_gov": {"CA", "CALIFORNIA"},
}

# COUNTY-level sources: which Net.region values map to which source. Same
# free-text-alias matching as STATE_ALIASES, with a trailing "county"
# stripped first (same reasoning as fetch_data_ca_gov's own county-filter
# normalization -- Net.region's established placeholder is "Snohomish
# County").
COUNTY_ALIASES = {
    "sonoma_county_gov": {"SONOMA"},
}


def _strip_county_suffix(value: str) -> str:
    normalized = value.strip().upper()
    if normalized.endswith(" COUNTY"):
        normalized = normalized[: -len(" COUNTY")].strip()
    return normalized


def select_source_for_state(state: Optional[str]) -> Optional[str]:
    if not state:
        return None
    normalized = state.strip().upper()
    for source, aliases in STATE_ALIASES.items():
        if normalized in aliases:
            return source
    return None


def select_source_for_county(region: Optional[str]) -> Optional[str]:
    if not region:
        return None
    normalized = _strip_county_suffix(region)
    for source, aliases in COUNTY_ALIASES.items():
        if normalized in aliases:
            return source
    return None


async def sync_net_evac_zones(net: Net, db: AsyncSession) -> int:
    """Fetches from every source that matches this net's state (active-
    incidents feed) AND region (a county's full catalog, if one's
    registered) -- a net can pull from both at once, e.g. California's
    statewide feed for "what's active right now" alongside Sonoma
    County's own catalog for "what zones exist at all". Each matched
    source replaces its own EvacZoneBoundary rows for (net.id, source) in
    one transaction -- a source's zone set is small (tens to low
    hundreds of rows), so a full delete-and-reinsert per source is
    simpler than diffing individual zone lifecycle (a retired/merged
    zone just disappears on the next sync). Raises UnsupportedSourceError
    if NEITHER state nor region matches any registered source; lets any
    fetch failure (network error, non-2xx response) propagate as-is --
    on a multi-source net, one failing aborts the whole sync rather than
    partially committing, so a retry doesn't need to guess what's stale."""
    sources = []
    state_source = select_source_for_state(net.state)
    if state_source:
        sources.append(state_source)
    county_source = select_source_for_county(net.region)
    if county_source and county_source not in sources:
        sources.append(county_source)
    if not sources:
        raise UnsupportedSourceError(net.state)

    total = 0
    for source in sources:
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
        total += len(zones)
    await db.commit()
    return total
