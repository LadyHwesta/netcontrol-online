"""
Tests for authentication endpoints:
  POST /auth/register
  POST /auth/login
  GET  /auth/me
  PATCH /auth/profile
  POST/DELETE /auth/photo, GET /users/{id}/photo
"""

import io
import re

from helpers import register, login, auth


class TestRegistration:
    def test_first_user_becomes_admin_and_active(self, client):
        resp = register(client)
        assert resp.status_code == 201
        data = resp.json()
        assert data["callsign"] == "W1TEST"
        assert data["is_active"] is True
        assert data["is_admin"] is True

    def test_second_user_requires_approval(self, client):
        register(client, "W1FIRST", "First User", "first@example.com")
        resp = register(client, "W2SECOND", "Second User", "second@example.com")
        assert resp.status_code == 201
        data = resp.json()
        assert data["is_active"] is False
        assert data["is_admin"] is False

    def test_callsign_stored_uppercase(self, client):
        resp = register(client, callsign="w1test")
        assert resp.status_code == 201
        assert resp.json()["callsign"] == "W1TEST"

    def test_duplicate_callsign_rejected(self, client):
        register(client)
        # Same callsign, different email
        resp = register(client, email="other@example.com")
        assert resp.status_code == 400
        assert "callsign" in resp.json()["detail"].lower()

    def test_duplicate_email_rejected(self, client):
        register(client)
        # Same email, different callsign
        resp = register(client, callsign="W2OTHER")
        assert resp.status_code == 400
        assert "email" in resp.json()["detail"].lower()


class TestLogin:
    def test_login_with_callsign(self, client):
        register(client)
        resp = client.post("/auth/login", data={
            "username": "W1TEST", "password": "testpass123"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_with_email(self, client):
        register(client)
        resp = client.post("/auth/login", data={
            "username": "test@example.com", "password": "testpass123"
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_login_callsign_case_insensitive(self, client):
        """Login should succeed regardless of callsign case (stored uppercase)."""
        register(client)
        resp = client.post("/auth/login", data={
            "username": "w1test", "password": "testpass123"
        })
        assert resp.status_code == 200

    def test_login_wrong_password(self, client):
        register(client)
        resp = client.post("/auth/login", data={
            "username": "W1TEST", "password": "wrongpassword"
        })
        assert resp.status_code == 401

    def test_login_inactive_user_blocked(self, client):
        """A user awaiting approval must not be able to log in."""
        register(client, "W1ADMIN", "Admin", "admin@example.com")  # auto-approved
        register(client, "W2PEND", "Pending", "pending@example.com")  # needs approval
        resp = client.post("/auth/login", data={
            "username": "W2PEND", "password": "testpass123"
        })
        assert resp.status_code == 403

    def test_login_unknown_callsign(self, client):
        resp = client.post("/auth/login", data={
            "username": "W9NOBODY", "password": "pass"
        })
        assert resp.status_code == 401

    def test_login_response_includes_user(self, client):
        """Login response should include user info so the frontend doesn't
        need a separate /auth/me call immediately after login."""
        register(client)
        resp = client.post("/auth/login", data={
            "username": "W1TEST", "password": "testpass123"
        })
        data = resp.json()
        assert "user" in data
        assert data["user"]["callsign"] == "W1TEST"


class TestMe:
    def test_get_me_returns_current_user(self, client, admin_headers):
        resp = client.get("/auth/me", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["callsign"] == "W1ADMIN"

    def test_get_me_requires_auth(self, client):
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_get_me_with_bad_token(self, client):
        resp = client.get("/auth/me", headers={"Authorization": "Bearer notavalidtoken"})
        assert resp.status_code == 401


def _profile_payload(name="Admin User", email="admin@example.com", callsign="W1ADMIN", phone=None):
    """Defaults match admin_headers' own user (registered as W1ADMIN/Admin
    User/admin@example.com in conftest.py's admin_token fixture) so a test
    overriding only one field doesn't accidentally rename the others."""
    return {"name": name, "email": email, "callsign": callsign, "phone": phone}


class TestProfileUpdate:
    """PATCH /auth/profile (issue follow-up) -- self-service name/email/
    callsign/phone, previously fixed at registration with no way to fix a
    typo or update contact info."""

    def test_updates_all_fields(self, client, admin_headers):
        resp = client.patch("/auth/profile", json=_profile_payload(
            name="New Name", email="new@example.com", callsign="W2NEW", phone="555-1234",
        ), headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "New Name"
        assert data["email"] == "new@example.com"
        assert data["callsign"] == "W2NEW"
        assert data["phone"] == "555-1234"

        me = client.get("/auth/me", headers=admin_headers).json()
        assert me["callsign"] == "W2NEW"

    def test_callsign_stored_uppercase(self, client, admin_headers):
        resp = client.patch("/auth/profile", json=_profile_payload(callsign="w2new"), headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["callsign"] == "W2NEW"

    def test_phone_can_be_cleared(self, client, admin_headers):
        client.patch("/auth/profile", json=_profile_payload(phone="555-1234"), headers=admin_headers)
        resp = client.patch("/auth/profile", json=_profile_payload(phone=None), headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["phone"] is None

    def test_name_required(self, client, admin_headers):
        resp = client.patch("/auth/profile", json=_profile_payload(name="   "), headers=admin_headers)
        assert resp.status_code == 400

    def test_callsign_conflict_with_another_user_rejected(self, client, admin_headers):
        register(client, "W2OTHER", "Other", "other@example.com")
        resp = client.patch("/auth/profile", json=_profile_payload(callsign="W2OTHER"), headers=admin_headers)
        assert resp.status_code == 400
        assert "callsign" in resp.json()["detail"].lower()

    def test_email_conflict_with_another_user_rejected(self, client, admin_headers):
        register(client, "W2OTHER", "Other", "other@example.com")
        resp = client.patch("/auth/profile", json=_profile_payload(email="other@example.com"), headers=admin_headers)
        assert resp.status_code == 400
        assert "email" in resp.json()["detail"].lower()

    def test_keeping_own_callsign_and_email_is_not_a_conflict(self, client, admin_headers):
        """Saving the form unchanged (or changing only e.g. phone) must not
        trip the "already registered" check against the user's own row."""
        resp = client.patch("/auth/profile", json=_profile_payload(phone="555-9999"), headers=admin_headers)
        assert resp.status_code == 200

    def test_requires_auth(self, client):
        resp = client.patch("/auth/profile", json=_profile_payload())
        assert resp.status_code == 401

    def test_session_survives_callsign_change(self, client, admin_headers):
        """The JWT is keyed by user.id, not callsign/email (see login()) --
        an existing session must keep working after either changes."""
        client.patch("/auth/profile", json=_profile_payload(callsign="W2NEW", email="new@example.com"), headers=admin_headers)
        resp = client.get("/auth/me", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["callsign"] == "W2NEW"

    def test_email_change_without_smtp_updates_immediately(self, client, admin_headers):
        """Default test environment: SMTP unconfigured -- matches
        registration's own "nobody needs verification" behavior."""
        resp = client.patch("/auth/profile", json=_profile_payload(email="new@example.com"), headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["email"] == "new@example.com"
        assert resp.json()["email_verified"] is True

    def test_email_change_with_smtp_requires_reverification(self, client, admin_headers, smtp_configured, sent_emails, app_base_url):
        resp = client.patch("/auth/profile", json=_profile_payload(email="new@example.com"), headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["email_verified"] is False

        verify_emails = [e for e in sent_emails if e["subject"] == "[NetControl Online] Verify Your New Email Address"]
        assert len(verify_emails) == 1
        assert verify_emails[0]["to"] == ["new@example.com"]

        # Existing session (this same JWT) still works -- only a *future*
        # login is blocked until the new address is verified.
        assert client.get("/auth/me", headers=admin_headers).status_code == 200

        login_attempt = client.post("/auth/login", data={"username": "W1ADMIN", "password": "testpass123"})
        assert login_attempt.status_code == 403

        match = re.search(r"token=([\w-]+)", verify_emails[0]["body_text"])
        assert match, "no token found in the re-verification email body"
        verify = client.get(f"/auth/verify-email?token={match.group(1)}")
        assert verify.status_code in (200, 307)

        login_attempt = client.post("/auth/login", data={"username": "W1ADMIN", "password": "testpass123"})
        assert login_attempt.status_code == 200

    def test_email_change_with_smtp_but_unchanged_email_does_not_reverify(self, client, admin_headers, smtp_configured, sent_emails):
        """Saving the profile form with the same email address shouldn't
        force a pointless re-verification."""
        resp = client.patch("/auth/profile", json=_profile_payload(name="New Name"), headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["email_verified"] is True
        assert not any(e["subject"] == "[NetControl Online] Verify Your New Email Address" for e in sent_emails)


class TestProfilePhoto:
    """POST/DELETE /auth/photo, public GET /users/{id}/photo (issue
    follow-up) -- shown on the public live page next to whoever's running
    the net / the assigned broadcaster. No has_photo flag anywhere -- the
    frontend just always tries the <img>, same as the org/instance logo."""

    def _photo_file(self, filename="photo.png"):
        return {"file": (filename, io.BytesIO(b"not a real image, just bytes"), "image/png")}

    def test_upload_and_fetch(self, client, admin_headers):
        me = client.get("/auth/me", headers=admin_headers).json()
        resp = client.post("/auth/photo", files=self._photo_file(), headers=admin_headers)
        assert resp.status_code == 204, resp.text

        img = client.get(f"/users/{me['id']}/photo")
        assert img.status_code == 200
        assert img.content == b"not a real image, just bytes"
        assert img.headers["content-type"] == "image/png"

    def test_uploading_new_photo_replaces_old_one(self, client, admin_headers):
        me = client.get("/auth/me", headers=admin_headers).json()
        client.post("/auth/photo", files=self._photo_file("first.png"), headers=admin_headers)
        client.post("/auth/photo", files=self._photo_file("second.jpg"), headers=admin_headers)

        img = client.get(f"/users/{me['id']}/photo")
        assert img.status_code == 200
        assert img.headers["content-type"] == "image/jpeg"

    def test_delete_photo(self, client, admin_headers):
        me = client.get("/auth/me", headers=admin_headers).json()
        client.post("/auth/photo", files=self._photo_file(), headers=admin_headers)

        resp = client.delete("/auth/photo", headers=admin_headers)
        assert resp.status_code == 204
        assert client.get(f"/users/{me['id']}/photo").status_code == 404

    def test_fetch_404s_with_none_uploaded(self, client, admin_headers):
        me = client.get("/auth/me", headers=admin_headers).json()
        assert client.get(f"/users/{me['id']}/photo").status_code == 404

    def test_public_fetch_requires_no_auth(self, client, admin_headers):
        me = client.get("/auth/me", headers=admin_headers).json()
        client.post("/auth/photo", files=self._photo_file(), headers=admin_headers)
        assert client.get(f"/users/{me['id']}/photo").status_code == 200

    def test_rejects_unsupported_file_type(self, client, admin_headers):
        resp = client.post("/auth/photo",
            files={"file": ("photo.svg", io.BytesIO(b"<svg></svg>"), "image/svg+xml")},
            headers=admin_headers)
        assert resp.status_code == 400

    def test_upload_requires_auth(self, client):
        resp = client.post("/auth/photo", files=self._photo_file())
        assert resp.status_code == 401
