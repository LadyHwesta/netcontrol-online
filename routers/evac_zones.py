"""
Evacuation Zone routes (ARES/ACES) — per-callsign evac zone tracking for a
net. The checkin traffic-toggle endpoints that used to live under this same
banner in main.py moved to routers/checkins.py, where they actually belong.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import EvacZone, User
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
