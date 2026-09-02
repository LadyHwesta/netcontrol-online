"""
Tests for Fediverse participation via ActivityPub (issue follow-up):
  - Public protocol endpoints (routers/activitypub.py): WebFinger, actor
    document, followers/following/outbox stubs, dereferenceable post
    objects, and the inbox (Follow/Undo/Delete + signature verification).
  - Signing/verification round-trip (activitypub_signing.py) directly.
  - Org-admin enable/disable + status (routers/orgs.py's activitypub pair).
  - The session start/end announcement hooks (routers/sessions.py calling
    activitypub_delivery.announce_session_start()/announce_session_end()).

A "remote follower" is simulated throughout: its own RSA keypair is
generated locally, its actor document is a plain dict, and
activitypub_delivery.fetch_remote_actor() is monkeypatched to return it
instead of making a real network call -- exactly the boundary this app
actually depends on, without needing a live remote server.

activitypub_app_base_url is passed explicitly per test (not module-wide)
so test_enable_requires_app_base_url can exercise the unconfigured case.
"""

import json

import pytest
from sqlalchemy import select

import activitypub_delivery
import activitypub_signing
from models import ActivityPubFollower, ActivityPubPost, Organization

REMOTE_ACTOR_ID = "https://remote.example/users/alice"
REMOTE_INBOX = "https://remote.example/users/alice/inbox"
REMOTE_SHARED_INBOX = "https://remote.example/inbox"


@pytest.fixture
def remote_keypair():
    return activitypub_signing.generate_keypair()   # (private_pem, public_pem)


@pytest.fixture
def remote_actor(remote_keypair, monkeypatch):
    """Monkeypatches fetch_remote_actor() so the inbox handler (signature
    verification + follower inbox resolution) sees this fake remote actor
    instead of making a real HTTP call."""
    _, public_pem = remote_keypair
    doc = {
        "id": REMOTE_ACTOR_ID,
        "inbox": REMOTE_INBOX,
        "endpoints": {"sharedInbox": REMOTE_SHARED_INBOX},
        "publicKey": {"id": f"{REMOTE_ACTOR_ID}#main-key", "publicKeyPem": public_pem},
    }
    monkeypatch.setattr(activitypub_delivery, "fetch_remote_actor", lambda actor_id: doc)
    return doc


@pytest.fixture
def ap_deliveries(monkeypatch):
    """Intercepts httpx.post (used by activitypub_delivery._deliver_signed
    for both outbound Create broadcasts and Accept replies) and records
    each call instead of hitting the network."""
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


def _enable_org_activitypub(client, org_id, admin_headers):
    resp = client.put(f"/orgs/{org_id}/activitypub", json={"enabled": True}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _org_slug(client, admin_headers):
    return client.get("/orgs/mine", headers=admin_headers).json()[0]["slug"]


# ---------------------------------------------------------------------------
# Signing / verification (no HTTP)
# ---------------------------------------------------------------------------

class TestSigning:
    def test_sign_then_verify_round_trip(self):
        private_pem, public_pem = activitypub_signing.generate_keypair()
        body = b'{"hello":"world"}'
        date = activitypub_signing.http_date()
        digest, sig_header = activitypub_signing.sign_request(
            private_pem, "POST", "/ap/orgs/test/inbox", "example.com", date, body, "https://example.com/actor#main-key",
        )
        headers = {
            "host": "example.com", "date": date, "digest": digest,
            "content-type": "application/activity+json", "signature": sig_header,
        }
        assert activitypub_signing.verify_signature(headers, "POST", "/ap/orgs/test/inbox", public_pem)

    def test_tampered_signed_header_fails_verification(self):
        """The Digest header is one of the SIGNED headers -- changing it
        after signing (simulating a body swap that keeps the old Digest,
        or any other after-the-fact header tamper) breaks the RSA
        signature check, since the reconstructed signing string no longer
        matches what was actually signed."""
        private_pem, public_pem = activitypub_signing.generate_keypair()
        body = b'{"hello":"world"}'
        date = activitypub_signing.http_date()
        digest, sig_header = activitypub_signing.sign_request(
            private_pem, "POST", "/ap/orgs/test/inbox", "example.com", date, body, "https://example.com/actor#main-key",
        )
        headers = {
            "host": "example.com", "date": date, "digest": "SHA-256=" + "0" * 44,
            "content-type": "application/activity+json", "signature": sig_header,
        }
        assert not activitypub_signing.verify_signature(headers, "POST", "/ap/orgs/test/inbox", public_pem)

    def test_wrong_key_fails_verification(self):
        private_pem, _ = activitypub_signing.generate_keypair()
        _, other_public_pem = activitypub_signing.generate_keypair()
        body = b"{}"
        date = activitypub_signing.http_date()
        digest, sig_header = activitypub_signing.sign_request(
            private_pem, "POST", "/x", "example.com", date, body, "https://example.com/actor#main-key",
        )
        headers = {"host": "example.com", "date": date, "digest": digest, "content-type": "application/activity+json", "signature": sig_header}
        assert not activitypub_signing.verify_signature(headers, "POST", "/x", other_public_pem)

    def test_missing_signature_header_fails(self):
        assert not activitypub_signing.verify_signature({}, "POST", "/x", "not-a-real-key")


# ---------------------------------------------------------------------------
# Org-admin enable/disable + status
# ---------------------------------------------------------------------------

class TestOrgActivityPubAdmin:
    def test_requires_org_admin(self, client, user_headers, net):
        resp = client.get(f"/orgs/{net['org_id']}/activitypub", headers=user_headers)
        assert resp.status_code == 403

    def test_get_disabled_by_default(self, client, admin_headers, net):
        resp = client.get(f"/orgs/{net['org_id']}/activitypub", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "handle": None, "actor_url": None, "follower_count": 0}

    def test_enable_requires_app_base_url(self, client, admin_headers, net):
        # activitypub_app_base_url fixture deliberately NOT used here
        resp = client.put(f"/orgs/{net['org_id']}/activitypub", json={"enabled": True}, headers=admin_headers)
        assert resp.status_code == 400

    def test_enable_generates_handle(self, client, admin_headers, net, activitypub_app_base_url):
        slug = _org_slug(client, admin_headers)
        data = _enable_org_activitypub(client, net["org_id"], admin_headers)
        assert data["enabled"] is True
        assert data["handle"] == f"{slug}@testserver"
        assert data["actor_url"] == f"http://testserver/ap/orgs/{slug}/actor"
        assert data["follower_count"] == 0

    async def test_enabling_twice_does_not_regenerate_keypair(self, client, admin_headers, net, activitypub_app_base_url, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        org = (await db.execute(select(Organization).filter(Organization.id == net["org_id"]))).scalar_one()
        first_key = org.activitypub_public_key
        assert first_key

        client.put(f"/orgs/{net['org_id']}/activitypub", json={"enabled": False}, headers=admin_headers)
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        await db.refresh(org)
        assert org.activitypub_public_key == first_key

    async def test_disable_flips_flag_keeps_followers(self, client, admin_headers, net, activitypub_app_base_url, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        db.add(ActivityPubFollower(org_id=net["org_id"], actor_id=REMOTE_ACTOR_ID, inbox_url=REMOTE_INBOX))
        await db.commit()

        client.put(f"/orgs/{net['org_id']}/activitypub", json={"enabled": False}, headers=admin_headers)
        data = _enable_org_activitypub(client, net["org_id"], admin_headers)
        assert data["follower_count"] == 1


# ---------------------------------------------------------------------------
# WebFinger + actor document + collection stubs
# ---------------------------------------------------------------------------

class TestPublicEndpoints:
    def test_webfinger_404_for_unknown_org(self, client):
        resp = client.get("/.well-known/webfinger", params={"resource": "acct:nosuchorg@testserver"})
        assert resp.status_code == 404

    def test_webfinger_404_when_not_enabled(self, client, admin_headers, net):
        slug = _org_slug(client, admin_headers)
        resp = client.get("/.well-known/webfinger", params={"resource": f"acct:{slug}@testserver"})
        assert resp.status_code == 404

    def test_webfinger_200_when_enabled(self, client, admin_headers, net, activitypub_app_base_url):
        data = _enable_org_activitypub(client, net["org_id"], admin_headers)
        resp = client.get("/.well-known/webfinger", params={"resource": f"acct:{data['handle']}"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/jrd+json")
        body = resp.json()
        assert body["subject"] == f"acct:{data['handle']}"
        self_links = [l for l in body["links"] if l["rel"] == "self"]
        assert self_links and self_links[0]["type"] == "application/activity+json"
        assert self_links[0]["href"] == data["actor_url"]

    def test_actor_document(self, client, admin_headers, net, activitypub_app_base_url):
        data = _enable_org_activitypub(client, net["org_id"], admin_headers)
        slug = data["handle"].split("@")[0]
        resp = client.get(f"/ap/orgs/{slug}/actor")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/activity+json")
        doc = resp.json()
        assert doc["id"] == data["actor_url"]
        assert doc["type"] == "Service"
        assert doc["preferredUsername"] == slug
        assert doc["publicKey"]["id"] == f"{data['actor_url']}#main-key"
        assert doc["publicKey"]["publicKeyPem"]

    def test_actor_document_404_when_disabled(self, client, admin_headers, net):
        slug = _org_slug(client, admin_headers)
        resp = client.get(f"/ap/orgs/{slug}/actor")
        assert resp.status_code == 404

    def test_followers_collection_stub(self, client, admin_headers, net, activitypub_app_base_url):
        data = _enable_org_activitypub(client, net["org_id"], admin_headers)
        slug = data["handle"].split("@")[0]
        resp = client.get(f"/ap/orgs/{slug}/followers")
        assert resp.status_code == 200
        assert resp.json()["type"] == "OrderedCollection"
        assert resp.json()["totalItems"] == 0

    def test_following_collection_empty(self, client, admin_headers, net, activitypub_app_base_url):
        data = _enable_org_activitypub(client, net["org_id"], admin_headers)
        slug = data["handle"].split("@")[0]
        resp = client.get(f"/ap/orgs/{slug}/following")
        assert resp.status_code == 200
        assert resp.json()["totalItems"] == 0
        assert resp.json()["orderedItems"] == []

    def test_outbox_empty_when_no_posts(self, client, admin_headers, net, activitypub_app_base_url):
        data = _enable_org_activitypub(client, net["org_id"], admin_headers)
        slug = data["handle"].split("@")[0]
        resp = client.get(f"/ap/orgs/{slug}/outbox")
        assert resp.status_code == 200
        assert resp.json()["totalItems"] == 0


# ---------------------------------------------------------------------------
# Inbox: Follow / Undo / Delete
# ---------------------------------------------------------------------------

class TestInbox:
    def _slug(self, client, org_id, admin_headers):
        return _enable_org_activitypub(client, org_id, admin_headers)["handle"].split("@")[0]

    def test_follow_requires_signature(self, client, admin_headers, net, activitypub_app_base_url):
        slug = self._slug(client, net["org_id"], admin_headers)
        resp = client.post(f"/ap/orgs/{slug}/inbox", json={"type": "Follow", "actor": REMOTE_ACTOR_ID, "object": "x"})
        assert resp.status_code == 401

    def test_valid_follow_creates_follower_and_sends_accept(self, client, admin_headers, net, activitypub_app_base_url, remote_keypair, remote_actor, ap_deliveries):
        private_pem, _ = remote_keypair
        slug = self._slug(client, net["org_id"], admin_headers)
        actor_url = client.get(f"/ap/orgs/{slug}/actor").json()["id"]
        follow = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{REMOTE_ACTOR_ID}#follows/1",
            "type": "Follow",
            "actor": REMOTE_ACTOR_ID,
            "object": actor_url,
        }
        resp = _signed_post(client, f"/ap/orgs/{slug}/inbox", follow, private_pem, f"{REMOTE_ACTOR_ID}#main-key")
        assert resp.status_code == 202

        status = client.get(f"/orgs/{net['org_id']}/activitypub", headers=admin_headers).json()
        assert status["follower_count"] == 1
        # An Accept was delivered back to the follower's inbox
        assert len(ap_deliveries) == 1
        accept_body = json.loads(ap_deliveries[0]["content"])
        assert accept_body["type"] == "Accept"
        assert accept_body["object"]["id"] == follow["id"]

    def test_bad_signature_rejected(self, client, admin_headers, net, activitypub_app_base_url, remote_actor):
        slug = self._slug(client, net["org_id"], admin_headers)
        actor_url = client.get(f"/ap/orgs/{slug}/actor").json()["id"]
        follow = {"type": "Follow", "actor": REMOTE_ACTOR_ID, "object": actor_url, "id": "x"}
        # Signed with a DIFFERENT private key than the one remote_actor advertises
        other_private, _ = activitypub_signing.generate_keypair()
        resp = _signed_post(client, f"/ap/orgs/{slug}/inbox", follow, other_private, f"{REMOTE_ACTOR_ID}#main-key")
        assert resp.status_code == 401
        status = client.get(f"/orgs/{net['org_id']}/activitypub", headers=admin_headers).json()
        assert status["follower_count"] == 0

    def test_undo_follow_removes_follower(self, client, admin_headers, net, activitypub_app_base_url, remote_keypair, remote_actor, ap_deliveries):
        private_pem, _ = remote_keypair
        slug = self._slug(client, net["org_id"], admin_headers)
        actor_url = client.get(f"/ap/orgs/{slug}/actor").json()["id"]
        follow = {"id": f"{REMOTE_ACTOR_ID}#follows/1", "type": "Follow", "actor": REMOTE_ACTOR_ID, "object": actor_url}
        _signed_post(client, f"/ap/orgs/{slug}/inbox", follow, private_pem, f"{REMOTE_ACTOR_ID}#main-key")
        assert client.get(f"/orgs/{net['org_id']}/activitypub", headers=admin_headers).json()["follower_count"] == 1

        undo = {"id": f"{REMOTE_ACTOR_ID}#follows/1/undo", "type": "Undo", "actor": REMOTE_ACTOR_ID, "object": follow}
        resp = _signed_post(client, f"/ap/orgs/{slug}/inbox", undo, private_pem, f"{REMOTE_ACTOR_ID}#main-key")
        assert resp.status_code == 202
        assert client.get(f"/orgs/{net['org_id']}/activitypub", headers=admin_headers).json()["follower_count"] == 0

    def test_delete_removes_follower(self, client, admin_headers, net, activitypub_app_base_url, remote_keypair, remote_actor, ap_deliveries):
        private_pem, _ = remote_keypair
        slug = self._slug(client, net["org_id"], admin_headers)
        actor_url = client.get(f"/ap/orgs/{slug}/actor").json()["id"]
        follow = {"id": f"{REMOTE_ACTOR_ID}#follows/1", "type": "Follow", "actor": REMOTE_ACTOR_ID, "object": actor_url}
        _signed_post(client, f"/ap/orgs/{slug}/inbox", follow, private_pem, f"{REMOTE_ACTOR_ID}#main-key")

        delete = {"id": f"{REMOTE_ACTOR_ID}#delete", "type": "Delete", "actor": REMOTE_ACTOR_ID, "object": REMOTE_ACTOR_ID}
        resp = _signed_post(client, f"/ap/orgs/{slug}/inbox", delete, private_pem, f"{REMOTE_ACTOR_ID}#main-key")
        assert resp.status_code == 202
        assert client.get(f"/orgs/{net['org_id']}/activitypub", headers=admin_headers).json()["follower_count"] == 0

    def test_unknown_activity_type_does_not_crash(self, client, admin_headers, net, activitypub_app_base_url, remote_keypair, remote_actor):
        private_pem, _ = remote_keypair
        slug = self._slug(client, net["org_id"], admin_headers)
        like = {"id": "x", "type": "Like", "actor": REMOTE_ACTOR_ID, "object": "https://example.com/notes/1"}
        resp = _signed_post(client, f"/ap/orgs/{slug}/inbox", like, private_pem, f"{REMOTE_ACTOR_ID}#main-key")
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Session start/end -> announcement hooks
# ---------------------------------------------------------------------------

class TestSessionAnnouncements:
    def _ap_net(self, client, admin_headers, org_id, name="AP Net"):
        resp = client.post("/nets", json={"name": name, "is_ares": False, "activitypub_announce": True}, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_start_session_posts_when_opted_in(self, client, admin_headers, net, activitypub_app_base_url, ap_deliveries, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        ap_net = self._ap_net(client, admin_headers, net["org_id"])
        db.add(ActivityPubFollower(org_id=net["org_id"], actor_id=REMOTE_ACTOR_ID, inbox_url=REMOTE_INBOX))
        await db.commit()

        resp = client.post(f"/nets/{ap_net['id']}/sessions", json={}, headers=admin_headers)
        assert resp.status_code == 201, resp.text

        posts = (await db.execute(select(ActivityPubPost).filter(ActivityPubPost.net_id == ap_net["id"]))).scalars().all()
        assert len(posts) == 1
        assert posts[0].kind == "start"
        assert ap_net["name"] in posts[0].content_html
        assert len(ap_deliveries) == 1

    async def test_end_session_posts_summary(self, client, admin_headers, net, activitypub_app_base_url, ap_deliveries, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        ap_net = self._ap_net(client, admin_headers, net["org_id"])
        db.add(ActivityPubFollower(org_id=net["org_id"], actor_id=REMOTE_ACTOR_ID, inbox_url=REMOTE_INBOX))
        await db.commit()

        sess = client.post(f"/nets/{ap_net['id']}/sessions", json={}, headers=admin_headers).json()
        resp = client.patch(f"/sessions/{sess['id']}/end", headers=admin_headers)
        assert resp.status_code == 200

        posts = (await db.execute(
            select(ActivityPubPost).filter(ActivityPubPost.session_id == sess["id"], ActivityPubPost.kind == "end")
        )).scalars().all()
        assert len(posts) == 1
        assert "0 check-in" in posts[0].content_html

    async def test_note_and_create_are_dereferenceable(self, client, admin_headers, net, activitypub_app_base_url, ap_deliveries, db):
        data = _enable_org_activitypub(client, net["org_id"], admin_headers)
        slug = data["handle"].split("@")[0]
        ap_net = self._ap_net(client, admin_headers, net["org_id"])
        client.post(f"/nets/{ap_net['id']}/sessions", json={}, headers=admin_headers)

        post = (await db.execute(select(ActivityPubPost).filter(ActivityPubPost.net_id == ap_net["id"]))).scalar_one()

        note_resp = client.get(f"/ap/objects/notes/{post.uuid}")
        assert note_resp.status_code == 200
        assert note_resp.json()["content"] == post.content_html

        create_resp = client.get(f"/ap/activities/create/{post.uuid}")
        assert create_resp.status_code == 200
        assert create_resp.json()["object"]["id"] == note_resp.json()["id"]

        outbox = client.get(f"/ap/orgs/{slug}/outbox").json()
        assert outbox["totalItems"] == 1

    async def test_no_post_when_org_disabled(self, client, admin_headers, net, activitypub_app_base_url, ap_deliveries, db):
        # Org AP left disabled -- net opted in doesn't matter
        ap_net = self._ap_net(client, admin_headers, net["org_id"])
        resp = client.post(f"/nets/{ap_net['id']}/sessions", json={}, headers=admin_headers)
        assert resp.status_code == 201

        posts = (await db.execute(select(ActivityPubPost).filter(ActivityPubPost.net_id == ap_net["id"]))).scalars().all()
        assert posts == []
        assert len(ap_deliveries) == 0

    async def test_no_post_when_net_not_opted_in(self, client, admin_headers, net, activitypub_app_base_url, ap_deliveries, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        # `net` fixture doesn't set activitypub_announce -- defaults False
        resp = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers)
        assert resp.status_code == 201

        posts = (await db.execute(select(ActivityPubPost).filter(ActivityPubPost.net_id == net["id"]))).scalars().all()
        assert posts == []

    async def test_no_post_for_offline_session(self, client, admin_headers, net, activitypub_app_base_url, ap_deliveries, db):
        from datetime import datetime, timezone
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        ap_net = self._ap_net(client, admin_headers, net["org_id"])
        resp = client.post(f"/nets/{ap_net['id']}/sessions", json={
            "is_offline": True, "occurred_at": datetime.now(timezone.utc).isoformat(),
        }, headers=admin_headers)
        assert resp.status_code == 201

        posts = (await db.execute(select(ActivityPubPost).filter(ActivityPubPost.net_id == ap_net["id"]))).scalars().all()
        assert posts == []

    async def test_delivery_failure_never_breaks_session_start(self, client, admin_headers, net, activitypub_app_base_url, monkeypatch, db):
        _enable_org_activitypub(client, net["org_id"], admin_headers)
        ap_net = self._ap_net(client, admin_headers, net["org_id"])
        db.add(ActivityPubFollower(org_id=net["org_id"], actor_id=REMOTE_ACTOR_ID, inbox_url=REMOTE_INBOX))
        await db.commit()

        import httpx

        def raising_post(*args, **kwargs):
            raise httpx.ConnectError("simulated network failure")

        monkeypatch.setattr(httpx, "post", raising_post)
        resp = client.post(f"/nets/{ap_net['id']}/sessions", json={}, headers=admin_headers)
        assert resp.status_code == 201
