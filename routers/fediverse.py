"""
Fediverse interaction client (issue follow-up) -- the AUTHENTICATED
client actions built on top of what routers/activitypub.py's inbox
persists: list replies/Likes received on the org's own posts, reply/Like
back, and compose an ad-hoc post. Deliberately a separate file from
routers/activitypub.py (which stays only the public/unauthenticated
protocol surface) and from routers/orgs.py's org-admin-only enable/
hashtags pair (which stays exactly that -- account-level settings, not
day-to-day interaction).

Access here is broader than org-admin: a super admin, an org admin of
this org, OR an approved member holding the fediverse_operator role (see
models.py's OrganizationMembershipRole docstring) -- require_fediverse_
access below, mirroring routers/orgs.py's own require_org_admin but with
that one extra allowance. fediverse_operator is org-wide with no
NetShare/per-net counterpart (one Fediverse actor per org, not per net),
so this dependency is the entirety of its enforcement -- there's no
further per-net gate the way tactical_operator/broadcaster get.
"""

import uuid
from typing import Optional

from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub_delivery
from database import get_db
from models import (
    ActivityPubInteraction, ActivityPubPost, Organization, OrganizationMembership, OrganizationMembershipRole, User, utcnow,
)
from routers.deps import get_current_user

router = APIRouter()


async def require_fediverse_access(org_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> User:
    """Org admin, an approved member holding fediverse_operator in this
    org, or a super admin (User.is_admin bypasses org scoping everywhere,
    same as require_org_admin)."""
    if current_user.is_admin:
        return current_user
    membership = (await db.execute(select(OrganizationMembership).filter(
        OrganizationMembership.org_id == org_id,
        OrganizationMembership.user_id == current_user.id,
        OrganizationMembership.approved == True,
    ))).scalar_one_or_none()
    if membership and membership.role == "admin":
        return current_user
    if membership:
        has_role = (await db.execute(select(OrganizationMembershipRole.id).filter(
            OrganizationMembershipRole.membership_id == membership.id,
            OrganizationMembershipRole.role == "fediverse_operator",
        ))).scalar_one_or_none()
        if has_role:
            return current_user
    raise HTTPException(403, "Fediverse access required")


async def _get_org_or_404(org_id: int, db: AsyncSession) -> Organization:
    org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not found")
    return org


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class InteractionOut(BaseModel):
    id: int
    kind: str   # 'reply' | 'like'
    remote_actor_handle: Optional[str] = None
    remote_actor_name: Optional[str] = None
    content_html: Optional[str] = None
    received_at: datetime
    liked_at: Optional[datetime] = None
    post_id: int
    post_kind: str
    post_content_html: str


class PostOut(BaseModel):
    id: int
    kind: str   # 'start' | 'end' | 'reply' | 'manual'
    content_html: str
    published_at: datetime

    model_config = {"from_attributes": True}


class ReplyIn(BaseModel):
    content: str


class ComposeIn(BaseModel):
    content: str


# ---------------------------------------------------------------------------
# Interactions -- replies/Likes received on the org's own posts
# ---------------------------------------------------------------------------

@router.get("/orgs/{org_id}/fediverse/interactions", response_model=list[InteractionOut])
async def list_interactions(org_id: int, limit: int = Query(100, ge=1, le=500), user: User = Depends(require_fediverse_access), db: AsyncSession = Depends(get_db)):
    await _get_org_or_404(org_id, db)
    rows = (await db.execute(
        select(ActivityPubInteraction, ActivityPubPost)
        .join(ActivityPubPost, ActivityPubPost.id == ActivityPubInteraction.post_id)
        .filter(ActivityPubInteraction.org_id == org_id)
        .order_by(ActivityPubInteraction.received_at.desc())
        .limit(limit)
    )).all()
    return [
        InteractionOut(
            id=i.id, kind=i.kind, remote_actor_handle=i.remote_actor_handle, remote_actor_name=i.remote_actor_name,
            content_html=i.content_html, received_at=i.received_at, liked_at=i.liked_at,
            post_id=p.id, post_kind=p.kind, post_content_html=p.content_html,
        )
        for i, p in rows
    ]


@router.post("/orgs/{org_id}/fediverse/interactions/{interaction_id}/reply", response_model=PostOut, status_code=201)
async def reply_to_interaction(
    org_id: int, interaction_id: int, data: ReplyIn, background_tasks: BackgroundTasks,
    user: User = Depends(require_fediverse_access), db: AsyncSession = Depends(get_db),
):
    org = await _get_org_or_404(org_id, db)
    if not org.activitypub_enabled or not org.activitypub_private_key:
        raise HTTPException(400, "Fediverse participation is not enabled for this organization")
    interaction = (await db.execute(select(ActivityPubInteraction).filter(
        ActivityPubInteraction.id == interaction_id, ActivityPubInteraction.org_id == org_id,
    ))).scalar_one_or_none()
    if not interaction:
        raise HTTPException(404, "Interaction not found")

    content_html = activitypub_delivery.render_user_content_html(data.content)
    if not content_html:
        raise HTTPException(400, "Reply text is required")

    post = ActivityPubPost(
        org_id=org.id, uuid=str(uuid.uuid4()), kind="reply", content_html=content_html,
        in_reply_to=interaction.remote_object_id, in_reply_to_actor=interaction.remote_actor_id,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)

    if interaction.remote_inbox_url:
        background_tasks.add_task(activitypub_delivery.deliver_to_targets, org, post, [interaction.remote_inbox_url])
    return PostOut.model_validate(post)


@router.post("/orgs/{org_id}/fediverse/interactions/{interaction_id}/like", status_code=204)
async def like_interaction(
    org_id: int, interaction_id: int, background_tasks: BackgroundTasks,
    user: User = Depends(require_fediverse_access), db: AsyncSession = Depends(get_db),
):
    org = await _get_org_or_404(org_id, db)
    interaction = (await db.execute(select(ActivityPubInteraction).filter(
        ActivityPubInteraction.id == interaction_id, ActivityPubInteraction.org_id == org_id,
    ))).scalar_one_or_none()
    if not interaction:
        raise HTTPException(404, "Interaction not found")
    if interaction.liked_at:
        return None   # already liked -- no-op, not an error
    if not org.activitypub_enabled or not org.activitypub_private_key:
        raise HTTPException(400, "Fediverse participation is not enabled for this organization")

    if interaction.remote_inbox_url:
        background_tasks.add_task(activitypub_delivery.deliver_like, org, interaction.remote_object_id, interaction.remote_inbox_url)
    interaction.liked_at = utcnow()
    await db.commit()
    return None


# ---------------------------------------------------------------------------
# Posts -- the org's own outgoing history + composing an ad-hoc one
# ---------------------------------------------------------------------------

@router.get("/orgs/{org_id}/fediverse/posts", response_model=list[PostOut])
async def list_posts(org_id: int, limit: int = Query(50, ge=1, le=200), user: User = Depends(require_fediverse_access), db: AsyncSession = Depends(get_db)):
    await _get_org_or_404(org_id, db)
    posts = (await db.execute(
        select(ActivityPubPost).filter(ActivityPubPost.org_id == org_id).order_by(ActivityPubPost.published_at.desc()).limit(limit)
    )).scalars().all()
    return [PostOut.model_validate(p) for p in posts]


@router.post("/orgs/{org_id}/fediverse/posts", response_model=PostOut, status_code=201)
async def compose_post(
    org_id: int, data: ComposeIn, background_tasks: BackgroundTasks,
    user: User = Depends(require_fediverse_access), db: AsyncSession = Depends(get_db),
):
    org = await _get_org_or_404(org_id, db)
    extra_tags = activitypub_delivery._parse_hashtag_field(org.activitypub_hashtags)
    content_html = activitypub_delivery.render_user_content_html(data.content, extra_tags)
    if not content_html:
        raise HTTPException(400, "Post text is required")

    result = await activitypub_delivery.record_manual_post(org, content_html, db)
    if not result:
        raise HTTPException(400, "Fediverse participation is not enabled for this organization")
    post, dest_urls = result
    background_tasks.add_task(activitypub_delivery.deliver_to_targets, org, post, dest_urls)
    return PostOut.model_validate(post)
