"""
"My Assignments" — the self-service view for the Tactical Operator and
Broadcaster roles (role revamp, issue follow-up). A Net Control Op keeps
using the normal My Nets flow (index.html) unchanged, since that role has
identical privileges to the pre-revamp "member" role; this is purely
ADDITIONAL, for the two new minimal roles, and shows both in one page since
a user can hold either or both at once (see assignments.html).

Two read endpoints power the page's two sections; the actual sign-on/off and
schedule-signup/cancel actions reuse routers/tactical.py's and
routers/schedules.py's existing endpoints unchanged (this module never
mutates anything itself).
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Net, NetControlSignup, NetSchedule, NetSession, NetShare, NetShareRole, TacticalPosition, User
from routers.deps import get_current_user
from routers.schedules import DAYS, _next_occurrences
from routers.tactical import TacticalPositionOut, _position_to_out

router = APIRouter()


async def _net_ids_with_role(user: User, db: AsyncSession, role: str) -> list[int]:
    """Net ids in the caller's current org where they hold this specific
    extra role via NetShare/NetShareRole (owned nets don't need this — an
    owner already has every capability through the normal My Nets flow)."""
    shares = (await db.execute(
        select(NetShare.id, NetShare.net_id)
        .join(Net, Net.id == NetShare.net_id)
        .filter(
            Net.org_id == user.current_org_id,
            or_(NetShare.user_id == user.id, NetShare.user_id == None),
        )
    )).all()
    if not shares:
        return []
    share_ids = [s.id for s in shares]
    role_share_ids = set((await db.execute(
        select(NetShareRole.net_share_id).filter(
            NetShareRole.net_share_id.in_(share_ids), NetShareRole.role == role,
        )
    )).scalars().all())
    return sorted({net_id for share_id, net_id in shares if share_id in role_share_ids})


class TacticalNetAssignments(BaseModel):
    net_id: int
    net_name: str
    net_type: str = "ham"
    session_id: Optional[int] = None   # the net's current live activation, if any
    positions: list[TacticalPositionOut] = []
    note: Optional[str] = None   # e.g. "No activation is currently live on this net"


@router.get("/my/tactical-assignments", response_model=list[TacticalNetAssignments])
async def my_tactical_assignments(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Every net (in the caller's current org) they hold the Tactical
    Operator role on, with that net's currently-live activation's tactical
    positions if one is running -- sign-on/off itself is
    POST /tactical-positions/{id}/sign-on|sign-off, unchanged (now
    identity-enforced for a caller without full net access, see
    routers/tactical.py)."""
    net_ids = await _net_ids_with_role(current_user, db, "tactical_operator")
    if not net_ids:
        return []
    nets = (await db.execute(select(Net).filter(Net.id.in_(net_ids)).order_by(Net.name))).scalars().all()
    out = []
    for net in nets:
        session = (await db.execute(
            select(NetSession).filter(
                NetSession.net_id == net.id, NetSession.is_activation == True, NetSession.ended_at.is_(None),
            ).order_by(NetSession.started_at.desc()).limit(1)
        )).scalar_one_or_none()
        if not session:
            out.append(TacticalNetAssignments(net_id=net.id, net_name=net.name, net_type=net.net_type, note="No activation is currently live on this net"))
            continue
        positions = (await db.execute(
            select(TacticalPosition).filter(TacticalPosition.session_id == session.id)
            .order_by(TacticalPosition.is_net_control.desc(), TacticalPosition.created_at)
        )).scalars().all()
        out.append(TacticalNetAssignments(
            net_id=net.id, net_name=net.name, net_type=net.net_type, session_id=session.id,
            positions=[await _position_to_out(p, db) for p in positions],
        ))
    return out


class BroadcasterSlotOut(BaseModel):
    slot_date: date
    day_name: str
    schedule_id: int
    net_id: int
    net_name: str
    signup_id: Optional[int] = None   # set if the broadcaster slot is already claimed
    signup_callsign: Optional[str] = None
    is_mine: bool = False


@router.get("/my/broadcaster-assignments", response_model=list[BroadcasterSlotOut])
async def my_broadcaster_assignments(weeks: int = 8, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Upcoming broadcaster slots across every net (in the caller's current
    org) they hold the Broadcaster role on -- claiming/cancelling one is
    POST /nets/{id}/signups (role="broadcaster") / DELETE /signups/{id},
    unchanged (now reachable by a Broadcaster-role share, see
    routers/schedules.py)."""
    net_ids = await _net_ids_with_role(current_user, db, "broadcaster")
    if not net_ids:
        return []
    nets = {n.id: n for n in (await db.execute(select(Net).filter(Net.id.in_(net_ids)))).scalars().all()}
    out: list[BroadcasterSlotOut] = []
    for net_id, net in nets.items():
        if not net.has_broadcast:
            continue
        schedules = (await db.execute(select(NetSchedule).filter(NetSchedule.net_id == net_id))).scalars().all()
        for sched in schedules:
            for slot_date in _next_occurrences(sched.day_of_week, weeks):
                signups = (await db.execute(select(NetControlSignup).filter(
                    NetControlSignup.schedule_id == sched.id, NetControlSignup.slot_date == slot_date,
                ))).scalars().all()
                bc = next((s for s in signups if s.role in ("broadcaster", "both")), None)
                out.append(BroadcasterSlotOut(
                    slot_date=slot_date, day_name=DAYS[sched.day_of_week], schedule_id=sched.id,
                    net_id=net_id, net_name=net.name,
                    signup_id=bc.id if bc else None,
                    signup_callsign=bc.callsign if bc else None,
                    is_mine=bool(bc and bc.user_id == current_user.id),
                ))
    out.sort(key=lambda s: s.slot_date)
    return out
