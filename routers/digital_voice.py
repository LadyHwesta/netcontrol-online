"""
Digital Voice Integration (DMR, D-Star, YSF, NXDN, P25, M17 — issue #26).

Started as DMR-only; a WPSD/Pi-Star hotspot's last-heard feed actually
reports every digital voice mode it hears, tagged per-entry, so this was
generalized rather than duplicated per mode. Internal names (DmrConfig,
/nets/{id}/dmr/*, dmr_talkgroup/dmr_region) are kept as-is — they're
already generic enough (freeform strings, presence-based on/off switch)
to serve any mode; only the user-facing labels changed. BrandMeister
stays DMR-only (it's a real, centralized DMR network API — no equivalent
exists for the other modes, whose reflectors are individually operated
with inconsistent, often non-JSON dashboard software).
"""

import json
import time as _time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import DmrConfig, User
from routers.deps import get_current_user
from routers.helpers import (
    _assert_ham_net, _email_log, _get_editable_net, _get_net_for_user, _get_setting,
    _redis_cache_read, _redis_cache_write, _set_setting,
)

router = APIRouter()


class DmrConfigCreate(BaseModel):
    source_type: str = "wpsd"           # wpsd | pistar | brandmeister
    mode: str = "dmr"                   # dmr | dstar | ysf | nxdn | p25 | m17 (issue #26)
    hotspot_url: Optional[str] = None   # for wpsd/pistar
    talkgroup_id: Optional[int] = None  # for brandmeister
    filter_callsign: Optional[str] = None
    direct_mode: bool = False


class DmrConfigOut(BaseModel):
    source_type: str
    mode: str = "dmr"
    hotspot_url: Optional[str] = None
    talkgroup_id: Optional[int] = None
    filter_callsign: Optional[str] = None
    direct_mode: bool

    model_config = {"from_attributes": True}


class DmrHeardEntry(BaseModel):
    callsign: str
    dmr_id: Optional[str] = None
    name: Optional[str] = None
    talk_group: Optional[str] = None
    timeslot: Optional[str] = None
    region: Optional[str] = None
    heard_at: Optional[str] = None
    duration: Optional[str] = None
    mode: Optional[str] = None   # dmr | dstar | ysf | nxdn | p25 | m17 (issue #26)


# In-memory cache for relay-pushed digital voice data { net_id: {"entries": [...], "pushed_at": float} }
# This is backed by SystemSetting so it survives server restarts.
_dmr_push_cache: dict = {}

_DMR_CACHE_TTL = 300  # seconds — matches the stale-data check in dmr_cache()


def _dmr_cache_key(net_id: int) -> str:
    return f"dmr_cache_{net_id}"


async def _dmr_cache_write(net_id: int, entries: list, db: AsyncSession) -> None:
    """Write relay entries to the in-memory dict, SystemSetting (survives
    restarts), and Redis if configured (shared across workers -- see
    routers/helpers.py's Redis cache section)."""
    now = _time.time()
    payload = json.dumps({"entries": entries, "pushed_at": now})
    _dmr_push_cache[net_id] = {"entries": entries, "pushed_at": now}
    await _set_setting(_dmr_cache_key(net_id), payload, db)
    await db.commit()
    await _redis_cache_write(_dmr_cache_key(net_id), payload, _DMR_CACHE_TTL)


async def _dmr_cache_read(net_id: int, db: AsyncSession) -> Optional[dict]:
    """Return the relay cache for net_id. Redis first when configured --
    the only tier that's actually correct across multiple workers; the
    in-memory dict below is just this worker's own last-seen copy, and the
    DB fallback after that is the original single-worker-only path this
    cache has always had (e.g. after a restart, or when Redis isn't set up
    at all)."""
    raw = await _redis_cache_read(_dmr_cache_key(net_id))
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    cached = _dmr_push_cache.get(net_id)
    if cached:
        return cached
    # Fallback: load from SystemSetting (e.g., after a server restart)
    raw = await _get_setting(_dmr_cache_key(net_id), db)
    if raw:
        try:
            data = json.loads(raw)
            _dmr_push_cache[net_id] = data  # repopulate in-memory cache
            return data
        except Exception:
            pass
    return None


# Real WPSD/Pi-Star mode strings -> our canonical short codes (issue #26).
# Confirmed against the actual dashboard source (WPSD-M17/WPSD-WebCode,
# f1rmb/Pi-Star_DV_Dash): the last-heard feed reports whichever digital
# voice mode(s) the hotspot actually heard, tagged per-entry in `mode` —
# it's not DMR-only data, it just used to be normalized as if it were.
_HOTSPOT_MODE_MAP = {
    "D-Star": "dstar",
    "YSF": "ysf",
    "P25": "p25",
    "NXDN": "nxdn",
    "M17": "m17",
}


def _dmr_normalize_wpsd(entry: dict) -> Optional[dict]:
    """Normalize a WPSD/Pi-Star last-heard entry to a common dict.

    Field names below match the REAL API response shape (verified against
    the dashboard source, not just docs): {time_utc, mode, callsign, name,
    callsign_suffix, target, src, duration, loss}. There is no top-level
    `slot`/`dst`/`country`/`start` key in the real payload — those were
    what this function used to read, which meant Talk Group, Timeslot,
    Region, and Heard-At always came back empty. `target` is the
    talkgroup/reflector string for every mode; `src` is "RF"/"Net"
    (transmission direction, not a radio ID); the DMR timeslot is embedded
    in `mode` itself ("DMR Slot 1"/"DMR Slot 2") rather than a separate
    field. There's no region/country data in this feed at all -- that's
    left None here rather than faked."""
    raw_mode = str(entry.get("mode", "")).strip()
    if raw_mode == "POCSAG":
        return None  # paging, not a voice check-in concern
    mode = "dmr" if raw_mode.startswith("DMR") else _HOTSPOT_MODE_MAP.get(raw_mode)
    timeslot = None
    if raw_mode.startswith("DMR Slot"):
        ts = raw_mode.replace("DMR Slot", "").strip()
        if ts:
            timeslot = f"TS{ts}"
    return {
        "callsign": str(entry.get("callsign", "")).upper().strip(),
        "dmr_id":   str(entry.get("callsign_suffix", "")).strip() or None,
        "name":     entry.get("name") or None,
        "talk_group": str(entry.get("target", "")).strip() or None,
        "timeslot": timeslot,
        "region":   None,
        "heard_at": entry.get("time_utc") or None,
        "duration": str(entry.get("duration", "")).strip() or None,
        "mode":     mode,
    }


def _dmr_normalize_brandmeister(entry: dict) -> dict:
    """Normalize a BrandMeister talkgroup/rx entry to a common dict."""
    slot = entry.get("slot")
    start_ts = entry.get("start")
    stop_ts  = entry.get("stop")
    duration = None
    if start_ts and stop_ts and stop_ts > start_ts:
        duration = f"{stop_ts - start_ts}s"
    heard_at = None
    if start_ts:
        try:
            heard_at = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    region = entry.get("sourceState") or entry.get("sourceCountry") or None
    return {
        "callsign":   str(entry.get("callsign", "")).upper().strip(),
        "dmr_id":     str(entry.get("SourceID", "")).strip() or None,
        "name":       entry.get("sourceName") or None,
        "talk_group": str(entry.get("DestinationID", "")).strip() or None,
        "timeslot":   f"TS{slot}" if slot else None,
        "region":     region,
        "heard_at":   heard_at,
        "duration":   duration,
        "mode":       "dmr",
    }


def _dmr_filter_mode(entries: list[dict], mode: str) -> list[dict]:
    """Narrow a normalized entry list down to one digital voice mode
    (issue #26) -- applied at READ time (proxy fetch + relay cache reads),
    not at push time, so a relay watching a mixed-mode hotspot can push
    everything and each net just sees the mode it configured. An entry
    with no mode info (older relay push, or an unrecognized hotspot mode
    string) is kept rather than dropped, so nothing silently disappears."""
    return [e for e in entries if not e.get("mode") or e.get("mode") == mode]


def _dmr_fetch_proxy(cfg: DmrConfig) -> list[dict]:
    """Fetch last-heard from hotspot via backend proxy (non-direct mode)."""
    try:
        if cfg.source_type == "brandmeister":
            if not cfg.talkgroup_id:
                return []
            r = httpx.get(
                "https://api.brandmeister.network/v2/talkgroup/rx/",
                params={"talkgroup": cfg.talkgroup_id, "limit": 30},
                timeout=10,
            )
            r.raise_for_status()
            raw = r.json() if isinstance(r.json(), list) else []
            return [_dmr_normalize_brandmeister(e) for e in raw]

        elif cfg.source_type == "pistar":
            if not cfg.hotspot_url:
                return []
            base = cfg.hotspot_url.rstrip("/")
            # Real classic Pi-Star endpoint is /api/last_heard.php with a
            # num_transmissions query param -- NOT /api/local/lastheard
            # with `limit`, which doesn't exist in the actual dashboard
            # codebase (confirmed against f1rmb/Pi-Star_DV_Dash's source).
            url = base if base.endswith(".php") else base + "/api/last_heard.php"
            r = httpx.get(url, params={"num_transmissions": 30}, timeout=10)
            r.raise_for_status()
            raw = r.json() if isinstance(r.json(), list) else []
            entries = [_dmr_normalize_wpsd(e) for e in raw[:30]]
            return _dmr_filter_mode([e for e in entries if e], cfg.mode)

        else:  # wpsd (default)
            if not cfg.hotspot_url:
                return []
            r = httpx.get(cfg.hotspot_url, params={"limit": 30}, timeout=10)
            r.raise_for_status()
            raw = r.json() if isinstance(r.json(), list) else []
            entries = [_dmr_normalize_wpsd(e) for e in raw]
            return _dmr_filter_mode([e for e in entries if e], cfg.mode)

    except httpx.ConnectError as exc:
        raise HTTPException(502, f"Cannot reach hotspot: {exc}. If your hotspot is on a local network, enable direct mode so the browser fetches it instead.")
    except httpx.TimeoutException:
        raise HTTPException(504, "Hotspot request timed out. Check that the URL is correct and the hotspot is online.")
    except Exception as exc:
        _email_log.warning("DMR fetch error: %s", exc)
        raise HTTPException(502, f"DMR fetch failed: {exc}")


@router.get("/nets/{net_id}/dmr/config", response_model=Optional[DmrConfigOut])
async def get_dmr_config(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(DmrConfig).filter(DmrConfig.net_id == net_id))).scalar_one_or_none()
    return cfg  # None → null in JSON → frontend shows "not configured"


@router.put("/nets/{net_id}/dmr/config", response_model=DmrConfigOut)
async def save_dmr_config(net_id: int, data: DmrConfigCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_editable_net(net_id, current_user, db)
    _assert_ham_net(net)
    # BrandMeister is a DMR-only network -- can't return any other mode's
    # traffic. The UI already hides this combination; this is defense in
    # depth for a direct API call (issue #26).
    if data.source_type == "brandmeister" and data.mode != "dmr":
        raise HTTPException(400, "BrandMeister only supports DMR mode")
    cfg = (await db.execute(select(DmrConfig).filter(DmrConfig.net_id == net_id))).scalar_one_or_none()
    if cfg:
        cfg.source_type     = data.source_type
        cfg.mode             = data.mode
        cfg.hotspot_url     = data.hotspot_url or None
        cfg.talkgroup_id    = data.talkgroup_id
        cfg.filter_callsign = (data.filter_callsign or "").upper().strip() or None
        cfg.direct_mode     = data.direct_mode
    else:
        cfg = DmrConfig(
            net_id          = net_id,
            source_type     = data.source_type,
            mode            = data.mode,
            hotspot_url     = data.hotspot_url or None,
            talkgroup_id    = data.talkgroup_id,
            filter_callsign = (data.filter_callsign or "").upper().strip() or None,
            direct_mode     = data.direct_mode,
        )
        db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return cfg


@router.delete("/nets/{net_id}/dmr/config", status_code=204)
async def delete_dmr_config(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_editable_net(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(DmrConfig).filter(DmrConfig.net_id == net_id))).scalar_one_or_none()
    if cfg:
        await db.delete(cfg)
        await db.commit()


@router.get("/nets/{net_id}/dmr/lastheard", response_model=list[DmrHeardEntry])
async def dmr_lastheard(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Backend-proxy last-heard fetch. Only used when direct_mode=False."""
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(DmrConfig).filter(DmrConfig.net_id == net_id))).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "DMR not configured for this net")
    entries = _dmr_fetch_proxy(cfg)

    # Filter out the NCS callsign
    skip = (cfg.filter_callsign or "").upper()
    if skip:
        entries = [e for e in entries if e["callsign"] != skip]

    return entries


class DmrPushPayload(BaseModel):
    entries: list[DmrHeardEntry]


@router.post("/nets/{net_id}/dmr/push", status_code=204)
async def dmr_push(
    net_id: int,
    data: DmrPushPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept last-heard data pushed from a local relay script (bypasses CORS entirely)."""
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(DmrConfig).filter(DmrConfig.net_id == net_id))).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "DMR not configured for this net")
    # Filter out NCS callsign server-side too
    skip = (cfg.filter_callsign or "").upper()
    entries = [e.model_dump() for e in data.entries]
    if skip:
        entries = [e for e in entries if (e.get("callsign") or "").upper() != skip]
    await _dmr_cache_write(net_id, entries, db)


class DmrRawPushPayload(BaseModel):
    """Raw (un-normalized) last-heard entries from a hotspot API.

    The relay script should send whatever the hotspot returns directly, along with
    the source type so the backend can apply the correct normalizer.  This keeps all
    normalization logic in one place and prevents relay ↔ backend drift.
    """
    source: str = "wpsd"   # wpsd | pistar | brandmeister
    entries: list[dict]


@router.post("/nets/{net_id}/dmr/push/raw", status_code=204)
async def dmr_push_raw(
    net_id: int,
    data: DmrRawPushPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept raw hotspot JSON from a relay script and normalize server-side.

    Prefer this endpoint over /dmr/push — it keeps normalization logic in the backend
    so relay scripts stay simple fetch-and-forward proxies.
    """
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(DmrConfig).filter(DmrConfig.net_id == net_id))).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "DMR not configured for this net")

    source = data.source.lower()
    if source in ("wpsd", "pistar"):
        # _dmr_normalize_wpsd returns None for entries it drops entirely
        # (e.g. POCSAG paging traffic) -- filtered out below. Deliberately
        # NOT mode-filtered here: the cache holds everything a mixed-mode
        # hotspot reports; /dmr/cache and /dmr/lastheard narrow it down to
        # cfg.mode at read time (issue #26) so one relay can serve any
        # net regardless of which mode(s) it cares about.
        entries = [_dmr_normalize_wpsd(e) for e in data.entries]
    elif source == "brandmeister":
        entries = [_dmr_normalize_brandmeister(e) for e in data.entries]
    else:
        raise HTTPException(400, f"Unknown source type '{source}'. Use wpsd, pistar, or brandmeister.")

    # Filter out NCS callsign and any entries with no callsign after normalization
    skip = (cfg.filter_callsign or "").upper()
    entries = [e for e in entries if e and e.get("callsign")]
    if skip:
        entries = [e for e in entries if e["callsign"].upper() != skip]

    await _dmr_cache_write(net_id, entries, db)


@router.get("/nets/{net_id}/dmr/cache")
async def dmr_cache(
    net_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return relay-pushed DMR data with freshness info."""
    net = await _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = (await db.execute(select(DmrConfig).filter(DmrConfig.net_id == net_id))).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "DMR not configured for this net")
    cached = await _dmr_cache_read(net_id, db)
    if not cached:
        raise HTTPException(404, "No relay data for this net — is the relay script running?")
    age = int(_time.time() - cached["pushed_at"])
    if age > _DMR_CACHE_TTL:
        raise HTTPException(
            404,
            f"Relay data is stale ({age}s old). Is the relay script still running?",
        )
    return {"entries": _dmr_filter_mode(cached["entries"], cfg.mode), "age_seconds": age}
