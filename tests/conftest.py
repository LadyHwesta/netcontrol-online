"""
Pytest configuration and shared fixtures for the NetControl Online test suite.

Strategy
--------
- DATABASE_URL is set to a temp file-based SQLite DB BEFORE any app
  imports, so database.py never tries to connect to PostgreSQL. A FILE
  (not sqlite+aiosqlite:///:memory:) is required: TestClient runs the
  ASGI app in its own background thread via an anyio blocking portal,
  with its own event loop — an in-memory DB shared via StaticPool would
  mean one aiosqlite connection driven from two different event
  loops/threads (pytest-asyncio's fixture loop and the portal's), which
  deadlocks. A file is naturally shared across separate connections, so
  no StaticPool is needed. Verified empirically before writing this.
- Rate limiting is disabled so tests can hit /auth endpoints freely.
- Tables are created once per session; rows are wiped between tests.
- Async: pytest-asyncio with asyncio_mode=auto (see pytest.ini) and
  asyncio_default_fixture_loop_scope=session — the session scope is
  required for session-scoped async autouse fixtures (setup_database
  below) to work at all; with the default function scope every test,
  sync or async, fails collection with a ScopeMismatch. With session
  scope, plain sync `def test_...` functions depend on async autouse
  fixtures (and can even request an async fixture directly) with no
  special handling needed on their part.
"""

import os
import pathlib
import shutil
import sys
import tempfile

# Add this directory to sys.path so helpers.py is importable from test files.
sys.path.insert(0, os.path.dirname(__file__))

# ── Must happen before any app imports ──────────────────────────────────────
_DB_PATH = os.path.join(tempfile.gettempdir(), f"netcontrol_test_{os.getpid()}.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"  # database.py rewrites this to +aiosqlite
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "60")

# Force SMTP "not configured" for the whole suite, regardless of what a real
# .env file in this checkout has set for the live/demo deployment. main.py
# calls load_dotenv() at import time (below), which does NOT override an
# already-set env var — so setting these here first wins. Without this, a
# checkout with real SMTP creds would make the suite try to actually send
# mail to fake test addresses and fail on the resulting bounce, since
# behavior now branches on _smtp_configured() (email verification gate,
# support ticket endpoint). Tests that need SMTP "on" use the smtp_configured
# fixture below, which monkeypatches these back on for just that test.
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["SUPPORT_EMAIL"] = ""

# Same reasoning, same fix, for Net Repository: without this, a checkout with
# a real NET_REPOSITORY_URL/API_KEY in .env would make create_net/update_net
# tests actually POST test fixtures to the live public directory.
os.environ["NET_REPOSITORY_URL"] = ""
os.environ["NET_REPOSITORY_API_KEY"] = ""

# ── Now safe to import the app ───────────────────────────────────────────────
# database.py builds its own async engine/session factory from DATABASE_URL
# at import time (with the postgresql/sqlite -> +asyncpg/+aiosqlite rewrite
# already applied there) -- since that env var is already set to our temp
# file above, database.engine/SessionLocal are already correctly pointed at
# the test DB. No monkeypatching needed (unlike the old sync setup, which
# needed a StaticPool override specifically for :memory: connection-sharing
# -- moot now that tests use a real file).
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from models import Base  # noqa: E402
from database import engine, SessionLocal  # noqa: E402
from main import app, limiter  # noqa: E402
from helpers import register, login, auth  # noqa: E402, F401
import routers.helpers as helpers  # noqa: E402

# Redirect all org-logo / user-photo file storage to an isolated temp
# directory for the whole test run. routers/orgs.py, routers/auth.py, and
# routers/helpers.py's own functions all resolve `helpers.UPLOADS_DIR` at
# call time rather than importing a frozen copy of it, so reassigning the
# module attribute here -- before any test/request runs -- is enough to
# keep every test's file writes off this checkout's real uploads/ dir.
# Without this, running the suite (e.g. deploy.sh's automatic
# `pytest tests/ -q` on every deploy to a testing-branch instance) wipes
# any real org logo or user profile photo sitting in the live uploads/
# directory via clean_tables' glob cleanup below -- caught after exactly
# that happened to a user's freshly-uploaded profile photo on a deploy.
_UPLOADS_TEST_DIR = pathlib.Path(tempfile.mkdtemp(prefix="netcontrol_test_uploads_"))
helpers.UPLOADS_DIR = _UPLOADS_TEST_DIR

# Disable rate limiting so tests can call auth endpoints without hitting caps.
limiter.enabled = False


# ── Database lifecycle ───────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
async def setup_database():
    """Create all tables once for the entire test session; remove the temp
    DB file (and the isolated uploads/ dir set up above) afterward."""
    if os.path.exists(_DB_PATH):
        os.remove(_DB_PATH)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    if os.path.exists(_DB_PATH):
        os.remove(_DB_PATH)
    shutil.rmtree(_UPLOADS_TEST_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
async def clean_tables():
    """Wipe all rows after each test to guarantee isolation. Also removes any
    per-org logo (`org_{id}_logo.*`) or per-user profile photo
    (`user_{id}_photo.*`, issue follow-up) file a test uploaded -- unlike the
    DB rows above, files on disk aren't reset by anything else, and org/user
    ids get reused across tests (rows are wiped, not the autoincrement
    sequence), so a leftover file from one test's id=1 would otherwise leak
    into the next test that happens to get the same id. This only ever
    touches the isolated temp dir set up above -- never this checkout's real
    uploads/ dir -- but is still scoped to these two specific patterns
    (never a bare logo.*) as a second layer of safety."""
    yield
    async with SessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            await db.execute(table.delete())
        await db.commit()
    for pattern in ("org_*_logo.*", "user_*_photo.*"):
        for f in helpers.UPLOADS_DIR.glob(pattern):
            f.unlink(missing_ok=True)


# ── Test client ──────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    """FastAPI TestClient — uses `with` so the startup event fires."""
    with TestClient(app) as c:
        yield c


# ── Email ────────────────────────────────────────────────────────────────────

@pytest.fixture
def smtp_configured(monkeypatch):
    """Makes _smtp_configured() return True, for tests exercising SMTP-gated code
    paths (email verification, support tickets). Pair with sent_emails so no real
    network call is attempted.

    Patches routers.helpers, not main -- SMTP config and _smtp_configured()
    live there since the main.py -> routers/ split (shared by every router
    that sends email), and a function only ever sees monkeypatched globals
    in the module where IT is defined, not wherever it happens to be
    imported from."""
    from routers import helpers
    monkeypatch.setattr(helpers, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(helpers, "SMTP_USER", "bot@example.com")
    monkeypatch.setattr(helpers, "SMTP_PASSWORD", "secret")


@pytest.fixture
def app_base_url(monkeypatch):
    """Sets APP_BASE_URL so emails (verify-your-email, account-approved) include
    a clickable link — without it those links are omitted (routers/helpers.py's
    _app_url()). Patches routers.helpers -- see smtp_configured above."""
    from routers import helpers
    monkeypatch.setattr(helpers, "APP_BASE_URL", "http://testserver")


@pytest.fixture
def sent_emails(monkeypatch):
    """Intercepts send_email() and records each call instead of hitting the
    network. Patches routers.helpers.send_email -- every router calls it via
    qualified access (`helpers.send_email(...)`, never a bare `from
    routers.helpers import send_email`) specifically so this one patch is
    observed everywhere, regardless of which router the call originates
    from."""
    from routers import helpers
    calls = []

    def fake_send_email(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(helpers, "send_email", fake_send_email)
    return calls


# ── Bot protection: Turnstile / reCAPTCHA / ALTCHA ─────────────────────────────
# One CallList helper shared by the three network-calling providers' *_verify
# fixtures below (ALTCHA doesn't need one -- it's real local crypto, tests
# exercise it end-to-end instead of mocking it).

class _CallList(list):
    """Plain list can't take arbitrary attributes -- subclass so set_result
    can be attached to the returned calls list (same pattern as
    pushed_nets_and_stats' CallList elsewhere in this file)."""
    pass


@pytest.fixture
def turnstile_configured(monkeypatch):
    """Makes _captcha_configured() return True for Turnstile, for tests
    exercising the CAPTCHA-gated code paths on /auth/register and
    /auth/login. Pair with turnstile_verify so no real network call is
    attempted. Patches routers.helpers -- see smtp_configured's docstring
    above for why (captcha config + _verify_*/_captcha_configured() all live
    there since the main.py -> routers/ split)."""
    from routers import helpers
    monkeypatch.setattr(helpers, "CAPTCHA_PROVIDER", "turnstile")
    monkeypatch.setattr(helpers, "TURNSTILE_SITE_KEY", "1x00000000000000000000AA")
    monkeypatch.setattr(helpers, "TURNSTILE_SECRET_KEY", "1x0000000000000000000000000000000AA")


@pytest.fixture
def turnstile_verify(monkeypatch):
    """Intercepts _verify_turnstile() so tests control pass/fail without a
    real call to Cloudflare. Defaults to always passing; call
    calls.set_result(False) to simulate a failed challenge."""
    from routers import helpers
    calls = _CallList()
    result = {"ok": True}

    def fake_verify(token, remote_ip):
        calls.append({"token": token, "remote_ip": remote_ip})
        return result["ok"]

    monkeypatch.setattr(helpers, "_verify_turnstile", fake_verify)
    calls.set_result = lambda ok: result.update(ok=ok)
    return calls


@pytest.fixture
def recaptcha_configured(monkeypatch):
    """Makes _captcha_configured() return True for reCAPTCHA. Pair with
    recaptcha_verify so no real network call is attempted."""
    from routers import helpers
    monkeypatch.setattr(helpers, "CAPTCHA_PROVIDER", "recaptcha")
    monkeypatch.setattr(helpers, "RECAPTCHA_SITE_KEY", "6Lc-test-site-key")
    monkeypatch.setattr(helpers, "RECAPTCHA_SECRET_KEY", "6Lc-test-secret-key")


@pytest.fixture
def recaptcha_verify(monkeypatch):
    """Intercepts _verify_recaptcha() so tests control pass/fail without a
    real call to Google. Defaults to always passing; call
    calls.set_result(False) to simulate a failed challenge."""
    from routers import helpers
    calls = _CallList()
    result = {"ok": True}

    def fake_verify(token, remote_ip):
        calls.append({"token": token, "remote_ip": remote_ip})
        return result["ok"]

    monkeypatch.setattr(helpers, "_verify_recaptcha", fake_verify)
    calls.set_result = lambda ok: result.update(ok=ok)
    return calls


@pytest.fixture
def altcha_configured(monkeypatch):
    """Makes _captcha_configured() return True for ALTCHA, with a fixed HMAC
    key so tests can generate/solve/verify real challenges deterministically
    -- no mocking needed, ALTCHA does no network calls of its own."""
    from routers import helpers
    monkeypatch.setattr(helpers, "CAPTCHA_PROVIDER", "altcha")
    monkeypatch.setattr(helpers, "ALTCHA_HMAC_KEY", "test-hmac-key-for-altcha")


# ── Net Repository ───────────────────────────────────────────────────────────

@pytest.fixture
def net_repo_configured(monkeypatch):
    """Makes net_repository_configured(db) return True (URL + an env-var key).
    Pair with pushed_nets so no real network call is attempted."""
    import net_repository
    monkeypatch.setattr(net_repository, "NET_REPOSITORY_URL", "https://netrepo.example.com")
    monkeypatch.setattr(net_repository, "NET_REPOSITORY_API_KEY", "nr_testkey")


@pytest.fixture
def net_repo_url_only(monkeypatch):
    """Sets NET_REPOSITORY_URL but no key -- the "fresh install, about to
    request a self-service key" state."""
    import net_repository
    monkeypatch.setattr(net_repository, "NET_REPOSITORY_URL", "https://netrepo.example.com")


@pytest.fixture
def pushed_nets(monkeypatch):
    """Intercepts httpx.post inside net_repository and records each call's
    JSON payload instead of hitting the network."""
    import httpx
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"submission_id": 1, "message": "Submission received and queued for moderation."}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


@pytest.fixture
def pushed_nets_and_stats(monkeypatch):
    """Like pushed_nets, but the fake response carries a settable status_code
    (default 201) so tests can simulate Net Repository's 404 ("not published
    there yet") response for POST /nets/stats without a real network call.
    Records every httpx.post call regardless of URL (both /nets/submit and
    /nets/stats go through this), same as pushed_nets."""
    import httpx

    class CallList(list):
        """Plain list can't take arbitrary attributes -- subclass so
        set_status can be attached to the returned calls list."""
        pass

    calls = CallList()
    status = {"code": 201}

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "error", request=httpx.Request("POST", "http://x"), response=self,
                )

        def json(self):
            return {"submission_id": 1, "message": "ok", "id": 1}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(status["code"])

    monkeypatch.setattr(httpx, "post", fake_post)
    calls.set_status = lambda code: status.update(code=code)
    return calls


# ── Composite fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def admin_token(client):
    """Register the first user (auto-admin/active) and return their JWT."""
    register(client, "W1ADMIN", "Admin User", "admin@example.com")
    return login(client, "W1ADMIN")


@pytest.fixture
def admin_headers(admin_token):
    """Authorization headers for the admin user."""
    return auth(admin_token)


@pytest.fixture
def user_token(client, admin_token):
    """Register a second user, approve them as admin, return their JWT."""
    register(client, "W2USER", "Regular User", "user@example.com")
    users = client.get("/admin/users", headers=auth(admin_token)).json()
    pending = next(u for u in users if u["callsign"] == "W2USER")
    client.patch(f"/admin/users/{pending['id']}/approve", headers=auth(admin_token))
    return login(client, "W2USER")


@pytest.fixture
def user_headers(user_token):
    """Authorization headers for the regular (non-admin) user."""
    return auth(user_token)


@pytest.fixture
async def db():
    """Raw DB session for seeding test data directly (e.g. cache rows).
    Tests using this fixture must be `async def` (they'll need to `await`
    its query/commit calls)."""
    async with SessionLocal() as session:
        yield session


@pytest.fixture
def net(client, admin_headers):
    """Create and return a test net owned by the admin."""
    resp = client.post("/nets", json={
        "name": "Monday Night 2m Net",
        "frequency": "146.520 MHz",
        "description": "Test net",
        "is_ares": False,
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def session(client, admin_headers, net):
    """Start and return a live session on the test net."""
    resp = client.post(f"/nets/{net['id']}/sessions", json={
        "name": "Test Session",
        "notes": None,
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()
