"""
NetControl Online — incident-to-station matching (issue #28)

Figures out which stations are potentially affected by an incident, given
real evacuation zone geometry (issue #27's EvacZoneBoundary) selected as
the incident's affected area. Two complementary signals, since there is
no reliable, persisted, cross-session "where is this station right now"
data anywhere in this app (confirmed while planning this feature):

  - "zone_report": EvacZone (issue #27's free-text per-callsign roster --
    net-scoped, "most recent zone this callsign reported") string-matched
    against the incident's selected zones' name/external_id. The only
    signal that needs no coordinates at all, and the only one that's
    always available for any net with check-in history.
  - "position": real point-in-polygon matching (via shapely) against each
    callsign's most recent Checkin.lat/lon, searched across the WHOLE
    ORG's session history within a recency window -- not just the
    currently-open session, which is all today's only other lat/lon
    consumer (routers/aprs.py) looks at. This is a genuinely broader
    query than anything that existed before, but it's built entirely on
    data that already persists forever in the Checkin table; no new
    position-tracking machinery.

Deliberately NOT matched against: the live, ephemeral APRS cache
(routers/aprs.py's 5-minute-TTL, current-live-session-only cache) --
incidents this feature is for often won't have an active session running
at all, so that data usually wouldn't exist anyway.

A scan is add-only: it never overwrites or removes an IncidentStation row
an operator has already added or edited (status/notes), so re-scanning
(POST /incidents/{id}/scan) is always safe to repeat as new check-ins or
zone reports come in.
"""

from datetime import timedelta
from typing import Optional

from shapely.geometry import Point, shape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Checkin, EvacZone, EvacZoneBoundary, Incident, IncidentStation, IncidentZone, Net, NetSession, utcnow

DEFAULT_RECENCY_DAYS = 14


async def recent_checkin_info_for_org(org_id: int, db: AsyncSession, days: int = DEFAULT_RECENCY_DAYS) -> dict[str, dict]:
    """Most recent Checkin per callsign across every session in this org
    within the last `days` days -- {callsign: {name, lat, lon,
    checked_in_at}}. Kept regardless of whether that checkin has a
    position (lat/lon may be None) -- used both for point-in-polygon
    matching and for best-effort station-name backfill on zone_report-
    only matches. Only the single most recent row per callsign is kept."""
    cutoff = utcnow() - timedelta(days=days)
    rows = (await db.execute(
        select(Checkin.callsign, Checkin.name, Checkin.lat, Checkin.lon, Checkin.checked_in_at)
        .join(NetSession, NetSession.id == Checkin.session_id)
        .join(Net, Net.id == NetSession.net_id)
        .filter(Net.org_id == org_id, Checkin.checked_in_at >= cutoff)
        .order_by(Checkin.checked_in_at.desc())
    )).all()
    latest: dict[str, dict] = {}
    for callsign, name, lat, lon, checked_in_at in rows:
        if callsign not in latest:
            latest[callsign] = {"name": name, "lat": lat, "lon": lon, "checked_in_at": checked_in_at}
    return latest


async def scan_incident(incident: Incident, db: AsyncSession, days: int = DEFAULT_RECENCY_DAYS) -> int:
    """Runs both matching signals against the incident's selected zones
    and add-only-upserts newly-found IncidentStation rows. Returns how
    many were newly added. No zones selected -> 0, no error."""
    zone_rows = (await db.execute(
        select(EvacZoneBoundary)
        .join(IncidentZone, IncidentZone.evac_zone_boundary_id == EvacZoneBoundary.id)
        .filter(IncidentZone.incident_id == incident.id)
    )).scalars().all()
    if not zone_rows:
        return 0

    net = (await db.execute(select(Net).filter(Net.id == incident.net_id))).scalar_one_or_none()
    if not net or not net.org_id:
        return 0

    # Signal 1: zone_report -- case-insensitive, since a hand-edited
    # EvacZone.zone value might not match a boundary's name/external_id
    # casing exactly even though it's clearly the same zone.
    zone_name_set = {z.name.upper() for z in zone_rows if z.name} | {z.external_id.upper() for z in zone_rows}
    zone_reports = (await db.execute(
        select(EvacZone.callsign, EvacZone.zone).filter(
            EvacZone.net_id == incident.net_id,
            func.upper(EvacZone.zone).in_(zone_name_set),
        )
    )).all()

    matched: dict[str, dict] = {}
    for callsign, _zone in zone_reports:
        matched[callsign] = {"match_reason": "zone_report", "lat": None, "lon": None}

    # Signal 2: real point-in-polygon against each callsign's most recent
    # position anywhere in the org, within the recency window.
    checkin_info = await recent_checkin_info_for_org(net.org_id, db, days=days)
    polygons = [shape(z.geometry) for z in zone_rows]
    for callsign, info in checkin_info.items():
        lat, lon = info["lat"], info["lon"]
        if lat is None or lon is None:
            continue
        point = Point(lon, lat)   # GeoJSON coordinate order is (lon, lat)
        if not any(poly.contains(point) for poly in polygons):
            continue
        if callsign in matched:
            matched[callsign]["lat"] = lat
            matched[callsign]["lon"] = lon
        else:
            matched[callsign] = {"match_reason": "position", "lat": lat, "lon": lon}

    if not matched:
        return 0

    existing = set((await db.execute(
        select(IncidentStation.callsign).filter(IncidentStation.incident_id == incident.id)
    )).scalars().all())

    added = 0
    for callsign, info in matched.items():
        if callsign in existing:
            continue
        name: Optional[str] = None
        if callsign in checkin_info:
            name = checkin_info[callsign]["name"]
        db.add(IncidentStation(
            incident_id=incident.id, callsign=callsign, name=name,
            match_reason=info["match_reason"],
            last_position_lat=info["lat"], last_position_lon=info["lon"],
        ))
        added += 1
    await db.commit()
    return added
