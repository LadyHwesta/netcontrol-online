"""
NetControl Online — live hazard feed for the Incidents page (issue
follow-up to #28/#27)

Polls public, no-API-key government data sources for what's actively
happening in a net's county (fires, earthquakes, power outages, and
significant weather alerts) so an operator can turn one into an Incident
in a click instead of typing it up from scratch. Peer module to
evac_zone_sources.py -- same shape (a small registry of fetch_* functions,
one per source, each with its own field-name mapping baked in since every
source's schema differs), same COUNTY_ALIASES-style region-matching
convention (reuses _strip_county_suffix from evac_zone_sources.py
directly rather than re-implementing it).

Four sources, all verified live while building this feature -- real
current data for Sonoma County at the time:

  - USGS earthquakes (fdsnws Event Service) -- no county field, filtered
    by a per-county bounding box instead.
  - CAL FIRE incidents (fire.ca.gov's own GeoJsonList API) -- has County
    directly.
  - NWS active alerts (api.weather.gov, by county UGC code) -- real
    polygon geometry per alert, unlike the other three (point-only).
  - Cal OES statewide power outages (an ArcGIS FeatureServer aggregating
    PG&E/SCE/SDG&E/SMUD's own public outage maps, updated ~every 15 min
    per its own dataset notes) -- has County directly.

Deliberately live/on-demand every request, not cached or cron-synced --
same reasoning as evac_zone_sources.py's own choice (see its module
docstring), just stronger here: this data is the whole point of the
feature, and a stale copy of "what's happening right now" defeats it.
Nothing fetched here is ever persisted -- see IncidentFeedDismissal in
models.py for the one thing that IS (what an operator already did with a
given item), and list_feed_items_for_net() below for where that's
consulted.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from shapely.geometry import Point, shape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evac_zone_sources import _strip_county_suffix
from models import EvacZoneBoundary, IncidentFeedDismissal, Net

# Per-county config -- one entry to start (Sonoma), extending to another
# county later is one more entry here, same as evac_zone_sources.py's own
# COUNTY_ALIASES. bbox is (minlat, maxlat, minlon, maxlon), padded a bit
# generous around the county line since a quake felt locally can epicenter
# just outside it. county_match is compared case-insensitively against
# each source's own County field. nws_ugc is the NWS county UGC code --
# found via https://alerts.weather.gov/'s own zone/county list.
COUNTY_CONFIG = {
    "SONOMA": {
        "bbox": (38.0, 39.0, -123.6, -122.35),
        "county_match": {"SONOMA"},
        "nws_ugc": "CAC097",
    },
}

USGS_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
CALFIRE_INCIDENTS_URL = "https://www.fire.ca.gov/umbraco/api/IncidentApi/GeoJsonList"
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active/zone/{ugc}"
CALOES_POWER_OUTAGES_URL = (
    "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/"
    "Power_Outages_(View)/FeatureServer/0/query"
)

# api.weather.gov requires an identifying User-Agent on every request (its
# own published API etiquette) -- unlike the other three sources, an
# unidentified request isn't rejected outright but the docs ask for one.
_NWS_HEADERS = {"User-Agent": "NetControlOnline/1.0 (https://github.com/LadyHwesta/netcontrol-online)"}

# Default significance filters -- keeps the list to things actually worth
# an operator's attention, not every microquake or single-meter blip.
MIN_EARTHQUAKE_MAGNITUDE = 2.5
EARTHQUAKE_LOOKBACK_DAYS = 14
NWS_SIGNIFICANT_SEVERITIES = {"Moderate", "Severe", "Extreme"}
MIN_OUTAGE_CUSTOMERS = 50


def _parse_date(value) -> Optional[datetime]:
    """Same defensive epoch-ms-or-ISO-string handling as
    evac_zone_sources._parse_source_date -- purely informational, never
    worth blocking a fetch over. Kept as its own copy rather than a
    cross-module import since the two sources modules are peers, not
    dependents of each other."""
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


async def fetch_usgs_earthquakes(county: str) -> list[dict]:
    """Earthquakes within the county's configured bounding box, magnitude
    >= MIN_EARTHQUAKE_MAGNITUDE, from the last EARTHQUAKE_LOOKBACK_DAYS
    days. No county field on this source at all -- bbox is the only
    filter available, so a result can technically epicenter just outside
    the county line; acceptable since shaking isn't bounded by it either."""
    config = COUNTY_CONFIG[county]
    minlat, maxlat, minlon, maxlon = config["bbox"]
    start = (datetime.now(timezone.utc) - timedelta(days=EARTHQUAKE_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    params = {
        "format": "geojson", "starttime": start,
        "minlatitude": minlat, "maxlatitude": maxlat,
        "minlongitude": minlon, "maxlongitude": maxlon,
        "minmagnitude": MIN_EARTHQUAKE_MAGNITUDE, "orderby": "time", "limit": 50,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(USGS_QUERY_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    items = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        mag = props.get("mag")
        items.append({
            "source": "usgs_earthquakes",
            "external_id": feature.get("id") or "",
            "category": "earthquake",
            "title": props.get("title") or props.get("place") or "Earthquake",
            "description": props.get("place"),
            "severity": f"M{mag:.1f}" if isinstance(mag, (int, float)) else None,
            "county": None,
            "lat": lat, "lon": lon,
            "geometry": None,
            "occurred_at": _parse_date(props.get("time")),
            "url": props.get("url"),
        })
    return items


async def fetch_calfire_incidents(county: str) -> list[dict]:
    """Active wildfires (IsActive) whose own County field matches this
    county's configured aliases. fire.ca.gov redirects this endpoint
    (http->https observed live), so follow_redirects is required."""
    config = COUNTY_CONFIG[county]
    params = {"year": datetime.now(timezone.utc).year, "inactive": "false"}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(CALFIRE_INCIDENTS_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    items = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        if not props.get("IsActive"):
            continue
        county_name = (props.get("County") or "").strip()
        if county_name.upper() not in config["county_match"]:
            continue
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        acres = props.get("AcresBurned")
        pct = props.get("PercentContained")
        severity_bits = []
        if isinstance(acres, (int, float)):
            severity_bits.append(f"{acres:,.0f} ac")
        if isinstance(pct, (int, float)):
            severity_bits.append(f"{pct:.0f}% contained")
        items.append({
            "source": "calfire_incidents",
            "external_id": props.get("UniqueId") or "",
            "category": "wildfire",
            "title": (props.get("Name") or "Wildfire").strip(),
            "description": props.get("Location"),
            "severity": ", ".join(severity_bits) or None,
            "county": county_name,
            "lat": lat, "lon": lon,
            "geometry": None,
            "occurred_at": _parse_date(props.get("Started")),
            "url": props.get("Url"),
        })
    return items


async def fetch_nws_alerts(county: str) -> list[dict]:
    """Active alerts for this county's UGC code, restricted to
    NWS_SIGNIFICANT_SEVERITIES -- drops routine Minor/Unknown-severity
    advisories. The only source of the four with real polygon geometry
    per item (affectedZones' own shape isn't fetched separately; the
    alert feature's own geometry, when present, covers it -- some
    county-wide alerts carry null geometry, meaning "the whole zone",
    left as None here rather than guessed at)."""
    config = COUNTY_CONFIG[county]
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(NWS_ALERTS_URL.format(ugc=config["nws_ugc"]), headers=_NWS_HEADERS)
        resp.raise_for_status()
        data = resp.json()

    items = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        severity = props.get("severity")
        if severity not in NWS_SIGNIFICANT_SEVERITIES:
            continue
        items.append({
            "source": "nws_alerts",
            "external_id": feature.get("id") or props.get("id") or "",
            "category": "weather_alert",
            "title": props.get("headline") or props.get("event") or "Weather Alert",
            "description": props.get("description"),
            "severity": severity,
            "county": None,
            "lat": None, "lon": None,
            "geometry": feature.get("geometry"),
            "occurred_at": _parse_date(props.get("sent")),
            "url": props.get("@id") or feature.get("id"),
        })
    return items


async def fetch_caloes_power_outages(county: str) -> list[dict]:
    """Active outages whose own County field matches this county's
    configured aliases, at or above MIN_OUTAGE_CUSTOMERS -- drops
    single-meter blips that show up as their own "incident" row but
    aren't worth a net-control Incident."""
    config = COUNTY_CONFIG[county]
    params = {"where": "OutageStatus='Active'", "outFields": "*", "f": "geojson", "returnGeometry": "true"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(CALOES_POWER_OUTAGES_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    items = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        county_name = (props.get("County") or "").strip()
        if county_name.upper() not in config["county_match"]:
            continue
        customers = props.get("ImpactedCustomers")
        if not isinstance(customers, (int, float)) or customers < MIN_OUTAGE_CUSTOMERS:
            continue
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        utility = (props.get("UtilityCompany") or "").strip()
        items.append({
            "source": "caloes_power_outages",
            "external_id": props.get("IncidentId") or str(props.get("OBJECTID") or ""),
            "category": "power_outage",
            "title": f"Power Outage — {customers:,.0f} customers" + (f" ({utility})" if utility else ""),
            "description": props.get("Cause"),
            "severity": f"{customers:,.0f} customers",
            "county": county_name,
            "lat": lat, "lon": lon,
            "geometry": None,
            "occurred_at": _parse_date(props.get("StartDate")),
            "url": None,
        })
    return items


# Registry -- add a new source by writing one fetch_*(county) function
# above and one entry here, same convention as evac_zone_sources.SOURCES.
SOURCES = {
    "usgs_earthquakes": fetch_usgs_earthquakes,
    "calfire_incidents": fetch_calfire_incidents,
    "nws_alerts": fetch_nws_alerts,
    "caloes_power_outages": fetch_caloes_power_outages,
}


def _item_shape(item: dict):
    """The shapely geometry to test a feed item against zone boundaries
    with -- its own polygon when the source gave one (NWS), else a point
    from lat/lon, else None (nothing to match)."""
    if item.get("geometry"):
        try:
            return shape(item["geometry"])
        except Exception:
            return None
    if item.get("lat") is not None and item.get("lon") is not None:
        return Point(item["lon"], item["lat"])
    return None


async def list_feed_items_for_net(net: Net, db: AsyncSession) -> dict:
    """The DB-touching orchestrator, peer to sync_net_evac_zones() --
    resolves this net's county config, fetches all four sources
    CONCURRENTLY (deliberately different from sync_net_evac_zones()'s
    fail-hard-on-any-error: zone boundary correctness is load-bearing for
    check-ins, this is a convenience list, so one source being down
    shouldn't blank the other three), computes each item's
    suggested_zone_ids against the net's own already-synced
    EvacZoneBoundary rows, and drops anything already dismissed/turned
    into an Incident. Returns {"items": [...], "sources_failed": [...],
    "county": "SONOMA" | None}. An unconfigured region returns an empty
    result with county=None, not an error -- same tone as
    evac_zone_sources.UnsupportedSourceError, just not fatal here since
    the caller (routers/incident_feed.py) needs to render a normal page
    either way."""
    county = _strip_county_suffix(net.region) if net.region else None
    if county not in COUNTY_CONFIG:
        return {"items": [], "sources_failed": [], "county": None}

    results = await asyncio.gather(
        *(fetch(county) for fetch in SOURCES.values()), return_exceptions=True,
    )

    items: list[dict] = []
    sources_failed: list[str] = []
    for source_name, result in zip(SOURCES.keys(), results):
        if isinstance(result, Exception):
            sources_failed.append(source_name)
        else:
            items.extend(result)

    # Already-handled items (created into an Incident, or explicitly
    # dismissed as not relevant) never reappear.
    handled = set((await db.execute(
        select(IncidentFeedDismissal.source, IncidentFeedDismissal.external_id)
        .filter(IncidentFeedDismissal.net_id == net.id)
    )).all())
    items = [i for i in items if (i["source"], i["external_id"]) not in handled]

    # suggested_zone_ids: point/polygon-in-polygon against this net's own
    # synced boundaries, same shapely technique incident_matching.py
    # already uses for station position matching. Boundary shapes are
    # parsed once and reused across every item, not once per item.
    boundaries = (await db.execute(
        select(EvacZoneBoundary).filter(EvacZoneBoundary.net_id == net.id)
    )).scalars().all()
    boundary_shapes = []
    for b in boundaries:
        try:
            boundary_shapes.append((b.id, shape(b.geometry)))
        except Exception:
            continue

    for item in items:
        item_shape = _item_shape(item) if boundary_shapes else None
        item["suggested_zone_ids"] = (
            [bid for bid, bshape in boundary_shapes if item_shape.intersects(bshape)]
            if item_shape is not None else []
        )

    items.sort(key=lambda i: i["occurred_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {"items": items, "sources_failed": sources_failed, "county": county}
