"""
Organizations (issue #1 — multi-tenancy) + Branding.
"""

import pathlib
import re
import secrets
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Checkin, EnabledLanguage, Net, NetSession, Organization, OrganizationMembership, OrgEnabledLanguage, SystemSetting, User
from routers import helpers
from routers.deps import get_current_user
from routers.helpers import _get_or_create_org, _net_to_out
from routers.schemas import AdminUserOut, NetOut, OrganizationOut, UserOut
from routers.translation import _translation_configured, run_enable_language_job

router = APIRouter()


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


class MyOrgOut(OrganizationOut):
    """Like OrganizationOut, plus the caller's own role in that org — lets the
    frontend show the org-admin panel only where the user actually has it."""
    role: str


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
async def list_orgs(db: AsyncSession = Depends(get_db)):
    """Organizations that actually have someone who could approve a join
    request — name+slug only — powers the "join an existing organization"
    picker at registration. No auth required: same trust level as
    callsign/name being visible in the registration form itself, and an
    org's existence isn't sensitive. Excludes an org with no approved admin
    (e.g. its founder was rejected/deleted before anyone else joined) —
    await _delete_orphaned_orgs() cleans those up outright, but this filter is a
    second line of defense against ever listing a dead-end org (issue #1
    follow-up)."""
    return (
        (await db.execute(select(Organization).join(OrganizationMembership, OrganizationMembership.org_id == Organization.id).filter(OrganizationMembership.role == "admin", OrganizationMembership.approved == True).distinct().order_by(Organization.name))).scalars().all()
    )


@router.get("/orgs/mine", response_model=list[MyOrgOut])
async def list_my_orgs(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The current user's own approved organizations, with their role in each
    — powers the org switcher and the org-admin panel visibility check."""
    rows = (await db.execute(
        select(Organization, OrganizationMembership.role)
        .join(OrganizationMembership, OrganizationMembership.org_id == Organization.id)
        .filter(OrganizationMembership.user_id == current_user.id, OrganizationMembership.approved == True)
        .order_by(Organization.name)
    )).all()
    return [MyOrgOut(id=org.id, name=org.name, slug=org.slug, website_url=org.website_url, banner_message=org.banner_message, role=role) for org, role in rows]


class OrganizationUpdate(BaseModel):
    name: str
    website_url: Optional[str] = None
    banner_message: Optional[str] = None


@router.patch("/orgs/{org_id}", response_model=OrganizationOut)
async def update_org(org_id: int, data: OrganizationUpdate, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Rename an org / fix its website / set its banner message — previously
    there was no way to do any of this at all once created (issue #1
    follow-up; an org's name is its own property, independent of the
    instance-wide Branding settings, so changing Branding doesn't
    retroactively rename any org). Slug is intentionally not editable here
    — it's baked into public /directory/<slug> and /live/<slug> URLs."""
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
    await db.commit()
    await db.refresh(org)
    return org


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


@router.post("/orgs/join", response_model=OrganizationOut, status_code=201)
async def join_org(data: OrgJoinRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Already-logged-in self-service: request to join an additional org (or
    create a new one), same join-or-create semantics as registration. Does
    not touch is_active — the caller is already active via an existing org.
    Unlike registration, a newly founded org here is ALWAYS pending (never
    self-approved) — the caller being active elsewhere doesn't make them a
    trustworthy org founder; a super admin still needs to sign off via the
    existing /admin/users/{id}/approve (issue #1 follow-up)."""
    org, org_created = await _get_or_create_org(data.org_slug, data.org_name, data.org_website_url, db)
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


@router.get("/orgs/{org_id}/pending-members", response_model=list[OrgMemberOut])
async def list_pending_org_members(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.org_id == org_id, OrganizationMembership.approved == False)
        .order_by(OrganizationMembership.created_at.desc())
    )).all()
    return [
        OrgMemberOut(
            user_id=u.id, callsign=u.callsign, name=u.name, email=u.email,
            role=m.role, approved=m.approved, requested_at=m.created_at,
        )
        for m, u in rows
    ]


@router.get("/orgs/{org_id}/members", response_model=list[OrgMemberOut])
async def list_org_members(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.org_id == org_id, OrganizationMembership.approved == True)
        .order_by(User.callsign)
    )).all()
    return [
        OrgMemberOut(
            user_id=u.id, callsign=u.callsign, name=u.name, email=u.email,
            role=m.role, approved=m.approved, requested_at=m.created_at,
        )
        for m, u in rows
    ]


@router.get("/orgs/{org_id}/nets", response_model=list[NetOut])
async def list_org_nets(org_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Every net in this org, regardless of ownership or sharing — lets an
    org admin see (and reassign ownership of) every net in their org, not
    just ones they personally own or are shared on (issue follow-up).
    list_nets doesn't do this for non-super-admins: org-admin role alone
    was never a substitute for owning or being shared on a net."""
    nets = (await db.execute(select(Net).filter(Net.org_id == org_id).order_by(Net.name))).scalars().all()
    return [await _net_to_out(n, admin, db) for n in nets]


@router.patch("/orgs/{org_id}/members/{user_id}/approve", status_code=204)
async def approve_org_member(org_id: int, user_id: int, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    membership = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id, OrganizationMembership.user_id == user_id,
    ))).scalar_one_or_none()
    if not membership:
        raise HTTPException(404, "Membership not found")
    membership.approved = True
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


@router.post("/orgs/{org_id}/users", response_model=AdminUserOut, status_code=201)
async def create_org_user(org_id: int, data: OrgUserCreate, admin: User = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Admin-seeds an operator account directly — for bringing existing
    operators onto the org without a self-registration/approval round trip
    (issue #1 follow-up). Auto-approved (the admin creating it IS the
    approval): is_active is already True, but hashed_password is an unusable
    random placeholder, so login is impossible until the operator follows
    the emailed link to set their own password. Requires SMTP to be
    configured — otherwise the account would be created with no way to ever
    become usable."""
    import hashlib

    from routers.auth import hash_password

    if not helpers._smtp_configured():
        raise HTTPException(400, "Email must be configured (Admin → Email) before creating operator accounts this way — the invite link is sent by email.")
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    if (await db.execute(select(User).filter(User.callsign == data.callsign))).scalar_one_or_none():
        raise HTTPException(400, "Callsign already registered")
    if (await db.execute(select(User).filter(User.email == data.email))).scalar_one_or_none():
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
    await db.commit()
    await db.refresh(user)

    db.add(OrganizationMembership(org_id=org.id, user_id=user.id, role=data.role, approved=True))
    await db.commit()

    set_link = helpers._app_url(f"/?setpw={raw_token}")
    helpers.send_email(
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
