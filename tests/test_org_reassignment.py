"""
Tests for admin-created operator accounts and cross-org reassignment
(issue #1 follow-up — "started single-tenant, needs to split into
multiple orgs and rearrange things"):
  POST  /orgs/{org_id}/users
  POST  /auth/set-password
  PATCH /admin/users/{user_id}/org
  PATCH /admin/nets/{net_id}/org
"""

from helpers import login, auth
from test_organizations import _bootstrap_super_admin, _org_owner


def _create_org(client, super_token, callsign, org_slug, org_name, website="https://example.org"):
    """Founds a new org (approved by the super admin) and returns (org_id, admin_token)."""
    token = _org_owner(client, super_token, callsign, org_slug, org_name, website)
    orgs = client.get("/orgs").json()
    org = next(o for o in orgs if o["slug"] == org_slug)
    return org["id"], token


class TestCreateOrgUser:
    def test_requires_smtp_configured(self, client):
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        resp = client.post(f"/orgs/{org_id}/users", headers=auth(admin_token), json={
            "callsign": "W1NEW", "name": "New Op", "email": "new@example.com",
        })
        assert resp.status_code == 400
        assert "email" in resp.json()["detail"].lower()

    def test_org_admin_creates_approved_active_user(self, client, smtp_configured, sent_emails, app_base_url):
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        sent_emails.clear()  # discard the org-founding verification/notification emails from setup above
        resp = client.post(f"/orgs/{org_id}/users", headers=auth(admin_token), json={
            "callsign": "w1new", "name": "New Op", "email": "new@example.com", "gmrs_callsign": "wrxx123",
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["callsign"] == "W1NEW"
        assert data["gmrs_callsign"] == "WRXX123"
        assert data["is_active"] is True
        assert data["email_verified"] is True
        assert data["org_name"] == "Org A"

        members = client.get(f"/orgs/{org_id}/members", headers=auth(admin_token)).json()
        assert any(m["callsign"] == "W1NEW" and m["approved"] for m in members)

        assert len(sent_emails) == 1
        assert sent_emails[0]["to"] == ["new@example.com"]
        assert "setpw=" in sent_emails[0]["body_html"]

    def test_new_user_cannot_log_in_before_setting_password(self, client, smtp_configured, sent_emails, app_base_url):
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        client.post(f"/orgs/{org_id}/users", headers=auth(admin_token), json={
            "callsign": "W1NEW", "name": "New Op", "email": "new@example.com",
        })
        resp = client.post("/auth/login", data={"username": "W1NEW", "password": "anything"})
        assert resp.status_code == 401

    def test_duplicate_callsign_rejected(self, client, smtp_configured, sent_emails, app_base_url):
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        resp = client.post(f"/orgs/{org_id}/users", headers=auth(admin_token), json={
            "callsign": "W1A", "name": "Dup", "email": "dup@example.com",
        })
        assert resp.status_code == 400
        assert "callsign" in resp.json()["detail"].lower()

    def test_duplicate_email_rejected(self, client, smtp_configured, sent_emails, app_base_url):
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        resp = client.post(f"/orgs/{org_id}/users", headers=auth(admin_token), json={
            "callsign": "W1DUP", "name": "Dup", "email": "w1a@example.com",
        })
        assert resp.status_code == 400
        assert "email" in resp.json()["detail"].lower()

    def test_non_org_admin_forbidden(self, client, smtp_configured, sent_emails, app_base_url):
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        # A plain member of Org A (not its admin) cannot create operators
        resp = client.post("/auth/register", json={
            "callsign": "W1MEM", "name": "Member", "email": "mem@example.com",
            "password": "testpass123", "org_slug": "orga",
        })
        assert resp.status_code == 201
        member_id = resp.json()["id"]
        client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(admin_token))
        member_token = login(client, "W1MEM")
        resp = client.post(f"/orgs/{org_id}/users", headers=auth(member_token), json={
            "callsign": "W1BLOCKED", "name": "Blocked", "email": "blocked@example.com",
        })
        assert resp.status_code == 403

    def test_org_admin_of_other_org_forbidden(self, client, smtp_configured, sent_emails, app_base_url):
        super_token = _bootstrap_super_admin(client)
        org_a_id, _ = _create_org(client, super_token, "W1A", "orga", "Org A")
        _, admin_b_token = _create_org(client, super_token, "W2B", "orgb", "Org B")
        resp = client.post(f"/orgs/{org_a_id}/users", headers=auth(admin_b_token), json={
            "callsign": "W1CROSS", "name": "Cross", "email": "cross@example.com",
        })
        assert resp.status_code == 403


class TestSetPassword:
    def test_valid_token_sets_password_and_logs_in(self, client, smtp_configured, sent_emails, app_base_url):
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        client.post(f"/orgs/{org_id}/users", headers=auth(admin_token), json={
            "callsign": "W1NEW", "name": "New Op", "email": "new@example.com",
        })
        body_text = sent_emails[-1]["body_text"]
        link = [line for line in body_text.splitlines() if line.startswith("http")][0]
        token = link.split("setpw=")[1]

        resp = client.post("/auth/set-password", json={"token": token, "password": "brandnewpass123"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["access_token"]
        assert data["user"]["callsign"] == "W1NEW"

        # New password works; old (unusable) placeholder never did
        assert login(client, "W1NEW", password="brandnewpass123")

    def test_token_is_single_use(self, client, smtp_configured, sent_emails, app_base_url):
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        client.post(f"/orgs/{org_id}/users", headers=auth(admin_token), json={
            "callsign": "W1NEW", "name": "New Op", "email": "new@example.com",
        })
        body_text = sent_emails[-1]["body_text"]
        link = [line for line in body_text.splitlines() if line.startswith("http")][0]
        token = link.split("setpw=")[1]

        first = client.post("/auth/set-password", json={"token": token, "password": "brandnewpass123"})
        assert first.status_code == 200
        second = client.post("/auth/set-password", json={"token": token, "password": "anotherpass456"})
        assert second.status_code == 400

    def test_invalid_token_rejected(self, client):
        resp = client.post("/auth/set-password", json={"token": "bogus", "password": "brandnewpass123"})
        assert resp.status_code == 400

    def test_short_password_rejected(self, client, smtp_configured, sent_emails, app_base_url):
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        client.post(f"/orgs/{org_id}/users", headers=auth(admin_token), json={
            "callsign": "W1NEW", "name": "New Op", "email": "new@example.com",
        })
        body_text = sent_emails[-1]["body_text"]
        link = [line for line in body_text.splitlines() if line.startswith("http")][0]
        token = link.split("setpw=")[1]
        resp = client.post("/auth/set-password", json={"token": token, "password": "short"})
        assert resp.status_code == 400


class TestReassignUser:
    def test_super_admin_moves_user_between_orgs(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, _ = _create_org(client, super_token, "W1A", "orga", "Org A")
        org_b_id, _ = _create_org(client, super_token, "W2B", "orgb", "Org B")

        resp = client.get("/admin/users", headers=auth(super_token)).json()
        user_a = next(u for u in resp if u["callsign"] == "W1A")

        move = client.patch(f"/admin/users/{user_a['id']}/org", headers=auth(super_token), json={
            "org_id": org_b_id, "role": "member",
        })
        assert move.status_code == 200, move.text
        assert move.json()["org_name"] == "Org B"

        # current_org_id switched, and old org's membership is gone
        me = login(client, "W1A")
        current = client.get("/auth/me", headers=auth(me)).json()
        assert current["current_org_id"] == org_b_id

        mine = client.get("/orgs/mine", headers=auth(me)).json()
        assert [o["id"] for o in mine] == [org_b_id]
        assert mine[0]["role"] == "member"

    def test_moving_last_member_orphans_and_deletes_old_org(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, _ = _create_org(client, super_token, "W1A", "orga", "Org A")
        org_b_id, _ = _create_org(client, super_token, "W2B", "orgb", "Org B")

        users = client.get("/admin/users", headers=auth(super_token)).json()
        user_a = next(u for u in users if u["callsign"] == "W1A")
        client.patch(f"/admin/users/{user_a['id']}/org", headers=auth(super_token), json={
            "org_id": org_b_id, "role": "member",
        })

        orgs = client.get("/orgs").json()
        assert not any(o["id"] == org_a_id for o in orgs)

    def test_non_super_admin_forbidden(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, admin_a_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        org_b_id, _ = _create_org(client, super_token, "W2B", "orgb", "Org B")
        users = client.get("/admin/users", headers=auth(super_token)).json()
        user_b = next(u for u in users if u["callsign"] == "W2B")
        resp = client.patch(f"/admin/users/{user_b['id']}/org", headers=auth(admin_a_token), json={
            "org_id": org_a_id,
        })
        assert resp.status_code == 403

    def test_unknown_org_404(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, _ = _create_org(client, super_token, "W1A", "orga", "Org A")
        users = client.get("/admin/users", headers=auth(super_token)).json()
        user_a = next(u for u in users if u["callsign"] == "W1A")
        resp = client.patch(f"/admin/users/{user_a['id']}/org", headers=auth(super_token), json={
            "org_id": 999999,
        })
        assert resp.status_code == 404


class TestAddMembership:
    def test_super_admin_adds_second_org_without_touching_first(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, _ = _create_org(client, super_token, "W1A", "orga", "Org A")
        org_b_id, _ = _create_org(client, super_token, "W2B", "orgb", "Org B")
        users = client.get("/admin/users", headers=auth(super_token)).json()
        user_a = next(u for u in users if u["callsign"] == "W1A")

        resp = client.post(f"/admin/users/{user_a['id']}/orgs", headers=auth(super_token), json={
            "org_id": org_b_id, "role": "member",
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["org_id"] == org_b_id
        assert data["role"] == "member"

        # Original Org A membership (as admin) is untouched, current_org_id unchanged
        me = login(client, "W1A")
        current = client.get("/auth/me", headers=auth(me)).json()
        assert current["current_org_id"] == org_a_id

        mine = client.get("/orgs/mine", headers=auth(me)).json()
        roles_by_org = {o["id"]: o["role"] for o in mine}
        assert roles_by_org == {org_a_id: "admin", org_b_id: "member"}

    def test_approves_existing_pending_membership_in_place(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, admin_a_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        org_b_id, _ = _create_org(client, super_token, "W2B", "orgb", "Org B")

        # W1A self-service requests to join Org B (pending, since it's an existing org)
        join = client.post("/orgs/join", headers=auth(admin_a_token), json={"org_slug": "orgb"})
        assert join.status_code == 201, join.text

        users = client.get("/admin/users", headers=auth(super_token)).json()
        user_a = next(u for u in users if u["callsign"] == "W1A")
        resp = client.post(f"/admin/users/{user_a['id']}/orgs", headers=auth(super_token), json={
            "org_id": org_b_id, "role": "admin",
        })
        assert resp.status_code == 201, resp.text
        assert resp.json()["role"] == "admin"

        mine = client.get("/orgs/mine", headers=auth(admin_a_token)).json()
        org_b_membership = next(o for o in mine if o["id"] == org_b_id)
        assert org_b_membership["role"] == "admin"

    def test_already_approved_member_rejected(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, _ = _create_org(client, super_token, "W1A", "orga", "Org A")
        users = client.get("/admin/users", headers=auth(super_token)).json()
        user_a = next(u for u in users if u["callsign"] == "W1A")
        resp = client.post(f"/admin/users/{user_a['id']}/orgs", headers=auth(super_token), json={
            "org_id": org_a_id,
        })
        assert resp.status_code == 400

    def test_non_super_admin_forbidden(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, admin_a_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        org_b_id, _ = _create_org(client, super_token, "W2B", "orgb", "Org B")
        users = client.get("/admin/users", headers=auth(super_token)).json()
        user_b = next(u for u in users if u["callsign"] == "W2B")
        resp = client.post(f"/admin/users/{user_b['id']}/orgs", headers=auth(admin_a_token), json={
            "org_id": org_a_id,
        })
        assert resp.status_code == 403

    def test_unknown_org_404(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, _ = _create_org(client, super_token, "W1A", "orga", "Org A")
        users = client.get("/admin/users", headers=auth(super_token)).json()
        user_a = next(u for u in users if u["callsign"] == "W1A")
        resp = client.post(f"/admin/users/{user_a['id']}/orgs", headers=auth(super_token), json={
            "org_id": 999999,
        })
        assert resp.status_code == 404


class TestReassignNet:
    def test_super_admin_moves_net(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, admin_a_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        org_b_id, _ = _create_org(client, super_token, "W2B", "orgb", "Org B")

        net = client.post("/nets", headers=auth(admin_a_token), json={"name": "Test Net"}).json()
        assert net["org_id"] == org_a_id

        move = client.patch(f"/admin/nets/{net['id']}/org", headers=auth(super_token), json={
            "org_id": org_b_id,
        })
        assert move.status_code == 200, move.text
        assert move.json()["org_id"] == org_b_id
        # Owner (W1A) is not a member of Org B
        assert move.json()["owner_not_member"] is True

        nets = client.get("/nets", headers=auth(super_token)).json()
        moved = next(n for n in nets if n["id"] == net["id"])
        assert moved["org_id"] == org_b_id

    def test_owner_not_member_false_when_owner_belongs_to_target_org(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, admin_a_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        org_b_id, _ = _create_org(client, super_token, "W2B", "orgb", "Org B")

        net = client.post("/nets", headers=auth(admin_a_token), json={"name": "Test Net"}).json()
        # Move the owner into Org B first, then the net
        users = client.get("/admin/users", headers=auth(super_token)).json()
        user_a = next(u for u in users if u["callsign"] == "W1A")
        client.patch(f"/admin/users/{user_a['id']}/org", headers=auth(super_token), json={"org_id": org_b_id})

        move = client.patch(f"/admin/nets/{net['id']}/org", headers=auth(super_token), json={"org_id": org_b_id})
        assert move.json()["owner_not_member"] is False

    def test_non_super_admin_forbidden(self, client):
        super_token = _bootstrap_super_admin(client)
        org_a_id, admin_a_token = _create_org(client, super_token, "W1A", "orga", "Org A")
        org_b_id, _ = _create_org(client, super_token, "W2B", "orgb", "Org B")
        net = client.post("/nets", headers=auth(admin_a_token), json={"name": "Test Net"}).json()
        resp = client.patch(f"/admin/nets/{net['id']}/org", headers=auth(admin_a_token), json={"org_id": org_b_id})
        assert resp.status_code == 403
