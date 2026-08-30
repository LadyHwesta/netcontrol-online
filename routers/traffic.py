"""
Traffic messages — formal/informal/health-and-welfare message logging
within a net session.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import TrafficMessage, User
from routers.deps import get_current_user
from routers.helpers import _get_session_for_user

router = APIRouter()


class TrafficMessageCreate(BaseModel):
    origin_callsign: str
    dest_info: Optional[str] = None
    msg_number: Optional[str] = None
    msg_type: str = "formal"       # formal | informal | health_welfare
    status: str = "received"       # received | relayed | delivered | undeliverable
    notes: Optional[str] = None


class TrafficMessageUpdate(BaseModel):
    dest_info: Optional[str] = None
    msg_number: Optional[str] = None
    msg_type: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class TrafficMessageOut(BaseModel):
    id: int
    session_id: int
    msg_number: Optional[str]
    origin_callsign: str
    dest_info: Optional[str]
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
