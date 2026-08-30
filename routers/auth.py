"""
Auth helpers + Auth routes + API Token management.
"""

import hashlib
import html
import logging
import logging.handlers
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import bcrypt as _bcrypt
import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, field_validator
from slowapi.util import get_remote_address
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import ApiToken, OrganizationMembership, User
from routers import helpers
from routers.deps import ALGORITHM, SECRET_KEY, get_current_user, limiter
from routers.helpers import _captcha_configured, _captcha_log, _get_or_create_org, _verify_captcha
from routers.schemas import UserOut

router = APIRouter()

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8 hours
VERIFICATION_TOKEN_TTL_DAYS = 7
PASSWORD_SET_TOKEN_TTL_DAYS = 14   # admin-created accounts' invite link (issue #1 follow-up) — longer than email verification since an operator may not check email daily

# ---------------------------------------------------------------------------
# Auth failure logger (for fail2ban)
# ---------------------------------------------------------------------------
AUTH_LOG_FILE = os.getenv("AUTH_LOG_FILE", "")   # e.g. /var/log/nettracker/auth.log

_auth_log = logging.getLogger("ham_net_tracker.auth")
if AUTH_LOG_FILE:
    _auth_handler = logging.handlers.WatchedFileHandler(AUTH_LOG_FILE)
    _auth_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    _auth_log.addHandler(_auth_handler)
_auth_log.setLevel(logging.WARNING)


def _log_auth_fail(request: Request, reason: str) -> None:
    ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown").split(",")[0].strip()
    _auth_log.warning("AUTH_FAIL ip=%s reason=%s", ip, reason)


class UserCreate(BaseModel):
    callsign: str
    name: str
    email: EmailStr
    password: str
    # Multi-tenancy (issue #1) — which organization to join, keyed by slug. If the
    # slug doesn't exist yet it's created (with org_name as its display name,
    # org_website_url required) and this user becomes its admin, pending a
    # super admin's approval before they can log in; if it already exists,
    # this creates a pending membership an admin of that org must approve
    # instead. Omitted entirely means "join-or-create the default org" —
    # preserves the old single-tenant bootstrap (first-ever registration
    # creates it and is immediately active; everyone after that requests to
    # join it).
    org_slug: Optional[str] = None
    org_name: Optional[str] = None
    org_website_url: Optional[str] = None
    captcha_token: Optional[str] = None  # bot-protection widget response, required only if CAPTCHA_PROVIDER is set

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        return v.upper().strip()


class ThemeUpdate(BaseModel):
    theme: Literal["lcars", "dark", "light", "high-contrast", "system"]


class GmrsCallsignUpdate(BaseModel):
    gmrs_callsign: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserOut


class ApiTokenCreate(BaseModel):
    name: str   # human label, e.g. "DMR Relay - shack Pi"


class ApiTokenOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    last_used_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ApiTokenCreated(BaseModel):
    """Returned once at creation — includes the raw token (never stored)."""
    id: int
    name: str
    token: str          # raw token — show to user once, then discard
    created_at: datetime


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=15))
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/auth/register", response_model=UserOut, status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, data: UserCreate, db: AsyncSession = Depends(get_db)):
    if _captcha_configured() and not _verify_captcha(data.captcha_token, get_remote_address(request)):
        raise HTTPException(400, "Verification failed — please try again.")
    if (await db.execute(select(User).filter(User.callsign == data.callsign))).scalar_one_or_none():
        raise HTTPException(400, "Callsign already registered")
    if (await db.execute(select(User).filter(User.email == data.email))).scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    # First registered user becomes (super) admin and is immediately active,
    # independent of org — is_admin bypasses org scoping entirely.
    is_first_user = (await db.execute(select(func.count()).select_from(User))).scalar() == 0
    # The bootstrap admin is trusted implicitly (they had server access to deploy this at
    # all) and skips verification so a first-run SMTP misconfiguration can't lock them out.
    needs_verification = helpers._smtp_configured() and not is_first_user
    # The raw token goes in the email link; only its hash is stored, same pattern
    # as api_tokens, so a DB leak alone can't be used to verify arbitrary accounts.
    verification_token = secrets.token_urlsafe(32) if needs_verification else None
    verification_token_hash = hashlib.sha256(verification_token.encode()).hexdigest() if verification_token else None

    # Multi-tenancy (issue #1). Founding a brand new org makes this user its
    # admin immediately (no one else could approve that membership); joining
    # an existing one leaves the membership pending until that org's own
    # admin approves it — unchanged. Either way, actually being able to LOG
    # IN (is_active) now always needs a super admin's sign-off, since an org
    # founder approving themselves would be no approval at all — except the
    # instance's literal first-ever user, who has no one else to ask.
    org, org_created = await _get_or_create_org(data.org_slug, data.org_name, data.org_website_url, db)
    membership_approved = is_first_user or org_created
    user_is_active = is_first_user

    user = User(
        callsign=data.callsign,
        name=data.name,
        email=data.email,
        hashed_password=hash_password(data.password),
        is_active=user_is_active,
        is_admin=is_first_user,
        email_verified=not needs_verification,
        verification_token=verification_token_hash,
        verification_sent_at=datetime.now(timezone.utc) if needs_verification else None,
        current_org_id=org.id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    db.add(OrganizationMembership(
        org_id=org.id,
        user_id=user.id,
        role="admin" if org_created else "member",
        approved=membership_approved,
    ))
    await db.commit()

    if needs_verification:
        verify_link = helpers._app_url(f"/auth/verify-email?token={verification_token}")
        helpers.send_email(
            to=[user.email],
            subject="[NetControl Online] Verify Your Email",
            body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">Verify Your Email</h2>
  <p>Hello <strong>{user.name}</strong> ({user.callsign}),</p>
  <p>Thanks for registering with NetControl Online. Please confirm this is your email address before an administrator can approve your account.</p>
  {f'<p style="margin-top:16px"><a href="{verify_link}" style="background:#FF9900;color:#000;padding:10px 20px;text-decoration:none;border-radius:20px;font-weight:bold;display:inline-block">Verify Email</a></p>' if verify_link else '<p>Contact your administrator to have your account verified.</p>'}
  <p style="color:#888;font-size:12px">If you did not request this account, please disregard this message.</p>
</div>""",
            body_text=(
                f"Hello {user.name} ({user.callsign}),\n\n"
                f"Thanks for registering with NetControl Online. Please confirm this is your email "
                f"address before an administrator can approve your account.\n\n"
                + (f"Verify here: {verify_link}\n\n" if verify_link else "Contact your administrator to have your account verified.\n\n")
                + "If you did not request this account, please disregard this message."
            ),
        )

    # Notify whoever can actually approve this registration — skip only for
    # the instance's bootstrap user, who has no one else to ask (issue #1).
    if not is_first_user:
        if org_created:
            # Founding a brand new org: there's no other org admin yet, so a
            # super admin has to review it via the existing global
            # /admin/users/{id}/approve (membership itself is already
            # approved — only is_active is still gated).
            notify_admins = (
                (await db.execute(select(User).filter(User.is_admin == True, User.notify_new_registrations == True, User.is_active == True))).scalars().all()
            )
            if notify_admins:
                helpers.send_email(
                    to=[a.email for a in notify_admins],
                    subject=f"[NetControl Online] New Organization Pending Approval: {org.name}",
                    body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">New Organization Registration</h2>
  <p>A new user has registered, founding a new organization, and is awaiting your approval:</p>
  <table style="border-collapse:collapse;width:100%">
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Organization</td><td>{html.escape(org.name)}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Website</td><td>{html.escape(org.website_url or '')}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Callsign</td><td>{user.callsign}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Name</td><td>{user.name}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Email</td><td>{user.email}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Registered</td><td>{user.created_at.strftime('%Y-%m-%d %H:%M UTC')}</td></tr>
  </table>
  <p style="margin-top:16px">Log in to the <strong>Admin</strong> panel to approve or reject this account.</p>
</div>""",
                    body_text=(
                        f"New organization pending approval:\n"
                        f"  Organization : {org.name}\n"
                        f"  Website      : {org.website_url or ''}\n"
                        f"  Callsign     : {user.callsign}\n"
                        f"  Name         : {user.name}\n"
                        f"  Email        : {user.email}\n\n"
                        f"Log in to the Admin panel to approve or reject this account."
                    ),
                )
        else:
            # Joining an existing org: that org's own admins approve it.
            notify_admins = (
                (await db.execute(select(User).join(OrganizationMembership, OrganizationMembership.user_id == User.id).filter(
                    OrganizationMembership.org_id == org.id,
                    OrganizationMembership.role == "admin",
                    OrganizationMembership.approved == True,
                    User.notify_new_registrations == True,
                    User.is_active == True,
                ))).scalars().all()
            )
            if notify_admins:
                helpers.send_email(
                    to=[a.email for a in notify_admins],
                    subject=f"[NetControl Online] New Registration: {user.callsign}",
                    body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">New Operator Registration</h2>
  <p>A new user has requested to join <strong>{html.escape(org.name)}</strong> and is awaiting your approval:</p>
  <table style="border-collapse:collapse;width:100%">
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Callsign</td><td>{user.callsign}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Name</td><td>{user.name}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Email</td><td>{user.email}</td></tr>
    <tr><td style="padding:6px 12px 6px 0;font-weight:bold">Registered</td><td>{user.created_at.strftime('%Y-%m-%d %H:%M UTC')}</td></tr>
  </table>
  <p style="margin-top:16px">Log in to the <strong>Admin</strong> panel to approve or reject this account.</p>
</div>""",
                    body_text=(
                        f"New registration pending approval to join {org.name}:\n"
                        f"  Callsign : {user.callsign}\n"
                        f"  Name     : {user.name}\n"
                        f"  Email    : {user.email}\n\n"
                        f"Log in to the Admin panel to approve or reject this account."
                    ),
                )

    return user


@router.get("/auth/verify-email", include_in_schema=False)
async def verify_email(token: str, db: AsyncSession = Depends(get_db)):
    """Public link clicked from the verification email. Redirects back to the
    login page with a query param the frontend uses to show a result toast."""
    if not token:
        return RedirectResponse(url="/?verified=0")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    user = (await db.execute(select(User).filter(User.verification_token == token_hash))).scalar_one_or_none()
    if not user:
        return RedirectResponse(url="/?verified=0")
    if user.verification_sent_at:
        # SQLite returns tz-naive datetimes; PostgreSQL returns tz-aware — normalize to UTC.
        sent_at = user.verification_sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - sent_at > timedelta(days=VERIFICATION_TOKEN_TTL_DAYS):
            return RedirectResponse(url="/?verified=0")
    user.email_verified = True
    user.verification_token = None
    await db.commit()
    return RedirectResponse(url="/?verified=1")


class SetPasswordRequest(BaseModel):
    token: str
    password: str


@router.post("/auth/set-password", response_model=Token)
@limiter.limit("10/minute")
async def set_password(request: Request, data: SetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Redeems the invite link from an admin-created account's "set your
    password" email (issue #1 follow-up) — the account already exists and is
    active, but hashed_password is an unusable random placeholder until this
    runs. Logs the user straight in on success, same response shape as
    /auth/login, since they have no password to log in with beforehand."""
    if len(data.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    token_hash = hashlib.sha256(data.token.encode()).hexdigest()
    user = (await db.execute(select(User).filter(User.password_set_token == token_hash))).scalar_one_or_none()
    if not user:
        raise HTTPException(400, "This link is invalid or has already been used.")
    if user.password_set_sent_at:
        sent_at = user.password_set_sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - sent_at > timedelta(days=PASSWORD_SET_TOKEN_TTL_DAYS):
            raise HTTPException(400, "This link has expired. Contact your organization admin for a new invite.")
    user.hashed_password = hash_password(data.password)
    user.password_set_token = None
    user.password_set_sent_at = None
    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": str(user.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": token, "token_type": "bearer", "user": user}


@router.post("/auth/login", response_model=Token)
@limiter.limit("10/minute")
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    captcha_token: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if _captcha_configured() and not _verify_captcha(captcha_token, get_remote_address(request)):
        _log_auth_fail(request, f"captcha_failed username={form_data.username!r}")
        raise HTTPException(status_code=400, detail="Verification failed — please try again.")
    # Accept callsign or email as username
    user = (
        (await db.execute(select(User).filter(User.callsign == form_data.username.upper()))).scalar_one_or_none()
        or (await db.execute(select(User).filter(User.email == form_data.username.lower()))).scalar_one_or_none()
    )
    if not user or not verify_password(form_data.password, user.hashed_password):
        _log_auth_fail(request, f"bad_credentials username={form_data.username!r}")
        raise HTTPException(status_code=401, detail="Incorrect callsign/email or password")
    if not user.email_verified:
        _log_auth_fail(request, f"unverified_email username={form_data.username!r}")
        raise HTTPException(status_code=403, detail="Please verify your email before logging in — check your inbox for the verification link.")
    if not user.is_active:
        _log_auth_fail(request, f"inactive_account username={form_data.username!r}")
        raise HTTPException(status_code=403, detail="Account pending approval. Please contact the net administrator.")

    token = create_access_token(
        {"sub": str(user.id)},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {"access_token": token, "token_type": "bearer", "user": user}


@router.get("/auth/config")
def auth_config():
    """Public, unauthenticated — tells the login/register page whether to
    render a bot-protection widget, and if so which provider and (for
    Turnstile/reCAPTCHA) which site key to use. Site keys are meant to be
    public; only the *_SECRET_KEY / ALTCHA_HMAC_KEY values are sensitive.
    ALTCHA needs no site key at all — its widget instead points at
    /captcha/altcha-challenge below."""
    configured = _captcha_configured()
    site_key = None
    if configured:
        if helpers.CAPTCHA_PROVIDER == "turnstile":
            site_key = helpers.TURNSTILE_SITE_KEY
        elif helpers.CAPTCHA_PROVIDER == "recaptcha":
            site_key = helpers.RECAPTCHA_SITE_KEY
    return {
        "captcha_provider": helpers.CAPTCHA_PROVIDER if configured else None,
        "captcha_site_key": site_key,
        # Deprecated aliases, kept for any external client still reading the
        # old shape — TURNSTILE_SITE_KEY doubles as the "enabled" flag's
        # provider check since Turnstile was the only option before.
        "turnstile_enabled": configured and helpers.CAPTCHA_PROVIDER == "turnstile",
        "turnstile_site_key": site_key if helpers.CAPTCHA_PROVIDER == "turnstile" else None,
    }


@router.get("/captcha/altcha-challenge")
@limiter.limit("30/minute")
def altcha_challenge(request: Request):
    """Public, unauthenticated — issues a fresh ALTCHA proof-of-work
    challenge. The <altcha-widget> on the login/register page fetches this
    itself (via its challengeurl attribute) and solves it client-side; no
    external network call is involved on either side."""
    if helpers.CAPTCHA_PROVIDER != "altcha":
        raise HTTPException(404, "ALTCHA is not the active CAPTCHA provider")
    try:
        import altcha
    except ImportError:
        _captcha_log.error("CAPTCHA_PROVIDER=altcha but the altcha package isn't installed — pip install altcha")
        raise HTTPException(500, "ALTCHA is misconfigured on this server — the altcha package isn't installed")
    challenge = altcha.create_challenge_v1(
        hmac_key=helpers.ALTCHA_HMAC_KEY,
        max_number=100_000,
        expires=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return challenge.to_dict()


@router.get("/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/auth/theme", response_model=UserOut)
async def update_theme(
    data: ThemeUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    current_user.theme = data.theme
    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.patch("/auth/gmrs-callsign", response_model=UserOut)
async def update_gmrs_callsign(
    data: GmrsCallsignUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Self-service: set or clear the operator's own GMRS callsign (issue #23)
    — separate from their amateur callsign, used as Net Control on GMRS nets."""
    current_user.gmrs_callsign = (data.gmrs_callsign or "").strip().upper() or None
    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.post("/auth/tokens", response_model=ApiTokenCreated, status_code=201)
async def create_api_token(
    data: ApiTokenCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a long-lived API token. The raw token is returned once — store it securely."""
    raw_token = "nt_" + secrets.token_hex(32)   # 64 hex chars → 256 bits
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    api_token = ApiToken(
        user_id=current_user.id,
        name=data.name,
        token_hash=token_hash,
    )
    db.add(api_token)
    await db.commit()
    await db.refresh(api_token)
    return ApiTokenCreated(id=api_token.id, name=api_token.name, token=raw_token, created_at=api_token.created_at)


@router.get("/auth/tokens", response_model=list[ApiTokenOut])
async def list_api_tokens(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return (await db.execute(select(ApiToken).filter(ApiToken.user_id == current_user.id))).scalars().all()


@router.delete("/auth/tokens/{token_id}", status_code=204)
async def delete_api_token(
    token_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    api_token = (await db.execute(select(ApiToken).filter(ApiToken.id == token_id, ApiToken.user_id == current_user.id))).scalar_one_or_none()
    if not api_token:
        raise HTTPException(404, "Token not found")
    await db.delete(api_token)
    await db.commit()
