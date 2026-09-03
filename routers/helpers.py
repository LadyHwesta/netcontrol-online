"""
Helper functions and constants shared by 2+ router modules — email/SMTP,
bot-protection (CAPTCHA), the generic settings store, org bootstrap, and
the net/session access-control helpers. Anything used by only one router
lives directly in that router file instead (see the helper-usage mapping
done for the main.py split for the full single-vs-shared breakdown).
"""

import hashlib
import json
import logging
import os
import pathlib
import re
import secrets
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Checkin, Net, NetSession, NetShare, NetShareRole, Organization, OrganizationMembership, OrganizationMembershipRole, PushSubscription, StationRemark, SystemSetting, TacticalPosition, User, utcnow
from routers.schemas import NetOut

# ---------------------------------------------------------------------------
# App-wide paths
# ---------------------------------------------------------------------------
UPLOADS_DIR = pathlib.Path(__file__).parent.parent / "uploads"
STATIC_DIR = pathlib.Path(__file__).parent.parent / "static"

_LOGO_EXTS = ("png", "jpg", "jpeg", "gif", "webp", "svg")


def _org_logo_file(org_id: int) -> Optional[pathlib.Path]:
    """Return an org's own uploaded logo file if one exists (per-org branding,
    issue follow-up) -- same "glob the uploads dir by extension" shape as
    routers/orgs.py's instance-wide _logo_file(), just namespaced per org so
    each organization's logo lives alongside the shared one without
    colliding. Lives here (not orgs.py) since routers/public.py -- the
    org-scoped /directory and /live pages -- needs it too.
    Both this and routers/orgs.py's upload_logo()/_logo_file() share the
    "org_{id}_logo.{ext}" naming; keep in sync if that ever changes."""
    for ext in _LOGO_EXTS:
        p = UPLOADS_DIR / f"org_{org_id}_logo.{ext}"
        if p.exists():
            return p
    return None

# ---------------------------------------------------------------------------
# SMTP / Email config
# ---------------------------------------------------------------------------
SMTP_HOST     = os.getenv("SMTP_HOST", "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", "")        # e.g. "NetControl Online <noreply@example.com>"
SMTP_USE_TLS  = os.getenv("SMTP_USE_TLS", "true").lower() == "true"   # STARTTLS (port 587)
SMTP_USE_SSL  = os.getenv("SMTP_USE_SSL", "false").lower() == "true"  # SSL/TLS (port 465)
# smtplib has NO timeout by default (blocks forever on a dead/unreachable
# host) -- every email send happens synchronously inside a request handler,
# so a bad SMTP config wouldn't just fail an email, it'd hang that request
# (registration, approval, etc.) indefinitely instead of erroring quickly.
SMTP_TIMEOUT_SECONDS = int(os.getenv("SMTP_TIMEOUT_SECONDS", "10"))
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")    # e.g. https://netcontrol.example.org — used for links in emails

_email_log = logging.getLogger("ham_net_tracker.email")


def _smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def _app_url(path: str = "") -> Optional[str]:
    """Absolute link back to this app for use in emails. None if APP_BASE_URL isn't configured."""
    return f"{APP_BASE_URL}{path}" if APP_BASE_URL else None


def _public_base_url(request: Request) -> str:
    """APP_BASE_URL if configured (same convention as email links via
    _app_url()) so a reverse-proxied instance's public URL is reflected
    correctly; otherwise derived from the request itself."""
    return APP_BASE_URL or str(request.base_url).rstrip("/")


def send_email(
    to: list[str],
    subject: str,
    body_html: str,
    body_text: str = "",
    ics_content: str | None = None,
    ics_filename: str = "netcontrol.ics",
    reply_to: str | None = None,
) -> bool:
    """Send an HTML email, optionally with an ICS calendar attachment.
    Silently skips (logs warning) if SMTP is not configured. Returns whether
    the email was actually sent — most callers are fire-and-forget and ignore
    this, but it lets a caller like create_support_ticket report a real failure."""
    if not _smtp_configured():
        _email_log.debug("SMTP not configured — skipping email: %s", subject)
        return False
    if not to:
        return False

    from_addr = SMTP_FROM or SMTP_USER

    if ics_content:
        # multipart/mixed wraps alternative body + ics attachment
        outer = MIMEMultipart("mixed")
        outer["Subject"] = subject
        outer["From"]    = from_addr
        outer["To"]      = ", ".join(to)

        alt = MIMEMultipart("alternative")
        if body_text:
            alt.attach(MIMEText(body_text, "plain"))
        alt.attach(MIMEText(body_html, "html"))
        outer.attach(alt)

        ics_part = MIMEBase("text", "calendar", method="REQUEST", charset="UTF-8")
        ics_part.set_payload(ics_content.encode("utf-8"))
        ics_part["Content-Disposition"] = f'attachment; filename="{ics_filename}"'
        outer.attach(ics_part)
        msg = outer
    else:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = ", ".join(to)
        if body_text:
            msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

    if reply_to:
        msg["Reply-To"] = reply_to

    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as srv:
                srv.login(SMTP_USER, SMTP_PASSWORD)
                srv.sendmail(from_addr, to, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as srv:
                if SMTP_USE_TLS:
                    srv.starttls()
                srv.login(SMTP_USER, SMTP_PASSWORD)
                srv.sendmail(from_addr, to, msg.as_string())
        _email_log.info("Email sent to %s — %s", to, subject)
        return True
    except Exception as exc:
        _email_log.warning("Failed to send email to %s: %s", to, exc)
        return False


# ---------------------------------------------------------------------------
# Web push notifications (issue follow-up) — a second, app-native channel
# alongside the email reminders above, for "you're Net Control/Broadcaster
# soon" and, during an activation, "your rotation shift is starting soon".
# Opt-in, same "leave blank to disable" convention as SMTP/ALTCHA: with the
# three VAPID_* settings unset, GET /push/vapid-public-key 404s, the Account
# page's Notifications card hides itself, and send_reminders.py's own copy
# of _send_web_push (it never imports anything under routers/, see that
# file) silently skips the push half of each reminder.
# ---------------------------------------------------------------------------
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CONTACT_EMAIL = os.getenv("VAPID_CONTACT_EMAIL", "")

_push_log = logging.getLogger("ham_net_tracker.push")


def _vapid_configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_CONTACT_EMAIL)


async def _send_web_push(db: AsyncSession, user_id: int, title: str, body: str, url: str = "/") -> int:
    """Sends a push notification to every subscription this user has (one
    per browser/device — see PushSubscription's docstring). Best-effort:
    never raises. A subscription pywebpush reports as gone (404/410 — the
    browser revoked it, or the user cleared site data) is deleted rather
    than logged as a failure, since that's expected steady-state cleanup,
    not an error. Returns how many sends actually succeeded, mainly so
    POST /push/test can tell the caller "you have no active subscriptions"
    apart from "all of them failed"."""
    if not _vapid_configured():
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        _push_log.error("VAPID_* is set but the pywebpush package isn't installed — pip install pywebpush")
        return 0

    subs = (await db.execute(select(PushSubscription).filter(PushSubscription.user_id == user_id))).scalars().all()
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{VAPID_CONTACT_EMAIL}"},
            )
            sub.last_used_at = utcnow()
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                # Expected steady-state cleanup (the browser revoked it, or the
                # user cleared site data), not an error -- logged at INFO, not
                # WARNING, so it doesn't read as a false alarm months into
                # normal operation. Still logged (not silent) since this is
                # also exactly what a genuinely broken subscription looks like
                # right after subscribing, and that's otherwise undiagnosable:
                # without this line, POST /push/test's generic "no active
                # subscriptions" response looks identical whether there was
                # never a subscription at all, or one was just deleted here.
                _push_log.info("Push subscription for user %s reported gone (HTTP %s) -- removing it: %s", user_id, status, exc)
                await db.delete(sub)
            else:
                _push_log.warning("Push send failed for user %s: %s", user_id, exc)
        except Exception as exc:
            _push_log.warning("Push send failed for user %s: %s", user_id, exc)
    await db.commit()
    return sent


# ---------------------------------------------------------------------------
# Bot protection on registration/login — Cloudflare Turnstile, Google
# reCAPTCHA, or ALTCHA (open-source, self-contained proof-of-work)
# ---------------------------------------------------------------------------
# Exactly one provider is active at a time, chosen by CAPTCHA_PROVIDER
# ("turnstile" | "recaptcha" | "altcha"). Opt-in, same "leave blank to
# disable" convention as SMTP above — with it unset, no widget ever renders
# and no verification is required, so an existing deployment's
# registration/login flow is unaffected until an admin deliberately opts in.
CAPTCHA_PROVIDER = os.getenv("CAPTCHA_PROVIDER", "").strip().lower()

TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")      # public — safe to expose to the frontend
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")  # private — server-side verification only
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

RECAPTCHA_SITE_KEY = os.getenv("RECAPTCHA_SITE_KEY", "")      # public
RECAPTCHA_SECRET_KEY = os.getenv("RECAPTCHA_SECRET_KEY", "")  # private
RECAPTCHA_VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"

# ALTCHA (https://altcha.org, MIT-licensed) — a real open-source alternative
# to the two above: no third-party verification service at all, the app
# itself issues and checks a proof-of-work challenge with this HMAC key.
# Auto-generated at startup if not set via env — fine functionally (a
# restart just invalidates any challenge issued but not yet solved, a
# negligible race), but set it explicitly for a multi-worker/multi-instance
# deployment so every process agrees on the same key.
ALTCHA_HMAC_KEY = os.getenv("ALTCHA_HMAC_KEY", "") or secrets.token_hex(32)

_captcha_log = logging.getLogger("ham_net_tracker.captcha")


def _captcha_configured() -> bool:
    """Whether bot protection is actually usable right now — the selected
    provider AND its required credentials (where it needs any) are present."""
    if CAPTCHA_PROVIDER == "turnstile":
        return bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY)
    if CAPTCHA_PROVIDER == "recaptcha":
        return bool(RECAPTCHA_SITE_KEY and RECAPTCHA_SECRET_KEY)
    if CAPTCHA_PROVIDER == "altcha":
        return True  # self-contained — ALTCHA_HMAC_KEY always has a value
    return False


def _verify_turnstile(token: Optional[str], remote_ip: Optional[str]) -> bool:
    """Verifies a Turnstile response token with Cloudflare. Fails closed —
    any error (missing token, network failure, Cloudflare rejecting it)
    returns False, since a silent bypass would defeat the point."""
    if not token:
        return False
    try:
        payload = {"secret": TURNSTILE_SECRET_KEY, "response": token}
        if remote_ip:
            payload["remoteip"] = remote_ip
        resp = httpx.post(TURNSTILE_VERIFY_URL, data=payload, timeout=10)
        resp.raise_for_status()
        return bool(resp.json().get("success"))
    except Exception as exc:
        _captcha_log.warning("Turnstile verification failed: %s", exc)
        return False


def _verify_recaptcha(token: Optional[str], remote_ip: Optional[str]) -> bool:
    """Verifies a reCAPTCHA response token with Google. Same fail-closed
    shape as Turnstile above — the two APIs are near-identical."""
    if not token:
        return False
    try:
        payload = {"secret": RECAPTCHA_SECRET_KEY, "response": token}
        if remote_ip:
            payload["remoteip"] = remote_ip
        resp = httpx.post(RECAPTCHA_VERIFY_URL, data=payload, timeout=10)
        resp.raise_for_status()
        return bool(resp.json().get("success"))
    except Exception as exc:
        _captcha_log.warning("reCAPTCHA verification failed: %s", exc)
        return False


def _verify_altcha(token: Optional[str], remote_ip: Optional[str]) -> bool:
    """Verifies an ALTCHA solution payload against ALTCHA_HMAC_KEY — no
    network call at all, unlike the two providers above. remote_ip is
    accepted only so all three verifiers share one call signature; ALTCHA's
    protocol doesn't use it."""
    if not token:
        return False
    try:
        import altcha
        ok, err = altcha.verify_solution_v1(token, ALTCHA_HMAC_KEY, check_expires=True)
        if not ok:
            _captcha_log.info("ALTCHA verification failed: %s", err)
        return bool(ok)
    except ImportError:
        _captcha_log.error("CAPTCHA_PROVIDER=altcha but the altcha package isn't installed — pip install altcha")
        return False
    except Exception as exc:
        _captcha_log.warning("ALTCHA verification error: %s", exc)
        return False


def _verify_captcha(token: Optional[str], remote_ip: Optional[str]) -> bool:
    """Dispatches to whichever provider CAPTCHA_PROVIDER selects. Callers
    already check _captcha_configured() first; this returns False (fail
    closed) if somehow called with nothing configured."""
    if CAPTCHA_PROVIDER == "turnstile":
        return _verify_turnstile(token, remote_ip)
    if CAPTCHA_PROVIDER == "recaptcha":
        return _verify_recaptcha(token, remote_ip)
    if CAPTCHA_PROVIDER == "altcha":
        return _verify_altcha(token, remote_ip)
    return False


# ---------------------------------------------------------------------------
# Generic settings store (System Setting key/value rows)
# ---------------------------------------------------------------------------

async def _get_setting(key: str, db: AsyncSession) -> Optional[str]:
    row = (await db.execute(select(SystemSetting).filter(SystemSetting.key == key))).scalar_one_or_none()
    return row.value if row else None


async def _set_setting(key: str, value: Optional[str], db: AsyncSession):
    row = (await db.execute(select(SystemSetting).filter(SystemSetting.key == key))).scalar_one_or_none()
    if row:
        row.value = value
        row.updated_at = utcnow()
    else:
        db.add(SystemSetting(key=key, value=value))


# ---------------------------------------------------------------------------
# Optional Redis-backed cache (multi-worker DMR/APRS relay-push caches --
# TECH_DEBT.md, resolved)
# ---------------------------------------------------------------------------
# Shared by routers/digital_voice.py and routers/aprs.py's push caches, which
# are each already a two-tier in-memory-dict + SystemSetting fallback (see
# their own module comments) -- that pair is correct for a single uvicorn
# worker, but each worker's in-memory dict is private to that process, so a
# push landing on worker 1 isn't visible to a request served by worker 2
# until the SystemSetting fallback catches it, which only happens on that
# worker's own next cache miss. Redis, when configured, becomes a third tier
# consulted FIRST on read and always written on write -- the one tier that's
# actually shared and correctly fresh across every worker. Entirely additive:
# with REDIS_URL unset (the default), _get_redis_client() returns None
# immediately and every call here is a no-op, so behavior for a single
# worker is unchanged -- this is why the existing in-memory-dict + DB
# fallback stays in place underneath rather than being replaced.
REDIS_URL = os.getenv("REDIS_URL")
# Which logical Redis database (Redis's own SELECT n) this instance uses --
# .env-configurable (issue follow-up) since deploy.sh already supports
# several independent instances (main/testing/demo) sharing one server, and
# by extension nothing stops them sharing one Redis server too. Always the
# final word on the db index, overriding whatever (if anything) REDIS_URL's
# own path already says -- see _redis_url_with_db below.
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
# Second, independent layer of the same protection (issue follow-up): every
# key this app writes to Redis is namespaced by instance identity too, using
# the same SYSTEMD_SERVICE deploy.sh already requires to be unique per
# instance (for the systemd unit name and backup filenames) -- not a new
# setting, and not optional the way REDIS_DB is. This means two instances
# still can't collide even if REDIS_DB is left at the same value on both by
# mistake -- REDIS_DB is real isolation (separate keyspaces, inspectable
# separately via `redis-cli -n N`), this is a safety net under it, not a
# substitute for setting it correctly.
INSTANCE_KEY_PREFIX = os.getenv("SYSTEMD_SERVICE", "nettracker")
_redis_client = None  # created lazily on first actual use, not at import time


def _redis_url_with_db(base_url: str, db: int) -> str:
    """Returns base_url with its Redis DB index overridden to `db`. Only
    for the common redis://, rediss:// (and valkey equivalent) schemes,
    where the URL path IS the db index -- other schemes (unix sockets,
    sentinel, cluster, which don't support multiple logical databases the
    same way, or address the db differently) are returned unchanged; set
    the db directly in REDIS_URL for those instead."""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(base_url)
    if parts.scheme not in ("redis", "rediss", "valkey", "valkeys"):
        return base_url
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, parts.fragment))


def _get_redis_client():
    """Returns the shared async Redis client, or None if REDIS_URL isn't
    set. `redis` is only ever imported here, lazily, matching this app's
    usual optional-dependency pattern (e.g. _verify_altcha above) -- a
    deployment that never sets REDIS_URL never needs the package installed
    at all."""
    global _redis_client
    if not REDIS_URL:
        return None
    if _redis_client is None:
        try:
            import redis.asyncio as redis
        except ImportError:
            logging.getLogger("netcontrol.redis").error(
                "REDIS_URL is set but the redis package isn't installed — pip install redis"
            )
            return None
        _redis_client = redis.from_url(_redis_url_with_db(REDIS_URL, REDIS_DB), decode_responses=True)
    return _redis_client


async def _redis_cache_write(key: str, value: str, ttl_seconds: int) -> None:
    """Best-effort -- never raises. Redis here is purely a freshness
    optimization on top of the SystemSetting fallback that's already the
    real source of truth, so a Redis outage should degrade quietly back to
    single-worker-correct behavior, not break the request that triggered
    the write. `key` is namespaced with INSTANCE_KEY_PREFIX -- see above."""
    client = _get_redis_client()
    if client is None:
        return
    try:
        await client.set(f"{INSTANCE_KEY_PREFIX}:{key}", value, ex=ttl_seconds)
    except Exception as exc:
        logging.getLogger("netcontrol.redis").warning("Redis cache write failed for %s: %s", key, exc)


async def _redis_cache_read(key: str) -> Optional[str]:
    client = _get_redis_client()
    if client is None:
        return None
    key = f"{INSTANCE_KEY_PREFIX}:{key}"
    try:
        return await client.get(key)
    except Exception as exc:
        logging.getLogger("netcontrol.redis").warning("Redis cache read failed for %s: %s", key, exc)
        return None


# ---------------------------------------------------------------------------
# Organization bootstrap (issue #1 — multi-tenancy)
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "org"


async def _get_or_create_org(
    org_slug: Optional[str], org_name: Optional[str], org_website_url: Optional[str], db: AsyncSession,
    *, block_invite_only: bool = False,
) -> tuple[Organization, bool]:
    """Multi-tenancy (issue #1) join-or-create: resolves the org for a slug,
    creating it if it doesn't exist yet. Returns (org, created) — the caller
    uses `created` to decide whether this registrant becomes that org's
    (pending-super-admin-approval) admin or a pending member of an existing
    org instead (its own admin must approve). Omitting slug/name entirely
    resolves to the "default" org — the pre-multi-tenancy single-tenant
    bootstrap path: first-ever registration creates it, everyone after that
    requests to join it. A real "create a new org" request (org_slug or
    org_name given) requires a website URL so a super admin reviewing it has
    something to verify it against; the bare default-org bootstrap path does
    not, since nothing about it was actually requested by the caller.

    block_invite_only (issue follow-up): when True, resolving to an
    EXISTING org with registration_open=False raises 403 instead of
    returning it. Passed by the two genuinely self-service join paths
    (routers/auth.py's register(), routers/orgs.py's join_org()) — never by
    the admin-authenticated callers (routers/admin.py's admin_create_user()
    founding a brand new org on a super admin's behalf), for which joining
    a member into an invite-only org directly is the intended way in."""
    slug = org_slug or (_slugify(org_name) if org_name else "default")
    org = (await db.execute(select(Organization).filter(Organization.slug == slug))).scalar_one_or_none()
    if org:
        if block_invite_only and not org.registration_open:
            raise HTTPException(403, "This organization isn't accepting new registrations — contact an admin for an invite.")
        return org, False
    website = (org_website_url or "").strip()
    if org_slug or org_name:
        if not website:
            raise HTTPException(400, "Organization website URL is required when creating a new organization")
        # Restricted to http(s) — this gets rendered as a clickable link in the
        # admin approval queue, so anything else (e.g. a javascript: URI) would
        # be a stored-XSS vector against whoever reviews it.
        if not re.match(r"^https?://", website, re.IGNORECASE):
            raise HTTPException(400, "Organization website URL must start with http:// or https://")
    name = org_name or (await _get_setting("org_name", db) if slug == "default" else None) or slug.replace("-", " ").title()
    org = Organization(name=name, slug=slug, website_url=website or None)
    db.add(org)
    await db.flush()
    return org, True


async def _delete_orphaned_orgs(org_ids: set[int], db: AsyncSession) -> None:
    """Deletes any of the given orgs that now have zero memberships — call
    this AFTER deleting a user (with their org_ids captured beforehand),
    since a rejected/deleted user could have been an org's only member (most
    commonly its founder, still awaiting super admin approval). Otherwise
    the org is orphaned forever: it keeps showing up in the "join an
    existing organization" picker with no one left who could ever approve a
    join request (issue #1 follow-up)."""
    for org_id in org_ids:
        remaining = (await db.execute(
            select(func.count(OrganizationMembership.id)).filter(OrganizationMembership.org_id == org_id)
        )).scalar()
        if remaining == 0:
            org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
            if org:
                # Per-org branding logo (issue follow-up) has no DB column --
                # it's a bare file on disk (_org_logo_file) that db.delete(org)
                # below has no way to clean up on its own. Without this, a
                # deleted org's logo would leak on disk forever (org ids are
                # never reused once actually deleted, unlike a test DB's
                # row-wipe-between-tests -- see tests/conftest.py).
                for f in UPLOADS_DIR.glob(f"org_{org_id}_logo.*"):
                    f.unlink(missing_ok=True)
                await db.delete(org)


async def _create_invited_user(
    callsign: str, name: str, email: str, gmrs_callsign: Optional[str],
    org: Organization, role: str, db: AsyncSession,
) -> User:
    """Admin-seeds an operator account directly, into `org`, with an emailed
    invite link to set their own password (models.py's password_set_token
    doc comment) -- shared by POST /orgs/{id}/users (an org admin, always
    their own current org) and POST /admin/users (a super admin, any
    existing org or a brand new one -- issue follow-up). Auto-approved: the
    admin creating it IS the approval, so is_active is already True, but
    hashed_password is an unusable random placeholder until the invite link
    is followed. Requires SMTP to be configured -- otherwise the account
    would be created with no way to ever become usable. Raises
    HTTPException on a duplicate callsign/email, same as registration."""
    if not _smtp_configured():
        raise HTTPException(400, "Email must be configured (Admin → Email) before creating operator accounts this way — the invite link is sent by email.")
    if (await db.execute(select(User).filter(User.callsign == callsign))).scalar_one_or_none():
        raise HTTPException(400, "Callsign already registered")
    if (await db.execute(select(User).filter(User.email == email))).scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    from routers.auth import hash_password  # local import -- avoids a routers.auth <-> routers.helpers cycle

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    user = User(
        callsign=callsign,
        name=name,
        email=email,
        gmrs_callsign=gmrs_callsign,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        is_active=True,
        is_admin=False,
        email_verified=True,   # vouched for by the admin who entered it
        current_org_id=org.id,
        password_set_token=token_hash,
        password_set_sent_at=utcnow(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    db.add(OrganizationMembership(org_id=org.id, user_id=user.id, role=role, approved=True))
    await db.commit()

    set_link = _app_url(f"/?setpw={raw_token}")
    send_email(
        to=[user.email],
        subject=f"[NetControl Online] You've Been Added to {org.name}",
        body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">Welcome to {org.name}</h2>
  <p>Hello <strong>{user.name}</strong> ({user.callsign}),</p>
  <p>An administrator has created an account for you on NetControl Online, part of <strong>{org.name}</strong>. Set a password to get started:</p>
  {f'<p style="margin-top:16px"><a href="{set_link}" style="background:#FF9900;color:#000;padding:10px 20px;text-decoration:none;border-radius:20px;font-weight:bold;display:inline-block">Set Your Password</a></p>' if set_link else '<p>Contact your administrator for a link to set your password.</p>'}
  <p style="color:#888;font-size:12px">If you weren't expecting this, please disregard this message.</p>
</div>""",
        body_text=(
            f"Hello {user.name} ({user.callsign}),\n\n"
            f"An administrator has created an account for you on NetControl Online, part of {org.name}. "
            f"Set a password to get started:\n\n"
            + (f"{set_link}\n\n" if set_link else "Contact your administrator for a link to set your password.\n\n")
            + "If you weren't expecting this, please disregard this message."
        ),
    )
    return user


# ---------------------------------------------------------------------------
# Ham-only integration guard (DMR, APRS — issue #22/#26)
# ---------------------------------------------------------------------------

def _assert_ham_net(net: Net):
    """Raise 400 if the net is GMRS — shared by every ham-only integration
    (DMR, APRS — issue #22) since GMRS has no allocation for either."""
    if net and net.net_type == "gmrs":
        raise HTTPException(400, "This integration is not available for GMRS nets")


# ---------------------------------------------------------------------------
# Callsign display-name / tactical-callsign lookups
# (used by Checkins, Session summary & ICS-205, History/CSV Export)
# ---------------------------------------------------------------------------

async def _preferred_names_for_net(net_id: int, db: AsyncSession) -> dict:
    """callsign -> preferred_name for every station remark on this net that has one set."""
    rows = (await db.execute(
        select(StationRemark.callsign, StationRemark.preferred_name)
        .filter(StationRemark.net_id == net_id, StationRemark.preferred_name.isnot(None))
    )).all()
    return {r.callsign: r.preferred_name for r in rows}


async def _tactical_callsigns_for_session(session_id: int, db: AsyncSession) -> dict:
    """tactical_position_id -> tactical_callsign for this session's positions
    (issue #21) — avoids an N+1 lookup per checkin row in list_checkins()."""
    rows = (await db.execute(
        select(TacticalPosition.id, TacticalPosition.tactical_callsign)
        .filter(TacticalPosition.session_id == session_id)
    )).all()
    return {r.id: r.tactical_callsign for r in rows}


async def _tactical_callsigns_for_net(net_id: int, db: AsyncSession) -> dict:
    """Same as _tactical_callsigns_for_session, but across every session on
    this net — for the multi-session net-wide CSV export."""
    rows = (await db.execute(
        select(TacticalPosition.id, TacticalPosition.tactical_callsign)
        .join(NetSession, NetSession.id == TacticalPosition.session_id)
        .filter(NetSession.net_id == net_id)
    )).all()
    return {r.id: r.tactical_callsign for r in rows}


async def _net_has_prior_checkin_history(net_id: int, exclude_session_id: int, db: AsyncSession) -> bool:
    """True if this net has at least one checkin in a session other than
    exclude_session_id (issue follow-up) -- distinguishes a genuinely new
    face on an established net from the trivial case of a brand-new net's
    very first session, where Checkin.is_first_checkin is True for every
    single row simply because there's no history yet to compare against.
    Powers the frontend's "👋 welcome new folks" banner (SessionOut.
    net_has_history) so it doesn't fire as "welcome" for an entire roster on
    day one."""
    return bool((await db.execute(
        select(Checkin.id)
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id, NetSession.id != exclude_session_id)
        .limit(1)
    )).scalar())


# ---------------------------------------------------------------------------
# Role revamp (issue follow-up) — org-level roles + net-level grants.
#
# Four canonical role names are used everywhere (frontend, API, net-level
# grants): "admin", "net_control_op", "tactical_operator", "broadcaster".
# OrganizationMembership.role itself keeps its original two DB values
# ('admin' | 'member') unchanged -- no data migration needed -- and is just
# *displayed*/translated as "net_control_op" everywhere else; the two new
# roles (tactical_operator/broadcaster) are additional and multi-valued, so
# they live in the separate OrganizationMembershipRole table instead of
# becoming more values of that single column. A membership's full canonical
# role set is {ORG_ROLE_DISPLAY[role]} | {its extra_roles}.
#
# At the net level, "net_control_op" is still exactly NetShare.can_edit (full
# access already implies every other role); "tactical_operator" and
# "broadcaster" are additional per-share grants in NetShareRole, each only
# ever offerable for a role the target user's org membership already holds
# (enforced in routers/nets.py's update_net_shares, not by a DB constraint,
# since that check spans both tables).
# ---------------------------------------------------------------------------
ORG_ROLE_DISPLAY = {"admin": "admin", "member": "net_control_op"}
SELF_REQUESTABLE_ROLES = ("net_control_op", "tactical_operator", "broadcaster")  # never "admin"
NET_EXTRA_ROLES = ("tactical_operator", "broadcaster")  # NetShareRole values; net_control_op is can_edit


async def _org_role_set(org_id: int, user_id: int, db: AsyncSession) -> set[str]:
    """Every canonical role name an APPROVED membership holds in this org --
    empty set if not a member (or not yet approved). Used to decide which
    extra roles are actually offerable when sharing a net with this user."""
    membership = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id,
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.approved == True,
    ))).scalar_one_or_none()
    if not membership:
        return set()
    roles = {ORG_ROLE_DISPLAY.get(membership.role, membership.role)}
    extra = (await db.execute(select(OrganizationMembershipRole.role).filter(
        OrganizationMembershipRole.membership_id == membership.id
    ))).scalars().all()
    roles.update(extra)
    return roles


# ---------------------------------------------------------------------------
# Net/session access control (used by nearly every router)
# ---------------------------------------------------------------------------

async def _net_to_out(net: Net, user: User, db: AsyncSession) -> NetOut:
    """Build a NetOut with sharing metadata attached."""
    shares = (await db.execute(select(NetShare).filter(NetShare.net_id == net.id))).scalars().all()
    owner = (await db.execute(select(User).filter(User.id == net.owner_id))).scalar_one_or_none()
    all_share = next((s for s in shares if s.user_id is None), None)
    out = NetOut.model_validate(net)
    out.is_owner = (net.owner_id == user.id or user.is_admin)
    out.shared_with_all = all_share is not None
    out.can_edit_all = bool(all_share and all_share.can_edit)
    out.shared_user_ids = [s.user_id for s in shares if s.user_id is not None]
    out.editor_user_ids = [s.user_id for s in shares if s.user_id is not None and s.can_edit]
    my_share = next((s for s in shares if s.user_id == user.id), None)
    out.can_edit = out.is_owner or out.can_edit_all or bool(my_share and my_share.can_edit)
    out.owner_callsign = owner.callsign if owner else None
    return out


async def _get_owned_net(net_id: int, user: User, db: AsyncSession) -> Net:
    """Fetch a net; require owner or admin. Non-admins are further scoped to
    their current org (issue #1) — a net in a different org 404s rather than
    403s, so its existence isn't leaked across tenants. Super admins bypass
    org scoping entirely, same as they already bypass ownership. Deliberately
    NOT satisfied by an edit-rights share (see _get_editable_net below) —
    reserved for destructive/sensitive actions: deleting the net, and
    managing sharing itself (an editor granting themselves or others further
    access would be a privilege-escalation chain)."""
    net = (await db.execute(select(Net).filter(Net.id == net_id))).scalar_one_or_none()
    if not net:
        raise HTTPException(404, "Net not found")
    if user.is_admin:
        return net
    if net.org_id != user.current_org_id:
        raise HTTPException(404, "Net not found")
    if net.owner_id != user.id:
        raise HTTPException(403, "Not your net")
    return net


async def _get_editable_net(net_id: int, user: User, db: AsyncSession) -> Net:
    """Like _get_owned_net, but also allows a user explicitly granted edit
    rights via sharing (NetShare.can_edit) — issue follow-up: previously
    sharing only ever granted view/check-in access, with no way to let a
    trusted co-operator help maintain a net's schedule, DMR config, evac
    zones, etc. without handing them full ownership. Used for exactly that
    kind of net-configuration endpoint; delete_net and the sharing endpoints
    themselves stay on the stricter _get_owned_net."""
    try:
        return await _get_owned_net(net_id, user, db)
    except HTTPException as e:
        if e.status_code == 403:
            # A net can legitimately have BOTH an individual share for this
            # user AND a separate share-with-all row (user_id IS NULL) at
            # the same time -- this is an existence check, not a unique
            # lookup, so .limit(1) + .scalars().first() rather than
            # scalar_one_or_none(), which would raise if both exist.
            share = (await db.execute(select(NetShare).filter(
                NetShare.net_id == net_id,
                NetShare.can_edit == True,
                or_(NetShare.user_id == user.id, NetShare.user_id == None),
            ).limit(1))).scalars().first()
            if share:
                return (await db.execute(select(Net).filter(Net.id == net_id))).scalar_one_or_none()
        raise


async def _get_net_for_user(net_id: int, user: User, db: AsyncSession) -> Net:
    """Fetch a net; allow owner, admin, or user the net is shared with.
    Org-scoped for non-admins the same way as _get_owned_net above."""
    net = (await db.execute(select(Net).filter(Net.id == net_id))).scalar_one_or_none()
    if not net:
        raise HTTPException(404, "Net not found")
    if user.is_admin:
        return net
    if net.org_id != user.current_org_id:
        raise HTTPException(404, "Net not found")
    if net.owner_id == user.id:
        return net
    # Check shares: shared with all (user_id IS NULL) or shared with this user
    # -- both rows can legitimately exist at once for the same net, so this
    # is an existence check (.limit(1) + .scalars().first()), not a unique
    # lookup; scalar_one_or_none() would raise if both do.
    share = (
        (await db.execute(select(NetShare).filter(
            NetShare.net_id == net_id,
            or_(NetShare.user_id == user.id, NetShare.user_id == None),
        ).limit(1))).scalars().first()
    )
    if not share:
        raise HTTPException(403, "Access denied")
    return net


async def _get_net_for_role(net_id: int, user: User, db: AsyncSession, role: str) -> Net:
    """Fetch a net; require owner/org-admin, a full-edit (net_control_op)
    share -- which already implies every other role -- or a NetShare
    carrying this specific extra role (tactical_operator/broadcaster, see
    NET_EXTRA_ROLES). Used by the self-service tactical/broadcaster
    endpoints (issue follow-up); net_control_op access itself is still just
    _get_editable_net, unchanged."""
    net = await _get_net_for_user(net_id, user, db)  # raises 403/404; also org-scopes
    if net.owner_id == user.id or user.is_admin:
        return net
    shares = (await db.execute(select(NetShare).filter(
        NetShare.net_id == net_id, or_(NetShare.user_id == user.id, NetShare.user_id == None),
    ))).scalars().all()
    if any(s.can_edit for s in shares):
        return net  # full access implies every role
    if role in NET_EXTRA_ROLES and shares:
        share_ids = [s.id for s in shares]
        has_role = (await db.execute(select(NetShareRole.id).filter(
            NetShareRole.net_share_id.in_(share_ids), NetShareRole.role == role,
        ).limit(1))).scalar()
        if has_role:
            return net
    raise HTTPException(403, f"The {role} role is required on this net")


async def _get_session_for_user(session_id: int, user: User, db: AsyncSession) -> NetSession:
    session = (await db.execute(select(NetSession).filter(NetSession.id == session_id))).scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    await _get_net_for_user(session.net_id, user, db)
    return session
