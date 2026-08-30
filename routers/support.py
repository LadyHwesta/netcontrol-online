"""
Support tickets — POST /support/ticket, emails a support request to
SUPPORT_EMAIL on the current user's behalf.
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from models import User
from routers import helpers
from routers.deps import get_current_user

router = APIRouter()

SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "")   # helpdesk address for support tickets


class SupportTicketCreate(BaseModel):
    type: str
    subject: str
    body: str


@router.post("/support/ticket", status_code=204)
def create_support_ticket(
    data: SupportTicketCreate,
    current_user: User = Depends(get_current_user),
):
    if not helpers._smtp_configured():
        raise HTTPException(503, "Email is not configured on this server")
    if not SUPPORT_EMAIL:
        raise HTTPException(503, "Support email address is not configured on this server")
    if not data.subject.strip() or not data.body.strip():
        raise HTTPException(400, "Subject and body are required")

    subject = f"[NetControl Online] {data.type}: {data.subject.strip()}"
    body_html = f"""
<p><strong>From:</strong> {current_user.name} ({current_user.callsign})<br>
<strong>Email:</strong> {current_user.email}<br>
<strong>Type:</strong> {data.type}</p>
<hr>
<p>{data.body.replace(chr(10), '<br>')}</p>
<hr>
<p style="color:#888;font-size:12px">Sent from NetControl Online by {current_user.callsign} — reply to this email to respond directly to the user.</p>
"""
    body_text = (
        f"From: {current_user.name} ({current_user.callsign})\n"
        f"Email: {current_user.email}\n"
        f"Type: {data.type}\n\n"
        f"{data.body}\n\n"
        f"---\nReply to: {current_user.email}"
    )

    sent = helpers.send_email(
        to=[SUPPORT_EMAIL],
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        reply_to=f"{current_user.name} <{current_user.email}>",
    )
    if not sent:
        raise HTTPException(500, "Failed to send email — please try again later")
    helpers._email_log.info("Support ticket sent from %s — %s", current_user.callsign, subject)
