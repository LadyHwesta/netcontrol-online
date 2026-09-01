"""
Public live page — unauthenticated endpoints powering /live, /directory,
and their supporting JSON APIs.
"""

import html
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Checkin, Net, NetSchedule, NetSession, Organization, User
from routers.aprs import _public_aprs_positions
from routers.helpers import STATIC_DIR
from routers.orgs import _org_to_out
from routers.schedules import _duty_labels_for_session, _schedule_to_out
from routers.schemas import OrganizationOut

router = APIRouter()

# Project root -- STATIC_DIR is <root>/static, so its parent is <root>, where
# public.html/directory.html/index.html live alongside main.py.
_PROJECT_ROOT = STATIC_DIR.parent


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


@router.get("/live", response_class=HTMLResponse, include_in_schema=False)
@router.get("/live/{org_slug}", response_class=HTMLResponse, include_in_schema=False)
async def public_live_page(request: Request, org_slug: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """Serve the public live nets page. org_slug (issue #1), if present, is
    read client-side from the URL path — same SPA path-routing convention as
    /directory/{slug} below. Bare /live with no slug renders an org picker.
    Title/description/canonical are also injected server-side per org (see
    _inject_seo_meta) for crawlers and link-preview bots that don't run JS."""
    content = (_PROJECT_ROOT / "public.html").read_text()
    if org_slug:
        org = (await db.execute(select(Organization).filter(Organization.slug == org_slug))).scalar_one_or_none()
        if org:
            content = _inject_seo_meta(
                content,
                title=f"Live Nets — {org.name}",
                description=f"See which amateur radio and GMRS nets are on the air right now for {org.name}, with live check-in rosters.",
                canonical_path=f"/live/{org_slug}",
                request=request,
            )
    return HTMLResponse(content)


@router.get("/public/active")
async def public_active_sessions(org: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """Return all currently active net sessions for one org — no auth
    required. Org-scoped (issue #1); omitting `org` falls back to the
    "default" org (single-tenant backward compat — see _get_or_create_org).
    Deliberately NOT gated on Net.public_listed, unlike /public/directory —
    this page has always shown any net currently in progress in the org,
    listed or not (see TestSchedules::test_public_active_shows_broadcaster)."""
    org_row = (await db.execute(select(Organization).filter(Organization.slug == (org or "default")))).scalar_one_or_none()
    if not org_row:
        return []
    sessions = (
        (await db.execute(select(NetSession).join(Net, Net.id == NetSession.net_id).filter(NetSession.ended_at == None, Net.org_id == org_row.id).order_by(NetSession.started_at))).scalars().all()
    )
    result = []
    for s in sessions:
        net = (await db.execute(select(Net).filter(Net.id == s.net_id))).scalar_one_or_none()
        if not net:
            continue
        count = (await db.execute(select(func.count(Checkin.id)).filter(Checkin.session_id == s.id))).scalar()
        aprs_positions, aprs_source = await _public_aprs_positions(net, db)
        result.append({
            "session_id": s.id,
            "net_name": net.name,
            "frequency": net.frequency,
            "started_at": s.started_at.isoformat(),
            "checkin_count": count,
            "aprs_map_enabled": net.aprs_map_enabled,
            "aprs_positions": aprs_positions,
            "aprs_source": aprs_source,   # "aprs_fi" | "relay" | None -- required aprs.fi credit on the map
            **await _duty_labels_for_session(net, s, db),
        })
    return result


@router.get("/public/sessions/{session_id}")
async def public_session_detail(session_id: int, db: AsyncSession = Depends(get_db)):
    """Return session info + checkin list — no auth required. Keyed directly
    by session ID (reached by clicking through from the already org-scoped
    /public/active list), so no separate org check is needed here."""
    s = (await db.execute(select(NetSession).filter(NetSession.id == session_id, NetSession.ended_at == None))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Session not found or no longer active")
    net = (await db.execute(select(Net).filter(Net.id == s.net_id))).scalar_one_or_none()
    checkins = (
        (await db.execute(select(Checkin).filter(Checkin.session_id == session_id).order_by(Checkin.checked_in_at))).scalars().all()
    )
    duty = await _duty_labels_for_session(net, s, db) if net else {
        "ncs_callsign": None, "ncs_name": None,
        "broadcaster_callsign": None, "broadcaster_name": None, "broadcast_label": None,
        "next_ncs_callsign": None, "next_ncs_name": None,
        "next_broadcaster_callsign": None, "next_broadcaster_name": None,
    }
    aprs_positions, aprs_source = await _public_aprs_positions(net, db) if net else ([], None)
    return {
        "session_id": s.id,
        "net_name": net.name if net else "Unknown Net",
        "frequency": net.frequency if net else None,
        "started_at": s.started_at.isoformat(),
        "checkins": [
            {"callsign": c.callsign, "name": c.name}
            for c in checkins
        ],
        "aprs_map_enabled": net.aprs_map_enabled if net else False,
        "aprs_positions": aprs_positions,
        "aprs_source": aprs_source,   # "aprs_fi" | "relay" | None -- required aprs.fi credit on the map
        "aprs_default_lat": net.aprs_default_lat if net else None,
        "aprs_default_lon": net.aprs_default_lon if net else None,
        "aprs_default_zoom": net.aprs_default_zoom if net else None,
        **duty,
    }


@router.get("/directory", response_class=HTMLResponse, include_in_schema=False)
@router.get("/directory/{org_slug}", response_class=HTMLResponse, include_in_schema=False)
async def public_directory_page(request: Request, org_slug: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """Serve the public net directory page. org_slug (issue #1), if present,
    is read client-side from the URL path — the frontend calls
    /public/directory?org=<slug> accordingly. Bare /directory with no slug
    renders an org picker (from /public/organizations) instead of a net list.
    Title/description/canonical are also injected server-side per org (see
    _inject_seo_meta) for crawlers and link-preview bots that don't run JS."""
    content = (_PROJECT_ROOT / "directory.html").read_text(encoding="utf-8")
    if org_slug:
        org = (await db.execute(select(Organization).filter(Organization.slug == org_slug))).scalar_one_or_none()
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


@router.get("/public/organizations", response_model=list[OrganizationOut])
async def public_organizations(db: AsyncSession = Depends(get_db)):
    """Orgs with at least one net in the public directory — powers the org
    picker shown at bare /directory or /live (no slug in the URL)."""
    orgs = (
        (await db.execute(select(Organization).join(Net, Net.org_id == Organization.id).filter(Net.public_listed == True).distinct().order_by(Organization.name))).scalars().all()
    )
    return [_org_to_out(org) for org in orgs]


@router.get("/public/organizations/{slug}", response_model=OrganizationOut)
async def public_organization_by_slug(slug: str, db: AsyncSession = Depends(get_db)):
    """One org's public branding (name/tagline/logo) by slug — powers
    per-org branding on /directory/{slug} and /live/{slug} (issue
    follow-up). Deliberately NOT filtered by Net.public_listed like the
    picker above — an org's own page shows its own branding regardless of
    whether it happens to have a public net listed right now."""
    org = (await db.execute(select(Organization).filter(Organization.slug == slug))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    return _org_to_out(org)


@router.get("/public/directory")
async def public_directory(org: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """Return every net whose owner has opted into the public directory, for
    one org — no auth required. Org-scoped (issue #1); omitting `org` falls
    back to the "default" org (single-tenant backward compat)."""
    org_row = (await db.execute(select(Organization).filter(Organization.slug == (org or "default")))).scalar_one_or_none()
    if not org_row:
        return []
    nets = (
        (await db.execute(select(Net).filter(Net.public_listed == True, Net.org_id == org_row.id).order_by(Net.name))).scalars().all()
    )
    result = []
    for net in nets:
        owner = (await db.execute(select(User).filter(User.id == net.owner_id))).scalar_one_or_none()
        schedules = (
            (await db.execute(select(NetSchedule).filter(NetSchedule.net_id == net.id).order_by(NetSchedule.day_of_week))).scalars().all()
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
