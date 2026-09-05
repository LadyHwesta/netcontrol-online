"""
Tests for POST /auth/change-password (issue follow-up) -- self-service
password change for an already-logged-in user, distinct from
/auth/set-password (admin-invite token) and /auth/reset-password
(forgot-password link): this one proves identity via the current
password rather than a token.
"""
from helpers import login


class TestChangePassword:
    def test_wrong_current_password_rejected(self, client, admin_headers):
        resp = client.post("/auth/change-password", json={
            "current_password": "wrongpassword", "new_password": "newpassword123",
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "incorrect" in resp.json()["detail"].lower()

    def test_new_password_too_short_rejected(self, client, admin_headers):
        resp = client.post("/auth/change-password", json={
            "current_password": "testpass123", "new_password": "short",
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "8 characters" in resp.json()["detail"]

    def test_successful_change_allows_login_with_new_password(self, client, admin_headers):
        resp = client.post("/auth/change-password", json={
            "current_password": "testpass123", "new_password": "brandnewpassword456",
        }, headers=admin_headers)
        assert resp.status_code == 204

        # Old password no longer works.
        old_login = client.post("/auth/login", data={"username": "W1ADMIN", "password": "testpass123"})
        assert old_login.status_code == 401

        # New password does.
        login(client, "W1ADMIN", "brandnewpassword456")

    def test_requires_authentication(self, client):
        resp = client.post("/auth/change-password", json={
            "current_password": "testpass123", "new_password": "newpassword123",
        })
        assert resp.status_code == 401

    def test_does_not_invalidate_current_session(self, client, admin_headers):
        # The JWT that made the change itself should keep working -- it's
        # keyed by user.id, not the password, same as update_profile's own
        # email/callsign change never invalidating the current session.
        client.post("/auth/change-password", json={
            "current_password": "testpass123", "new_password": "brandnewpassword456",
        }, headers=admin_headers)
        resp = client.get("/auth/me", headers=admin_headers)
        assert resp.status_code == 200
