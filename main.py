"""
NetControl Online — FastAPI backend

Endpoints
---------
Auth
  POST /auth/register   — create a net-control operator account
  POST /auth/login      — returns JWT access token

Nets
  GET    /nets          — list nets owned by current user
  POST   /nets          — create a net
  GET    /nets/{id}     — get net details
  PUT    /nets/{id}     — update net
  DELETE /nets/{id}     — delete net

Net Sessions
  GET    /nets/{id}/sessions          — list sessions for a net
  POST   /nets/{id}/sessions          — start a new session
  GET    /sessions/{id}               — get session details
  PATCH  /sessions/{id}/end           — end (close) a session
  DELETE /sessions/{id}               — delete session

Checkins
  GET    /sessions/{id}/checkins      — list checkins in a session
  POST   /sessions/{id}/checkins      — add a checkin
  POST   /sessions/{id}/checkins/import — bulk-add checkins from a CSV upload
  GET    /checkins/import-sample      — downloadable sample CSV showing the expected columns
  DELETE /checkins/{id}               — remove a checkin

History / Stats
  GET    /nets/{id}/history           — checkin counts per callsign across all sessions
  GET    /sessions/{id}/export        — CSV export of all checkins in a session
  GET    /nets/{id}/export            — CSV export of all checkins across all sessions

Callsign Lookup
  GET    /callsign/{callsign}/lookup  — look up FCC license data (name, class, state, grid)
"""

import csv
import hashlib
import html
import io
import logging
import json
import logging.handlers
import os
import pathlib
import re
import secrets
import smtplib
import time as _time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional, Literal

import httpx
# altcha is imported lazily (inside _verify_altcha/altcha_challenge below),
# not here -- it's the one bot-protection dependency that isn't already a
# transitive dependency of something else this app requires regardless, so a
# deployment that only ever uses Turnstile/reCAPTCHA (or no CAPTCHA_PROVIDER
# at all) can skip installing it entirely without the app failing to start.

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
import jwt
import bcrypt as _bcrypt
from pydantic import BaseModel, EmailStr, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import ApiToken, CallsignCache, Checkin, DmrConfig, EvacZone, GmrsLicense, Net, NetControlShift, NetControlSignup, NetSchedule, NetSession, NetShare, Organization, OrganizationMembership, StationRemark, SystemSetting, TacticalPosition, TrafficMessage, User, utcnow
import net_repository

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Without this, only loggers with their own explicit handler (AUTH_LOG_FILE
# below) produce visible output. Everything else falls back to Python's
# WARNING-level "handler of last resort", so INFO messages — a successful
# Net Repository push, a sent email — never appear anywhere, not even in the
# systemd journal, even though the equivalent failures (logged at WARNING)
# already do.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-use-a-long-random-string")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8 hours

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
ADMIN_CONTACT_EMAIL = os.getenv("ADMIN_CONTACT_EMAIL", "")  # shown in approval emails as human contact
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")    # e.g. https://netcontrol.example.org — used for links in emails
VERIFICATION_TOKEN_TTL_DAYS = 7
PASSWORD_SET_TOKEN_TTL_DAYS = 14   # admin-created accounts' invite link (issue #1 follow-up) — longer than email verification since an operator may not check email daily

_email_log = logging.getLogger("ham_net_tracker.email")

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


def _smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def _app_url(path: str = "") -> Optional[str]:
    """Absolute link back to this app for use in emails. None if APP_BASE_URL isn't configured."""
    return f"{APP_BASE_URL}{path}" if APP_BASE_URL else None


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


def _build_ics(net: "Net", schedule: "NetSchedule", signup: "NetControlSignup", role_label: str = "Net Control") -> str:
    """Build an iCalendar (ICS) event string for a net control / broadcaster signup."""
    import re
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz_str = schedule.timezone or "UTC"
    try:
        tz = ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
        tz_str = "UTC"

    h, m = map(int, schedule.start_time.split(":"))
    naive_start = datetime(
        signup.slot_date.year, signup.slot_date.month, signup.slot_date.day, h, m
    )
    local_start = naive_start.replace(tzinfo=tz)
    utc_start   = local_start.astimezone(ZoneInfo("UTC"))
    utc_end     = utc_start + timedelta(hours=1)   # default 1-hour block

    dtstamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    dtstart = utc_start.strftime("%Y%m%dT%H%M%SZ")
    dtend   = utc_end.strftime("%Y%m%dT%H%M%SZ")

    uid = f"netcontrol-{signup.id}-{signup.slot_date}@hamnettracker"

    # Build description (escape commas and newlines per RFC 5545)
    desc_parts = [f"You are scheduled as {role_label} for {net.name}."]
    if net.frequency:
        desc_parts.append(f"Frequency: {net.frequency}")
    desc_parts.append(f"Date: {signup.slot_date}")
    desc_parts.append(f"Time: {schedule.start_time} {tz_str}")
    if schedule.notes:
        desc_parts.append(f"Net notes: {schedule.notes}")
    if signup.notes:
        desc_parts.append(f"Your notes: {signup.notes}")
    description = "\\n".join(desc_parts)

    # Organizer — strip display name if present
    organizer_raw = SMTP_FROM or SMTP_USER or ""
    m2 = re.search(r"<(.+?)>", organizer_raw)
    organizer_email = m2.group(1) if m2 else organizer_raw

    attendee_name  = signup.name or signup.callsign
    attendee_email = signup.email or ""

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//NetControl Online//Ham Radio//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{net.name} – {role_label}",
        f"DESCRIPTION:{description}",
    ]
    if organizer_email:
        lines.append(f"ORGANIZER:mailto:{organizer_email}")
    if attendee_email:
        lines.append(f"ATTENDEE;CN={attendee_name};RSVP=FALSE:mailto:{attendee_email}")
    lines += ["END:VEVENT", "END:VCALENDAR"]

    return "\r\n".join(lines) + "\r\n"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

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


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
UPLOADS_DIR = pathlib.Path(__file__).parent / "uploads"
LOGO_PATH   = UPLOADS_DIR / "logo"
STATIC_DIR  = pathlib.Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app):
    init_db()
    UPLOADS_DIR.mkdir(exist_ok=True)
    yield


app = FastAPI(title="NetControl Online", version="2.11.2", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

_HTML_FILE = Path(__file__).parent / "index.html"
_STATIC_DIR = Path(__file__).parent


def _serve_html(name: str) -> HTMLResponse:
    """Read and serve a standalone HTML page from the app directory."""
    return HTMLResponse(content=(_STATIC_DIR / name).read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_frontend():
    """Serve the SPA (My Nets + Session views)."""
    return HTMLResponse(content=_HTML_FILE.read_text(encoding="utf-8"))


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def serve_admin():
    return _serve_html("admin.html")


@app.get("/tokens", response_class=HTMLResponse, include_in_schema=False)
def serve_tokens():
    return _serve_html("tokens.html")


@app.get("/help", response_class=HTMLResponse, include_in_schema=False)
def serve_help():
    return _serve_html("help.html")


@app.get("/report", response_class=HTMLResponse, include_in_schema=False)
def serve_report():
    return _serve_html("report.html")


@app.get("/manifest.json", include_in_schema=False)
def serve_manifest(db: Session = Depends(get_db)):
    """PWA web manifest (issue #9). Generated dynamically rather than a static
    file so name/short_name pick up the org's own Branding settings instead of
    a hardcoded name — icons stay fixed to the built-in mark (reliable/square)
    regardless of any uploaded club logo."""
    org_name = _get_setting("org_name", db) or "NetControl Online"
    return {
        "name": org_name,
        "short_name": org_name if len(org_name) <= 15 else "NetControl Online",
        "description": "Track amateur radio and GMRS net check-ins",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0a0a1a",
        "theme_color": "#0a0a1a",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


@app.get("/sw.js", include_in_schema=False)
def serve_service_worker():
    """Service worker (issue #9), served from the root path (not /static/) so
    its default registration scope is "/" and it can control every page."""
    content = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    return Response(content=content, media_type="application/javascript")


def _public_base_url(request: Request) -> str:
    """APP_BASE_URL if configured (same convention as email links via
    _app_url()) so a reverse-proxied instance's public URL is reflected
    correctly; otherwise derived from the request itself."""
    return APP_BASE_URL or str(request.base_url).rstrip("/")


@app.get("/robots.txt", include_in_schema=False)
def robots_txt(request: Request):
    """Everything here requires a login except the public /directory and
    /live pages (org-scoped net info) — steer crawlers to just those, and
    point them at the sitemap for the actual per-org URLs to index."""
    lines = [
        "User-agent: *",
        "Allow: /directory",
        "Allow: /live",
        "Disallow: /",
        "",
        f"Sitemap: {_public_base_url(request)}/sitemap.xml",
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(request: Request, db: Session = Depends(get_db)):
    """Lists each organization's public directory/live pages (issue #1) — the
    same set /public/organizations already computes: orgs with at least one
    net actually opted into the public directory, so nothing thin or private
    gets listed."""
    base = _public_base_url(request)
    orgs = (
        db.query(Organization)
        .join(Net, Net.org_id == Organization.id)
        .filter(Net.public_listed == True)
        .distinct()
        .order_by(Organization.name)
        .all()
    )
    entries = [(f"{base}/directory", "0.5", "weekly"), (f"{base}/live", "0.3", "daily")]
    for org in orgs:
        entries.append((f"{base}/directory/{org.slug}", "0.9", "weekly"))
        entries.append((f"{base}/live/{org.slug}", "0.4", "hourly"))
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, priority, changefreq in entries:
        body.append(f"  <url><loc>{html.escape(loc)}</loc><changefreq>{changefreq}</changefreq><priority>{priority}</priority></url>")
    body.append("</urlset>")
    return Response(content="\n".join(body) + "\n", media_type="application/xml")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

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


class UserOut(BaseModel):
    id: int
    callsign: str
    gmrs_callsign: Optional[str] = None
    name: str
    email: str
    is_active: bool
    is_admin: bool
    notify_new_registrations: bool
    theme: str
    email_verified: bool
    created_at: datetime
    current_org_id: Optional[int] = None

    model_config = {"from_attributes": True}


class AdminUserOut(UserOut):
    """UserOut plus the user's current org's name/website — lets a super
    admin reviewing a pending registration (especially one founding a brand
    new org) verify it without a separate lookup (issue #1 follow-up)."""
    org_name: Optional[str] = None
    org_website_url: Optional[str] = None


class OrganizationOut(BaseModel):
    id: int
    name: str
    slug: str
    website_url: Optional[str] = None

    model_config = {"from_attributes": True}


class MyOrgOut(OrganizationOut):
    """Like OrganizationOut, plus the caller's own role in that org — lets the
    frontend show the org-admin panel only where the user actually has it."""
    role: str


class OrgMemberOut(BaseModel):
    """A user's membership within one org — used for the org-admin approval
    queue and member list. Distinct from UserOut since it's per-membership,
    not per-account (a user can appear once per org they belong to)."""
    user_id: int
    callsign: str
    name: str
    email: str
    role: str
    approved: bool
    requested_at: datetime


class ThemeUpdate(BaseModel):
    theme: Literal["lcars", "dark", "light", "high-contrast", "system"]


class GmrsCallsignUpdate(BaseModel):
    gmrs_callsign: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserOut


class NetCreate(BaseModel):
    name: str
    frequency: Optional[str] = None
    description: Optional[str] = None
    net_type: str = "ham"       # "ham" | "gmrs"
    is_ares: bool = False       # ham only; ignored (forced False) for GMRS nets
    dmr_talkgroup: Optional[str] = None   # ham only
    script: Optional[str] = None   # net control script, shown alongside the check-in screen
    has_broadcast: bool = False    # e.g. a Newsline segment carried during the net
    broadcast_label: Optional[str] = None   # e.g. "Amateur Radio Newsline"
    reminder_enabled: bool = False   # email signed-up Net Control / Broadcaster before start
    reminder_minutes_before: Optional[int] = None   # lead time, e.g. 30
    public_listed: bool = False    # shown in the public /directory (no login required)
    # Optional directory metadata — not used locally, only forwarded to Net Repository
    band: Optional[str] = None
    mode: Optional[str] = None
    ctcss_tone: Optional[str] = None
    region: Optional[str] = None
    state: Optional[str] = None
    website: Optional[str] = None

    @field_validator("reminder_minutes_before")
    @classmethod
    def valid_reminder_lead(cls, v):
        if v is not None and not (1 <= v <= 1440):
            raise ValueError("reminder_minutes_before must be between 1 and 1440")
        return v


class NetOut(BaseModel):
    id: int
    name: str
    frequency: Optional[str]
    description: Optional[str]
    net_type: str
    is_ares: bool
    dmr_talkgroup: Optional[str] = None
    script: Optional[str] = None
    has_broadcast: bool = False
    broadcast_label: Optional[str] = None
    public_listed: bool = False
    reminder_enabled: bool = False
    reminder_minutes_before: Optional[int] = None
    band: Optional[str] = None
    mode: Optional[str] = None
    ctcss_tone: Optional[str] = None
    region: Optional[str] = None
    state: Optional[str] = None
    website: Optional[str] = None
    owner_id: int
    org_id: int
    created_at: datetime
    # Sharing fields (populated by helper, not from ORM attributes directly)
    is_owner: bool = True
    shared_with_all: bool = False
    shared_user_ids: list[int] = []
    can_edit_all: bool = False           # edit rights granted to the "shared with all" grant
    editor_user_ids: list[int] = []      # subset of shared_user_ids also granted edit rights
    can_edit: bool = False               # whether the CALLER (owner, admin, or an editor share) can edit this net
    owner_callsign: Optional[str] = None

    model_config = {"from_attributes": True}


class UserPublicOut(BaseModel):
    id: int
    callsign: str
    gmrs_callsign: Optional[str] = None
    name: str

    model_config = {"from_attributes": True}


class NetShareUpdate(BaseModel):
    share_with_all: bool = False
    can_edit_all: bool = False        # edit rights for the "shared with all" grant, only meaningful when share_with_all=True
    user_ids: list[int] = []          # specific user IDs to share with (ignored when share_with_all=True)
    editor_user_ids: list[int] = []   # subset of user_ids to also grant edit rights


class NetOwnerUpdate(BaseModel):
    owner_id: int


class BrandingOut(BaseModel):
    org_name: Optional[str] = None
    tagline: Optional[str] = None
    website_url: Optional[str] = None
    has_logo: bool = False


class BrandingUpdate(BaseModel):
    org_name: Optional[str] = None
    tagline: Optional[str] = None
    website_url: Optional[str] = None


class SessionCreate(BaseModel):
    name: Optional[str] = None
    notes: Optional[str] = None
    # Manual broadcaster override for this session — takes precedence over the
    # schedule sign-up for the session's date (issue #17).
    broadcaster_override_callsign: Optional[str] = None
    broadcaster_override_name: Optional[str] = None
    # ARES/ACES activation (issue #21) — forced False server-side unless the net
    # is is_ares. Set once at start; enables tactical positions and the
    # simplified roster for this session only, not every session on the net.
    is_activation: bool = False
    # Offline net entry (issue #20) — backfilling a net that already happened
    # with no access to the web tool. occurred_at is required when is_offline
    # is set; it becomes both started_at and ended_at (no live view, matches
    # the issue). ncs_override_* mirrors broadcaster_override_* above — usually
    # needed here since whoever backfills the log may not be who ran the net.
    is_offline: bool = False
    occurred_at: Optional[datetime] = None
    ncs_override_callsign: Optional[str] = None
    ncs_override_name: Optional[str] = None


class SessionRename(BaseModel):
    name: Optional[str] = None


class SessionOut(BaseModel):
    id: int
    net_id: int
    operator_id: Optional[int]
    name: Optional[str]
    notes: Optional[str]
    started_at: datetime
    ended_at: Optional[datetime]
    is_activation: bool = False
    is_offline: bool = False
    is_offline_locked: bool = False
    checkin_count: int = 0
    # Scheduled duty for this session's date, from the Schedule sign-up if one exists
    # (net control falls back to whoever started the session when no sign-up matches)
    ncs_callsign: Optional[str] = None
    ncs_name: Optional[str] = None
    broadcaster_callsign: Optional[str] = None
    broadcaster_name: Optional[str] = None
    broadcast_label: Optional[str] = None
    # Same, but for the schedule sign-up one week after this session's date (no fallback —
    # there's no operator yet for a session that hasn't started).
    next_ncs_callsign: Optional[str] = None
    next_ncs_name: Optional[str] = None
    next_broadcaster_callsign: Optional[str] = None
    next_broadcaster_name: Optional[str] = None

    model_config = {"from_attributes": True}


class CheckinCreate(BaseModel):
    callsign: str
    name: Optional[str] = None
    signal_report: Optional[str] = None
    comments: Optional[str] = None
    has_traffic: bool = False
    evac_zone: Optional[str] = None
    dmr_talkgroup: Optional[str] = None
    dmr_region: Optional[str] = None

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        return v.upper().strip()


class CheckinOut(BaseModel):
    id: int
    session_id: int
    callsign: str
    name: Optional[str]
    signal_report: Optional[str]
    comments: Optional[str]
    has_traffic: bool
    traffic_called: bool = False
    evac_zone: Optional[str]
    dmr_talkgroup: Optional[str] = None
    dmr_region: Optional[str] = None
    checked_in_at: datetime
    # Tactical position shift tracking (issue #21, activation sessions only).
    # tactical_callsign is denormalized from the linked TacticalPosition for
    # display — populated by list_checkins()/sign_on_tactical_position(), null
    # whenever the checkin isn't tied to a position.
    tactical_position_id: Optional[int] = None
    tactical_callsign: Optional[str] = None
    signed_off_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TacticalPositionCreate(BaseModel):
    tactical_callsign: str
    location: Optional[str] = None
    assigned_callsign: Optional[str] = None   # planned/expected operator (optional)
    assigned_name: Optional[str] = None
    scheduled_start: Optional[datetime] = None   # planned shift sign-on time (optional)

    @field_validator("tactical_callsign")
    @classmethod
    def tactical_callsign_upper(cls, v):
        v = v.strip().upper()
        if not v:
            raise ValueError("tactical_callsign is required")
        return v


class TacticalPositionOut(BaseModel):
    id: int
    session_id: int
    tactical_callsign: str
    location: Optional[str]
    assigned_callsign: Optional[str]
    assigned_name: Optional[str]
    scheduled_start: Optional[datetime] = None
    # Auto-created, one per activation session — Net Control tracked the same way as any
    # other tactical position (sign-on/off, shift history), not user-creatable or removable.
    is_net_control: bool = False
    created_at: datetime
    # Derived from the checkin (if any) currently holding this position —
    # tactical_position_id set, signed_off_at still null.
    current_checkin_id: Optional[int] = None
    current_callsign: Optional[str] = None
    current_name: Optional[str] = None
    signed_on_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TacticalPositionUpdate(BaseModel):
    """Edit a position's plan -- location, planned operator, scheduled sign-on time.
    tactical_callsign and is_net_control are identity, not plan, and aren't editable
    here. Full-replace semantics (like TacticalPositionCreate): every field is sent
    on every save, so an empty value clears it rather than leaving it untouched."""
    location: Optional[str] = None
    assigned_callsign: Optional[str] = None
    assigned_name: Optional[str] = None
    scheduled_start: Optional[datetime] = None


class TacticalSignOn(BaseModel):
    callsign: str
    name: Optional[str] = None

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        v = v.strip().upper()
        if not v:
            raise ValueError("callsign is required")
        return v


class NetControlShiftCreate(BaseModel):
    callsign: str
    name: Optional[str] = None
    scheduled_start: datetime

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        v = v.strip().upper()
        if not v:
            raise ValueError("callsign is required")
        return v


class NetControlShiftOut(BaseModel):
    id: int
    session_id: int
    callsign: str
    name: Optional[str]
    scheduled_start: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class DmrConfigCreate(BaseModel):
    source_type: str = "wpsd"           # wpsd | pistar | brandmeister
    hotspot_url: Optional[str] = None   # for wpsd/pistar
    talkgroup_id: Optional[int] = None  # for brandmeister
    filter_callsign: Optional[str] = None
    direct_mode: bool = False


class DmrConfigOut(BaseModel):
    source_type: str
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


class EvacZoneOut(BaseModel):
    callsign: str
    zone: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class EvacZoneUpdate(BaseModel):
    zone: str


class ExpectedStation(BaseModel):
    callsign: str
    name: Optional[str]
    checkin_count: int   # in the requested window
    last_checkin: datetime


class CallsignHistoryItem(BaseModel):
    callsign: str
    name: Optional[str]
    total_checkins: int
    recent_checkins: int           # checkins in the past 14 days
    recent_4w_checkins: int        # checkins in the past 28 days
    checked_in_last_session: bool  # present in the most recent ended session
    last_checkin: datetime


# ── Traffic messages ─────────────────────────────────────────────────────────

class TrafficMessageCreate(BaseModel):
    origin_callsign: str
    dest_info: Optional[str] = None
    msg_number: Optional[str] = None
    msg_type: str = "formal"       # formal | informal | health_welfare
    status: str = "received"       # received | relayed | delivered | undeliverable
    notes: Optional[str] = None


class TrafficMessageUpdate(BaseModel):
    dest_info: Optional[str] = None
    msg_number: Optional[str] = None
    msg_type: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class TrafficMessageOut(BaseModel):
    id: int
    session_id: int
    msg_number: Optional[str]
    origin_callsign: str
    dest_info: Optional[str]
    msg_type: str
    status: str
    notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Station remarks ──────────────────────────────────────────────────────────

class StationRemarkUpsert(BaseModel):
    remark: Optional[str] = None
    preferred_name: Optional[str] = None   # overrides FCC name in Expected Stations + reports


class StationRemarkOut(BaseModel):
    callsign: str
    net_id: int
    remark: Optional[str] = None
    preferred_name: Optional[str] = None
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── API Tokens ───────────────────────────────────────────────────────────────

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


# ── Session summary ──────────────────────────────────────────────────────────

class SessionSummary(BaseModel):
    session_id: int
    net_name: str
    started_at: datetime
    ended_at: Optional[datetime]
    duration_minutes: Optional[int]
    total_checkins: int
    traffic_count: int
    new_stations: int      # callsigns appearing for the first time on this net
    operator_callsign: Optional[str]
    net_frequency: Optional[str]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=15))
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # --- Try long-lived API token first (format: "nt_<64 hex chars>") ---
    if token.startswith("nt_"):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        api_token = db.query(ApiToken).filter(ApiToken.token_hash == token_hash).first()
        if api_token is None:
            raise credentials_exception
        user = db.query(User).filter(User.id == api_token.user_id).first()
        if user is None or not user.is_active:
            raise credentials_exception
        # Update last_used_at (fire-and-forget; don't fail the request if this errors)
        try:
            api_token.last_used_at = datetime.now(timezone.utc)
            db.commit()
        except Exception:
            db.rollback()
        return user

    # --- Fall back to JWT ---
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None or not user.is_active:
        raise credentials_exception
    return user


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "org"


def _get_or_create_org(
    org_slug: Optional[str], org_name: Optional[str], org_website_url: Optional[str], db: Session,
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
    org = db.query(Organization).filter(Organization.slug == slug).first()
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
    name = org_name or (_get_setting("org_name", db) if slug == "default" else None) or slug.replace("-", " ").title()
    org = Organization(name=name, slug=slug, website_url=website or None)
    db.add(org)
    db.flush()
    return org, True


def _delete_orphaned_orgs(org_ids: set[int], db: Session) -> None:
    """Deletes any of the given orgs that now have zero memberships — call
    this AFTER deleting a user (with their org_ids captured beforehand),
    since a rejected/deleted user could have been an org's only member (most
    commonly its founder, still awaiting super admin approval). Otherwise
    the org is orphaned forever: it keeps showing up in the "join an
    existing organization" picker with no one left who could ever approve a
    join request (issue #1 follow-up)."""
    for org_id in org_ids:
        remaining = db.query(func.count(OrganizationMembership.id)).filter(
            OrganizationMembership.org_id == org_id
        ).scalar()
        if remaining == 0:
            org = db.query(Organization).filter(Organization.id == org_id).first()
            if org:
                db.delete(org)


@app.post("/auth/register", response_model=UserOut, status_code=201)
@limiter.limit("5/minute")
def register(request: Request, data: UserCreate, db: Session = Depends(get_db)):
    if _captcha_configured() and not _verify_captcha(data.captcha_token, get_remote_address(request)):
        raise HTTPException(400, "Verification failed — please try again.")
    if db.query(User).filter(User.callsign == data.callsign).first():
        raise HTTPException(400, "Callsign already registered")
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(400, "Email already registered")

    # First registered user becomes (super) admin and is immediately active,
    # independent of org — is_admin bypasses org scoping entirely.
    is_first_user = db.query(User).count() == 0
    # The bootstrap admin is trusted implicitly (they had server access to deploy this at
    # all) and skips verification so a first-run SMTP misconfiguration can't lock them out.
    needs_verification = _smtp_configured() and not is_first_user
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
    org, org_created = _get_or_create_org(data.org_slug, data.org_name, data.org_website_url, db)
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
    db.commit()
    db.refresh(user)

    db.add(OrganizationMembership(
        org_id=org.id,
        user_id=user.id,
        role="admin" if org_created else "member",
        approved=membership_approved,
    ))
    db.commit()

    if needs_verification:
        verify_link = _app_url(f"/auth/verify-email?token={verification_token}")
        send_email(
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
                db.query(User)
                .filter(User.is_admin == True, User.notify_new_registrations == True, User.is_active == True)
                .all()
            )
            if notify_admins:
                send_email(
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
                db.query(User)
                .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
                .filter(
                    OrganizationMembership.org_id == org.id,
                    OrganizationMembership.role == "admin",
                    OrganizationMembership.approved == True,
                    User.notify_new_registrations == True,
                    User.is_active == True,
                )
                .all()
            )
            if notify_admins:
                send_email(
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


@app.get("/auth/verify-email", include_in_schema=False)
def verify_email(token: str, db: Session = Depends(get_db)):
    """Public link clicked from the verification email. Redirects back to the
    login page with a query param the frontend uses to show a result toast."""
    if not token:
        return RedirectResponse(url="/?verified=0")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    user = db.query(User).filter(User.verification_token == token_hash).first()
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
    db.commit()
    return RedirectResponse(url="/?verified=1")


class SetPasswordRequest(BaseModel):
    token: str
    password: str


@app.post("/auth/set-password", response_model=Token)
@limiter.limit("10/minute")
def set_password(request: Request, data: SetPasswordRequest, db: Session = Depends(get_db)):
    """Redeems the invite link from an admin-created account's "set your
    password" email (issue #1 follow-up) — the account already exists and is
    active, but hashed_password is an unusable random placeholder until this
    runs. Logs the user straight in on success, same response shape as
    /auth/login, since they have no password to log in with beforehand."""
    if len(data.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    token_hash = hashlib.sha256(data.token.encode()).hexdigest()
    user = db.query(User).filter(User.password_set_token == token_hash).first()
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
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": str(user.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": token, "token_type": "bearer", "user": user}


@app.post("/auth/login", response_model=Token)
@limiter.limit("10/minute")
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    captcha_token: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if _captcha_configured() and not _verify_captcha(captcha_token, get_remote_address(request)):
        _log_auth_fail(request, f"captcha_failed username={form_data.username!r}")
        raise HTTPException(status_code=400, detail="Verification failed — please try again.")
    # Accept callsign or email as username
    user = (
        db.query(User).filter(User.callsign == form_data.username.upper()).first()
        or db.query(User).filter(User.email == form_data.username.lower()).first()
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


@app.get("/auth/config")
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
        if CAPTCHA_PROVIDER == "turnstile":
            site_key = TURNSTILE_SITE_KEY
        elif CAPTCHA_PROVIDER == "recaptcha":
            site_key = RECAPTCHA_SITE_KEY
    return {
        "captcha_provider": CAPTCHA_PROVIDER if configured else None,
        "captcha_site_key": site_key,
        # Deprecated aliases, kept for any external client still reading the
        # old shape — TURNSTILE_SITE_KEY doubles as the "enabled" flag's
        # provider check since Turnstile was the only option before.
        "turnstile_enabled": configured and CAPTCHA_PROVIDER == "turnstile",
        "turnstile_site_key": site_key if CAPTCHA_PROVIDER == "turnstile" else None,
    }


@app.get("/captcha/altcha-challenge")
@limiter.limit("30/minute")
def altcha_challenge(request: Request):
    """Public, unauthenticated — issues a fresh ALTCHA proof-of-work
    challenge. The <altcha-widget> on the login/register page fetches this
    itself (via its challengeurl attribute) and solves it client-side; no
    external network call is involved on either side."""
    if CAPTCHA_PROVIDER != "altcha":
        raise HTTPException(404, "ALTCHA is not the active CAPTCHA provider")
    try:
        import altcha
    except ImportError:
        _captcha_log.error("CAPTCHA_PROVIDER=altcha but the altcha package isn't installed — pip install altcha")
        raise HTTPException(500, "ALTCHA is misconfigured on this server — the altcha package isn't installed")
    challenge = altcha.create_challenge_v1(
        hmac_key=ALTCHA_HMAC_KEY,
        max_number=100_000,
        expires=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return challenge.to_dict()


@app.get("/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@app.patch("/auth/theme", response_model=UserOut)
def update_theme(
    data: ThemeUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_user.theme = data.theme
    db.commit()
    db.refresh(current_user)
    return current_user


@app.patch("/auth/gmrs-callsign", response_model=UserOut)
def update_gmrs_callsign(
    data: GmrsCallsignUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Self-service: set or clear the operator's own GMRS callsign (issue #23)
    — separate from their amateur callsign, used as Net Control on GMRS nets."""
    current_user.gmrs_callsign = (data.gmrs_callsign or "").strip().upper() or None
    db.commit()
    db.refresh(current_user)
    return current_user


# ---------------------------------------------------------------------------
# Organizations (issue #1 — multi-tenancy)
# ---------------------------------------------------------------------------

def require_org_admin(org_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    """Org-scoped equivalent of require_admin — an approved admin of THIS org,
    or a super admin (User.is_admin bypasses org scoping everywhere, including
    here)."""
    if current_user.is_admin:
        return current_user
    membership = db.query(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id,
        OrganizationMembership.user_id == current_user.id,
        OrganizationMembership.role == "admin",
        OrganizationMembership.approved == True,
    ).first()
    if not membership:
        raise HTTPException(403, "Organization admin access required")
    return current_user


@app.get("/orgs", response_model=list[OrganizationOut])
def list_orgs(db: Session = Depends(get_db)):
    """Organizations that actually have someone who could approve a join
    request — name+slug only — powers the "join an existing organization"
    picker at registration. No auth required: same trust level as
    callsign/name being visible in the registration form itself, and an
    org's existence isn't sensitive. Excludes an org with no approved admin
    (e.g. its founder was rejected/deleted before anyone else joined) —
    _delete_orphaned_orgs() cleans those up outright, but this filter is a
    second line of defense against ever listing a dead-end org (issue #1
    follow-up)."""
    return (
        db.query(Organization)
        .join(OrganizationMembership, OrganizationMembership.org_id == Organization.id)
        .filter(OrganizationMembership.role == "admin", OrganizationMembership.approved == True)
        .distinct()
        .order_by(Organization.name)
        .all()
    )


@app.get("/orgs/mine", response_model=list[MyOrgOut])
def list_my_orgs(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The current user's own approved organizations, with their role in each
    — powers the org switcher and the org-admin panel visibility check."""
    rows = (
        db.query(Organization, OrganizationMembership.role)
        .join(OrganizationMembership, OrganizationMembership.org_id == Organization.id)
        .filter(OrganizationMembership.user_id == current_user.id, OrganizationMembership.approved == True)
        .order_by(Organization.name)
        .all()
    )
    return [MyOrgOut(id=org.id, name=org.name, slug=org.slug, website_url=org.website_url, role=role) for org, role in rows]


class OrganizationUpdate(BaseModel):
    name: str
    website_url: Optional[str] = None


@app.patch("/orgs/{org_id}", response_model=OrganizationOut)
def update_org(org_id: int, data: OrganizationUpdate, admin: User = Depends(require_org_admin), db: Session = Depends(get_db)):
    """Rename an org / fix its website — previously there was no way to do
    this at all once created (issue #1 follow-up; an org's name is its own
    property, independent of the instance-wide Branding settings, so
    changing Branding doesn't retroactively rename any org). Slug is
    intentionally not editable here — it's baked into public
    /directory/<slug> and /live/<slug> URLs."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(404, "Organization not found")
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Organization name is required")
    website = (data.website_url or "").strip()
    if website and not re.match(r"^https?://", website, re.IGNORECASE):
        raise HTTPException(400, "Organization website URL must start with http:// or https://")
    org.name = name
    org.website_url = website or None
    db.commit()
    db.refresh(org)
    return org


class OrgJoinRequest(BaseModel):
    org_slug: Optional[str] = None
    org_name: Optional[str] = None
    org_website_url: Optional[str] = None


@app.post("/orgs/join", response_model=OrganizationOut, status_code=201)
def join_org(data: OrgJoinRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Already-logged-in self-service: request to join an additional org (or
    create a new one), same join-or-create semantics as registration. Does
    not touch is_active — the caller is already active via an existing org.
    Unlike registration, a newly founded org here is ALWAYS pending (never
    self-approved) — the caller being active elsewhere doesn't make them a
    trustworthy org founder; a super admin still needs to sign off via the
    existing /admin/users/{id}/approve (issue #1 follow-up)."""
    org, org_created = _get_or_create_org(data.org_slug, data.org_name, data.org_website_url, db)
    existing = db.query(OrganizationMembership).filter(
        OrganizationMembership.org_id == org.id, OrganizationMembership.user_id == current_user.id,
    ).first()
    if existing:
        raise HTTPException(400, "Already a member (or pending member) of this organization")
    db.add(OrganizationMembership(
        org_id=org.id,
        user_id=current_user.id,
        role="admin" if org_created else "member",
        approved=False,
    ))
    db.commit()
    return org


class CurrentOrgUpdate(BaseModel):
    org_id: int


@app.patch("/auth/current-org", response_model=UserOut)
def switch_current_org(data: CurrentOrgUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Switch which org the user is "working as" — every net/session/checkin
    endpoint scopes to current_org_id from here on. Restricted to orgs the user
    has an APPROVED membership in (super admins may switch to any org, since
    they already see everything regardless)."""
    if not current_user.is_admin:
        membership = db.query(OrganizationMembership).filter(
            OrganizationMembership.org_id == data.org_id,
            OrganizationMembership.user_id == current_user.id,
            OrganizationMembership.approved == True,
        ).first()
        if not membership:
            raise HTTPException(403, "Not an approved member of that organization")
    else:
        if not db.query(Organization).filter(Organization.id == data.org_id).first():
            raise HTTPException(404, "Organization not found")
    current_user.current_org_id = data.org_id
    db.commit()
    db.refresh(current_user)
    return current_user


@app.get("/orgs/{org_id}/pending-members", response_model=list[OrgMemberOut])
def list_pending_org_members(org_id: int, admin: User = Depends(require_org_admin), db: Session = Depends(get_db)):
    rows = (
        db.query(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.org_id == org_id, OrganizationMembership.approved == False)
        .order_by(OrganizationMembership.created_at.desc())
        .all()
    )
    return [
        OrgMemberOut(
            user_id=u.id, callsign=u.callsign, name=u.name, email=u.email,
            role=m.role, approved=m.approved, requested_at=m.created_at,
        )
        for m, u in rows
    ]


@app.get("/orgs/{org_id}/members", response_model=list[OrgMemberOut])
def list_org_members(org_id: int, admin: User = Depends(require_org_admin), db: Session = Depends(get_db)):
    rows = (
        db.query(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.org_id == org_id, OrganizationMembership.approved == True)
        .order_by(User.callsign)
        .all()
    )
    return [
        OrgMemberOut(
            user_id=u.id, callsign=u.callsign, name=u.name, email=u.email,
            role=m.role, approved=m.approved, requested_at=m.created_at,
        )
        for m, u in rows
    ]


@app.get("/orgs/{org_id}/nets", response_model=list[NetOut])
def list_org_nets(org_id: int, admin: User = Depends(require_org_admin), db: Session = Depends(get_db)):
    """Every net in this org, regardless of ownership or sharing — lets an
    org admin see (and reassign ownership of) every net in their org, not
    just ones they personally own or are shared on (issue follow-up).
    list_nets doesn't do this for non-super-admins: org-admin role alone
    was never a substitute for owning or being shared on a net."""
    nets = db.query(Net).filter(Net.org_id == org_id).order_by(Net.name).all()
    return [_net_to_out(n, admin, db) for n in nets]


@app.patch("/orgs/{org_id}/members/{user_id}/approve", status_code=204)
def approve_org_member(org_id: int, user_id: int, admin: User = Depends(require_org_admin), db: Session = Depends(get_db)):
    membership = db.query(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id, OrganizationMembership.user_id == user_id,
    ).first()
    if not membership:
        raise HTTPException(404, "Membership not found")
    membership.approved = True
    user = db.query(User).filter(User.id == user_id).first()
    # Only their FIRST approved org needs to flip is_active — a user already
    # active via another org just needed this specific membership approved.
    if user and not user.is_active:
        user.is_active = True
        user.email_verified = True
        user.verification_token = None
    db.commit()

    if user:
        login_link = _app_url("/")
        send_email(
            to=[user.email],
            subject="[NetControl Online] Your Account Has Been Approved",
            body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">Account Approved!</h2>
  <p>Hello <strong>{user.name}</strong> ({user.callsign}),</p>
  <p>Your request to join has been approved. You can now log in and start using the system.</p>
  {f'<p style="margin-top:16px"><a href="{login_link}" style="background:#FF9900;color:#000;padding:10px 20px;text-decoration:none;border-radius:20px;font-weight:bold;display:inline-block">Log In Now</a></p>' if login_link else ''}
  <p style="color:#888;font-size:12px">If you did not request this account, please disregard this message.</p>
</div>""",
            body_text=(
                f"Hello {user.name} ({user.callsign}),\n\n"
                f"Your request to join has been approved. You can now log in.\n\n"
                + (f"Log in here: {login_link}\n\n" if login_link else "")
                + "If you did not request this account, please disregard this message."
            ),
        )


@app.post("/orgs/{org_id}/members/{user_id}/reject", status_code=204)
def reject_org_member(org_id: int, user_id: int, admin: User = Depends(require_org_admin), db: Session = Depends(get_db)):
    """Rejects (deletes) a pending membership request. Unlike the legacy
    single-tenant /admin/users/{id}/reject, this does NOT delete the user
    account itself — they may hold approved memberships in other orgs, or be
    free to request a different org."""
    membership = db.query(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id, OrganizationMembership.user_id == user_id,
    ).first()
    if not membership:
        raise HTTPException(404, "Membership not found")
    if membership.approved:
        raise HTTPException(400, "Cannot reject an already-approved membership — remove them from the org instead")
    db.delete(membership)
    db.commit()


class OrgMemberRoleUpdate(BaseModel):
    role: Literal["member", "admin"]


@app.patch("/orgs/{org_id}/members/{user_id}/role", response_model=OrgMemberOut)
def update_org_member_role(
    org_id: int, user_id: int, data: OrgMemberRoleUpdate,
    admin: User = Depends(require_org_admin), db: Session = Depends(get_db),
):
    """Promote/demote an already-approved member's role within this org —
    previously an org admin could approve or reject a new member but had no
    way to grant admin to someone already in the org, so a single-admin org
    had no way to add a second one without a super admin's help. Changing
    your own role is blocked (mirrors the "can't act on your own account"
    pattern used elsewhere) so an org can't end up with zero admins via a
    single self-demote."""
    if user_id == admin.id:
        raise HTTPException(400, "Cannot change your own role")
    membership = db.query(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id,
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.approved == True,
    ).first()
    if not membership:
        raise HTTPException(404, "Membership not found")
    membership.role = data.role
    db.commit()

    user = db.query(User).filter(User.id == user_id).first()
    return OrgMemberOut(
        user_id=user.id, callsign=user.callsign, name=user.name, email=user.email,
        role=membership.role, approved=membership.approved, requested_at=membership.created_at,
    )


class OrgUserCreate(BaseModel):
    callsign: str
    name: str
    email: EmailStr
    gmrs_callsign: Optional[str] = None
    role: Literal["member", "admin"] = "member"

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        return v.upper().strip()


@app.post("/orgs/{org_id}/users", response_model=AdminUserOut, status_code=201)
def create_org_user(org_id: int, data: OrgUserCreate, admin: User = Depends(require_org_admin), db: Session = Depends(get_db)):
    """Admin-seeds an operator account directly — for bringing existing
    operators onto the org without a self-registration/approval round trip
    (issue #1 follow-up). Auto-approved (the admin creating it IS the
    approval): is_active is already True, but hashed_password is an unusable
    random placeholder, so login is impossible until the operator follows
    the emailed link to set their own password. Requires SMTP to be
    configured — otherwise the account would be created with no way to ever
    become usable."""
    if not _smtp_configured():
        raise HTTPException(400, "Email must be configured (Admin → Email) before creating operator accounts this way — the invite link is sent by email.")
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(404, "Organization not found")
    if db.query(User).filter(User.callsign == data.callsign).first():
        raise HTTPException(400, "Callsign already registered")
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(400, "Email already registered")

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    user = User(
        callsign=data.callsign,
        name=data.name,
        email=data.email,
        gmrs_callsign=(data.gmrs_callsign or "").strip().upper() or None,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        is_active=True,
        is_admin=False,
        email_verified=True,   # vouched for by the org admin who entered it
        current_org_id=org.id,
        password_set_token=token_hash,
        password_set_sent_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    db.add(OrganizationMembership(org_id=org.id, user_id=user.id, role=data.role, approved=True))
    db.commit()

    set_link = _app_url(f"/?setpw={raw_token}")
    send_email(
        to=[user.email],
        subject=f"[NetControl Online] You've Been Added to {org.name}",
        body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">Welcome to {html.escape(org.name)}</h2>
  <p>Hello <strong>{user.name}</strong> ({user.callsign}),</p>
  <p>An administrator has created an account for you on NetControl Online, part of <strong>{html.escape(org.name)}</strong>. Set a password to get started:</p>
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

    return AdminUserOut(
        **UserOut.model_validate(user).model_dump(),
        org_name=org.name,
        org_website_url=org.website_url,
    )


@app.get("/stats")
def get_stats(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Quick stats for the sidebar dashboard panel."""
    from datetime import date, datetime, timezone

    # Net IDs the user can see (owned + shared), scoped to their current org (issue #1)
    owned_ids = [
        r[0] for r in
        db.query(Net.id).filter(Net.owner_id == current_user.id, Net.org_id == current_user.current_org_id).all()
    ]
    shared_ids = [
        r[0] for r in
        db.query(NetShare.net_id)
        .join(Net, Net.id == NetShare.net_id)
        .filter(NetShare.user_id == current_user.id, Net.org_id == current_user.current_org_id)
        .all()
    ]
    all_net_ids = list(set(owned_ids + shared_ids))

    total_nets = len(all_net_ids)

    active_sessions = 0
    checkins_today = 0
    if all_net_ids:
        active_sessions = (
            db.query(func.count(NetSession.id))
            .filter(NetSession.net_id.in_(all_net_ids), NetSession.ended_at.is_(None))
            .scalar() or 0
        )
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        checkins_today = (
            db.query(func.count(Checkin.id))
            .join(NetSession, Checkin.session_id == NetSession.id)
            .filter(NetSession.net_id.in_(all_net_ids), Checkin.checked_in_at >= today_start)
            .scalar() or 0
        )

    gmrs_row = db.query(SystemSetting).filter(SystemSetting.key == "gmrs_db_synced_at").first()

    return {
        "total_nets": total_nets,
        "active_sessions": active_sessions,
        "checkins_today": checkins_today,
        "gmrs_synced_at": gmrs_row.value[:10] if gmrs_row and gmrs_row.value else None,
    }


# ---------------------------------------------------------------------------
# API Token management
# ---------------------------------------------------------------------------

@app.post("/auth/tokens", response_model=ApiTokenCreated, status_code=201)
def create_api_token(
    data: ApiTokenCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
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
    db.commit()
    db.refresh(api_token)
    return ApiTokenCreated(id=api_token.id, name=api_token.name, token=raw_token, created_at=api_token.created_at)


@app.get("/auth/tokens", response_model=list[ApiTokenOut])
def list_api_tokens(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.query(ApiToken).filter(ApiToken.user_id == current_user.id).all()


@app.delete("/auth/tokens/{token_id}", status_code=204)
def delete_api_token(
    token_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    api_token = db.query(ApiToken).filter(ApiToken.id == token_id, ApiToken.user_id == current_user.id).first()
    if not api_token:
        raise HTTPException(404, "Token not found")
    db.delete(api_token)
    db.commit()


# ---------------------------------------------------------------------------
# Public live page
# ---------------------------------------------------------------------------

def _inject_seo_meta(html_content: str, *, title: str, description: str, canonical_path: str, request: Request) -> str:
    """Overwrite the placeholder SEO tags (see the id="seo-*" elements in
    directory.html/public.html) with org-specific values before serving.
    Done server-side, not by the pages' own client-side JS, because search
    crawlers and link-preview bots (Slack, social media) generally read only
    the initial HTML response and don't execute JavaScript — a client-side
    document.title update alone would be invisible to them."""
    canonical_url = str(request.base_url).rstrip("/") + canonical_path
    esc_title = html.escape(title)
    esc_desc = html.escape(description)
    esc_url = html.escape(canonical_url)
    replacements = {
        '<title id="seo-title">Net Directory — NetControl Online</title>': f'<title id="seo-title">{esc_title}</title>',
        '<title id="seo-title">Live Nets — NetControl Online</title>': f'<title id="seo-title">{esc_title}</title>',
        'id="seo-description" name="description" content="Browse amateur radio and GMRS nets — schedules, frequencies, and how to check in."':
            f'id="seo-description" name="description" content="{esc_desc}"',
        'id="seo-description" name="description" content="See which amateur radio and GMRS nets are on the air right now, with live check-in rosters."':
            f'id="seo-description" name="description" content="{esc_desc}"',
        'id="seo-canonical" rel="canonical" href="/directory"': f'id="seo-canonical" rel="canonical" href="{esc_url}"',
        'id="seo-canonical" rel="canonical" href="/live"': f'id="seo-canonical" rel="canonical" href="{esc_url}"',
        'id="seo-og-title" property="og:title" content="Net Directory — NetControl Online"': f'id="seo-og-title" property="og:title" content="{esc_title}"',
        'id="seo-og-title" property="og:title" content="Live Nets — NetControl Online"': f'id="seo-og-title" property="og:title" content="{esc_title}"',
        'id="seo-og-description" property="og:description" content="Browse amateur radio and GMRS nets — schedules, frequencies, and how to check in."':
            f'id="seo-og-description" property="og:description" content="{esc_desc}"',
        'id="seo-og-description" property="og:description" content="See which amateur radio and GMRS nets are on the air right now, with live check-in rosters."':
            f'id="seo-og-description" property="og:description" content="{esc_desc}"',
        'id="seo-og-url" property="og:url" content="/directory"': f'id="seo-og-url" property="og:url" content="{esc_url}"',
        'id="seo-og-url" property="og:url" content="/live"': f'id="seo-og-url" property="og:url" content="{esc_url}"',
    }
    for old, new in replacements.items():
        html_content = html_content.replace(old, new)
    return html_content


@app.get("/live", response_class=HTMLResponse, include_in_schema=False)
@app.get("/live/{org_slug}", response_class=HTMLResponse, include_in_schema=False)
def public_live_page(request: Request, org_slug: Optional[str] = None, db: Session = Depends(get_db)):
    """Serve the public live nets page. org_slug (issue #1), if present, is
    read client-side from the URL path — same SPA path-routing convention as
    /directory/{slug} below. Bare /live with no slug renders an org picker.
    Title/description/canonical are also injected server-side per org (see
    _inject_seo_meta) for crawlers and link-preview bots that don't run JS."""
    import pathlib
    content = (pathlib.Path(__file__).parent / "public.html").read_text()
    if org_slug:
        org = db.query(Organization).filter(Organization.slug == org_slug).first()
        if org:
            content = _inject_seo_meta(
                content,
                title=f"Live Nets — {org.name}",
                description=f"See which amateur radio and GMRS nets are on the air right now for {org.name}, with live check-in rosters.",
                canonical_path=f"/live/{org_slug}",
                request=request,
            )
    return HTMLResponse(content)


@app.get("/public/active")
def public_active_sessions(org: Optional[str] = None, db: Session = Depends(get_db)):
    """Return all currently active net sessions for one org — no auth
    required. Org-scoped (issue #1); omitting `org` falls back to the
    "default" org (single-tenant backward compat — see _get_or_create_org).
    Deliberately NOT gated on Net.public_listed, unlike /public/directory —
    this page has always shown any net currently in progress in the org,
    listed or not (see TestSchedules::test_public_active_shows_broadcaster)."""
    org_row = db.query(Organization).filter(Organization.slug == (org or "default")).first()
    if not org_row:
        return []
    sessions = (
        db.query(NetSession)
        .join(Net, Net.id == NetSession.net_id)
        .filter(NetSession.ended_at == None, Net.org_id == org_row.id)
        .order_by(NetSession.started_at)
        .all()
    )
    result = []
    for s in sessions:
        net = db.query(Net).filter(Net.id == s.net_id).first()
        if not net:
            continue
        count = db.query(func.count(Checkin.id)).filter(Checkin.session_id == s.id).scalar()
        result.append({
            "session_id": s.id,
            "net_name": net.name,
            "frequency": net.frequency,
            "started_at": s.started_at.isoformat(),
            "checkin_count": count,
            **_duty_labels_for_session(net, s, db),
        })
    return result


@app.get("/public/sessions/{session_id}")
def public_session_detail(session_id: int, db: Session = Depends(get_db)):
    """Return session info + checkin list — no auth required. Keyed directly
    by session ID (reached by clicking through from the already org-scoped
    /public/active list), so no separate org check is needed here."""
    s = db.query(NetSession).filter(NetSession.id == session_id, NetSession.ended_at == None).first()
    if not s:
        raise HTTPException(404, "Session not found or no longer active")
    net = db.query(Net).filter(Net.id == s.net_id).first()
    checkins = (
        db.query(Checkin)
        .filter(Checkin.session_id == session_id)
        .order_by(Checkin.checked_in_at)
        .all()
    )
    duty = _duty_labels_for_session(net, s, db) if net else {
        "ncs_callsign": None, "ncs_name": None,
        "broadcaster_callsign": None, "broadcaster_name": None, "broadcast_label": None,
        "next_ncs_callsign": None, "next_ncs_name": None,
        "next_broadcaster_callsign": None, "next_broadcaster_name": None,
    }
    return {
        "session_id": s.id,
        "net_name": net.name if net else "Unknown Net",
        "frequency": net.frequency if net else None,
        "started_at": s.started_at.isoformat(),
        "checkins": [
            {"callsign": c.callsign, "name": c.name}
            for c in checkins
        ],
        **duty,
    }


@app.get("/directory", response_class=HTMLResponse, include_in_schema=False)
@app.get("/directory/{org_slug}", response_class=HTMLResponse, include_in_schema=False)
def public_directory_page(request: Request, org_slug: Optional[str] = None, db: Session = Depends(get_db)):
    """Serve the public net directory page. org_slug (issue #1), if present,
    is read client-side from the URL path — the frontend calls
    /public/directory?org=<slug> accordingly. Bare /directory with no slug
    renders an org picker (from /public/organizations) instead of a net list.
    Title/description/canonical are also injected server-side per org (see
    _inject_seo_meta) for crawlers and link-preview bots that don't run JS."""
    content = (_STATIC_DIR / "directory.html").read_text(encoding="utf-8")
    if org_slug:
        org = db.query(Organization).filter(Organization.slug == org_slug).first()
        if org:
            canonical_path = f"/directory/{org_slug}"
            content = _inject_seo_meta(
                content,
                title=f"{org.name} Net Directory",
                description=f"Amateur radio and GMRS net schedules for {org.name} — frequencies, meeting times, and how to check in.",
                canonical_path=canonical_path,
                request=request,
            )
            jsonld = {
                "@context": "https://schema.org",
                "@type": "Organization",
                "name": org.name,
                "url": org.website_url or (str(request.base_url).rstrip("/") + canonical_path),
            }
            # Escaping "</" within the JSON body (only) guards against the org
            # name breaking out of the <script> tag if it ever contained that
            # sequence — applied before wrapping, so the real closing tag is untouched.
            jsonld_str = json.dumps(jsonld).replace("</", "<\\/")
            script = f'<script type="application/ld+json">{jsonld_str}</script>'
            content = content.replace("<!--SEO_JSONLD-->", script)
    return HTMLResponse(content)


@app.get("/public/organizations", response_model=list[OrganizationOut])
def public_organizations(db: Session = Depends(get_db)):
    """Orgs with at least one net in the public directory — powers the org
    picker shown at bare /directory or /live (no slug in the URL)."""
    return (
        db.query(Organization)
        .join(Net, Net.org_id == Organization.id)
        .filter(Net.public_listed == True)
        .distinct()
        .order_by(Organization.name)
        .all()
    )


@app.get("/public/directory")
def public_directory(org: Optional[str] = None, db: Session = Depends(get_db)):
    """Return every net whose owner has opted into the public directory, for
    one org — no auth required. Org-scoped (issue #1); omitting `org` falls
    back to the "default" org (single-tenant backward compat)."""
    org_row = db.query(Organization).filter(Organization.slug == (org or "default")).first()
    if not org_row:
        return []
    nets = (
        db.query(Net)
        .filter(Net.public_listed == True, Net.org_id == org_row.id)
        .order_by(Net.name)
        .all()
    )
    result = []
    for net in nets:
        owner = db.query(User).filter(User.id == net.owner_id).first()
        schedules = (
            db.query(NetSchedule)
            .filter(NetSchedule.net_id == net.id)
            .order_by(NetSchedule.day_of_week)
            .all()
        )
        result.append({
            "id": net.id,
            "name": net.name,
            "net_type": net.net_type,
            "frequency": net.frequency,
            "description": net.description,
            "has_broadcast": net.has_broadcast,
            "broadcast_label": net.broadcast_label,
            "owner_callsign": owner.callsign if owner else None,
            "owner_name": owner.name if owner else None,
            "schedules": [_schedule_to_out(s) for s in schedules],
        })
    return result


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

BRANDING_KEYS = ("org_name", "tagline", "website_url")


def _get_setting(key: str, db: Session) -> Optional[str]:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row.value if row else None


def _set_setting(key: str, value: Optional[str], db: Session):
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row:
        row.value = value
        row.updated_at = utcnow()
    else:
        db.add(SystemSetting(key=key, value=value))


def _logo_file() -> Optional[pathlib.Path]:
    """Return the logo file path if one exists (any image extension)."""
    for ext in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        p = UPLOADS_DIR / f"logo.{ext}"
        if p.exists():
            return p
    return None


@app.get("/branding", response_model=BrandingOut)
def get_branding(db: Session = Depends(get_db)):
    """Public endpoint — returns current branding settings."""
    return BrandingOut(
        org_name=_get_setting("org_name", db),
        tagline=_get_setting("tagline", db),
        website_url=_get_setting("website_url", db),
        has_logo=_logo_file() is not None,
    )


@app.get("/logo")
def get_logo():
    """Public endpoint — serves the uploaded logo file."""
    p = _logo_file()
    if not p:
        raise HTTPException(404, "No logo uploaded")
    ext = p.suffix.lstrip(".")
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")
    return Response(content=p.read_bytes(), media_type=mime)


@app.put("/admin/branding", response_model=BrandingOut)
def update_branding(
    data: BrandingUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Admin only — update branding text settings."""
    if not current_user.is_admin:
        raise HTTPException(403, "Admin only")
    _set_setting("org_name", data.org_name or None, db)
    _set_setting("tagline", data.tagline or None, db)
    _set_setting("website_url", data.website_url or None, db)
    db.commit()
    return BrandingOut(
        org_name=_get_setting("org_name", db),
        tagline=_get_setting("tagline", db),
        website_url=_get_setting("website_url", db),
        has_logo=_logo_file() is not None,
    )


@app.post("/admin/branding/logo", status_code=204)
async def upload_logo(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Admin only — upload a logo image (PNG, JPG, GIF, WebP, SVG)."""
    if not current_user.is_admin:
        raise HTTPException(403, "Admin only")
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        raise HTTPException(400, "Unsupported file type — use PNG, JPG, GIF, WebP, or SVG")
    # Remove any old logo files
    for old in UPLOADS_DIR.glob("logo.*"):
        old.unlink(missing_ok=True)
    dest = UPLOADS_DIR / f"logo.{ext}"
    dest.write_bytes(await file.read())


@app.delete("/admin/branding/logo", status_code=204)
def delete_logo(current_user: User = Depends(get_current_user)):
    """Admin only — remove the current logo."""
    if not current_user.is_admin:
        raise HTTPException(403, "Admin only")
    for old in UPLOADS_DIR.glob("logo.*"):
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Support tickets
# ---------------------------------------------------------------------------

class SupportTicketCreate(BaseModel):
    type: str
    subject: str
    body: str


SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "")   # helpdesk address for support tickets

@app.post("/support/ticket", status_code=204)
def create_support_ticket(
    data: SupportTicketCreate,
    current_user: User = Depends(get_current_user),
):
    if not _smtp_configured():
        raise HTTPException(503, "Email is not configured on this server")
    if not SUPPORT_EMAIL:
        raise HTTPException(503, "Support email address is not configured on this server")
    if not data.subject.strip() or not data.body.strip():
        raise HTTPException(400, "Subject and body are required")

    subject = f"[NetControl Online] {data.type}: {data.subject.strip()}"
    body_html = f"""
<p><strong>From:</strong> {current_user.name} ({current_user.callsign})<br>
<strong>Email:</strong> {current_user.email}<br>
<strong>Type:</strong> {data.type}</p>
<hr>
<p>{data.body.replace(chr(10), '<br>')}</p>
<hr>
<p style="color:#888;font-size:12px">Sent from NetControl Online by {current_user.callsign} — reply to this email to respond directly to the user.</p>
"""
    body_text = (
        f"From: {current_user.name} ({current_user.callsign})\n"
        f"Email: {current_user.email}\n"
        f"Type: {data.type}\n\n"
        f"{data.body}\n\n"
        f"---\nReply to: {current_user.email}"
    )

    sent = send_email(
        to=[SUPPORT_EMAIL],
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        reply_to=f"{current_user.name} <{current_user.email}>",
    )
    if not sent:
        raise HTTPException(500, "Failed to send email — please try again later")
    _email_log.info("Support ticket sent from %s — %s", current_user.callsign, subject)


# ---------------------------------------------------------------------------
# Net routes
# ---------------------------------------------------------------------------

@app.get("/users", response_model=list[UserPublicOut])
def list_users(net_id: Optional[int] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return active users in scope for a share/assignment picker (issue #1).
    Defaults to the caller's own current org. Pass net_id to scope to THAT
    net's org instead — necessary once a super admin can edit any net
    regardless of their own current_org_id, and once Reassign (issue #1
    follow-up) can move a net (or its shared/assigned users) to an org other
    than whichever one the caller happens to be working as right now; without
    this, already-shared users from the net's actual org would silently not
    even appear as selectable in the sharing/schedule pickers."""
    org_id = current_user.current_org_id
    if net_id is not None:
        org_id = _get_editable_net(net_id, current_user, db).org_id

    users = (
        db.query(User)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .filter(
            User.is_active == True,
            User.id != current_user.id,
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.approved == True,
        )
        .order_by(User.callsign)
        .all()
    )
    return users


@app.get("/nets", response_model=list[NetOut])
def list_nets(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.is_admin:
        # Super admins see every net, across every org
        nets = db.query(Net).order_by(Net.name).all()
    else:
        # Owned nets + nets shared with this user + nets shared with all,
        # scoped to the org the user is currently working as (issue #1)
        shared_net_ids = (
            db.query(NetShare.net_id)
            .filter(or_(NetShare.user_id == current_user.id, NetShare.user_id == None))
            .scalar_subquery()
        )
        nets = (
            db.query(Net)
            .filter(
                Net.org_id == current_user.current_org_id,
                or_(Net.owner_id == current_user.id, Net.id.in_(shared_net_ids)),
            )
            .order_by(Net.name)
            .all()
        )
    return [_net_to_out(n, current_user, db) for n in nets]


@app.post("/nets", response_model=NetOut, status_code=201)
def create_net(data: NetCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.current_org_id:
        raise HTTPException(400, "No current organization selected")
    net_type = data.net_type if data.net_type in ("ham", "gmrs") else "ham"
    net = Net(
        name=data.name,
        frequency=data.frequency,
        description=data.description,
        net_type=net_type,
        is_ares=data.is_ares if net_type == "ham" else False,
        dmr_talkgroup=data.dmr_talkgroup or None if net_type == "ham" else None,
        script=data.script,
        has_broadcast=data.has_broadcast,
        broadcast_label=(data.broadcast_label or None) if data.has_broadcast else None,
        reminder_enabled=data.reminder_enabled,
        reminder_minutes_before=(data.reminder_minutes_before or 30) if data.reminder_enabled else None,
        public_listed=data.public_listed,
        band=data.band or None,
        mode=data.mode or None,
        ctcss_tone=data.ctcss_tone or None,
        region=data.region or None,
        state=data.state or None,
        website=data.website or None,
        owner_id=current_user.id,
        org_id=current_user.current_org_id,
    )
    db.add(net)
    db.commit()
    db.refresh(net)
    net_repository.push_net(net, db)
    return _net_to_out(net, current_user, db)


@app.get("/nets/{net_id}", response_model=NetOut)
def get_net(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    net = _get_net_for_user(net_id, current_user, db)
    return _net_to_out(net, current_user, db)


@app.put("/nets/{net_id}", response_model=NetOut)
def update_net(net_id: int, data: NetCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    net = _get_editable_net(net_id, current_user, db)
    net_type = data.net_type if data.net_type in ("ham", "gmrs") else "ham"
    net.name = data.name
    net.frequency = data.frequency
    net.description = data.description
    net.net_type = net_type
    net.is_ares = data.is_ares if net_type == "ham" else False
    net.dmr_talkgroup = data.dmr_talkgroup or None if net_type == "ham" else None
    net.script = data.script
    net.has_broadcast = data.has_broadcast
    net.broadcast_label = (data.broadcast_label or None) if data.has_broadcast else None
    net.reminder_enabled = data.reminder_enabled
    net.reminder_minutes_before = (data.reminder_minutes_before or 30) if data.reminder_enabled else None
    net.public_listed = data.public_listed
    net.band = data.band or None
    net.mode = data.mode or None
    net.ctcss_tone = data.ctcss_tone or None
    net.region = data.region or None
    net.state = data.state or None
    net.website = data.website or None
    db.commit()
    db.refresh(net)
    net_repository.push_net(net, db)
    return _net_to_out(net, current_user, db)


@app.patch("/nets/{net_id}/owner", response_model=NetOut)
def transfer_net_owner(net_id: int, data: NetOwnerUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Reassign a net's owner — previously the only way to change who
    controls a net was deleting and recreating it (issue follow-up).
    Available to the net's current owner (hand off to someone else), an
    admin of the net's own org, or a super admin. The new owner must
    already be an approved member of the net's org — unlike Move a Net
    (which only warns about this, since the admin may fix it in either
    order), this is a deliberate single assignment so it's enforced
    outright rather than left as a warning."""
    net = db.query(Net).filter(Net.id == net_id).first()
    if not net:
        raise HTTPException(404, "Net not found")
    if not current_user.is_admin:
        if net.org_id != current_user.current_org_id:
            raise HTTPException(404, "Net not found")
        is_owner = net.owner_id == current_user.id
        is_org_admin = db.query(OrganizationMembership).filter(
            OrganizationMembership.org_id == net.org_id,
            OrganizationMembership.user_id == current_user.id,
            OrganizationMembership.role == "admin",
            OrganizationMembership.approved == True,
        ).first() is not None
        if not (is_owner or is_org_admin):
            raise HTTPException(403, "Not your net")

    new_owner = db.query(User).filter(User.id == data.owner_id).first()
    if not new_owner:
        raise HTTPException(404, "User not found")
    is_member = db.query(OrganizationMembership).filter(
        OrganizationMembership.org_id == net.org_id,
        OrganizationMembership.user_id == new_owner.id,
        OrganizationMembership.approved == True,
    ).first() is not None
    if not is_member:
        raise HTTPException(400, f"{new_owner.callsign} is not a member of this net's organization")

    net.owner_id = new_owner.id
    db.commit()
    db.refresh(net)
    return _net_to_out(net, current_user, db)


@app.delete("/nets/{net_id}", status_code=204)
def delete_net(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    net = _get_owned_net(net_id, current_user, db)
    db.delete(net)
    db.commit()


# ---------------------------------------------------------------------------
# Session routes
# ---------------------------------------------------------------------------

@app.get("/nets/{net_id}/sessions", response_model=list[SessionOut])
def list_sessions(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_net_for_user(net_id, current_user, db)
    sessions = (
        db.query(NetSession)
        .filter(NetSession.net_id == net_id)
        .order_by(NetSession.started_at.desc())
        .all()
    )
    result = []
    for s in sessions:
        count = db.query(func.count(Checkin.id)).filter(Checkin.session_id == s.id).scalar()
        out = SessionOut.model_validate(s)
        out.checkin_count = count
        result.append(out)
    return result


@app.post("/nets/{net_id}/sessions", response_model=SessionOut, status_code=201)
def start_session(net_id: int, data: SessionCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    net = _get_net_for_user(net_id, current_user, db)
    if data.is_offline and not data.occurred_at:
        raise HTTPException(400, "occurred_at is required for an offline net entry")
    session = NetSession(
        net_id=net_id, operator_id=current_user.id, name=data.name, notes=data.notes,
        broadcaster_override_callsign=(data.broadcaster_override_callsign or "").strip().upper() or None,
        broadcaster_override_name=(data.broadcaster_override_name or "").strip() or None,
        is_activation=data.is_activation if net.is_ares else False,
        is_offline=data.is_offline,
        ncs_override_callsign=(data.ncs_override_callsign or "").strip().upper() or None,
        ncs_override_name=(data.ncs_override_name or "").strip() or None,
    )
    if data.is_offline:
        session.started_at = data.occurred_at
    db.add(session)
    db.commit()
    db.refresh(session)

    if data.is_offline:
        # No live view for a backfilled entry (issue #20) -- put it straight into
        # the "ended" state at the reported timestamp. add_checkin() specifically
        # lets checkins through despite ended_at being set for sessions like this.
        session.ended_at = session.started_at
        db.commit()
        db.refresh(session)

    # Auto-create the Net Control tactical position for an activation session, seeded
    # from the same day's-schedule/whoever-started-it resolution routine sessions use,
    # and sign them straight on if known — NCS is live the moment the net starts, and
    # from here on hands off through the same sign-on/off flow as any other position
    # (issue #21 follow-up: routine sessions' single day-level NCS wasn't enough for a
    # multi-hour activation where net control itself rotates).
    if session.is_activation:
        duty = _duty_labels_for_session(net, session, db)
        nc_position = TacticalPosition(
            session_id=session.id,
            tactical_callsign="NET CONTROL",
            is_net_control=True,
            assigned_callsign=duty["ncs_callsign"],
            assigned_name=duty["ncs_name"],
        )
        db.add(nc_position)
        db.commit()
        db.refresh(nc_position)
        if duty["ncs_callsign"]:
            db.add(Checkin(
                session_id=session.id,
                callsign=duty["ncs_callsign"],
                name=duty["ncs_name"],
                has_traffic=False,
                tactical_position_id=nc_position.id,
            ))
            db.commit()

    out = SessionOut.model_validate(session)
    out.checkin_count = 0
    return out


@app.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_session_for_user(session_id, current_user, db)
    count = db.query(func.count(Checkin.id)).filter(Checkin.session_id == session.id).scalar()
    out = SessionOut.model_validate(session)
    out.checkin_count = count
    net = db.query(Net).filter(Net.id == session.net_id).first()
    if net:
        for k, v in _duty_labels_for_session(net, session, db).items():
            setattr(out, k, v)
    return out


@app.patch("/sessions/{session_id}/end", response_model=SessionOut)
def end_session(session_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_session_for_user(session_id, current_user, db)
    # An offline entry (issue #20) already has ended_at set from creation (it's
    # never live), so that can't also signal "done entering data" for these --
    # is_offline_locked is that separate signal, and is what add_checkin() checks
    # for offline sessions instead of ended_at.
    if session.is_offline:
        if session.is_offline_locked:
            raise HTTPException(400, "This logged net has already been closed")
        session.is_offline_locked = True
    else:
        if session.ended_at is not None:
            raise HTTPException(400, "Session already ended")
        session.ended_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)
    count = db.query(func.count(Checkin.id)).filter(Checkin.session_id == session.id).scalar()
    net = db.query(Net).filter(Net.id == session.net_id).first()
    if net:
        net_repository.push_session_stats(net, session, count, db)
    out = SessionOut.model_validate(session)
    out.checkin_count = count
    return out


@app.patch("/sessions/{session_id}/rename", response_model=SessionOut)
def rename_session(session_id: int, data: SessionRename, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_session_for_user(session_id, current_user, db)
    session.name = data.name
    db.commit()
    db.refresh(session)
    count = db.query(func.count(Checkin.id)).filter(Checkin.session_id == session.id).scalar()
    out = SessionOut.model_validate(session)
    out.checkin_count = count
    return out


@app.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_session_for_user(session_id, current_user, db)
    db.delete(session)
    db.commit()


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------

def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.get("/admin/users", response_model=list[AdminUserOut])
def admin_list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """List all users (active, pending, and inactive), with each user's
    current org name/website attached (issue #1 follow-up) — lets a super
    admin verify a pending registration, especially one founding a brand new
    org, without a separate lookup."""
    rows = (
        db.query(User, Organization)
        .outerjoin(Organization, Organization.id == User.current_org_id)
        .order_by(User.created_at.desc())
        .all()
    )
    return [
        AdminUserOut(
            **UserOut.model_validate(u).model_dump(),
            org_name=org.name if org else None,
            org_website_url=org.website_url if org else None,
        )
        for u, org in rows
    ]


@app.patch("/admin/users/{user_id}/approve", response_model=UserOut)
def admin_approve_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Activate a pending user account and notify them by email.

    Also marks the account email-verified: an admin manually approving someone
    is a stronger trust signal than the automated link-click, and it's the only
    way to unblock a user whose verification email never arrived or whose link
    can't work because APP_BASE_URL isn't configured on this instance.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = True
    user.email_verified = True
    user.verification_token = None
    # Super-admin approval is a global escape hatch (issue #1) — clear every
    # pending org membership too, not just the account-level gate, since a
    # super admin isn't scoped to any one org's approval queue.
    db.query(OrganizationMembership).filter(
        OrganizationMembership.user_id == user.id, OrganizationMembership.approved == False,
    ).update({"approved": True})
    db.commit()
    db.refresh(user)

    login_link = _app_url("/")
    send_email(
        to=[user.email],
        subject="[NetControl Online] Your Account Has Been Approved",
        body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">Account Approved!</h2>
  <p>Hello <strong>{user.name}</strong> ({user.callsign}),</p>
  <p>Your NetControl Online account has been reviewed and approved. You can now log in and start using the system.</p>
  {f'<p style="margin-top:16px"><a href="{login_link}" style="background:#FF9900;color:#000;padding:10px 20px;text-decoration:none;border-radius:20px;font-weight:bold;display:inline-block">Log In Now</a></p>' if login_link else ''}
  {f'<p style="color:#888;font-size:12px">This email box is not monitored. If you have any questions please email <a href="mailto:{ADMIN_CONTACT_EMAIL}" style="color:#FF9900">{ADMIN_CONTACT_EMAIL}</a>.</p>' if ADMIN_CONTACT_EMAIL else ''}
  <p style="color:#888;font-size:12px">If you did not request this account, please disregard this message.</p>
</div>""",
        body_text=(
            f"Hello {user.name} ({user.callsign}),\n\n"
            f"Your NetControl Online account has been approved. You can now log in.\n\n"
            + (f"Log in here: {login_link}\n\n" if login_link else "")
            + (f"This email box is not monitored. If you have any questions please email {ADMIN_CONTACT_EMAIL}.\n\n" if ADMIN_CONTACT_EMAIL else "")
            + "If you did not request this account, please disregard this message."
        ),
    )

    return user


class RejectUserBody(BaseModel):
    message: Optional[str] = None   # optional custom note to include in the rejection email


GITHUB_URL = os.getenv("GITHUB_URL", "https://github.com/LadyHwesta/netcontrol-online")


@app.post("/admin/users/{user_id}/reject", status_code=204)
def admin_reject_user(user_id: int, body: RejectUserBody = RejectUserBody(), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Send a rejection email then permanently delete the pending account."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "Cannot reject your own account")

    custom_block_html = ""
    custom_block_text = ""
    if body.message and body.message.strip():
        msg = body.message.strip()
        custom_block_html = f'<p style="margin:12px 0"><strong>Message from the administrator:</strong><br>{msg}</p>'
        custom_block_text = f"\nMessage from the administrator:\n{msg}\n"

    github_block_html = (
        f'<p style="margin:12px 0;font-size:12px;color:#888">'
        f'NetControl Online is open source. If you\'d like to run your own instance, '
        f'the code is available at <a href="{GITHUB_URL}" style="color:#FF9900">{GITHUB_URL}</a>.</p>'
    )
    github_block_text = (
        f"\nNetControl Online is open source. If you'd like to run your own instance, "
        f"the code is available at {GITHUB_URL}.\n"
    )

    send_email(
        to=[user.email],
        subject="[NetControl Online] Registration Not Approved",
        body_html=f"""<div style="font-family:sans-serif;max-width:520px">
  <h2 style="color:#FF9900">Registration Not Approved</h2>
  <p>Hello <strong>{user.name}</strong> ({user.callsign}),</p>
  <p>Thank you for registering. Unfortunately your account request has not been approved at this time.</p>
  {custom_block_html}
  {f'<p style="color:#888;font-size:12px">If you have questions, please contact <a href="mailto:{ADMIN_CONTACT_EMAIL}" style="color:#FF9900">{ADMIN_CONTACT_EMAIL}</a>.</p>' if ADMIN_CONTACT_EMAIL else ''}
  {github_block_html}
</div>""",
        body_text=(
            f"Hello {user.name} ({user.callsign}),\n\n"
            f"Thank you for registering. Unfortunately your account request has not been approved at this time.\n"
            f"{custom_block_text}"
            + (f"\nIf you have questions, please contact {ADMIN_CONTACT_EMAIL}.\n" if ADMIN_CONTACT_EMAIL else "")
            + github_block_text
        ),
    )

    org_ids = {r[0] for r in db.query(OrganizationMembership.org_id).filter(OrganizationMembership.user_id == user.id).all()}
    db.delete(user)
    db.flush()
    _delete_orphaned_orgs(org_ids, db)
    db.commit()


@app.patch("/admin/users/{user_id}/deactivate", response_model=UserOut)
def admin_deactivate_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Deactivate a user account (they can no longer log in)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "Cannot deactivate your own account")
    user.is_active = False
    db.commit()
    db.refresh(user)
    return user


@app.patch("/admin/users/{user_id}/make-admin", response_model=UserOut)
def admin_make_admin(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Grant admin privileges to a user."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    user.is_admin = True
    user.is_active = True   # admins must be active
    db.commit()
    db.refresh(user)
    return user


@app.delete("/admin/users/{user_id}", status_code=204)
def admin_delete_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Permanently delete a user account."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "Cannot delete your own account")
    org_ids = {r[0] for r in db.query(OrganizationMembership.org_id).filter(OrganizationMembership.user_id == user.id).all()}
    db.delete(user)
    db.flush()
    _delete_orphaned_orgs(org_ids, db)
    db.commit()


@app.patch("/admin/users/{user_id}/notify", response_model=UserOut)
def admin_toggle_notify(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Toggle email notification opt-in for new registrations (admin accounts only)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if not user.is_admin:
        raise HTTPException(400, "Only admins can receive registration notifications")
    user.notify_new_registrations = not user.notify_new_registrations
    db.commit()
    db.refresh(user)
    return user


class OrgReassignUser(BaseModel):
    org_id: int
    role: Literal["member", "admin"] = "member"


@app.patch("/admin/users/{user_id}/org", response_model=AdminUserOut)
def admin_reassign_user_org(user_id: int, data: OrgReassignUser, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Move a user wholesale into a different organization — removes every
    other org membership they hold and switches current_org_id to the
    target, so a deployment that started single-tenant can be split into
    per-region orgs after the fact (issue #1 follow-up). Super-admin only,
    since it crosses tenant boundaries by definition; existing nets they own
    are NOT moved along with them — reassign those separately below."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    org = db.query(Organization).filter(Organization.id == data.org_id).first()
    if not org:
        raise HTTPException(404, "Organization not found")

    old_org_ids = {r[0] for r in db.query(OrganizationMembership.org_id).filter(OrganizationMembership.user_id == user.id).all()}
    db.query(OrganizationMembership).filter(OrganizationMembership.user_id == user.id).delete()
    db.add(OrganizationMembership(org_id=org.id, user_id=user.id, role=data.role, approved=True))
    user.current_org_id = org.id
    user.is_active = True
    db.flush()
    _delete_orphaned_orgs(old_org_ids - {org.id}, db)
    db.commit()
    db.refresh(user)

    return AdminUserOut(
        **UserOut.model_validate(user).model_dump(),
        org_name=org.name,
        org_website_url=org.website_url,
    )


class OrgAddMembership(BaseModel):
    org_id: int
    role: Literal["member", "admin"] = "member"


class AddMembershipResult(BaseModel):
    user_id: int
    org_id: int
    org_name: str
    role: str


@app.post("/admin/users/{user_id}/orgs", response_model=AddMembershipResult, status_code=201)
def admin_add_user_to_org(user_id: int, data: OrgAddMembership, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Add a user to an ADDITIONAL organization without touching their
    existing memberships — distinct from the wholesale move above (issue #1
    follow-up). For an operator who legitimately needs to work across more
    than one org (e.g. a regional coordinator), not for splitting a
    single-tenant deployment apart. If the user already has a pending
    membership in the target org (e.g. a self-service /orgs/join request),
    this approves it in place rather than erroring."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    org = db.query(Organization).filter(Organization.id == data.org_id).first()
    if not org:
        raise HTTPException(404, "Organization not found")

    membership = db.query(OrganizationMembership).filter(
        OrganizationMembership.org_id == org.id, OrganizationMembership.user_id == user.id,
    ).first()
    if membership and membership.approved:
        raise HTTPException(400, "User is already a member of this organization")
    if membership:
        membership.role = data.role
        membership.approved = True
    else:
        db.add(OrganizationMembership(org_id=org.id, user_id=user.id, role=data.role, approved=True))
    user.is_active = True
    db.commit()

    return AddMembershipResult(user_id=user.id, org_id=org.id, org_name=org.name, role=data.role)


class OrgReassignNet(BaseModel):
    org_id: int


class NetReassignResult(BaseModel):
    id: int
    org_id: int
    org_name: str
    owner_not_member: bool


@app.patch("/admin/nets/{net_id}/org", response_model=NetReassignResult)
def admin_reassign_net_org(net_id: int, data: OrgReassignNet, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Move a net into a different organization (issue #1 follow-up). Does
    not touch ownership or sharing — if the net's owner isn't a member of
    the target org, owner_not_member comes back True so the admin panel can
    flag it (the owner will need to be added to the target org, or ownership
    transferred, before they can manage it themselves again); super admins
    can always reach it regardless."""
    net = db.query(Net).filter(Net.id == net_id).first()
    if not net:
        raise HTTPException(404, "Net not found")
    org = db.query(Organization).filter(Organization.id == data.org_id).first()
    if not org:
        raise HTTPException(404, "Organization not found")

    net.org_id = org.id
    db.commit()

    owner_is_member = db.query(OrganizationMembership).filter(
        OrganizationMembership.org_id == org.id,
        OrganizationMembership.user_id == net.owner_id,
        OrganizationMembership.approved == True,
    ).first() is not None

    return NetReassignResult(id=net.id, org_id=org.id, org_name=org.name, owner_not_member=not owner_is_member)


@app.get("/admin/email-status")
def admin_email_status(admin: User = Depends(require_admin)):
    """Return whether SMTP is configured (no credentials exposed)."""
    return {
        "configured": _smtp_configured(),
        "from_address": SMTP_FROM or SMTP_USER or None,
        "host": SMTP_HOST or None,
    }


# ---------------------------------------------------------------------------
# Net Repository — self-service API key requests
# ---------------------------------------------------------------------------

class NetRepoKeyRequestIn(BaseModel):
    name: str = Field(..., max_length=100)
    contact_callsign: Optional[str] = Field(default=None, max_length=12)
    instance_url: Optional[str] = Field(default=None, max_length=500)
    request_notes: Optional[str] = Field(default=None, max_length=1000)


class NetRepoStatusOut(BaseModel):
    url_configured: bool
    has_key: bool
    key_source: Optional[str] = None       # "env" | "self-service" | None
    request_status: str                     # "none" | "pending" | "claimed" | "rejected"


class NetRepoActionResult(BaseModel):
    ok: bool
    status: Optional[str] = None
    message: str
    request_id: Optional[int] = None
    api_key: Optional[str] = None   # present exactly once, on the poll that claims a newly-approved key


@app.get("/admin/net-repository/status", response_model=NetRepoStatusOut)
def admin_net_repository_status(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Current Net Repository integration status. Never exposes the raw API
    key or claim token — those are internal to net_repository.py."""
    return NetRepoStatusOut(
        url_configured=bool(net_repository.NET_REPOSITORY_URL),
        has_key=bool(net_repository.get_api_key(db)),
        key_source=net_repository.get_key_source(db),
        request_status=net_repository.get_request_status(db),
    )


@app.post("/admin/net-repository/request-key", response_model=NetRepoActionResult)
def admin_request_net_repository_key(
    data: NetRepoKeyRequestIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Request a Net Repository API key on this instance's behalf via its
    self-service POST /keys/request. Enters that instance's admin review
    queue; check status with admin_check_net_repository_key below."""
    result = net_repository.request_api_key(
        data.name, data.contact_callsign, data.instance_url, data.request_notes, db,
    )
    return NetRepoActionResult(**result)


@app.post("/admin/net-repository/check-status", response_model=NetRepoActionResult)
def admin_check_net_repository_key(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Poll Net Repository for the outcome of a pending key request. Once
    approved, this stores the issued key so pushes start working immediately
    — no restart needed."""
    result = net_repository.check_key_request_status(db)
    return NetRepoActionResult(**result)


@app.delete("/admin/net-repository/key", status_code=204)
def admin_clear_net_repository_key(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Forget the self-service key and any in-flight request, to start over.
    Does not affect NET_REPOSITORY_API_KEY if set via .env."""
    net_repository.clear_stored_key(db)


# ---------------------------------------------------------------------------
# Checkin routes
# ---------------------------------------------------------------------------

@app.get("/sessions/{session_id}/checkins", response_model=list[CheckinOut])
def list_checkins(session_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_session_for_user(session_id, current_user, db)
    # Newest first — this is the live roster's data source; CSV export and
    # ICS-205 have their own chronological (oldest-first) queries, unaffected.
    checkins = db.query(Checkin).filter(Checkin.session_id == session_id).order_by(Checkin.checked_in_at.desc()).all()
    preferred_names = _preferred_names_for_net(session.net_id, db)
    tactical_callsigns = _tactical_callsigns_for_session(session_id, db)
    out = [CheckinOut.model_validate(c) for c in checkins]
    for c, o in zip(checkins, out):
        if c.callsign in preferred_names:
            o.name = preferred_names[c.callsign]
        if c.tactical_position_id:
            o.tactical_callsign = tactical_callsigns.get(c.tactical_position_id)
    return out


def _create_checkin(session: NetSession, net: Optional[Net], data: CheckinCreate, db: Session) -> Checkin:
    """Shared per-checkin logic behind both add_checkin (one at a time) and
    import_checkins_csv (bulk, issue #26) — same validation either way so
    the two paths can't drift apart. Raises HTTPException on any rejection;
    caller decides whether that aborts the whole request (add_checkin) or is
    just recorded and skipped (the CSV importer)."""
    # An offline-entered session (issue #20) is created already "ended" -- at
    # the reported net date/time, not now -- specifically so it can still take
    # checkins after the fact. Its own is_offline_locked flag (set via the same
    # /sessions/{id}/end endpoint) is what closes it to further checkins instead.
    if session.is_offline:
        if session.is_offline_locked:
            raise HTTPException(400, "This logged net has been closed — no more check-ins can be added")
    elif session.ended_at is not None:
        raise HTTPException(400, "Cannot add checkins to an ended session")

    # Prevent duplicate callsign in the same session — except for GMRS nets where a
    # single family licence is shared among multiple stations.
    is_gmrs = net and net.net_type == "gmrs"
    if not is_gmrs:
        existing = db.query(Checkin).filter(
            Checkin.session_id == session.id,
            Checkin.callsign == data.callsign,
        ).first()
        if existing:
            raise HTTPException(409, f"{data.callsign} has already checked in to this session")

    checkin = Checkin(
        session_id=session.id,
        callsign=data.callsign,
        name=data.name,
        signal_report=data.signal_report,
        comments=data.comments,
        has_traffic=data.has_traffic,
        evac_zone=data.evac_zone or None,
        dmr_talkgroup=data.dmr_talkgroup or None,
        dmr_region=data.dmr_region or None,
    )
    if session.is_offline:
        # Stamp with the reported net date/time, not real "now" (issue #20).
        checkin.checked_in_at = session.started_at
    db.add(checkin)
    db.commit()
    db.refresh(checkin)

    # Auto-upsert evac zone when provided (ARES/ACES nets)
    if data.evac_zone:
        existing_ez = db.query(EvacZone).filter(
            EvacZone.net_id == session.net_id,
            EvacZone.callsign == data.callsign,
        ).first()
        if existing_ez:
            existing_ez.zone = data.evac_zone
            existing_ez.updated_at = datetime.now(timezone.utc)
        else:
            db.add(EvacZone(net_id=session.net_id, callsign=data.callsign, zone=data.evac_zone))
        db.commit()

    return checkin


@app.post("/sessions/{session_id}/checkins", response_model=CheckinOut, status_code=201)
def add_checkin(session_id: int, data: CheckinCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_session_for_user(session_id, current_user, db)
    net = db.query(Net).filter(Net.id == session.net_id).first()
    return _create_checkin(session, net, data, db)


class CheckinImportError(BaseModel):
    row: int              # 1-indexed as a spreadsheet would show it (header is row 1)
    callsign: Optional[str] = None
    reason: str


class CheckinImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[CheckinImportError]


# Header matching is deliberately loose -- letters/digits only, case-folded --
# so "Signal Report", "signal_report", and "SignalReport" all land the same
# way. Only "callsign" is required; everything else is optional, matching
# CheckinCreate.
_CHECKIN_IMPORT_COLUMNS = {
    "callsign": "callsign",
    "name": "name",
    "signalreport": "signal_report",
    "sigreport": "signal_report",
    "comments": "comments",
    "comment": "comments",
    "notes": "comments",
    "hastraffic": "has_traffic",
    "traffic": "has_traffic",
    "evaczone": "evac_zone",
    "zone": "evac_zone",
    "dmrtalkgroup": "dmr_talkgroup",
    "talkgroup": "dmr_talkgroup",
    "dmrregion": "dmr_region",
    "region": "dmr_region",
}


def _normalize_csv_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


@app.post("/sessions/{session_id}/checkins/import", response_model=CheckinImportResult)
async def import_checkins_csv(
    session_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Bulk-add checkins from an uploaded CSV (issue #26) -- built mainly for
    "Log a Net That Already Happened" (issue #20), where re-typing a whole
    paper roster one row at a time is tedious, but works for any session
    that can still take checkins (a live one, or an unlocked offline one) --
    same rules as add_checkin above, via the same _create_checkin helper.
    Each row is validated and inserted independently; one bad row is
    recorded in the response and skipped rather than aborting the rest. See
    GET /checkins/import-sample for the expected column shape."""
    session = _get_session_for_user(session_id, current_user, db)
    net = db.query(Net).filter(Net.id == session.net_id).first()

    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    try:
        header_row = next(reader)
    except StopIteration:
        raise HTTPException(400, "CSV file is empty")

    columns = [_CHECKIN_IMPORT_COLUMNS.get(_normalize_csv_header(h)) for h in header_row]
    if "callsign" not in columns:
        raise HTTPException(400, 'CSV must have a "Callsign" column — download the sample for the expected format')

    imported = 0
    errors: list[CheckinImportError] = []
    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        if not any(cell.strip() for cell in raw_row):
            continue  # skip blank rows
        row = {col: val for col, val in zip(columns, raw_row) if col}
        callsign = (row.get("callsign") or "").strip().upper()
        if not callsign:
            errors.append(CheckinImportError(row=row_num, reason="Missing callsign"))
            continue
        try:
            data = CheckinCreate(
                callsign=callsign,
                name=(row.get("name") or "").strip() or None,
                signal_report=(row.get("signal_report") or "").strip() or None,
                comments=(row.get("comments") or "").strip() or None,
                has_traffic=(row.get("has_traffic") or "").strip().lower() in ("1", "true", "yes", "y"),
                evac_zone=(row.get("evac_zone") or "").strip() or None,
                dmr_talkgroup=(row.get("dmr_talkgroup") or "").strip() or None,
                dmr_region=(row.get("dmr_region") or "").strip() or None,
            )
            _create_checkin(session, net, data, db)
            imported += 1
        except HTTPException as e:
            errors.append(CheckinImportError(row=row_num, callsign=callsign, reason=str(e.detail)))
        except Exception as e:
            errors.append(CheckinImportError(row=row_num, callsign=callsign, reason=str(e)))

    return CheckinImportResult(imported=imported, skipped=len(errors), errors=errors)


@app.get("/checkins/import-sample")
def download_checkin_import_sample(current_user: User = Depends(get_current_user)):
    """Downloadable template showing exactly the columns import_checkins_csv
    above expects, with a couple of filled-in example rows."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Callsign", "Name", "Signal Report", "Comments", "Has Traffic", "Evac Zone", "DMR Talkgroup", "DMR Region"])
    writer.writerow(["W1AW", "Hiram Percy Maxim", "59", "Mobile, first check-in", "no", "", "", ""])
    writer.writerow(["KJ7ABC", "Jane Doe", "55", "", "yes", "Zone 3", "3120", "Western WA"])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="checkin_import_sample.csv"'},
    )


@app.delete("/checkins/{checkin_id}", status_code=204)
def delete_checkin(checkin_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    checkin = db.query(Checkin).filter(Checkin.id == checkin_id).first()
    if not checkin:
        raise HTTPException(404, "Checkin not found")
    # Verify ownership via session → net
    _get_session_for_user(checkin.session_id, current_user, db)
    db.delete(checkin)
    db.commit()


# ---------------------------------------------------------------------------
# Evacuation Zone routes (ARES/ACES)
# ---------------------------------------------------------------------------

@app.get("/nets/{net_id}/evac-zones", response_model=list[EvacZoneOut])
def list_evac_zones(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return all known evacuation zones for this net, sorted by zone then callsign."""
    _get_editable_net(net_id, current_user, db)
    return (
        db.query(EvacZone)
        .filter(EvacZone.net_id == net_id)
        .order_by(EvacZone.zone, EvacZone.callsign)
        .all()
    )


@app.patch("/nets/{net_id}/evac-zones/{callsign}", response_model=EvacZoneOut)
def update_evac_zone(
    net_id: int,
    callsign: str,
    data: EvacZoneUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Manually set or update the evac zone for a callsign on this net."""
    _get_editable_net(net_id, current_user, db)
    callsign = callsign.upper().strip()
    existing = db.query(EvacZone).filter(EvacZone.net_id == net_id, EvacZone.callsign == callsign).first()
    if existing:
        existing.zone = data.zone
        existing.updated_at = datetime.now(timezone.utc)
    else:
        existing = EvacZone(net_id=net_id, callsign=callsign, zone=data.zone)
        db.add(existing)
    db.commit()
    db.refresh(existing)
    return existing


@app.delete("/nets/{net_id}/evac-zones/{callsign}", status_code=204)
def delete_evac_zone(
    net_id: int,
    callsign: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a callsign's evac zone record."""
    _get_editable_net(net_id, current_user, db)
    ez = db.query(EvacZone).filter(EvacZone.net_id == net_id, EvacZone.callsign == callsign.upper()).first()
    if ez:
        db.delete(ez)
        db.commit()


@app.patch("/checkins/{checkin_id}/traffic", response_model=CheckinOut)
def toggle_traffic(checkin_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Toggle has_traffic flag on an existing checkin."""
    checkin = db.query(Checkin).filter(Checkin.id == checkin_id).first()
    if not checkin:
        raise HTTPException(404, "Checkin not found")
    _get_session_for_user(checkin.session_id, current_user, db)
    checkin.has_traffic = not checkin.has_traffic
    db.commit()
    db.refresh(checkin)
    return checkin


@app.patch("/checkins/{checkin_id}/traffic-called", response_model=CheckinOut)
def toggle_traffic_called(checkin_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Toggle traffic_called flag on an existing checkin -- tracks whether the
    operator has already passed this station's traffic, and persists across
    session close/reopen (unlike the old client-side-only tracking)."""
    checkin = db.query(Checkin).filter(Checkin.id == checkin_id).first()
    if not checkin:
        raise HTTPException(404, "Checkin not found")
    _get_session_for_user(checkin.session_id, current_user, db)
    checkin.traffic_called = not checkin.traffic_called
    db.commit()
    db.refresh(checkin)
    return checkin


# ---------------------------------------------------------------------------
# Tactical Positions — ARES/ACES activation mode (issue #21)
#
# Session-scoped, not a reusable net-level template: different activations
# commonly need an entirely different tactical roster. Only usable on a
# session explicitly started as an activation (NetSession.is_activation) —
# a routine session on an ARES net is rejected the same as a non-ARES net,
# so "is_ares" alone never turns this on.
#
# Signing on creates a brand-new Checkin row every time rather than reusing
# add_checkin() — that endpoint blocks a second checkin for the same
# callsign on ham nets, which would wrongly stop an operator holding two
# positions, or re-signing onto one later in the same activation. Each
# sign-on IS a shift-history entry; nothing extra to store for that.
# ---------------------------------------------------------------------------

def _get_activation_session(session_id: int, user: User, db: Session) -> NetSession:
    """Fetch a session, requiring net access and that it's an activation."""
    session = _get_session_for_user(session_id, user, db)
    net = db.query(Net).filter(Net.id == session.net_id).first()
    if not net or not net.is_ares:
        raise HTTPException(400, "Tactical positions require an ARES/ACES net")
    if not session.is_activation:
        raise HTTPException(400, "This session is not marked as an activation")
    return session


def _get_position_for_user(position_id: int, user: User, db: Session) -> TacticalPosition:
    position = db.query(TacticalPosition).filter(TacticalPosition.id == position_id).first()
    if not position:
        raise HTTPException(404, "Tactical position not found")
    _get_session_for_user(position.session_id, user, db)  # raises 403/404 if no access
    return position


def _current_occupant(position_id: int, db: Session) -> Optional[Checkin]:
    return (
        db.query(Checkin)
        .filter(Checkin.tactical_position_id == position_id, Checkin.signed_off_at.is_(None))
        .order_by(Checkin.checked_in_at.desc())
        .first()
    )


def _position_to_out(position: TacticalPosition, db: Session) -> TacticalPositionOut:
    out = TacticalPositionOut.model_validate(position)
    current = _current_occupant(position.id, db)
    if current:
        out.current_checkin_id = current.id
        out.current_callsign = current.callsign
        out.current_name = current.name
        out.signed_on_at = current.checked_in_at
    return out


@app.get("/sessions/{session_id}/tactical-positions", response_model=list[TacticalPositionOut])
def list_tactical_positions(session_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_activation_session(session_id, current_user, db)
    positions = (
        db.query(TacticalPosition)
        .filter(TacticalPosition.session_id == session.id)
        .order_by(TacticalPosition.is_net_control.desc(), TacticalPosition.created_at)
        .all()
    )
    return [_position_to_out(p, db) for p in positions]


@app.post("/sessions/{session_id}/tactical-positions", response_model=TacticalPositionOut, status_code=201)
def create_tactical_position(session_id: int, data: TacticalPositionCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_activation_session(session_id, current_user, db)
    position = TacticalPosition(
        session_id=session.id,
        tactical_callsign=data.tactical_callsign,
        location=(data.location or "").strip() or None,
        assigned_callsign=(data.assigned_callsign or "").strip().upper() or None,
        assigned_name=(data.assigned_name or "").strip() or None,
        scheduled_start=data.scheduled_start,
    )
    db.add(position)
    db.commit()
    db.refresh(position)
    return _position_to_out(position, db)


@app.patch("/tactical-positions/{position_id}", response_model=TacticalPositionOut)
def update_tactical_position(position_id: int, data: TacticalPositionUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Edit a position's plan (location, planned operator, scheduled sign-on). This is
    the only way to plan ahead for Net Control specifically -- it's auto-created at
    session start with no creation form of its own, so without this there'd be no way
    to set who's expected next or when (issue #21 follow-up)."""
    position = _get_position_for_user(position_id, current_user, db)
    position.location = (data.location or "").strip() or None
    position.assigned_callsign = (data.assigned_callsign or "").strip().upper() or None
    position.assigned_name = (data.assigned_name or "").strip() or None
    position.scheduled_start = data.scheduled_start
    db.commit()
    db.refresh(position)
    return _position_to_out(position, db)


@app.delete("/tactical-positions/{position_id}", status_code=204)
def delete_tactical_position(position_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    position = _get_position_for_user(position_id, current_user, db)
    if position.is_net_control:
        raise HTTPException(400, "Cannot remove the Net Control position — hand it off instead")
    db.delete(position)  # checkins keep their history; tactical_position_id -> NULL via ON DELETE SET NULL
    db.commit()


@app.get("/tactical-positions/{position_id}/shifts", response_model=list[CheckinOut])
def list_tactical_shifts(position_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    position = _get_position_for_user(position_id, current_user, db)
    shifts = (
        db.query(Checkin)
        .filter(Checkin.tactical_position_id == position.id)
        .order_by(Checkin.checked_in_at)
        .all()
    )
    out = [CheckinOut.model_validate(c) for c in shifts]
    for o in out:
        o.tactical_callsign = position.tactical_callsign
    return out


@app.post("/tactical-positions/{position_id}/sign-on", response_model=CheckinOut, status_code=201)
def sign_on_tactical_position(position_id: int, data: TacticalSignOn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    position = _get_position_for_user(position_id, current_user, db)
    session = db.query(NetSession).filter(NetSession.id == position.session_id).first()
    if session.ended_at is not None:
        raise HTTPException(400, "Cannot sign on to a position on an ended session")

    outgoing = _current_occupant(position.id, db)
    if outgoing:
        outgoing.signed_off_at = utcnow()

    checkin = Checkin(
        session_id=session.id,
        callsign=data.callsign,
        name=(data.name or "").strip() or None,
        has_traffic=False,
        tactical_position_id=position.id,
    )
    db.add(checkin)
    db.commit()
    db.refresh(checkin)
    out = CheckinOut.model_validate(checkin)
    out.tactical_callsign = position.tactical_callsign
    return out


@app.post("/tactical-positions/{position_id}/sign-off", response_model=CheckinOut)
def sign_off_tactical_position(position_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    position = _get_position_for_user(position_id, current_user, db)
    outgoing = _current_occupant(position.id, db)
    if not outgoing:
        raise HTTPException(404, "This position is not currently occupied")
    outgoing.signed_off_at = utcnow()
    db.commit()
    db.refresh(outgoing)
    out = CheckinOut.model_validate(outgoing)
    out.tactical_callsign = position.tactical_callsign
    return out


# ---------------------------------------------------------------------------
# Net Control rotation schedule (issue #21 follow-up)
#
# A forward-looking queue of planned future Net Control shifts, kept separate
# from tactical positions' single assigned_callsign/scheduled_start -- Net
# Control classically rotates on a fixed cadence throughout a long activation,
# so operators plan a whole rotation ahead of time rather than just "who's
# next". Handing off Net Control (sign-on to the is_net_control position)
# pre-fills from whichever shift here is scheduled earliest; the frontend
# removes that entry once the handoff is confirmed.
# ---------------------------------------------------------------------------

def _get_shift_for_user(shift_id: int, user: User, db: Session) -> NetControlShift:
    shift = db.query(NetControlShift).filter(NetControlShift.id == shift_id).first()
    if not shift:
        raise HTTPException(404, "Shift not found")
    _get_session_for_user(shift.session_id, user, db)  # raises 403/404 if no access
    return shift


@app.get("/sessions/{session_id}/net-control-shifts", response_model=list[NetControlShiftOut])
def list_net_control_shifts(session_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_activation_session(session_id, current_user, db)
    shifts = (
        db.query(NetControlShift)
        .filter(NetControlShift.session_id == session.id)
        .order_by(NetControlShift.scheduled_start)
        .all()
    )
    return [NetControlShiftOut.model_validate(s) for s in shifts]


@app.post("/sessions/{session_id}/net-control-shifts", response_model=NetControlShiftOut, status_code=201)
def create_net_control_shift(session_id: int, data: NetControlShiftCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_activation_session(session_id, current_user, db)
    shift = NetControlShift(
        session_id=session.id,
        callsign=data.callsign,
        name=(data.name or "").strip() or None,
        scheduled_start=data.scheduled_start,
    )
    db.add(shift)
    db.commit()
    db.refresh(shift)
    return NetControlShiftOut.model_validate(shift)


@app.delete("/net-control-shifts/{shift_id}", status_code=204)
def delete_net_control_shift(shift_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    shift = _get_shift_for_user(shift_id, current_user, db)
    db.delete(shift)
    db.commit()


# ---------------------------------------------------------------------------
# Expected Stations
# ---------------------------------------------------------------------------

def _preferred_names_for_net(net_id: int, db: Session) -> dict:
    """callsign -> preferred_name for every station remark on this net that has one set."""
    rows = (
        db.query(StationRemark.callsign, StationRemark.preferred_name)
        .filter(StationRemark.net_id == net_id, StationRemark.preferred_name.isnot(None))
        .all()
    )
    return {r.callsign: r.preferred_name for r in rows}


def _tactical_callsigns_for_session(session_id: int, db: Session) -> dict:
    """tactical_position_id -> tactical_callsign for this session's positions
    (issue #21) — avoids an N+1 lookup per checkin row in list_checkins()."""
    rows = (
        db.query(TacticalPosition.id, TacticalPosition.tactical_callsign)
        .filter(TacticalPosition.session_id == session_id)
        .all()
    )
    return {r.id: r.tactical_callsign for r in rows}


def _tactical_callsigns_for_net(net_id: int, db: Session) -> dict:
    """Same as _tactical_callsigns_for_session, but across every session on
    this net — for the multi-session net-wide CSV export."""
    rows = (
        db.query(TacticalPosition.id, TacticalPosition.tactical_callsign)
        .join(NetSession, NetSession.id == TacticalPosition.session_id)
        .filter(NetSession.net_id == net_id)
        .all()
    )
    return {r.id: r.tactical_callsign for r in rows}


@app.get("/nets/{net_id}/expected", response_model=list[ExpectedStation])
def expected_stations(
    net_id: int,
    weeks: int = Query(4, ge=1, le=52),
    min_checkins: int = Query(2, ge=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return callsigns that checked in >= min_checkins times in the past N weeks for this net."""
    _get_editable_net(net_id, current_user, db)

    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)

    rows = (
        db.query(
            Checkin.callsign,
            func.max(Checkin.name).label("name"),
            func.count(Checkin.id).label("cnt"),
            func.max(Checkin.checked_in_at).label("last_checkin"),
        )
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id, Checkin.checked_in_at >= cutoff)
        .group_by(Checkin.callsign)
        .having(func.count(Checkin.id) >= min_checkins)
        .order_by(func.count(Checkin.id).desc())
        .all()
    )

    import re as _re
    def _suffix(cs: str) -> str:
        """Return just the letter suffix after the district digit for sorting."""
        m = _re.search(r'\d([A-Z]+)$', cs.upper())
        return m.group(1) if m else cs

    preferred_names = _preferred_names_for_net(net_id, db)
    stations = [
        ExpectedStation(
            callsign=r.callsign,
            name=preferred_names.get(r.callsign, r.name),
            checkin_count=r.cnt,
            last_checkin=r.last_checkin,
        )
        for r in rows
    ]
    stations.sort(key=lambda s: _suffix(s.callsign))
    return stations


# ---------------------------------------------------------------------------
# Session summary & ICS-205
# ---------------------------------------------------------------------------

@app.get("/sessions/{session_id}/summary", response_model=SessionSummary)
def session_summary(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _get_session_for_user(session_id, current_user, db)
    net = db.query(Net).filter(Net.id == session.net_id).first()
    checkins = db.query(Checkin).filter(Checkin.session_id == session_id).all()

    duration_minutes = None
    if session.started_at and session.ended_at:
        delta = session.ended_at - session.started_at
        duration_minutes = int(delta.total_seconds() / 60)

    # New stations: callsigns that appear in this session but not in any prior session for this net
    this_callsigns = {c.callsign for c in checkins}
    prior = (
        db.query(Checkin.callsign)
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == session.net_id, NetSession.id != session_id)
        .distinct()
        .all()
    )
    prior_callsigns = {r.callsign for r in prior}
    new_stations = len(this_callsigns - prior_callsigns)

    operator_callsign = None
    if session.operator_id:
        op = db.query(User).filter(User.id == session.operator_id).first()
        operator_callsign = op.callsign if op else None

    return SessionSummary(
        session_id=session_id,
        net_name=net.name if net else "Unknown",
        started_at=session.started_at,
        ended_at=session.ended_at,
        duration_minutes=duration_minutes,
        total_checkins=len(checkins),
        traffic_count=sum(1 for c in checkins if c.has_traffic),
        new_stations=new_stations,
        operator_callsign=operator_callsign,
        net_frequency=net.frequency if net else None,
    )


@app.get("/sessions/{session_id}/ics205")
def session_ics205(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return a printable HTML ICS-205 / net log for this session."""
    session = _get_session_for_user(session_id, current_user, db)
    net = db.query(Net).filter(Net.id == session.net_id).first()
    checkins = db.query(Checkin).filter(Checkin.session_id == session_id).order_by(Checkin.checked_in_at).all()
    traffic_msgs = db.query(TrafficMessage).filter(TrafficMessage.session_id == session_id).order_by(TrafficMessage.created_at).all()

    op_callsign = ""
    if session.operator_id:
        op = db.query(User).filter(User.id == session.operator_id).first()
        op_callsign = op.callsign if op else ""

    started = session.started_at.strftime("%Y-%m-%d %H%MZ") if session.started_at else ""
    ended   = session.ended_at.strftime("%H%MZ") if session.ended_at else "—"
    freq    = net.frequency if net and net.frequency else "—"

    preferred_names = _preferred_names_for_net(session.net_id, db)
    tactical_callsigns = _tactical_callsigns_for_session(session_id, db) if session.is_activation else {}

    checkin_rows = ""
    for i, c in enumerate(checkins, 1):
        traffic_flag = " 📢" if c.has_traffic else ""
        display_name = preferred_names.get(c.callsign, c.name)
        zone_cell = f"<td>{html.escape(c.evac_zone or '—')}</td>" if net and net.is_ares else ""
        tactical_cell = (
            f"<td>{html.escape(tactical_callsigns.get(c.tactical_position_id, '') or '—')}</td>"
            if session.is_activation else ""
        )
        checkin_rows += (
            f"<tr><td>{i}</td><td>{c.checked_in_at.strftime('%H%MZ')}</td>"
            f"<td><strong>{html.escape(c.callsign)}</strong></td><td>{html.escape(display_name or '')}</td>"
            f"<td>{html.escape(c.signal_report or '')}</td><td>{html.escape(c.comments or '')}{traffic_flag}</td>"
            f"{zone_cell}{tactical_cell}</tr>\n"
        )

    traffic_rows = ""
    for i, m in enumerate(traffic_msgs, 1):
        traffic_rows += (
            f"<tr><td>{i}</td><td>{html.escape(m.msg_number or '—')}</td>"
            f"<td>{html.escape(m.origin_callsign)}</td><td>{html.escape(m.dest_info or '—')}</td>"
            f"<td>{html.escape(m.msg_type.replace('_',' ').title())}</td>"
            f"<td>{html.escape(m.status.title())}</td><td>{html.escape(m.notes or '')}</td></tr>\n"
        )

    zone_th = "<th>Zone</th>" if net and net.is_ares else ""
    tactical_th = "<th>Tactical</th>" if session.is_activation else ""
    traffic_section = ""
    if traffic_msgs:
        traffic_section = f"""
        <h3>Traffic Log ({len(traffic_msgs)} message{'s' if len(traffic_msgs)!=1 else ''})</h3>
        <table><thead><tr><th>#</th><th>Msg #</th><th>Origin</th><th>Destination</th>
        <th>Type</th><th>Status</th><th>Notes</th></tr></thead>
        <tbody>{traffic_rows}</tbody></table>"""

    net_name_esc = html.escape(net.name) if net else 'Net'
    op_callsign_esc = html.escape(op_callsign)
    freq_esc = html.escape(freq)

    page_html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>ICS-205 Net Log — {net_name_esc}</title>
<style>
  body {{ font-family: Arial, sans-serif; font-size: 11pt; margin: 20mm; color: #000; }}
  h1 {{ font-size: 16pt; margin-bottom: 4px; }}
  h2 {{ font-size: 13pt; margin-top: 16px; margin-bottom: 4px; border-bottom: 1px solid #000; }}
  h3 {{ font-size: 12pt; margin-top: 16px; margin-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 10pt; }}
  th {{ background: #ddd; border: 1px solid #999; padding: 4px 6px; text-align: left; }}
  td {{ border: 1px solid #ccc; padding: 3px 6px; }}
  .header-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 12px 0; }}
  .field {{ margin-bottom: 4px; }}
  .label {{ font-weight: bold; font-size: 9pt; color: #555; display: block; }}
  .value {{ font-size: 11pt; border-bottom: 1px solid #aaa; padding-bottom: 2px; min-height: 18px; }}
  @media print {{ body {{ margin: 10mm; }} }}
</style>
</head><body>
<h1>ICS-205 — Amateur Radio Net Log</h1>
<div class="header-grid">
  <div>
    <div class="field"><span class="label">Net Name / Incident</span>
      <span class="value">{net_name_esc if net else ''}</span></div>
    <div class="field"><span class="label">Frequency / Mode</span>
      <span class="value">{freq_esc}</span></div>
    <div class="field"><span class="label">Net Control Station</span>
      <span class="value">{op_callsign_esc}</span></div>
  </div>
  <div>
    <div class="field"><span class="label">Session Start (UTC)</span>
      <span class="value">{started}</span></div>
    <div class="field"><span class="label">Session End (UTC)</span>
      <span class="value">{ended}</span></div>
    <div class="field"><span class="label">Total Check-Ins</span>
      <span class="value">{len(checkins)}</span></div>
  </div>
</div>

<h2>Station Check-In Log</h2>
<table>
  <thead><tr><th>#</th><th>Time (UTC)</th><th>Callsign</th><th>Name</th>
    <th>Signal</th><th>Comments / Traffic</th>{zone_th}{tactical_th}</tr></thead>
  <tbody>{checkin_rows}</tbody>
</table>
{traffic_section}

<p style="margin-top:24px;font-size:9pt;color:#555">
  Prepared by: {op_callsign_esc} &nbsp;|&nbsp; Printed: <span id="print-ts"></span>
</p>
<script>document.getElementById('print-ts').textContent = new Date().toUTCString();</script>
</body></html>"""

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=page_html)


# ---------------------------------------------------------------------------
# Traffic messages
# ---------------------------------------------------------------------------

@app.get("/sessions/{session_id}/traffic-messages", response_model=list[TrafficMessageOut])
def list_traffic_messages(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_session_for_user(session_id, current_user, db)
    return db.query(TrafficMessage).filter(TrafficMessage.session_id == session_id).order_by(TrafficMessage.created_at).all()


@app.post("/sessions/{session_id}/traffic-messages", response_model=TrafficMessageOut, status_code=201)
def create_traffic_message(
    session_id: int,
    body: TrafficMessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_session_for_user(session_id, current_user, db)
    msg = TrafficMessage(
        session_id=session_id,
        origin_callsign=body.origin_callsign.upper().strip(),
        dest_info=body.dest_info,
        msg_number=body.msg_number,
        msg_type=body.msg_type,
        status=body.status,
        notes=body.notes,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


@app.patch("/traffic-messages/{msg_id}", response_model=TrafficMessageOut)
def update_traffic_message(
    msg_id: int,
    body: TrafficMessageUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    msg = db.query(TrafficMessage).filter(TrafficMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(404, "Message not found")
    _get_session_for_user(msg.session_id, current_user, db)
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(msg, field, val)
    db.commit()
    db.refresh(msg)
    return msg


@app.delete("/traffic-messages/{msg_id}", status_code=204)
def delete_traffic_message(
    msg_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    msg = db.query(TrafficMessage).filter(TrafficMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(404, "Message not found")
    _get_session_for_user(msg.session_id, current_user, db)
    db.delete(msg)
    db.commit()


# ---------------------------------------------------------------------------
# Station remarks
# ---------------------------------------------------------------------------

@app.get("/nets/{net_id}/stations/{callsign}/remark", response_model=Optional[StationRemarkOut])
def get_station_remark(
    net_id: int,
    callsign: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_editable_net(net_id, current_user, db)
    remark = db.query(StationRemark).filter(
        StationRemark.net_id == net_id,
        StationRemark.callsign == callsign.upper(),
    ).first()
    return remark  # None returns as null → 200 with null body; frontend handles


@app.put("/nets/{net_id}/stations/{callsign}/remark", response_model=Optional[StationRemarkOut])
def upsert_station_remark(
    net_id: int,
    callsign: str,
    body: StationRemarkUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_editable_net(net_id, current_user, db)
    cs = callsign.upper().strip()
    remark_text = (body.remark or "").strip() or None
    preferred_name = (body.preferred_name or "").strip() or None

    existing = db.query(StationRemark).filter(
        StationRemark.net_id == net_id,
        StationRemark.callsign == cs,
    ).first()

    if not remark_text and not preferred_name:
        # Nothing left to store -- clear the row rather than leaving an empty one.
        if existing:
            db.delete(existing)
            db.commit()
        return None

    if existing:
        existing.remark = remark_text
        existing.preferred_name = preferred_name
        existing.updated_by = current_user.id
        existing.updated_at = datetime.now(timezone.utc)
        remark = existing
    else:
        remark = StationRemark(
            net_id=net_id,
            callsign=cs,
            remark=remark_text,
            preferred_name=preferred_name,
            updated_by=current_user.id,
        )
        db.add(remark)
    db.commit()
    db.refresh(remark)
    return remark


@app.delete("/nets/{net_id}/stations/{callsign}/remark", status_code=204)
def delete_station_remark(
    net_id: int,
    callsign: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_editable_net(net_id, current_user, db)
    remark = db.query(StationRemark).filter(
        StationRemark.net_id == net_id,
        StationRemark.callsign == callsign.upper(),
    ).first()
    if remark:
        db.delete(remark)
        db.commit()


# ---------------------------------------------------------------------------
# History / Stats
# ---------------------------------------------------------------------------

@app.get("/nets/{net_id}/history", response_model=list[CallsignHistoryItem])
def net_history(
    net_id: int,
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return checkin counts per callsign across all sessions of a net.
    Also includes recent_checkins: count of checkins in the past 14 days.
    """
    _get_editable_net(net_id, current_user, db)

    rows = (
        db.query(
            Checkin.callsign,
            func.max(Checkin.name).label("name"),
            func.count(Checkin.id).label("total_checkins"),
            func.max(Checkin.checked_in_at).label("last_checkin"),
        )
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id)
        .group_by(Checkin.callsign)
        .order_by(func.count(Checkin.id).desc())
        .limit(limit)
        .all()
    )

    now = datetime.now(timezone.utc)

    # Recent 14-day counts
    cutoff_2w = now - timedelta(days=14)
    recent_2w = {
        r.callsign: r.cnt
        for r in db.query(Checkin.callsign, func.count(Checkin.id).label("cnt"))
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id, Checkin.checked_in_at >= cutoff_2w)
        .group_by(Checkin.callsign).all()
    }

    # Recent 28-day counts
    cutoff_4w = now - timedelta(days=28)
    recent_4w = {
        r.callsign: r.cnt
        for r in db.query(Checkin.callsign, func.count(Checkin.id).label("cnt"))
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id, Checkin.checked_in_at >= cutoff_4w)
        .group_by(Checkin.callsign).all()
    }

    # Who checked in to the most recent ended session?
    last_session = (
        db.query(NetSession)
        .filter(NetSession.net_id == net_id, NetSession.ended_at.isnot(None))
        .order_by(NetSession.started_at.desc())
        .first()
    )
    last_session_callsigns: set = set()
    if last_session:
        last_session_callsigns = {
            c.callsign for c in
            db.query(Checkin).filter(Checkin.session_id == last_session.id).all()
        }

    preferred_names = _preferred_names_for_net(net_id, db)
    return [
        CallsignHistoryItem(
            callsign=r.callsign,
            name=preferred_names.get(r.callsign, r.name),
            total_checkins=r.total_checkins,
            recent_checkins=recent_2w.get(r.callsign, 0),
            recent_4w_checkins=recent_4w.get(r.callsign, 0),
            checked_in_last_session=(r.callsign in last_session_callsigns),
            last_checkin=r.last_checkin,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------

@app.get("/sessions/{session_id}/export")
def export_session_csv(session_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = _get_session_for_user(session_id, current_user, db)
    checkins = db.query(Checkin).filter(Checkin.session_id == session_id).order_by(Checkin.checked_in_at).all()
    net = db.query(Net).filter(Net.id == session.net_id).first()
    preferred_names = _preferred_names_for_net(session.net_id, db)
    tactical_callsigns = _tactical_callsigns_for_session(session_id, db) if session.is_activation else {}

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["#", "Callsign", "Name", "Signal Report", "Comments", "Checked In At"]
    if session.is_activation:
        header.insert(1, "Tactical Callsign")
        header.append("Signed Off At")
    writer.writerow(header)
    for i, c in enumerate(checkins, start=1):
        display_name = preferred_names.get(c.callsign, c.name)
        row = [i, c.callsign, display_name or "", c.signal_report or "", c.comments or "", c.checked_in_at.isoformat()]
        if session.is_activation:
            row.insert(1, tactical_callsigns.get(c.tactical_position_id, "") or "")
            row.append(c.signed_off_at.isoformat() if c.signed_off_at else "")
        writer.writerow(row)

    filename = f"session_{session_id}_{net.name.replace(' ', '_')}.csv"
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/nets/{net_id}/export")
def export_net_csv(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    net = _get_net_for_user(net_id, current_user, db)
    preferred_names = _preferred_names_for_net(net_id, db)
    tactical_callsigns = _tactical_callsigns_for_net(net_id, db) if net.is_ares else {}

    rows = (
        db.query(Checkin, NetSession)
        .join(NetSession, NetSession.id == Checkin.session_id)
        .filter(NetSession.net_id == net_id)
        .order_by(NetSession.started_at.desc(), Checkin.checked_in_at)
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["Session ID", "Session Started", "Callsign", "Name", "Signal Report", "Comments", "Checked In At"]
    if net.is_ares:
        header.insert(3, "Tactical Callsign")
    writer.writerow(header)
    for checkin, session in rows:
        display_name = preferred_names.get(checkin.callsign, checkin.name)
        row = [
            session.id,
            session.started_at.isoformat(),
            checkin.callsign,
            display_name or "",
            checkin.signal_report or "",
            checkin.comments or "",
            checkin.checked_in_at.isoformat(),
        ]
        if net.is_ares:
            row.insert(3, tactical_callsigns.get(checkin.tactical_position_id, "") or "")
        writer.writerow(row)

    filename = f"net_{net_id}_{net.name.replace(' ', '_')}_all_sessions.csv"
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Callsign Lookup
# ---------------------------------------------------------------------------

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


@app.get("/callsign/search", response_model=list[CallsignSearchResult])
def search_callsigns(
    q: str = Query(..., min_length=2, max_length=12),
    net_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Search checkin history for callsigns whose suffix matches q.
    Searches the current net first (if net_id provided), then all nets owned by the user.
    Results are sorted by callsign suffix.
    MUST be defined before /callsign/{callsign}/lookup so 'search' is not captured as a path param."""
    import re as _re

    q = q.upper().strip()

    def _suffix(cs: str) -> str:
        m = _re.search(r'\d([A-Z]+)$', cs.upper())
        return m.group(1) if m else cs

    def _run_query(extra_filter) -> list[CallsignSearchResult]:
        rows = (
            db.query(
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
            .all()
        )
        results = [
            CallsignSearchResult(callsign=r.callsign, name=r.name, license_class=None)
            for r in rows
        ]
        results.sort(key=lambda r: _suffix(r.callsign))
        return results[:20]

    # 1. Search current net's history first
    if net_id:
        results = _run_query(Net.id == net_id)
        if results:
            return results

    # 2. Fall back to all nets owned by this user
    results = _run_query(True)
    return results


# Cache TTLs for callsign lookups
_CALLSIGN_CACHE_TTL_FOUND = 30 * 24 * 3600      # 30 days — licenses rarely change
_CALLSIGN_CACHE_TTL_NOT_FOUND = 7 * 24 * 3600   # 7 days — callsign might get issued


def _callsign_cache_read(callsign: str, db: Session) -> Optional[CallsignLookupResult]:
    """Return a cached lookup result if still within TTL, else None."""
    row = db.query(CallsignCache).filter(CallsignCache.callsign == callsign).first()
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


def _callsign_cache_write(result: CallsignLookupResult, db: Session) -> None:
    """Upsert a lookup result into the local cache."""
    row = db.query(CallsignCache).filter(CallsignCache.callsign == result.callsign).first()
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
    db.commit()


import re as _re
_GMRS_CS_RE = _re.compile(r'^[A-Z]{3,4}\d{3,4}$')

def _is_gmrs_callsign(cs: str) -> bool:
    """Return True if callsign matches the FCC GMRS format (e.g. WQXH7777)."""
    return bool(_GMRS_CS_RE.match(cs))


@app.get("/callsign/{callsign}/lookup", response_model=CallsignLookupResult)
async def lookup_callsign(
    callsign: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
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
    import logging
    log = logging.getLogger("callsign_lookup")
    callsign = callsign.upper().strip()

    # ── GMRS branch ──────────────────────────────────────────────────────────
    if _is_gmrs_callsign(callsign):
        # 1. Local gmrs_licenses table (fast, no external call)
        row = db.query(GmrsLicense).filter(GmrsLicense.callsign == callsign).first()
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
        cached = _callsign_cache_read(callsign, db)
        if cached:
            return cached

        def _save(result: CallsignLookupResult) -> CallsignLookupResult:
            _callsign_cache_write(result, db)
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
                        return _save(CallsignLookupResult(
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
        return _save(CallsignLookupResult(callsign=callsign, status="not_found"))

    # ── Ham branch ───────────────────────────────────────────────────────────
    # Return cached result if still fresh
    cached = _callsign_cache_read(callsign, db)
    if cached:
        return cached

    def _save(result: CallsignLookupResult) -> CallsignLookupResult:
        """Persist to cache then return."""
        _callsign_cache_write(result, db)
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
                    return _save(CallsignLookupResult(
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
                    return _save(CallsignLookupResult(
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

                    return _save(CallsignLookupResult(
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
    return _save(CallsignLookupResult(callsign=callsign, status="not_found"))


# ---------------------------------------------------------------------------
# Net Schedules
# ---------------------------------------------------------------------------

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ScheduleCreate(BaseModel):
    day_of_week: int        # 0=Monday … 6=Sunday
    start_time: str         # "HH:MM"
    timezone: str = "UTC"
    notes: Optional[str] = None

    @field_validator("day_of_week")
    @classmethod
    def valid_day(cls, v):
        if not 0 <= v <= 6:
            raise ValueError("day_of_week must be 0 (Monday) through 6 (Sunday)")
        return v

    @field_validator("start_time")
    @classmethod
    def valid_time(cls, v):
        import re
        if not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError("start_time must be HH:MM")
        return v


class ScheduleOut(BaseModel):
    id: int
    net_id: int
    day_of_week: int
    day_name: str
    start_time: str
    timezone: str
    notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class SignupCreate(BaseModel):
    schedule_id: int
    slot_date: date
    role: str = "net_control"   # 'net_control' | 'broadcaster' | 'both'
    # Self sign-up: provide callsign directly.
    # Assignment: provide assigned_user_id and callsign/name/email are pulled from that user.
    callsign: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None
    assigned_user_id: Optional[int] = None   # set when net owner assigns another operator

    @field_validator("callsign")
    @classmethod
    def callsign_upper(cls, v):
        if v:
            return v.upper().strip()
        return v

    @field_validator("role")
    @classmethod
    def valid_role(cls, v):
        if v not in ("net_control", "broadcaster", "both"):
            raise ValueError("role must be net_control, broadcaster, or both")
        return v


class SignupOut(BaseModel):
    id: int
    schedule_id: int
    net_id: int
    slot_date: date
    role: str = "net_control"
    callsign: str
    name: Optional[str]
    email: Optional[str]
    notes: Optional[str]
    signed_up_at: datetime
    is_mine: bool = False   # True if current user owns this signup

    model_config = {"from_attributes": True}


class UpcomingSlot(BaseModel):
    slot_date: date
    day_name: str
    schedule_id: int
    signups: list[SignupOut] = []   # empty = fully open


def _next_occurrences(day_of_week: int, weeks: int = 8) -> list[date]:
    """Return the next `weeks` dates (including today if it matches) for a given weekday.
    Uses the UTC calendar date — matching _duty_labels_for_session's use of
    session.started_at.date() and send_reminders.py's now_utc.date() — so that
    signup slot_date, session dates, and reminder-window dates never disagree near
    midnight in timezones behind UTC (server-local date.today() would drift by a day)."""
    today = datetime.now(timezone.utc).date()
    days_ahead = (day_of_week - today.weekday()) % 7
    first = today + timedelta(days=days_ahead)
    return [first + timedelta(weeks=i) for i in range(weeks)]


def _signup_to_out(s: NetControlSignup, current_user: User) -> SignupOut:
    return SignupOut(
        id=s.id, schedule_id=s.schedule_id, net_id=s.net_id,
        slot_date=s.slot_date, role=s.role, callsign=s.callsign, name=s.name,
        email=s.email, notes=s.notes, signed_up_at=s.signed_up_at,
        is_mine=(s.user_id == current_user.id),
    )


def _duty_for_date(net_id: int, slot_date: date, db: Session) -> tuple:
    """Return (net_control_signup, broadcaster_signup) ORM rows for this net on slot_date,
    across all of its schedules. A signup with role='both' fills both."""
    signups = (
        db.query(NetControlSignup)
        .filter(NetControlSignup.net_id == net_id, NetControlSignup.slot_date == slot_date)
        .all()
    )
    nc = next((s for s in signups if s.role in ("net_control", "both")), None)
    bc = next((s for s in signups if s.role in ("broadcaster", "both")), None)
    return nc, bc


def _duty_labels_for_session(net: Net, session: NetSession, db: Session) -> dict:
    """Net Control / Broadcaster display info for a session, sourced from the schedule
    sign-up matching the session's date when one exists, falling back to whoever
    actually started the session for Net Control. Also includes the sign-up (if any)
    for one week later, so a script can announce next week's duty.

    A manual broadcaster override set at session start (issue #17) takes precedence
    over the schedule sign-up — covers the case where the broadcaster isn't known
    until the net is about to begin. A manual Net Control override (issue #20,
    mainly for offline-entered nets where whoever backfills the log may not be
    who actually ran it) takes the same precedence over the schedule sign-up."""
    session_date = session.started_at.date()
    nc, bc = _duty_for_date(net.id, session_date, db)
    next_nc, next_bc = _duty_for_date(net.id, session_date + timedelta(days=7), db)
    operator = db.query(User).filter(User.id == session.operator_id).first() if session.operator_id else None
    # On a GMRS net, prefer the operator's separate GMRS callsign (issue #23) over
    # their amateur one, when they have one set — only relevant for the "whoever
    # started the session" fallback; an explicit schedule sign-up's callsign
    # (typed at sign-up time) always wins regardless.
    operator_callsign = None
    if operator:
        operator_callsign = (
            (operator.gmrs_callsign or operator.callsign) if net.net_type == "gmrs" else operator.callsign
        )
    ncs_callsign = session.ncs_override_callsign or (nc.callsign if nc else operator_callsign)
    ncs_name = session.ncs_override_name or (nc.name if nc else (operator.name if operator else None))
    broadcaster_callsign = session.broadcaster_override_callsign or (bc.callsign if bc else None)
    broadcaster_name = session.broadcaster_override_name or (bc.name if bc else None)
    return {
        "ncs_callsign": ncs_callsign,
        "ncs_name": ncs_name,
        "broadcaster_callsign": broadcaster_callsign,
        "broadcaster_name": broadcaster_name,
        "broadcast_label": net.broadcast_label if (net.has_broadcast and broadcaster_callsign) else None,
        "next_ncs_callsign": next_nc.callsign if next_nc else None,
        "next_ncs_name": next_nc.name if next_nc else None,
        "next_broadcaster_callsign": next_bc.callsign if next_bc else None,
        "next_broadcaster_name": next_bc.name if next_bc else None,
    }


def _schedule_to_out(s: NetSchedule) -> ScheduleOut:
    return ScheduleOut(
        id=s.id,
        net_id=s.net_id,
        day_of_week=s.day_of_week,
        day_name=DAYS[s.day_of_week],
        start_time=s.start_time,
        timezone=s.timezone,
        notes=s.notes,
        created_at=s.created_at,
    )


@app.get("/nets/{net_id}/schedules", response_model=list[ScheduleOut])
def list_schedules(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_editable_net(net_id, current_user, db)
    schedules = db.query(NetSchedule).filter(NetSchedule.net_id == net_id).order_by(NetSchedule.day_of_week).all()
    return [_schedule_to_out(s) for s in schedules]


@app.post("/nets/{net_id}/schedules", response_model=ScheduleOut, status_code=201)
def create_schedule(net_id: int, data: ScheduleCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_editable_net(net_id, current_user, db)
    sched = NetSchedule(
        net_id=net_id,
        day_of_week=data.day_of_week,
        start_time=data.start_time,
        timezone=data.timezone,
        notes=data.notes,
    )
    db.add(sched)
    db.commit()
    db.refresh(sched)
    return _schedule_to_out(sched)


@app.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    sched = db.query(NetSchedule).filter(NetSchedule.id == schedule_id).first()
    if not sched:
        raise HTTPException(404, "Schedule not found")
    _get_editable_net(sched.net_id, current_user, db)
    db.delete(sched)
    db.commit()


@app.get("/nets/{net_id}/upcoming", response_model=list[UpcomingSlot])
def upcoming_slots(
    net_id: int,
    weeks: int = Query(8, ge=1, le=26),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the next `weeks` scheduled dates across all schedules for a net, with signup info."""
    _get_editable_net(net_id, current_user, db)
    schedules = db.query(NetSchedule).filter(NetSchedule.net_id == net_id).all()

    # Gather all upcoming dates across all schedules
    slots: list[UpcomingSlot] = []
    for sched in schedules:
        for slot_date in _next_occurrences(sched.day_of_week, weeks):
            signup_rows = db.query(NetControlSignup).filter(
                NetControlSignup.schedule_id == sched.id,
                NetControlSignup.slot_date == slot_date,
            ).all()
            slots.append(UpcomingSlot(
                slot_date=slot_date,
                day_name=DAYS[sched.day_of_week],
                schedule_id=sched.id,
                signups=[_signup_to_out(s, current_user) for s in signup_rows],
            ))

    # Sort chronologically
    slots.sort(key=lambda s: s.slot_date)
    return slots


# ---------------------------------------------------------------------------
# DMR Integration
# ---------------------------------------------------------------------------

# In-memory cache for relay-pushed DMR data { net_id: {"entries": [...], "pushed_at": float} }
# This is backed by SystemSetting so it survives server restarts.
_dmr_push_cache: dict = {}

_DMR_CACHE_TTL = 300  # seconds — matches the stale-data check in dmr_cache()


def _dmr_cache_key(net_id: int) -> str:
    return f"dmr_cache_{net_id}"


def _dmr_cache_write(net_id: int, entries: list, db: Session) -> None:
    """Write relay entries to both the in-memory dict and SystemSetting (survives restarts)."""
    now = _time.time()
    _dmr_push_cache[net_id] = {"entries": entries, "pushed_at": now}
    _set_setting(_dmr_cache_key(net_id), json.dumps({"entries": entries, "pushed_at": now}), db)
    db.commit()


def _dmr_cache_read(net_id: int, db: Session) -> Optional[dict]:
    """Return the relay cache for net_id, restoring from DB if the in-memory dict was wiped."""
    cached = _dmr_push_cache.get(net_id)
    if cached:
        return cached
    # Fallback: load from SystemSetting (e.g., after a server restart)
    raw = _get_setting(_dmr_cache_key(net_id), db)
    if raw:
        try:
            data = json.loads(raw)
            _dmr_push_cache[net_id] = data  # repopulate in-memory cache
            return data
        except Exception:
            pass
    return None


def _dmr_normalize_wpsd(entry: dict) -> dict:
    """Normalize a WPSD/Pi-Star last-heard entry to a common dict."""
    slot = str(entry.get("slot", "")).strip()
    return {
        "callsign": str(entry.get("callsign", "")).upper().strip(),
        "dmr_id":   str(entry.get("src", entry.get("id", ""))).strip() or None,
        "name":     entry.get("name") or None,
        "talk_group": str(entry.get("dst", "")).strip() or None,
        "timeslot": f"TS{slot}" if slot else None,
        "region":   entry.get("country") or None,
        "heard_at": entry.get("start") or None,
        "duration": str(entry.get("duration", "")).strip() or None,
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
    }


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
            # Pi-Star endpoint
            url = base if base.endswith("lastheard") else base + "/api/local/lastheard"
            r = httpx.get(url, timeout=10)
            r.raise_for_status()
            raw = r.json() if isinstance(r.json(), list) else []
            return [_dmr_normalize_wpsd(e) for e in raw[:30]]

        else:  # wpsd (default)
            if not cfg.hotspot_url:
                return []
            r = httpx.get(cfg.hotspot_url, params={"limit": 30, "names": "true", "country": "true"}, timeout=10)
            r.raise_for_status()
            raw = r.json() if isinstance(r.json(), list) else []
            return [_dmr_normalize_wpsd(e) for e in raw]

    except httpx.ConnectError as exc:
        raise HTTPException(502, f"Cannot reach hotspot: {exc}. If your hotspot is on a local network, enable direct mode so the browser fetches it instead.")
    except httpx.TimeoutException:
        raise HTTPException(504, "Hotspot request timed out. Check that the URL is correct and the hotspot is online.")
    except Exception as exc:
        _email_log.warning("DMR fetch error: %s", exc)
        raise HTTPException(502, f"DMR fetch failed: {exc}")


def _assert_ham_net(net: Net):
    """Raise 400 if the net is GMRS — DMR is not permitted on GMRS frequencies."""
    if net and net.net_type == "gmrs":
        raise HTTPException(400, "DMR integration is not available for GMRS nets")


@app.get("/nets/{net_id}/dmr/config", response_model=Optional[DmrConfigOut])
def get_dmr_config(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    net = _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = db.query(DmrConfig).filter(DmrConfig.net_id == net_id).first()
    return cfg  # None → null in JSON → frontend shows "not configured"


@app.put("/nets/{net_id}/dmr/config", response_model=DmrConfigOut)
def save_dmr_config(net_id: int, data: DmrConfigCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    net = _get_editable_net(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = db.query(DmrConfig).filter(DmrConfig.net_id == net_id).first()
    if cfg:
        cfg.source_type     = data.source_type
        cfg.hotspot_url     = data.hotspot_url or None
        cfg.talkgroup_id    = data.talkgroup_id
        cfg.filter_callsign = (data.filter_callsign or "").upper().strip() or None
        cfg.direct_mode     = data.direct_mode
    else:
        cfg = DmrConfig(
            net_id          = net_id,
            source_type     = data.source_type,
            hotspot_url     = data.hotspot_url or None,
            talkgroup_id    = data.talkgroup_id,
            filter_callsign = (data.filter_callsign or "").upper().strip() or None,
            direct_mode     = data.direct_mode,
        )
        db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


@app.delete("/nets/{net_id}/dmr/config", status_code=204)
def delete_dmr_config(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    net = _get_editable_net(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = db.query(DmrConfig).filter(DmrConfig.net_id == net_id).first()
    if cfg:
        db.delete(cfg)
        db.commit()


@app.get("/nets/{net_id}/dmr/lastheard", response_model=list[DmrHeardEntry])
def dmr_lastheard(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Backend-proxy last-heard fetch. Only used when direct_mode=False."""
    net = _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = db.query(DmrConfig).filter(DmrConfig.net_id == net_id).first()
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


@app.post("/nets/{net_id}/dmr/push", status_code=204)
def dmr_push(
    net_id: int,
    data: DmrPushPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Accept last-heard data pushed from a local relay script (bypasses CORS entirely)."""
    net = _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = db.query(DmrConfig).filter(DmrConfig.net_id == net_id).first()
    if not cfg:
        raise HTTPException(404, "DMR not configured for this net")
    # Filter out NCS callsign server-side too
    skip = (cfg.filter_callsign or "").upper()
    entries = [e.model_dump() for e in data.entries]
    if skip:
        entries = [e for e in entries if (e.get("callsign") or "").upper() != skip]
    _dmr_cache_write(net_id, entries, db)


class DmrRawPushPayload(BaseModel):
    """Raw (un-normalized) last-heard entries from a hotspot API.

    The relay script should send whatever the hotspot returns directly, along with
    the source type so the backend can apply the correct normalizer.  This keeps all
    normalization logic in one place and prevents relay ↔ backend drift.
    """
    source: str = "wpsd"   # wpsd | pistar | brandmeister
    entries: list[dict]


@app.post("/nets/{net_id}/dmr/push/raw", status_code=204)
def dmr_push_raw(
    net_id: int,
    data: DmrRawPushPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Accept raw hotspot JSON from a relay script and normalize server-side.

    Prefer this endpoint over /dmr/push — it keeps normalization logic in the backend
    so relay scripts stay simple fetch-and-forward proxies.
    """
    net = _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cfg = db.query(DmrConfig).filter(DmrConfig.net_id == net_id).first()
    if not cfg:
        raise HTTPException(404, "DMR not configured for this net")

    source = data.source.lower()
    if source in ("wpsd", "pistar"):
        entries = [_dmr_normalize_wpsd(e) for e in data.entries]
    elif source == "brandmeister":
        entries = [_dmr_normalize_brandmeister(e) for e in data.entries]
    else:
        raise HTTPException(400, f"Unknown source type '{source}'. Use wpsd, pistar, or brandmeister.")

    # Filter out NCS callsign and any entries with no callsign after normalization
    skip = (cfg.filter_callsign or "").upper()
    entries = [e for e in entries if e.get("callsign")]
    if skip:
        entries = [e for e in entries if e["callsign"].upper() != skip]

    _dmr_cache_write(net_id, entries, db)


@app.get("/nets/{net_id}/dmr/cache")
def dmr_cache(
    net_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return relay-pushed DMR data with freshness info."""
    net = _get_net_for_user(net_id, current_user, db)
    _assert_ham_net(net)
    cached = _dmr_cache_read(net_id, db)
    if not cached:
        raise HTTPException(404, "No relay data for this net — is the relay script running?")
    age = int(_time.time() - cached["pushed_at"])
    if age > _DMR_CACHE_TTL:
        raise HTTPException(
            404,
            f"Relay data is stale ({age}s old). Is the relay script still running?",
        )
    return {"entries": cached["entries"], "age_seconds": age}


# ---------------------------------------------------------------------------
# Net Control Signups
# ---------------------------------------------------------------------------

@app.post("/nets/{net_id}/signups", response_model=SignupOut, status_code=201)
def create_signup(net_id: int, data: SignupCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    net = _get_editable_net(net_id, current_user, db)

    # Verify the schedule belongs to this net
    sched = db.query(NetSchedule).filter(
        NetSchedule.id == data.schedule_id,
        NetSchedule.net_id == net_id,
    ).first()
    if not sched:
        raise HTTPException(404, "Schedule not found for this net")

    # Verify the slot_date is actually a valid occurrence for this schedule
    if data.slot_date.weekday() != sched.day_of_week:
        raise HTTPException(400, f"That date is not a {DAYS[sched.day_of_week]}")

    if data.role in ("broadcaster", "both") and not net.has_broadcast:
        raise HTTPException(400, "This net does not have a broadcaster role enabled")

    # A 'both' signup occupies the date exclusively; net_control/broadcaster only conflict
    # with the same role or an existing 'both' signup.
    existing_roles = {
        r for (r,) in db.query(NetControlSignup.role).filter(
            NetControlSignup.schedule_id == data.schedule_id,
            NetControlSignup.slot_date == data.slot_date,
        ).all()
    }
    conflicting = (
        "both" in existing_roles
        or data.role == "both" and existing_roles
        or data.role in existing_roles
    )
    if conflicting:
        raise HTTPException(409, "That date/role is already claimed")

    # Determine who is being signed up
    if data.assigned_user_id:
        # Net owner assigning a registered operator
        if net.owner_id != current_user.id:
            raise HTTPException(403, "Only the net owner can assign other operators")
        assigned = (
            db.query(User)
            .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
            .filter(
                User.id == data.assigned_user_id, User.is_active == True,
                OrganizationMembership.org_id == net.org_id, OrganizationMembership.approved == True,
            )
            .first()
        )
        if not assigned:
            raise HTTPException(404, "Assigned user not found")
        signup_user_id = assigned.id
        signup_callsign = (assigned.gmrs_callsign or assigned.callsign) if net.net_type == "gmrs" else assigned.callsign
        signup_name = assigned.name
        signup_email = assigned.email
    else:
        # Self sign-up
        if not data.callsign:
            raise HTTPException(400, "callsign is required for self sign-up")
        signup_user_id = current_user.id
        signup_callsign = data.callsign
        signup_name = data.name
        signup_email = data.email

    signup = NetControlSignup(
        schedule_id=data.schedule_id,
        net_id=net_id,
        slot_date=data.slot_date,
        role=data.role,
        user_id=signup_user_id,
        callsign=signup_callsign,
        name=signup_name,
        email=signup_email,
        notes=data.notes,
    )
    db.add(signup)
    db.commit()
    db.refresh(signup)

    role_label = {
        "net_control": "Net Control",
        "broadcaster": net.broadcast_label or "Broadcaster",
        "both": f"Net Control & {net.broadcast_label or 'Broadcaster'}",
    }[data.role]

    # Send confirmation email with calendar attachment if we have an address
    _email_log.info(
        "Signup created: callsign=%s role=%s email=%r smtp_configured=%s",
        signup_callsign, data.role, signup_email, _smtp_configured(),
    )
    if signup_email:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        day_name = days[sched.day_of_week]
        assigned_by_admin = bool(data.assigned_user_id)
        action = "assigned you as" if assigned_by_admin else "confirmed your sign-up as"
        subject = f"[{net.name}] {role_label} – {signup.slot_date.strftime('%a %b %-d, %Y')}"
        body_html = f"""
<html><body style="font-family:sans-serif;color:#222;max-width:600px">
<h2 style="color:#1a6496">{net.name}</h2>
<p>Hi {signup_name or signup_callsign},</p>
<p>This email {action} <strong>{role_label}</strong> for the following session:</p>
<table style="border-collapse:collapse;margin:16px 0">
  <tr><td style="padding:6px 16px 6px 0;font-weight:bold">Date</td>
      <td style="padding:6px 0">{signup.slot_date.strftime('%A, %B %-d, %Y')}</td></tr>
  <tr><td style="padding:6px 16px 6px 0;font-weight:bold">Time</td>
      <td style="padding:6px 0">{sched.start_time} {sched.timezone}</td></tr>
  {"<tr><td style='padding:6px 16px 6px 0;font-weight:bold'>Frequency</td><td style='padding:6px 0'>" + net.frequency + "</td></tr>" if net.frequency else ""}
  {"<tr><td style='padding:6px 16px 6px 0;font-weight:bold'>Notes</td><td style='padding:6px 0'>" + signup.notes + "</td></tr>" if signup.notes else ""}
</table>
<p>A calendar event is attached — add it to your calendar to set a reminder.</p>
<p style="color:#666;font-size:12px">73 de NetControl Online</p>
</body></html>"""
        body_text = (
            f"{net.name} – {role_label} Confirmation\n\n"
            f"Hi {signup_name or signup_callsign},\n\n"
            f"This email {action} {role_label} for:\n"
            f"  Date:      {signup.slot_date.strftime('%A, %B %-d, %Y')}\n"
            f"  Time:      {sched.start_time} {sched.timezone}\n"
            + (f"  Frequency: {net.frequency}\n" if net.frequency else "")
            + (f"  Notes:     {signup.notes}\n" if signup.notes else "")
            + "\nA calendar event (.ics) is attached.\n\n73 de NetControl Online"
        )
        try:
            ics = _build_ics(net, sched, signup, role_label=role_label)
            send_email(
                to=[signup_email],
                subject=subject,
                body_html=body_html,
                body_text=body_text,
                ics_content=ics,
                ics_filename=f"netcontrol-{signup.slot_date}.ics",
            )
        except Exception as exc:
            _email_log.warning("Failed to send signup confirmation to %s: %s", signup_email, exc)

    return _signup_to_out(signup, current_user)


@app.delete("/signups/{signup_id}", status_code=204)
def delete_signup(signup_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    signup = db.query(NetControlSignup).filter(NetControlSignup.id == signup_id).first()
    if not signup:
        raise HTTPException(404, "Signup not found")
    # Net owner can delete any signup; operators can only delete their own
    net = db.query(Net).filter(Net.id == signup.net_id).first()
    if signup.user_id != current_user.id and (not net or net.owner_id != current_user.id):
        raise HTTPException(403, "Not authorised to remove this signup")
    db.delete(signup)
    db.commit()


@app.get("/nets/{net_id}/signups", response_model=list[SignupOut])
def list_signups(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_editable_net(net_id, current_user, db)
    signups = (
        db.query(NetControlSignup)
        .filter(NetControlSignup.net_id == net_id)
        .order_by(NetControlSignup.slot_date)
        .all()
    )
    return [_signup_to_out(s, current_user) for s in signups]


# ---------------------------------------------------------------------------
# Net share management endpoints
# ---------------------------------------------------------------------------

@app.get("/nets/{net_id}/shares")
def get_net_shares(net_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the current sharing config for a net (owner or admin only)."""
    _get_owned_net(net_id, current_user, db)
    shares = db.query(NetShare).filter(NetShare.net_id == net_id).all()
    all_share = next((s for s in shares if s.user_id is None), None)
    return {
        "share_with_all": all_share is not None,
        "can_edit_all": bool(all_share and all_share.can_edit),
        "user_ids": [s.user_id for s in shares if s.user_id is not None],
        "editor_user_ids": [s.user_id for s in shares if s.user_id is not None and s.can_edit],
    }


@app.put("/nets/{net_id}/shares", status_code=204)
def update_net_shares(net_id: int, data: NetShareUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Replace the sharing config for a net (owner or admin only)."""
    _get_owned_net(net_id, current_user, db)
    # Wipe existing shares for this net
    db.query(NetShare).filter(NetShare.net_id == net_id).delete()
    if data.share_with_all:
        db.add(NetShare(net_id=net_id, user_id=None, can_edit=data.can_edit_all))
    else:
        editor_ids = set(data.editor_user_ids)
        for uid in data.user_ids:
            db.add(NetShare(net_id=net_id, user_id=uid, can_edit=uid in editor_ids))
    db.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _net_to_out(net: Net, user: User, db: Session) -> NetOut:
    """Build a NetOut with sharing metadata attached."""
    shares = db.query(NetShare).filter(NetShare.net_id == net.id).all()
    owner = db.query(User).filter(User.id == net.owner_id).first()
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


def _get_owned_net(net_id: int, user: User, db: Session) -> Net:
    """Fetch a net; require owner or admin. Non-admins are further scoped to
    their current org (issue #1) — a net in a different org 404s rather than
    403s, so its existence isn't leaked across tenants. Super admins bypass
    org scoping entirely, same as they already bypass ownership. Deliberately
    NOT satisfied by an edit-rights share (see _get_editable_net below) —
    reserved for destructive/sensitive actions: deleting the net, and
    managing sharing itself (an editor granting themselves or others further
    access would be a privilege-escalation chain)."""
    net = db.query(Net).filter(Net.id == net_id).first()
    if not net:
        raise HTTPException(404, "Net not found")
    if user.is_admin:
        return net
    if net.org_id != user.current_org_id:
        raise HTTPException(404, "Net not found")
    if net.owner_id != user.id:
        raise HTTPException(403, "Not your net")
    return net


def _get_editable_net(net_id: int, user: User, db: Session) -> Net:
    """Like _get_owned_net, but also allows a user explicitly granted edit
    rights via sharing (NetShare.can_edit) — issue follow-up: previously
    sharing only ever granted view/check-in access, with no way to let a
    trusted co-operator help maintain a net's schedule, DMR config, evac
    zones, etc. without handing them full ownership. Used for exactly that
    kind of net-configuration endpoint; delete_net and the sharing endpoints
    themselves stay on the stricter _get_owned_net."""
    try:
        return _get_owned_net(net_id, user, db)
    except HTTPException as e:
        if e.status_code == 403:
            share = db.query(NetShare).filter(
                NetShare.net_id == net_id,
                NetShare.can_edit == True,
                or_(NetShare.user_id == user.id, NetShare.user_id == None),
            ).first()
            if share:
                return db.query(Net).filter(Net.id == net_id).first()
        raise


def _get_net_for_user(net_id: int, user: User, db: Session) -> Net:
    """Fetch a net; allow owner, admin, or user the net is shared with.
    Org-scoped for non-admins the same way as _get_owned_net above."""
    net = db.query(Net).filter(Net.id == net_id).first()
    if not net:
        raise HTTPException(404, "Net not found")
    if user.is_admin:
        return net
    if net.org_id != user.current_org_id:
        raise HTTPException(404, "Net not found")
    if net.owner_id == user.id:
        return net
    # Check shares: shared with all (user_id IS NULL) or shared with this user
    share = (
        db.query(NetShare)
        .filter(
            NetShare.net_id == net_id,
            or_(NetShare.user_id == user.id, NetShare.user_id == None),
        )
        .first()
    )
    if not share:
        raise HTTPException(403, "Access denied")
    return net


def _get_session_for_user(session_id: int, user: User, db: Session) -> NetSession:
    session = db.query(NetSession).filter(NetSession.id == session_id).first()
    if not session:
        raise HTTPException(404, "Session not found")
    _get_net_for_user(session.net_id, user, db)
    return session
