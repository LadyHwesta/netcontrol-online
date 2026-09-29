"""
Default admin account seeding (issue follow-up) -- closes a real security
race in the old bootstrap: previously, the FIRST person to register on a
freshly deployed instance became its super admin automatically
(routers/auth.py's register(), is_first_user), active immediately, no
approval. On an internet-reachable install, that's whoever gets there
first -- not necessarily the installer.

ensure_default_admin() runs once at every app startup (main.py's
lifespan, right after init_db()) and creates a reserved admin account with
a freshly generated password IF the instance has no admin at all yet --
not "if this is a genuinely fresh install," but "this instance always has
at least one admin," so it also self-heals an instance that somehow ends
up admin-less later. Once that account exists, register()'s own
is_first_user path is never reachable again in practice (its own logic is
untouched -- see that function's own comment) -- this is a pure ordering
fix, not a rewrite of registration.

A flat top-level module, not under routers/ -- same precedent as
net_repository.py/send_reminders.py/demo_reset.py for a standalone concern
that isn't one HTTP domain's routes.
"""

import logging
import os
import secrets
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import User

_log = logging.getLogger("ham_net_tracker.bootstrap")

SEED_DEFAULT_ADMIN = os.getenv("SEED_DEFAULT_ADMIN", "true").strip().lower() == "true"
DEFAULT_ADMIN_CALLSIGN = os.getenv("DEFAULT_ADMIN_CALLSIGN", "ADMIN").strip() or "ADMIN"
# Unset by default -- a random password is generated and logged. Set this to
# pin a known password instead (deployment automation, or the test suite's
# opt-in coverage of this function -- see tests/test_default_admin.py).
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD") or None
DEFAULT_ADMIN_EMAIL = "admin@localhost"


async def ensure_default_admin(db: AsyncSession) -> Optional[User]:
    """Creates the reserved default admin account if this instance has no
    admin at all yet. Returns the created User, or None if it was a no-op
    (seeding disabled, an admin already exists, or a collision was found).
    Never raises -- a seeding problem should never block the app from
    starting."""
    if not SEED_DEFAULT_ADMIN:
        return None

    existing_admin_count = (await db.execute(
        select(func.count()).select_from(User).filter(User.is_admin == True)
    )).scalar()
    if existing_admin_count:
        return None

    # Defensive: on an instance upgraded from an older version, some row
    # could already hold the reserved callsign/email (extremely unlikely --
    # ham/GMRS callsigns never look like "ADMIN" -- but User.callsign/email
    # are both UNIQUE, so an unguarded insert would raise IntegrityError and
    # crash startup rather than just skip). Log and bail instead.
    conflict = (await db.execute(select(User.id).filter(
        (User.callsign == DEFAULT_ADMIN_CALLSIGN) | (User.email == DEFAULT_ADMIN_EMAIL)
    ))).scalar_one_or_none()
    if conflict:
        _log.warning(
            "No admin exists on this instance, but the reserved default-admin "
            "callsign/email (%s / %s) is already taken by user id=%s -- skipping "
            "default admin seeding. Set DEFAULT_ADMIN_CALLSIGN to a different "
            "value, or grant is_admin to an existing account by hand.",
            DEFAULT_ADMIN_CALLSIGN, DEFAULT_ADMIN_EMAIL, conflict,
        )
        return None

    from routers.auth import hash_password  # local import -- avoids a bootstrap <-> routers.auth import-order concern at module load

    password = DEFAULT_ADMIN_PASSWORD or secrets.token_urlsafe(16)

    user = User(
        callsign=DEFAULT_ADMIN_CALLSIGN,
        name="System Administrator",
        email=DEFAULT_ADMIN_EMAIL,
        hashed_password=hash_password(password),
        is_active=True,
        is_admin=True,
        email_verified=True,
        current_org_id=None,   # platform-level account -- not tied to any one org
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # WARNING (not INFO) so this is never filtered out regardless of
    # LOG_LEVEL, and stands out from routine startup lines -- main.py's
    # StreamHandler is always attached, so this reaches stdout/the systemd
    # journal unconditionally, the only realistic channel an installer with
    # no logged-in account yet can retrieve it from (confirmed no existing
    # "write a credentials file" precedent in this codebase, and a plaintext
    # file lingering on disk indefinitely would be a worse trade-off than a
    # one-time log line). Only ever printed once, the moment this account is
    # created -- never again on a later restart.
    _log.warning(
        "\n"
        + "=" * 64 + "\n"
        + "  NetControl Online -- default admin account created\n"
        + "\n"
        + f"    Callsign: {DEFAULT_ADMIN_CALLSIGN}\n"
        + f"    Password: {password}\n"
        + "\n"
        + "  Log in now and change the password (and email) under Account.\n"
        + "  This message will not be shown again.\n"
        + "=" * 64
    )
    return user
