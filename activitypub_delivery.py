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

Async: record_and_get_targets()/record_manual_post() are the only async
halves (DB work, matching the callers' own request handlers) --
everything past that (deliver_to_targets, deliver_accept, deliver_like,
fetch_remote_actor) is plain sync httpx, matching net_repository.py's
existing convention for external calls in this codebase, and runs inside
a FastAPI background task so it never blocks a request.

Issue follow-up: a small Fediverse interaction client (routers/
fediverse.py) closes the loop on the one-way broadcast above -- an org
admin or fediverse_operator can see replies/Likes a post receives
(persisted as ActivityPubInteraction rows by routers/activitypub.py's
post_inbox, models.py has both), reply/Like back (deliver_to_targets/
deliver_like, single-target rather than broadcast), and compose an
ad-hoc post (record_manual_post). See render_user_content_html() for how
an operator's own typed text becomes content_html.
"""

import html
import json
import logging
import mimetypes
import os
import re
import uuid
from html.parser import HTMLParser
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
    note = {
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
        "tag": _hashtag_tag_objects(post.content_html),
    }
    # Fediverse interaction client (issue follow-up): only set for an
    # outgoing reply (kind='reply') -- NULL for every other kind, so this
    # is a no-op for the existing start/end/manual shapes. Mastodon
    # convention for a public reply: cc the person being replied to
    # alongside our own followers, so it reaches them even if they don't
    # follow us.
    in_reply_to = getattr(post, "in_reply_to", None)
    if in_reply_to:
        note["inReplyTo"] = in_reply_to
        in_reply_to_actor = getattr(post, "in_reply_to_actor", None)
        if in_reply_to_actor:
            note["cc"] = [in_reply_to_actor, f"{_org_base(org)}/followers"]
    return note


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
# Hashtags -- a stock amateur-radio-centric set (net_type/Activation-based)
# plus whatever an org admin (Organization.activitypub_hashtags) and/or the
# net's own owner (Net.activitypub_hashtags) have added on top. The
# fediverse leans heavily on hashtags for discovery (hashtag timelines/
# follows on Mastodon and similar), so posting with none at all would leave
# these announcements far less discoverable than a hand-posted one.
#
# Rendered into content_html as real hashtag anchors (Mastodon's own
# `class="mention hashtag" rel="tag"` shape) so they read as clickable tags
# rather than plain text, and re-extracted from that same persisted HTML at
# build_note_object() time to populate the Note's `tag` array -- no
# separate column, keeping ActivityPubPost's "content_html is the only
# thing that has to survive" shape (see its own docstring) intact; the tag
# metadata is always exactly what the post itself displays.
# ---------------------------------------------------------------------------

STOCK_HASHTAGS_HAM = ("HamRadio", "AmateurRadio")
STOCK_HASHTAGS_GMRS = ("GMRS",)
STOCK_HASHTAG_ACTIVATION = "EmComm"   # net.is_ares, either net type

MAX_HASHTAGS = 20   # generous ceiling against pathological input, not a UX limit
HASHTAG_WORD_RE = re.compile(r"^\w{1,50}$", re.UNICODE)


def _parse_hashtag_field(raw: Optional[str]) -> list:
    """Splits an admin/owner-typed hashtag field (space and/or comma
    separated, '#' optional) into clean tag names -- silently drops any
    token that isn't a single bare word (no spaces/punctuation) rather than
    rejecting the whole field, since this is free text with no save-time
    validation."""
    if not raw:
        return []
    tags = []
    for token in re.split(r"[\s,]+", raw.strip()):
        word = token.lstrip("#")
        if word and HASHTAG_WORD_RE.match(word):
            tags.append(word)
    return tags


def _hashtags_for_net(net, org) -> list:
    """Stock tags first (net_type, then Activation & Incident Response if
    on), then the org's own, then this net's own -- in that order, deduped
    case-insensitively (first-seen casing wins, so an org's preferred
    capitalization beats a net owner's later duplicate)."""
    tags = list(STOCK_HASHTAGS_GMRS if net.net_type == "gmrs" else STOCK_HASHTAGS_HAM)
    if net.is_ares:
        tags.append(STOCK_HASHTAG_ACTIVATION)
    tags += _parse_hashtag_field(getattr(org, "activitypub_hashtags", None))
    tags += _parse_hashtag_field(getattr(net, "activitypub_hashtags", None))

    seen = set()
    deduped = []
    for tag in tags:
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(tag)
    return deduped[:MAX_HASHTAGS]


def _hashtag_anchor(tag: str) -> str:
    """The single Mastodon-style hashtag anchor template -- used both by
    _hashtags_html() below (a trailing block of tags appended to an
    automated post) and by render_user_content_html()'s inline auto-
    linkify (issue follow-up, Fediverse interaction client) for `#word`
    tokens an operator typed directly. Every hashtag anywhere in any
    post's content_html goes through this one template, so
    _hashtag_tag_objects()'s extraction below stays correct regardless of
    which path rendered it."""
    escaped = html.escape(tag)
    return f'<a href="{APP_BASE_URL}/tags/{escaped}" class="mention hashtag" rel="tag">#<span>{escaped}</span></a>'


def _hashtags_html(tags: list) -> str:
    if not tags:
        return ""
    return f"<p>{' '.join(_hashtag_anchor(tag) for tag in tags)}</p>"


_HASHTAG_ANCHOR_RE = re.compile(r'rel="tag">#<span>([^<]+)</span></a>')
_INLINE_HASHTAG_RE = re.compile(r'#(\w{1,50})')


def _hashtag_tag_objects(content_html: str) -> list:
    """Rebuilds the Note's `tag` array (Mastodon Hashtag objects, used for
    hashtag-timeline/search federation) from the hashtag anchors already
    embedded in content_html by _hashtags_html() above -- see this
    section's own docstring for why that's the single source of truth
    rather than a stored list."""
    return [
        {"type": "Hashtag", "href": f"{APP_BASE_URL}/tags/{name}", "name": f"#{name}"}
        for name in _HASHTAG_ANCHOR_RE.findall(content_html)
    ]


# ---------------------------------------------------------------------------
# Post content -- the actual announcement text for the two occasions.
# ---------------------------------------------------------------------------

def _live_link(org) -> str:
    url = f"{APP_BASE_URL}/live/{org.slug}"
    return f'<a href="{url}">{url}</a>'


def start_content_html(net, org) -> str:
    freq = f" on {html.escape(net.frequency)}" if net.frequency else ""
    return (
        f"<p>📡 {html.escape(net.name)} is starting now{freq}. Check in: {_live_link(org)}</p>"
        + _hashtags_html(_hashtags_for_net(net, org))
    )


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
        + _hashtags_html(_hashtags_for_net(net, org))
    )


# ---------------------------------------------------------------------------
# User-composed content (issue follow-up, Fediverse interaction client) --
# an operator's own typed text for a reply or an ad-hoc post, as opposed
# to the two fixed announcement templates above.
# ---------------------------------------------------------------------------

def render_user_content_html(raw_text: str, extra_hashtags: Optional[list] = None) -> str:
    """Escapes raw_text, wraps blank-line-separated paragraphs in <p>, and
    auto-linkifies any inline `#word` the operator typed using the exact
    same anchor markup _hashtags_html() produces (see _hashtag_anchor's
    own docstring for why that matters) -- then appends _hashtags_html()
    for any of extra_hashtags not already typed inline, deduped
    case-insensitively same as _hashtags_for_net(). Returns "" for blank
    input (callers should reject an empty post before calling this)."""
    text = (raw_text or "").strip()
    if not text:
        return ""
    escaped = html.escape(text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", escaped) if p.strip()]
    body = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
    inline_tags = {m.group(1).lower() for m in _INLINE_HASHTAG_RE.finditer(body)}
    body = _INLINE_HASHTAG_RE.sub(lambda m: _hashtag_anchor(m.group(1)), body)

    extra = [t for t in (extra_hashtags or []) if t.lower() not in inline_tags]
    seen = set(inline_tags)
    deduped_extra = []
    for tag in extra:
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            deduped_extra.append(tag)
    return body + _hashtags_html(deduped_extra)


class _TextExtractor(HTMLParser):
    """Collects only the text content of an HTML fragment, treating
    <p>/<br>/<div> as line breaks and discarding every tag itself
    (attributes included). Used solely by sanitize_remote_content() below."""
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "div"):
            self._parts.append("\n")

    def handle_data(self, data):
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def sanitize_remote_content(raw_html: str) -> str:
    """Strips a remote actor's Note `content` (a reply left on one of our
    posts) down to plain text (issue follow-up, Fediverse interaction
    client). That HTML comes from an untrusted third-party Fediverse
    server -- any AP-speaking server can send anything, regardless of
    what a well-behaved client like Mastodon would actually produce -- so
    it must never be stored/rendered verbatim; the interaction client
    would otherwise be a stored-XSS vector against whoever views it.
    <p>/<br>/<div> become newlines, every tag is discarded keeping only
    its text, and the result is capped against a hostile giant payload.
    Uses the stdlib html.parser -- no new dependency for what's a small,
    contained parse. The RESULT is plain text, not HTML -- callers
    display it escaped (e.g. via a CSS white-space:pre-wrap container),
    same trust level as any other user-supplied plain-text field."""
    if not raw_html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", parser.text()).strip()
    return text[:5000]


# ---------------------------------------------------------------------------
# Recording a post + resolving delivery targets -- the fast, DB-only half,
# called synchronously from the request handler.
# ---------------------------------------------------------------------------

async def _resolve_follower_targets(org, db) -> list:
    """Every follower's shared inbox (falling back to its own inbox),
    deduped -- so a broadcast to N followers on the same remote server is
    one HTTP request, not N. Shared by every "post to all followers" path
    below (announcements and ad-hoc posts alike)."""
    followers = (await db.execute(
        select(ActivityPubFollower).filter(ActivityPubFollower.org_id == org.id)
    )).scalars().all()
    return list({(f.shared_inbox_url or f.inbox_url) for f in followers})


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

    dest_urls = await _resolve_follower_targets(org, db)
    return post, dest_urls


async def announce_session_start(net, org, session, db) -> Optional[tuple]:
    return await record_and_get_targets(org, net, session, "start", start_content_html(net, org), db)


async def announce_session_end(net, org, session, checkin_count: int, db) -> Optional[tuple]:
    return await record_and_get_targets(org, net, session, "end", end_content_html(net, org, session, checkin_count), db)


async def record_manual_post(org, content_html: str, db) -> Optional[tuple]:
    """The Fediverse interaction client's ad-hoc "New Post" (issue
    follow-up) -- same shape/precondition checks as
    record_and_get_targets() above minus the net/session-specific ones
    (no net.activitypub_announce or session.is_offline to check; a manual
    post isn't tied to either), so it's a separate function rather than
    threading Optional[net]/Optional[session] through the existing one."""
    if not activitypub_configured():
        return None
    if not org or not org.activitypub_enabled:
        return None
    if not org.activitypub_private_key or not org.activitypub_public_key:
        return None

    post = ActivityPubPost(
        org_id=org.id, net_id=None, session_id=None,
        uuid=str(uuid.uuid4()), kind="manual", content_html=content_html,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)

    dest_urls = await _resolve_follower_targets(org, db)
    return post, dest_urls


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


def deliver_like(org, target_object_id: str, target_inbox_url: str) -> None:
    """Delivers a signed Like back to whoever left an interaction our org
    is Liking (issue follow-up, Fediverse interaction client) -- same
    single-target shape as deliver_accept above, backgrounded by the
    caller the same way."""
    like = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{APP_BASE_URL}/ap/activities/like/{uuid.uuid4()}",
        "type": "Like",
        "actor": build_actor_id(org),
        "object": target_object_id,
    }
    _deliver_signed(org, json.dumps(like).encode(), target_inbox_url)


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


def _actor_display(actor_doc: Optional[dict]) -> tuple:
    """(handle, name) for showing "who replied"/"who liked this" in the
    Fediverse interaction client (issue follow-up) -- built from an actor
    document already fetched by fetch_remote_actor() above (the inbox
    handler fetches it anyway, for signature verification, so there's no
    second HTTP call). Handle is derived the same way build_handle()
    derives our own -- preferredUsername@<host from the actor id> -- name
    falls back to the handle if the remote doc doesn't set one."""
    doc = actor_doc or {}
    username = doc.get("preferredUsername") or "?"
    host = urlparse(doc.get("id", "")).netloc
    handle = f"{username}@{host}" if host else username
    name = doc.get("name") or handle
    return handle, name
