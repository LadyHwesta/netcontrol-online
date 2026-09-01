"""
Tactical Positions — ARES/ACES activation mode (issue #21), plus the Net
Control rotation schedule (issue #21 follow-up).

Not a reusable net-level template: different activations commonly need an
entirely different tactical roster. A live position/shift only ever acts on
a session explicitly started as an activation (NetSession.is_activation) —
a routine session on an ARES net is rejected the same as a non-ARES net, so
"is_ares" alone never turns this on.

Pre-activation planning (issue follow-up): a position/shift can also be
created ahead of time, before any session exists, via the /nets/{id}/planned-*
endpoints below — these just set net_id with session_id left NULL. The
moment the net's next activation session is started, start_session() (in
routers/sessions.py) attaches every such row to it by filling in session_id,
at which point it's indistinguishable from one created live. Every endpoint
below that takes a position_id/shift_id (get/update/delete/sign-on/off)
works the same whether the row is planned or already attached to a session
— only the two list/create endpoints are split by which state they act on,
since "list this net's still-unattached plan" and "list this session's live
roster" are genuinely different queries.

Signing on creates a brand-new Checkin row every time rather than reusing
add_checkin() — that endpoint blocks a second checkin for the same
callsign on ham nets, which would wrongly stop an operator holding two
positions, or re-signing onto one later in the same activation. Each
sign-on IS a shift-history entry; nothing extra to store for that.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Checkin, Net, NetControlShift, NetSession, TacticalPosition, User, utcnow
from routers.deps import get_current_user
from routers.helpers import _get_net_for_user, _get_session_for_user
from routers.schemas import CheckinOut

router = APIRouter()


class TacticalPositionCreate(BaseModel):
    tactical_callsign: str
    location: Optional[str] = None
    assigned_callsign: Optional[str] = None   # planned/expected operator (optional)
    assigned_name: Optional[str] = None
    scheduled_start: Optional[datetime] = None   # planned shift sign-on time (optional)

    @field_validator("tactical_callsign")
    @classmethod
    def tactical_callsign_upper(cls, v):
        v = v.strip().upper()
        if not v:
            raise ValueError("tactical_callsign is required")
        return v


class TacticalPositionOut(BaseModel):
    id: int
    net_id: int
    session_id: Optional[int] = None   # None = planned, not yet attached to a session
    tactical_callsign: str
    location: Optional[str]
    assigned_callsign: Optional[str]
    assigned_name: Optional[str]
    scheduled_start: Optional[datetime] = None
    # Auto-created, one per activation session — Net Control tracked the same way as any
    # other tactical position (sign-on/off, shift history), not user-creatable or removable.
    is_net_control: bool = False
    created_at: datetime
    # Derived from the checkin (if any) currently holding this position —
    # tactical_position_id set, signed_off_at still null.
    current_checkin_id: Optional[int] = None
    current_callsign: Optional[str] = None
    current_name: Optional[str] = None
    signed_on_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TacticalPositionUpdate(BaseModel):
    """Edit a position's plan -- location, planned operator, scheduled sign-on time.
    tactical_callsign and is_net_control are identity, not plan, and aren't editable
    here. Full-replace semantics (like TacticalPositionCreate): every field is sent
    on every save, so an empty value clears it rather than leaving it untouched."""
    location: Optional[str] = None
    assigned_callsign: Optional[str] = None
    assigned_name: Optional[str] = None
    scheduled_start: Optional[datetime] = None


class TacticalSignOn(BaseModel):
    callsign: str
    name: Optional[str] = None

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        v = v.strip().upper()
        if not v:
            raise ValueError("callsign is required")
        return v


class NetControlShiftCreate(BaseModel):
    callsign: str
    name: Optional[str] = None
    scheduled_start: datetime

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        v = v.strip().upper()
        if not v:
            raise ValueError("callsign is required")
        return v


class NetControlShiftOut(BaseModel):
    id: int
    net_id: int
    session_id: Optional[int] = None   # None = planned, not yet attached to a session
    callsign: str
    name: Optional[str]
    scheduled_start: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


async def _get_activation_session(session_id: int, user: User, db: AsyncSession) -> NetSession:
    """Fetch a session, requiring net access and that it's an activation."""
    session = await _get_session_for_user(session_id, user, db)
    net = (await db.execute(select(Net).filter(Net.id == session.net_id))).scalar_one_or_none()
    if not net or not net.is_ares:
        raise HTTPException(400, "Tactical positions require an ARES/ACES net")
    if not session.is_activation:
        raise HTTPException(400, "This session is not marked as an activation")
    return session


async def _get_activation_net(net_id: int, user: User, db: AsyncSession) -> Net:
    """Fetch a net for pre-activation planning (issue follow-up) — the tactical
    roster / NC rotation queued up before the next activation session exists.
    Same access level as live tactical-position management above (plain net
    access, not edit-rights specifically — planning ahead is as much a normal
    operator task as filling positions in once the net is live), just without
    requiring a session to already exist."""
    net = await _get_net_for_user(net_id, user, db)
    if not net.is_ares:
        raise HTTPException(400, "Tactical positions require an ARES/ACES net")
    return net


async def _get_position_for_user(position_id: int, user: User, db: AsyncSession) -> TacticalPosition:
    position = (await db.execute(select(TacticalPosition).filter(TacticalPosition.id == position_id))).scalar_one_or_none()
    if not position:
        raise HTTPException(404, "Tactical position not found")
    if position.session_id is not None:
        await _get_session_for_user(position.session_id, user, db)  # raises 403/404 if no access
    else:
        await _get_net_for_user(position.net_id, user, db)  # planned, not yet attached to a session
    return position


async def _current_occupant(position_id: int, db: AsyncSession) -> Optional[Checkin]:
    # Take-the-first (.limit(1) + .scalars().first()), not scalar_one_or_none() --
    # normal app flow keeps this to one row (sign-on always closes the prior
    # occupant first), but two operators racing to sign on to the same vacant
    # position concurrently could still momentarily produce two open rows; the
    # .order_by() here already signals "pick the latest deterministically"
    # rather than "there must be exactly one," so this should degrade
    # gracefully instead of 500ing (see issue found on _net_history's
    # equivalent last-session lookup).
    return (
        (await db.execute(
            select(Checkin)
            .filter(Checkin.tactical_position_id == position_id, Checkin.signed_off_at.is_(None))
            .order_by(Checkin.checked_in_at.desc())
            .limit(1)
        )).scalars().first()
    )


async def _position_to_out(position: TacticalPosition, db: AsyncSession) -> TacticalPositionOut:
    out = TacticalPositionOut.model_validate(position)
    current = await _current_occupant(position.id, db)
    if current:
        out.current_checkin_id = current.id
        out.current_callsign = current.callsign
        out.current_name = current.name
        out.signed_on_at = current.checked_in_at
    return out


@router.get("/sessions/{session_id}/tactical-positions", response_model=list[TacticalPositionOut])
async def list_tactical_positions(session_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_activation_session(session_id, current_user, db)
    positions = (
        (await db.execute(select(TacticalPosition).filter(TacticalPosition.session_id == session.id).order_by(TacticalPosition.is_net_control.desc(), TacticalPosition.created_at))).scalars().all()
    )
    return [await _position_to_out(p, db) for p in positions]


@router.post("/sessions/{session_id}/tactical-positions", response_model=TacticalPositionOut, status_code=201)
async def create_tactical_position(session_id: int, data: TacticalPositionCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_activation_session(session_id, current_user, db)
    position = TacticalPosition(
        net_id=session.net_id,
        session_id=session.id,
        tactical_callsign=data.tactical_callsign,
        location=(data.location or "").strip() or None,
        assigned_callsign=(data.assigned_callsign or "").strip().upper() or None,
        assigned_name=(data.assigned_name or "").strip() or None,
        scheduled_start=data.scheduled_start,
    )
    db.add(position)
    await db.commit()
    await db.refresh(position)
    return await _position_to_out(position, db)


@router.get("/nets/{net_id}/planned-tactical-positions", response_model=list[TacticalPositionOut])
async def list_planned_tactical_positions(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The tactical roster queued up for this net's *next* activation session —
    not attached to any session yet (issue follow-up)."""
    net = await _get_activation_net(net_id, current_user, db)
    positions = (
        (await db.execute(select(TacticalPosition).filter(
            TacticalPosition.net_id == net.id, TacticalPosition.session_id.is_(None),
        ).order_by(TacticalPosition.created_at))).scalars().all()
    )
    return [await _position_to_out(p, db) for p in positions]


@router.post("/nets/{net_id}/planned-tactical-positions", response_model=TacticalPositionOut, status_code=201)
async def create_planned_tactical_position(net_id: int, data: TacticalPositionCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_activation_net(net_id, current_user, db)
    position = TacticalPosition(
        net_id=net.id,
        session_id=None,
        tactical_callsign=data.tactical_callsign,
        location=(data.location or "").strip() or None,
        assigned_callsign=(data.assigned_callsign or "").strip().upper() or None,
        assigned_name=(data.assigned_name or "").strip() or None,
        scheduled_start=data.scheduled_start,
    )
    db.add(position)
    await db.commit()
    await db.refresh(position)
    return await _position_to_out(position, db)


@router.patch("/tactical-positions/{position_id}", response_model=TacticalPositionOut)
async def update_tactical_position(position_id: int, data: TacticalPositionUpdate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Edit a position's plan (location, planned operator, scheduled sign-on). This is
    the only way to plan ahead for Net Control specifically -- it's auto-created at
    session start with no creation form of its own, so without this there'd be no way
    to set who's expected next or when (issue #21 follow-up)."""
    position = await _get_position_for_user(position_id, current_user, db)
    position.location = (data.location or "").strip() or None
    position.assigned_callsign = (data.assigned_callsign or "").strip().upper() or None
    position.assigned_name = (data.assigned_name or "").strip() or None
    position.scheduled_start = data.scheduled_start
    await db.commit()
    await db.refresh(position)
    return await _position_to_out(position, db)


@router.delete("/tactical-positions/{position_id}", status_code=204)
async def delete_tactical_position(position_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    position = await _get_position_for_user(position_id, current_user, db)
    if position.is_net_control:
        raise HTTPException(400, "Cannot remove the Net Control position — hand it off instead")
    await db.delete(position)  # checkins keep their history; tactical_position_id -> NULL via ON DELETE SET NULL
    await db.commit()


@router.get("/tactical-positions/{position_id}/shifts", response_model=list[CheckinOut])
async def list_tactical_shifts(position_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    position = await _get_position_for_user(position_id, current_user, db)
    shifts = (
        (await db.execute(select(Checkin).filter(Checkin.tactical_position_id == position.id).order_by(Checkin.checked_in_at))).scalars().all()
    )
    out = [CheckinOut.model_validate(c) for c in shifts]
    for o in out:
        o.tactical_callsign = position.tactical_callsign
    return out


@router.post("/tactical-positions/{position_id}/sign-on", response_model=CheckinOut, status_code=201)
async def sign_on_tactical_position(position_id: int, data: TacticalSignOn, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    position = await _get_position_for_user(position_id, current_user, db)
    if position.session_id is None:
        raise HTTPException(400, "This position is only planned so far — start the activation session first")
    session = (await db.execute(select(NetSession).filter(NetSession.id == position.session_id))).scalar_one_or_none()
    if session.ended_at is not None:
        raise HTTPException(400, "Cannot sign on to a position on an ended session")

    outgoing = await _current_occupant(position.id, db)
    if outgoing:
        outgoing.signed_off_at = utcnow()

    checkin = Checkin(
        session_id=session.id,
        callsign=data.callsign,
        name=(data.name or "").strip() or None,
        has_traffic=False,
        tactical_position_id=position.id,
    )
    db.add(checkin)
    await db.commit()
    await db.refresh(checkin)
    out = CheckinOut.model_validate(checkin)
    out.tactical_callsign = position.tactical_callsign
    return out


@router.post("/tactical-positions/{position_id}/sign-off", response_model=CheckinOut)
async def sign_off_tactical_position(position_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    position = await _get_position_for_user(position_id, current_user, db)
    outgoing = await _current_occupant(position.id, db)
    if not outgoing:
        raise HTTPException(404, "This position is not currently occupied")
    outgoing.signed_off_at = utcnow()
    await db.commit()
    await db.refresh(outgoing)
    out = CheckinOut.model_validate(outgoing)
    out.tactical_callsign = position.tactical_callsign
    return out


async def _get_shift_for_user(shift_id: int, user: User, db: AsyncSession) -> NetControlShift:
    shift = (await db.execute(select(NetControlShift).filter(NetControlShift.id == shift_id))).scalar_one_or_none()
    if not shift:
        raise HTTPException(404, "Shift not found")
    if shift.session_id is not None:
        await _get_session_for_user(shift.session_id, user, db)  # raises 403/404 if no access
    else:
        await _get_net_for_user(shift.net_id, user, db)  # planned, not yet attached to a session
    return shift


@router.get("/sessions/{session_id}/net-control-shifts", response_model=list[NetControlShiftOut])
async def list_net_control_shifts(session_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_activation_session(session_id, current_user, db)
    shifts = (
        (await db.execute(select(NetControlShift).filter(NetControlShift.session_id == session.id).order_by(NetControlShift.scheduled_start))).scalars().all()
    )
    return [NetControlShiftOut.model_validate(s) for s in shifts]


@router.post("/sessions/{session_id}/net-control-shifts", response_model=NetControlShiftOut, status_code=201)
async def create_net_control_shift(session_id: int, data: NetControlShiftCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_activation_session(session_id, current_user, db)
    shift = NetControlShift(
        net_id=session.net_id,
        session_id=session.id,
        callsign=data.callsign,
        name=(data.name or "").strip() or None,
        scheduled_start=data.scheduled_start,
    )
    db.add(shift)
    await db.commit()
    await db.refresh(shift)
    return NetControlShiftOut.model_validate(shift)


@router.get("/nets/{net_id}/planned-net-control-shifts", response_model=list[NetControlShiftOut])
async def list_planned_net_control_shifts(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The Net Control rotation queued up for this net's *next* activation
    session — not attached to any session yet (issue follow-up)."""
    net = await _get_activation_net(net_id, current_user, db)
    shifts = (
        (await db.execute(select(NetControlShift).filter(
            NetControlShift.net_id == net.id, NetControlShift.session_id.is_(None),
        ).order_by(NetControlShift.scheduled_start))).scalars().all()
    )
    return [NetControlShiftOut.model_validate(s) for s in shifts]


@router.post("/nets/{net_id}/planned-net-control-shifts", response_model=NetControlShiftOut, status_code=201)
async def create_planned_net_control_shift(net_id: int, data: NetControlShiftCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_activation_net(net_id, current_user, db)
    shift = NetControlShift(
        net_id=net.id,
        session_id=None,
        callsign=data.callsign,
        name=(data.name or "").strip() or None,
        scheduled_start=data.scheduled_start,
    )
    db.add(shift)
    await db.commit()
    await db.refresh(shift)
    return NetControlShiftOut.model_validate(shift)


@router.delete("/net-control-shifts/{shift_id}", status_code=204)
async def delete_net_control_shift(shift_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    shift = await _get_shift_for_user(shift_id, current_user, db)
    await db.delete(shift)
    await db.commit()
