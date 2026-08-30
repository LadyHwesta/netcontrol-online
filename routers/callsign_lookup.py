"""
Callsign Lookup — resolve a callsign to FCC license data (ham or GMRS),
plus a checkin-history-based search used for autocomplete.
"""

import logging
import re as _re
from datetime import timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import CallsignCache, Checkin, GmrsLicense, Net, NetSession, User, utcnow
from routers.deps import get_current_user

router = APIRouter()


class CallsignLookupResult(BaseModel):
    callsign: str
    status: str          # "found" | "not_found" | "error"
    name: Optional[str] = None
    license_class: Optional[str] = None
    state: Optional[str] = None
    grid: Optional[str] = None
    expires: Optional[str] = None
    source: Optional[str] = None


class CallsignSearchResult(BaseModel):
    callsign: str
    name: Optional[str] = None
    license_class: Optional[str] = None
    state: Optional[str] = None


@router.get("/callsign/search", response_model=list[CallsignSearchResult])
async def search_callsigns(
    q: str = Query(..., min_length=2, max_length=12),
    net_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Search checkin history for callsigns whose suffix matches q.
    Searches the current net first (if net_id provided), then all nets owned by the user.
    Results are sorted by callsign suffix.
    MUST be defined before /callsign/{callsign}/lookup so 'search' is not captured as a path param."""
    q = q.upper().strip()

    def _suffix(cs: str) -> str:
        m = _re.search(r'\d([A-Z]+)$', cs.upper())
        return m.group(1) if m else cs

    async def _run_query(extra_filter) -> list[CallsignSearchResult]:
        rows = (await db.execute(
            select(
                Checkin.callsign,
                func.max(Checkin.name).label("name"),
            )
            .join(NetSession, NetSession.id == Checkin.session_id)
            .join(Net, Net.id == NetSession.net_id)
            .filter(Net.owner_id == current_user.id)
            .filter(extra_filter)
            # suffix match: callsign ends with q (case-insensitive)
            .filter(Checkin.callsign.ilike(f"%{q}"))
            .group_by(Checkin.callsign)
        )).all()
        results = [
            CallsignSearchResult(callsign=r.callsign, name=r.name, license_class=None)
            for r in rows
        ]
        results.sort(key=lambda r: _suffix(r.callsign))
        return results[:20]

    # 1. Search current net's history first
    if net_id:
        results = await _run_query(Net.id == net_id)
        if results:
            return results

    # 2. Fall back to all nets owned by this user
    results = await _run_query(True)
    return results


# Cache TTLs for callsign lookups
_CALLSIGN_CACHE_TTL_FOUND = 30 * 24 * 3600      # 30 days — licenses rarely change
_CALLSIGN_CACHE_TTL_NOT_FOUND = 7 * 24 * 3600   # 7 days — callsign might get issued


async def _callsign_cache_read(callsign: str, db: AsyncSession) -> Optional[CallsignLookupResult]:
    """Return a cached lookup result if still within TTL, else None."""
    row = (await db.execute(select(CallsignCache).filter(CallsignCache.callsign == callsign))).scalar_one_or_none()
    if not row:
        return None
    ttl = _CALLSIGN_CACHE_TTL_FOUND if row.status == "found" else _CALLSIGN_CACHE_TTL_NOT_FOUND
    # SQLite returns tz-naive datetimes; PostgreSQL returns tz-aware — normalize to UTC.
    cached_at = row.cached_at
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    if (utcnow() - cached_at).total_seconds() > ttl:
        return None
    return CallsignLookupResult(
        callsign=row.callsign,
        status=row.status,
        name=row.name,
        license_class=row.license_class,
        state=row.state,
        grid=row.grid,
        expires=row.expires,
        source=row.source,
    )


async def _callsign_cache_write(result: CallsignLookupResult, db: AsyncSession) -> None:
    """Upsert a lookup result into the local cache."""
    row = (await db.execute(select(CallsignCache).filter(CallsignCache.callsign == result.callsign))).scalar_one_or_none()
    if row:
        row.status = result.status
        row.name = result.name
        row.license_class = result.license_class
        row.state = result.state
        row.grid = result.grid
        row.expires = result.expires
        row.source = result.source
        row.cached_at = utcnow()
    else:
        db.add(CallsignCache(
            callsign=result.callsign,
            status=result.status,
            name=result.name,
            license_class=result.license_class,
            state=result.state,
            grid=result.grid,
            expires=result.expires,
            source=result.source,
        ))
    await db.commit()


_GMRS_CS_RE = _re.compile(r'^[A-Z]{3,4}\d{3,4}$')

def _is_gmrs_callsign(cs: str) -> bool:
    """Return True if callsign matches the FCC GMRS format (e.g. WQXH7777)."""
    return bool(_GMRS_CS_RE.match(cs))


@router.get("/callsign/{callsign}/lookup", response_model=CallsignLookupResult)
async def lookup_callsign(
    callsign: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Resolve a callsign to FCC license data.

    GMRS callsigns (e.g. WQXH7777):
      1. Local gmrs_licenses table (populated by gmrs_sync.py from FCC bulk download)
      2. FCC ULS API fallback (if local DB is empty / callsign not found locally)

    Ham callsigns (e.g. W1AW):
      1. Local callsign_cache (30-day TTL found / 7-day not_found)
      2. FCC ULS API
      3. HamDB.org
      4. callook.info
    """
    log = logging.getLogger("callsign_lookup")
    callsign = callsign.upper().strip()

    # ── GMRS branch ──────────────────────────────────────────────────────────
    if _is_gmrs_callsign(callsign):
        # 1. Local gmrs_licenses table (fast, no external call)
        row = (await db.execute(select(GmrsLicense).filter(GmrsLicense.callsign == callsign))).scalar_one_or_none()
        log.info("GMRS lookup: callsign=%s row_found=%s status=%r", callsign, row is not None, row.status if row else None)
        if row:
            status = "found" if (row.status or "").strip() == "A" else "not_found"
            return CallsignLookupResult(
                callsign=row.callsign,
                status=status,
                name=row.licensee_name,
                license_class=None,   # GMRS has no license classes
                state=row.state,
                grid=None,
                expires=row.expires,
                source="FCC GMRS DB",
            )

        # 2. FCC ULS API fallback (when local DB hasn't been synced yet, or callsign
        #    is very newly issued between weekly syncs)
        log.info("GMRS %s not in local DB — trying FCC ULS API", callsign)
        cached = await _callsign_cache_read(callsign, db)
        if cached:
            return cached

        async def _save(result: CallsignLookupResult) -> CallsignLookupResult:
            await _callsign_cache_write(result, db)
            return result

        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                r = await client.get(
                    "https://data.fcc.gov/api/license-view/basicSearch/getLicenses",
                    params={"format": "json", "searchValue": callsign},
                    headers={"User-Agent": "HamNetTracker/1.0"},
                )
                if r.status_code == 200:
                    data = r.json()
                    rows = data.get("Licenses", {}).get("License", [])
                    match = next(
                        (lic for lic in rows if lic.get("callsign", "").upper() == callsign),
                        None,
                    )
                    if match and match.get("statusDesc", "").lower() == "active":
                        name = (match.get("licenseeName") or "").strip().title() or None
                        return await _save(CallsignLookupResult(
                            callsign=match["callsign"],
                            status="found",
                            name=name,
                            license_class=None,
                            state=None,
                            grid=None,
                            expires=match.get("expiredDate") or None,
                            source="FCC ULS",
                        ))
                    elif match:
                        log.info("FCC ULS: GMRS %s found but status=%s", callsign, match.get("statusDesc"))
                else:
                    log.warning("FCC ULS HTTP %s for GMRS %s", r.status_code, callsign)
            except Exception as exc:
                log.warning("FCC ULS error for GMRS %s: %s", callsign, exc)

        log.warning("GMRS lookup exhausted for %s", callsign)
        return await _save(CallsignLookupResult(callsign=callsign, status="not_found"))

    # ── Ham branch ───────────────────────────────────────────────────────────
    # Return cached result if still fresh
    cached = await _callsign_cache_read(callsign, db)
    if cached:
        return cached

    async def _save(result: CallsignLookupResult) -> CallsignLookupResult:
        """Persist to cache then return."""
        await _callsign_cache_write(result, db)
        return result

    async with httpx.AsyncClient(timeout=8.0) as client:

        # --- 1. FCC ULS (official database) ---
        try:
            r = await client.get(
                "https://data.fcc.gov/api/license-view/basicSearch/getLicenses",
                params={"format": "json", "searchValue": callsign, "licenseType": "Amateur"},
                headers={"User-Agent": "HamNetTracker/1.0"},
            )
            if r.status_code == 200:
                data = r.json()
                licenses = data.get("Licenses", {})
                rows = licenses.get("License", [])
                # Find exact callsign match (search can return partial matches)
                match = next(
                    (lic for lic in rows if lic.get("callsign", "").upper() == callsign),
                    None,
                )
                if match and match.get("statusDesc", "").lower() == "active":
                    name = (match.get("licenseeName") or "").strip().title() or None
                    # FCC returns "JOHN DOE" — title-case it to "John Doe"
                    return await _save(CallsignLookupResult(
                        callsign=match["callsign"],
                        status="found",
                        name=name,
                        license_class=match.get("licenseClass") or None,
                        state=None,   # not in basic FCC search result
                        grid=None,
                        expires=match.get("expiredDate") or None,
                        source="FCC ULS",
                    ))
                elif match:
                    # Callsign exists but licence is not active
                    log.info("FCC ULS: %s found but status=%s", callsign, match.get("statusDesc"))
            else:
                log.warning("FCC ULS HTTP %s for %s", r.status_code, callsign)
        except Exception as exc:
            log.warning("FCC ULS error for %s: %s", callsign, exc)

        # --- 2. HamDB.org ---
        try:
            r = await client.get(
                f"https://hamdb.org/api/{callsign}/json",
                headers={"User-Agent": "HamNetTracker/1.0"},
            )
            if r.status_code == 200:
                data = r.json()
                if not isinstance(data, dict):
                    log.info("HamDB: unexpected response type %s for %s", type(data).__name__, callsign)
                    raise ValueError("unexpected response")
                hamdb = data.get("hamdb", {})
                msgs = hamdb.get("messages", {})
                cs = hamdb.get("callsign", {})
                if msgs.get("status") == "OK" and cs.get("call"):
                    fname = (cs.get("fname") or "").strip()
                    lname = (cs.get("name") or "").strip()
                    name = f"{fname} {lname}".strip() or None
                    return await _save(CallsignLookupResult(
                        callsign=cs["call"],
                        status="found",
                        name=name,
                        license_class=cs.get("class") or None,
                        state=cs.get("state") or None,
                        grid=cs.get("grid") or None,
                        expires=cs.get("expires") or None,
                        source="HamDB",
                    ))
                else:
                    log.info("HamDB: no result for %s (status=%s)", callsign, msgs.get("status"))
            else:
                log.warning("HamDB HTTP %s for %s", r.status_code, callsign)
        except Exception as exc:
            log.warning("HamDB error for %s: %s", callsign, exc)

        # --- 3. callook.info ---
        try:
            r = await client.get(
                f"https://callook.info/{callsign}/json",
                headers={"User-Agent": "HamNetTracker/1.0"},
            )
            if r.status_code == 200:
                data = r.json()
                if not isinstance(data, dict):
                    log.info("callook.info: unexpected top-level type %s for %s", type(data).__name__, callsign)
                elif data.get("status") == "VALID":
                    # Each nested field may be a dict OR a plain string depending
                    # on license type — use _safe_get() throughout.
                    def _safe_get(obj, key, default=None):
                        if isinstance(obj, dict):
                            return obj.get(key, default)
                        return default

                    name_obj  = data.get("name", {})
                    current   = data.get("current", {})
                    addr      = data.get("address", {})
                    loc       = data.get("location", {})
                    other     = data.get("otherInfo", {})

                    # Name: might be {"first":..,"last":..} or a plain string
                    if isinstance(name_obj, dict):
                        first = (_safe_get(name_obj, "first") or "").strip()
                        last  = (_safe_get(name_obj, "last")  or "").strip()
                        name  = f"{first} {last}".strip() or None
                    else:
                        name = str(name_obj).strip() or None

                    # State from address line2 e.g. "NEWINGTON, CT 06111"
                    state = None
                    line2 = _safe_get(addr, "line2") or ""
                    if "," in line2:
                        parts = line2.split(",")
                        state_zip = parts[-1].strip().split()
                        state = state_zip[0] if state_zip else None

                    return await _save(CallsignLookupResult(
                        callsign=_safe_get(current, "callsign") or callsign,
                        status="found",
                        name=name,
                        license_class=_safe_get(current, "operClass") or None,
                        state=state,
                        grid=_safe_get(loc, "gridsquare") or None,
                        expires=_safe_get(other, "expiryDate") or None,
                        source="callook.info",
                    ))
                else:
                    log.info("callook.info: status=%s for %s", data.get("status") if isinstance(data, dict) else data, callsign)
            else:
                log.warning("callook.info HTTP %s for %s", r.status_code, callsign)
        except Exception as exc:
            log.warning("callook.info error for %s: %s", callsign, exc)

    log.warning("All sources exhausted for %s — returning not_found", callsign)
    return await _save(CallsignLookupResult(callsign=callsign, status="not_found"))
