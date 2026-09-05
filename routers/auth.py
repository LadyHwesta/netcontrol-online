"""
Auth helpers + Auth routes + API Token management.
"""

import hashlib
import html
import logging
import logging.handlers
import os
import pathlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import bcrypt as _bcrypt
import jwt
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
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
from routers.helpers import SELF_REQUESTABLE_ROLES, _captcha_configured, _captcha_log, _get_or_create_org, _verify_captcha
from routers.schemas import UserOut

router = APIRouter()

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8 hours
VERIFICATION_TOKEN_TTL_DAYS = 7
PASSWORD_SET_TOKEN_TTL_DAYS = 14   # admin-created accounts' invite link (issue #1 follow-up) — longer than email verification since an operator may not check email daily
PASSWORD_RESET_TOKEN_TTL_HOURS = 1   # forgot-password link (issue follow-up) — short-lived: self-triggered by anyone who knows the account's callsign/email, not vouched for by an admin like the invite link above

# Self-service profile photo (issue follow-up) -- same "glob the uploads dir
# by extension" shape as routers/orgs.py's _org_logo_file/_LOGO_EXTS, just
# namespaced per user ("user_{id}_photo.{ext}") and kept local to this file
# rather than routers/helpers.py since nothing outside auth.py resolves an
# actual file -- schedules.py/sessions.py only ever hand the frontend a raw
# user_id, which builds the <img src="/users/{id}/photo"> URL itself.
_PHOTO_EXTS = ("png", "jpg", "jpeg", "gif", "webp")  # no svg -- a real photo, not a logo/icon


def _user_photo_file(user_id: int) -> Optional[pathlib.Path]:
    for ext in _PHOTO_EXTS:
        p = helpers.UPLOADS_DIR / f"user_{user_id}_photo.{ext}"
        if p.exists():
            return p
    return None


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


def _token_expired(sent_at: Optional[datetime], ttl: timedelta) -> bool:
    """True if a *_sent_at timestamp is older than ttl -- shared by every
    token-redemption endpoint below (email verify, admin-invite set-password,
    forgot-password reset). sent_at=None means "no expiry recorded", treated
    as not-expired (matches these tokens' original individual behavior).
    SQLite returns tz-naive datetimes; PostgreSQL returns tz-aware --
    normalize to UTC before comparing."""
    if not sent_at:
        return False
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - sent_at > ttl


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
    # Role revamp (issue follow-up): which role(s) the registrant is interested
    # in filling -- an informational hint only (OrganizationMembership.
    # requested_roles), never authoritative; the org admin decides the actual
    # grant at approval time. "admin" is deliberately not self-requestable.
    requested_roles: Optional[list[str]] = None

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        return v.upper().strip()

    @field_validator("requested_roles")
    @classmethod
    def valid_requested_roles(cls, v):
        if v is None:
            return v
        bad = [r for r in v if r not in SELF_REQUESTABLE_ROLES]
        if bad:
            raise ValueError(f"Invalid requested role(s): {', '.join(bad)}")
        return v


class ThemeUpdate(BaseModel):
    theme: Literal["lcars", "dark", "light", "high-contrast", "pink", "purple", "blue", "matrix", "earth", "system"]


class LanguageUpdate(BaseModel):
    language: Optional[str] = None  # ISO code, or null to reset to English/browser default


class GmrsCallsignUpdate(BaseModel):
    gmrs_callsign: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ProfileUpdate(BaseModel):
    """Self-service name/email/callsign/phone (issue follow-up) -- one
    combined endpoint (PATCH /auth/profile) rather than a field each,
    matching how PATCH /orgs/{id} saves name+website+banner+tagline
    together from one form, since that's how the Account page's own
    Profile card presents these too."""
    name: str
    email: EmailStr
    callsign: str
    phone: Optional[str] = None

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        return v.upper().strip()


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
    org, org_created = await _get_or_create_org(data.org_slug, data.org_name, data.org_website_url, db, block_invite_only=True)
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
        requested_roles=",".join(data.requested_roles) if data.requested_roles else None,
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
    if _token_expired(user.verification_sent_at, timedelta(days=VERIFICATION_TOKEN_TTL_DAYS)):
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
    if _token_expired(user.password_set_sent_at, timedelta(days=PASSWORD_SET_TOKEN_TTL_DAYS)):
        raise HTTPException(400, "This link has expired. Contact your organization admin for a new invite.")
    user.hashed_password = hash_password(data.password)
    user.password_set_token = None
    user.password_set_sent_at = None
    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": str(user.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": token, "token_type": "bearer", "user": user}


class ForgotPasswordRequest(BaseModel):
    identifier: str   # callsign or email, same dual lookup as /auth/login's username field


@router.post("/auth/forgot-password", status_code=204)
@limiter.limit("5/minute")
async def forgot_password(request: Request, data: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Self-service "forgot password" (issue follow-up) — issues a short-lived
    reset link by email, if a matching account exists. Always responds the
    same way (204, no body) regardless of whether one was found or whether
    SMTP is even configured (send_email() itself already no-ops quietly when
    it isn't) — telling the caller which is true would let this be used to
    enumerate registered callsigns/emails. No CAPTCHA on this endpoint
    (unlike registration/login) — it can only ever email an *existing*
    account, so the abuse surface is a handful of unwanted reset emails to a
    real user, not open-ended spam; rate limiting alone is proportionate."""
    identifier = data.identifier.strip()
    user = (
        (await db.execute(select(User).filter(User.callsign == identifier.upper()))).scalar_one_or_none()
        or (await db.execute(select(User).filter(User.email == identifier.lower()))).scalar_one_or_none()
    )
    # Nothing to do if SMTP isn't configured -- the token could never be
    # delivered anyway, so skip generating and storing one pointlessly (the
    # frontend already hides the link entirely in this case; this only
    # matters for a direct API call). Response is identical either way.
    if user and helpers._smtp_configured():
        raw_token = secrets.token_urlsafe(32)
        user.password_reset_token = hashlib.sha256(raw_token.encode()).hexdigest()
        user.password_reset_sent_at = datetime.now(timezone.utc)
        await db.commit()

        reset_link = helpers._app_url(f"/?resetpw={raw_token}")
        helpers.send_email(
            to=[user.email],
            subject="[NetControl Online] Reset Your Password",
            body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">Reset Your Password</h2>
  <p>Hello <strong>{html.escape(user.name)}</strong> ({user.callsign}),</p>
  <p>Someone (hopefully you) requested a password reset for your NetControl Online account. This link expires in {PASSWORD_RESET_TOKEN_TTL_HOURS} hour{'s' if PASSWORD_RESET_TOKEN_TTL_HOURS != 1 else ''}.</p>
  {f'<p style="margin-top:16px"><a href="{reset_link}" style="background:#FF9900;color:#000;padding:10px 20px;text-decoration:none;border-radius:20px;font-weight:bold;display:inline-block">Reset Password</a></p>' if reset_link else '<p>Contact your administrator for help resetting your password.</p>'}
  <p style="color:#888;font-size:12px">If you didn't request this, you can safely ignore this email — your password won't change unless you click the link above and set a new one.</p>
</div>""",
            body_text=(
                f"Hello {user.name} ({user.callsign}),\n\n"
                f"Someone (hopefully you) requested a password reset for your NetControl Online account. "
                f"This link expires in {PASSWORD_RESET_TOKEN_TTL_HOURS} hour{'s' if PASSWORD_RESET_TOKEN_TTL_HOURS != 1 else ''}.\n\n"
                + (f"Reset your password: {reset_link}\n\n" if reset_link else "Contact your administrator for help resetting your password.\n\n")
                + "If you didn't request this, you can safely ignore this email — your password won't change unless you follow the link above and set a new one."
            ),
        )


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


@router.post("/auth/reset-password", response_model=Token)
@limiter.limit("10/minute")
async def reset_password(request: Request, data: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Redeems a forgot-password link (issue follow-up). Logs the user
    straight in on success, same shape/convention as /auth/set-password."""
    if len(data.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    token_hash = hashlib.sha256(data.token.encode()).hexdigest()
    user = (await db.execute(select(User).filter(User.password_reset_token == token_hash))).scalar_one_or_none()
    if not user:
        raise HTTPException(400, "This link is invalid or has already been used.")
    if _token_expired(user.password_reset_sent_at, timedelta(hours=PASSWORD_RESET_TOKEN_TTL_HOURS)):
        raise HTTPException(400, "This link has expired. Request a new one from the login page.")
    user.hashed_password = hash_password(data.password)
    user.password_reset_token = None
    user.password_reset_sent_at = None
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
    /captcha/altcha-challenge below. Also reports whether SMTP is
    configured (issue follow-up) -- the login page hides its "Forgot
    password?" link entirely rather than offering a form that can never
    actually deliver a reset email."""
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
        "smtp_configured": helpers._smtp_configured(),
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


@router.patch("/auth/language", response_model=UserOut)
async def update_language(
    data: LanguageUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    current_user.language = data.language
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


@router.patch("/auth/profile", response_model=UserOut)
async def update_profile(
    data: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Self-service: name/email/callsign/phone (issue follow-up) -- previously
    these were fixed at registration with no way to fix a typo or update
    contact info. No current-password confirmation required, matching every
    other self-service field this app already has (GMRS callsign, theme,
    language). Callsign/email changes never invalidate an existing session
    -- the JWT is keyed by user.id, not either of them (see login())."""
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Name is required")

    if (await db.execute(select(User).filter(User.callsign == data.callsign, User.id != current_user.id))).scalar_one_or_none():
        raise HTTPException(400, "Callsign already registered")
    if (await db.execute(select(User).filter(User.email == data.email, User.id != current_user.id))).scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    email_changed = data.email != current_user.email
    current_user.name = name
    current_user.callsign = data.callsign
    current_user.email = data.email
    current_user.phone = (data.phone or "").strip() or None

    # Email change re-verification (issue follow-up) -- same gating and
    # token/email shape as registration's own needs_verification path;
    # GET /auth/verify-email is reused completely unchanged, since it just
    # resolves user-by-token-hash and flips email_verified=True with no
    # check on which email is current. login() already rejects an
    # unverified account, so re-using that is the only enforcement needed
    # -- this session's own JWT (keyed by user.id) still works until it
    # expires, only a *future* login is blocked until confirmed.
    reverify_sent = False
    if email_changed and helpers._smtp_configured():
        current_user.email_verified = False
        verification_token = secrets.token_urlsafe(32)
        current_user.verification_token = hashlib.sha256(verification_token.encode()).hexdigest()
        current_user.verification_sent_at = datetime.now(timezone.utc)
        reverify_sent = True

    await db.commit()
    await db.refresh(current_user)

    if reverify_sent:
        verify_link = helpers._app_url(f"/auth/verify-email?token={verification_token}")
        helpers.send_email(
            to=[current_user.email],
            subject="[NetControl Online] Verify Your New Email Address",
            body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">Verify Your New Email</h2>
  <p>Hello <strong>{html.escape(current_user.name)}</strong> ({current_user.callsign}),</p>
  <p>You (or someone with access to your account) changed the email address on your NetControl Online account to this one. Please confirm it's really yours before you can log in again.</p>
  {f'<p style="margin-top:16px"><a href="{verify_link}" style="background:#FF9900;color:#000;padding:10px 20px;text-decoration:none;border-radius:20px;font-weight:bold;display:inline-block">Verify Email</a></p>' if verify_link else '<p>Contact your administrator to have your account verified.</p>'}
  <p style="color:#888;font-size:12px">If you didn't make this change, contact your administrator right away.</p>
</div>""",
            body_text=(
                f"Hello {current_user.name} ({current_user.callsign}),\n\n"
                f"Your NetControl Online account's email was changed to this address. Please confirm it's really yours before you can log in again.\n\n"
                + (f"Verify here: {verify_link}\n\n" if verify_link else "Contact your administrator to have your account verified.\n\n")
                + "If you didn't make this change, contact your administrator right away."
            ),
        )

    return current_user


@router.post("/auth/change-password", status_code=204)
@limiter.limit("10/minute")
async def change_password(
    request: Request,
    data: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Self-service password change for an already-logged-in user (issue
    follow-up) -- distinct from /auth/set-password (admin-invite token) and
    /auth/reset-password (forgot-password link): both of those exist
    specifically because the caller ISN'T logged in yet, so they prove a
    token instead. Here the caller already has a valid session; proving the
    current password is what stands in for that -- unlike update_profile's
    other self-service fields, a password change is sensitive enough to
    need it (also stops an attacker who's grabbed an unattended, still-
    logged-in session from silently locking the real owner out)."""
    if not verify_password(data.current_password, current_user.hashed_password):
        raise HTTPException(400, "Current password is incorrect")
    if len(data.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    current_user.hashed_password = hash_password(data.new_password)
    await db.commit()


@router.post("/auth/photo", status_code=204)
async def upload_profile_photo(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """Self-service profile photo (issue follow-up) -- shown on the public
    live page next to whoever's running the net (Net Control) and/or the
    assigned broadcaster (see routers/schedules.py's _duty_labels_for_session).
    Same validate/replace-existing-file shape as routers/orgs.py's
    upload_org_logo, keyed by the caller's own id instead of an org_id."""
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in _PHOTO_EXTS:
        raise HTTPException(400, "Unsupported file type — use PNG, JPG, GIF, or WebP")
    for old in helpers.UPLOADS_DIR.glob(f"user_{current_user.id}_photo.*"):
        old.unlink(missing_ok=True)
    dest = helpers.UPLOADS_DIR / f"user_{current_user.id}_photo.{ext}"
    dest.write_bytes(await file.read())


@router.delete("/auth/photo", status_code=204)
def delete_profile_photo(current_user: User = Depends(get_current_user)):
    """Self-service — remove the caller's own profile photo."""
    for old in helpers.UPLOADS_DIR.glob(f"user_{current_user.id}_photo.*"):
        old.unlink(missing_ok=True)


@router.get("/users/{user_id}/photo")
def get_user_photo(user_id: int):
    """Public endpoint — serves a user's uploaded profile photo. No auth:
    loaded directly in <img src> on the public live page and the
    authenticated duty bar alike, same trust level as the instance/org
    logo endpoints (GET /logo, GET /orgs/{id}/logo)."""
    p = _user_photo_file(user_id)
    if not p:
        raise HTTPException(404, "No photo uploaded for this user")
    ext = p.suffix.lstrip(".")
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return Response(content=p.read_bytes(), media_type=mime)


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
