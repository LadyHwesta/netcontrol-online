"""
Tests for self-service "forgot password" (issue follow-up):
  POST /auth/forgot-password
  POST /auth/reset-password
  GET  /auth/config's smtp_configured field

Same fixtures/conventions as tests/test_email_verification.py: SMTP is never
configured by default in the test environment, so `smtp_configured` +
`sent_emails` + `app_base_url` are needed to exercise the email-sending path
and extract the real token from the intercepted email body.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from helpers import register, login, auth
from models import User


def _extract_reset_token(sent_emails):
    email = next(e for e in sent_emails if e["subject"] == "[NetControl Online] Reset Your Password")
    match = re.search(r"resetpw=([\w-]+)", email["body_text"])
    assert match, "no token found in the reset email body -- is app_base_url fixture active?"
    return match.group(1)


class TestAuthConfigReportsSmtp:
    def test_smtp_not_configured_by_default(self, client):
        assert client.get("/auth/config").json()["smtp_configured"] is False

    def test_smtp_configured_flag_reflects_setting(self, client, smtp_configured):
        assert client.get("/auth/config").json()["smtp_configured"] is True


class TestForgotPasswordRequest:
    def test_sends_reset_email_by_callsign(self, client, smtp_configured, sent_emails, app_base_url):
        register(client, "W1FGT", "Forgot User", "forgot@example.com")
        resp = client.post("/auth/forgot-password", json={"identifier": "W1FGT"})
        assert resp.status_code == 204
        email = next(e for e in sent_emails if e["subject"] == "[NetControl Online] Reset Your Password")
        assert email["to"] == ["forgot@example.com"]

    def test_sends_reset_email_by_email(self, client, smtp_configured, sent_emails, app_base_url):
        register(client, "W1FGT2", "Forgot User", "forgot2@example.com")
        resp = client.post("/auth/forgot-password", json={"identifier": "forgot2@example.com"})
        assert resp.status_code == 204
        assert any(e["subject"] == "[NetControl Online] Reset Your Password" for e in sent_emails)

    def test_callsign_lookup_is_case_insensitive(self, client, smtp_configured, sent_emails, app_base_url):
        register(client, "W1CASE", "Case User", "case@example.com")
        resp = client.post("/auth/forgot-password", json={"identifier": "w1case"})
        assert resp.status_code == 204
        assert len(sent_emails) == 1

    def test_nonexistent_account_still_204_and_sends_no_email(self, client, smtp_configured, sent_emails, app_base_url):
        resp = client.post("/auth/forgot-password", json={"identifier": "W9NOBODY"})
        assert resp.status_code == 204
        assert sent_emails == []

    def test_no_smtp_configured_still_204(self, client):
        """No smtp_configured fixture here -- SMTP is unconfigured, same as a
        real instance that hasn't set it up. send_email() itself no-ops
        quietly; the endpoint must not error either way."""
        register(client, "W1NOSMTP", "No Smtp", "nosmtp@example.com")
        resp = client.post("/auth/forgot-password", json={"identifier": "W1NOSMTP"})
        assert resp.status_code == 204


class TestResetPassword:
    def _get_token(self, client, sent_emails, callsign="W1RESET", email="reset@example.com"):
        register(client, callsign, "Reset User", email)
        client.post("/auth/forgot-password", json={"identifier": callsign})
        return _extract_reset_token(sent_emails)

    def test_valid_token_resets_password_and_logs_in(self, client, smtp_configured, sent_emails, app_base_url):
        token = self._get_token(client, sent_emails)
        resp = client.post("/auth/reset-password", json={"token": token, "password": "brandnewpass123"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["access_token"]
        assert data["user"]["callsign"] == "W1RESET"
        assert login(client, "W1RESET", password="brandnewpass123")

    def test_old_password_no_longer_works(self, client, smtp_configured, sent_emails, app_base_url):
        token = self._get_token(client, sent_emails)
        client.post("/auth/reset-password", json={"token": token, "password": "brandnewpass123"})
        resp = client.post("/auth/login", data={"username": "W1RESET", "password": "testpass123"})
        assert resp.status_code == 401

    def test_token_is_single_use(self, client, smtp_configured, sent_emails, app_base_url):
        token = self._get_token(client, sent_emails)
        first = client.post("/auth/reset-password", json={"token": token, "password": "brandnewpass123"})
        assert first.status_code == 200
        second = client.post("/auth/reset-password", json={"token": token, "password": "anotherpass456"})
        assert second.status_code == 400

    def test_invalid_token_rejected(self, client):
        resp = client.post("/auth/reset-password", json={"token": "bogus", "password": "brandnewpass123"})
        assert resp.status_code == 400

    def test_short_password_rejected(self, client, smtp_configured, sent_emails, app_base_url):
        token = self._get_token(client, sent_emails)
        resp = client.post("/auth/reset-password", json={"token": token, "password": "short"})
        assert resp.status_code == 400

    async def test_expired_token_rejected(self, client, db, smtp_configured, sent_emails, app_base_url):
        token = self._get_token(client, sent_emails)
        user = (await db.execute(select(User).filter(User.callsign == "W1RESET"))).scalar_one_or_none()
        user.password_reset_sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await db.commit()

        resp = client.post("/auth/reset-password", json={"token": token, "password": "brandnewpass123"})
        assert resp.status_code == 400

    def test_second_forgot_password_request_invalidates_the_first_link(self, client, smtp_configured, sent_emails, app_base_url):
        """Requesting a new reset link overwrites the stored token -- the
        first email's link stops working, same single-active-token shape as
        the admin-invite flow."""
        register(client, "W1TWICE", "Twice User", "twice@example.com")
        client.post("/auth/forgot-password", json={"identifier": "W1TWICE"})
        first_token = _extract_reset_token(sent_emails)
        client.post("/auth/forgot-password", json={"identifier": "W1TWICE"})

        resp = client.post("/auth/reset-password", json={"token": first_token, "password": "brandnewpass123"})
        assert resp.status_code == 400
