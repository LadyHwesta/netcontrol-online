"""
Session routes + Session summary & ICS-205.
"""

import html
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import net_repository
from database import get_db
from models import Checkin, Net, NetSession, TacticalPosition, TrafficMessage, User
from routers.deps import get_current_user
from routers.helpers import _get_net_for_user, _get_session_for_user, _preferred_names_for_net, _tactical_callsigns_for_session
from routers.schedules import _duty_labels_for_session

router = APIRouter()


class SessionCreate(BaseModel):
    name: Optional[str] = None
    notes: Optional[str] = None
    # Manual broadcaster override for this session — takes precedence over the
    # schedule sign-up for the session's date (issue #17).
    broadcaster_override_callsign: Optional[str] = None
    broadcaster_override_name: Optional[str] = None
    # ARES/ACES activation (issue #21) — forced False server-side unless the net
    # is is_ares. Set once at start; enables tactical positions and the
    # simplified roster for this session only, not every session on the net.
    is_activation: bool = False
    # Offline net entry (issue #20) — backfilling a net that already happened
    # with no access to the web tool. occurred_at is required when is_offline
    # is set; it becomes both started_at and ended_at (no live view, matches
    # the issue). ncs_override_* mirrors broadcaster_override_* above — usually
    # needed here since whoever backfills the log may not be who ran the net.
    is_offline: bool = False
    occurred_at: Optional[datetime] = None
    ncs_override_callsign: Optional[str] = None
    ncs_override_name: Optional[str] = None


class SessionRename(BaseModel):
    name: Optional[str] = None


class SessionOut(BaseModel):
    id: int
    net_id: int
    operator_id: Optional[int]
    name: Optional[str]
    notes: Optional[str]
    started_at: datetime
    ended_at: Optional[datetime]
    is_activation: bool = False
    is_offline: bool = False
    is_offline_locked: bool = False
    checkin_count: int = 0
    # Scheduled duty for this session's date, from the Schedule sign-up if one exists
    # (net control falls back to whoever started the session when no sign-up matches)
    ncs_callsign: Optional[str] = None
    ncs_name: Optional[str] = None
    broadcaster_callsign: Optional[str] = None
    broadcaster_name: Optional[str] = None
    broadcast_label: Optional[str] = None
    # Same, but for the schedule sign-up one week after this session's date (no fallback --
    # there's no operator yet for a session that hasn't started).
    next_ncs_callsign: Optional[str] = None
    next_ncs_name: Optional[str] = None
    next_broadcaster_callsign: Optional[str] = None
    next_broadcaster_name: Optional[str] = None

    model_config = {"from_attributes": True}


class SessionSummary(BaseModel):
    session_id: int
    net_name: str
    started_at: datetime
    ended_at: Optional[datetime]
    duration_minutes: Optional[int]
    total_checkins: int
    traffic_count: int
    new_stations: int      # callsigns appearing for the first time on this net
    operator_callsign: Optional[str]
    net_frequency: Optional[str]


@router.get("/nets/{net_id}/sessions", response_model=list[SessionOut])
async def list_sessions(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_net_for_user(net_id, current_user, db)
    sessions = (
        (await db.execute(select(NetSession).filter(NetSession.net_id == net_id).order_by(NetSession.started_at.desc()))).scalars().all()
    )
    result = []
    for s in sessions:
        count = (await db.execute(select(func.count(Checkin.id)).filter(Checkin.session_id == s.id))).scalar()
        out = SessionOut.model_validate(s)
        out.checkin_count = count
        result.append(out)
    return result


@router.post("/nets/{net_id}/sessions", response_model=SessionOut, status_code=201)
async def start_session(net_id: int, data: SessionCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_net_for_user(net_id, current_user, db)
    if data.is_offline and not data.occurred_at:
        raise HTTPException(400, "occurred_at is required for an offline net entry")
    session = NetSession(
        net_id=net_id, operator_id=current_user.id, name=data.name, notes=data.notes,
        broadcaster_override_callsign=(data.broadcaster_override_callsign or "").strip().upper() or None,
        broadcaster_override_name=(data.broadcaster_override_name or "").strip() or None,
        is_activation=data.is_activation if net.is_ares else False,
        is_offline=data.is_offline,
        ncs_override_callsign=(data.ncs_override_callsign or "").strip().upper() or None,
        ncs_override_name=(data.ncs_override_name or "").strip() or None,
    )
    if data.is_offline:
        session.started_at = data.occurred_at
    db.add(session)
    await db.commit()
    await db.refresh(session)

    if data.is_offline:
        # No live view for a backfilled entry (issue #20) -- put it straight into
        # the "ended" state at the reported timestamp. add_checkin() specifically
        # lets checkins through despite ended_at being set for sessions like this.
        session.ended_at = session.started_at
        await db.commit()
        await db.refresh(session)

    # Auto-create the Net Control tactical position for an activation session, seeded
    # from the same day's-schedule/whoever-started-it resolution routine sessions use,
    # and sign them straight on if known — NCS is live the moment the net starts, and
    # from here on hands off through the same sign-on/off flow as any other position
    # (issue #21 follow-up: routine sessions' single day-level NCS wasn't enough for a
    # multi-hour activation where net control itself rotates).
    if session.is_activation:
        duty = await _duty_labels_for_session(net, session, db)
        nc_position = TacticalPosition(
            session_id=session.id,
            tactical_callsign="NET CONTROL",
            is_net_control=True,
            assigned_callsign=duty["ncs_callsign"],
            assigned_name=duty["ncs_name"],
        )
        db.add(nc_position)
        await db.commit()
        await db.refresh(nc_position)
        if duty["ncs_callsign"]:
            db.add(Checkin(
                session_id=session.id,
                callsign=duty["ncs_callsign"],
                name=duty["ncs_name"],
                has_traffic=False,
                tactical_position_id=nc_position.id,
            ))
            await db.commit()

    out = SessionOut.model_validate(session)
    out.checkin_count = 0
    return out


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_session_for_user(session_id, current_user, db)
    count = (await db.execute(select(func.count(Checkin.id)).filter(Checkin.session_id == session.id))).scalar()
    out = SessionOut.model_validate(session)
    out.checkin_count = count
    net = (await db.execute(select(Net).filter(Net.id == session.net_id))).scalar_one_or_none()
    if net:
        for k, v in (await _duty_labels_for_session(net, session, db)).items():
            setattr(out, k, v)
    return out


@router.patch("/sessions/{session_id}/end", response_model=SessionOut)
async def end_session(session_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_session_for_user(session_id, current_user, db)
    # An offline entry (issue #20) already has ended_at set from creation (it's
    # never live), so that can't also signal "done entering data" for these --
    # is_offline_locked is that separate signal, and is what add_checkin() checks
    # for offline sessions instead of ended_at.
    if session.is_offline:
        if session.is_offline_locked:
            raise HTTPException(400, "This logged net has already been closed")
        session.is_offline_locked = True
    else:
        if session.ended_at is not None:
            raise HTTPException(400, "Session already ended")
        session.ended_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    count = (await db.execute(select(func.count(Checkin.id)).filter(Checkin.session_id == session.id))).scalar()
    net = (await db.execute(select(Net).filter(Net.id == session.net_id))).scalar_one_or_none()
    if net:
        await net_repository.push_session_stats(net, session, count, db)
    out = SessionOut.model_validate(session)
    out.checkin_count = count
    return out


@router.patch("/sessions/{session_id}/rename", response_model=SessionOut)
async def rename_session(session_id: int, data: SessionRename, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_session_for_user(session_id, current_user, db)
    session.name = data.name
    await db.commit()
    await db.refresh(session)
    count = (await db.execute(select(func.count(Checkin.id)).filter(Checkin.session_id == session.id))).scalar()
    out = SessionOut.model_validate(session)
    out.checkin_count = count
    return out


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_session_for_user(session_id, current_user, db)
    await db.delete(session)
    await db.commit()


@router.get("/sessions/{session_id}/summary", response_model=SessionSummary)
async def session_summary(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_for_user(session_id, current_user, db)
    net = (await db.execute(select(Net).filter(Net.id == session.net_id))).scalar_one_or_none()
    checkins = (await db.execute(select(Checkin).filter(Checkin.session_id == session_id))).scalars().all()

    duration_minutes = None
    if session.started_at and session.ended_at:
        delta = session.ended_at - session.started_at
        duration_minutes = int(delta.total_seconds() / 60)

    # New stations: callsigns that appear in this session but not in any prior session for this net
    this_callsigns = {c.callsign for c in checkins}
    prior = (await db.execute(
        select(Checkin.callsign)
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == session.net_id, NetSession.id != session_id)
        .distinct()
    )).all()
    prior_callsigns = {r.callsign for r in prior}
    new_stations = len(this_callsigns - prior_callsigns)

    operator_callsign = None
    if session.operator_id:
        op = (await db.execute(select(User).filter(User.id == session.operator_id))).scalar_one_or_none()
        operator_callsign = op.callsign if op else None

    return SessionSummary(
        session_id=session_id,
        net_name=net.name if net else "Unknown",
        started_at=session.started_at,
        ended_at=session.ended_at,
        duration_minutes=duration_minutes,
        total_checkins=len(checkins),
        traffic_count=sum(1 for c in checkins if c.has_traffic),
        new_stations=new_stations,
        operator_callsign=operator_callsign,
        net_frequency=net.frequency if net else None,
    )


@router.get("/sessions/{session_id}/ics205")
async def session_ics205(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a printable HTML ICS-205 / net log for this session."""
    session = await _get_session_for_user(session_id, current_user, db)
    net = (await db.execute(select(Net).filter(Net.id == session.net_id))).scalar_one_or_none()
    checkins = (await db.execute(select(Checkin).filter(Checkin.session_id == session_id).order_by(Checkin.checked_in_at))).scalars().all()
    traffic_msgs = (await db.execute(select(TrafficMessage).filter(TrafficMessage.session_id == session_id).order_by(TrafficMessage.created_at))).scalars().all()

    op_callsign = ""
    if session.operator_id:
        op = (await db.execute(select(User).filter(User.id == session.operator_id))).scalar_one_or_none()
        op_callsign = op.callsign if op else ""

    started = session.started_at.strftime("%Y-%m-%d %H%MZ") if session.started_at else ""
    ended   = session.ended_at.strftime("%H%MZ") if session.ended_at else "—"
    freq    = net.frequency if net and net.frequency else "—"

    preferred_names = await _preferred_names_for_net(session.net_id, db)
    tactical_callsigns = await _tactical_callsigns_for_session(session_id, db) if session.is_activation else {}

    checkin_rows = ""
    for i, c in enumerate(checkins, 1):
        traffic_flag = " 📢" if c.has_traffic else ""
        display_name = preferred_names.get(c.callsign, c.name)
        zone_cell = f"<td>{html.escape(c.evac_zone or '—')}</td>" if net and net.is_ares else ""
        tactical_cell = (
            f"<td>{html.escape(tactical_callsigns.get(c.tactical_position_id, '') or '—')}</td>"
            if session.is_activation else ""
        )
        checkin_rows += (
            f"<tr><td>{i}</td><td>{c.checked_in_at.strftime('%H%MZ')}</td>"
            f"<td><strong>{html.escape(c.callsign)}</strong></td><td>{html.escape(display_name or '')}</td>"
            f"<td>{html.escape(c.signal_report or '')}</td><td>{html.escape(c.comments or '')}{traffic_flag}</td>"
            f"{zone_cell}{tactical_cell}</tr>\n"
        )

    traffic_rows = ""
    for i, m in enumerate(traffic_msgs, 1):
        traffic_rows += (
            f"<tr><td>{i}</td><td>{html.escape(m.msg_number or '—')}</td>"
            f"<td>{html.escape(m.origin_callsign)}</td><td>{html.escape(m.dest_info or '—')}</td>"
            f"<td>{html.escape(m.msg_type.replace('_',' ').title())}</td>"
            f"<td>{html.escape(m.status.title())}</td><td>{html.escape(m.notes or '')}</td></tr>\n"
        )

    zone_th = "<th>Zone</th>" if net and net.is_ares else ""
    tactical_th = "<th>Tactical</th>" if session.is_activation else ""
    traffic_section = ""
    if traffic_msgs:
        traffic_section = f"""
        <h3>Traffic Log ({len(traffic_msgs)} message{'s' if len(traffic_msgs)!=1 else ''})</h3>
        <table><thead><tr><th>#</th><th>Msg #</th><th>Origin</th><th>Destination</th>
        <th>Type</th><th>Status</th><th>Notes</th></tr></thead>
        <tbody>{traffic_rows}</tbody></table>"""

    net_name_esc = html.escape(net.name) if net else 'Net'
    op_callsign_esc = html.escape(op_callsign)
    freq_esc = html.escape(freq)

    page_html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>ICS-205 Net Log — {net_name_esc}</title>
<style>
  body {{ font-family: Arial, sans-serif; font-size: 11pt; margin: 20mm; color: #000; }}
  h1 {{ font-size: 16pt; margin-bottom: 4px; }}
  h2 {{ font-size: 13pt; margin-top: 16px; margin-bottom: 4px; border-bottom: 1px solid #000; }}
  h3 {{ font-size: 12pt; margin-top: 16px; margin-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 10pt; }}
  th {{ background: #ddd; border: 1px solid #999; padding: 4px 6px; text-align: left; }}
  td {{ border: 1px solid #ccc; padding: 3px 6px; }}
  .header-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 12px 0; }}
  .field {{ margin-bottom: 4px; }}
  .label {{ font-weight: bold; font-size: 9pt; color: #555; display: block; }}
  .value {{ font-size: 11pt; border-bottom: 1px solid #aaa; padding-bottom: 2px; min-height: 18px; }}
  @media print {{ body {{ margin: 10mm; }} }}
</style>
</head><body>
<h1>ICS-205 — Amateur Radio Net Log</h1>
<div class="header-grid">
  <div>
    <div class="field"><span class="label">Net Name / Incident</span>
      <span class="value">{net_name_esc if net else ''}</span></div>
    <div class="field"><span class="label">Frequency / Mode</span>
      <span class="value">{freq_esc}</span></div>
    <div class="field"><span class="label">Net Control Station</span>
      <span class="value">{op_callsign_esc}</span></div>
  </div>
  <div>
    <div class="field"><span class="label">Session Start (UTC)</span>
      <span class="value">{started}</span></div>
    <div class="field"><span class="label">Session End (UTC)</span>
      <span class="value">{ended}</span></div>
    <div class="field"><span class="label">Total Check-Ins</span>
      <span class="value">{len(checkins)}</span></div>
  </div>
</div>

<h2>Station Check-In Log</h2>
<table>
  <thead><tr><th>#</th><th>Time (UTC)</th><th>Callsign</th><th>Name</th>
    <th>Signal</th><th>Comments / Traffic</th>{zone_th}{tactical_th}</tr></thead>
  <tbody>{checkin_rows}</tbody>
</table>
{traffic_section}

<p style="margin-top:24px;font-size:9pt;color:#555">
  Prepared by: {op_callsign_esc} &nbsp;|&nbsp; Printed: <span id="print-ts"></span>
</p>
<script>document.getElementById('print-ts').textContent = new Date().toUTCString();</script>
</body></html>"""

    return HTMLResponse(content=page_html)
