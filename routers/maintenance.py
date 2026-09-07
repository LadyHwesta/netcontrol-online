"""
Maintenance notice — a temporary "an update is coming" banner for the
nightly deploy cron job (see "Nightly deploy" in README.md).

State is a plain in-memory module global, not a database row, on purpose:
the whole point of the banner is to disappear once the update has actually
landed, and the update lands via a systemd restart of this exact process —
which wipes an in-memory global back to its default for free. No explicit
"clear" step is needed anywhere in deploy.sh. A DB-backed flag would need
one, and would keep showing the stale banner across the very restart it's
supposed to be announcing.

If the deploy never happens (e.g. deploy.sh's --skip-if-unchanged found
nothing new and skipped the restart), _EXPIRES_AFTER below still bounds how
long the banner lingers, so it's never stuck on indefinitely.
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException

router = APIRouter()

DEPLOY_NOTICE_SECRET = os.getenv("DEPLOY_NOTICE_SECRET", "")

# 30 min heads-up + generous headroom for a slow night's backup/pip
# install/pytest run before the actual restart -- see deploy.sh's own
# comment on why the cron job that arms this fires 30 minutes early.
_EXPIRES_AFTER = timedelta(minutes=90)

_armed_until: datetime | None = None


@router.post("/internal/maintenance-notice", include_in_schema=False)
async def arm_maintenance_notice(x_deploy_secret: str = Header(default="")):
    """Called by cron ~30 minutes before the nightly deploy. Not user-facing
    and not tied to any org/user account -- gated by a shared secret instead,
    since the caller is a cron job, not a logged-in browser. Disabled
    entirely (404s) unless DEPLOY_NOTICE_SECRET is set, so instances that
    don't use this stay exactly as they were before this endpoint existed."""
    if not DEPLOY_NOTICE_SECRET:
        raise HTTPException(404)
    if x_deploy_secret != DEPLOY_NOTICE_SECRET:
        raise HTTPException(403)
    global _armed_until
    _armed_until = datetime.now(timezone.utc) + _EXPIRES_AFTER
    return {"armed": True}


@router.get("/maintenance-notice", include_in_schema=False)
async def get_maintenance_notice():
    """Polled by every open tab (see static/js/utils.js) to show/hide the
    banner. Deliberately public/unauthenticated -- a scheduled-maintenance
    notice is meant for every visitor, logged in or not."""
    active = _armed_until is not None and datetime.now(timezone.utc) < _armed_until
    return {
        "active": active,
        "message": "A scheduled update is starting soon. You may need to reload the page once it's finished.",
    }
