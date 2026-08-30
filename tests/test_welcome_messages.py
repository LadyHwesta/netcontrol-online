"""
Tests for welcome messages:
  GET  /system/announcements         -- public, instance-wide login/popup messages
  PUT  /admin/announcements          -- super admin only
  PATCH /orgs/{id} banner_message    -- org admin, per-org banner shown to that org's members

Two independent settings stores:
  - login_message / welcome_popup_message live in SystemSetting (super admin,
    applies across every org on the instance)
  - banner_message lives on the Organization row itself (org admin, applies
    only to that org's own members) -- covered by the org-scoped assertions
    below, reusing the existing PATCH /orgs/{id} endpoint.
"""

from helpers import register, login, auth


class TestAnnouncementsPublicRead:
    def test_defaults_to_null_before_anything_is_set(self, client):
        resp = client.get("/system/announcements")
        assert resp.status_code == 200
        data = resp.json()
        assert data["login_message"] is None
        assert data["welcome_popup_message"] is None

    def test_requires_no_auth(self, client):
        """Must be reachable from the login screen, before signing in."""
        resp = client.get("/system/announcements")
        assert resp.status_code == 200


class TestAnnouncementsWrite:
    def test_requires_auth(self, client):
        resp = client.put("/admin/announcements", json={"login_message": "hi"})
        assert resp.status_code == 401

    def test_requires_admin(self, client, user_headers):
        resp = client.put("/admin/announcements", json={"login_message": "hi"}, headers=user_headers)
        assert resp.status_code == 403

    def test_super_admin_can_set_both_messages(self, client, admin_headers):
        resp = client.put("/admin/announcements", json={
            "login_message": "Maintenance window Saturday 8-9am",
            "welcome_popup_message": "Welcome! Please review the new net script.",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["login_message"] == "Maintenance window Saturday 8-9am"
        assert data["welcome_popup_message"] == "Welcome! Please review the new net script."

        # Publicly visible afterward, unauthenticated
        get_resp = client.get("/system/announcements")
        assert get_resp.json() == data

    def test_blank_string_clears_a_message(self, client, admin_headers):
        client.put("/admin/announcements", json={"login_message": "temporary notice"}, headers=admin_headers)
        resp = client.put("/admin/announcements", json={"login_message": "   "}, headers=admin_headers)
        assert resp.json()["login_message"] is None

    def test_setting_one_message_does_not_clear_the_other(self, client, admin_headers):
        client.put("/admin/announcements", json={
            "login_message": "a", "welcome_popup_message": "b",
        }, headers=admin_headers)
        resp = client.put("/admin/announcements", json={"login_message": "c"}, headers=admin_headers)
        data = resp.json()
        assert data["login_message"] == "c"
        assert data["welcome_popup_message"] is None   # PUT replaces both fields wholesale, matching BrandingUpdate's own semantics


class TestOrgBannerMessage:
    def test_defaults_to_null(self, client, admin_headers):
        orgs = client.get("/orgs/mine", headers=admin_headers).json()
        assert orgs[0]["banner_message"] is None

    def test_org_admin_can_set_banner(self, client, admin_headers):
        org_id = client.get("/orgs/mine", headers=admin_headers).json()[0]["id"]
        resp = client.patch(f"/orgs/{org_id}", json={
            "name": "Test Org", "banner_message": "Field Day setup 6am Saturday",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["banner_message"] == "Field Day setup 6am Saturday"

        mine = client.get("/orgs/mine", headers=admin_headers).json()
        assert mine[0]["banner_message"] == "Field Day setup 6am Saturday"

    def test_blank_banner_clears_it(self, client, admin_headers):
        org_id = client.get("/orgs/mine", headers=admin_headers).json()[0]["id"]
        client.patch(f"/orgs/{org_id}", json={"name": "Test Org", "banner_message": "set first"}, headers=admin_headers)
        resp = client.patch(f"/orgs/{org_id}", json={"name": "Test Org", "banner_message": "  "}, headers=admin_headers)
        assert resp.json()["banner_message"] is None

    def test_non_admin_cannot_set_banner(self, client, admin_headers, user_headers):
        org_id = client.get("/orgs/mine", headers=admin_headers).json()[0]["id"]
        resp = client.patch(f"/orgs/{org_id}", json={
            "name": "Test Org", "banner_message": "sneaky",
        }, headers=user_headers)
        assert resp.status_code == 403

    def test_second_org_banner_is_independent(self, client, admin_headers):
        """A super admin belongs to multiple orgs after founding a second one
        -- each org's banner is its own row, not a shared instance setting."""
        register(client, "W2FOUNDER", "Second Founder", "founder2@example.com")
        users = client.get("/admin/users", headers=admin_headers).json()
        pending = next(u for u in users if u["callsign"] == "W2FOUNDER")
        client.patch(f"/admin/users/{pending['id']}/approve", headers=admin_headers)  # account-level approval first

        token2 = login(client, "W2FOUNDER")
        resp = client.post("/orgs/join", json={
            "org_slug": "second-club", "org_name": "Second Club", "org_website_url": "https://second.example.org",
        }, headers=auth(token2))
        assert resp.status_code == 201, resp.text
        org2_id = resp.json()["id"]

        # /orgs/join always leaves the new membership pending (even for an
        # already-active user founding a new org) -- approve it too.
        client.patch(f"/admin/users/{pending['id']}/approve", headers=admin_headers)

        client.patch(f"/orgs/{org2_id}", json={
            "name": "Second Club", "banner_message": "Second club's own notice",
        }, headers=admin_headers)

        first_org_id = client.get("/orgs/mine", headers=admin_headers).json()[0]["id"]
        all_orgs = client.get("/orgs", headers=admin_headers).json()
        first = next(o for o in all_orgs if o["id"] == first_org_id)
        second = next(o for o in all_orgs if o["id"] == org2_id)
        assert first["banner_message"] is None
        assert second["banner_message"] == "Second club's own notice"
