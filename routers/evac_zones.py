"""
Evacuation Zone routes (ARES/ACES) — per-callsign evac zone tracking for a
net. The checkin traffic-toggle endpoints that used to live under this same
banner in main.py moved to routers/checkins.py, where they actually belong.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import evac_zone_sources
from database import get_db
from models import EvacZone, EvacZoneBoundary, User
from routers.deps import get_current_user
from routers.helpers import _get_editable_net

router = APIRouter()


class EvacZoneOut(BaseModel):
    callsign: str
    zone: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class EvacZoneUpdate(BaseModel):
    zone: str


@router.get("/nets/{net_id}/evac-zones", response_model=list[EvacZoneOut])
async def list_evac_zones(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Return all known evacuation zones for this net, sorted by zone then callsign."""
    await _get_editable_net(net_id, current_user, db)
    return (
        (await db.execute(select(EvacZone).filter(EvacZone.net_id == net_id).order_by(EvacZone.zone, EvacZone.callsign))).scalars().all()
    )


@router.patch("/nets/{net_id}/evac-zones/{callsign}", response_model=EvacZoneOut)
async def update_evac_zone(
    net_id: int,
    callsign: str,
    data: EvacZoneUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually set or update the evac zone for a callsign on this net."""
    await _get_editable_net(net_id, current_user, db)
    callsign = callsign.upper().strip()
    existing = (await db.execute(select(EvacZone).filter(EvacZone.net_id == net_id, EvacZone.callsign == callsign))).scalar_one_or_none()
    if existing:
        existing.zone = data.zone
        existing.updated_at = datetime.now(timezone.utc)
    else:
        existing = EvacZone(net_id=net_id, callsign=callsign, zone=data.zone)
        db.add(existing)
    await db.commit()
    await db.refresh(existing)
    return existing


@router.delete("/nets/{net_id}/evac-zones/{callsign}", status_code=204)
async def delete_evac_zone(
    net_id: int,
    callsign: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a callsign's evac zone record."""
    await _get_editable_net(net_id, current_user, db)
    ez = (await db.execute(select(EvacZone).filter(EvacZone.net_id == net_id, EvacZone.callsign == callsign.upper()))).scalar_one_or_none()
    if ez:
        await db.delete(ez)
        await db.commit()


# ---------------------------------------------------------------------------
# Evacuation zone boundaries synced from an external GIS API (issue #27) --
# the authoritative zone catalog + geometry, distinct from the free-text
# per-callsign roster above.
# ---------------------------------------------------------------------------

class EvacZoneBoundaryOut(BaseModel):
    id: int
    source: str
    external_id: str
    name: Optional[str] = None
    county: Optional[str] = None
    status: Optional[str] = None
    geometry: dict
    source_updated_at: Optional[datetime] = None
    synced_at: datetime

    model_config = {"from_attributes": True}


class EvacZoneSyncOut(BaseModel):
    count: int
    synced_at: datetime


@router.get("/nets/{net_id}/evac-zone-boundaries", response_model=list[EvacZoneBoundaryOut])
async def list_evac_zone_boundaries(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Current synced zone boundaries for this net -- powers the zone map
    and the check-in zone datalist's suggestions."""
    await _get_editable_net(net_id, current_user, db)
    return (
        (await db.execute(select(EvacZoneBoundary).filter(EvacZoneBoundary.net_id == net_id).order_by(EvacZoneBoundary.name))).scalars().all()
    )


@router.post("/nets/{net_id}/evac-zone-sync", response_model=EvacZoneSyncOut)
async def sync_evac_zones(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Live-fetches the current evacuation zone set for this net's
    state/region from an external GIS API and replaces the stored
    boundaries -- deliberately on demand (not cron), see
    evac_zone_sources.py's module docstring for why. Errors surface
    directly to the caller rather than being swallowed, since this is a
    deliberate admin action, not a background side effect."""
    net = await _get_editable_net(net_id, current_user, db)
    if not net.is_ares:
        raise HTTPException(400, "Evacuation zone sync is only available for ARES/ACES nets")
    try:
        count = await evac_zone_sources.sync_net_evac_zones(net, db)
    except evac_zone_sources.UnsupportedSourceError:
        raise HTTPException(
            400,
            f"No evacuation zone data source is available for state '{net.state or '(not set)'}' "
            f"or region '{net.region or '(not set)'}' yet",
        )
    except Exception as exc:
        raise HTTPException(502, f"Failed to fetch evacuation zone data: {exc}")
    return EvacZoneSyncOut(count=count, synced_at=datetime.now(timezone.utc))
