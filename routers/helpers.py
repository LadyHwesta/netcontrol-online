"""
Helper functions and constants shared by 2+ router modules — email/SMTP,
bot-protection (CAPTCHA), the generic settings store, org bootstrap, and
the net/session access-control helpers. Anything used by only one router
lives directly in that router file instead (see the helper-usage mapping
done for the main.py split for the full single-vs-shared breakdown).
"""

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

from models import Net, NetSession, NetShare, Organization, OrganizationMembership, StationRemark, SystemSetting, TacticalPosition, User, utcnow
from routers.schemas import NetOut

# ---------------------------------------------------------------------------
# App-wide paths
# ---------------------------------------------------------------------------
UPLOADS_DIR = pathlib.Path(__file__).parent.parent / "uploads"
STATIC_DIR = pathlib.Path(__file__).parent.parent / "static"

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
# Organization bootstrap (issue #1 — multi-tenancy)
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "org"


async def _get_or_create_org(
    org_slug: Optional[str], org_name: Optional[str], org_website_url: Optional[str], db: AsyncSession,
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
    not, since nothing about it was actually requested by the caller."""
    slug = org_slug or (_slugify(org_name) if org_name else "default")
    org = (await db.execute(select(Organization).filter(Organization.slug == slug))).scalar_one_or_none()
    if org:
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
                await db.delete(org)


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


async def _get_session_for_user(session_id: int, user: User, db: AsyncSession) -> NetSession:
    session = (await db.execute(select(NetSession).filter(NetSession.id == session_id))).scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    await _get_net_for_user(session.net_id, user, db)
    return session
