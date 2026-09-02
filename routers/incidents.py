"""
Incident reporting (issue #28) — records of an incident that doesn't
require a full net activation, its affected area (one or more of a net's
already-synced real evacuation zone boundaries, issue #27), and a "mini
net" roster of potentially affected stations that can be checked off as
contacted with notes on their situation. See incident_matching.py for how
that roster actually gets populated. The public-facing map/count lives in
routers/public.py's GET /public/incidents instead, since it must be
reachable with no auth at all -- this file is authenticated-only.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import incident_matching
from database import get_db
from models import EvacZoneBoundary, Incident, IncidentStation, IncidentZone, User
from routers.deps import get_current_user
from routers.helpers import _get_editable_net

router = APIRouter()


class IncidentOut(BaseModel):
    id: int
    net_id: int
    title: str
    description: Optional[str] = None
    status: str
    created_by_id: Optional[int] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None
    zone_ids: list[int] = []
    station_count: int = 0

    model_config = {"from_attributes": True}


class IncidentCreate(BaseModel):
    title: str
    description: Optional[str] = None
    evac_zone_boundary_ids: list[int] = []


class IncidentUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    evac_zone_boundary_ids: Optional[list[int]] = None   # when given, fully replaces the current selection


class IncidentScanOut(BaseModel):
    added: int


class IncidentStationOut(BaseModel):
    id: int
    incident_id: int
    callsign: str
    name: Optional[str] = None
    match_reason: str
    status: str
    notes: Optional[str] = None
    last_position_lat: Optional[float] = None
    last_position_lon: Optional[float] = None
    added_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IncidentStationCreate(BaseModel):
    callsign: str
    name: Optional[str] = None


class IncidentStationUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


VALID_STATION_STATUSES = {"not_contacted", "attempted", "contacted", "confirmed_safe", "needs_assistance"}


async def _get_incident_for_editor(incident_id: int, user: User, db: AsyncSession) -> Incident:
    """Load an incident, then delegate to its net's own edit-rights check
    -- same "load child, check parent" shape as routers/helpers.py's
    _get_session_for_user. Incidents have no access control of their own;
    they're always reached through the net they belong to."""
    incident = (await db.execute(select(Incident).filter(Incident.id == incident_id))).scalar_one_or_none()
    if not incident:
        raise HTTPException(404, "Incident not found")
    await _get_editable_net(incident.net_id, user, db)
    return incident


async def _incident_to_out(incident: Incident, db: AsyncSession) -> IncidentOut:
    zone_ids = (await db.execute(
        select(IncidentZone.evac_zone_boundary_id).filter(IncidentZone.incident_id == incident.id)
    )).scalars().all()
    station_count = (await db.execute(
        select(func.count(IncidentStation.id)).filter(IncidentStation.incident_id == incident.id)
    )).scalar()
    out = IncidentOut.model_validate(incident)
    out.zone_ids = list(zone_ids)
    out.station_count = station_count or 0
    return out


async def _set_incident_zones(incident_id: int, evac_zone_boundary_ids: list[int], net_id: int, db: AsyncSession) -> None:
    """Replaces an incident's zone selection outright -- only boundaries
    belonging to the SAME net are honored (silently dropping any others),
    since an incident's affected area only ever draws from its own net's
    synced zones."""
    valid_ids = set((await db.execute(
        select(EvacZoneBoundary.id).filter(
            EvacZoneBoundary.net_id == net_id, EvacZoneBoundary.id.in_(evac_zone_boundary_ids),
        )
    )).scalars().all())
    await db.execute(delete(IncidentZone).filter(IncidentZone.incident_id == incident_id))
    for zone_id in valid_ids:
        db.add(IncidentZone(incident_id=incident_id, evac_zone_boundary_id=zone_id))


@router.post("/nets/{net_id}/incidents", response_model=IncidentOut, status_code=201)
async def create_incident(net_id: int, data: IncidentCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_editable_net(net_id, current_user, db)
    title = data.title.strip()
    if not title:
        raise HTTPException(400, "Incident title is required")
    incident = Incident(net_id=net_id, title=title, description=(data.description or "").strip() or None, created_by_id=current_user.id)
    db.add(incident)
    await db.commit()
    await db.refresh(incident)
    if data.evac_zone_boundary_ids:
        await _set_incident_zones(incident.id, data.evac_zone_boundary_ids, net_id, db)
        await db.commit()
    return await _incident_to_out(incident, db)


@router.get("/nets/{net_id}/incidents", response_model=list[IncidentOut])
async def list_incidents(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_editable_net(net_id, current_user, db)
    incidents = (await db.execute(
        select(Incident).filter(Incident.net_id == net_id).order_by(Incident.created_at.desc())
    )).scalars().all()
    return [await _incident_to_out(i, db) for i in incidents]


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
async def get_incident(incident_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    incident = await _get_incident_for_editor(incident_id, current_user, db)
    return await _incident_to_out(incident, db)


@router.patch("/incidents/{incident_id}", response_model=IncidentOut)
async def update_incident(incident_id: int, data: IncidentUpdate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    incident = await _get_incident_for_editor(incident_id, current_user, db)
    if data.title is not None:
        title = data.title.strip()
        if not title:
            raise HTTPException(400, "Incident title is required")
        incident.title = title
    if data.description is not None:
        incident.description = data.description.strip() or None
    if data.status is not None:
        if data.status not in ("active", "resolved"):
            raise HTTPException(400, "status must be 'active' or 'resolved'")
        incident.status = data.status
        incident.resolved_at = datetime.now(timezone.utc) if data.status == "resolved" else None
    await db.commit()
    if data.evac_zone_boundary_ids is not None:
        await _set_incident_zones(incident.id, data.evac_zone_boundary_ids, incident.net_id, db)
        await db.commit()
    await db.refresh(incident)
    return await _incident_to_out(incident, db)


@router.delete("/incidents/{incident_id}", status_code=204)
async def delete_incident(incident_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    incident = await _get_incident_for_editor(incident_id, current_user, db)
    await db.delete(incident)
    await db.commit()


@router.post("/incidents/{incident_id}/scan", response_model=IncidentScanOut)
async def scan_incident(incident_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    incident = await _get_incident_for_editor(incident_id, current_user, db)
    added = await incident_matching.scan_incident(incident, db)
    return IncidentScanOut(added=added)


@router.get("/incidents/{incident_id}/stations", response_model=list[IncidentStationOut])
async def list_incident_stations(incident_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_incident_for_editor(incident_id, current_user, db)
    return (await db.execute(
        select(IncidentStation).filter(IncidentStation.incident_id == incident_id).order_by(IncidentStation.callsign)
    )).scalars().all()


@router.post("/incidents/{incident_id}/stations", response_model=IncidentStationOut, status_code=201)
async def add_incident_station(incident_id: int, data: IncidentStationCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_incident_for_editor(incident_id, current_user, db)
    callsign = data.callsign.strip().upper()
    if not callsign:
        raise HTTPException(400, "callsign is required")
    existing = (await db.execute(select(IncidentStation).filter(
        IncidentStation.incident_id == incident_id, IncidentStation.callsign == callsign,
    ))).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "That callsign is already on this incident's station list")
    station = IncidentStation(incident_id=incident_id, callsign=callsign, name=(data.name or "").strip() or None, match_reason="manual")
    db.add(station)
    await db.commit()
    await db.refresh(station)
    return station


@router.patch("/incidents/{incident_id}/stations/{station_id}", response_model=IncidentStationOut)
async def update_incident_station(incident_id: int, station_id: int, data: IncidentStationUpdate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_incident_for_editor(incident_id, current_user, db)
    station = (await db.execute(select(IncidentStation).filter(
        IncidentStation.id == station_id, IncidentStation.incident_id == incident_id,
    ))).scalar_one_or_none()
    if not station:
        raise HTTPException(404, "Station not found")
    if data.status is not None:
        if data.status not in VALID_STATION_STATUSES:
            raise HTTPException(400, f"status must be one of {sorted(VALID_STATION_STATUSES)}")
        station.status = data.status
    if data.notes is not None:
        station.notes = data.notes.strip() or None
    station.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(station)
    return station


@router.delete("/incidents/{incident_id}/stations/{station_id}", status_code=204)
async def delete_incident_station(incident_id: int, station_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_incident_for_editor(incident_id, current_user, db)
    station = (await db.execute(select(IncidentStation).filter(
        IncidentStation.id == station_id, IncidentStation.incident_id == incident_id,
    ))).scalar_one_or_none()
    if station:
        await db.delete(station)
        await db.commit()
