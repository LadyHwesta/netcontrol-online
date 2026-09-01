"""
Checkin routes + Station remarks + the checkin traffic-toggle endpoints
(moved here from the Evacuation Zone banner in main.py, where they used to
live despite being checkin endpoints, not evac-zone ones).
"""

import csv
import io
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Checkin, EvacZone, Net, NetSession, StationRemark, User
from routers.callsign_lookup import lookup_callsign
from routers.deps import get_current_user
from routers.helpers import _get_editable_net, _get_session_for_user, _preferred_names_for_net, _tactical_callsigns_for_session
from routers.schemas import CheckinOut

router = APIRouter()


class CheckinCreate(BaseModel):
    callsign: str
    name: Optional[str] = None
    signal_report: Optional[str] = None
    comments: Optional[str] = None
    has_traffic: bool = False
    evac_zone: Optional[str] = None
    dmr_talkgroup: Optional[str] = None
    dmr_region: Optional[str] = None

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        return v.upper().strip()


class StationRemarkUpsert(BaseModel):
    remark: Optional[str] = None
    preferred_name: Optional[str] = None   # overrides FCC name in Expected Stations + reports


class StationRemarkOut(BaseModel):
    callsign: str
    net_id: int
    remark: Optional[str] = None
    preferred_name: Optional[str] = None
    updated_at: datetime

    model_config = {"from_attributes": True}


@router.get("/sessions/{session_id}/checkins", response_model=list[CheckinOut])
async def list_checkins(session_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_session_for_user(session_id, current_user, db)
    # Newest first — this is the live roster's data source; CSV export and
    # ICS-205 have their own chronological (oldest-first) queries, unaffected.
    checkins = (await db.execute(select(Checkin).filter(Checkin.session_id == session_id).order_by(Checkin.checked_in_at.desc()))).scalars().all()
    preferred_names = await _preferred_names_for_net(session.net_id, db)
    tactical_callsigns = await _tactical_callsigns_for_session(session_id, db)
    out = [CheckinOut.model_validate(c) for c in checkins]
    for c, o in zip(checkins, out):
        if c.callsign in preferred_names:
            o.name = preferred_names[c.callsign]
        if c.tactical_position_id:
            o.tactical_callsign = tactical_callsigns.get(c.tactical_position_id)
    return out


async def _is_first_checkin_for_net(net_id: int, callsign: str, db: AsyncSession) -> bool:
    """True if `callsign` has never checked into this net before (any session,
    any type of checkin -- routine, offline-logged, or tactical sign-on all
    count as prior participation). Called before inserting the new row, so
    the row being created is never counted against itself."""
    # GMRS nets allow the same callsign to check in multiple times (shared
    # family licence), so this is deliberately "any match" (.limit(1) +
    # .scalar(), matching the old .first()'s take-one-ignore-rest semantics)
    # rather than scalar_one_or_none(), which would raise on the GMRS case.
    prior = (await db.execute(
        select(Checkin.id)
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id, Checkin.callsign == callsign)
        .limit(1)
    )).scalar()
    return prior is None


async def _lookup_name_for_import(net_id: int, callsign: str, current_user: User, db: AsyncSession) -> Optional[str]:
    """Cascade used by the CSV importer's optional "look up missing names"
    pass (issue follow-up) -- for a row with no Name column value:
      1. This net's own check-in history -- the most recent prior check-in
         by this callsign on this net (any session), if it has a name on
         file. Often more accurate/personal than a fresh FCC lookup for a
         repeat attendee (a preferred nickname vs. the FCC's legal name),
         so it's tried first.
      2. An FCC/GMRS lookup (the same one the manual check-in form's
         as-you-type auto-fill uses), for a callsign that's never checked
         in here before.
    Returns None if neither source has anything -- the row is left with no
    name, same as if the option had been off. lookup_callsign never raises
    (every external call it makes is already caught internally and
    cascaded), but wrapped defensively anyway so one row's lookup can never
    abort the rest of a bulk import."""
    name = (await db.execute(
        select(Checkin.name)
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id, Checkin.callsign == callsign, Checkin.name.isnot(None), Checkin.name != "")
        .order_by(Checkin.checked_in_at.desc())
        .limit(1)
    )).scalar()
    if name:
        return name
    try:
        result = await lookup_callsign(callsign, current_user, db)
        if result.status == "found" and result.name:
            return result.name
    except Exception:
        pass
    return None


async def _create_checkin(session: NetSession, net: Optional[Net], data: CheckinCreate, db: AsyncSession) -> Checkin:
    """Shared per-checkin logic behind both add_checkin (one at a time) and
    import_checkins_csv (bulk, issue #26) — same validation either way so
    the two paths can't drift apart. Raises HTTPException on any rejection;
    caller decides whether that aborts the whole request (add_checkin) or is
    just recorded and skipped (the CSV importer)."""
    # An offline-entered session (issue #20) is created already "ended" -- at
    # the reported net date/time, not now -- specifically so it can still take
    # checkins after the fact. Its own is_offline_locked flag (set via the same
    # /sessions/{id}/end endpoint) is what closes it to further checkins instead.
    if session.is_offline:
        if session.is_offline_locked:
            raise HTTPException(400, "This logged net has been closed — no more check-ins can be added")
    elif session.ended_at is not None:
        raise HTTPException(400, "Cannot add checkins to an ended session")

    # Prevent duplicate callsign in the same session — except for GMRS nets where a
    # single family licence is shared among multiple stations.
    is_gmrs = net and net.net_type == "gmrs"
    if not is_gmrs:
        # This check IS the (app-level, not DB-level) enforcement of
        # uniqueness here -- .limit(1) + .scalars().first() rather than
        # scalar_one_or_none(), so two check-ins racing for the same
        # callsign can't turn into a 500 for whoever submits third.
        existing = (await db.execute(select(Checkin).filter(
            Checkin.session_id == session.id,
            Checkin.callsign == data.callsign,
        ).limit(1))).scalars().first()
        if existing:
            raise HTTPException(409, f"{data.callsign} has already checked in to this session")

    checkin = Checkin(
        session_id=session.id,
        callsign=data.callsign,
        name=data.name,
        signal_report=data.signal_report,
        comments=data.comments,
        has_traffic=data.has_traffic,
        is_first_checkin=await _is_first_checkin_for_net(session.net_id, data.callsign, db),
        evac_zone=data.evac_zone or None,
        dmr_talkgroup=data.dmr_talkgroup or None,
        dmr_region=data.dmr_region or None,
    )
    if session.is_offline:
        # Stamp with the reported net date/time, not real "now" (issue #20).
        checkin.checked_in_at = session.started_at
    db.add(checkin)
    await db.commit()
    await db.refresh(checkin)

    # Auto-upsert evac zone when provided (ARES/ACES nets)
    if data.evac_zone:
        existing_ez = (await db.execute(select(EvacZone).filter(
            EvacZone.net_id == session.net_id,
            EvacZone.callsign == data.callsign,
        ))).scalar_one_or_none()
        if existing_ez:
            existing_ez.zone = data.evac_zone
            existing_ez.updated_at = datetime.now(timezone.utc)
        else:
            db.add(EvacZone(net_id=session.net_id, callsign=data.callsign, zone=data.evac_zone))
        await db.commit()

    return checkin


@router.post("/sessions/{session_id}/checkins", response_model=CheckinOut, status_code=201)
async def add_checkin(session_id: int, data: CheckinCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session = await _get_session_for_user(session_id, current_user, db)
    net = (await db.execute(select(Net).filter(Net.id == session.net_id))).scalar_one_or_none()
    return await _create_checkin(session, net, data, db)


class CheckinImportError(BaseModel):
    row: int              # 1-indexed as a spreadsheet would show it (header is row 1)
    callsign: Optional[str] = None
    reason: str


class CheckinImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[CheckinImportError]
    names_looked_up: int = 0   # rows whose blank Name column got auto-filled (issue follow-up)


# Header matching is deliberately loose -- letters/digits only, case-folded --
# so "Signal Report", "signal_report", and "SignalReport" all land the same
# way. Only "callsign" is required; everything else is optional, matching
# CheckinCreate.
_CHECKIN_IMPORT_COLUMNS = {
    "callsign": "callsign",
    "name": "name",
    "signalreport": "signal_report",
    "sigreport": "signal_report",
    "comments": "comments",
    "comment": "comments",
    "notes": "comments",
    "hastraffic": "has_traffic",
    "traffic": "has_traffic",
    "evaczone": "evac_zone",
    "zone": "evac_zone",
    "dmrtalkgroup": "dmr_talkgroup",
    "talkgroup": "dmr_talkgroup",
    "dmrregion": "dmr_region",
    "region": "dmr_region",
}


def _normalize_csv_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


@router.post("/sessions/{session_id}/checkins/import", response_model=CheckinImportResult)
async def import_checkins_csv(
    session_id: int,
    file: UploadFile = File(...),
    lookup_missing_names: bool = Form(False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-add checkins from an uploaded CSV (issue #26) -- built mainly for
    "Log a Net That Already Happened" (issue #20), where re-typing a whole
    paper roster one row at a time is tedious, but works for any session
    that can still take checkins (a live one, or an unlocked offline one) --
    same rules as add_checkin above, via the same _create_checkin helper.
    Each row is validated and inserted independently; one bad row is
    recorded in the response and skipped rather than aborting the rest. See
    GET /checkins/import-sample for the expected column shape.

    lookup_missing_names (issue follow-up): when a row's Name column is
    blank -- common for a roster that's just a list of callsigns -- fills
    it in via _lookup_name_for_import (this net's own check-in history,
    then an FCC/GMRS lookup) instead of leaving it empty. Off by default
    since it's slower (a network round-trip per uncached callsign) and not
    every import wants it; a row with a Name already provided is never
    touched either way."""
    session = await _get_session_for_user(session_id, current_user, db)
    net = (await db.execute(select(Net).filter(Net.id == session.net_id))).scalar_one_or_none()

    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    try:
        header_row = next(reader)
    except StopIteration:
        raise HTTPException(400, "CSV file is empty")

    columns = [_CHECKIN_IMPORT_COLUMNS.get(_normalize_csv_header(h)) for h in header_row]
    if "callsign" not in columns:
        raise HTTPException(400, 'CSV must have a "Callsign" column — download the sample for the expected format')

    imported = 0
    names_looked_up = 0
    errors: list[CheckinImportError] = []
    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        if not any(cell.strip() for cell in raw_row):
            continue  # skip blank rows
        row = {col: val for col, val in zip(columns, raw_row) if col}
        callsign = (row.get("callsign") or "").strip().upper()
        if not callsign:
            errors.append(CheckinImportError(row=row_num, reason="Missing callsign"))
            continue
        try:
            name = (row.get("name") or "").strip() or None
            if not name and lookup_missing_names:
                name = await _lookup_name_for_import(session.net_id, callsign, current_user, db)
                if name:
                    names_looked_up += 1
            data = CheckinCreate(
                callsign=callsign,
                name=name,
                signal_report=(row.get("signal_report") or "").strip() or None,
                comments=(row.get("comments") or "").strip() or None,
                has_traffic=(row.get("has_traffic") or "").strip().lower() in ("1", "true", "yes", "y"),
                evac_zone=(row.get("evac_zone") or "").strip() or None,
                dmr_talkgroup=(row.get("dmr_talkgroup") or "").strip() or None,
                dmr_region=(row.get("dmr_region") or "").strip() or None,
            )
            await _create_checkin(session, net, data, db)
            imported += 1
        except HTTPException as e:
            errors.append(CheckinImportError(row=row_num, callsign=callsign, reason=str(e.detail)))
        except Exception as e:
            errors.append(CheckinImportError(row=row_num, callsign=callsign, reason=str(e)))

    return CheckinImportResult(imported=imported, skipped=len(errors), errors=errors, names_looked_up=names_looked_up)


@router.get("/checkins/import-sample")
def download_checkin_import_sample(current_user: User = Depends(get_current_user)):
    """Downloadable template showing exactly the columns import_checkins_csv
    above expects, with a couple of filled-in example rows."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Callsign", "Name", "Signal Report", "Comments", "Has Traffic", "Evac Zone", "DMR Talkgroup", "DMR Region"])
    writer.writerow(["W1AW", "Hiram Percy Maxim", "59", "Mobile, first check-in", "no", "", "", ""])
    writer.writerow(["KJ7ABC", "Jane Doe", "55", "", "yes", "Zone 3", "3120", "Western WA"])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="checkin_import_sample.csv"'},
    )


@router.delete("/checkins/{checkin_id}", status_code=204)
async def delete_checkin(checkin_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    checkin = (await db.execute(select(Checkin).filter(Checkin.id == checkin_id))).scalar_one_or_none()
    if not checkin:
        raise HTTPException(404, "Checkin not found")
    # Verify ownership via session → net
    await _get_session_for_user(checkin.session_id, current_user, db)
    await db.delete(checkin)
    await db.commit()


@router.patch("/checkins/{checkin_id}/traffic", response_model=CheckinOut)
async def toggle_traffic(checkin_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Toggle has_traffic flag on an existing checkin."""
    checkin = (await db.execute(select(Checkin).filter(Checkin.id == checkin_id))).scalar_one_or_none()
    if not checkin:
        raise HTTPException(404, "Checkin not found")
    await _get_session_for_user(checkin.session_id, current_user, db)
    checkin.has_traffic = not checkin.has_traffic
    await db.commit()
    await db.refresh(checkin)
    return checkin


@router.patch("/checkins/{checkin_id}/traffic-called", response_model=CheckinOut)
async def toggle_traffic_called(checkin_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Toggle traffic_called flag on an existing checkin -- tracks whether the
    operator has already passed this station's traffic, and persists across
    session close/reopen (unlike the old client-side-only tracking)."""
    checkin = (await db.execute(select(Checkin).filter(Checkin.id == checkin_id))).scalar_one_or_none()
    if not checkin:
        raise HTTPException(404, "Checkin not found")
    await _get_session_for_user(checkin.session_id, current_user, db)
    checkin.traffic_called = not checkin.traffic_called
    await db.commit()
    await db.refresh(checkin)
    return checkin


class CheckinPositionUpdate(BaseModel):
    """Both null clears a previously-set position; a lat without a lon (or
    vice versa) is rejected -- a position is a pair, not two independent
    fields."""
    lat: Optional[float] = None
    lon: Optional[float] = None

    @field_validator("lat")
    @classmethod
    def valid_lat(cls, v):
        if v is not None and not (-90 <= v <= 90):
            raise ValueError("lat must be between -90 and 90")
        return v

    @field_validator("lon")
    @classmethod
    def valid_lon(cls, v):
        if v is not None and not (-180 <= v <= 180):
            raise ValueError("lon must be between -180 and 180")
        return v


@router.patch("/checkins/{checkin_id}/position", response_model=CheckinOut)
async def set_checkin_position(checkin_id: int, data: CheckinPositionUpdate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Manually-reported GPS position (issue follow-up) -- for an operator
    with no APRS capability but who can read off their own coordinates over
    the air. Deliberately separate from check-in creation itself (keeps the
    fast check-in form uncluttered) and from the APRS integration (works
    with zero APRS setup on the net at all); merged into the same station
    map as APRS-derived positions by routers/aprs.py, tagged "manual" there."""
    if (data.lat is None) != (data.lon is None):
        raise HTTPException(400, "lat and lon must be set (or cleared) together")
    checkin = (await db.execute(select(Checkin).filter(Checkin.id == checkin_id))).scalar_one_or_none()
    if not checkin:
        raise HTTPException(404, "Checkin not found")
    await _get_session_for_user(checkin.session_id, current_user, db)
    checkin.lat = data.lat
    checkin.lon = data.lon
    await db.commit()
    await db.refresh(checkin)
    return checkin


@router.get("/nets/{net_id}/stations/{callsign}/remark", response_model=Optional[StationRemarkOut])
async def get_station_remark(
    net_id: int,
    callsign: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_editable_net(net_id, current_user, db)
    remark = (await db.execute(select(StationRemark).filter(
        StationRemark.net_id == net_id,
        StationRemark.callsign == callsign.upper(),
    ))).scalar_one_or_none()
    return remark  # None returns as null → 200 with null body; frontend handles


@router.put("/nets/{net_id}/stations/{callsign}/remark", response_model=Optional[StationRemarkOut])
async def upsert_station_remark(
    net_id: int,
    callsign: str,
    body: StationRemarkUpsert,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_editable_net(net_id, current_user, db)
    cs = callsign.upper().strip()
    remark_text = (body.remark or "").strip() or None
    preferred_name = (body.preferred_name or "").strip() or None

    existing = (await db.execute(select(StationRemark).filter(
        StationRemark.net_id == net_id,
        StationRemark.callsign == cs,
    ))).scalar_one_or_none()

    if not remark_text and not preferred_name:
        # Nothing left to store -- clear the row rather than leaving an empty one.
        if existing:
            await db.delete(existing)
            await db.commit()
        return None

    if existing:
        existing.remark = remark_text
        existing.preferred_name = preferred_name
        existing.updated_by = current_user.id
        existing.updated_at = datetime.now(timezone.utc)
        remark = existing
    else:
        remark = StationRemark(
            net_id=net_id,
            callsign=cs,
            remark=remark_text,
            preferred_name=preferred_name,
            updated_by=current_user.id,
        )
        db.add(remark)
    await db.commit()
    await db.refresh(remark)
    return remark


@router.delete("/nets/{net_id}/stations/{callsign}/remark", status_code=204)
async def delete_station_remark(
    net_id: int,
    callsign: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_editable_net(net_id, current_user, db)
    remark = (await db.execute(select(StationRemark).filter(
        StationRemark.net_id == net_id,
        StationRemark.callsign == callsign.upper(),
    ))).scalar_one_or_none()
    if remark:
        await db.delete(remark)
        await db.commit()
