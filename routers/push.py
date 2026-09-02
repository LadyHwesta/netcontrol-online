"""
Web push notifications (issue follow-up) — subscribe/unsubscribe/test-send
endpoints for the second, app-native reminder channel alongside email. The
actual periodic sends (upcoming Net Control/Broadcaster shifts, and
activation rotation shift changes) are driven entirely by send_reminders.py,
not from here — this router is just the browser-facing subscription API plus
a self-test so a user can confirm their subscription actually works right
after enabling it.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import PushSubscription, User
from routers import helpers
from routers.deps import get_current_user
from routers.helpers import _send_web_push, _vapid_configured

router = APIRouter()


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeIn(BaseModel):
    """The exact shape of the browser's own PushSubscription.toJSON()."""
    endpoint: str
    keys: PushSubscriptionKeys


class PushUnsubscribeIn(BaseModel):
    endpoint: str


@router.get("/push/vapid-public-key")
def get_vapid_public_key():
    """Public, no auth -- this is sent to every browser that subscribes
    anyway, same trust level as a CAPTCHA site key. 404 (not just an empty
    value) when unconfigured, so the frontend can cleanly hide the whole
    Notifications card instead of offering a toggle that would just fail."""
    if not _vapid_configured():
        raise HTTPException(404, "Push notifications are not configured on this server")
    return {"public_key": helpers.VAPID_PUBLIC_KEY}


@router.post("/push/subscribe", status_code=204)
async def subscribe_push(
    data: PushSubscribeIn, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Upserts by endpoint -- a browser's subscription endpoint is globally
    unique per origin, so re-subscribing the same browser (e.g. after
    clearing the toggle off/on, or a different account logging in on a
    shared browser) updates the existing row rather than creating a
    duplicate."""
    existing = (
        await db.execute(select(PushSubscription).filter(PushSubscription.endpoint == data.endpoint))
    ).scalar_one_or_none()
    if existing:
        existing.user_id = current_user.id
        existing.p256dh = data.keys.p256dh
        existing.auth = data.keys.auth
    else:
        db.add(PushSubscription(
            user_id=current_user.id, endpoint=data.endpoint,
            p256dh=data.keys.p256dh, auth=data.keys.auth,
        ))
    await db.commit()


@router.delete("/push/subscribe", status_code=204)
async def unsubscribe_push(
    data: PushUnsubscribeIn, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Idempotent -- deletes only if it belongs to the caller, 204 either
    way (matches this app's other delete-if-present endpoints), so the
    frontend doesn't need to special-case "already gone"."""
    await db.execute(delete(PushSubscription).filter(
        PushSubscription.endpoint == data.endpoint, PushSubscription.user_id == current_user.id,
    ))
    await db.commit()


@router.post("/push/test")
async def test_push(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Lets a user confirm their subscription actually works right after
    enabling it, instead of waiting for a real reminder window to roll
    around."""
    sent = await _send_web_push(
        db, current_user.id,
        title="🔔 Test Notification",
        body="If you can see this, push notifications are working!",
        url="/",
    )
    if not sent:
        raise HTTPException(400, "No active push subscriptions found for your account — enable notifications first.")
    return {"sent": sent}
