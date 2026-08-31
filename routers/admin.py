"""
Admin routes + Net Repository self-service API key requests.
"""

import os
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

import net_repository
from database import engine, get_db
from models import EnabledLanguage, Organization, OrganizationMembership, User
from routers import helpers
from routers.deps import get_current_user
from routers.helpers import _delete_orphaned_orgs
from routers.schemas import AdminUserOut, UserOut
from routers.translation import _translation_configured, run_enable_language_job

router = APIRouter()

GITHUB_URL = os.getenv("GITHUB_URL", "https://github.com/LadyHwesta/netcontrol-online")
ADMIN_CONTACT_EMAIL = os.getenv("ADMIN_CONTACT_EMAIL", "")  # shown in approval/rejection emails as human contact


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


@router.get("/admin/users", response_model=list[AdminUserOut])
async def admin_list_users(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """List all users (active, pending, and inactive), with each user's
    current org name/website attached (issue #1 follow-up) — lets a super
    admin verify a pending registration, especially one founding a brand new
    org, without a separate lookup."""
    rows = (await db.execute(
        select(User, Organization)
        .outerjoin(Organization, Organization.id == User.current_org_id)
        .order_by(User.created_at.desc())
    )).all()
    return [
        AdminUserOut(
            **UserOut.model_validate(u).model_dump(),
            org_name=org.name if org else None,
            org_website_url=org.website_url if org else None,
        )
        for u, org in rows
    ]


@router.patch("/admin/users/{user_id}/approve", response_model=UserOut)
async def admin_approve_user(user_id: int, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Activate a pending user account and notify them by email.

    Also marks the account email-verified: an admin manually approving someone
    is a stronger trust signal than the automated link-click, and it's the only
    way to unblock a user whose verification email never arrived or whose link
    can't work because APP_BASE_URL isn't configured on this instance.
    """
    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = True
    user.email_verified = True
    user.verification_token = None
    # Super-admin approval is a global escape hatch (issue #1) — clear every
    # pending org membership too, not just the account-level gate, since a
    # super admin isn't scoped to any one org's approval queue.
    await db.execute(update(OrganizationMembership).where(
        OrganizationMembership.user_id == user.id, OrganizationMembership.approved == False,
    ).values(approved=True))
    await db.commit()
    await db.refresh(user)

    login_link = helpers._app_url("/")
    helpers.send_email(
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


@router.post("/admin/users/{user_id}/reject", status_code=204)
async def admin_reject_user(user_id: int, body: RejectUserBody = RejectUserBody(), admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Send a rejection email then permanently delete the pending account."""
    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
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

    helpers.send_email(
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

    org_ids = set((await db.execute(select(OrganizationMembership.org_id).filter(OrganizationMembership.user_id == user.id))).scalars().all())
    await db.delete(user)
    await db.flush()
    await _delete_orphaned_orgs(org_ids, db)
    await db.commit()


@router.patch("/admin/users/{user_id}/deactivate", response_model=UserOut)
async def admin_deactivate_user(user_id: int, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Deactivate a user account (they can no longer log in)."""
    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "Cannot deactivate your own account")
    user.is_active = False
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/admin/users/{user_id}/make-admin", response_model=UserOut)
async def admin_make_admin(user_id: int, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Grant admin privileges to a user."""
    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    user.is_admin = True
    user.is_active = True   # admins must be active
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/admin/users/{user_id}", status_code=204)
async def admin_delete_user(user_id: int, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Permanently delete a user account."""
    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "Cannot delete your own account")
    org_ids = set((await db.execute(select(OrganizationMembership.org_id).filter(OrganizationMembership.user_id == user.id))).scalars().all())
    await db.delete(user)
    await db.flush()
    await _delete_orphaned_orgs(org_ids, db)
    await db.commit()


@router.patch("/admin/users/{user_id}/notify", response_model=UserOut)
async def admin_toggle_notify(user_id: int, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Toggle email notification opt-in for new registrations (admin accounts only)."""
    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    if not user.is_admin:
        raise HTTPException(400, "Only admins can receive registration notifications")
    user.notify_new_registrations = not user.notify_new_registrations
    await db.commit()
    await db.refresh(user)
    return user


class OrgReassignUser(BaseModel):
    org_id: int
    role: Literal["member", "admin"] = "member"


@router.patch("/admin/users/{user_id}/org", response_model=AdminUserOut)
async def admin_reassign_user_org(user_id: int, data: OrgReassignUser, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Move a user wholesale into a different organization — removes every
    other org membership they hold and switches current_org_id to the
    target, so a deployment that started single-tenant can be split into
    per-region orgs after the fact (issue #1 follow-up). Super-admin only,
    since it crosses tenant boundaries by definition; existing nets they own
    are NOT moved along with them — reassign those separately below."""
    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    org = (await db.execute(select(Organization).filter(Organization.id == data.org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")

    old_org_ids = set((await db.execute(select(OrganizationMembership.org_id).filter(OrganizationMembership.user_id == user.id))).scalars().all())
    await db.execute(delete(OrganizationMembership).where(OrganizationMembership.user_id == user.id))
    db.add(OrganizationMembership(org_id=org.id, user_id=user.id, role=data.role, approved=True))
    user.current_org_id = org.id
    user.is_active = True
    await db.flush()
    await _delete_orphaned_orgs(old_org_ids - {org.id}, db)
    await db.commit()
    await db.refresh(user)

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


@router.post("/admin/users/{user_id}/orgs", response_model=AddMembershipResult, status_code=201)
async def admin_add_user_to_org(user_id: int, data: OrgAddMembership, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Add a user to an ADDITIONAL organization without touching their
    existing memberships — distinct from the wholesale move above (issue #1
    follow-up). For an operator who legitimately needs to work across more
    than one org (e.g. a regional coordinator), not for splitting a
    single-tenant deployment apart. If the user already has a pending
    membership in the target org (e.g. a self-service /orgs/join request),
    this approves it in place rather than erroring."""
    user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    org = (await db.execute(select(Organization).filter(Organization.id == data.org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")

    membership = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org.id, OrganizationMembership.user_id == user.id,
    ))).scalar_one_or_none()
    if membership and membership.approved:
        raise HTTPException(400, "User is already a member of this organization")
    if membership:
        membership.role = data.role
        membership.approved = True
    else:
        db.add(OrganizationMembership(org_id=org.id, user_id=user.id, role=data.role, approved=True))
    user.is_active = True
    await db.commit()

    return AddMembershipResult(user_id=user.id, org_id=org.id, org_name=org.name, role=data.role)


class OrgReassignNet(BaseModel):
    org_id: int


class NetReassignResult(BaseModel):
    id: int
    org_id: int
    org_name: str
    owner_not_member: bool


@router.patch("/admin/nets/{net_id}/org", response_model=NetReassignResult)
async def admin_reassign_net_org(net_id: int, data: OrgReassignNet, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Move a net into a different organization (issue #1 follow-up). Does
    not touch ownership or sharing — if the net's owner isn't a member of
    the target org, owner_not_member comes back True so the admin panel can
    flag it (the owner will need to be added to the target org, or ownership
    transferred, before they can manage it themselves again); super admins
    can always reach it regardless."""
    from models import Net

    net = (await db.execute(select(Net).filter(Net.id == net_id))).scalar_one_or_none()
    if not net:
        raise HTTPException(404, "Net not found")
    org = (await db.execute(select(Organization).filter(Organization.id == data.org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")

    net.org_id = org.id
    await db.commit()

    owner_is_member = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org.id,
        OrganizationMembership.user_id == net.owner_id,
        OrganizationMembership.approved == True,
    ))).scalar_one_or_none() is not None

    return NetReassignResult(id=net.id, org_id=org.id, org_name=org.name, owner_not_member=not owner_is_member)


@router.get("/admin/email-status")
def admin_email_status(admin: User = Depends(require_admin)):
    """Return whether SMTP is configured (no credentials exposed)."""
    return {
        "configured": helpers._smtp_configured(),
        "from_address": helpers.SMTP_FROM or helpers.SMTP_USER or None,
        "host": helpers.SMTP_HOST or None,
    }


# ---------------------------------------------------------------------------
# Database stats (native, lightweight Postgres visibility for Admin)
#
# Not a pghero replacement -- pghero is a Ruby/Rack tool, which would mean
# running a second language runtime as its own service for a single-process,
# club-scale deployment (see TECH_DEBT.md). This covers the handful of stats
# people actually check day to day -- connection counts, table sizes, and
# (if the pg_stat_statements extension is installed) slow queries -- with
# zero new dependencies or services. A no-op on SQLite deployments.
# ---------------------------------------------------------------------------

class DbConnectionStats(BaseModel):
    total: int
    active: int
    idle: int


class DbTableStat(BaseModel):
    name: str
    size: str
    row_estimate: int


class DbSlowQuery(BaseModel):
    query: str
    calls: int
    mean_time_ms: float
    total_time_ms: float


class DbStatsOut(BaseModel):
    dialect: str                     # "postgresql" | "sqlite" | whatever SQLAlchemy names it
    database_size: Optional[str] = None
    connections: Optional[DbConnectionStats] = None
    tables: list[DbTableStat] = []
    pg_stat_statements_available: bool = False
    slow_queries: list[DbSlowQuery] = []
    slow_queries_note: Optional[str] = None   # e.g. extension not installed, or a version-mismatch error


@router.get("/admin/db-stats", response_model=DbStatsOut)
async def admin_db_stats(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Instance-wide DB diagnostics -- super-admin only, same as
    Branding/Email/Net Repository/Reassign, since this exposes internals
    across every org, not just the caller's own."""
    dialect = engine.dialect.name
    if dialect != "postgresql":
        return DbStatsOut(dialect=dialect)

    database_size = (await db.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))"))).scalar()

    conn_row = (await db.execute(text(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE state = 'active') AS active, "
        "count(*) FILTER (WHERE state = 'idle') AS idle "
        "FROM pg_stat_activity WHERE datname = current_database()"
    ))).one()
    connections = DbConnectionStats(total=conn_row.total, active=conn_row.active, idle=conn_row.idle)

    table_rows = (await db.execute(text(
        "SELECT relname AS name, pg_size_pretty(pg_total_relation_size(relid)) AS size, "
        "n_live_tup AS row_estimate FROM pg_stat_user_tables "
        "ORDER BY pg_total_relation_size(relid) DESC LIMIT 15"
    ))).all()
    tables = [DbTableStat(name=r.name, size=r.size, row_estimate=r.row_estimate or 0) for r in table_rows]

    ext_available = bool((await db.execute(text(
        "SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'"
    ))).scalar())

    slow_queries: list[DbSlowQuery] = []
    slow_queries_note = None
    if ext_available:
        try:
            # mean_exec_time/total_exec_time are the PG13+ column names
            # (renamed from mean_time/total_time) -- this app's own
            # deployment target is recent enough that an older PG isn't
            # worth branching for; if the columns don't exist, the
            # exception below surfaces a clear message instead of a 500.
            slow_rows = (await db.execute(text(
                "SELECT query, calls, "
                "round(mean_exec_time::numeric, 2) AS mean_time_ms, "
                "round(total_exec_time::numeric, 2) AS total_time_ms "
                "FROM pg_stat_statements "
                "WHERE query NOT ILIKE '%pg_stat_statements%' "
                "ORDER BY mean_exec_time DESC LIMIT 15"
            ))).all()
            slow_queries = [
                DbSlowQuery(
                    query=(r.query or "")[:300],
                    calls=r.calls,
                    mean_time_ms=float(r.mean_time_ms or 0),
                    total_time_ms=float(r.total_time_ms or 0),
                )
                for r in slow_rows
            ]
        except Exception as exc:
            await db.rollback()  # the failed statement leaves the transaction unusable otherwise
            # SQLAlchemy's asyncpg adapter wraps the real driver error in its
            # own Error class, whose __str__ embeds "<class '...'>: message"
            # for log-friendliness -- __cause__ is the original asyncpg
            # exception underneath, whose str() is just the plain message.
            orig = getattr(exc, "orig", None)
            detail = str(getattr(orig, "__cause__", None) or orig or exc)
            slow_queries_note = f"Could not read pg_stat_statements: {detail}"
    else:
        slow_queries_note = (
            "pg_stat_statements extension is not installed -- run "
            "`CREATE EXTENSION pg_stat_statements;` as a superuser (and add it to "
            "shared_preload_libraries) to enable slow-query tracking."
        )

    return DbStatsOut(
        dialect=dialect,
        database_size=database_size,
        connections=connections,
        tables=tables,
        pg_stat_statements_available=ext_available,
        slow_queries=slow_queries,
        slow_queries_note=slow_queries_note,
    )


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


@router.get("/admin/net-repository/status", response_model=NetRepoStatusOut)
async def admin_net_repository_status(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Current Net Repository integration status. Never exposes the raw API
    key or claim token — those are internal to net_repository.py."""
    return NetRepoStatusOut(
        url_configured=bool(net_repository.NET_REPOSITORY_URL),
        has_key=bool(await net_repository.get_api_key(db)),
        key_source=await net_repository.get_key_source(db),
        request_status=await net_repository.get_request_status(db),
    )


@router.post("/admin/net-repository/request-key", response_model=NetRepoActionResult)
async def admin_request_net_repository_key(
    data: NetRepoKeyRequestIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Request a Net Repository API key on this instance's behalf via its
    self-service POST /keys/request. Enters that instance's admin review
    queue; check status with admin_check_net_repository_key below."""
    result = await net_repository.request_api_key(
        data.name, data.contact_callsign, data.instance_url, data.request_notes, db,
    )
    return NetRepoActionResult(**result)


@router.post("/admin/net-repository/check-status", response_model=NetRepoActionResult)
async def admin_check_net_repository_key(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Poll Net Repository for the outcome of a pending key request. Once
    approved, this stores the issued key so pushes start working immediately
    — no restart needed."""
    result = await net_repository.check_key_request_status(db)
    return NetRepoActionResult(**result)


@router.delete("/admin/net-repository/key", status_code=204)
async def admin_clear_net_repository_key(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Forget the self-service key and any in-flight request, to start over.
    Does not affect NET_REPOSITORY_API_KEY if set via .env."""
    await net_repository.clear_stored_key(db)


# ---------------------------------------------------------------------------
# UI translation (argos-translate, opt-in TRANSLATION_ENABLED) — instance-
# wide like Branding/Email/DB Stats/Net Repository above, not org-scoped.
# ---------------------------------------------------------------------------

class LanguageAdminOut(BaseModel):
    code: str
    display_name: str
    model_status: str
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}


class LanguageCreate(BaseModel):
    code: str
    display_name: str


@router.get("/admin/languages", response_model=list[LanguageAdminOut])
async def admin_list_languages(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(EnabledLanguage).order_by(EnabledLanguage.display_name))).scalars().all()


@router.post("/admin/languages", response_model=LanguageAdminOut, status_code=201)
async def admin_enable_language(
    data: LanguageCreate,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Creates the EnabledLanguage row, then kicks off a background job that
    installs the argos-translate model and bulk pre-translates the known
    UI strings -- both real blocking/network work, so this endpoint returns
    immediately with model_status='pending' rather than making the admin's
    browser wait on a model download."""
    if not _translation_configured():
        raise HTTPException(503, "Translation isn't configured on this server (set TRANSLATION_ENABLED=true)")
    code = data.code.strip().lower()
    existing = (await db.execute(select(EnabledLanguage).filter(EnabledLanguage.code == code))).scalar_one_or_none()
    if existing:
        raise HTTPException(400, f"{code} is already enabled")

    lang = EnabledLanguage(code=code, display_name=data.display_name.strip(), model_status="pending")
    db.add(lang)
    await db.commit()
    await db.refresh(lang)
    background_tasks.add_task(run_enable_language_job, code, lang.display_name)
    return lang


@router.delete("/admin/languages/{code}", status_code=204)
async def admin_disable_language(code: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Removes the language from the switcher/auto-detect list. Cached
    translations in translation_cache are left in place -- cheap to keep,
    instant to re-enable later."""
    lang = (await db.execute(select(EnabledLanguage).filter(EnabledLanguage.code == code))).scalar_one_or_none()
    if lang:
        await db.delete(lang)
        await db.commit()
