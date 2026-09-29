"""
Tests for the default admin account seed (issue follow-up):
  bootstrap.py's ensure_default_admin()

The whole suite runs with SEED_DEFAULT_ADMIN=false (see conftest.py) so
every existing test's "register() as the first user -> instantly
active/admin" convenience keeps working unchanged (100+ call sites across
~14 files rely on that). These tests cover the actual production bootstrap
path directly instead -- either calling ensure_default_admin() against a
raw session (no HTTP), or explicitly re-enabling seeding via
monkeypatch.setattr(bootstrap, ...) for the end-to-end cases, matching this
codebase's established "patch the module attribute directly, not the env
var" convention (module-level constants are read from os.getenv() once at
import time, so changing the env var afterward has no effect).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import bootstrap
from database import SessionLocal
from helpers import auth, login, register
from main import app
from models import User
from routers.auth import verify_password


async def _admin_count(db) -> int:
    return len((await db.execute(select(User).filter(User.is_admin == True))).scalars().all())


class TestEnsureDefaultAdmin:
    @pytest.fixture(autouse=True)
    def _seeding_enabled(self, monkeypatch):
        # The whole suite runs with SEED_DEFAULT_ADMIN=false (conftest.py) so
        # the 100+ tests relying on the OLD first-user-becomes-admin
        # convenience keep working unchanged -- these tests call
        # ensure_default_admin() directly and need it re-enabled to exercise
        # anything beyond the disabled-flag case itself.
        monkeypatch.setattr(bootstrap, "SEED_DEFAULT_ADMIN", True)

    async def test_creates_admin_when_none_exists(self, db):
        assert await _admin_count(db) == 0
        user = await bootstrap.ensure_default_admin(db)
        assert user is not None
        assert user.callsign == "ADMIN"
        assert user.is_admin is True
        assert user.is_active is True
        assert user.email_verified is True
        assert user.current_org_id is None
        assert await _admin_count(db) == 1

    async def test_noop_when_admin_already_exists(self, db):
        db.add(User(
            callsign="W1EXIST", name="Existing Admin", email="existing@example.com",
            hashed_password="x", is_active=True, is_admin=True,
        ))
        await db.commit()

        result = await bootstrap.ensure_default_admin(db)
        assert result is None
        assert await _admin_count(db) == 1   # no second admin created

    async def test_honors_password_override(self, db, monkeypatch):
        monkeypatch.setattr(bootstrap, "DEFAULT_ADMIN_PASSWORD", "fixed-test-password-123")
        user = await bootstrap.ensure_default_admin(db)
        assert verify_password("fixed-test-password-123", user.hashed_password)

    async def test_random_password_differs_each_time(self, db):
        """Two fresh instances (no fixed password) shouldn't ever coincide --
        a basic sanity check that this is actually randomly generated, not
        a hardcoded placeholder."""
        user1 = await bootstrap.ensure_default_admin(db)
        await db.delete(user1)
        await db.commit()
        user2 = await bootstrap.ensure_default_admin(db)
        assert user1.hashed_password != user2.hashed_password

    async def test_skips_on_callsign_collision_without_crashing(self, db):
        """A non-admin row already holding the reserved callsign (e.g. an
        upgraded instance) must not crash startup with an IntegrityError."""
        db.add(User(
            callsign="ADMIN", name="Someone Else", email="someone@example.com",
            hashed_password="x", is_active=True, is_admin=False,
        ))
        await db.commit()

        result = await bootstrap.ensure_default_admin(db)
        assert result is None
        assert await _admin_count(db) == 0   # still no admin -- collision, not a silent grant

    async def test_disabled_via_flag_is_a_noop(self, db, monkeypatch):
        monkeypatch.setattr(bootstrap, "SEED_DEFAULT_ADMIN", False)
        result = await bootstrap.ensure_default_admin(db)
        assert result is None
        assert await _admin_count(db) == 0


class TestDefaultAdminEndToEnd:
    """Re-enables seeding for just these tests and exercises it through a
    real TestClient (so main.py's lifespan actually runs it), proving the
    registration race is closed: even the very first self-registration in
    a fresh DB now requires approval, because the seeded admin already
    exists by the time that request is handled."""

    @pytest.fixture
    def seeded_client(self, monkeypatch):
        monkeypatch.setattr(bootstrap, "SEED_DEFAULT_ADMIN", True)
        monkeypatch.setattr(bootstrap, "DEFAULT_ADMIN_PASSWORD", "seeded-test-password-123")
        with TestClient(app) as c:
            yield c

    def test_seeded_admin_can_log_in(self, seeded_client):
        token = login(seeded_client, "ADMIN", "seeded-test-password-123")
        me = seeded_client.get("/auth/me", headers=auth(token)).json()
        assert me["is_admin"] is True
        assert me["current_org_id"] is None

    def test_first_self_registration_still_requires_approval(self, seeded_client):
        resp = register(seeded_client, "W1FIRST", "First Real User", "first@example.com")
        assert resp.status_code == 201
        data = resp.json()
        assert data["is_active"] is False
        assert data["is_admin"] is False

    async def test_restarting_does_not_reseed_or_reprint(self, seeded_client, monkeypatch, db):
        """A second app instance against the same DB (simulating a restart)
        must not create a second admin or regenerate the password -- it's
        already satisfied ("an admin exists"), so it's a no-op."""
        assert await _admin_count(db) == 1
        with TestClient(app) as c2:
            resp = c2.get("/auth/me", headers=auth(login(c2, "ADMIN", "seeded-test-password-123")))
            assert resp.status_code == 200
        assert await _admin_count(db) == 1
