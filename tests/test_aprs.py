"""
Tests for the APRS station map integration (issue #22):
  GET    /nets/{id}/aprs/config
  PUT    /nets/{id}/aprs/config
  DELETE /nets/{id}/aprs/config
  GET    /nets/{id}/aprs/positions
  POST   /nets/{id}/aprs/push
  GET    /nets/{id}/aprs/cache
  GET    /nets/{id}/aprs/relay-script
  GET    /public/active, /public/sessions/{id}  — aprs_map_enabled gating

Mirrors the DMR test conventions (test_gmrs.py's GMRS-block tests,
test_captcha.py's monkeypatch.setattr(main.httpx, ...) pattern for
mocking the aprs.fi HTTP call).
"""

import pytest

import main
from helpers import auth


@pytest.fixture(autouse=True)
def _clear_aprs_memory_cache():
    """_aprs_push_cache is a process-global in-memory dict (same two-tier
    cache shape as DMR's), not covered by conftest's per-test DB row wipe.
    Net IDs get reused starting from 1 each test (SQLite doesn't reset the
    id sequence on DELETE, but the tables ARE emptied), so without this a
    cache entry written by an earlier test would leak into a later test
    that happens to get the same net id — clear it on both sides."""
    main._aprs_push_cache.clear()
    yield
    main._aprs_push_cache.clear()


def make_gmrs_net(client, headers):
    resp = client.post("/nets", json={
        "name": "Family GMRS Net", "frequency": "462.550 MHz",
        "net_type": "gmrs", "is_ares": False,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestAprsConfigCrud:
    def test_get_config_null_when_unconfigured(self, client, admin_headers, net):
        resp = client.get(f"/nets/{net['id']}/aprs/config", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() is None

    def test_put_creates_config(self, client, admin_headers, net):
        resp = client.put(f"/nets/{net['id']}/aprs/config", json={
            "source_type": "relay", "filter_callsign": "w1aw",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source_type"] == "relay"
        assert body["filter_callsign"] == "W1AW"  # uppercased server-side

        get_resp = client.get(f"/nets/{net['id']}/aprs/config", headers=admin_headers)
        assert get_resp.json()["source_type"] == "relay"

    def test_put_upserts_existing_config(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        resp = client.put(f"/nets/{net['id']}/aprs/config", json={
            "source_type": "aprs_fi", "aprs_fi_api_key": "abc123",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["source_type"] == "aprs_fi"
        assert resp.json()["aprs_fi_api_key"] == "abc123"

    def test_delete_removes_config(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        resp = client.delete(f"/nets/{net['id']}/aprs/config", headers=admin_headers)
        assert resp.status_code == 204
        assert client.get(f"/nets/{net['id']}/aprs/config", headers=admin_headers).json() is None

    def test_editor_share_can_configure(self, client, admin_headers, user_headers, net, db):
        # Grant edit rights to the second user, then have them configure APRS.
        import models
        user = db.query(models.User).filter(models.User.callsign == "W2USER").first()
        resp = client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "can_edit_all": False,
            "user_ids": [user.id], "editor_user_ids": [user.id],
        }, headers=admin_headers)
        assert resp.status_code == 204, resp.text
        resp = client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=user_headers)
        assert resp.status_code == 200


class TestAprsGmrsBlocked:
    def test_config_get_blocked(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.get(f"/nets/{n['id']}/aprs/config", headers=admin_headers)
        assert resp.status_code == 400

    def test_config_put_blocked(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.put(f"/nets/{n['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        assert resp.status_code == 400

    def test_positions_blocked(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.get(f"/nets/{n['id']}/aprs/positions", headers=admin_headers)
        assert resp.status_code == 400

    def test_push_blocked(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.post(f"/nets/{n['id']}/aprs/push", json={"entries": []}, headers=admin_headers)
        assert resp.status_code == 400

    def test_cache_blocked(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.get(f"/nets/{n['id']}/aprs/cache", headers=admin_headers)
        assert resp.status_code == 400

    def test_relay_script_blocked(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.get(f"/nets/{n['id']}/aprs/relay-script", headers=admin_headers)
        assert resp.status_code == 400


class TestAprsPush:
    def test_push_requires_config(self, client, admin_headers, net):
        resp = client.post(f"/nets/{net['id']}/aprs/push", json={"entries": []}, headers=admin_headers)
        assert resp.status_code == 404

    def test_push_then_positions_roundtrip(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        resp = client.post(f"/nets/{net['id']}/aprs/push", json={"entries": [
            {"callsign": "W1AW", "lat": 41.7, "lon": -72.7, "comment": "Field team"},
        ]}, headers=admin_headers)
        assert resp.status_code == 204

        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        assert len(positions) == 1
        assert positions[0]["callsign"] == "W1AW"
        assert positions[0]["comment"] == "Field team"

    def test_filter_callsign_excluded_on_push(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/aprs/config", json={
            "source_type": "relay", "filter_callsign": "W1NCS",
        }, headers=admin_headers)
        client.post(f"/nets/{net['id']}/aprs/push", json={"entries": [
            {"callsign": "W1NCS", "lat": 41.7, "lon": -72.7},
            {"callsign": "K1ABC", "lat": 42.0, "lon": -73.0},
        ]}, headers=admin_headers)
        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        callsigns = [p["callsign"] for p in positions]
        assert "W1NCS" not in callsigns
        assert "K1ABC" in callsigns

    def test_positions_without_config_404s(self, client, admin_headers, net):
        resp = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers)
        assert resp.status_code == 404


class TestAprsCache:
    def test_cache_404_when_no_relay_data(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        resp = client.get(f"/nets/{net['id']}/aprs/cache", headers=admin_headers)
        assert resp.status_code == 404

    def test_cache_returns_pushed_data_with_age(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        client.post(f"/nets/{net['id']}/aprs/push", json={"entries": [
            {"callsign": "W1AW", "lat": 41.7, "lon": -72.7},
        ]}, headers=admin_headers)
        resp = client.get(f"/nets/{net['id']}/aprs/cache", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"][0]["callsign"] == "W1AW"
        assert body["age_seconds"] >= 0

    def test_cache_404_when_stale(self, client, admin_headers, net, monkeypatch):
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        client.post(f"/nets/{net['id']}/aprs/push", json={"entries": [
            {"callsign": "W1AW", "lat": 41.7, "lon": -72.7},
        ]}, headers=admin_headers)
        # Push the clock forward past the cache TTL.
        real_time = main._time.time
        monkeypatch.setattr(main._time, "time", lambda: real_time() + main._APRS_CACHE_TTL + 1)
        resp = client.get(f"/nets/{net['id']}/aprs/cache", headers=admin_headers)
        assert resp.status_code == 404


class TestAprsFiFetch:
    def test_positions_calls_aprsfi_and_normalizes(self, client, admin_headers, net, monkeypatch):
        client.put(f"/nets/{net['id']}/aprs/config", json={
            "source_type": "aprs_fi", "aprs_fi_api_key": "testkey",
        }, headers=admin_headers)
        # Give the net an active session with a check-in so there's a
        # watch-list of callsigns for the aprs_fi branch to query.
        client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers)
        sessions = client.get(f"/nets/{net['id']}/sessions", headers=admin_headers).json()
        session_id = sessions[0]["id"] if isinstance(sessions, list) else sessions["id"]
        client.post(f"/sessions/{session_id}/checkins", json={"callsign": "W1AW"}, headers=admin_headers)

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": "ok", "entries": [{
                    "name": "w1aw", "lat": "41.7", "lng": "-72.7",
                    "comment": "test", "symbol": "/>", "course": "088",
                    "speed": "36", "altitude": "None", "lasttime": "1700000000",
                }]}

        calls = []
        monkeypatch.setattr(main.httpx, "get", lambda *a, **k: (calls.append(1), FakeResponse())[1])

        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        assert len(calls) == 1
        assert len(positions) == 1
        entry = positions[0]
        assert entry["callsign"] == "W1AW"
        assert entry["lat"] == 41.7
        assert entry["lon"] == -72.7
        assert entry["course"] == 88
        assert entry["speed"] == 36.0
        assert entry["altitude"] is None  # tolerates the "None" string aprs.fi sends
        assert entry["heard_at"] is not None

    def test_aprsfi_result_error_returns_empty(self, client, admin_headers, net, monkeypatch):
        client.put(f"/nets/{net['id']}/aprs/config", json={
            "source_type": "aprs_fi", "aprs_fi_api_key": "testkey",
        }, headers=admin_headers)
        client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers)
        sessions = client.get(f"/nets/{net['id']}/sessions", headers=admin_headers).json()
        session_id = sessions[0]["id"] if isinstance(sessions, list) else sessions["id"]
        client.post(f"/sessions/{session_id}/checkins", json={"callsign": "W1AW"}, headers=admin_headers)

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": "fail", "description": "Invalid API key"}

        monkeypatch.setattr(main.httpx, "get", lambda *a, **k: FakeResponse())
        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        assert positions == []

    def test_aprsfi_connect_error_502(self, client, admin_headers, net, monkeypatch):
        client.put(f"/nets/{net['id']}/aprs/config", json={
            "source_type": "aprs_fi", "aprs_fi_api_key": "testkey",
        }, headers=admin_headers)
        client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers)
        sessions = client.get(f"/nets/{net['id']}/sessions", headers=admin_headers).json()
        session_id = sessions[0]["id"] if isinstance(sessions, list) else sessions["id"]
        client.post(f"/sessions/{session_id}/checkins", json={"callsign": "W1AW"}, headers=admin_headers)

        import httpx as httpx_module

        def raise_connect_error(*a, **k):
            raise httpx_module.ConnectError("boom")

        monkeypatch.setattr(main.httpx, "get", raise_connect_error)
        resp = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers)
        assert resp.status_code == 502


class TestAprsRelayScriptDownload:
    def test_download_prefills_server_netid_callsign(self, client, admin_headers, net):
        resp = client.get(f"/nets/{net['id']}/aprs/relay-script", headers=admin_headers)
        assert resp.status_code == 200
        text = resp.text
        assert f'"{net["id"]}"' in text or f'NT_NET_ID", "{net["id"]}"' in text
        assert "W1ADMIN" in text
        assert "def aprs_passcode" in text  # it's the real file, not a stub

    def test_download_requires_edit_rights(self, client, user_headers, net):
        resp = client.get(f"/nets/{net['id']}/aprs/relay-script", headers=user_headers)
        assert resp.status_code == 403


class TestAprsPublicExposure:
    def test_public_active_omits_positions_when_map_disabled(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        client.post(f"/nets/{net['id']}/aprs/push", json={"entries": [
            {"callsign": "W1AW", "lat": 41.7, "lon": -72.7},
        ]}, headers=admin_headers)
        client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers)

        resp = client.get("/public/active")
        row = next(r for r in resp.json() if r["net_name"] == net["name"])
        assert row["aprs_map_enabled"] is False
        assert row["aprs_positions"] == []

    def test_public_active_includes_positions_when_map_enabled(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}", json={
            "name": net["name"], "aprs_map_enabled": True,
        }, headers=admin_headers)
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        client.post(f"/nets/{net['id']}/aprs/push", json={"entries": [
            {"callsign": "W1AW", "lat": 41.7, "lon": -72.7},
        ]}, headers=admin_headers)
        client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers)

        resp = client.get("/public/active")
        row = next(r for r in resp.json() if r["net_name"] == net["name"])
        assert row["aprs_map_enabled"] is True
        assert len(row["aprs_positions"]) == 1
        assert row["aprs_positions"][0]["callsign"] == "W1AW"

    def test_public_session_detail_respects_toggle(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}", json={
            "name": net["name"], "aprs_map_enabled": True,
        }, headers=admin_headers)
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        client.post(f"/nets/{net['id']}/aprs/push", json={"entries": [
            {"callsign": "W1AW", "lat": 41.7, "lon": -72.7},
        ]}, headers=admin_headers)
        s = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()

        resp = client.get(f"/public/sessions/{s['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["aprs_map_enabled"] is True
        assert len(body["aprs_positions"]) == 1

    def test_gmrs_net_aprs_map_enabled_forced_false(self, client, admin_headers):
        resp = client.post("/nets", json={
            "name": "GMRS Family Net", "net_type": "gmrs",
            "aprs_map_enabled": True,
        }, headers=admin_headers)
        assert resp.status_code == 201
        # GMRS has no APRS allocation -- the flag is silently ignored server-side.
        assert resp.json()["aprs_map_enabled"] is False
