"""
Tests for the Fediverse interaction client (issue follow-up):
  - The fediverse_operator org-wide role: requestable at registration/join,
    grantable at approval and after the fact, same shape as the other
    three roles (tests/test_roles.py already covers those).
  - routers/activitypub.py's post_inbox now persisting a Create(reply)/
    Like as an ActivityPubInteraction when (and only when) it targets one
    of the org's own posts -- everything else stays silently ignored, and
    a duplicate delivery of the same activity is a no-op.
  - routers/fediverse.py's endpoints: list interactions/posts (gated by
    require_fediverse_access), reply, like (idempotent), compose an
    ad-hoc post.
  - activitypub_delivery.py's render_user_content_html() and
    sanitize_remote_content() as pure functions.

Reuses tests/test_activitypub.py's inbound-signed-activity pattern
(remote keypair + fetch_remote_actor monkeypatch + HTTP-signature POST)
and tests/test_roles.py's org-founding/join/approve helpers, rather than
importing them across files (each is small and self-contained -- see
those files' own docstrings for why this codebase already does that in
a couple of places, e.g. test_org_reassignment.py importing from
test_organizations.py; this one just stays local instead).
"""

import json

import pytest
from sqlalchemy import select

import activitypub_delivery
import activitypub_signing
from helpers import auth, login
from models import ActivityPubFollower, ActivityPubInteraction, ActivityPubPost

REMOTE_ACTOR_ID = "https://remote.example/users/alice"
REMOTE_INBOX = "https://remote.example/users/alice/inbox"


@pytest.fixture
def remote_keypair():
    return activitypub_signing.generate_keypair()


@pytest.fixture
def remote_actor(remote_keypair, monkeypatch):
    _, public_pem = remote_keypair
    doc = {
        "id": REMOTE_ACTOR_ID,
        "preferredUsername": "alice",
        "name": "Alice Example",
        "inbox": REMOTE_INBOX,
        "endpoints": {"sharedInbox": "https://remote.example/inbox"},
        "publicKey": {"id": f"{REMOTE_ACTOR_ID}#main-key", "publicKeyPem": public_pem},
    }
    monkeypatch.setattr(activitypub_delivery, "fetch_remote_actor", lambda actor_id: doc)
    return doc


@pytest.fixture
def ap_deliveries(monkeypatch):
    import httpx
    calls = []

    class FakeResponse:
        status_code = 200

    def fake_post(url, content=None, headers=None, timeout=None):
        calls.append({"url": url, "content": content, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def _signed_post(client, path: str, body: dict, private_pem: str, key_id: str):
    raw = json.dumps(body).encode()
    date = activitypub_signing.http_date()
    digest, sig_header = activitypub_signing.sign_request(private_pem, "POST", path, "testserver", date, raw, key_id)
    return client.post(path, content=raw, headers={
        "Date": date, "Digest": digest, "Content-Type": "application/activity+json", "Signature": sig_header,
    })


def _enable_org_activitypub(client, org_id, headers):
    resp = client.put(f"/orgs/{org_id}/activitypub", json={"enabled": True}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _org_slug(client, headers):
    return client.get("/orgs/mine", headers=headers).json()[0]["slug"]


async def _seed_post(db, org_id, kind="start", content_html="<p>Test Net is starting now.</p>"):
    import uuid
    post = ActivityPubPost(org_id=org_id, uuid=str(uuid.uuid4()), kind=kind, content_html=content_html)
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return post


def _join_org(client, org_slug, callsign, requested_roles=None):
    body = {
        "callsign": callsign, "name": callsign, "email": f"{callsign.lower()}@example.com",
        "password": "testpass123", "org_slug": org_slug,
    }
    if requested_roles is not None:
        body["requested_roles"] = requested_roles
    resp = client.post("/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# fediverse_operator role plumbing
# ---------------------------------------------------------------------------

class TestFediverseOperatorRole:
    def test_can_be_requested_at_registration(self, client, admin_headers, net):
        slug = _org_slug(client, admin_headers)
        uid = _join_org(client, slug, "W2FED", requested_roles=["fediverse_operator"])
        pending = client.get(f"/orgs/{net['org_id']}/pending-members", headers=admin_headers).json()
        row = next(m for m in pending if m["user_id"] == uid)
        assert row["requested_roles"] == ["fediverse_operator"]

    def test_can_be_granted_at_approval(self, client, admin_headers, net):
        slug = _org_slug(client, admin_headers)
        uid = _join_org(client, slug, "W2FED", requested_roles=["fediverse_operator"])
        resp = client.patch(
            f"/orgs/{net['org_id']}/members/{uid}/approve", json={"roles": ["fediverse_operator"]}, headers=admin_headers,
        )
        assert resp.status_code == 204, resp.text
        members = client.get(f"/orgs/{net['org_id']}/members", headers=admin_headers).json()
        row = next(m for m in members if m["user_id"] == uid)
        assert "fediverse_operator" in row["roles"]

    def test_can_be_toggled_after_the_fact(self, client, admin_headers, net):
        slug = _org_slug(client, admin_headers)
        uid = _join_org(client, slug, "W2FED")
        client.patch(f"/orgs/{net['org_id']}/members/{uid}/approve", json={"roles": ["net_control_op"]}, headers=admin_headers)

        resp = client.put(
            f"/orgs/{net['org_id']}/members/{uid}/extra-roles", json={"roles": ["net_control_op", "fediverse_operator"]}, headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        members = client.get(f"/orgs/{net['org_id']}/members", headers=admin_headers).json()
        row = next(m for m in members if m["user_id"] == uid)
        assert "fediverse_operator" in row["roles"]

    def test_not_granted_by_default_on_plain_approve(self, client, admin_headers, net):
        slug = _org_slug(client, admin_headers)
        uid = _join_org(client, slug, "W2FED")
        client.patch(f"/orgs/{net['org_id']}/members/{uid}/approve", json={"roles": ["net_control_op"]}, headers=admin_headers)
        members = client.get(f"/orgs/{net['org_id']}/members", headers=admin_headers).json()
        row = next(m for m in members if m["user_id"] == uid)
        assert "fediverse_operator" not in row["roles"]


# ---------------------------------------------------------------------------
# Inbox: Create(reply) / Like -> ActivityPubInteraction
# ---------------------------------------------------------------------------

class TestInboxInteractions:
    async def test_reply_to_our_note_is_persisted(self, client, admin_headers, net, activitypub_app_base_url, remote_actor, remote_keypair, db):
        private_pem, _ = remote_keypair
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        slug = _org_slug(client, admin_headers)
        post = await _seed_post(db, net["org_id"])
        note_url = f"http://testserver/ap/objects/notes/{post.uuid}"

        create = {
            "id": f"{REMOTE_ACTOR_ID}#create/1", "type": "Create", "actor": REMOTE_ACTOR_ID,
            "object": {
                "id": f"{REMOTE_ACTOR_ID}#note/1", "type": "Note", "attributedTo": REMOTE_ACTOR_ID,
                "inReplyTo": note_url, "content": "<p>Great net tonight!</p>",
            },
        }
        resp = _signed_post(client, f"/ap/orgs/{slug}/inbox", create, private_pem, f"{REMOTE_ACTOR_ID}#main-key")
        assert resp.status_code == 202, resp.text

        rows = (await db.execute(select(ActivityPubInteraction).filter(ActivityPubInteraction.post_id == post.id))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == "reply"
        assert row.content_html == "Great net tonight!"   # sanitized to plain text, tags stripped
        assert row.remote_actor_handle == "alice@remote.example"
        assert row.remote_actor_name == "Alice Example"
        assert row.remote_inbox_url == REMOTE_INBOX

    async def test_like_on_our_note_is_persisted(self, client, admin_headers, net, activitypub_app_base_url, remote_actor, remote_keypair, db):
        private_pem, _ = remote_keypair
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        slug = _org_slug(client, admin_headers)
        post = await _seed_post(db, net["org_id"])
        note_url = f"http://testserver/ap/objects/notes/{post.uuid}"

        like = {"id": f"{REMOTE_ACTOR_ID}#like/1", "type": "Like", "actor": REMOTE_ACTOR_ID, "object": note_url}
        resp = _signed_post(client, f"/ap/orgs/{slug}/inbox", like, private_pem, f"{REMOTE_ACTOR_ID}#main-key")
        assert resp.status_code == 202, resp.text

        rows = (await db.execute(select(ActivityPubInteraction).filter(ActivityPubInteraction.post_id == post.id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].kind == "like"
        assert rows[0].content_html is None

    async def test_reply_to_something_else_is_ignored(self, client, admin_headers, net, activitypub_app_base_url, remote_actor, remote_keypair, db):
        private_pem, _ = remote_keypair
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        slug = _org_slug(client, admin_headers)
        await _seed_post(db, net["org_id"])   # exists, but the reply below doesn't target it

        create = {
            "id": f"{REMOTE_ACTOR_ID}#create/2", "type": "Create", "actor": REMOTE_ACTOR_ID,
            "object": {
                "id": f"{REMOTE_ACTOR_ID}#note/2", "type": "Note", "attributedTo": REMOTE_ACTOR_ID,
                "inReplyTo": "https://elsewhere.example/notes/999", "content": "<p>Unrelated reply</p>",
            },
        }
        resp = _signed_post(client, f"/ap/orgs/{slug}/inbox", create, private_pem, f"{REMOTE_ACTOR_ID}#main-key")
        assert resp.status_code == 202

        rows = (await db.execute(select(ActivityPubInteraction).filter(ActivityPubInteraction.org_id == net["org_id"]))).scalars().all()
        assert rows == []

    async def test_duplicate_delivery_is_a_no_op(self, client, admin_headers, net, activitypub_app_base_url, remote_actor, remote_keypair, db):
        private_pem, _ = remote_keypair
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        slug = _org_slug(client, admin_headers)
        post = await _seed_post(db, net["org_id"])
        note_url = f"http://testserver/ap/objects/notes/{post.uuid}"
        like = {"id": f"{REMOTE_ACTOR_ID}#like/dup", "type": "Like", "actor": REMOTE_ACTOR_ID, "object": note_url}

        # Same activity delivered twice (Mastodon retries on a timeout/5xx).
        for _ in range(2):
            resp = _signed_post(client, f"/ap/orgs/{slug}/inbox", like, private_pem, f"{REMOTE_ACTOR_ID}#main-key")
            assert resp.status_code == 202

        rows = (await db.execute(select(ActivityPubInteraction).filter(ActivityPubInteraction.post_id == post.id))).scalars().all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# routers/fediverse.py endpoints
# ---------------------------------------------------------------------------

class TestFediverseEndpoints:
    def test_plain_member_is_denied(self, client, admin_headers, user_headers, net, activitypub_app_base_url):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        resp = client.get(f"/orgs/{net['org_id']}/fediverse/interactions", headers=user_headers)
        assert resp.status_code == 403

    def test_fediverse_operator_is_allowed(self, client, admin_headers, net, activitypub_app_base_url):
        slug = _org_slug(client, admin_headers)
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        uid = _join_org(client, slug, "W2FED")
        client.patch(f"/orgs/{net['org_id']}/members/{uid}/approve", json={"roles": ["fediverse_operator"]}, headers=admin_headers)
        op_headers = auth(login(client, "W2FED"))

        resp = client.get(f"/orgs/{net['org_id']}/fediverse/interactions", headers=op_headers)
        assert resp.status_code == 200

    async def test_list_interactions_includes_post_context(self, client, admin_headers, net, activitypub_app_base_url, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        post = await _seed_post(db, net["org_id"])
        db.add(ActivityPubInteraction(
            org_id=net["org_id"], post_id=post.id, kind="reply", remote_actor_id=REMOTE_ACTOR_ID,
            remote_actor_handle="alice@remote.example", remote_actor_name="Alice Example",
            remote_object_id=f"{REMOTE_ACTOR_ID}#note/1", remote_inbox_url=REMOTE_INBOX, content_html="Nice net!",
        ))
        await db.commit()

        resp = client.get(f"/orgs/{net['org_id']}/fediverse/interactions", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["content_html"] == "Nice net!"
        assert data[0]["post_id"] == post.id
        assert data[0]["post_kind"] == "start"

    async def test_reply_delivers_signed_create_and_persists_post(self, client, admin_headers, net, activitypub_app_base_url, ap_deliveries, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        post = await _seed_post(db, net["org_id"])
        interaction = ActivityPubInteraction(
            org_id=net["org_id"], post_id=post.id, kind="reply", remote_actor_id=REMOTE_ACTOR_ID,
            remote_object_id=f"{REMOTE_ACTOR_ID}#note/1", remote_inbox_url=REMOTE_INBOX, content_html="Nice net!",
        )
        db.add(interaction)
        await db.commit()
        await db.refresh(interaction)

        resp = client.post(
            f"/orgs/{net['org_id']}/fediverse/interactions/{interaction.id}/reply",
            json={"content": "Thanks for checking in!"}, headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        assert "Thanks for checking in!" in resp.json()["content_html"]
        assert resp.json()["kind"] == "reply"
        assert len(ap_deliveries) == 1
        assert ap_deliveries[0]["url"] == REMOTE_INBOX

        reply_post = (await db.execute(select(ActivityPubPost).filter(
            ActivityPubPost.org_id == net["org_id"], ActivityPubPost.kind == "reply",
        ))).scalar_one()
        assert reply_post.in_reply_to == interaction.remote_object_id
        assert reply_post.in_reply_to_actor == REMOTE_ACTOR_ID

    async def test_like_delivers_signed_like_and_is_idempotent(self, client, admin_headers, net, activitypub_app_base_url, ap_deliveries, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        post = await _seed_post(db, net["org_id"])
        interaction = ActivityPubInteraction(
            org_id=net["org_id"], post_id=post.id, kind="reply", remote_actor_id=REMOTE_ACTOR_ID,
            remote_object_id=f"{REMOTE_ACTOR_ID}#note/1", remote_inbox_url=REMOTE_INBOX, content_html="Nice net!",
        )
        db.add(interaction)
        await db.commit()
        await db.refresh(interaction)

        resp = client.post(f"/orgs/{net['org_id']}/fediverse/interactions/{interaction.id}/like", headers=admin_headers)
        assert resp.status_code == 204
        assert len(ap_deliveries) == 1

        # Second click -- already liked, no-op, no second delivery.
        resp2 = client.post(f"/orgs/{net['org_id']}/fediverse/interactions/{interaction.id}/like", headers=admin_headers)
        assert resp2.status_code == 204
        assert len(ap_deliveries) == 1

    async def test_compose_persists_manual_post_and_broadcasts(self, client, admin_headers, net, activitypub_app_base_url, ap_deliveries, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        db.add(ActivityPubFollower(org_id=net["org_id"], actor_id=REMOTE_ACTOR_ID, inbox_url=REMOTE_INBOX))
        await db.commit()

        resp = client.post(f"/orgs/{net['org_id']}/fediverse/posts", json={"content": "Field Day prep this weekend!"}, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        assert resp.json()["kind"] == "manual"
        assert "Field Day prep this weekend!" in resp.json()["content_html"]
        assert len(ap_deliveries) == 1

        posts_resp = client.get(f"/orgs/{net['org_id']}/fediverse/posts", headers=admin_headers)
        assert any(p["kind"] == "manual" for p in posts_resp.json())

    def test_compose_rejects_blank_content(self, client, admin_headers, net, activitypub_app_base_url):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        resp = client.post(f"/orgs/{net['org_id']}/fediverse/posts", json={"content": "   "}, headers=admin_headers)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# render_user_content_html / sanitize_remote_content -- pure functions
# ---------------------------------------------------------------------------

class TestRenderUserContentHtml:
    def test_blank_input_returns_empty_string(self):
        assert activitypub_delivery.render_user_content_html("") == ""
        assert activitypub_delivery.render_user_content_html("   ") == ""

    def test_paragraphs_and_escaping(self):
        rendered = activitypub_delivery.render_user_content_html("Hello <script>alert(1)</script>\n\nSecond paragraph")
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered
        assert rendered.count("<p>") == 2

    def test_inline_hashtag_uses_same_anchor_as_hashtags_html(self, activitypub_app_base_url):
        rendered = activitypub_delivery.render_user_content_html("Great net, #HamRadio!")
        expected_anchor = activitypub_delivery._hashtag_anchor("HamRadio")
        assert expected_anchor in rendered

    def test_extra_hashtags_appended_when_not_already_typed(self, activitypub_app_base_url):
        rendered = activitypub_delivery.render_user_content_html("Field Day prep!", extra_hashtags=["HamRadio"])
        assert activitypub_delivery._hashtag_anchor("HamRadio") in rendered

    def test_extra_hashtag_not_duplicated_if_already_typed_inline(self, activitypub_app_base_url):
        rendered = activitypub_delivery.render_user_content_html("Great net #HamRadio!", extra_hashtags=["HamRadio"])
        assert rendered.count(">#<span>HamRadio</span></a>") == 1


class TestSanitizeRemoteContent:
    def test_strips_tags_keeps_text(self):
        assert activitypub_delivery.sanitize_remote_content("<p>Hello <b>world</b></p>") == "Hello world"

    def test_paragraph_and_br_become_newlines(self):
        result = activitypub_delivery.sanitize_remote_content("<p>Line one</p><p>Line two</p>")
        assert "Line one" in result and "Line two" in result
        assert "\n" in result

    def test_empty_input(self):
        assert activitypub_delivery.sanitize_remote_content("") == ""

    def test_strips_a_potential_script_tag_to_inert_text(self):
        result = activitypub_delivery.sanitize_remote_content("<script>alert(1)</script>hi")
        assert "<script>" not in result
        assert "hi" in result

    def test_caps_pathological_length(self):
        result = activitypub_delivery.sanitize_remote_content("<p>" + ("x" * 10000) + "</p>")
        assert len(result) <= 5000
