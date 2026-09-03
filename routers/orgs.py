"""
Organizations (issue #1 — multi-tenancy) + Branding.
"""

import pathlib
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub_delivery
import activitypub_signing
from database import get_db
from models import ActivityPubFollower, Checkin, EnabledLanguage, Net, NetSession, Organization, OrganizationMembership, OrganizationMembershipRole, OrgEnabledLanguage, SystemSetting, User
from routers import helpers
from routers.deps import get_current_user
from routers.helpers import ORG_EXTRA_ROLES, SELF_REQUESTABLE_ROLES, _get_or_create_org, _grant_default_net_control_op, _net_to_out, _org_logo_file, _org_role_set
from routers.schemas import AdminUserOut, NetOut, OrganizationOut, UserOut
from routers.translation import _translation_configured, run_enable_language_job

router = APIRouter()


def _org_to_out(org: Organization, cls=OrganizationOut, **extra) -> OrganizationOut:
    """Builds OrganizationOut (or a subclass -- see MyOrgOut's `cls=` callers)
    with has_logo computed from disk -- not a real column, so it can't be
    picked up by response_model's automatic from_attributes conversion.
    Every endpoint returning an org (or list of them) goes through this
    instead of a bare `return org`/`.scalars().all()` so has_logo/tagline
    are never silently dropped."""
    return cls(
        id=org.id, name=org.name, slug=org.slug, website_url=org.website_url,
        banner_message=org.banner_message, tagline=org.tagline,
        has_logo=_org_logo_file(org.id) is not None,
        registration_open=org.registration_open, **extra,
    )


class OrgMemberOut(BaseModel):
    """A user's membership within one org — used for the org-admin approval
    queue and member list. Distinct from UserOut since it's per-membership,
    not per-account (a user can appear once per org they belong to)."""
    user_id: int
    callsign: str
    name: str
    email: str
    role: str
    # Role revamp (issue follow-up): the membership's full canonical role set
    # -- {"admin"} or {"net_control_op"} plus any of tactical_operator/
    # broadcaster -- and, for a still-pending row, what the registrant asked
    # for (informational hint only, see OrganizationMembership.requested_roles).
    roles: list[str] = []
    requested_roles: list[str] = []
    approved: bool
    requested_at: datetime


class MyOrgOut(OrganizationOut):
    """Like OrganizationOut, plus the caller's own role in that org — lets the
    frontend show the org-admin panel only where the user actually has it."""
    role: str
    roles: list[str] = []


async def require_org_admin(org_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> User:
    """Org-scoped equivalent of require_admin — an approved admin of THIS org,
    or a super admin (User.is_admin bypasses org scoping everywhere, including
    here)."""
    if current_user.is_admin:
        return current_user
    membership = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id,
        OrganizationMembership.user_id == current_user.id,
        OrganizationMembership.role == "admin",
        OrganizationMembership.approved == True,
    ))).scalar_one_or_none()
    if not membership:
        raise HTTPException(403, "Organization admin access required")
    return current_user


@router.get("/orgs", response_model=list[OrganizationOut])
async def list_orgs(registration_open: Optional[bool] = None, db: AsyncSession = Depends(get_db)):
    """Organizations that actually have someone who could approve a join
    request — name+slug only. No auth required: same trust level as
    callsign/name being visible in the registration form itself, and an
    org's existence isn't sensitive. Excludes an org with no approved admin
    (e.g. its founder was rejected/deleted before anyone else joined) —
    await _delete_orphaned_orgs() cleans those up outright, but this filter is a
    second line of defense against ever listing a dead-end org (issue #1
    follow-up).

    This one endpoint has two different audiences, so the invite-only
    filter (issue follow-up) is opt-in via ?registration_open=true rather
    than baked in: the public "join an existing organization" picker at
    registration (routers/helpers.py's loadRegOrgPicker()) passes it, since
    self-registration into an invite-only org is separately blocked
    server-side anyway (_get_or_create_org) and showing it there would just
    be a dead end. But three *authenticated admin* call sites also reuse
    this same endpoint to list every org regardless (the org-edit form
    finding the admin's own org, a super admin's "add operator"/"reassign"
    org pickers) -- those must keep seeing invite-only orgs, since Add
    Operator is the intended way *into* one, and an org admin has to be
    able to find their own now-hidden org to ever flip this back off."""
    query = select(Organization).join(OrganizationMembership, OrganizationMembership.org_id == Organization.id).filter(
        OrganizationMembership.role == "admin", OrganizationMembership.approved == True,
    )
    if registration_open is not None:
        query = query.filter(Organization.registration_open == registration_open)
    orgs = (await db.execute(query.distinct().order_by(Organization.name))).scalars().all()
    return [_org_to_out(org) for org in orgs]


@router.get("/orgs/mine", response_model=list[MyOrgOut])
async def list_my_orgs(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The current user's own approved organizations, with their role in each
    — powers the org switcher and the org-admin panel visibility check."""
    rows = (await db.execute(
        select(Organization, OrganizationMembership)
        .join(OrganizationMembership, OrganizationMembership.org_id == Organization.id)
        .filter(OrganizationMembership.user_id == current_user.id, OrganizationMembership.approved == True)
        .order_by(Organization.name)
    )).all()
    out = []
    for org, m in rows:
        roles = {"admin"} if m.role == "admin" else set()
        extra = (await db.execute(select(OrganizationMembershipRole.role).filter(
            OrganizationMembershipRole.membership_id == m.id
        ))).scalars().all()
        roles.update(extra)
        out.append(_org_to_out(org, cls=MyOrgOut, role=m.role, roles=sorted(roles)))
    return out


class OrganizationUpdate(BaseModel):
    name: str
    website_url: Optional[str] = None
    banner_message: Optional[str] = None
    tagline: Optional[str] = None
    registration_open: bool = True


@router.patch("/orgs/{org_id}", response_model=OrganizationOut)
async def update_org(org_id: int, data: OrganizationUpdate, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Rename an org / fix its website / set its banner message or tagline —
    previously there was no way to do any of this at all once created (issue
    #1 follow-up; an org's name is its own property, independent of the
    instance-wide Branding settings, so changing Branding doesn't
    retroactively rename any org). Slug is intentionally not editable here
    — it's baked into public /directory/<slug> and /live/<slug> URLs. The
    logo half of per-org branding is a separate upload -- see
    POST/DELETE /orgs/{org_id}/logo below."""
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
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
    org.banner_message = (data.banner_message or "").strip() or None
    org.tagline = (data.tagline or "").strip() or None
    org.registration_open = data.registration_open
    await db.commit()
    await db.refresh(org)
    return _org_to_out(org)


class OrgAprsKeyOut(BaseModel):
    aprs_fi_api_key: Optional[str] = None


class OrgAprsKeyUpdate(BaseModel):
    aprs_fi_api_key: Optional[str] = None


@router.get("/orgs/{org_id}/aprs-key", response_model=OrgAprsKeyOut)
async def get_org_aprs_key(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Org admin only -- deliberately NOT part of OrganizationOut (returned
    by GET /orgs, GET /orgs/mine, GET /public/organizations), since a real
    secret has no business in those broadly-readable responses. One key per
    org, shared by every net in it that uses aprs_fi as its APRS source
    (issue follow-up) -- see routers/aprs.py's _aprs_positions_for_net."""
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    return OrgAprsKeyOut(aprs_fi_api_key=org.aprs_fi_api_key)


@router.put("/orgs/{org_id}/aprs-key", response_model=OrgAprsKeyOut)
async def update_org_aprs_key(org_id: int, data: OrgAprsKeyUpdate, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    org.aprs_fi_api_key = (data.aprs_fi_api_key or "").strip() or None
    await db.commit()
    return OrgAprsKeyOut(aprs_fi_api_key=org.aprs_fi_api_key)


# ---------------------------------------------------------------------------
# Fediverse participation (issue follow-up) — org-admin enable/disable +
# status. The actual public-facing ActivityPub protocol endpoints (actor
# document, WebFinger, inbox, ...) live in routers/activitypub.py instead,
# since those must be reachable with no auth at all; this pair is the only
# ActivityPub-related surface that needs an org admin logged in.
# ---------------------------------------------------------------------------

class OrgActivityPubOut(BaseModel):
    enabled: bool
    handle: Optional[str] = None
    actor_url: Optional[str] = None
    follower_count: int = 0


class OrgActivityPubUpdate(BaseModel):
    enabled: bool


@router.get("/orgs/{org_id}/activitypub", response_model=OrgActivityPubOut)
async def get_org_activitypub(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    if not org.activitypub_enabled:
        return OrgActivityPubOut(enabled=False)
    follower_count = (await db.execute(
        select(func.count(ActivityPubFollower.id)).filter(ActivityPubFollower.org_id == org.id)
    )).scalar()
    return OrgActivityPubOut(
        enabled=True,
        handle=activitypub_delivery.build_handle(org),
        actor_url=activitypub_delivery.build_actor_id(org),
        follower_count=follower_count,
    )


@router.put("/orgs/{org_id}/activitypub", response_model=OrgActivityPubOut)
async def update_org_activitypub(org_id: int, data: OrgActivityPubUpdate, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Enabling generates the org's RSA keypair the first time only --
    never regenerated on later enable/disable toggles, since an existing
    remote follower's cached publicKeyPem would silently break otherwise
    (see activitypub_delivery.py/the Organization model's own comment).
    Disabling just flips the flag; the keypair and follower list are kept,
    so re-enabling resumes posting to the same followers."""
    if data.enabled and not activitypub_delivery.activitypub_configured():
        raise HTTPException(400, "APP_BASE_URL must be configured on this instance before Fediverse participation can be enabled")
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    if data.enabled and not org.activitypub_private_key:
        org.activitypub_private_key, org.activitypub_public_key = activitypub_signing.generate_keypair()
    org.activitypub_enabled = data.enabled
    await db.commit()
    await db.refresh(org)
    return await get_org_activitypub(org_id, admin, db)


# ---------------------------------------------------------------------------
# Per-organization branding — logo (issue follow-up). The text half
# (tagline) is edited via PATCH /orgs/{org_id} above, alongside name/
# website/banner; the logo is a file upload so it gets its own endpoints,
# mirroring the instance-wide POST/DELETE /admin/branding/logo and public
# GET /logo below exactly, just org-scoped via require_org_admin and
# namespaced on disk via _org_logo_file().
# ---------------------------------------------------------------------------

@router.post("/orgs/{org_id}/logo", status_code=204)
async def upload_org_logo(org_id: int, file: UploadFile = File(...), admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Org admin only — upload this org's own logo (PNG, JPG, GIF, WebP, SVG),
    shown on this org's public /directory and /live pages and in the header
    while working within it (falls back to instance-wide Branding's logo, if
    any, when an org hasn't set its own — see static/js/branding.js)."""
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in helpers._LOGO_EXTS:
        raise HTTPException(400, "Unsupported file type — use PNG, JPG, GIF, WebP, or SVG")
    for old in helpers.UPLOADS_DIR.glob(f"org_{org_id}_logo.*"):
        old.unlink(missing_ok=True)
    dest = helpers.UPLOADS_DIR / f"org_{org_id}_logo.{ext}"
    dest.write_bytes(await file.read())


@router.delete("/orgs/{org_id}/logo", status_code=204)
async def delete_org_logo(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Org admin only — remove this org's own logo (instance-wide Branding's
    logo, if set, takes back over for this org's pages)."""
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    for old in helpers.UPLOADS_DIR.glob(f"org_{org_id}_logo.*"):
        old.unlink(missing_ok=True)


@router.get("/orgs/{org_id}/logo")
def get_org_logo(org_id: int):
    """Public endpoint — serves an org's uploaded logo file. No auth: loaded
    directly in <img src> on public /directory/{slug} and /live/{slug} pages,
    same trust level as the instance-wide GET /logo."""
    p = _org_logo_file(org_id)
    if not p:
        raise HTTPException(404, "No logo uploaded for this organization")
    ext = p.suffix.lstrip(".")
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")
    return Response(content=p.read_bytes(), media_type=mime)


# ---------------------------------------------------------------------------
# UI translation (argos-translate, opt-in TRANSLATION_ENABLED) — per-org
# opt-in into the server-wide language catalog (models.EnabledLanguage).
# Each org's admin manages their own org's list independently; installing a
# not-yet-seen language's model is shared, one-time work triggered by
# whichever org asks for it first (see OrgEnabledLanguage's docstring).
# ---------------------------------------------------------------------------

class OrgLanguageOut(BaseModel):
    code: str
    display_name: str
    model_status: str
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}


class OrgLanguageCreate(BaseModel):
    code: str
    display_name: str


@router.get("/orgs/{org_id}/languages", response_model=list[OrgLanguageOut])
async def org_list_languages(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(EnabledLanguage)
        .join(OrgEnabledLanguage, OrgEnabledLanguage.code == EnabledLanguage.code)
        .filter(OrgEnabledLanguage.org_id == org_id)
        .order_by(EnabledLanguage.display_name)
    )).scalars().all()
    return rows


@router.post("/orgs/{org_id}/languages", response_model=OrgLanguageOut, status_code=201)
async def org_enable_language(
    org_id: int,
    data: OrgLanguageCreate,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """Opts this org into a language. If nobody on this server has enabled
    this code before, this also creates the shared catalog row and kicks off
    the real work (model install + bulk pretranslate) -- same background job
    as before, just no longer gated behind a super admin. If another org
    already enabled this code, this org just piggybacks on the existing
    (possibly already-ready) catalog row -- no re-install, no re-download."""
    if not _translation_configured():
        raise HTTPException(503, "Translation isn't configured on this server (set TRANSLATION_ENABLED=true)")
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    code = data.code.strip().lower()

    already = (await db.execute(select(OrgEnabledLanguage).filter(
        OrgEnabledLanguage.org_id == org_id, OrgEnabledLanguage.code == code,
    ))).scalar_one_or_none()
    if already:
        raise HTTPException(400, f"{code} is already enabled for this organization")

    lang = (await db.execute(select(EnabledLanguage).filter(EnabledLanguage.code == code))).scalar_one_or_none()
    if lang is None:
        lang = EnabledLanguage(code=code, display_name=data.display_name.strip(), model_status="pending")
        db.add(lang)
        await db.flush()
        background_tasks.add_task(run_enable_language_job, code, lang.display_name)

    db.add(OrgEnabledLanguage(org_id=org_id, code=code))
    await db.commit()
    await db.refresh(lang)
    return lang


@router.delete("/orgs/{org_id}/languages/{code}", status_code=204)
async def org_disable_language(org_id: int, code: str, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Removes this org's opt-in only. The installed model, its
    translation_cache rows, and the shared EnabledLanguage catalog row are
    left untouched -- other orgs may still be using it (super-admin-only
    GET/DELETE /admin/languages covers fully removing a language from the
    server's catalog)."""
    row = (await db.execute(select(OrgEnabledLanguage).filter(
        OrgEnabledLanguage.org_id == org_id, OrgEnabledLanguage.code == code,
    ))).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()


class OrgJoinRequest(BaseModel):
    org_slug: Optional[str] = None
    org_name: Optional[str] = None
    org_website_url: Optional[str] = None
    requested_roles: Optional[list[str]] = None   # same role-interest hint as registration (issue follow-up)

    @field_validator("requested_roles")
    @classmethod
    def valid_requested_roles(cls, v):
        if v is None:
            return v
        bad = [r for r in v if r not in SELF_REQUESTABLE_ROLES]
        if bad:
            raise ValueError(f"Invalid requested role(s): {', '.join(bad)}")
        return v


@router.post("/orgs/join", response_model=OrganizationOut, status_code=201)
async def join_org(data: OrgJoinRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Already-logged-in self-service: request to join an additional org (or
    create a new one), same join-or-create semantics as registration. Does
    not touch is_active — the caller is already active via an existing org.
    Unlike registration, a newly founded org here is ALWAYS pending (never
    self-approved) — the caller being active elsewhere doesn't make them a
    trustworthy org founder; a super admin still needs to sign off via the
    existing /admin/users/{id}/approve (issue #1 follow-up)."""
    org, org_created = await _get_or_create_org(data.org_slug, data.org_name, data.org_website_url, db, block_invite_only=True)
    existing = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org.id, OrganizationMembership.user_id == current_user.id,
    ))).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Already a member (or pending member) of this organization")
    db.add(OrganizationMembership(
        org_id=org.id,
        user_id=current_user.id,
        role="admin" if org_created else "member",
        approved=False,
        requested_roles=",".join(data.requested_roles) if data.requested_roles else None,
    ))
    await db.commit()
    return org


class CurrentOrgUpdate(BaseModel):
    org_id: int


@router.patch("/auth/current-org", response_model=UserOut)
async def switch_current_org(data: CurrentOrgUpdate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Switch which org the user is "working as" — every net/session/checkin
    endpoint scopes to current_org_id from here on. Restricted to orgs the user
    has an APPROVED membership in (super admins may switch to any org, since
    they already see everything regardless)."""
    if not current_user.is_admin:
        membership = (await db.execute(select(OrganizationMembership).filter(
            OrganizationMembership.org_id == data.org_id,
            OrganizationMembership.user_id == current_user.id,
            OrganizationMembership.approved == True,
        ))).scalar_one_or_none()
        if not membership:
            raise HTTPException(403, "Not an approved member of that organization")
    else:
        if not (await db.execute(select(Organization).filter(Organization.id == data.org_id))).scalar_one_or_none():
            raise HTTPException(404, "Organization not found")
    current_user.current_org_id = data.org_id
    await db.commit()
    await db.refresh(current_user)
    return current_user


async def _org_member_out_rows(rows: list, db: AsyncSession) -> list[OrgMemberOut]:
    """Shared by the pending-queue and member-list endpoints below — attaches
    each membership's full canonical role set (+ requested_roles, for the
    pending queue's approval-hint) in one extra query rather than N+1."""
    membership_ids = [m.id for m, _u in rows]
    extra_by_membership: dict[int, set[str]] = {}
    if membership_ids:
        extra_rows = (await db.execute(select(OrganizationMembershipRole).filter(
            OrganizationMembershipRole.membership_id.in_(membership_ids)
        ))).scalars().all()
        for r in extra_rows:
            extra_by_membership.setdefault(r.membership_id, set()).add(r.role)
    out = []
    for m, u in rows:
        roles = ({"admin"} if m.role == "admin" else set()) | extra_by_membership.get(m.id, set())
        requested = [r for r in (m.requested_roles or "").split(",") if r]
        out.append(OrgMemberOut(
            user_id=u.id, callsign=u.callsign, name=u.name, email=u.email,
            role=m.role, roles=sorted(roles), requested_roles=requested,
            approved=m.approved, requested_at=m.created_at,
        ))
    return out


@router.get("/orgs/{org_id}/pending-members", response_model=list[OrgMemberOut])
async def list_pending_org_members(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.org_id == org_id, OrganizationMembership.approved == False)
        .order_by(OrganizationMembership.created_at.desc())
    )).all()
    return await _org_member_out_rows(rows, db)


@router.get("/orgs/{org_id}/members", response_model=list[OrgMemberOut])
async def list_org_members(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.org_id == org_id, OrganizationMembership.approved == True)
        .order_by(User.callsign)
    )).all()
    return await _org_member_out_rows(rows, db)


@router.get("/orgs/{org_id}/nets", response_model=list[NetOut])
async def list_org_nets(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Every net in this org, regardless of ownership or sharing — lets an
    org admin see (and reassign ownership of) every net in their org, not
    just ones they personally own or are shared on (issue follow-up).
    list_nets doesn't do this for non-super-admins: org-admin role alone
    was never a substitute for owning or being shared on a net."""
    nets = (await db.execute(select(Net).filter(Net.org_id == org_id).order_by(Net.name))).scalars().all()
    return [await _net_to_out(n, admin, db) for n in nets]


class OrgMemberApprove(BaseModel):
    # Role revamp (issue follow-up): the roles (net_control_op/
    # tactical_operator/broadcaster) to grant right away, alongside approval
    # -- typically the admin panel's approve button pre-fills this with
    # net_control_op checked by default (the normal case) plus whatever the
    # pending row's own requested_roles suggests, editable before submitting.
    # Omitted/empty grants none at all; roles can always be changed later via
    # PUT .../extra-roles. "admin" is not settable here -- see
    # update_org_member_role for that.
    roles: list[str] = []

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, v):
        bad = [r for r in v if r not in ORG_EXTRA_ROLES]
        if bad:
            raise ValueError(f"Invalid role(s): {', '.join(bad)} — only {', '.join(ORG_EXTRA_ROLES)} may be set here")
        return v


@router.patch("/orgs/{org_id}/members/{user_id}/approve", status_code=204)
async def approve_org_member(org_id: int, user_id: int, data: OrgMemberApprove = OrgMemberApprove(), admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    membership = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id, OrganizationMembership.user_id == user_id,
    ))).scalar_one_or_none()
    if not membership:
        raise HTTPException(404, "Membership not found")
    membership.approved = True
    for role in data.roles:
        db.add(OrganizationMembershipRole(membership_id=membership.id, role=role))
    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    # Only their FIRST approved org needs to flip is_active — a user already
    # active via another org just needed this specific membership approved.
    if user and not user.is_active:
        user.is_active = True
        user.email_verified = True
        user.verification_token = None
    await db.commit()

    if user:
        login_link = helpers._app_url("/")
        helpers.send_email(
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


@router.post("/orgs/{org_id}/members/{user_id}/reject", status_code=204)
async def reject_org_member(org_id: int, user_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Rejects (deletes) a pending membership request. Unlike the legacy
    single-tenant /admin/users/{id}/reject, this does NOT delete the user
    account itself — they may hold approved memberships in other orgs, or be
    free to request a different org."""
    membership = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id, OrganizationMembership.user_id == user_id,
    ))).scalar_one_or_none()
    if not membership:
        raise HTTPException(404, "Membership not found")
    if membership.approved:
        raise HTTPException(400, "Cannot reject an already-approved membership — remove them from the org instead")
    await db.delete(membership)
    await db.commit()


class OrgMemberRoleUpdate(BaseModel):
    role: Literal["member", "admin"]


@router.patch("/orgs/{org_id}/members/{user_id}/role", response_model=OrgMemberOut)
async def update_org_member_role(
    org_id: int, user_id: int, data: OrgMemberRoleUpdate,
    admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db),
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
    membership = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id,
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.approved == True,
    ))).scalar_one_or_none()
    if not membership:
        raise HTTPException(404, "Membership not found")
    membership.role = data.role
    await db.commit()

    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    rows = await _org_member_out_rows([(membership, user)], db)
    return rows[0]


class OrgMemberExtraRolesUpdate(BaseModel):
    roles: list[str] = []   # replaces the membership's full extra-role set

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, v):
        bad = [r for r in v if r not in ORG_EXTRA_ROLES]
        if bad:
            raise ValueError(f"Invalid role(s): {', '.join(bad)} — only {', '.join(ORG_EXTRA_ROLES)} may be set here")
        return v


@router.put("/orgs/{org_id}/members/{user_id}/extra-roles", response_model=OrgMemberOut)
async def update_org_member_extra_roles(
    org_id: int, user_id: int, data: OrgMemberExtraRolesUpdate,
    admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db),
):
    """Set which of the three participant roles (Net Control Op, Tactical
    Operator, Broadcaster — issue follow-up) an already-approved member
    holds at the org level — a full replace, same shape as the sharing
    endpoints' full-replace convention. Separate from update_org_member_role
    above since these are additive/multi-valued (unlike admin, which stays a
    single base role) — see OrganizationMembership's docstring."""
    membership = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id,
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.approved == True,
    ))).scalar_one_or_none()
    if not membership:
        raise HTTPException(404, "Membership not found")
    await db.execute(delete(OrganizationMembershipRole).where(OrganizationMembershipRole.membership_id == membership.id))
    for role in set(data.roles):
        db.add(OrganizationMembershipRole(membership_id=membership.id, role=role))
    await db.commit()

    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    rows = await _org_member_out_rows([(membership, user)], db)
    return rows[0]


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


@router.post("/orgs/{org_id}/users", response_model=AdminUserOut, status_code=201)
async def create_org_user(org_id: int, data: OrgUserCreate, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Admin-seeds an operator account directly — for bringing existing
    operators onto the org without a self-registration/approval round trip
    (issue #1 follow-up). Always targets THIS org (org_id, the caller's own
    current one in practice); a super admin wanting to target a different
    org, or create a brand new one on the spot, uses POST /admin/users
    instead (issue follow-up) — same underlying _create_invited_user."""
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    user = await helpers._create_invited_user(
        data.callsign, data.name, data.email,
        (data.gmrs_callsign or "").strip().upper() or None,
        org, data.role, db,
    )
    return AdminUserOut(
        **UserOut.model_validate(user).model_dump(),
        org_name=org.name,
        org_website_url=org.website_url,
    )


@router.get("/stats")
async def get_stats(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Quick stats for the sidebar dashboard panel."""
    from models import NetShare

    # Net IDs the user can see (owned + shared), scoped to their current org (issue #1)
    owned_ids = (await db.execute(
        select(Net.id).filter(Net.owner_id == current_user.id, Net.org_id == current_user.current_org_id)
    )).scalars().all()
    shared_ids = (await db.execute(
        select(NetShare.net_id)
        .join(Net, Net.id == NetShare.net_id)
        .filter(NetShare.user_id == current_user.id, Net.org_id == current_user.current_org_id)
    )).scalars().all()
    all_net_ids = list(set(list(owned_ids) + list(shared_ids)))

    total_nets = len(all_net_ids)

    active_sessions = 0
    checkins_today = 0
    if all_net_ids:
        active_sessions = (await db.execute(
            select(func.count(NetSession.id))
            .filter(NetSession.net_id.in_(all_net_ids), NetSession.ended_at.is_(None))
        )).scalar() or 0
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        checkins_today = (await db.execute(
            select(func.count(Checkin.id))
            .join(NetSession, Checkin.session_id == NetSession.id)
            .filter(NetSession.net_id.in_(all_net_ids), Checkin.checked_in_at >= today_start)
        )).scalar() or 0

    gmrs_row = (await db.execute(select(SystemSetting).filter(SystemSetting.key == "gmrs_db_synced_at"))).scalar_one_or_none()

    return {
        "total_nets": total_nets,
        "active_sessions": active_sessions,
        "checkins_today": checkins_today,
        "gmrs_synced_at": gmrs_row.value[:10] if gmrs_row and gmrs_row.value else None,
    }


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

BRANDING_KEYS = ("org_name", "tagline", "website_url")


class BrandingOut(BaseModel):
    org_name: Optional[str] = None
    tagline: Optional[str] = None
    website_url: Optional[str] = None
    has_logo: bool = False


class BrandingUpdate(BaseModel):
    org_name: Optional[str] = None
    tagline: Optional[str] = None
    website_url: Optional[str] = None


def _logo_file() -> Optional[pathlib.Path]:
    """Return the logo file path if one exists (any image extension)."""
    for ext in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        p = helpers.UPLOADS_DIR / f"logo.{ext}"
        if p.exists():
            return p
    return None


@router.get("/branding", response_model=BrandingOut)
async def get_branding(db: AsyncSession = Depends(get_db)):
    """Public endpoint — returns current branding settings."""
    return BrandingOut(
        org_name=await helpers._get_setting("org_name", db),
        tagline=await helpers._get_setting("tagline", db),
        website_url=await helpers._get_setting("website_url", db),
        has_logo=_logo_file() is not None,
    )


@router.get("/logo")
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


@router.put("/admin/branding", response_model=BrandingOut)
async def update_branding(
    data: BrandingUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin only — update branding text settings."""
    if not current_user.is_admin:
        raise HTTPException(403, "Admin only")
    await helpers._set_setting("org_name", data.org_name or None, db)
    await helpers._set_setting("tagline", data.tagline or None, db)
    await helpers._set_setting("website_url", data.website_url or None, db)
    await db.commit()
    return BrandingOut(
        org_name=await helpers._get_setting("org_name", db),
        tagline=await helpers._get_setting("tagline", db),
        website_url=await helpers._get_setting("website_url", db),
        has_logo=_logo_file() is not None,
    )


@router.post("/admin/branding/logo", status_code=204)
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
    for old in helpers.UPLOADS_DIR.glob("logo.*"):
        old.unlink(missing_ok=True)
    dest = helpers.UPLOADS_DIR / f"logo.{ext}"
    dest.write_bytes(await file.read())


@router.delete("/admin/branding/logo", status_code=204)
def delete_logo(current_user: User = Depends(get_current_user)):
    """Admin only — remove the current logo."""
    if not current_user.is_admin:
        raise HTTPException(403, "Admin only")
    for old in helpers.UPLOADS_DIR.glob("logo.*"):
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Announcements — instance-wide welcome messages (super admin only)
#
# Distinct from an org's own banner_message (set via PATCH /orgs/{id} above,
# by an org admin, shown only to that org's members): these two apply
# across every org on the instance, set only by a super admin.
# ---------------------------------------------------------------------------

ANNOUNCEMENT_KEYS = ("login_message", "welcome_popup_message")


class AnnouncementsOut(BaseModel):
    login_message: Optional[str] = None          # shown on the login screen, before signing in
    welcome_popup_message: Optional[str] = None   # shown as a dismissible popup right after logging in


class AnnouncementsUpdate(BaseModel):
    login_message: Optional[str] = None
    welcome_popup_message: Optional[str] = None


@router.get("/system/announcements", response_model=AnnouncementsOut)
async def get_announcements(db: AsyncSession = Depends(get_db)):
    """Public endpoint — the login screen needs this before the user has
    signed in at all, so it can't be gated on auth. Also reused post-login
    to check the welcome-popup message (fine either way — nothing here is
    sensitive)."""
    return AnnouncementsOut(
        login_message=await helpers._get_setting("login_message", db),
        welcome_popup_message=await helpers._get_setting("welcome_popup_message", db),
    )


@router.put("/admin/announcements", response_model=AnnouncementsOut)
async def update_announcements(
    data: AnnouncementsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Super admin only — instance-wide, not scoped to the caller's current org."""
    if not current_user.is_admin:
        raise HTTPException(403, "Admin only")
    await helpers._set_setting("login_message", (data.login_message or "").strip() or None, db)
    await helpers._set_setting("welcome_popup_message", (data.welcome_popup_message or "").strip() or None, db)
    await db.commit()
    return AnnouncementsOut(
        login_message=await helpers._get_setting("login_message", db),
        welcome_popup_message=await helpers._get_setting("welcome_popup_message", db),
    )
