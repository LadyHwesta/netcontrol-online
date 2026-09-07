"""
Tests for super-admin promotion/revocation:
  PATCH /admin/users/{id}/make-admin
  PATCH /admin/users/{id}/remove-admin

Neither endpoint had any test coverage before this file -- found while
adding remove-admin (make-admin's previously-missing undo) as a follow-up
to a user asking how to get a second super admin, which make-admin already
supported end-to-end.
"""

from helpers import auth, login, register


def _first_admin_and_second_user(client):
    """First registration is auto-admin+active; second is a plain pending
    user in the same (default, single-tenant) org -- see helpers.register's
    own docstring precedent in test_auth.py."""
    register(client, "W1ADMIN", "First Admin", "admin@example.com")
    admin_token = login(client, "W1ADMIN")
    resp = register(client, "K2SECOND", "Second User", "second@example.com")
    user_id = resp.json()["id"]
    return admin_token, user_id


class TestMakeAdmin:
    def test_grants_admin_and_activates(self, client):
        admin_token, user_id = _first_admin_and_second_user(client)
        resp = client.patch(f"/admin/users/{user_id}/make-admin", headers=auth(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is True
        assert data["is_active"] is True  # make-admin force-activates a pending user

        # The promoted user is now a genuine second super admin -- can log
        # in and reach a super-admin-only endpoint on their own account.
        second_token = login(client, "K2SECOND")
        me = client.get("/auth/me", headers=auth(second_token)).json()
        assert me["is_admin"] is True
        assert client.get("/admin/users", headers=auth(second_token)).status_code == 200

    def test_requires_super_admin(self, client):
        register(client, "W1ADMIN", "First Admin", "admin@example.com")
        admin_token = login(client, "W1ADMIN")
        resp = register(client, "K2SECOND", "Second User", "second@example.com")
        target_id = resp.json()["id"]
        # Approve (but don't promote) a third user -- an active, ordinary
        # operator, the actual caller this guard needs to reject.
        register(client, "N3THIRD", "Third User", "third@example.com")
        third_id = client.get("/admin/users", headers=auth(admin_token)).json()
        third_id = next(u["id"] for u in third_id if u["callsign"] == "N3THIRD")
        client.patch(f"/admin/users/{third_id}/approve", headers=auth(admin_token))
        third_token = login(client, "N3THIRD")

        resp = client.patch(f"/admin/users/{target_id}/make-admin", headers=auth(third_token))
        assert resp.status_code == 403

    def test_nonexistent_user_404s(self, client):
        register(client, "W1ADMIN", "First Admin", "admin@example.com")
        admin_token = login(client, "W1ADMIN")
        resp = client.patch("/admin/users/9999/make-admin", headers=auth(admin_token))
        assert resp.status_code == 404


class TestRemoveAdmin:
    def test_revokes_admin_but_keeps_account_active(self, client):
        admin_token, user_id = _first_admin_and_second_user(client)
        client.patch(f"/admin/users/{user_id}/make-admin", headers=auth(admin_token))

        resp = client.patch(f"/admin/users/{user_id}/remove-admin", headers=auth(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is False
        assert data["is_active"] is True  # revoking admin doesn't touch the account itself

        # Demoted user keeps their account and can still log in -- just as
        # a regular operator, no longer able to reach admin-only endpoints.
        second_token = login(client, "K2SECOND")
        assert client.get("/admin/users", headers=auth(second_token)).status_code == 403

    def test_cannot_remove_own_admin_access(self, client):
        """The self-block is what guarantees this endpoint can never leave
        zero super admins: the caller always keeps their own admin status."""
        register(client, "W1ADMIN", "First Admin", "admin@example.com")
        admin_token = login(client, "W1ADMIN")
        me = client.get("/auth/me", headers=auth(admin_token)).json()

        resp = client.patch(f"/admin/users/{me['id']}/remove-admin", headers=auth(admin_token))
        assert resp.status_code == 400
        # Still an admin afterward.
        assert client.get("/auth/me", headers=auth(admin_token)).json()["is_admin"] is True

    def test_requires_super_admin(self, client):
        admin_token, user_id = _first_admin_and_second_user(client)
        client.patch(f"/admin/users/{user_id}/make-admin", headers=auth(admin_token))
        # Approve (but don't promote) a third user -- an active, ordinary
        # operator, the actual caller this guard needs to reject.
        register(client, "N3THIRD", "Third User", "third@example.com")
        users = client.get("/admin/users", headers=auth(admin_token)).json()
        third_id = next(u["id"] for u in users if u["callsign"] == "N3THIRD")
        client.patch(f"/admin/users/{third_id}/approve", headers=auth(admin_token))
        third_token = login(client, "N3THIRD")

        resp = client.patch(f"/admin/users/{user_id}/remove-admin", headers=auth(third_token))
        assert resp.status_code == 403

    def test_nonexistent_user_404s(self, client):
        register(client, "W1ADMIN", "First Admin", "admin@example.com")
        admin_token = login(client, "W1ADMIN")
        resp = client.patch("/admin/users/9999/remove-admin", headers=auth(admin_token))
        assert resp.status_code == 404
