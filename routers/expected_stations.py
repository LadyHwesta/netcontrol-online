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

    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)

    rows = (await db.execute(
        select(
            Checkin.callsign,
            func.max(Checkin.name).label("name"),
            func.count(Checkin.id).label("cnt"),
            func.max(Checkin.checked_in_at).label("last_checkin"),
        )
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id, Checkin.checked_in_at >= cutoff)
        .group_by(Checkin.callsign)
        .having(func.count(Checkin.id) >= min_checkins)
        .order_by(func.count(Checkin.id).desc())
    )).all()

    def _suffix(cs: str) -> str:
        """Return just the letter suffix after the district digit for sorting."""
        m = _re.search(r'\d([A-Z]+)$', cs.upper())
        return m.group(1) if m else cs

    preferred_names = await _preferred_names_for_net(net_id, db)
    stations = [
        ExpectedStation(
            callsign=r.callsign,
            name=preferred_names.get(r.callsign, r.name),
            checkin_count=r.cnt,
            last_checkin=r.last_checkin,
        )
        for r in rows
    ]
    stations.sort(key=lambda s: _suffix(s.callsign))
    return stations
