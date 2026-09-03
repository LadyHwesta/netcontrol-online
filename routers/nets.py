"""
Net routes + Net share management endpoints.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import net_repository
from database import get_db
from models import Net, NetShare, NetShareRole, OrganizationMembership, OrganizationMembershipRole, User
from routers.deps import get_current_user
from routers.helpers import NET_EXTRA_ROLES, _get_editable_net, _get_net_for_user, _get_owned_net, _net_to_out, _org_role_set
from routers.schemas import NetOut

router = APIRouter()


class UserPublicOut(BaseModel):
    id: int
    callsign: str
    gmrs_callsign: Optional[str] = None
    name: str
    # Role revamp (issue follow-up): this user's canonical org role set --
    # lets the sharing/schedule pickers show which of Net Control Op/
    # Tactical Operator/Broadcaster are actually offerable for them, instead
    # of silently dropping a selection the org admin never approved (issue
    # follow-up -- see update_net_shares' own docstring on that gate).
    roles: list[str] = []

    model_config = {"from_attributes": True}


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
    activitypub_announce: bool = False   # post to the org's Fediverse actor on session start/end (issue follow-up)
    aprs_map_enabled: bool = False   # shows an APRS station map on the public live page (issue #22)
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


class NetShareUpdate(BaseModel):
    share_with_all: bool = False
    can_edit_all: bool = False        # edit rights for the "shared with all" grant, only meaningful when share_with_all=True
    user_ids: list[int] = []          # specific user IDs to share with (ignored when share_with_all=True)
    editor_user_ids: list[int] = []   # subset of user_ids to also grant edit rights (net_control_op)
    # Role revamp (issue follow-up): subsets of user_ids to also grant the two
    # minimal self-service roles -- each only actually applied to a user whose
    # own org membership already holds that role (silently dropped otherwise,
    # so stale UI state never grants more than the org admin approved).
    tactical_operator_user_ids: list[int] = []
    broadcaster_user_ids: list[int] = []
    # Same idea for the "shared with all" grant, only meaningful when
    # share_with_all=True -- still gated per-user against org membership below.
    tactical_operator_all: bool = False
    broadcaster_all: bool = False


class NetOwnerUpdate(BaseModel):
    owner_id: int


@router.get("/users", response_model=list[UserPublicOut])
async def list_users(net_id: Optional[int] = None, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
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
        org_id = (await _get_editable_net(net_id, current_user, db)).org_id

    rows = (
        (await db.execute(select(User, OrganizationMembership).join(OrganizationMembership, OrganizationMembership.user_id == User.id).filter(
            User.is_active == True,
            User.id != current_user.id,
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.approved == True,
        ).order_by(User.callsign))).all()
    )
    membership_ids = [m.id for _u, m in rows]
    extra_by_membership: dict[int, set[str]] = {}
    if membership_ids:
        extra_rows = (await db.execute(select(OrganizationMembershipRole).filter(
            OrganizationMembershipRole.membership_id.in_(membership_ids)
        ))).scalars().all()
        for r in extra_rows:
            extra_by_membership.setdefault(r.membership_id, set()).add(r.role)

    out = []
    for u, m in rows:
        roles = ({"admin"} if m.role == "admin" else set()) | extra_by_membership.get(m.id, set())
        out.append(UserPublicOut(id=u.id, callsign=u.callsign, gmrs_callsign=u.gmrs_callsign, name=u.name, roles=sorted(roles)))
    return out


@router.get("/nets", response_model=list[NetOut])
async def list_nets(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current_user.is_admin:
        # Super admins see every net, across every org
        nets = (await db.execute(select(Net).order_by(Net.name))).scalars().all()
    else:
        # Owned nets + nets shared with this user + nets shared with all,
        # scoped to the org the user is currently working as (issue #1)
        shared_net_ids = (
            select(NetShare.net_id)
            .filter(or_(NetShare.user_id == current_user.id, NetShare.user_id == None))
            .scalar_subquery()
        )
        nets = (
            (await db.execute(select(Net).filter(
                Net.org_id == current_user.current_org_id,
                or_(Net.owner_id == current_user.id, Net.id.in_(shared_net_ids)),
            ).order_by(Net.name))).scalars().all()
        )
    return [await _net_to_out(n, current_user, db) for n in nets]


@router.post("/nets", response_model=NetOut, status_code=201)
async def create_net(data: NetCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
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
        activitypub_announce=data.activitypub_announce,
        aprs_map_enabled=data.aprs_map_enabled if net_type == "ham" else False,
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
    await db.commit()
    await db.refresh(net)
    await net_repository.push_net(net, db)
    return await _net_to_out(net, current_user, db)


@router.get("/nets/{net_id}", response_model=NetOut)
async def get_net(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_net_for_user(net_id, current_user, db)
    return await _net_to_out(net, current_user, db)


@router.put("/nets/{net_id}", response_model=NetOut)
async def update_net(net_id: int, data: NetCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_editable_net(net_id, current_user, db)
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
    net.activitypub_announce = data.activitypub_announce
    net.aprs_map_enabled = data.aprs_map_enabled if net_type == "ham" else False
    net.band = data.band or None
    net.mode = data.mode or None
    net.ctcss_tone = data.ctcss_tone or None
    net.region = data.region or None
    net.state = data.state or None
    net.website = data.website or None
    await db.commit()
    await db.refresh(net)
    await net_repository.push_net(net, db)
    return await _net_to_out(net, current_user, db)


@router.patch("/nets/{net_id}/owner", response_model=NetOut)
async def transfer_net_owner(net_id: int, data: NetOwnerUpdate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Reassign a net's owner — previously the only way to change who
    controls a net was deleting and recreating it (issue follow-up).
    Available to the net's current owner (hand off to someone else), an
    admin of the net's own org, or a super admin. The new owner must
    already be an approved member of the net's org — unlike Move a Net
    (which only warns about this, since the admin may fix it in either
    order), this is a deliberate single assignment so it's enforced
    outright rather than left as a warning."""
    net = (await db.execute(select(Net).filter(Net.id == net_id))).scalar_one_or_none()
    if not net:
        raise HTTPException(404, "Net not found")
    if not current_user.is_admin:
        if net.org_id != current_user.current_org_id:
            raise HTTPException(404, "Net not found")
        is_owner = net.owner_id == current_user.id
        is_org_admin = (await db.execute(select(OrganizationMembership).filter(
            OrganizationMembership.org_id == net.org_id,
            OrganizationMembership.user_id == current_user.id,
            OrganizationMembership.role == "admin",
            OrganizationMembership.approved == True,
        ))).scalar_one_or_none() is not None
        if not (is_owner or is_org_admin):
            raise HTTPException(403, "Not your net")

    new_owner = (await db.execute(select(User).filter(User.id == data.owner_id))).scalar_one_or_none()
    if not new_owner:
        raise HTTPException(404, "User not found")
    is_member = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == net.org_id,
        OrganizationMembership.user_id == new_owner.id,
        OrganizationMembership.approved == True,
    ))).scalar_one_or_none() is not None
    if not is_member:
        raise HTTPException(400, f"{new_owner.callsign} is not a member of this net's organization")

    net.owner_id = new_owner.id
    await db.commit()
    await db.refresh(net)
    return await _net_to_out(net, current_user, db)


@router.delete("/nets/{net_id}", status_code=204)
async def delete_net(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    net = await _get_owned_net(net_id, current_user, db)
    await db.delete(net)
    await db.commit()


@router.get("/nets/{net_id}/shares")
async def get_net_shares(net_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Return the current sharing config for a net (owner or admin only)."""
    await _get_owned_net(net_id, current_user, db)
    shares = (await db.execute(select(NetShare).filter(NetShare.net_id == net_id))).scalars().all()
    share_ids = [s.id for s in shares]
    extra_rows = (
        (await db.execute(select(NetShareRole).filter(NetShareRole.net_share_id.in_(share_ids)))).scalars().all()
        if share_ids else []
    )
    extra_by_share = {}
    for r in extra_rows:
        extra_by_share.setdefault(r.net_share_id, set()).add(r.role)
    all_share = next((s for s in shares if s.user_id is None), None)
    all_extra = extra_by_share.get(all_share.id, set()) if all_share else set()
    return {
        "share_with_all": all_share is not None,
        "can_edit_all": bool(all_share and all_share.can_edit),
        "tactical_operator_all": "tactical_operator" in all_extra,
        "broadcaster_all": "broadcaster" in all_extra,
        "user_ids": [s.user_id for s in shares if s.user_id is not None],
        "editor_user_ids": [s.user_id for s in shares if s.user_id is not None and s.can_edit],
        "tactical_operator_user_ids": [s.user_id for s in shares if s.user_id is not None and "tactical_operator" in extra_by_share.get(s.id, set())],
        "broadcaster_user_ids": [s.user_id for s in shares if s.user_id is not None and "broadcaster" in extra_by_share.get(s.id, set())],
    }


@router.put("/nets/{net_id}/shares", status_code=204)
async def update_net_shares(net_id: int, data: NetShareUpdate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Replace the sharing config for a net (owner or admin only). Role
    revamp (issue follow-up): a per-user tactical_operator/broadcaster grant
    is only actually written when that user's own org membership already
    holds the role -- an org admin decides who's *eligible* to be offered a
    role (via approval), sharing decides which of their eligible roles apply
    to THIS net. Silently dropped rather than a 400 so stale client-side
    state (e.g. a role revoked at the org level after the share form was
    opened) never grants more than intended."""
    net = await _get_owned_net(net_id, current_user, db)
    # Wipe existing shares for this net. NetShareRole's ON DELETE CASCADE
    # isn't actually enforced by SQLite for a bulk Core-level DELETE like
    # this one (no PRAGMA foreign_keys=ON) -- relying on it silently orphans
    # role rows in dev/test and, worse, a later INSERT reusing a freed rowid
    # can then collide with one. Postgres enforces it either way, so the
    # explicit delete below is a no-op there; being explicit is correct on
    # both.
    old_share_ids = (await db.execute(select(NetShare.id).filter(NetShare.net_id == net_id))).scalars().all()
    if old_share_ids:
        await db.execute(delete(NetShareRole).where(NetShareRole.net_share_id.in_(old_share_ids)))
    await db.execute(delete(NetShare).where(NetShare.net_id == net_id))
    await db.flush()

    async def _add_share(user_id, can_edit, extra_roles):
        share = NetShare(net_id=net_id, user_id=user_id, can_edit=can_edit)
        db.add(share)
        await db.flush()
        for role in extra_roles:
            db.add(NetShareRole(net_share_id=share.id, role=role))

    if data.share_with_all:
        extra = set()
        if data.tactical_operator_all:
            extra.add("tactical_operator")
        if data.broadcaster_all:
            extra.add("broadcaster")
        await _add_share(None, data.can_edit_all, extra)
    else:
        editor_ids = set(data.editor_user_ids)
        tactical_ids = set(data.tactical_operator_user_ids)
        broadcaster_ids = set(data.broadcaster_user_ids)
        for uid in data.user_ids:
            extra = set()
            if uid in tactical_ids or uid in broadcaster_ids:
                held = await _org_role_set(net.org_id, uid, db)
                if uid in tactical_ids and "tactical_operator" in held:
                    extra.add("tactical_operator")
                if uid in broadcaster_ids and "broadcaster" in held:
                    extra.add("broadcaster")
            await _add_share(uid, uid in editor_ids, extra)
    await db.commit()
