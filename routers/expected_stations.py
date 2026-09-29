"""
Expected Stations — callsigns that have checked into a net repeatedly over
a recent window, e.g. for a "who usually shows up" roster.
"""

import re as _re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Checkin, NetSession, User
from routers.deps import get_current_user
from routers.helpers import _get_editable_net, _preferred_names_for_net

router = APIRouter()


class ExpectedStation(BaseModel):
    callsign: str
    name: Optional[str]
    checkin_count: int   # in the requested window
    last_checkin: datetime


def _suffix(cs: str) -> str:
    """Return just the letter suffix after the district digit for sorting."""
    m = _re.search(r'\d([A-Z]+)$', cs.upper())
    return m.group(1) if m else cs


async def _checkin_counts_in_window(net_id: int, weeks: int, db: AsyncSession):
    """Per-callsign checkin count/name/last-checkin for this net within the
    past `weeks` weeks, unfiltered by any threshold -- shared by both the
    qualifying (>= min_checkins) and almost-qualifying (== min_checkins - 1)
    endpoints below so the window/grouping logic stays in one place."""
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    result = await db.execute(
        select(
            Checkin.callsign,
            func.max(Checkin.name).label("name"),
            func.count(Checkin.id).label("cnt"),
            func.max(Checkin.checked_in_at).label("last_checkin"),
        )
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id, Checkin.checked_in_at >= cutoff)
        .group_by(Checkin.callsign)
    )
    return result.all()


@router.get("/nets/{net_id}/expected", response_model=list[ExpectedStation])
async def expected_stations(
    net_id: int,
    weeks: int = Query(4, ge=1, le=52),
    min_checkins: int = Query(2, ge=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return callsigns that checked in >= min_checkins times in the past N weeks for this net."""
    await _get_editable_net(net_id, current_user, db)

    rows = await _checkin_counts_in_window(net_id, weeks, db)
    preferred_names = await _preferred_names_for_net(net_id, db)
    stations = [
        ExpectedStation(
            callsign=r.callsign,
            name=preferred_names.get(r.callsign, r.name),
            checkin_count=r.cnt,
            last_checkin=r.last_checkin,
        )
        for r in rows if r.cnt >= min_checkins
    ]
    stations.sort(key=lambda s: _suffix(s.callsign))
    return stations


@router.get("/nets/{net_id}/expected/almost", response_model=list[ExpectedStation])
async def almost_expected_stations(
    net_id: int,
    weeks: int = Query(4, ge=1, le=52),
    min_checkins: int = Query(2, ge=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stations one check-in short of the Expected Stations threshold: not on
    that list yet, but a single check-in to this net (within the same
    window) is all it would take to put them on it next time. With
    min_checkins <= 1 there's no meaningful "one short" -- everyone with any
    checkin already qualifies -- so that returns an empty list."""
    await _get_editable_net(net_id, current_user, db)

    needed = min_checkins - 1
    if needed <= 0:
        return []

    rows = await _checkin_counts_in_window(net_id, weeks, db)
    preferred_names = await _preferred_names_for_net(net_id, db)
    stations = [
        ExpectedStation(
            callsign=r.callsign,
            name=preferred_names.get(r.callsign, r.name),
            checkin_count=r.cnt,
            last_checkin=r.last_checkin,
        )
        for r in rows if r.cnt == needed
    ]
    stations.sort(key=lambda s: _suffix(s.callsign))
    return stations
