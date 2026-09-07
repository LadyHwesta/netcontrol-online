"""
Tests for the scheduled-maintenance notice (nightly deploy cron helper):
  POST /internal/maintenance-notice
  GET  /maintenance-notice

See routers/maintenance.py's own module docstring for why the state is a
plain in-memory global rather than a database row -- the intent is that a
service restart (i.e. the deploy this notice announces) clears it for free.
"""

from datetime import datetime, timedelta, timezone

import pytest

from routers import maintenance


@pytest.fixture(autouse=True)
def _reset_maintenance_state():
    """_armed_until is a process-global, not a DB row -- not covered by
    conftest's per-test DB wipe, so reset it on both sides same as the
    APRS/DMR in-memory caches do."""
    maintenance._armed_until = None
    yield
    maintenance._armed_until = None


def test_arm_endpoint_disabled_when_secret_unset(client, monkeypatch):
    monkeypatch.setattr(maintenance, "DEPLOY_NOTICE_SECRET", "")
    resp = client.post("/internal/maintenance-notice", headers={"X-Deploy-Secret": "anything"})
    assert resp.status_code == 404


def test_arm_endpoint_rejects_wrong_secret(client, monkeypatch):
    monkeypatch.setattr(maintenance, "DEPLOY_NOTICE_SECRET", "correct-secret")
    resp = client.post("/internal/maintenance-notice", headers={"X-Deploy-Secret": "wrong"})
    assert resp.status_code == 403
    # Rejected attempt must not have armed the notice.
    assert client.get("/maintenance-notice").json()["active"] is False


def test_arm_endpoint_rejects_missing_secret(client, monkeypatch):
    monkeypatch.setattr(maintenance, "DEPLOY_NOTICE_SECRET", "correct-secret")
    resp = client.post("/internal/maintenance-notice")
    assert resp.status_code == 403


def test_notice_inactive_by_default(client):
    resp = client.get("/maintenance-notice")
    assert resp.status_code == 200
    assert resp.json()["active"] is False


def test_arming_makes_notice_active(client, monkeypatch):
    monkeypatch.setattr(maintenance, "DEPLOY_NOTICE_SECRET", "correct-secret")
    arm_resp = client.post("/internal/maintenance-notice", headers={"X-Deploy-Secret": "correct-secret"})
    assert arm_resp.status_code == 200
    assert arm_resp.json() == {"armed": True}

    get_resp = client.get("/maintenance-notice")
    assert get_resp.json()["active"] is True
    assert "update" in get_resp.json()["message"].lower()


def test_notice_expires_on_its_own(client, monkeypatch):
    """Covers the --skip-if-unchanged case: no restart ever clears the
    in-memory flag, so it must still stop showing on its own eventually."""
    monkeypatch.setattr(maintenance, "DEPLOY_NOTICE_SECRET", "correct-secret")
    client.post("/internal/maintenance-notice", headers={"X-Deploy-Secret": "correct-secret"})
    assert client.get("/maintenance-notice").json()["active"] is True

    # Simulate the armed window having already elapsed.
    maintenance._armed_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.get("/maintenance-notice").json()["active"] is False


def test_restart_clears_notice(client, monkeypatch):
    """The real-world 'update finished' path: nothing calls a clear
    endpoint, the process restarting is what resets the module global."""
    monkeypatch.setattr(maintenance, "DEPLOY_NOTICE_SECRET", "correct-secret")
    client.post("/internal/maintenance-notice", headers={"X-Deploy-Secret": "correct-secret"})
    assert client.get("/maintenance-notice").json()["active"] is True

    # A fresh import-time value, exactly what a process restart produces.
    maintenance._armed_until = None
    assert client.get("/maintenance-notice").json()["active"] is False
