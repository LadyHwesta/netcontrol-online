"""
Traffic messages — formal/informal/health-and-welfare message logging
within a net session, exportable per-message as an ICS-213 General Message
(issue follow-up) two ways: plain text ready to paste into a Winlink
message body or attach as a .txt file, and a printable HTML form (opened
in a new tab, same "print/save as PDF via the browser" convention as
routers/sessions.py's session_ics205) for a local paper/PDF copy.
"""

import html
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Net, TrafficMessage, User
from routers.deps import get_current_user
from routers.helpers import _get_session_for_user

router = APIRouter()


class TrafficMessageCreate(BaseModel):
    origin_callsign: str
    dest_info: Optional[str] = None
    msg_number: Optional[str] = None
    subject: Optional[str] = None
    msg_type: str = "formal"       # formal | informal | health_welfare
    status: str = "received"       # received | relayed | delivered | undeliverable
    notes: Optional[str] = None


class TrafficMessageUpdate(BaseModel):
    dest_info: Optional[str] = None
    msg_number: Optional[str] = None
    subject: Optional[str] = None
    msg_type: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class TrafficMessageOut(BaseModel):
    id: int
    session_id: int
    msg_number: Optional[str]
    origin_callsign: str
    dest_info: Optional[str]
    subject: Optional[str]
    msg_type: str
    status: str
    notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/sessions/{session_id}/traffic-messages", response_model=list[TrafficMessageOut])
async def list_traffic_messages(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_session_for_user(session_id, current_user, db)
    return (await db.execute(select(TrafficMessage).filter(TrafficMessage.session_id == session_id).order_by(TrafficMessage.created_at))).scalars().all()


@router.post("/sessions/{session_id}/traffic-messages", response_model=TrafficMessageOut, status_code=201)
async def create_traffic_message(
    session_id: int,
    body: TrafficMessageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_session_for_user(session_id, current_user, db)
    msg = TrafficMessage(
        session_id=session_id,
        origin_callsign=body.origin_callsign.upper().strip(),
        dest_info=body.dest_info,
        msg_number=body.msg_number,
        subject=body.subject,
        msg_type=body.msg_type,
        status=body.status,
        notes=body.notes,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


@router.patch("/traffic-messages/{msg_id}", response_model=TrafficMessageOut)
async def update_traffic_message(
    msg_id: int,
    body: TrafficMessageUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    msg = (await db.execute(select(TrafficMessage).filter(TrafficMessage.id == msg_id))).scalar_one_or_none()
    if not msg:
        raise HTTPException(404, "Message not found")
    await _get_session_for_user(msg.session_id, current_user, db)
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(msg, field, val)
    await db.commit()
    await db.refresh(msg)
    return msg


@router.delete("/traffic-messages/{msg_id}", status_code=204)
async def delete_traffic_message(
    msg_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    msg = (await db.execute(select(TrafficMessage).filter(TrafficMessage.id == msg_id))).scalar_one_or_none()
    if not msg:
        raise HTTPException(404, "Message not found")
    await _get_session_for_user(msg.session_id, current_user, db)
    await db.delete(msg)
    await db.commit()


_MSG_TYPE_LABELS = {"formal": "Formal", "informal": "Informal", "health_welfare": "Health & Welfare"}


async def _load_traffic_message_for_export(
    msg_id: int, current_user: User, db: AsyncSession,
) -> tuple[TrafficMessage, Net, Optional[str]]:
    """Shared by both ICS-213 export endpoints below -- resolves the
    message (404 if unknown), enforces the same net-access check as the
    rest of this router, and best-effort resolves "Approved by" from
    whoever's Net Control for the session."""
    msg = (await db.execute(select(TrafficMessage).filter(TrafficMessage.id == msg_id))).scalar_one_or_none()
    if not msg:
        raise HTTPException(404, "Message not found")
    session = await _get_session_for_user(msg.session_id, current_user, db)
    net = (await db.execute(select(Net).filter(Net.id == session.net_id))).scalar_one_or_none()

    approved_by = None
    try:
        from routers.schedules import _duty_labels_for_session
        duty = await _duty_labels_for_session(net, session, db)
        if duty.get("ncs_callsign"):
            approved_by = f"{duty['ncs_name']} ({duty['ncs_callsign']})" if duty.get("ncs_name") else duty["ncs_callsign"]
    except Exception:
        pass  # Approved by is a nice-to-have, never worth failing the export over

    return msg, net, approved_by


def _build_ics213_text(msg: TrafficMessage, net: Net, approved_by: Optional[str]) -> str:
    """Renders one TrafficMessage as a plain-text ICS-213 General Message
    (issue follow-up) -- the standard FEMA ICS-213 field set (Incident Name/
    To/From/Subject/Date/Time/Message/Approved by), formatted the way
    Winlink Express itself renders a submitted form's message body (a
    readable plain-text rendering alongside the interactive form's own XML
    attachment -- see this endpoint's own docstring for why plain text
    rather than attempting to reproduce Winlink's proprietary, versioned
    form XML). Ready to paste directly into a Winlink message body, or
    save/attach as a .txt file."""
    lines = [
        "ICS-213  GENERAL MESSAGE",
        "=" * 40,
        f"Incident Name : {net.name}",
        f"Msg #         : {msg.msg_number or '—'}",
        "",
        f"To            : {msg.dest_info or '—'}",
        f"From          : {msg.origin_callsign} ({_MSG_TYPE_LABELS.get(msg.msg_type, msg.msg_type)} traffic)",
        "",
        f"Date          : {msg.created_at.strftime('%Y-%m-%d')}",
        f"Time          : {msg.created_at.strftime('%H%M')}Z",
        "",
        f"Subject       : {msg.subject or '—'}",
        "",
        "Message:",
        msg.notes or "(no message text logged)",
        "",
    ]
    if approved_by:
        lines += [f"Approved by   : {approved_by}", f"Position/Title: Net Control Station, {net.name}", ""]
    lines += ["-" * 40, "Generated by NetControl Online -- paste into a Winlink message body,", "or attach this file, to relay via Winlink."]
    return "\n".join(lines) + "\n"


@router.get("/traffic-messages/{msg_id}/ics213")
async def export_traffic_ics213(
    msg_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Downloads one traffic message as a plain-text ICS-213 General Message
    (issue follow-up), formatted for relay via Winlink -- see
    _build_ics213_text's own docstring for the format rationale."""
    msg, net, approved_by = await _load_traffic_message_for_export(msg_id, current_user, db)
    text = _build_ics213_text(msg, net, approved_by)
    filename = f"ICS213_{(msg.msg_number or f'msg{msg.id}').replace('/', '-').replace(' ', '_')}.txt"
    return Response(
        content=text, media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_ics213_html(msg: TrafficMessage, net: Net, approved_by: Optional[str]) -> str:
    """Renders one TrafficMessage as a printable ICS-213 General Message
    (issue follow-up) -- same numbered-field layout as the real FEMA form,
    same "open in a new tab, print or save-as-PDF via the browser" pattern
    and CSS conventions as routers/sessions.py's session_ics205, for a
    local paper/PDF copy when Winlink relay isn't the point."""
    net_name = html.escape(net.name) if net else ""
    to = html.escape(msg.dest_info or "—")
    frm = html.escape(msg.origin_callsign)
    msg_type_label = html.escape(_MSG_TYPE_LABELS.get(msg.msg_type, msg.msg_type))
    subject = html.escape(msg.subject or "—")
    date_str = msg.created_at.strftime("%Y-%m-%d")
    time_str = msg.created_at.strftime("%H%M") + "Z"
    message_text = html.escape(msg.notes or "(no message text logged)")
    approved_esc = html.escape(approved_by) if approved_by else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>ICS-213 — {net_name}</title>
<style>
  body {{ font-family: Arial, sans-serif; font-size: 11pt; margin: 20mm; color: #000; }}
  h1 {{ font-size: 16pt; margin-bottom: 2px; }}
  .subtitle {{ font-size: 10pt; color: #555; margin-bottom: 14px; }}
  table.frame {{ width: 100%; border-collapse: collapse; border: 2px solid #000; }}
  table.frame td {{ border: 1px solid #000; padding: 6px 8px; vertical-align: top; }}
  .num {{ font-weight: bold; font-size: 9pt; }}
  .label {{ font-weight: bold; font-size: 9pt; color: #333; display: block; margin-bottom: 2px; }}
  .value {{ font-size: 11pt; min-height: 18px; }}
  .msgbox {{ min-height: 140px; white-space: pre-wrap; font-size: 11pt; }}
  .sigline {{ border-top: 1px solid #000; margin-top: 28px; padding-top: 2px; font-size: 9pt; color: #555; }}
  @media print {{ body {{ margin: 10mm; }} }}
</style>
</head><body>
<h1>ICS 213 — General Message</h1>
<div class="subtitle">Generated by NetControl Online — {html.escape(net_name)}</div>
<table class="frame">
  <tr>
    <td colspan="4"><span class="num">1. Incident Name</span><div class="value">{net_name}</div></td>
  </tr>
  <tr>
    <td colspan="2"><span class="num">2. To (Name/Position)</span><div class="value">{to}</div></td>
    <td colspan="2"><span class="num">3. From (Name/Position)</span><div class="value">{frm} ({msg_type_label} traffic)</div></td>
  </tr>
  <tr>
    <td colspan="2"><span class="num">4. Subject</span><div class="value">{subject}</div></td>
    <td><span class="num">5. Date</span><div class="value">{date_str}</div></td>
    <td><span class="num">6. Time</span><div class="value">{time_str}</div></td>
  </tr>
  <tr>
    <td colspan="4"><span class="num">7. Message</span><div class="msgbox">{message_text}</div></td>
  </tr>
  <tr>
    <td colspan="2"><span class="num">8. Approved by (Name/Position)</span><div class="value">{approved_esc}</div></td>
    <td colspan="2"><span class="label">Position/Title</span><div class="value">{'Net Control Station' if approved_esc else ''}</div></td>
  </tr>
</table>
<p class="sigline">Msg # {html.escape(msg.msg_number or '—')} &nbsp;|&nbsp; Printed: <span id="print-ts"></span></p>
<script>document.getElementById('print-ts').textContent = new Date().toUTCString();</script>
</body></html>"""


@router.get("/traffic-messages/{msg_id}/ics213-print")
async def print_traffic_ics213(
    msg_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Printable HTML ICS-213 General Message (issue follow-up) -- opened in
    a new tab; the browser's own Print dialog (Save as PDF) is what actually
    produces a PDF, same as session_ics205's established convention rather
    than a server-side PDF-generation dependency this app doesn't otherwise
    need."""
    msg, net, approved_by = await _load_traffic_message_for_export(msg_id, current_user, db)
    return HTMLResponse(content=_build_ics213_html(msg, net, approved_by))
