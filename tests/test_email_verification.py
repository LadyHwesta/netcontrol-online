"""
Tests for email verification (tech debt: "No email verification on registration"):
  GET /auth/verify-email
  email_verified gate on POST /auth/login
  email_verified / verification email behavior on POST /auth/register
  admin approval also marking an account verified (the only way to unblock a
  user whose link never arrived or can't work without APP_BASE_URL configured)

SMTP is never configured in the test environment (see conftest.py), so by
default every registration is auto-verified — matching today's behavior for
anyone who hasn't set up SMTP. The `smtp_configured` fixture flips
_smtp_configured() on for tests that need to exercise the verification gate,
`sent_emails` intercepts send_email() so no real network call is made, and
`app_base_url` makes the verification email include an actual link (without
it, _app_url() omits the link entirely and there's no token to extract).

verification_token is stored as a sha256 hash (like api_tokens.token_hash),
not the raw value — _extract_token_from_email() below pulls the real token
out of the intercepted email body, the same place the user would get it from.
"""
import re

from helpers import register, auth


def _extract_token_from_email(sent_emails):
    verify_email = next(e for e in sent_emails if e["subject"] == "[NetControl Online] Verify Your Email")
    match = re.search(r"token=([\w-]+)", verify_email["body_text"])
    assert match, "no token found in the verification email body -- is app_base_url fixture active?"
    return match.group(1)


class TestRegistrationWithoutSmtp:
    """Default test environment: SMTP unconfigured -- unchanged pre-existing behavior."""

    def test_user_is_auto_verified(self, client):
        resp = register(client, "W1FIRST", "First User", "first@example.com")
        assert resp.json()["email_verified"] is True

    def test_second_user_also_auto_verified(self, client):
        register(client, "W1FIRST", "First User", "first@example.com")
        resp = register(client, "W2SECOND", "Second User", "second@example.com")
        assert resp.json()["email_verified"] is True


class TestRegistrationWithSmtp:
    def test_first_user_still_auto_verified(self, client, smtp_configured, sent_emails):
        """Bootstrap admin skips verification even with SMTP configured -- avoids a
        first-run lockout if the fresh SMTP config turns out to be wrong."""
        resp = register(client, "W1FIRST", "First User", "first@example.com")
        assert resp.json()["email_verified"] is True
        assert not any(e["subject"] == "[NetControl Online] Verify Your Email" for e in sent_emails)

    def test_second_user_requires_verification(self, client, smtp_configured, sent_emails):
        register(client, "W1FIRST", "First User", "first@example.com")
        resp = register(client, "W2SECOND", "Second User", "second@example.com")
        assert resp.json()["email_verified"] is False

        verify_emails = [e for e in sent_emails if e["subject"] == "[NetControl Online] Verify Your Email"]
        assert len(verify_emails) == 1
        assert verify_emails[0]["to"] == ["second@example.com"]


class TestLoginGate:
    def test_login_blocked_when_unverified(self, client, smtp_configured, sent_emails):
        register(client, "W1FIRST", "First User", "first@example.com")
        register(client, "W2SECOND", "Second User", "second@example.com")

        resp = client.post("/auth/login", data={"username": "W2SECOND", "password": "testpass123"})
        assert resp.status_code == 403
        assert "verify" in resp.json()["detail"].lower()

    def test_login_still_blocked_after_verify_if_not_approved(self, client, smtp_configured, sent_emails, app_base_url):
        register(client, "W1FIRST", "First User", "first@example.com")
        register(client, "W2SECOND", "Second User", "second@example.com")

        token = _extract_token_from_email(sent_emails)
        client.get(f"/auth/verify-email?token={token}", follow_redirects=False)

        resp = client.post("/auth/login", data={"username": "W2SECOND", "password": "testpass123"})
        assert resp.status_code == 403
        assert "approval" in resp.json()["detail"].lower()

    def test_login_succeeds_once_verified_and_approved(self, client, smtp_configured, sent_emails, app_base_url):
        register(client, "W1FIRST", "First User", "first@example.com")
        register(client, "W2SECOND", "Second User", "second@example.com")

        admin_token = client.post("/auth/login", data={"username": "W1FIRST", "password": "testpass123"}).json()["access_token"]
        users = client.get("/admin/users", headers=auth(admin_token)).json()
        pending = next(u for u in users if u["callsign"] == "W2SECOND")
        client.patch(f"/admin/users/{pending['id']}/approve", headers=auth(admin_token))

        token = _extract_token_from_email(sent_emails)
        client.get(f"/auth/verify-email?token={token}", follow_redirects=False)

        resp = client.post("/auth/login", data={"username": "W2SECOND", "password": "testpass123"})
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_approval_alone_unblocks_login_without_verifying_first(self, client, smtp_configured, sent_emails, app_base_url):
        """The actual fix: an admin approving a still-unverified account (e.g. the
        verification email never arrived) is enough to let them log in — approval
        is the escape hatch, not just verification."""
        register(client, "W1FIRST", "First User", "first@example.com")
        register(client, "W2SECOND", "Second User", "second@example.com")

        admin_token = client.post("/auth/login", data={"username": "W1FIRST", "password": "testpass123"}).json()["access_token"]
        users = client.get("/admin/users", headers=auth(admin_token)).json()
        pending = next(u for u in users if u["callsign"] == "W2SECOND")
        approve_resp = client.patch(f"/admin/users/{pending['id']}/approve", headers=auth(admin_token))
        assert approve_resp.json()["email_verified"] is True

        # No verify-email call at all -- approval alone should be sufficient.
        resp = client.post("/auth/login", data={"username": "W2SECOND", "password": "testpass123"})
        assert resp.status_code == 200
        assert "access_token" in resp.json()


class TestVerifyEmailEndpoint:
    def test_valid_token_marks_verified_and_redirects(self, client, smtp_configured, sent_emails, app_base_url):
        register(client, "W1FIRST", "First User", "first@example.com")
        register(client, "W2SECOND", "Second User", "second@example.com")
        token = _extract_token_from_email(sent_emails)

        resp = client.get(f"/auth/verify-email?token={token}", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/?verified=1"

    def test_invalid_token_redirects_with_failure(self, client):
        resp = client.get("/auth/verify-email?token=not-a-real-token", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/?verified=0"

    def test_token_is_single_use(self, client, smtp_configured, sent_emails, app_base_url):
        register(client, "W1FIRST", "First User", "first@example.com")
        register(client, "W2SECOND", "Second User", "second@example.com")
        token = _extract_token_from_email(sent_emails)

        client.get(f"/auth/verify-email?token={token}", follow_redirects=False)
        resp = client.get(f"/auth/verify-email?token={token}", follow_redirects=False)
        assert resp.headers["location"] == "/?verified=0"

    async def test_expired_token_redirects_with_failure(self, client, db, smtp_configured, sent_emails, app_base_url):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select
        from models import User

        register(client, "W1FIRST", "First User", "first@example.com")
        register(client, "W2SECOND", "Second User", "second@example.com")
        token = _extract_token_from_email(sent_emails)

        user = (await db.execute(select(User).filter(User.callsign == "W2SECOND"))).scalar_one_or_none()
        user.verification_sent_at = datetime.now(timezone.utc) - timedelta(days=8)
        await db.commit()

        resp = client.get(f"/auth/verify-email?token={token}", follow_redirects=False)
        assert resp.headers["location"] == "/?verified=0"
