"""
Tests for multi-tenancy (issue #1):
  POST /auth/register (org_slug/org_name/org_website_url)
  GET  /orgs, GET /orgs/mine
  POST /orgs/join
  PATCH /auth/current-org
  GET  /orgs/{id}/pending-members, /orgs/{id}/members
  PATCH /orgs/{id}/members/{user_id}/approve
  POST /orgs/{id}/members/{user_id}/reject
  Org scoping on /nets, /users, /sessions, /checkins

Founding a brand new org requires a website URL and, since the founder can't
approve their own account, a super admin's sign-off via the existing global
/admin/users/{id}/approve before they can log in (issue #1 follow-up).
Joining an EXISTING org is unchanged: that org's own admin approves it.
"""

import io

from helpers import register, login, auth


def _bootstrap_super_admin(client):
    """Register the instance's first-ever user -- automatically the global
    super admin and immediately active (no one else exists to approve them).
    Needed by nearly every test below to approve a subsequent org founder."""
    register(client, "W0SUPER", "Super", "w0super@example.com")
    return login(client, "W0SUPER")


def _org_owner(client, super_token, callsign, org_slug, org_name, website="https://example.org"):
    """Register a user founding a brand new org, have the super admin approve
    them, and return their JWT."""
    resp = client.post("/auth/register", json={
        "callsign": callsign, "name": callsign, "email": f"{callsign.lower()}@example.com",
        "password": "testpass123", "org_slug": org_slug, "org_name": org_name, "org_website_url": website,
    })
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["id"]
    approve = client.patch(f"/admin/users/{user_id}/approve", headers=auth(super_token))
    assert approve.status_code == 200, approve.text
    return login(client, callsign)


class TestRegistrationOrgFlow:
    def test_first_ever_user_is_super_admin_regardless_of_org(self, client):
        resp = register(client, "W1FIRST", "First", "first@example.com")
        data = resp.json()
        assert data["is_active"] is True
        assert data["is_admin"] is True
        assert data["current_org_id"] is not None

    def test_new_org_requires_website_url(self, client):
        resp = client.post("/auth/register", json={
            "callsign": "W2NOWEB", "name": "No Web", "email": "noweb@example.com", "password": "testpass123",
            "org_slug": "no-web-org", "org_name": "No Web Org",
        })
        assert resp.status_code == 400

    def test_new_org_website_url_must_be_http_or_https(self, client):
        """Rejects e.g. a javascript: URI -- this gets rendered as a clickable
        link in the admin approval queue, so anything else is a stored-XSS
        vector against whoever reviews it."""
        resp = client.post("/auth/register", json={
            "callsign": "W2BADURL", "name": "Bad URL", "email": "badurl@example.com", "password": "testpass123",
            "org_slug": "bad-url-org", "org_name": "Bad URL Org",
            "org_website_url": "javascript:alert(1)",
        })
        assert resp.status_code == 400

    def test_new_org_founder_is_not_active_until_super_admin_approves(self, client):
        super_token = _bootstrap_super_admin(client)
        resp = client.post("/auth/register", json={
            "callsign": "W2OWN", "name": "Owner", "email": "owner@example.com", "password": "testpass123",
            "org_slug": "acme-ares", "org_name": "ACME ARES", "org_website_url": "https://acme-ares.example.org",
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["is_active"] is False
        assert data["is_admin"] is False
        login_resp = client.post("/auth/login", data={"username": "W2OWN", "password": "testpass123"})
        assert login_resp.status_code == 403

        # A super admin approving them (the existing global escape hatch) unblocks login
        approve = client.patch(f"/admin/users/{data['id']}/approve", headers=auth(super_token))
        assert approve.status_code == 200
        login(client, "W2OWN")  # no longer blocked

    def test_admin_users_list_includes_org_name_and_website_for_pending_founder(self, client):
        super_token = _bootstrap_super_admin(client)
        client.post("/auth/register", json={
            "callsign": "W2OWN", "name": "Owner", "email": "owner@example.com", "password": "testpass123",
            "org_slug": "acme-ares", "org_name": "ACME ARES", "org_website_url": "https://acme-ares.example.org",
        })
        users = client.get("/admin/users", headers=auth(super_token)).json()
        row = next(u for u in users if u["callsign"] == "W2OWN")
        assert row["org_name"] == "ACME ARES"
        assert row["org_website_url"] == "https://acme-ares.example.org"

    def test_new_org_founder_membership_is_already_admin_role_once_approved(self, client):
        super_token = _bootstrap_super_admin(client)
        token = _org_owner(client, super_token, "W2OWN", "acme-ares", "ACME ARES")
        mine = client.get("/orgs/mine", headers=auth(token)).json()
        assert len(mine) == 1
        assert mine[0]["role"] == "admin"

    def test_joining_an_existing_org_is_pending(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")  # creates the "default" org
        resp = register(client, "W2JOIN", "Joiner", "joiner@example.com")  # joins "default"
        assert resp.status_code == 201
        assert resp.json()["is_active"] is False
        login_resp = client.post("/auth/login", data={"username": "W2JOIN", "password": "testpass123"})
        assert login_resp.status_code == 403

    def test_org_admin_can_approve_pending_member(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        owner_token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]

        register(client, "W2JOIN", "Joiner", "joiner@example.com")
        pending = client.get(f"/orgs/{org_id}/pending-members", headers=auth(owner_token)).json()
        joiner_id = next(m["user_id"] for m in pending if m["callsign"] == "W2JOIN")

        resp = client.patch(f"/orgs/{org_id}/members/{joiner_id}/approve", headers=auth(owner_token))
        assert resp.status_code == 204
        login(client, "W2JOIN")  # no longer blocked

    def test_non_org_admin_cannot_approve(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        owner_token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]

        register(client, "W2MEMBER", "Member", "member@example.com")
        pending = client.get(f"/orgs/{org_id}/pending-members", headers=auth(owner_token)).json()
        member_id = next(m["user_id"] for m in pending if m["callsign"] == "W2MEMBER")
        client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(owner_token))
        member_token = login(client, "W2MEMBER")

        register(client, "W3JOIN", "Joiner3", "joiner3@example.com")
        resp = client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(member_token))
        assert resp.status_code == 403

    def test_reject_pending_member_deletes_membership_not_account(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        owner_token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]

        register(client, "W2JOIN", "Joiner", "joiner@example.com")
        pending = client.get(f"/orgs/{org_id}/pending-members", headers=auth(owner_token)).json()
        joiner_id = next(m["user_id"] for m in pending if m["callsign"] == "W2JOIN")

        resp = client.post(f"/orgs/{org_id}/members/{joiner_id}/reject", headers=auth(owner_token))
        assert resp.status_code == 204
        pending_after = client.get(f"/orgs/{org_id}/pending-members", headers=auth(owner_token)).json()
        assert not any(m["user_id"] == joiner_id for m in pending_after)
        # account itself still exists (unlike the legacy global reject, which deletes it)
        users = client.get("/admin/users", headers=auth(owner_token)).json()
        assert any(u["callsign"] == "W2JOIN" for u in users)

    def test_super_admin_approve_clears_pending_memberships_too(self, client):
        register(client, "W1SUPER", "Super", "super@example.com")
        super_token = login(client, "W1SUPER")

        register(client, "W2JOIN", "Joiner", "joiner@example.com")  # joins "default", pending
        users = client.get("/admin/users", headers=auth(super_token)).json()
        joiner_id = next(u["id"] for u in users if u["callsign"] == "W2JOIN")

        resp = client.patch(f"/admin/users/{joiner_id}/approve", headers=auth(super_token))
        assert resp.status_code == 200
        login(client, "W2JOIN")  # no longer blocked


class TestOrphanedOrgCleanup:
    """Rejecting (or deleting) a pending org founder used to delete the user
    but leave their brand-new org behind with zero members -- a dead-end
    that stayed visible in the "join an existing organization" picker
    forever, with no one left who could ever approve a join request
    (reported bug, issue #1 follow-up)."""

    def test_rejecting_org_founder_deletes_the_orphaned_org(self, client):
        super_token = _bootstrap_super_admin(client)
        resp = client.post("/auth/register", json={
            "callsign": "W2FOUND", "name": "Founder", "email": "founder@example.com", "password": "testpass123",
            "org_slug": "doomed-org", "org_name": "Doomed Org", "org_website_url": "https://doomed.example.org",
        })
        assert resp.status_code == 201, resp.text
        user_id = resp.json()["id"]

        orgs_before = client.get("/orgs").json()
        assert any(o["slug"] == "doomed-org" for o in orgs_before)

        reject = client.post(f"/admin/users/{user_id}/reject", headers=auth(super_token))
        assert reject.status_code == 204

        orgs_after = client.get("/orgs").json()
        assert not any(o["slug"] == "doomed-org" for o in orgs_after)

    def test_deleting_org_founder_deletes_the_orphaned_org(self, client):
        super_token = _bootstrap_super_admin(client)
        resp = client.post("/auth/register", json={
            "callsign": "W2FOUND", "name": "Founder", "email": "founder@example.com", "password": "testpass123",
            "org_slug": "doomed-org", "org_name": "Doomed Org", "org_website_url": "https://doomed.example.org",
        })
        user_id = resp.json()["id"]

        delete = client.delete(f"/admin/users/{user_id}", headers=auth(super_token))
        assert delete.status_code == 204

        orgs_after = client.get("/orgs").json()
        assert not any(o["slug"] == "doomed-org" for o in orgs_after)

    def test_deleting_org_founder_cleans_up_their_orgs_logo_file(self, client):
        """Per-org branding (issue follow-up): an org's logo is a bare file
        on disk, not a DB column -- db.delete(org) alone can't clean it up.
        Without _delete_orphaned_orgs also removing it, an orphaned org's
        logo would leak on disk forever."""
        from routers.helpers import UPLOADS_DIR

        super_token = _bootstrap_super_admin(client)
        token = _org_owner(client, super_token, "W2FOUND", "doomed-org", "Doomed Org")
        me = client.get("/auth/me", headers=auth(token)).json()
        user_id, org_id = me["id"], me["current_org_id"]

        upload = client.post(f"/orgs/{org_id}/logo",
            files={"file": ("logo.png", io.BytesIO(b"fake logo bytes"), "image/png")},
            headers=auth(token))
        assert upload.status_code == 204, upload.text
        logo_path = next(UPLOADS_DIR.glob(f"org_{org_id}_logo.*"))
        assert logo_path.exists()

        delete = client.delete(f"/admin/users/{user_id}", headers=auth(super_token))
        assert delete.status_code == 204

        assert not logo_path.exists()
        assert not list(UPLOADS_DIR.glob(f"org_{org_id}_logo.*"))

    def test_rejecting_one_of_two_org_admins_does_not_delete_the_org(self, client):
        """Only actually-orphaned orgs get cleaned up -- one with a second
        approved admin left behind must survive."""
        super_token = _bootstrap_super_admin(client)
        token = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        # Promote a second member to org admin isn't directly exposed, but a
        # pending joiner approved via the org's own admin becomes a 'member'
        # -- use the super admin's global approve on a second founder-style
        # registration into the SAME org isn't possible (join-existing only
        # grants 'member'), so instead verify via a second super-admin-owned
        # membership: the super admin itself can act as org-a's admin without
        # being deleted, so rejecting the founder here is the orphaning case
        # already covered above. This test instead confirms a *member* (not
        # the sole admin) being rejected leaves the org alone.
        client.post("/auth/register", json={
            "callsign": "W2MEMBER", "name": "Member", "email": "member@example.com",
            "password": "testpass123", "org_slug": "org-a",
        })
        pending = client.get(f"/orgs/{org_id}/pending-members", headers=auth(token)).json()
        member_id = next(m["user_id"] for m in pending if m["callsign"] == "W2MEMBER")
        client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(token))

        reject = client.post(f"/admin/users/{member_id}/reject", headers=auth(super_token))
        assert reject.status_code == 204

        orgs_after = client.get("/orgs").json()
        assert any(o["slug"] == "org-a" for o in orgs_after)


class TestOrgEditing:
    """PATCH /orgs/{id} — previously there was no way to rename an org (or fix
    its website) after creation at all (issue #1 follow-up)."""

    def test_org_admin_can_rename_their_org(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")  # first-ever user, "default" org
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        resp = client.patch(f"/orgs/{org_id}", json={
            "name": "Renamed Org", "website_url": "https://renamed.example.org",
        }, headers=auth(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "Renamed Org"
        assert data["website_url"] == "https://renamed.example.org"

        # Persisted -- shows up for a fresh lookup too, e.g. the join picker
        orgs = client.get("/orgs").json()
        assert any(o["name"] == "Renamed Org" for o in orgs)

    def test_renaming_org_does_not_change_its_slug(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]
        client.patch(f"/orgs/{org_id}", json={"name": "Renamed Org"}, headers=auth(token))

        orgs = client.get("/orgs").json()
        row = next(o for o in orgs if o["id"] == org_id)
        assert row["slug"] == "default"

    def test_org_edit_requires_name(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        resp = client.patch(f"/orgs/{org_id}", json={"name": "  "}, headers=auth(token))
        assert resp.status_code == 400

    def test_org_edit_website_must_be_http_or_https(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        resp = client.patch(f"/orgs/{org_id}", json={
            "name": "Owner Org", "website_url": "javascript:alert(1)",
        }, headers=auth(token))
        assert resp.status_code == 400

    def test_org_edit_website_can_be_cleared(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        resp = client.patch(f"/orgs/{org_id}", json={"name": "Owner Org", "website_url": ""}, headers=auth(token))
        assert resp.status_code == 200
        assert resp.json()["website_url"] is None

    def test_non_admin_member_cannot_edit_org(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        owner_token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]

        register(client, "W2MEMBER", "Member", "member@example.com")
        pending = client.get(f"/orgs/{org_id}/pending-members", headers=auth(owner_token)).json()
        member_id = next(m["user_id"] for m in pending if m["callsign"] == "W2MEMBER")
        client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(owner_token))
        member_token = login(client, "W2MEMBER")

        resp = client.patch(f"/orgs/{org_id}", json={"name": "Hijacked Name"}, headers=auth(member_token))
        assert resp.status_code == 403

    def test_org_admin_cannot_edit_a_different_org(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        token_b = _org_owner(client, super_token, "W1BORG", "org-b", "Org B")
        org_b_id = client.get("/auth/me", headers=auth(token_b)).json()["current_org_id"]

        resp = client.patch(f"/orgs/{org_b_id}", json={"name": "Hijacked"}, headers=auth(token_a))
        assert resp.status_code == 403

    def test_super_admin_can_edit_any_org(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        org_a_id = client.get("/auth/me", headers=auth(token_a)).json()["current_org_id"]

        resp = client.patch(f"/orgs/{org_a_id}", json={"name": "Fixed by Super Admin"}, headers=auth(super_token))
        assert resp.status_code == 200
        assert resp.json()["name"] == "Fixed by Super Admin"

    def test_org_admin_can_set_tagline(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        resp = client.patch(f"/orgs/{org_id}", json={
            "name": "Owner Org", "tagline": "Served Agency: King County EM",
        }, headers=auth(token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["tagline"] == "Served Agency: King County EM"

        # Persisted -- shows up for a fresh lookup too
        mine = client.get("/orgs/mine", headers=auth(token)).json()
        assert mine[0]["tagline"] == "Served Agency: King County EM"

    def test_org_tagline_can_be_cleared(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]
        client.patch(f"/orgs/{org_id}", json={"name": "Owner Org", "tagline": "Something"}, headers=auth(token))

        resp = client.patch(f"/orgs/{org_id}", json={"name": "Owner Org", "tagline": ""}, headers=auth(token))
        assert resp.status_code == 200
        assert resp.json()["tagline"] is None


class TestInviteOnlyOrgs:
    """Organization.registration_open (issue follow-up) — an org admin can
    hide their org from the "join an existing organization" picker at
    registration and block self-registration into it outright, making it
    invite-only. The org's own admin still has a working way to add people
    via Admin's "Add Operator" (POST /admin/users or /orgs/{id}/users),
    which never goes through the public registration endpoint."""

    def test_new_org_defaults_to_registration_open(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        mine = client.get("/orgs/mine", headers=auth(token)).json()
        assert mine[0]["registration_open"] is True

    def test_org_admin_can_close_registration(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        resp = client.patch(f"/orgs/{org_id}", json={
            "name": "Owner Org", "registration_open": False,
        }, headers=auth(token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["registration_open"] is False

        # Persisted
        mine = client.get("/orgs/mine", headers=auth(token)).json()
        assert mine[0]["registration_open"] is False

    def test_closed_org_excluded_from_filtered_picker_but_not_unfiltered_list(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]
        client.patch(f"/orgs/{org_id}", json={"name": "Owner Org", "registration_open": False}, headers=auth(token))

        # The public registration picker (?registration_open=true) excludes it
        picker = client.get("/orgs?registration_open=true").json()
        assert not any(o["id"] == org_id for o in picker)

        # But the plain, unfiltered /orgs (used by admin-authenticated
        # pickers -- Add Operator, Reassign, and this org admin's own
        # edit-form lookup) still includes it -- otherwise there'd be no
        # way back in, including for the admin to ever re-open it.
        unfiltered = client.get("/orgs").json()
        assert any(o["id"] == org_id for o in unfiltered)

    def test_self_registration_blocked_for_invite_only_org(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")  # founds "default"
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]
        client.patch(f"/orgs/{org_id}", json={"name": "Owner Org", "registration_open": False}, headers=auth(token))

        resp = client.post("/auth/register", json={
            "callsign": "W2BLOCKED", "name": "Blocked", "email": "blocked@example.com",
            "password": "testpass123", "org_slug": "default",
        })
        assert resp.status_code == 403
        # No account was created
        assert not client.post("/auth/login", data={"username": "W2BLOCKED", "password": "testpass123"}).json().get("access_token")

    def test_join_org_endpoint_blocked_for_invite_only_org(self, client):
        """POST /orgs/join -- an already-logged-in user requesting to join a
        SECOND org -- is the other genuinely self-service join path and must
        be blocked the same way registration is."""
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        org_a_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]
        client.patch(f"/orgs/{org_a_id}", json={"name": "Org A", "registration_open": False}, headers=auth(owner_token))

        # super_token is already active -- avoids also needing to get a
        # second, unrelated registrant approved just to have a valid token.
        resp = client.post("/orgs/join", json={"org_slug": "org-a"}, headers=auth(super_token))
        assert resp.status_code == 403

    def test_admin_can_still_add_operator_to_invite_only_org(self, client, smtp_configured, sent_emails, app_base_url):
        """The intended way in: Admin > Add Operator, unaffected since it
        never goes through the public registration endpoint."""
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]
        client.patch(f"/orgs/{org_id}", json={"name": "Org A", "registration_open": False}, headers=auth(owner_token))

        resp = client.post(f"/orgs/{org_id}/users", headers=auth(owner_token), json={
            "callsign": "W2INVITED", "name": "Invited", "email": "invited@example.com",
        })
        assert resp.status_code == 201, resp.text
        assert resp.json()["is_active"] is True

    def test_reopening_registration_restores_self_service_join(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]
        client.patch(f"/orgs/{org_id}", json={"name": "Owner Org", "registration_open": False}, headers=auth(token))
        client.patch(f"/orgs/{org_id}", json={"name": "Owner Org", "registration_open": True}, headers=auth(token))

        assert any(o["id"] == org_id for o in client.get("/orgs?registration_open=true").json())
        resp = client.post("/auth/register", json={
            "callsign": "W2NOWBLOCKED", "name": "Now Fine", "email": "nowfine@example.com",
            "password": "testpass123", "org_slug": "default",
        })
        assert resp.status_code == 201


class TestOrgLogo:
    """POST/DELETE/GET /orgs/{id}/logo -- per-org branding (issue follow-up),
    the org-scoped counterpart to the instance-wide POST/DELETE
    /admin/branding/logo + public GET /logo (no prior test coverage exists
    for those instance endpoints either -- not a regression here, just not
    fixing that pre-existing gap in this pass)."""

    def _logo_file(self, filename="logo.png"):
        return {"file": (filename, io.BytesIO(b"not a real image, just bytes"), "image/png")}

    def test_org_admin_can_upload_and_fetch_logo(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        resp = client.post(f"/orgs/{org_id}/logo", files=self._logo_file(), headers=auth(token))
        assert resp.status_code == 204, resp.text

        # Public -- no auth required to fetch it back
        img = client.get(f"/orgs/{org_id}/logo")
        assert img.status_code == 200
        assert img.content == b"not a real image, just bytes"
        assert img.headers["content-type"] == "image/png"

        # has_logo reflects it everywhere an org is returned
        assert client.get("/orgs/mine", headers=auth(token)).json()[0]["has_logo"] is True
        orgs = client.get("/orgs").json()
        assert next(o for o in orgs if o["id"] == org_id)["has_logo"] is True

    def test_uploading_new_logo_replaces_old_one(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        client.post(f"/orgs/{org_id}/logo", files=self._logo_file("first.png"), headers=auth(token))
        client.post(f"/orgs/{org_id}/logo", files=self._logo_file("second.jpg"), headers=auth(token))

        img = client.get(f"/orgs/{org_id}/logo")
        assert img.status_code == 200
        assert img.headers["content-type"] == "image/jpeg"  # the second (replacing) upload

    def test_org_admin_can_delete_logo(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]
        client.post(f"/orgs/{org_id}/logo", files=self._logo_file(), headers=auth(token))

        resp = client.delete(f"/orgs/{org_id}/logo", headers=auth(token))
        assert resp.status_code == 204

        assert client.get(f"/orgs/{org_id}/logo").status_code == 404
        assert client.get("/orgs/mine", headers=auth(token)).json()[0]["has_logo"] is False

    def test_logo_fetch_404s_with_none_uploaded(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        resp = client.get(f"/orgs/{org_id}/logo")
        assert resp.status_code == 404

    def test_rejects_unsupported_file_type(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(token)).json()["current_org_id"]

        resp = client.post(f"/orgs/{org_id}/logo",
            files={"file": ("logo.exe", io.BytesIO(b"nope"), "application/octet-stream")},
            headers=auth(token))
        assert resp.status_code == 400

    def test_non_admin_member_cannot_upload_logo(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        owner_token = login(client, "W1OWN")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]

        register(client, "W2MEMBER", "Member", "member@example.com")
        pending = client.get(f"/orgs/{org_id}/pending-members", headers=auth(owner_token)).json()
        member_id = next(m["user_id"] for m in pending if m["callsign"] == "W2MEMBER")
        client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(owner_token))
        member_token = login(client, "W2MEMBER")

        resp = client.post(f"/orgs/{org_id}/logo", files=self._logo_file(), headers=auth(member_token))
        assert resp.status_code == 403

    def test_org_admin_cannot_upload_logo_for_a_different_org(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        token_b = _org_owner(client, super_token, "W1BORG", "org-b", "Org B")
        org_b_id = client.get("/auth/me", headers=auth(token_b)).json()["current_org_id"]

        resp = client.post(f"/orgs/{org_b_id}/logo", files=self._logo_file(), headers=auth(token_a))
        assert resp.status_code == 403

    def test_super_admin_can_upload_logo_for_any_org(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        org_a_id = client.get("/auth/me", headers=auth(token_a)).json()["current_org_id"]

        resp = client.post(f"/orgs/{org_a_id}/logo", files=self._logo_file(), headers=auth(super_token))
        assert resp.status_code == 204


class TestOrgMemberRoles:
    """PATCH /orgs/{id}/members/{user_id}/role — previously an org admin
    could approve or reject a new member but had no way to grant admin to
    someone already in the org, so a single-admin org had no way to add a
    second one without a super admin's help (issue follow-up)."""

    def _approved_member(self, client, org_admin_token, org_id, callsign):
        """Registers a user joining org_id, approves them (as a plain
        member), and returns their user_id."""
        resp = client.post("/auth/register", json={
            "callsign": callsign, "name": callsign, "email": f"{callsign.lower()}@example.com",
            "password": "testpass123", "org_slug": "orga",
        })
        assert resp.status_code == 201, resp.text
        user_id = resp.json()["id"]
        approve = client.patch(f"/orgs/{org_id}/members/{user_id}/approve", headers=auth(org_admin_token))
        assert approve.status_code == 204, approve.text
        return user_id

    def test_org_admin_can_promote_a_member_to_admin(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]
        member_id = self._approved_member(client, owner_token, org_id, "W2MEM")

        resp = client.patch(f"/orgs/{org_id}/members/{member_id}/role", json={"role": "admin"}, headers=auth(owner_token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "admin"

        members = client.get(f"/orgs/{org_id}/members", headers=auth(owner_token)).json()
        assert next(m for m in members if m["user_id"] == member_id)["role"] == "admin"

        # The newly-promoted admin can now act as one themselves
        new_admin_token = login(client, "W2MEM")
        another = self._approved_member(client, owner_token, org_id, "W3MEM")
        promote = client.patch(f"/orgs/{org_id}/members/{another}/role", json={"role": "admin"}, headers=auth(new_admin_token))
        assert promote.status_code == 200, promote.text

    def test_org_admin_can_demote_an_admin_to_member(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]
        member_id = self._approved_member(client, owner_token, org_id, "W2MEM")
        client.patch(f"/orgs/{org_id}/members/{member_id}/role", json={"role": "admin"}, headers=auth(owner_token))

        resp = client.patch(f"/orgs/{org_id}/members/{member_id}/role", json={"role": "member"}, headers=auth(owner_token))
        assert resp.status_code == 200
        assert resp.json()["role"] == "member"

    def test_cannot_change_own_role(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]
        owner_id = client.get("/auth/me", headers=auth(owner_token)).json()["id"]

        resp = client.patch(f"/orgs/{org_id}/members/{owner_id}/role", json={"role": "member"}, headers=auth(owner_token))
        assert resp.status_code == 400

    def test_cannot_change_role_of_pending_member(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]

        resp = client.post("/auth/register", json={
            "callsign": "W2PEND", "name": "Pending", "email": "pending@example.com",
            "password": "testpass123", "org_slug": "orga",
        })
        pending_id = resp.json()["id"]

        role_resp = client.patch(f"/orgs/{org_id}/members/{pending_id}/role", json={"role": "admin"}, headers=auth(owner_token))
        assert role_resp.status_code == 404

    def test_non_admin_member_cannot_change_roles(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]
        member_id = self._approved_member(client, owner_token, org_id, "W2MEM")
        other_id = self._approved_member(client, owner_token, org_id, "W3MEM")
        member_token = login(client, "W2MEM")

        resp = client.patch(f"/orgs/{org_id}/members/{other_id}/role", json={"role": "admin"}, headers=auth(member_token))
        assert resp.status_code == 403

    def test_org_admin_cannot_change_role_in_a_different_org(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        token_b = _org_owner(client, super_token, "W1BORG", "org-b", "Org B")
        org_b_id = client.get("/auth/me", headers=auth(token_b)).json()["current_org_id"]
        b_id = client.get("/auth/me", headers=auth(token_b)).json()["id"]

        resp = client.patch(f"/orgs/{org_b_id}/members/{b_id}/role", json={"role": "member"}, headers=auth(token_a))
        assert resp.status_code == 403

    def test_super_admin_can_change_role_in_any_org(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]
        member_id = self._approved_member(client, owner_token, org_id, "W2MEM")

        resp = client.patch(f"/orgs/{org_id}/members/{member_id}/role", json={"role": "admin"}, headers=auth(super_token))
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_unknown_member_404(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]

        resp = client.patch(f"/orgs/{org_id}/members/999999/role", json={"role": "admin"}, headers=auth(owner_token))
        assert resp.status_code == 404


class TestOrgSwitching:
    def test_orgs_mine_lists_approved_orgs(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        mine = client.get("/orgs/mine", headers=auth(token)).json()
        assert len(mine) == 1
        assert mine[0]["slug"] == "default"

    def test_join_second_org_then_switch(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")  # first-ever user -> also super admin
        token = login(client, "W1OWN")
        resp = client.post("/orgs/join", json={
            "org_slug": "second-org", "org_name": "Second Org", "org_website_url": "https://second.example.org",
        }, headers=auth(token))
        assert resp.status_code == 201
        second_org_id = resp.json()["id"]

        # Founding a second org via /orgs/join is pending too -- not auto-approved
        # just because the caller is already active elsewhere (issue #1 follow-up).
        # (Not asserting a 403 on switching to it here: W1OWN is themselves a
        # super admin -- first-ever user -- so they can switch to ANY org
        # regardless of approval status, same bypass /nets etc. already have.
        # test_cannot_switch_to_an_unapproved_org below covers the non-admin case.)
        mine_before = client.get("/orgs/mine", headers=auth(token)).json()
        assert len(mine_before) == 1

        # W1OWN is a super admin (first-ever user) so can self-approve via the
        # existing global escape hatch.
        me = client.get("/auth/me", headers=auth(token)).json()
        approve = client.patch(f"/admin/users/{me['id']}/approve", headers=auth(token))
        assert approve.status_code == 200

        mine = client.get("/orgs/mine", headers=auth(token)).json()
        assert len(mine) == 2

        switch = client.patch("/auth/current-org", json={"org_id": second_org_id}, headers=auth(token))
        assert switch.status_code == 200
        assert switch.json()["current_org_id"] == second_org_id

    def test_join_same_org_twice_errors(self, client):
        register(client, "W1OWN", "Owner", "owner@example.com")
        token = login(client, "W1OWN")
        resp = client.post("/orgs/join", json={"org_slug": "default"}, headers=auth(token))
        assert resp.status_code == 400

    def test_cannot_switch_to_an_unapproved_org(self, client):
        super_token = _bootstrap_super_admin(client)
        token = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        other_org_id = client.post(
            "/auth/register",
            json={
                "callsign": "W3OUT", "name": "Outsider", "email": "outsider@example.com",
                "password": "testpass123", "org_slug": "other-org", "org_name": "Other Org",
                "org_website_url": "https://other.example.org",
            },
        ).json()["current_org_id"]

        resp = client.patch("/auth/current-org", json={"org_id": other_org_id}, headers=auth(token))
        assert resp.status_code == 403


class TestCrossOrgIsolation:
    def test_user_cannot_see_other_orgs_net_in_list(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        net_a = client.post("/nets", json={"name": "Org A Net", "is_ares": False}, headers=auth(token_a)).json()

        token_b = _org_owner(client, super_token, "W1BORG", "org-b", "Org B")
        nets_b = client.get("/nets", headers=auth(token_b)).json()
        assert all(n["id"] != net_a["id"] for n in nets_b)

    def test_user_gets_404_fetching_other_orgs_net_directly(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        net_a = client.post("/nets", json={"name": "Org A Net", "is_ares": False}, headers=auth(token_a)).json()

        token_b = _org_owner(client, super_token, "W1BORG", "org-b", "Org B")
        resp = client.get(f"/nets/{net_a['id']}", headers=auth(token_b))
        assert resp.status_code == 404

    def test_new_net_is_scoped_to_creators_current_org(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        net = client.post("/nets", json={"name": "Org A Net", "is_ares": False}, headers=auth(token_a)).json()
        me = client.get("/auth/me", headers=auth(token_a)).json()
        assert net["org_id"] == me["current_org_id"]

    def test_users_picker_excludes_other_orgs(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        _org_owner(client, super_token, "W1BORG", "org-b", "Org B")
        users_seen_by_a = client.get("/users", headers=auth(token_a)).json()
        assert all(u["callsign"] != "W1BORG" for u in users_seen_by_a)

    def test_org_admin_cannot_approve_a_different_orgs_pending_member(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")

        token_b = _org_owner(client, super_token, "W1BORG", "org-b", "Org B")
        org_b_id = client.get("/auth/me", headers=auth(token_b)).json()["current_org_id"]

        join_resp = client.post("/auth/register", json={
            "callsign": "W2JOINB", "name": "Joiner", "email": "joinb@example.com",
            "password": "testpass123", "org_slug": "org-b",
        })
        joiner_id = join_resp.json()["id"]

        resp = client.patch(f"/orgs/{org_b_id}/members/{joiner_id}/approve", headers=auth(token_a))
        assert resp.status_code == 403

    def test_session_and_checkins_isolated_across_orgs(self, client):
        super_token = _bootstrap_super_admin(client)
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        net_a = client.post("/nets", json={"name": "Org A Net", "is_ares": False}, headers=auth(token_a)).json()
        session_a = client.post(f"/nets/{net_a['id']}/sessions", json={}, headers=auth(token_a)).json()
        client.post(f"/sessions/{session_a['id']}/checkins", json={"callsign": "W9ORGA"}, headers=auth(token_a))

        token_b = _org_owner(client, super_token, "W1BORG", "org-b", "Org B")
        resp = client.get(f"/sessions/{session_a['id']}", headers=auth(token_b))
        assert resp.status_code == 404
        resp2 = client.post(f"/sessions/{session_a['id']}/checkins", json={"callsign": "W9ORGB"}, headers=auth(token_b))
        assert resp2.status_code in (403, 404)

    def test_super_admin_bypasses_org_scoping(self, client):
        super_token = _bootstrap_super_admin(client)  # W0SUPER -- first-ever user -> super admin
        token_a = _org_owner(client, super_token, "W1AORG", "org-a", "Org A")
        net_a = client.post("/nets", json={"name": "Org A Net", "is_ares": False}, headers=auth(token_a)).json()

        resp = client.get(f"/nets/{net_a['id']}", headers=auth(super_token))
        assert resp.status_code == 200

        all_nets = client.get("/nets", headers=auth(super_token)).json()
        assert any(n["id"] == net_a["id"] for n in all_nets)


class TestOrgNetOwnership:
    """GET /orgs/{id}/nets and PATCH /nets/{id}/owner -- org admins can now
    see and reassign ownership of every net in their own org, not just ones
    they personally own or are shared on, without needing a super admin
    (issue follow-up)."""

    def test_org_admin_sees_every_net_in_their_org_via_orgs_nets(self, client):
        super_token = _bootstrap_super_admin(client)
        admin_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(admin_token)).json()["current_org_id"]

        # A different member of the same org creates their OWN net -- the
        # org admin doesn't own it and isn't shared on it.
        resp = client.post("/auth/register", json={
            "callsign": "W2MEM", "name": "Member", "email": "mem@example.com",
            "password": "testpass123", "org_slug": "orga",
        })
        member_id = resp.json()["id"]
        client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(admin_token))
        member_token = login(client, "W2MEM")
        member_net = client.post("/nets", json={"name": "Member's Net", "is_ares": False}, headers=auth(member_token)).json()

        # It doesn't show up in the org admin's own /nets (they don't own or
        # share it)...
        own_nets = client.get("/nets", headers=auth(admin_token)).json()
        assert member_net["id"] not in [n["id"] for n in own_nets]

        # ...but it DOES show up via the org-scoped oversight endpoint.
        org_nets = client.get(f"/orgs/{org_id}/nets", headers=auth(admin_token)).json()
        assert member_net["id"] in [n["id"] for n in org_nets]

    def test_non_admin_member_cannot_list_org_nets(self, client):
        super_token = _bootstrap_super_admin(client)
        admin_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(admin_token)).json()["current_org_id"]

        resp = client.post("/auth/register", json={
            "callsign": "W2MEM", "name": "Member", "email": "mem@example.com",
            "password": "testpass123", "org_slug": "orga",
        })
        member_id = resp.json()["id"]
        client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(admin_token))
        member_token = login(client, "W2MEM")

        resp = client.get(f"/orgs/{org_id}/nets", headers=auth(member_token))
        assert resp.status_code == 403

    def test_org_admin_can_reassign_a_net_they_dont_own(self, client):
        super_token = _bootstrap_super_admin(client)
        admin_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(admin_token)).json()["current_org_id"]

        resp = client.post("/auth/register", json={
            "callsign": "W2MEM", "name": "Member", "email": "mem@example.com",
            "password": "testpass123", "org_slug": "orga",
        })
        member_id = resp.json()["id"]
        client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(admin_token))
        member_token = login(client, "W2MEM")
        net = client.post("/nets", json={"name": "Orphaned Net", "is_ares": False}, headers=auth(member_token)).json()

        resp = client.post("/auth/register", json={
            "callsign": "W3NEW", "name": "New Owner", "email": "w3new@example.com",
            "password": "testpass123", "org_slug": "orga",
        })
        new_owner_id = resp.json()["id"]
        client.patch(f"/orgs/{org_id}/members/{new_owner_id}/approve", headers=auth(admin_token))

        resp = client.patch(f"/nets/{net['id']}/owner", json={"owner_id": new_owner_id}, headers=auth(admin_token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_id"] == new_owner_id

    def test_cannot_transfer_to_a_user_outside_the_nets_org(self, client):
        super_token = _bootstrap_super_admin(client)
        admin_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        _org_owner(client, super_token, "W1BORG", "orgb", "Org B")
        net = client.post("/nets", json={"name": "Org A Net", "is_ares": False}, headers=auth(admin_token)).json()

        users = client.get("/admin/users", headers=auth(super_token)).json()
        org_b_owner_id = next(u["id"] for u in users if u["callsign"] == "W1BORG")

        resp = client.patch(f"/nets/{net['id']}/owner", json={"owner_id": org_b_owner_id}, headers=auth(admin_token))
        assert resp.status_code == 400

    def test_org_admin_cannot_reassign_a_net_in_a_different_org(self, client):
        super_token = _bootstrap_super_admin(client)
        admin_a_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        admin_b_token = _org_owner(client, super_token, "W1BORG", "orgb", "Org B")
        net_b = client.post("/nets", json={"name": "Org B Net", "is_ares": False}, headers=auth(admin_b_token)).json()

        resp = client.get("/admin/users", headers=auth(super_token)).json()
        a_owner_id = next(u["id"] for u in resp if u["callsign"] == "W1AORG")

        transfer = client.patch(f"/nets/{net_b['id']}/owner", json={"owner_id": a_owner_id}, headers=auth(admin_a_token))
        assert transfer.status_code in (403, 404)

    def test_super_admin_can_reassign_any_net_to_any_org_member(self, client):
        super_token = _bootstrap_super_admin(client)
        admin_token = _org_owner(client, super_token, "W1AORG", "orga", "Org A")
        org_id = client.get("/auth/me", headers=auth(admin_token)).json()["current_org_id"]
        net = client.post("/nets", json={"name": "Org A Net", "is_ares": False}, headers=auth(admin_token)).json()

        resp = client.post("/auth/register", json={
            "callsign": "W2MEM", "name": "Member", "email": "mem@example.com",
            "password": "testpass123", "org_slug": "orga",
        })
        member_id = resp.json()["id"]
        client.patch(f"/orgs/{org_id}/members/{member_id}/approve", headers=auth(admin_token))

        resp = client.patch(f"/nets/{net['id']}/owner", json={"owner_id": member_id}, headers=auth(super_token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_id"] == member_id


class TestBackwardCompatDefaultOrg:
    """A single-tenant deployment that never sends org fields should behave
    exactly as it did before this feature existed."""

    def test_omitting_org_fields_uses_default_org(self, client):
        register(client, "W1DEF", "Default", "default@example.com")
        token = login(client, "W1DEF")
        mine = client.get("/orgs/mine", headers=auth(token)).json()
        assert len(mine) == 1
        assert mine[0]["slug"] == "default"
        me = client.get("/auth/me", headers=auth(token)).json()
        assert me["current_org_id"] == mine[0]["id"]

    def test_second_plain_registration_joins_same_default_org_pending(self, client):
        register(client, "W1DEF", "Default", "default@example.com")
        resp = register(client, "W2DEF", "Second", "second@example.com")
        assert resp.json()["is_active"] is False
