"""
Push a net listing to the central Net Repository directory
(https://github.com/LadyHwesta/Net-Repository) — the "discover" side of
issue #12/#8. Shared by main.py (push on create/update, and the Admin >
Net Repository self-service key request UI) and the standalone
push_to_net_repository.py backfill script, so the request-building logic
lives in exactly one place.

Environment variables (read from .env)
---------------------------------------
    NET_REPOSITORY_URL        Base URL of the Net Repository instance, e.g.
                               https://mynetrepo.meskis.net. Required either
                               way — there's no way to request or use a key
                               without knowing which instance to talk to.
    NET_REPOSITORY_API_KEY    nr_-prefixed API key. Optional: takes precedence
                               if set, but a key obtained via the self-service
                               "Request API Key" flow in Admin > Net Repository
                               (stored in the database, not .env) works just
                               as well — see get_api_key().

Re-submitting the same net (matched by source_net_id) updates the existing
entry on Net Repository's side — in place if it's still pending review, or
directly (no new moderation) if it was already published. So every local
edit to a public-listed net just needs another POST /nets/submit; there's
nothing extra to do here to keep the two in sync.

Async: converted alongside main.py's sync->async SQLAlchemy migration.
push_net() deliberately queries the owner/schedules explicitly via `db`
rather than touching net.owner/net.schedules relationship attributes --
those are lazy-loaded by default, which raises MissingGreenlet under
AsyncSession unless already eager-loaded by the caller's own query.
Explicit queries here mean callers don't need to think about eager-loading
at all.
"""

import logging
import os
from typing import Optional

import httpx
from sqlalchemy import select

_log = logging.getLogger("ham_net_tracker.net_repository")

NET_REPOSITORY_URL = os.getenv("NET_REPOSITORY_URL", "").rstrip("/")
NET_REPOSITORY_API_KEY = os.getenv("NET_REPOSITORY_API_KEY", "")

_SETTING_API_KEY = "net_repository_api_key"
_SETTING_CLAIM_TOKEN = "net_repository_claim_token"
_SETTING_REQUEST_STATUS = "net_repository_key_request_status"


# ---------------------------------------------------------------------------
# SystemSetting helpers (duplicated from main.py's _get_setting/_set_setting —
# deliberately tiny and self-contained rather than importing from main.py,
# matching this repo's existing convention of standalone scripts/modules not
# depending on main.py; see send_reminders.py)
# ---------------------------------------------------------------------------

async def _get_setting(key: str, db) -> Optional[str]:
    from models import SystemSetting
    row = (await db.execute(select(SystemSetting).filter(SystemSetting.key == key))).scalar_one_or_none()
    return row.value if row else None


async def _set_setting(key: str, value: Optional[str], db) -> None:
    from models import SystemSetting
    row = (await db.execute(select(SystemSetting).filter(SystemSetting.key == key))).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=key, value=value))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

async def get_api_key(db) -> str:
    """NET_REPOSITORY_API_KEY env var takes precedence (explicit operator
    override); otherwise falls back to a key obtained via the self-service
    request flow (Admin > Net Repository), stored in the database."""
    if NET_REPOSITORY_API_KEY:
        return NET_REPOSITORY_API_KEY
    return await _get_setting(_SETTING_API_KEY, db) or ""


async def net_repository_configured(db) -> bool:
    return bool(NET_REPOSITORY_URL) and bool(await get_api_key(db))


async def get_key_source(db) -> Optional[str]:
    """'env' if NET_REPOSITORY_API_KEY is set, 'self-service' if a key was
    obtained via request_api_key()/check_key_request_status(), else None."""
    if NET_REPOSITORY_API_KEY:
        return "env"
    return "self-service" if await _get_setting(_SETTING_API_KEY, db) else None


async def get_request_status(db) -> str:
    """Last-known status of a self-service key request: 'none' | 'pending' |
    'claimed' | 'rejected'."""
    return await _get_setting(_SETTING_REQUEST_STATUS, db) or "none"


# ---------------------------------------------------------------------------
# Push a net
# ---------------------------------------------------------------------------

async def push_net(net, db) -> bool:
    """POST a net to Net Repository's submission queue. Returns True if the
    request was sent and accepted (202 — including the already-submitted/
    duplicate case), False if not configured, the net isn't public, or the
    request failed. Never raises — a Net Repository outage or misconfiguration
    must not block creating or editing a net locally."""
    if not await net_repository_configured(db):
        return False
    if not net.public_listed:
        return False

    from models import User, NetSchedule

    owner = (await db.execute(select(User).filter(User.id == net.owner_id))).scalar_one_or_none()
    owner_callsign = owner.callsign if owner else None
    schedule_rows = (await db.execute(select(NetSchedule).filter(NetSchedule.net_id == net.id))).scalars().all()

    payload = {
        "name": net.name,
        "net_type": net.net_type,
        "frequency": net.frequency,
        "band": net.band,
        "mode": net.mode,
        "ctcss_tone": net.ctcss_tone,
        "description": net.description,
        "region": net.region,
        "state": net.state,
        "country": "US",
        "website": net.website or await _get_setting("website_url", db),
        "dmr_talkgroup": net.dmr_talkgroup,
        "is_ares": net.is_ares,
        "contact_callsign": owner_callsign,
        "submitted_by_callsign": owner_callsign,
        "source_net_id": net.id,
        "schedules": [
            {
                "day_of_week": s.day_of_week,
                "start_time": s.start_time,
                "timezone": s.timezone,
            }
            for s in schedule_rows
        ],
    }
    try:
        # Deliberately still a plain sync httpx.post, not AsyncClient -- matches
        # every other httpx call site in this codebase (DMR/APRS fetch, Turnstile/
        # reCAPTCHA verify), all left sync-blocking-in-the-event-loop as a known,
        # disclosed follow-up rather than part of this DB-focused async migration.
        # Also keeps the existing pushed_nets test fixture (monkeypatches the
        # module-level httpx.post) working unchanged.
        resp = httpx.post(
            f"{NET_REPOSITORY_URL}/nets/submit",
            json=payload,
            headers={"Authorization": f"Bearer {await get_api_key(db)}"},
            timeout=10,
        )
        resp.raise_for_status()
        message = resp.json().get("message", "")
        _log.info("Pushed net %r (id=%s) to Net Repository: %s", net.name, net.id, message)
        return True
    except Exception as exc:
        _log.warning("Failed to push net %r (id=%s) to Net Repository: %s", net.name, net.id, exc)
        return False


async def push_session_stats(net, session, checkin_count: int, db) -> bool:
    """POST a closed session's stats to Net Repository (POST /nets/stats) —
    session count, average check-ins, and last-session date roll up into
    that net's public directory listing there. Only meaningful for a net
    already published on Net Repository's side (matched by source_net_id,
    same as push_net()) — a 404 there just means it hasn't been approved
    yet, which is expected and logged quietly rather than as a warning.
    Never raises — a Net Repository outage or misconfiguration must not
    block ending a session locally."""
    if not await net_repository_configured(db):
        return False
    if not net.public_listed:
        return False

    payload = {
        "source_net_id": net.id,
        "checkin_count": checkin_count,
        "session_date": session.started_at.isoformat(),
    }
    try:
        resp = httpx.post(
            f"{NET_REPOSITORY_URL}/nets/stats",
            json=payload,
            headers={"Authorization": f"Bearer {await get_api_key(db)}"},
            timeout=10,
        )
        if resp.status_code == 404:
            _log.info(
                "Skipped Net Repository session stats for net %r (id=%s) — not published there yet.",
                net.name, net.id,
            )
            return False
        resp.raise_for_status()
        _log.info("Logged session stats for net %r (id=%s) to Net Repository.", net.name, net.id)
        return True
    except Exception as exc:
        _log.warning("Failed to log session stats for net %r (id=%s) to Net Repository: %s", net.name, net.id, exc)
        return False


# ---------------------------------------------------------------------------
# Self-service API key requests
# ---------------------------------------------------------------------------

async def request_api_key(name: str, contact_callsign: Optional[str], instance_url: Optional[str],
                           request_notes: Optional[str], db) -> dict:
    """POST /keys/request on Net Repository. Stores the returned claim token
    (needed to poll for approval) in SystemSetting — it's never shown back to
    the admin, only used internally by check_key_request_status(). Returns
    {ok, message, request_id} — never raises."""
    if not NET_REPOSITORY_URL:
        return {"ok": False, "message": "NET_REPOSITORY_URL is not configured."}

    payload = {"name": name}
    if contact_callsign:
        payload["contact_callsign"] = contact_callsign
    if instance_url:
        payload["instance_url"] = instance_url
    if request_notes:
        payload["request_notes"] = request_notes

    try:
        resp = httpx.post(f"{NET_REPOSITORY_URL}/keys/request", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log.warning("Failed to request a Net Repository API key: %s", exc)
        return {"ok": False, "message": f"Request failed: {exc}"}

    await _set_setting(_SETTING_CLAIM_TOKEN, data["claim_token"], db)
    await _set_setting(_SETTING_REQUEST_STATUS, "pending", db)
    await db.commit()
    _log.info("Requested a Net Repository API key (request_id=%s)", data.get("request_id"))
    return {
        "ok": True,
        "message": data.get("message", "Request submitted for admin review."),
        "request_id": data.get("request_id"),
    }


async def check_key_request_status(db) -> dict:
    """Polls GET /keys/request/status using the stored claim token. If the
    request has been approved and claimed, stores the issued API key and
    clears the claim token (it's single-use on Net Repository's side once
    claimed, so there's nothing further to do with it). Returns
    {ok, status, message} — never raises. status is 'none' if there's no
    request on file (nothing ever requested, or already resolved)."""
    if not NET_REPOSITORY_URL:
        return {"ok": False, "status": "none", "message": "NET_REPOSITORY_URL is not configured."}

    claim_token = await _get_setting(_SETTING_CLAIM_TOKEN, db)
    if not claim_token:
        cached_status = await _get_setting(_SETTING_REQUEST_STATUS, db) or "none"
        return {"ok": True, "status": cached_status, "message": "No pending request."}

    try:
        resp = httpx.get(
            f"{NET_REPOSITORY_URL}/keys/request/status",
            headers={"Authorization": f"Bearer {claim_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log.warning("Failed to check Net Repository key request status: %s", exc)
        return {"ok": False, "status": "unknown", "message": f"Status check failed: {exc}"}

    status = data.get("status", "unknown")
    await _set_setting(_SETTING_REQUEST_STATUS, status, db)

    result = {"ok": True, "status": status, "message": data.get("message", "")}

    if status == "claimed":
        if data.get("api_key"):
            await _set_setting(_SETTING_API_KEY, data["api_key"], db)
            _log.info("Net Repository API key request approved — key received and saved.")
            # Net Repository only ever returns the raw key on the ONE poll that
            # claims it (like this one) — later polls report "claimed" with no
            # key. Hand it back to the caller this one time too, so the admin
            # gets a chance to see/copy it, same as any other API key/token in
            # this app. It is never stored anywhere in plaintext by this
            # function's caller beyond that single response.
            result["api_key"] = data["api_key"]
        # Claimed either just now (key present) or by an earlier poll (key
        # already saved then) — either way the claim token has no further use.
        await _set_setting(_SETTING_CLAIM_TOKEN, None, db)
    elif status == "rejected":
        await _set_setting(_SETTING_CLAIM_TOKEN, None, db)

    await db.commit()
    return result


async def clear_stored_key(db) -> None:
    """Admin action: forget the self-service key and any in-flight request,
    to start over. Does not affect NET_REPOSITORY_API_KEY if set via .env —
    that always takes precedence over the stored value regardless."""
    await _set_setting(_SETTING_API_KEY, None, db)
    await _set_setting(_SETTING_CLAIM_TOKEN, None, db)
    await _set_setting(_SETTING_REQUEST_STATUS, None, db)
    await db.commit()
