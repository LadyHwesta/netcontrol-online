"""
APRS Station Map (issue #22).

Mirrors the DMR integration almost exactly (per-net config, presence of a
config row is the on/off switch, cache rides on SystemSetting + an
in-memory dict) with one deliberate simplification: DMR has two divergent
push endpoints and a three-tier direct/proxy/relay-cache frontend fallback
chain; APRS has ONE push endpoint (the relay script does its own TNC2
parsing and pushes normalized entries) and one positions endpoint that
internally handles both source types, so the frontend just makes one call.
"""

import json
import time as _time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import AprsConfig, Checkin, Net, NetSession, Organization, User
from routers.deps import get_current_user
from routers.helpers import (
    STATIC_DIR, _assert_ham_net, _email_log, _get_editable_net, _get_net_for_user,
    _get_setting, _public_base_url, _redis_cache_read, _redis_cache_write, _set_setting,
)

router = APIRouter()


class AprsConfigCreate(BaseModel):
    source_type: str = "relay"              # aprs_fi | relay
    # aprs.fi's own API key is org-level now (Organization.aprs_fi_api_key,
    # see routers/orgs.py's GET/PUT /orgs/{id}/aprs-key), not per-net.
    filter_callsign: Optional[str] = None


class AprsConfigOut(BaseModel):
    source_type: str
    filter_callsign: Optional[str] = None

    model_config = {"from_attributes": True}


class AprsDefaultViewUpdate(BaseModel):
    """The map's default viewport (issue follow-up) -- lives on Net, not
    AprsConfig, since the station map (and this button) is available on
    every ham net regardless of whether real-time APRS is configured at
    all (manually-reported positions work with zero APRS setup). All three
    fields together or none -- omitting/nulling any of them clears the
    default entirely, falling back to static/js/aprs-map.js's hardcoded
    continental-US-ish view again."""
    lat: Optional[float] = None
    lon: Optional[float] = None
    zoom: Optional[int] = None

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

    @field_validator("zoom")
    @classmethod
    def valid_zoom(cls, v):
        if v is not None and not (0 <= v <= 19):  # Leaflet's usable range for the OSM tile set in use
            raise ValueError("zoom must be between 0 and 19")
        return v


class AprsPositionEntry(BaseModel):
    callsign: str
    lat: float
    lon: float
    comment: Optional[str] = None
    symbol: Optional[str] = None
    course: Optional[int] = None
    speed: Optional[float] = None
    altitude: Optional[float] = None
    heard_at: Optional[str] = None
    # aprs_fi | relay | manual -- stamped server-side (never trusted from a
    # push payload), so the map/attribution can tell a manually-reported
    # checkin position apart from real APRS data (issue follow-up).
    source: Optional[str] = None


class AprsPushPayload(BaseModel):
    entries: list[AprsPositionEntry]


_aprs_push_cache: dict = {}
_APRS_CACHE_TTL = 300  # seconds — matches the stale-data check in aprs_cache()


def _aprs_cache_key(net_id: int) -> str:
    return f"aprs_cache_{net_id}"


async def _aprs_cache_write(net_id: int, entries: list, db: AsyncSession) -> None:
    """Write position entries to the in-memory dict, SystemSetting (survives
    restarts), and Redis if configured (shared across workers -- see
    routers/helpers.py's Redis cache section)."""
    now = _time.time()
    payload = json.dumps({"entries": entries, "pushed_at": now})
    _aprs_push_cache[net_id] = {"entries": entries, "pushed_at": now}
    await _set_setting(_aprs_cache_key(net_id), payload, db)
    await db.commit()
    await _redis_cache_write(_aprs_cache_key(net_id), payload, _APRS_CACHE_TTL)


async def _aprs_cache_read(net_id: int, db: AsyncSession) -> Optional[dict]:
    """Return the position cache for net_id. Redis first when configured --
    the only tier that's actually correct across multiple workers; the
    in-memory dict below is just this worker's own last-seen copy, and the
    DB fallback after that is the original single-worker-only path (e.g.
    after a restart, or when Redis isn't set up at all)."""
    raw = await _redis_cache_read(_aprs_cache_key(net_id))
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    cached = _aprs_push_cache.get(net_id)
    if cached:
        return cached
    raw = await _get_setting(_aprs_cache_key(net_id), db)
    if raw:
        try:
            data = json.loads(raw)
            _aprs_push_cache[net_id] = data
            return data
        except Exception:
            pass
    return None


def _aprs_fetch_aprsfi(api_key: Optional[str], callsigns: list[str]) -> list[dict]:
    """Fetch current positions from aprs.fi (https://aprs.fi/page/api) for the
    given callsigns. api_key is the caller's ORG's key (Organization.
    aprs_fi_api_key), not a per-net one -- see AprsConfig's own comment."""
    if not callsigns or not api_key:
        return []
    try:
        r = httpx.get(
            "https://api.aprs.fi/api/get",
            params={"name": ",".join(callsigns), "what": "loc", "apikey": api_key, "format": "json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("result") != "ok":
            _email_log.warning("aprs.fi returned an error: %s", data.get("description"))
            return []
        entries = []
        for e in data.get("entries", []):
            try:
                lat = float(e["lat"])
                lon = float(e["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            heard_raw = e.get("lasttime") or e.get("time")
            heard_at = None
            if heard_raw:
                try:
                    from datetime import datetime, timezone
                    heard_at = datetime.fromtimestamp(int(heard_raw), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    pass

            def _num(key, cast):
                v = str(e.get(key, "")).strip()
                if not v or v.lower() == "none":
                    return None
                try:
                    return cast(v)
                except (TypeError, ValueError):
                    return None

            entries.append({
                "callsign": str(e.get("name", "")).upper().strip(),
                "lat": lat,
                "lon": lon,
                "comment": e.get("comment") or None,
                "symbol": e.get("symbol") or None,
                "course": _num("course", int),
                "speed": _num("speed", float),
                "altitude": _num("altitude", float),
                "heard_at": heard_at,
            })
        return entries
    except httpx.ConnectError as exc:
        raise HTTPException(502, f"Cannot reach aprs.fi: {exc}")
    except httpx.TimeoutException:
        raise HTTPException(504, "aprs.fi request timed out.")
    except Exception as exc:
        _email_log.warning("aprs.fi fetch error: %s", exc)
        raise HTTPException(502, f"aprs.fi fetch failed: {exc}")


async def _aprs_active_session_callsigns(net_id: int, db: AsyncSession) -> list[str]:
    """Callsigns checked into the net's current live session, if any — the
    watch-list aprs.fi mode queries positions for."""
    # Nothing stops a net from having more than one simultaneously-active
    # session (start_session doesn't check for one already open) -- take the
    # most recently started one deterministically rather than
    # scalar_one_or_none(), which would raise if that ever happens.
    session = (await db.execute(
        select(NetSession)
        .filter(NetSession.net_id == net_id, NetSession.ended_at.is_(None))
        .order_by(NetSession.started_at.desc())
        .limit(1)
    )).scalars().first()
    if not session:
        return []
    rows = (await db.execute(select(Checkin.callsign).filter(Checkin.session_id == session.id).distinct())).scalars().all()
    return list(rows)


async def _aprs_positions_for_net(net: Net, cfg: AprsConfig, db: AsyncSession) -> list[dict]:
    """Shared by the authenticated positions endpoint and the public live
    page (routers/public.py) — same cache-or-refresh logic either way, so
    the public page stays live without needing an authenticated viewer's
    browser to keep the poll warm. Takes the full net (not just its id) to
    reach its org's aprs.fi key."""
    if cfg.source_type == "aprs_fi":
        cached = await _aprs_cache_read(net.id, db)
        if cached and (_time.time() - cached["pushed_at"]) <= _APRS_CACHE_TTL:
            entries = cached["entries"]
        else:
            org = (await db.execute(select(Organization).filter(Organization.id == net.org_id))).scalar_one_or_none()
            callsigns = await _aprs_active_session_callsigns(net.id, db)
            entries = _aprs_fetch_aprsfi(org.aprs_fi_api_key if org else None, callsigns)
            await _aprs_cache_write(net.id, entries, db)
    else:  # relay — nothing to fetch on demand, just serve whatever's been pushed
        cached = await _aprs_cache_read(net.id, db)
        entries = cached["entries"] if cached else []

    skip = (cfg.filter_callsign or "").upper()
    if skip:
        entries = [e for e in entries if (e.get("callsign") or "").upper() != skip]
    for e in entries:
        e["source"] = cfg.source_type
    return entries


async def _manual_positions_for_net(net_id: int, db: AsyncSession, skip_callsign: str = "") -> list[dict]:
    """Manually-reported positions (issue follow-up) from the net's current
    live session's checkins -- works with zero APRS setup on the net at
    all, for an operator with no APRS capability who can read off their own
    coordinates. Same "current live session" scoping as
    _aprs_active_session_callsigns above."""
    session = (await db.execute(
        select(NetSession)
        .filter(NetSession.net_id == net_id, NetSession.ended_at.is_(None))
        .order_by(NetSession.started_at.desc())
        .limit(1)
    )).scalars().first()
    if not session:
        return []
    checkins = (await db.execute(
        select(Checkin).filter(
            Checkin.session_id == session.id,
            Checkin.lat.isnot(None),
            Checkin.lon.isnot(None),
        )
    )).scalars().all()
    return [
        {
            "callsign": c.callsign,
            "lat": c.lat,
            "lon": c.lon,
            "comment": c.comments or None,
            "symbol": None,
            "course": None,
            "speed": None,
            "altitude": None,
            "heard_at": c.checked_in_at.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "manual",
        }
        for c in checkins
        if c.callsign.upper() != skip_callsign
    ]


async def _all_positions_for_net(net: Net, db: AsyncSession) -> tuple[list[dict], Optional[str]]:
    """Automated (APRS, if configured) positions merged with manually-reported
    ones (always available, regardless of APRS setup) -- the one call both
    the authenticated positions endpoint and the public live page make.
    Returns (positions, source_type) -- source_type is the net's configured
    APRS source ("aprs_fi" | "relay"), or None if it has no AprsConfig at
    all (manual-only); used to decide whether the map owes aprs.fi credit."""
    cfg = (await db.execute(select(AprsConfig).filter(AprsConfig.net_id == net.id))).scalar_one_or_none()
    automated = await _aprs_positions_for_net(net, cfg, db) if cfg else []
    skip = (cfg.filter_callsign or "").upper() if cfg else ""
    manual = await _manual_positions_for_net(net.id, db, skip_callsign=skip)
    # A callsign already reporting via real APRS wins over its own manual
    # entry -- avoids two overlapping pins for the same station.
    automated_callsigns = {e["callsign"].upper() for e in automated}
    manual = [e for e in manual if e["callsign"].upper() not in automated_callsigns]
    return automated + manual, (cfg.source_type if cfg else None)


async def _public_aprs_positions(net: Net, db: AsyncSession) -> tuple[list[dict], Optional[str]]:
    """Positions for the public live page (used by routers/public.py's
    public_active_sessions / public_session_detail) — only computed at all
    if the net has opted in via aprs_map_enabled. Configuring a station map
    and exposing it publicly are two separate decisions: field team
    positions can be sensitive, so a net that hasn't opted in never even has
    its positions fetched for this path — manual positions included, same
    as the automated ones."""
    if not net or not net.aprs_map_enabled:
        return [], None
    return await _all_positions_for_net(net, db)


@router.get("/nets/{net_id}/aprs/config", response_model=Optional[AprsConfigOut])
async def get_aprs_config(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(AprsConfig).filter(AprsConfig.net_id == net_id))).scalar_one_or_none()
    return cfg  # None → null in JSON → frontend shows "not configured"


@router.put("/nets/{net_id}/aprs/config", response_model=AprsConfigOut)
async def save_aprs_config(net_id: int, data: AprsConfigCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_editable_net(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(AprsConfig).filter(AprsConfig.net_id == net_id))).scalar_one_or_none()
    if cfg:
        cfg.source_type = data.source_type
        cfg.filter_callsign = (data.filter_callsign or "").upper().strip() or None
    else:
        cfg = AprsConfig(
            net_id=net_id,
            source_type=data.source_type,
            filter_callsign=(data.filter_callsign or "").upper().strip() or None,
        )
        db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return cfg


@router.put("/nets/{net_id}/aprs/default-view", status_code=204)
async def set_aprs_default_view(net_id: int, data: AprsDefaultViewUpdate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Sets (or clears, if lat/lon/zoom are all omitted) the map's default
    viewport -- used both by the net edit form's manual lat/lon/zoom fields
    and the live map panel's "Set as Default View" button, which just PUTs
    whatever the map's current center/zoom happens to be. No AprsConfig row
    required (see AprsDefaultViewUpdate's docstring)."""
    net = await _get_editable_net(net_id, current_user, db)
    _assert_ham_net(net)
    net.aprs_default_lat = data.lat
    net.aprs_default_lon = data.lon
    net.aprs_default_zoom = data.zoom
    await db.commit()


@router.delete("/nets/{net_id}/aprs/config", status_code=204)
async def delete_aprs_config(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_editable_net(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(AprsConfig).filter(AprsConfig.net_id == net_id))).scalar_one_or_none()
    if cfg:
        await db.delete(cfg)
        await db.commit()


@router.get("/nets/{net_id}/aprs/positions", response_model=list[AprsPositionEntry])
async def aprs_positions(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Current station positions for the map panel — fetches fresh from
    aprs.fi if that's the configured source and the cache is stale, or just
    reads the relay-pushed cache otherwise, merged with any manually-reported
    checkin positions. No longer 404s when the net has no AprsConfig at all
    (issue follow-up) -- a ham net with zero APRS setup can still have
    manually-reported positions to show, so this always succeeds for any
    ham net the caller can access, empty list if there's truly nothing."""
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    positions, _source_type = await _all_positions_for_net(net, db)
    return positions


@router.post("/nets/{net_id}/aprs/push", status_code=204)
async def aprs_push(
    net_id: int,
    data: AprsPushPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept already-parsed positions pushed from aprs_relay.py — the one
    push endpoint (see the module note above on why DMR's two-endpoint
    split isn't repeated here)."""
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(AprsConfig).filter(AprsConfig.net_id == net_id))).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "APRS not configured for this net")
    skip = (cfg.filter_callsign or "").upper()
    entries = [e.model_dump() for e in data.entries]
    if skip:
        entries = [e for e in entries if (e.get("callsign") or "").upper() != skip]
    await _aprs_cache_write(net_id, entries, db)


@router.get("/nets/{net_id}/aprs/cache")
async def aprs_cache(
    net_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return relay-pushed APRS data with freshness info — a diagnostic
    endpoint (mirrors /dmr/cache) for confirming aprs_relay.py is actually
    running; the map panel's own polling uses /aprs/positions instead."""
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cached = await _aprs_cache_read(net_id, db)
    if not cached:
        raise HTTPException(404, "No relay data for this net — is aprs_relay.py running?")
    age = int(_time.time() - cached["pushed_at"])
    if age > _APRS_CACHE_TTL:
        raise HTTPException(
            404,
            f"Relay data is stale ({age}s old). Is aprs_relay.py still running?",
        )
    return {"entries": cached["entries"], "age_seconds": age}


@router.get("/nets/{net_id}/aprs/relay-script")
async def download_aprs_relay_script(
    net_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Serves the real aprs_relay.py from disk, verbatim, with the
    --server/--net-id/--my-callsign argparse defaults pre-filled for this
    net so the user only has to paste an API token (and, for online mode,
    a --callsigns watch-list) to run it. Deliberately NOT a second,
    JS-embedded copy of the relay logic the way DMR's tokens.js download
    button is — the APRS-IS passcode algorithm and TNC2 parser are
    substantial enough that keeping a single, unit-tested implementation
    is worth the slightly less turnkey download."""
    net = await _get_editable_net(net_id, current_user, db)
    _assert_ham_net(net)

    script_path = STATIC_DIR.parent / "aprs_relay.py"
    try:
        source = script_path.read_text()
    except OSError:
        raise HTTPException(500, "Relay script not found on server")

    backend = _public_base_url(request)
    source = source.replace(
        'p.add_argument("--server",      default=os.getenv("NT_SERVER", ""))',
        f'p.add_argument("--server",      default=os.getenv("NT_SERVER", {backend!r}))',
    )
    source = source.replace(
        'p.add_argument("--net-id",      default=os.getenv("NT_NET_ID", ""), type=int)',
        f'p.add_argument("--net-id",      default=os.getenv("NT_NET_ID", "{net_id}"), type=int)',
    )
    source = source.replace(
        'p.add_argument("--my-callsign", default=os.getenv("NT_MY_CALLSIGN", ""))',
        f'p.add_argument("--my-callsign", default=os.getenv("NT_MY_CALLSIGN", {current_user.callsign!r}))',
    )

    return StreamingResponse(
        iter([source]),
        media_type="text/x-python",
        headers={"Content-Disposition": 'attachment; filename="aprs_relay.py"'},
    )
