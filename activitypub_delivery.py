"""
NetControl Online — ActivityPub actor/activity building + delivery
(issue follow-up)

One ActivityPub actor per Organization (@org-slug@host, type Service),
posting a Create/Note to its followers when a net session starts and
again when it ends (routers/sessions.py's start_session()/end_session()
call announce_session_start()/announce_session_end() below). See the
plan for this feature for the full protocol rationale -- summarized here:

  - Followers are grouped by their own actor document's
    endpoints.sharedInbox (falling back to inbox) so one broadcast is one
    HTTP request per remote SERVER, not per follower.
  - Delivery is single-attempt, best-effort, and always backgrounded by
    the caller (FastAPI BackgroundTasks) -- never blocks session
    start/end, never retried. A dead follower inbox is dropped silently
    (logged, not surfaced) rather than building a retry queue.
  - HTTP Signatures are Cavage-draft (activitypub_signing.py), which is
    what Mastodon and most of the fediverse actually require today.

Async: record_and_get_targets() is the only async half (DB work,
matching routers/sessions.py's request handler) -- everything past that
(deliver_to_targets, deliver_accept, fetch_remote_actor) is plain sync
httpx, matching net_repository.py's existing convention for external
calls in this codebase, and runs inside a FastAPI background task so it
never blocks a request.
"""

import html
import json
import logging
import mimetypes
import os
import uuid
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

import activitypub_signing
from models import ActivityPubFollower, ActivityPubPost

_log = logging.getLogger("ham_net_tracker.activitypub")

# Duplicated from routers/helpers.py's APP_BASE_URL rather than imported --
# this is a flat top-level module (alongside net_repository.py, send_reminders.py)
# that deliberately doesn't import routers/, matching this codebase's existing
# precedent (see send_reminders.py's own VAPID_* duplication) for standalone
# modules that need the same env-var-backed setting.
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")


def activitypub_configured() -> bool:
    return bool(APP_BASE_URL)


# ---------------------------------------------------------------------------
# URL / handle construction -- single source of truth, since any
# inconsistency between how an actor/object id is built in one place vs.
# another breaks Mastodon's actor-matching (see the plan's gotchas).
# ---------------------------------------------------------------------------

def _host() -> str:
    return urlparse(APP_BASE_URL).netloc if APP_BASE_URL else ""


def _org_base(org) -> str:
    return f"{APP_BASE_URL}/ap/orgs/{org.slug}"


def build_actor_id(org) -> str:
    return f"{_org_base(org)}/actor"


def build_handle(org) -> str:
    return f"{org.slug}@{_host()}"


# ---------------------------------------------------------------------------
# Document builders -- pure functions, hand-built dicts (not Pydantic --
# AP's JSON-LD shapes are loosely typed and vary by activity type).
# ---------------------------------------------------------------------------

def build_actor_document(org) -> dict:
    base = _org_base(org)
    actor_id = f"{base}/actor"
    doc = {
        "@context": ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"],
        "id": actor_id,
        "type": "Service",
        "preferredUsername": org.slug,
        "name": org.name,
        "summary": f"Net Control announcements for {org.name}, via NetControl Online.",
        "url": f"{APP_BASE_URL}/live/{org.slug}",
        "inbox": f"{base}/inbox",
        "outbox": f"{base}/outbox",
        "followers": f"{base}/followers",
        "following": f"{base}/following",
        "publicKey": {
            "id": f"{actor_id}#main-key",
            "owner": actor_id,
            "publicKeyPem": org.activitypub_public_key,
        },
        "manuallyApprovesFollowers": False,
        "discoverable": True,
    }
    if org.created_at:
        doc["published"] = org.created_at.isoformat()

    # Reuse the org's existing logo (per-org branding) as the actor's avatar,
    # if one's uploaded -- omitted entirely otherwise. Lazy import to keep
    # this module import-independent of routers/, matching net_repository.py.
    from routers.helpers import _org_logo_file
    logo_path = _org_logo_file(org.id)
    if logo_path is not None:
        mime = mimetypes.guess_type(str(logo_path))[0] or "image/png"
        doc["icon"] = {"type": "Image", "mediaType": mime, "url": f"{APP_BASE_URL}/orgs/{org.id}/logo"}

    return doc


def build_note_object(org, post) -> dict:
    return {
        "id": f"{APP_BASE_URL}/ap/objects/notes/{post.uuid}",
        "type": "Note",
        "attributedTo": build_actor_id(org),
        "content": post.content_html,
        "url": f"{APP_BASE_URL}/live/{org.slug}",
        "published": post.published_at.isoformat(),
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [f"{_org_base(org)}/followers"],
        "summary": None,
        "sensitive": False,
        "attachment": [],
        "tag": [],
    }


def build_create_activity(org, post) -> dict:
    note = build_note_object(org, post)
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{APP_BASE_URL}/ap/activities/create/{post.uuid}",
        "type": "Create",
        "actor": build_actor_id(org),
        "published": note["published"],
        "to": note["to"],
        "cc": note["cc"],
        "object": note,
    }


# ---------------------------------------------------------------------------
# Post content -- the actual announcement text for the two occasions.
# ---------------------------------------------------------------------------

def _live_link(org) -> str:
    url = f"{APP_BASE_URL}/live/{org.slug}"
    return f'<a href="{url}">{url}</a>'


def start_content_html(net, org) -> str:
    freq = f" on {html.escape(net.frequency)}" if net.frequency else ""
    return f"<p>📡 {html.escape(net.name)} is starting now{freq}. Check in: {_live_link(org)}</p>"


def _duration_minutes(session) -> Optional[int]:
    if session.started_at and session.ended_at:
        return int((session.ended_at - session.started_at).total_seconds() / 60)
    return None


def end_content_html(net, org, session, checkin_count: int) -> str:
    duration = _duration_minutes(session)
    duration_txt = ""
    if duration is not None:
        duration_txt = f" over {duration} minute{'' if duration == 1 else 's'}"
    plural = "" if checkin_count == 1 else "s"
    return (
        f"<p>📡 {html.escape(net.name)} has ended. {checkin_count} check-in{plural}"
        f"{duration_txt}. Thanks everyone! {_live_link(org)}</p>"
    )


# ---------------------------------------------------------------------------
# Recording a post + resolving delivery targets -- the fast, DB-only half,
# called synchronously from the request handler.
# ---------------------------------------------------------------------------

async def record_and_get_targets(org, net, session, kind: str, content_html: str, db) -> Optional[tuple]:
    """Never raises; returns None if Fediverse posting isn't applicable
    here for any reason (not configured, not enabled, session not opted
    in, or a backfilled/offline entry -- announcing "starting now"/"just
    ended" for something that happened days ago would be misleading).
    Otherwise creates+commits the ActivityPubPost row and returns
    (post, dest_urls) for the caller to hand to BackgroundTasks."""
    if not activitypub_configured():
        return None
    if not org or not org.activitypub_enabled:
        return None
    if not net.activitypub_announce:
        return None
    if session.is_offline:
        return None
    if not org.activitypub_private_key or not org.activitypub_public_key:
        return None

    post = ActivityPubPost(
        org_id=org.id, net_id=net.id, session_id=session.id,
        uuid=str(uuid.uuid4()), kind=kind, content_html=content_html,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)

    followers = (await db.execute(
        select(ActivityPubFollower).filter(ActivityPubFollower.org_id == org.id)
    )).scalars().all()
    dest_urls = list({(f.shared_inbox_url or f.inbox_url) for f in followers})
    return post, dest_urls


async def announce_session_start(net, org, session, db) -> Optional[tuple]:
    return await record_and_get_targets(org, net, session, "start", start_content_html(net, org), db)


async def announce_session_end(net, org, session, checkin_count: int, db) -> Optional[tuple]:
    return await record_and_get_targets(org, net, session, "end", end_content_html(net, org, session, checkin_count), db)


# ---------------------------------------------------------------------------
# Outbound delivery -- sync, meant to run inside a background task.
# ---------------------------------------------------------------------------

def _deliver_signed(org, body: bytes, url: str) -> bool:
    try:
        parsed = urlparse(url)
        actor_id = build_actor_id(org)
        key_id = f"{actor_id}#main-key"
        date = activitypub_signing.http_date()
        digest, sig_header = activitypub_signing.sign_request(
            org.activitypub_private_key, "POST", parsed.path or "/", parsed.netloc, date, body, key_id,
        )
        resp = httpx.post(
            url,
            content=body,
            headers={
                "Host": parsed.netloc,
                "Date": date,
                "Digest": digest,
                "Content-Type": "application/activity+json",
                "Accept": "application/activity+json",
                "Signature": sig_header,
            },
            timeout=10,
        )
        if resp.status_code >= 300:
            _log.warning("ActivityPub delivery to %s failed: HTTP %s", url, resp.status_code)
            return False
        return True
    except Exception as exc:
        _log.warning("ActivityPub delivery to %s failed: %s", url, exc)
        return False


def deliver_to_targets(org, post, dest_urls: list) -> None:
    """Single attempt per destination, never raises -- see this module's
    docstring for why there's no retry queue."""
    if not dest_urls:
        return
    body = json.dumps(build_create_activity(org, post)).encode()
    for url in dest_urls:
        _deliver_signed(org, body, url)


def deliver_accept(org, follow_activity: dict, target_inbox_url: str) -> None:
    """Delivers a signed Accept back to a new follower -- per the
    protocol research, this must happen promptly or the follow shows
    "pending" forever on the Mastodon side, so the caller (the inbox
    handler) backgrounds this immediately after storing the follower."""
    accept = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{APP_BASE_URL}/ap/activities/accept/{uuid.uuid4()}",
        "type": "Accept",
        "actor": build_actor_id(org),
        "object": follow_activity,
    }
    _deliver_signed(org, json.dumps(accept).encode(), target_inbox_url)


def fetch_remote_actor(actor_id: str) -> Optional[dict]:
    """Fetches a remote actor's document -- used both to verify an inbound
    request's signature (publicKeyPem by keyId, #fragment stripped) and to
    resolve a new follower's inbox/sharedInbox at Follow time. No caching:
    fetched fresh every time, acceptable at this app's scale (a handful of
    org actors, not thousands) and means a follower's key rotation or
    inbox move is always picked up rather than served stale."""
    url = actor_id.split("#")[0]
    try:
        resp = httpx.get(url, headers={"Accept": "application/activity+json"}, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        _log.warning("Failed to fetch remote actor %s: %s", url, exc)
        return None
