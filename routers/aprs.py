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
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import AprsConfig, Checkin, Net, NetSession, User
from routers.deps import get_current_user
from routers.helpers import (
    STATIC_DIR, _assert_ham_net, _email_log, _get_editable_net, _get_net_for_user,
    _get_setting, _public_base_url, _set_setting,
)

router = APIRouter()


class AprsConfigCreate(BaseModel):
    source_type: str = "relay"              # aprs_fi | relay
    aprs_fi_api_key: Optional[str] = None   # for aprs_fi
    filter_callsign: Optional[str] = None


class AprsConfigOut(BaseModel):
    source_type: str
    aprs_fi_api_key: Optional[str] = None
    filter_callsign: Optional[str] = None

    model_config = {"from_attributes": True}


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


class AprsPushPayload(BaseModel):
    entries: list[AprsPositionEntry]


_aprs_push_cache: dict = {}
_APRS_CACHE_TTL = 300  # seconds — matches the stale-data check in aprs_cache()


def _aprs_cache_key(net_id: int) -> str:
    return f"aprs_cache_{net_id}"


async def _aprs_cache_write(net_id: int, entries: list, db: AsyncSession) -> None:
    """Write position entries to both the in-memory dict and SystemSetting (survives restarts)."""
    now = _time.time()
    _aprs_push_cache[net_id] = {"entries": entries, "pushed_at": now}
    await _set_setting(_aprs_cache_key(net_id), json.dumps({"entries": entries, "pushed_at": now}), db)
    await db.commit()


async def _aprs_cache_read(net_id: int, db: AsyncSession) -> Optional[dict]:
    """Return the position cache for net_id, restoring from DB if the in-memory dict was wiped."""
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


def _aprs_fetch_aprsfi(cfg: AprsConfig, callsigns: list[str]) -> list[dict]:
    """Fetch current positions from aprs.fi (https://aprs.fi/page/api) for the given callsigns."""
    if not callsigns or not cfg.aprs_fi_api_key:
        return []
    try:
        r = httpx.get(
            "https://api.aprs.fi/api/get",
            params={"name": ",".join(callsigns), "what": "loc", "apikey": cfg.aprs_fi_api_key, "format": "json"},
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


async def _aprs_positions_for_net(net_id: int, cfg: AprsConfig, db: AsyncSession) -> list[dict]:
    """Shared by the authenticated positions endpoint and the public live
    page (routers/public.py) — same cache-or-refresh logic either way, so
    the public page stays live without needing an authenticated viewer's
    browser to keep the poll warm."""
    if cfg.source_type == "aprs_fi":
        cached = await _aprs_cache_read(net_id, db)
        if cached and (_time.time() - cached["pushed_at"]) <= _APRS_CACHE_TTL:
            entries = cached["entries"]
        else:
            callsigns = await _aprs_active_session_callsigns(net_id, db)
            entries = _aprs_fetch_aprsfi(cfg, callsigns)
            await _aprs_cache_write(net_id, entries, db)
    else:  # relay — nothing to fetch on demand, just serve whatever's been pushed
        cached = await _aprs_cache_read(net_id, db)
        entries = cached["entries"] if cached else []

    skip = (cfg.filter_callsign or "").upper()
    if skip:
        entries = [e for e in entries if (e.get("callsign") or "").upper() != skip]
    return entries


async def _public_aprs_positions(net: Net, db: AsyncSession) -> list[dict]:
    """Positions for the public live page (used by routers/public.py's
    public_active_sessions / public_session_detail) — only computed at all
    if the net has opted in via aprs_map_enabled. Configuring APRS and
    exposing it publicly are two separate decisions: field team positions
    can be sensitive, so a net with APRS configured but not opted in never
    even has its positions fetched for this path."""
    if not net or not net.aprs_map_enabled:
        return []
    cfg = (await db.execute(select(AprsConfig).filter(AprsConfig.net_id == net.id))).scalar_one_or_none()
    if not cfg:
        return []
    return await _aprs_positions_for_net(net.id, cfg, db)


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
        cfg.aprs_fi_api_key = data.aprs_fi_api_key or None
        cfg.filter_callsign = (data.filter_callsign or "").upper().strip() or None
    else:
        cfg = AprsConfig(
            net_id=net_id,
            source_type=data.source_type,
            aprs_fi_api_key=data.aprs_fi_api_key or None,
            filter_callsign=(data.filter_callsign or "").upper().strip() or None,
        )
        db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return cfg


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
    reads the relay-pushed cache otherwise. One call covers both source
    types, unlike DMR's fallback chain."""
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(AprsConfig).filter(AprsConfig.net_id == net_id))).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "APRS not configured for this net")
    return await _aprs_positions_for_net(net_id, cfg, db)


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
