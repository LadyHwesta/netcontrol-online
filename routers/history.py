"""
History / Stats + CSV Export — checkin counts per callsign across all of a
net's sessions, and CSV exports of session/net checkin logs.
"""

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Checkin, Net, NetSession, User
from routers.deps import get_current_user
from routers.helpers import (
    _get_editable_net, _get_net_for_user, _get_session_for_user, _preferred_names_for_net,
    _tactical_callsigns_for_net, _tactical_callsigns_for_session,
)

router = APIRouter()


class CallsignHistoryItem(BaseModel):
    callsign: str
    name: Optional[str]
    total_checkins: int
    recent_checkins: int           # checkins in the past 14 days
    recent_4w_checkins: int        # checkins in the past 28 days
    checked_in_last_session: bool  # present in the most recent ended session
    last_checkin: datetime


@router.get("/nets/{net_id}/history", response_model=list[CallsignHistoryItem])
async def net_history(
    net_id: int,
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return checkin counts per callsign across all sessions of a net.
    Also includes recent_checkins: count of checkins in the past 14 days.
    """
    await _get_editable_net(net_id, current_user, db)

    rows = (await db.execute(
        select(
            Checkin.callsign,
            func.max(Checkin.name).label("name"),
            func.count(Checkin.id).label("total_checkins"),
            func.max(Checkin.checked_in_at).label("last_checkin"),
        )
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id)
        .group_by(Checkin.callsign)
        .order_by(func.count(Checkin.id).desc())
        .limit(limit)
    )).all()

    now = datetime.now(timezone.utc)

    # Recent 14-day counts
    cutoff_2w = now - timedelta(days=14)
    recent_2w = {
        r.callsign: r.cnt
        for r in (await db.execute(
            select(Checkin.callsign, func.count(Checkin.id).label("cnt"))
            .join(NetSession, NetSession.id == Checkin.session_id)
            .filter(NetSession.net_id == net_id, Checkin.checked_in_at >= cutoff_2w)
            .group_by(Checkin.callsign)
        )).all()
    }

    # Recent 28-day counts
    cutoff_4w = now - timedelta(days=28)
    recent_4w = {
        r.callsign: r.cnt
        for r in (await db.execute(
            select(Checkin.callsign, func.count(Checkin.id).label("cnt"))
            .join(NetSession, NetSession.id == Checkin.session_id)
            .filter(NetSession.net_id == net_id, Checkin.checked_in_at >= cutoff_4w)
            .group_by(Checkin.callsign)
        )).all()
    }

    # Who checked in to the most recent ended session? Deliberately take-the-
    # first (.limit(1) + .scalars().first()), not scalar_one_or_none() -- any
    # net used across more than one session has multiple rows matching this
    # filter by design, so scalar_one_or_none() would raise MultipleResultsFound
    # (found live: a real net with session history 500'd on this exact line).
    last_session = (
        (await db.execute(
            select(NetSession)
            .filter(NetSession.net_id == net_id, NetSession.ended_at.isnot(None))
            .order_by(NetSession.started_at.desc())
            .limit(1)
        )).scalars().first()
    )
    last_session_callsigns: set = set()
    if last_session:
        last_session_callsigns = {
            c.callsign for c in
            (await db.execute(select(Checkin).filter(Checkin.session_id == last_session.id))).scalars().all()
        }

    preferred_names = await _preferred_names_for_net(net_id, db)
    return [
        CallsignHistoryItem(
            callsign=r.callsign,
            name=preferred_names.get(r.callsign, r.name),
            total_checkins=r.total_checkins,
            recent_checkins=recent_2w.get(r.callsign, 0),
            recent_4w_checkins=recent_4w.get(r.callsign, 0),
            checked_in_last_session=(r.callsign in last_session_callsigns),
            last_checkin=r.last_checkin,
        )
        for r in rows
    ]


@router.get("/sessions/{session_id}/export")
async def export_session_csv(session_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_session_for_user(session_id, current_user, db)
    checkins = (await db.execute(select(Checkin).filter(Checkin.session_id == session_id).order_by(Checkin.checked_in_at))).scalars().all()
    net = (await db.execute(select(Net).filter(Net.id == session.net_id))).scalar_one_or_none()
    preferred_names = await _preferred_names_for_net(session.net_id, db)
    tactical_callsigns = await _tactical_callsigns_for_session(session_id, db) if session.is_activation else {}

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["#", "Callsign", "Name", "Signal Report", "Comments", "Checked In At"]
    if session.is_activation:
        header.insert(1, "Tactical Callsign")
        header.append("Signed Off At")
    writer.writerow(header)
    for i, c in enumerate(checkins, start=1):
        display_name = preferred_names.get(c.callsign, c.name)
        row = [i, c.callsign, display_name or "", c.signal_report or "", c.comments or "", c.checked_in_at.isoformat()]
        if session.is_activation:
            row.insert(1, tactical_callsigns.get(c.tactical_position_id, "") or "")
            row.append(c.signed_off_at.isoformat() if c.signed_off_at else "")
        writer.writerow(row)

    filename = f"session_{session_id}_{net.name.replace(' ', '_')}.csv"
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/nets/{net_id}/export")
async def export_net_csv(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_net_for_user(net_id, current_user, db)
    preferred_names = await _preferred_names_for_net(net_id, db)
    tactical_callsigns = await _tactical_callsigns_for_net(net_id, db) if net.is_ares else {}

    rows = (await db.execute(
        select(Checkin, NetSession)
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id)
        .order_by(NetSession.started_at.desc(), Checkin.checked_in_at)
    )).all()

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["Session ID", "Session Started", "Callsign", "Name", "Signal Report", "Comments", "Checked In At"]
    if net.is_ares:
        header.insert(3, "Tactical Callsign")
    writer.writerow(header)
    for checkin, session in rows:
        display_name = preferred_names.get(checkin.callsign, checkin.name)
        row = [
            session.id,
            session.started_at.isoformat(),
            checkin.callsign,
            display_name or "",
            checkin.signal_report or "",
            checkin.comments or "",
            checkin.checked_in_at.isoformat(),
        ]
        if net.is_ares:
            row.insert(3, tactical_callsigns.get(checkin.tactical_position_id, "") or "")
        writer.writerow(row)

    filename = f"net_{net_id}_{net.name.replace(' ', '_')}_all_sessions.csv"
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
