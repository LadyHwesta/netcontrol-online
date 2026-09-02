"""
Fediverse participation via ActivityPub (issue follow-up) — the public,
unauthenticated protocol endpoints for each organization's own actor
(@org-slug@host): WebFinger discovery, the actor document itself,
followers/following/outbox stubs, dereferenceable post objects, and the
inbox (Follow/Undo/Delete). Org-admin enable/disable + status lives in
routers/orgs.py instead, alongside the aprs-key pattern it mirrors — this
file is only the fediverse-facing side, all of which must be reachable
with no auth of any kind (a remote Mastodon server fetching our actor
document can't log in to this app).

The actual "post an announcement" call sites are
routers/sessions.py's start_session()/end_session(); see
activitypub_delivery.py for the document builders and delivery mechanics.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub_delivery
import activitypub_signing
from database import get_db
from models import ActivityPubFollower, ActivityPubPost, Organization

router = APIRouter()
_log = logging.getLogger("ham_net_tracker.activitypub")

AP_MEDIA_TYPE = "application/activity+json"


async def _get_enabled_org(slug: str, db: AsyncSession) -> Organization:
    org = (await db.execute(select(Organization).filter(Organization.slug == slug))).scalar_one_or_none()
    if not org or not org.activitypub_enabled or not activitypub_delivery.activitypub_configured():
        raise HTTPException(404, "No such Fediverse actor")
    return org


# ---------------------------------------------------------------------------
# WebFinger
# ---------------------------------------------------------------------------

@router.get("/.well-known/webfinger")
async def webfinger(resource: str = "", db: AsyncSession = Depends(get_db)):
    # resource is `acct:slug@host` -- host must match this instance's own
    # host (no cross-domain delegation support), and only the local-part
    # (slug) actually needs matching against an org.
    if not resource.startswith("acct:") or "@" not in resource:
        raise HTTPException(400, "resource must be an acct: URI")
    local_part = resource[len("acct:"):].split("@", 1)[0]
    org = (await db.execute(select(Organization).filter(Organization.slug == local_part))).scalar_one_or_none()
    if not org or not org.activitypub_enabled or not activitypub_delivery.activitypub_configured():
        raise HTTPException(404, "No such account")

    actor_id = activitypub_delivery.build_actor_id(org)
    doc = {
        "subject": f"acct:{activitypub_delivery.build_handle(org)}",
        "aliases": [actor_id],
        "links": [
            {"rel": "self", "type": AP_MEDIA_TYPE, "href": actor_id},
            {"rel": "http://webfinger.net/rel/profile-page", "type": "text/html",
             "href": f"{activitypub_delivery.APP_BASE_URL}/live/{org.slug}"},
        ],
    }
    return JSONResponse(doc, media_type="application/jrd+json")


# ---------------------------------------------------------------------------
# Actor document + its collections
# ---------------------------------------------------------------------------

@router.get("/ap/orgs/{slug}/actor")
async def get_actor(slug: str, db: AsyncSession = Depends(get_db)):
    org = await _get_enabled_org(slug, db)
    return JSONResponse(activitypub_delivery.build_actor_document(org), media_type=AP_MEDIA_TYPE)


@router.get("/ap/orgs/{slug}/followers")
async def get_followers(slug: str, db: AsyncSession = Depends(get_db)):
    org = await _get_enabled_org(slug, db)
    count = (await db.execute(
        select(func.count(ActivityPubFollower.id)).filter(ActivityPubFollower.org_id == org.id)
    )).scalar()
    doc = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{activitypub_delivery._org_base(org)}/followers",
        "type": "OrderedCollection",
        "totalItems": count,
    }
    return JSONResponse(doc, media_type=AP_MEDIA_TYPE)


@router.get("/ap/orgs/{slug}/following")
async def get_following(slug: str, db: AsyncSession = Depends(get_db)):
    org = await _get_enabled_org(slug, db)
    doc = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{activitypub_delivery._org_base(org)}/following",
        "type": "OrderedCollection",
        "totalItems": 0,
        "orderedItems": [],
    }
    return JSONResponse(doc, media_type=AP_MEDIA_TYPE)


@router.get("/ap/orgs/{slug}/outbox")
async def get_outbox(slug: str, db: AsyncSession = Depends(get_db)):
    org = await _get_enabled_org(slug, db)
    posts = (await db.execute(
        select(ActivityPubPost).filter(ActivityPubPost.org_id == org.id)
        .order_by(ActivityPubPost.published_at.desc()).limit(20)
    )).scalars().all()
    items = [activitypub_delivery.build_create_activity(org, p) for p in posts]
    doc = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{activitypub_delivery._org_base(org)}/outbox",
        "type": "OrderedCollection",
        "totalItems": len(items),
        "orderedItems": items,
    }
    return JSONResponse(doc, media_type=AP_MEDIA_TYPE)


# ---------------------------------------------------------------------------
# Dereferenceable post objects -- every Note/Create id must resolve back to
# the same content it was delivered with (Mastodon fetches these back to
# canonicalize/verify, and when a user clicks through or boosts).
# ---------------------------------------------------------------------------

@router.get("/ap/objects/notes/{post_uuid}")
async def get_note(post_uuid: str, db: AsyncSession = Depends(get_db)):
    post = (await db.execute(select(ActivityPubPost).filter(ActivityPubPost.uuid == post_uuid))).scalar_one_or_none()
    if not post:
        raise HTTPException(404, "Not found")
    org = (await db.execute(select(Organization).filter(Organization.id == post.org_id))).scalar_one_or_none()
    if not org or not org.activitypub_enabled:
        raise HTTPException(404, "Not found")
    return JSONResponse(activitypub_delivery.build_note_object(org, post), media_type=AP_MEDIA_TYPE)


@router.get("/ap/activities/create/{post_uuid}")
async def get_create_activity(post_uuid: str, db: AsyncSession = Depends(get_db)):
    post = (await db.execute(select(ActivityPubPost).filter(ActivityPubPost.uuid == post_uuid))).scalar_one_or_none()
    if not post:
        raise HTTPException(404, "Not found")
    org = (await db.execute(select(Organization).filter(Organization.id == post.org_id))).scalar_one_or_none()
    if not org or not org.activitypub_enabled:
        raise HTTPException(404, "Not found")
    return JSONResponse(activitypub_delivery.build_create_activity(org, post), media_type=AP_MEDIA_TYPE)


# ---------------------------------------------------------------------------
# Inbox -- Follow / Undo(Follow) / Delete. Everything else is ignored.
# Signature verification is mandatory (see activitypub_delivery.py's
# fetch_remote_actor + activitypub_signing.verify_signature) -- without it,
# anyone could forge a Follow/Undo/Delete and corrupt the follower list.
# ---------------------------------------------------------------------------

def _actor_field(value) -> str:
    """The `actor`/`object` field on an AP activity is sometimes a bare
    string id, sometimes an embedded object with its own `id` -- real-world
    senders (and Undo/Delete especially) use both shapes."""
    if isinstance(value, dict):
        return value.get("id", "")
    return value or ""


@router.post("/ap/orgs/{slug}/inbox", status_code=202)
async def post_inbox(slug: str, request: Request, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    org = (await db.execute(select(Organization).filter(Organization.slug == slug))).scalar_one_or_none()
    if not org or not org.activitypub_enabled or not activitypub_delivery.activitypub_configured():
        raise HTTPException(404, "No such Fediverse actor")

    raw_body = await request.body()
    try:
        activity = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    sig_header = request.headers.get("signature")
    if not sig_header:
        raise HTTPException(401, "Missing signature")
    key_id = activitypub_signing._parse_signature_header(sig_header).get("keyId", "")
    if not key_id:
        raise HTTPException(401, "Invalid signature")
    sender_actor = activitypub_delivery.fetch_remote_actor(key_id)
    public_key_pem = ((sender_actor or {}).get("publicKey") or {}).get("publicKeyPem")
    if not public_key_pem:
        raise HTTPException(401, "Could not resolve signer's public key")

    digest_header_value = request.headers.get("digest", "")
    if digest_header_value and digest_header_value != activitypub_signing.digest_header(raw_body):
        raise HTTPException(401, "Digest does not match body")

    headers = {k.lower(): v for k, v in request.headers.items()}
    path = request.url.path
    if not activitypub_signing.verify_signature(headers, "POST", path, public_key_pem):
        raise HTTPException(401, "Signature verification failed")

    actor_uri = _actor_field(activity.get("actor"))
    if not actor_uri or actor_uri != key_id.split("#")[0]:
        raise HTTPException(401, "Signer does not match activity actor")

    activity_type = activity.get("type")

    if activity_type == "Follow":
        inbox_url = (sender_actor or {}).get("inbox") or actor_uri
        shared_inbox_url = ((sender_actor or {}).get("endpoints") or {}).get("sharedInbox")
        existing = (await db.execute(select(ActivityPubFollower).filter(
            ActivityPubFollower.org_id == org.id, ActivityPubFollower.actor_id == actor_uri,
        ))).scalar_one_or_none()
        if existing:
            existing.inbox_url = inbox_url
            existing.shared_inbox_url = shared_inbox_url
        else:
            db.add(ActivityPubFollower(
                org_id=org.id, actor_id=actor_uri, inbox_url=inbox_url, shared_inbox_url=shared_inbox_url,
            ))
        await db.commit()
        background_tasks.add_task(activitypub_delivery.deliver_accept, org, activity, inbox_url)

    elif activity_type == "Undo":
        inner = activity.get("object")
        if isinstance(inner, dict) and inner.get("type") == "Follow":
            unfollow_actor = _actor_field(inner.get("actor"))
            await db.execute(delete(ActivityPubFollower).filter(
                ActivityPubFollower.org_id == org.id, ActivityPubFollower.actor_id == unfollow_actor,
            ))
            await db.commit()

    elif activity_type == "Delete":
        deleted_actor = _actor_field(activity.get("object"))
        if deleted_actor:
            await db.execute(delete(ActivityPubFollower).filter(
                ActivityPubFollower.org_id == org.id, ActivityPubFollower.actor_id == deleted_actor,
            ))
            await db.commit()

    else:
        # Like, Announce, Create (replies), etc. -- silently ignored, this
        # actor is output-only and doesn't support replies/interactions.
        _log.info("Ignored inbound %r activity from %s for org %s", activity_type, actor_uri, org.slug)

    return None
