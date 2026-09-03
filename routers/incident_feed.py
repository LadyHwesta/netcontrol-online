"""
Live hazard feed for the Incidents page (issue follow-up to #28/#27) --
polls public sources (fires, earthquakes, power outages, significant
weather alerts) for a net's county and lets an operator turn one item
straight into an Incident. See incident_feed_sources.py for the actual
fetching/normalizing; this router is thin -- access control plus the one
piece of state this feature persists (IncidentFeedDismissal).
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import incident_feed_sources
from database import get_db
from models import IncidentFeedDismissal, User
from routers.deps import get_current_user
from routers.helpers import _get_editable_net

router = APIRouter()


class IncidentFeedItemOut(BaseModel):
    source: str
    external_id: str
    category: str
    title: str
    description: Optional[str] = None
    severity: Optional[str] = None
    county: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    occurred_at: Optional[datetime] = None
    url: Optional[str] = None
    suggested_zone_ids: list[int] = []


class IncidentFeedOut(BaseModel):
    items: list[IncidentFeedItemOut]
    sources_failed: list[str]
    county: Optional[str] = None
    fetched_at: datetime


class IncidentFeedDismiss(BaseModel):
    source: str
    external_id: str
    incident_id: Optional[int] = None   # set = "created an Incident from this"; omitted = plain dismiss


@router.get("/nets/{net_id}/incident-feed", response_model=IncidentFeedOut)
async def get_incident_feed(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_editable_net(net_id, current_user, db)
    result = await incident_feed_sources.list_feed_items_for_net(net, db)
    return IncidentFeedOut(
        items=result["items"], sources_failed=result["sources_failed"],
        county=result["county"], fetched_at=datetime.now(timezone.utc),
    )


@router.post("/nets/{net_id}/incident-feed/dismiss", status_code=204)
async def dismiss_incident_feed_item(
    net_id: int, data: IncidentFeedDismiss,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Marks one feed item as handled so it stops reappearing -- either a
    plain dismiss (not relevant) or, when incident_id is given, a record
    of which Incident it became (the frontend calls this right after a
    successful POST /nets/{net_id}/incidents in the "create from feed"
    flow). Upserts on the same (net_id, source, external_id) a plain
    dismiss would have used, so creating an Incident after having earlier
    dismissed the same item (or vice versa) just updates the one row."""
    await _get_editable_net(net_id, current_user, db)
    existing = (await db.execute(select(IncidentFeedDismissal).filter(
        IncidentFeedDismissal.net_id == net_id,
        IncidentFeedDismissal.source == data.source,
        IncidentFeedDismissal.external_id == data.external_id,
    ))).scalar_one_or_none()
    status = "created" if data.incident_id else "dismissed"
    if existing:
        existing.status = status
        existing.incident_id = data.incident_id
    else:
        db.add(IncidentFeedDismissal(
            net_id=net_id, source=data.source, external_id=data.external_id,
            status=status, incident_id=data.incident_id,
        ))
    await db.commit()
