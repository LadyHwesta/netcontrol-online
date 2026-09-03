"""
Net Schedules + Net Control Signups (merged — Net Control Signups reuses
SignupCreate/SignupOut/DAYS/_signup_to_out, all defined here, so keeping
them in one file avoids a cross-router schema/helper dependency).

_duty_labels_for_session and _schedule_to_out are also imported by
routers/sessions.py and routers/public.py.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Net, NetControlSignup, NetSchedule, NetSession, OrganizationMembership, User
from routers import helpers
from routers.deps import get_current_user
from routers.helpers import _get_editable_net, _get_net_for_role, _get_net_for_user

router = APIRouter()

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ScheduleCreate(BaseModel):
    day_of_week: int        # 0=Monday … 6=Sunday
    start_time: str         # "HH:MM"
    timezone: str = "UTC"
    notes: Optional[str] = None

    @field_validator("day_of_week")
    @classmethod
    def valid_day(cls, v):
        if not 0 <= v <= 6:
            raise ValueError("day_of_week must be 0 (Monday) through 6 (Sunday)")
        return v

    @field_validator("start_time")
    @classmethod
    def valid_time(cls, v):
        if not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError("start_time must be HH:MM")
        return v


class ScheduleOut(BaseModel):
    id: int
    net_id: int
    day_of_week: int
    day_name: str
    start_time: str
    timezone: str
    notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class SignupCreate(BaseModel):
    schedule_id: int
    slot_date: date
    role: str = "net_control"   # 'net_control' | 'broadcaster' | 'both'
    # Self sign-up: provide callsign directly.
    # Assignment: provide assigned_user_id and callsign/name/email are pulled from that user.
    callsign: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None
    assigned_user_id: Optional[int] = None   # set when net owner assigns another operator

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        if v:
            return v.upper().strip()
        return v

    @field_validator("role")
    @classmethod
    def valid_role(cls, v):
        if v not in ("net_control", "broadcaster", "both"):
            raise ValueError("role must be net_control, broadcaster, or both")
        return v


class SignupOut(BaseModel):
    id: int
    schedule_id: int
    net_id: int
    slot_date: date
    role: str = "net_control"
    callsign: str
    name: Optional[str]
    email: Optional[str]
    phone: Optional[str] = None   # live-looked-up from the signed-up user's own
        # account (issue follow-up), not a snapshot like callsign/name/email --
        # the point is calling their *current* number, so staleness would work
        # against the feature. None once their account is gone (user_id -> NULL).
    notes: Optional[str]
    signed_up_at: datetime
    is_mine: bool = False   # True if current user owns this signup

    model_config = {"from_attributes": True}


class UpcomingSlot(BaseModel):
    slot_date: date
    day_name: str
    schedule_id: int
    signups: list[SignupOut] = []   # empty = fully open


def _next_occurrences(day_of_week: int, weeks: int = 8) -> list[date]:
    """Return the next `weeks` dates (including today if it matches) for a given weekday.
    Uses the UTC calendar date — matching _duty_labels_for_session's use of
    session.started_at.date() and send_reminders.py's now_utc.date() — so that
    signup slot_date, session dates, and reminder-window dates never disagree near
    midnight in timezones behind UTC (server-local date.today() would drift by a day)."""
    today = datetime.now(timezone.utc).date()
    days_ahead = (day_of_week - today.weekday()) % 7
    first = today + timedelta(days=days_ahead)
    return [first + timedelta(weeks=i) for i in range(weeks)]


async def _signup_to_out(s: NetControlSignup, current_user: User, db: AsyncSession) -> SignupOut:
    phone = None
    if s.user_id:
        phone = (await db.execute(select(User.phone).filter(User.id == s.user_id))).scalar()
    return SignupOut(
        id=s.id, schedule_id=s.schedule_id, net_id=s.net_id,
        slot_date=s.slot_date, role=s.role, callsign=s.callsign, name=s.name,
        email=s.email, phone=phone, notes=s.notes, signed_up_at=s.signed_up_at,
        is_mine=(s.user_id == current_user.id),
    )


async def _duty_for_date(net_id: int, slot_date: date, db: AsyncSession) -> tuple:
    """Return (net_control_signup, broadcaster_signup) ORM rows for this net on slot_date,
    across all of its schedules. A signup with role='both' fills both."""
    signups = (
        (await db.execute(select(NetControlSignup).filter(NetControlSignup.net_id == net_id, NetControlSignup.slot_date == slot_date))).scalars().all()
    )
    nc = next((s for s in signups if s.role in ("net_control", "both")), None)
    bc = next((s for s in signups if s.role in ("broadcaster", "both")), None)
    return nc, bc


async def _duty_labels_for_session(net: Net, session: NetSession, db: AsyncSession) -> dict:
    """Net Control / Broadcaster display info for a session, sourced from the schedule
    sign-up matching the session's date when one exists, falling back to whoever
    actually started the session for Net Control. Also includes the sign-up (if any)
    for one week later, so a script can announce next week's duty.

    A manual broadcaster override set at session start (issue #17) takes precedence
    over the schedule sign-up — covers the case where the broadcaster isn't known
    until the net is about to begin. A manual Net Control override (issue #20,
    mainly for offline-entered nets where whoever backfills the log may not be
    who actually ran it) takes the same precedence over the schedule sign-up."""
    session_date = session.started_at.date()
    nc, bc = await _duty_for_date(net.id, session_date, db)
    next_nc, next_bc = await _duty_for_date(net.id, session_date + timedelta(days=7), db)
    operator = (await db.execute(select(User).filter(User.id == session.operator_id))).scalar_one_or_none() if session.operator_id else None
    # On a GMRS net, prefer the operator's separate GMRS callsign (issue #23) over
    # their amateur one, when they have one set — only relevant for the "whoever
    # started the session" fallback; an explicit schedule sign-up's callsign
    # (typed at sign-up time) always wins regardless.
    operator_callsign = None
    if operator:
        operator_callsign = (
            (operator.gmrs_callsign or operator.callsign) if net.net_type == "gmrs" else operator.callsign
        )
    ncs_callsign = session.ncs_override_callsign or (nc.callsign if nc else operator_callsign)
    ncs_name = session.ncs_override_name or (nc.name if nc else (operator.name if operator else None))
    broadcaster_callsign = session.broadcaster_override_callsign or (bc.callsign if bc else None)
    broadcaster_name = session.broadcaster_override_name or (bc.name if bc else None)
    # Profile photo (issue follow-up) -- the frontend just builds
    # <img src="/users/{id}/photo">, so all that's needed here is the user id
    # behind whoever's actually shown above. None whenever a manual text
    # override is in play (session.*_override_callsign) -- there's no
    # account behind free-typed override text to have a photo at all.
    ncs_user_id = None if session.ncs_override_callsign else (nc.user_id if nc else (operator.id if operator else None))
    broadcaster_user_id = None if session.broadcaster_override_callsign else (bc.user_id if bc else None)
    return {
        "ncs_callsign": ncs_callsign,
        "ncs_name": ncs_name,
        "ncs_user_id": ncs_user_id,
        "broadcaster_callsign": broadcaster_callsign,
        "broadcaster_name": broadcaster_name,
        "broadcaster_user_id": broadcaster_user_id,
        "broadcast_label": net.broadcast_label if (net.has_broadcast and broadcaster_callsign) else None,
        "next_ncs_callsign": next_nc.callsign if next_nc else None,
        "next_ncs_name": next_nc.name if next_nc else None,
        "next_broadcaster_callsign": next_bc.callsign if next_bc else None,
        "next_broadcaster_name": next_bc.name if next_bc else None,
    }


def _schedule_to_out(s: NetSchedule) -> ScheduleOut:
    return ScheduleOut(
        id=s.id,
        net_id=s.net_id,
        day_of_week=s.day_of_week,
        day_name=DAYS[s.day_of_week],
        start_time=s.start_time,
        timezone=s.timezone,
        notes=s.notes,
        created_at=s.created_at,
    )


def _build_ics(net: "Net", schedule: "NetSchedule", signup: "NetControlSignup", role_label: str = "Net Control") -> str:
    """Build an iCalendar (ICS) event string for a net control / broadcaster signup."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz_str = schedule.timezone or "UTC"
    try:
        tz = ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
        tz_str = "UTC"

    h, m = map(int, schedule.start_time.split(":"))
    naive_start = datetime(
        signup.slot_date.year, signup.slot_date.month, signup.slot_date.day, h, m
    )
    local_start = naive_start.replace(tzinfo=tz)
    utc_start   = local_start.astimezone(ZoneInfo("UTC"))
    utc_end     = utc_start + timedelta(hours=1)   # default 1-hour block

    dtstamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    dtstart = utc_start.strftime("%Y%m%dT%H%M%SZ")
    dtend   = utc_end.strftime("%Y%m%dT%H%M%SZ")

    uid = f"netcontrol-{signup.id}-{signup.slot_date}@hamnettracker"

    # Build description (escape commas and newlines per RFC 5545)
    desc_parts = [f"You are scheduled as {role_label} for {net.name}."]
    if net.frequency:
        desc_parts.append(f"Frequency: {net.frequency}")
    desc_parts.append(f"Date: {signup.slot_date}")
    desc_parts.append(f"Time: {schedule.start_time} {tz_str}")
    if schedule.notes:
        desc_parts.append(f"Net notes: {schedule.notes}")
    if signup.notes:
        desc_parts.append(f"Your notes: {signup.notes}")
    description = "\\n".join(desc_parts)

    # Organizer — strip display name if present
    organizer_raw = helpers.SMTP_FROM or helpers.SMTP_USER or ""
    m2 = re.search(r"<(.+?)>", organizer_raw)
    organizer_email = m2.group(1) if m2 else organizer_raw

    attendee_name  = signup.name or signup.callsign
    attendee_email = signup.email or ""

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//NetControl Online//Ham Radio//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{net.name} – {role_label}",
        f"DESCRIPTION:{description}",
    ]
    if organizer_email:
        lines.append(f"ORGANIZER:mailto:{organizer_email}")
    if attendee_email:
        lines.append(f"ATTENDEE;CN={attendee_name};RSVP=FALSE:mailto:{attendee_email}")
    lines += ["END:VEVENT", "END:VCALENDAR"]

    return "\r\n".join(lines) + "\r\n"


@router.get("/nets/{net_id}/schedules", response_model=list[ScheduleOut])
async def list_schedules(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Read-only schedule data — any net share suffices (issue follow-up:
    # previously edit rights, which blocked a Tactical Operator/Broadcaster
    # share from even seeing the schedule to find their own slot).
    await _get_net_for_user(net_id, current_user, db)
    schedules = (await db.execute(select(NetSchedule).filter(NetSchedule.net_id == net_id).order_by(NetSchedule.day_of_week))).scalars().all()
    return [_schedule_to_out(s) for s in schedules]


@router.post("/nets/{net_id}/schedules", response_model=ScheduleOut, status_code=201)
async def create_schedule(net_id: int, data: ScheduleCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_editable_net(net_id, current_user, db)
    sched = NetSchedule(
        net_id=net_id,
        day_of_week=data.day_of_week,
        start_time=data.start_time,
        timezone=data.timezone,
        notes=data.notes,
    )
    db.add(sched)
    await db.commit()
    await db.refresh(sched)
    return _schedule_to_out(sched)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    sched = (await db.execute(select(NetSchedule).filter(NetSchedule.id == schedule_id))).scalar_one_or_none()
    if not sched:
        raise HTTPException(404, "Schedule not found")
    await _get_editable_net(sched.net_id, current_user, db)
    await db.delete(sched)
    await db.commit()


@router.get("/nets/{net_id}/upcoming", response_model=list[UpcomingSlot])
async def upcoming_slots(
    net_id: int,
    weeks: int = Query(8, ge=1, le=26),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the next `weeks` scheduled dates across all schedules for a net, with signup info."""
    await _get_net_for_user(net_id, current_user, db)  # read-only — any net share suffices, see list_schedules
    schedules = (await db.execute(select(NetSchedule).filter(NetSchedule.net_id == net_id))).scalars().all()

    # Gather all upcoming dates across all schedules
    slots: list[UpcomingSlot] = []
    for sched in schedules:
        for slot_date in _next_occurrences(sched.day_of_week, weeks):
            signup_rows = (await db.execute(select(NetControlSignup).filter(
                NetControlSignup.schedule_id == sched.id,
                NetControlSignup.slot_date == slot_date,
            ))).scalars().all()
            slots.append(UpcomingSlot(
                slot_date=slot_date,
                day_name=DAYS[sched.day_of_week],
                schedule_id=sched.id,
                signups=[await _signup_to_out(s, current_user, db) for s in signup_rows],
            ))

    # Sort chronologically
    slots.sort(key=lambda s: s.slot_date)
    return slots


@router.post("/nets/{net_id}/signups", response_model=SignupOut, status_code=201)
async def create_signup(net_id: int, data: SignupCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Role revamp (issue follow-up): a Broadcaster-role share (without full
    # edit rights) may self-signup for the broadcaster slot specifically --
    # never assign someone else, never claim net_control/both. Full edit
    # rights (net_control_op) is still the primary gate and keeps every
    # existing capability (assigning others, any role) unchanged.
    broadcaster_self_only = False
    try:
        net = await _get_editable_net(net_id, current_user, db)
    except HTTPException as e:
        if e.status_code != 403:
            raise
        net = await _get_net_for_role(net_id, current_user, db, "broadcaster")
        broadcaster_self_only = True
    if broadcaster_self_only:
        if data.role != "broadcaster":
            raise HTTPException(403, "The broadcaster role can only self-signup for the broadcaster slot")
        if data.assigned_user_id:
            raise HTTPException(403, "Only the net owner can assign other operators")

    # Verify the schedule belongs to this net
    sched = (await db.execute(select(NetSchedule).filter(
        NetSchedule.id == data.schedule_id,
        NetSchedule.net_id == net_id,
    ))).scalar_one_or_none()
    if not sched:
        raise HTTPException(404, "Schedule not found for this net")

    # Verify the slot_date is actually a valid occurrence for this schedule
    if data.slot_date.weekday() != sched.day_of_week:
        raise HTTPException(400, f"That date is not a {DAYS[sched.day_of_week]}")

    if data.role in ("broadcaster", "both") and not net.has_broadcast:
        raise HTTPException(400, "This net does not have a broadcaster role enabled")

    # A 'both' signup occupies the date exclusively; net_control/broadcaster only conflict
    # with the same role or an existing 'both' signup.
    existing_roles = set((await db.execute(select(NetControlSignup.role).filter(
        NetControlSignup.schedule_id == data.schedule_id,
        NetControlSignup.slot_date == data.slot_date,
    ))).scalars().all())
    conflicting = (
        "both" in existing_roles
        or data.role == "both" and existing_roles
        or data.role in existing_roles
    )
    if conflicting:
        raise HTTPException(409, "That date/role is already claimed")

    # Determine who is being signed up
    if data.assigned_user_id:
        # Net owner assigning a registered operator
        if net.owner_id != current_user.id:
            raise HTTPException(403, "Only the net owner can assign other operators")
        assigned = (
            (await db.execute(select(User).join(OrganizationMembership, OrganizationMembership.user_id == User.id).filter(
                User.id == data.assigned_user_id, User.is_active == True,
                OrganizationMembership.org_id == net.org_id, OrganizationMembership.approved == True,
            ))).scalar_one_or_none()
        )
        if not assigned:
            raise HTTPException(404, "Assigned user not found")
        signup_user_id = assigned.id
        signup_callsign = (assigned.gmrs_callsign or assigned.callsign) if net.net_type == "gmrs" else assigned.callsign
        signup_name = assigned.name
        signup_email = assigned.email
    else:
        # Self sign-up
        if not data.callsign:
            raise HTTPException(400, "callsign is required for self sign-up")
        signup_user_id = current_user.id
        signup_callsign = data.callsign
        signup_name = data.name
        signup_email = data.email

    signup = NetControlSignup(
        schedule_id=data.schedule_id,
        net_id=net_id,
        slot_date=data.slot_date,
        role=data.role,
        user_id=signup_user_id,
        callsign=signup_callsign,
        name=signup_name,
        email=signup_email,
        notes=data.notes,
    )
    db.add(signup)
    await db.commit()
    await db.refresh(signup)

    role_label = {
        "net_control": "Net Control",
        "broadcaster": net.broadcast_label or "Broadcaster",
        "both": f"Net Control & {net.broadcast_label or 'Broadcaster'}",
    }[data.role]

    # Send confirmation email with calendar attachment if we have an address
    helpers._email_log.info(
        "Signup created: callsign=%s role=%s email=%r smtp_configured=%s",
        signup_callsign, data.role, signup_email, helpers._smtp_configured(),
    )
    if signup_email:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        day_name = days[sched.day_of_week]
        assigned_by_admin = bool(data.assigned_user_id)
        action = "assigned you as" if assigned_by_admin else "confirmed your sign-up as"
        subject = f"[{net.name}] {role_label} – {signup.slot_date.strftime('%a %b %-d, %Y')}"
        body_html = f"""
<html><body style="font-family:sans-serif;color:#222;max-width:600px">
<h2 style="color:#1a6496">{net.name}</h2>
<p>Hi {signup_name or signup_callsign},</p>
<p>This email {action} <strong>{role_label}</strong> for the following session:</p>
<table style="border-collapse:collapse;margin:16px 0">
  <tr><td style="padding:6px 16px 6px 0;font-weight:bold">Date</td>
      <td style="padding:6px 0">{signup.slot_date.strftime('%A, %B %-d, %Y')}</td></tr>
  <tr><td style="padding:6px 16px 6px 0;font-weight:bold">Time</td>
      <td style="padding:6px 0">{sched.start_time} {sched.timezone}</td></tr>
  {"<tr><td style='padding:6px 16px 6px 0;font-weight:bold'>Frequency</td><td style='padding:6px 0'>" + net.frequency + "</td></tr>" if net.frequency else ""}
  {"<tr><td style='padding:6px 16px 6px 0;font-weight:bold'>Notes</td><td style='padding:6px 0'>" + signup.notes + "</td></tr>" if signup.notes else ""}
</table>
<p>A calendar event is attached — add it to your calendar to set a reminder.</p>
<p style="color:#666;font-size:12px">73 de NetControl Online</p>
</body></html>"""
        body_text = (
            f"{net.name} – {role_label} Confirmation\n\n"
            f"Hi {signup_name or signup_callsign},\n\n"
            f"This email {action} {role_label} for:\n"
            f"  Date:      {signup.slot_date.strftime('%A, %B %-d, %Y')}\n"
            f"  Time:      {sched.start_time} {sched.timezone}\n"
            + (f"  Frequency: {net.frequency}\n" if net.frequency else "")
            + (f"  Notes:     {signup.notes}\n" if signup.notes else "")
            + "\nA calendar event (.ics) is attached.\n\n73 de NetControl Online"
        )
        try:
            ics = _build_ics(net, sched, signup, role_label=role_label)
            helpers.send_email(
                to=[signup_email],
                subject=subject,
                body_html=body_html,
                body_text=body_text,
                ics_content=ics,
                ics_filename=f"netcontrol-{signup.slot_date}.ics",
            )
        except Exception as exc:
            helpers._email_log.warning("Failed to send signup confirmation to %s: %s", signup_email, exc)

    return await _signup_to_out(signup, current_user, db)


@router.delete("/signups/{signup_id}", status_code=204)
async def delete_signup(signup_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    signup = (await db.execute(select(NetControlSignup).filter(NetControlSignup.id == signup_id))).scalar_one_or_none()
    if not signup:
        raise HTTPException(404, "Signup not found")
    # Net owner can delete any signup; operators can only delete their own
    net = (await db.execute(select(Net).filter(Net.id == signup.net_id))).scalar_one_or_none()
    if signup.user_id != current_user.id and (not net or net.owner_id != current_user.id):
        raise HTTPException(403, "Not authorised to remove this signup")
    await db.delete(signup)
    await db.commit()


@router.get("/nets/{net_id}/signups", response_model=list[SignupOut])
async def list_signups(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_net_for_user(net_id, current_user, db)  # read-only — any net share suffices, see list_schedules
    signups = (
        (await db.execute(select(NetControlSignup).filter(NetControlSignup.net_id == net_id).order_by(NetControlSignup.slot_date))).scalars().all()
    )
    return [await _signup_to_out(s, current_user, db) for s in signups]
