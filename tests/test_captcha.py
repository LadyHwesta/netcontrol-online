"""
Tests for bot protection on registration/login -- Cloudflare Turnstile,
Google reCAPTCHA, and ALTCHA (open-source, self-contained proof-of-work):
  GET  /auth/config
  GET  /captcha/altcha-challenge
  POST /auth/register (captcha_token)
  POST /auth/login (captcha_token)

Opt-in via CAPTCHA_PROVIDER: with it unset (the default in every other test
file), none of this code path is exercised at all -- registration and login
behave exactly as before. Exactly one provider is active at a time.
"""

import pytest

import main
from helpers import register, login

# altcha is an OPTIONAL runtime dependency (see requirements.txt / main.py's
# lazy import) -- a deployment using only turnstile/recaptcha (or no
# CAPTCHA_PROVIDER at all) may not have it installed. Importing it at module
# level unconditionally would make pytest fail to even COLLECT this whole
# file -- and since a collection error aborts the entire run, every other
# test file in the suite too -- on such a deployment. Degrade gracefully
# instead: only the handful of tests that need to actually solve a real
# challenge client-side (requires_altcha below) are skipped.
try:
    import altcha
except ImportError:
    altcha = None

requires_altcha = pytest.mark.skipif(
    altcha is None, reason="altcha package not installed — optional dependency, see requirements.txt"
)


class TestTurnstileVerifyHelper:
    def test_no_token_fails_without_network_call(self, monkeypatch, turnstile_configured):
        calls = []
        monkeypatch.setattr(main.httpx, "post", lambda *a, **k: calls.append(1))
        assert main._verify_turnstile(None, "1.2.3.4") is False
        assert calls == []

    def test_cloudflare_success_true(self, monkeypatch, turnstile_configured):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"success": True}

        monkeypatch.setattr(main.httpx, "post", lambda *a, **k: FakeResponse())
        assert main._verify_turnstile("sometoken", "1.2.3.4") is True

    def test_cloudflare_success_false(self, monkeypatch, turnstile_configured):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"success": False, "error-codes": ["invalid-input-response"]}

        monkeypatch.setattr(main.httpx, "post", lambda *a, **k: FakeResponse())
        assert main._verify_turnstile("badtoken", "1.2.3.4") is False

    def test_network_error_fails_closed(self, monkeypatch, turnstile_configured):
        def raise_error(*a, **k):
            raise ConnectionError("boom")

        monkeypatch.setattr(main.httpx, "post", raise_error)
        assert main._verify_turnstile("sometoken", "1.2.3.4") is False


class TestRecaptchaVerifyHelper:
    def test_no_token_fails_without_network_call(self, monkeypatch, recaptcha_configured):
        calls = []
        monkeypatch.setattr(main.httpx, "post", lambda *a, **k: calls.append(1))
        assert main._verify_recaptcha(None, "1.2.3.4") is False
        assert calls == []

    def test_google_success_true(self, monkeypatch, recaptcha_configured):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"success": True}

        monkeypatch.setattr(main.httpx, "post", lambda *a, **k: FakeResponse())
        assert main._verify_recaptcha("sometoken", "1.2.3.4") is True

    def test_google_success_false(self, monkeypatch, recaptcha_configured):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"success": False, "error-codes": ["invalid-input-response"]}

        monkeypatch.setattr(main.httpx, "post", lambda *a, **k: FakeResponse())
        assert main._verify_recaptcha("badtoken", "1.2.3.4") is False

    def test_network_error_fails_closed(self, monkeypatch, recaptcha_configured):
        def raise_error(*a, **k):
            raise ConnectionError("boom")

        monkeypatch.setattr(main.httpx, "post", raise_error)
        assert main._verify_recaptcha("sometoken", "1.2.3.4") is False


def _solve_altcha_challenge(challenge_json: dict) -> str:
    """Solves a real ALTCHA challenge (as returned by GET
    /captcha/altcha-challenge) and returns the base64 payload a client would
    submit as captcha_token. No mocking -- exercises the real crypto."""
    challenge = altcha.ChallengeV1(
        algorithm=challenge_json["algorithm"],
        challenge=challenge_json["challenge"],
        max_number=challenge_json["maxNumber"],
        salt=challenge_json["salt"],
        signature=challenge_json["signature"],
    )
    solution = altcha.solve_challenge_v1(challenge)
    assert solution is not None, "could not solve test ALTCHA challenge"
    payload = altcha.PayloadV1(
        algorithm=challenge.algorithm,
        challenge=challenge.challenge,
        number=solution.number,
        salt=challenge.salt,
        signature=challenge.signature,
    )
    return payload.to_base64()


class TestAltchaVerifyHelper:
    def test_no_token_fails(self, altcha_configured):
        assert main._verify_altcha(None, "1.2.3.4") is False

    def test_garbage_token_fails(self, altcha_configured):
        assert main._verify_altcha("not-a-real-payload", "1.2.3.4") is False

    @requires_altcha
    def test_real_solved_challenge_passes(self, client, altcha_configured):
        resp = client.get("/captcha/altcha-challenge")
        assert resp.status_code == 200, resp.text
        token = _solve_altcha_challenge(resp.json())
        assert main._verify_altcha(token, "1.2.3.4") is True

    @requires_altcha
    def test_token_from_wrong_hmac_key_fails(self, client, altcha_configured, monkeypatch):
        resp = client.get("/captcha/altcha-challenge")
        token = _solve_altcha_challenge(resp.json())
        # A correctly-solved token for THIS key must fail verification
        # against a different one -- simulates a forged/replayed payload.
        monkeypatch.setattr(main, "ALTCHA_HMAC_KEY", "a-different-key-entirely")
        assert main._verify_altcha(token, "1.2.3.4") is False

    def test_challenge_endpoint_404s_when_not_the_active_provider(self, client, turnstile_configured):
        resp = client.get("/captcha/altcha-challenge")
        assert resp.status_code == 404


class TestAuthConfig:
    def test_disabled_by_default(self, client):
        resp = client.get("/auth/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["captcha_provider"] is None
        assert data["captcha_site_key"] is None
        assert data["turnstile_enabled"] is False
        assert data["turnstile_site_key"] is None

    def test_turnstile_exposes_site_key_only(self, client, turnstile_configured):
        resp = client.get("/auth/config")
        data = resp.json()
        assert data["captcha_provider"] == "turnstile"
        assert data["captcha_site_key"] == "1x00000000000000000000AA"
        assert data["turnstile_enabled"] is True
        assert data["turnstile_site_key"] == "1x00000000000000000000AA"
        assert "secret" not in str(data).lower()

    def test_recaptcha_exposes_site_key_only(self, client, recaptcha_configured):
        resp = client.get("/auth/config")
        data = resp.json()
        assert data["captcha_provider"] == "recaptcha"
        assert data["captcha_site_key"] == "6Lc-test-site-key"
        assert data["turnstile_enabled"] is False  # legacy alias -- only true for turnstile
        assert "secret" not in str(data).lower()

    def test_altcha_exposes_no_site_key(self, client, altcha_configured):
        resp = client.get("/auth/config")
        data = resp.json()
        assert data["captcha_provider"] == "altcha"
        assert data["captcha_site_key"] is None  # ALTCHA needs none -- uses the challenge endpoint instead
        assert "secret" not in str(data).lower()
        assert "test-hmac-key" not in str(data)


class TestRegistrationCaptcha:
    def test_registration_unaffected_when_not_configured(self, client):
        resp = register(client, "W1NOTS", "No Captcha", "nots@example.com")
        assert resp.status_code == 201

    def test_registration_requires_token_when_turnstile_configured(self, client, turnstile_configured):
        resp = client.post("/auth/register", json={
            "callsign": "W1NOTOK", "name": "No Token", "email": "notok@example.com", "password": "testpass123",
        })
        assert resp.status_code == 400

    def test_registration_rejects_failed_turnstile(self, client, turnstile_configured, turnstile_verify):
        turnstile_verify.set_result(False)
        resp = client.post("/auth/register", json={
            "callsign": "W1BADTOK", "name": "Bad Token", "email": "badtok@example.com",
            "password": "testpass123", "captcha_token": "bad",
        })
        assert resp.status_code == 400
        assert len(turnstile_verify) == 1

    def test_registration_succeeds_with_valid_turnstile(self, client, turnstile_configured, turnstile_verify):
        resp = client.post("/auth/register", json={
            "callsign": "W1GOODTOK", "name": "Good Token", "email": "goodtok@example.com",
            "password": "testpass123", "captcha_token": "good-token-value",
        })
        assert resp.status_code == 201, resp.text
        assert turnstile_verify[0]["token"] == "good-token-value"

    def test_registration_succeeds_with_valid_recaptcha(self, client, recaptcha_configured, recaptcha_verify):
        resp = client.post("/auth/register", json={
            "callsign": "W1RCGOOD", "name": "Good Recaptcha", "email": "rcgood@example.com",
            "password": "testpass123", "captcha_token": "good-recaptcha-value",
        })
        assert resp.status_code == 201, resp.text
        assert recaptcha_verify[0]["token"] == "good-recaptcha-value"

    def test_registration_rejects_failed_recaptcha(self, client, recaptcha_configured, recaptcha_verify):
        recaptcha_verify.set_result(False)
        resp = client.post("/auth/register", json={
            "callsign": "W1RCBAD", "name": "Bad Recaptcha", "email": "rcbad@example.com",
            "password": "testpass123", "captcha_token": "bad",
        })
        assert resp.status_code == 400

    @requires_altcha
    def test_registration_succeeds_with_solved_altcha(self, client, altcha_configured):
        challenge = client.get("/captcha/altcha-challenge").json()
        token = _solve_altcha_challenge(challenge)
        resp = client.post("/auth/register", json={
            "callsign": "W1ALTGOOD", "name": "Good Altcha", "email": "altgood@example.com",
            "password": "testpass123", "captcha_token": token,
        })
        assert resp.status_code == 201, resp.text

    def test_registration_rejects_unsolved_altcha(self, client, altcha_configured):
        resp = client.post("/auth/register", json={
            "callsign": "W1ALTBAD", "name": "Bad Altcha", "email": "altbad@example.com",
            "password": "testpass123", "captcha_token": "not-solved",
        })
        assert resp.status_code == 400


class TestLoginCaptcha:
    def test_login_unaffected_when_not_configured(self, client):
        register(client, "W1LOGINNOTS", "No CC", "loginnots@example.com")
        token = login(client, "W1LOGINNOTS")
        assert token

    def test_login_rejects_missing_token_when_configured(self, client, turnstile_configured):
        register(client, "W1LOGINMISS", "Login Miss", "loginmiss@example.com")
        resp = client.post("/auth/login", data={"username": "W1LOGINMISS", "password": "testpass123"})
        assert resp.status_code == 400

    def test_login_rejects_failed_verification(self, client, turnstile_configured, turnstile_verify):
        register(client, "W1LOGINBAD", "Login Bad", "loginbad@example.com")
        turnstile_verify.set_result(False)
        resp = client.post("/auth/login", data={
            "username": "W1LOGINBAD", "password": "testpass123", "captcha_token": "bad",
        })
        assert resp.status_code == 400

    def test_login_succeeds_with_valid_token(self, client, turnstile_configured, turnstile_verify):
        register(client, "W1LOGINGOOD", "Login Good", "logingood@example.com")
        resp = client.post("/auth/login", data={
            "username": "W1LOGINGOOD", "password": "testpass123", "captcha_token": "good",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["access_token"]

    @requires_altcha
    def test_login_succeeds_with_solved_altcha(self, client, altcha_configured):
        # Registration is ALSO gated while altcha_configured is active --
        # needs its own solved challenge before login's.
        reg_challenge = client.get("/captcha/altcha-challenge").json()
        reg_resp = client.post("/auth/register", json={
            "callsign": "W1LOGINALT", "name": "Login Altcha", "email": "loginalt@example.com",
            "password": "testpass123", "captcha_token": _solve_altcha_challenge(reg_challenge),
        })
        assert reg_resp.status_code == 201, reg_resp.text

        login_challenge = client.get("/captcha/altcha-challenge").json()
        resp = client.post("/auth/login", data={
            "username": "W1LOGINALT", "password": "testpass123",
            "captcha_token": _solve_altcha_challenge(login_challenge),
        })
        assert resp.status_code == 200, resp.text
