"""
NetControl Online — FastAPI backend (app assembly)

This file wires up the FastAPI app itself (logging, lifespan, middleware,
static mounts) and the SPA/service-worker/manifest/SEO routes that don't
belong to any one feature domain. Every domain's actual routes live in
routers/<domain>.py, included below — see TECH_DEBT.md's (resolved)
"Single-file backend" entry for the design behind this split.
"""

import html
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, init_db
from models import Net, Organization

from routers.deps import limiter
from routers.helpers import STATIC_DIR, UPLOADS_DIR, _get_setting, _public_base_url

from routers import (
    admin, aprs, auth, callsign_lookup, checkins, digital_voice, evac_zones,
    expected_stations, history, nets, orgs, public, schedules, sessions,
    support, tactical, traffic, translation,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Without this, only loggers with their own explicit handler (routers/auth.py's
# AUTH_LOG_FILE handler) produce visible output. Everything else falls back to
# Python's WARNING-level "handler of last resort", so INFO messages — a
# successful Net Repository push, a sent email — never appear anywhere, not
# even in the systemd journal, even though the equivalent failures (logged at
# WARNING) already do.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
LOGO_PATH = UPLOADS_DIR / "logo"


@asynccontextmanager
async def lifespan(_app):
    await init_db()
    UPLOADS_DIR.mkdir(exist_ok=True)
    yield


app = FastAPI(title="NetControl Online", version="2.34.0", lifespan=lifespan)
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
# Domain routers
# ---------------------------------------------------------------------------
app.include_router(auth.router)
app.include_router(orgs.router)
app.include_router(nets.router)
app.include_router(sessions.router)
app.include_router(checkins.router)
app.include_router(tactical.router)
app.include_router(evac_zones.router)
app.include_router(expected_stations.router)
app.include_router(traffic.router)
app.include_router(history.router)
app.include_router(callsign_lookup.router)
app.include_router(schedules.router)
app.include_router(digital_voice.router)
app.include_router(aprs.router)
app.include_router(admin.router)
app.include_router(public.router)
app.include_router(support.router)
app.include_router(translation.router)


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
async def serve_manifest(db: AsyncSession = Depends(get_db)):
    """PWA web manifest (issue #9). Generated dynamically rather than a static
    file so name/short_name pick up the org's own Branding settings instead of
    a hardcoded name — icons stay fixed to the built-in mark (reliable/square)
    regardless of any uploaded club logo."""
    org_name = await _get_setting("org_name", db) or "NetControl Online"
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
async def sitemap_xml(request: Request, db: AsyncSession = Depends(get_db)):
    """Lists each organization's public directory/live pages (issue #1) — the
    same set /public/organizations already computes: orgs with at least one
    net actually opted into the public directory, so nothing thin or private
    gets listed."""
    base = _public_base_url(request)
    orgs_list = (
        (await db.execute(select(Organization).join(Net, Net.org_id == Organization.id).filter(Net.public_listed == True).distinct().order_by(Organization.name))).scalars().all()
    )
    entries = [(f"{base}/directory", "0.5", "weekly"), (f"{base}/live", "0.3", "daily")]
    for org in orgs_list:
        entries.append((f"{base}/directory/{org.slug}", "0.9", "weekly"))
        entries.append((f"{base}/live/{org.slug}", "0.4", "hourly"))
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, priority, changefreq in entries:
        body.append(f"  <url><loc>{html.escape(loc)}</loc><changefreq>{changefreq}</changefreq><priority>{priority}</priority></url>")
    body.append("</urlset>")
    return Response(content="\n".join(body) + "\n", media_type="application/xml")
