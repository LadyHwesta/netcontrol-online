"""
Tests for the digital voice integration's config CRUD and hotspot-proxy
fetch (issue #26 — DMR, D-Star, YSF, NXDN, P25, M17):
  GET    /nets/{id}/dmr/config
  PUT    /nets/{id}/dmr/config
  DELETE /nets/{id}/dmr/config
  GET    /nets/{id}/dmr/lastheard

No dedicated config-CRUD test file existed before this — test_dmr_relay.py
only covers /dmr/push/raw's normalization, and test_gmrs.py only covers
the GMRS 400-block. This fills that gap while also covering the new
`mode` field, BrandMeister's DMR-only constraint, and mode-aware read
filtering added for issue #26.

Mocks httpx.get for the proxy fetch, same pattern as test_captcha.py /
test_aprs.py: monkeypatch.setattr(digital_voice.httpx, "get", ...).
"""

import pytest

from routers import digital_voice
from helpers import auth


@pytest.fixture(autouse=True)
def _clear_dmr_memory_cache():
    """_dmr_push_cache is a process-global in-memory dict, not covered by
    conftest's per-test DB row wipe. Net IDs get reused starting from 1
    each test, so without this a cache entry written by an earlier test
    could leak into a later one with the same net id."""
    digital_voice._dmr_push_cache.clear()
    yield
    digital_voice._dmr_push_cache.clear()


class TestDmrConfigCrud:
    def test_get_config_null_when_unconfigured(self, client, admin_headers, net):
        resp = client.get(f"/nets/{net['id']}/dmr/config", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() is None

    def test_put_creates_config_defaults_mode_to_dmr(self, client, admin_headers, net):
        resp = client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "hotspot_url": "http://wpsd.local/api",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["mode"] == "dmr"

    def test_put_explicit_mode_persists(self, client, admin_headers, net):
        resp = client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "mode": "ysf", "hotspot_url": "http://wpsd.local/api",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["mode"] == "ysf"

        get_resp = client.get(f"/nets/{net['id']}/dmr/config", headers=admin_headers)
        assert get_resp.json()["mode"] == "ysf"

    def test_put_upserts_mode_on_existing_config(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "mode": "dmr", "hotspot_url": "http://x",
        }, headers=admin_headers)
        resp = client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "mode": "dstar", "hotspot_url": "http://x",
        }, headers=admin_headers)
        assert resp.json()["mode"] == "dstar"

    def test_delete_removes_config(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "hotspot_url": "http://x",
        }, headers=admin_headers)
        resp = client.delete(f"/nets/{net['id']}/dmr/config", headers=admin_headers)
        assert resp.status_code == 204
        assert client.get(f"/nets/{net['id']}/dmr/config", headers=admin_headers).json() is None


class TestBrandmeisterModeConstraint:
    def test_brandmeister_with_dmr_mode_allowed(self, client, admin_headers, net):
        resp = client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "brandmeister", "mode": "dmr", "talkgroup_id": 3100,
        }, headers=admin_headers)
        assert resp.status_code == 200

    def test_brandmeister_with_non_dmr_mode_rejected(self, client, admin_headers, net):
        resp = client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "brandmeister", "mode": "ysf", "talkgroup_id": 3100,
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_brandmeister_mode_defaults_to_dmr_and_is_allowed(self, client, admin_headers, net):
        # mode isn't sent at all -> defaults to "dmr" -> compatible with brandmeister
        resp = client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "brandmeister", "talkgroup_id": 3100,
        }, headers=admin_headers)
        assert resp.status_code == 200


class TestDmrGmrsBlocked:
    def _gmrs_net(self, client, headers):
        resp = client.post("/nets", json={
            "name": "Family GMRS Net", "net_type": "gmrs", "is_ares": False,
        }, headers=headers)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def test_config_put_blocked_for_gmrs_with_mode_field(self, client, admin_headers):
        n = self._gmrs_net(client, admin_headers)
        resp = client.put(f"/nets/{n['id']}/dmr/config", json={
            "source_type": "wpsd", "mode": "ysf", "hotspot_url": "http://x",
        }, headers=admin_headers)
        assert resp.status_code == 400


class TestHotspotProxyFetch:
    """Mocks httpx.get to verify the fixed WPSD/Pi-Star field mapping and
    mode-based read-time filtering (issue #26)."""

    WPSD_RESPONSE = [
        {
            "time_utc": "2026-08-20 10:00:00", "mode": "DMR Slot 1",
            "callsign": "w1aw", "name": "Hiram Maxim", "callsign_suffix": "3109999",
            "target": "3100", "src": "RF", "duration": "5",
        },
        {
            "time_utc": "2026-08-20 10:01:00", "mode": "D-Star",
            "callsign": "K1ABC", "name": "", "callsign_suffix": "C",
            "target": "REF001 C", "src": "RF", "duration": "2",
        },
        {
            "time_utc": "2026-08-20 10:02:00", "mode": "POCSAG",
            "callsign": "DAPNET", "name": "", "callsign_suffix": "",
            "target": "", "src": "Net", "duration": "POCSAG",
        },
    ]

    def test_wpsd_proxy_filters_to_configured_mode(self, client, admin_headers, net, monkeypatch):
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "mode": "dmr", "hotspot_url": "http://wpsd.local/api",
        }, headers=admin_headers)

        calls = []

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return TestHotspotProxyFetch.WPSD_RESPONSE

        def fake_get(url, params=None, timeout=None):
            calls.append((url, params))
            return FakeResponse()

        monkeypatch.setattr(digital_voice.httpx, "get", fake_get)

        resp = client.get(f"/nets/{net['id']}/dmr/lastheard", headers=admin_headers)
        assert resp.status_code == 200
        entries = resp.json()
        callsigns = [e["callsign"] for e in entries]
        assert "W1AW" in callsigns
        assert "K1ABC" not in callsigns   # d-star entry, filtered (mode=dmr configured)
        assert "DAPNET" not in callsigns  # pocsag, always dropped

        w1aw = next(e for e in entries if e["callsign"] == "W1AW")
        assert w1aw["talk_group"] == "3100"
        assert w1aw["timeslot"] == "TS1"
        assert w1aw["region"] is None
        assert w1aw["mode"] == "dmr"

        # Real WPSD endpoint: base URL as-is, `limit` param only.
        assert len(calls) == 1
        assert calls[0][1] == {"limit": 30}

    def test_dstar_mode_config_shows_dstar_entries(self, client, admin_headers, net, monkeypatch):
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "mode": "dstar", "hotspot_url": "http://wpsd.local/api",
        }, headers=admin_headers)

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return TestHotspotProxyFetch.WPSD_RESPONSE

        monkeypatch.setattr(digital_voice.httpx, "get", lambda *a, **k: FakeResponse())

        entries = client.get(f"/nets/{net['id']}/dmr/lastheard", headers=admin_headers).json()
        callsigns = [e["callsign"] for e in entries]
        assert "K1ABC" in callsigns
        assert "W1AW" not in callsigns

        dstar_entry = next(e for e in entries if e["callsign"] == "K1ABC")
        assert dstar_entry["talk_group"] == "REF001 C"
        assert dstar_entry["timeslot"] is None
        assert dstar_entry["mode"] == "dstar"

    def test_pistar_proxy_uses_real_endpoint_and_params(self, client, admin_headers, net, monkeypatch):
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "pistar", "mode": "dmr", "hotspot_url": "http://pistar.local",
        }, headers=admin_headers)

        calls = []

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return []

        def fake_get(url, params=None, timeout=None):
            calls.append((url, params))
            return FakeResponse()

        monkeypatch.setattr(digital_voice.httpx, "get", fake_get)

        client.get(f"/nets/{net['id']}/dmr/lastheard", headers=admin_headers)
        assert len(calls) == 1
        assert calls[0][0] == "http://pistar.local/api/last_heard.php"
        assert calls[0][1] == {"num_transmissions": 30}

    def test_lastheard_without_config_404s(self, client, admin_headers, net):
        resp = client.get(f"/nets/{net['id']}/dmr/lastheard", headers=admin_headers)
        assert resp.status_code == 404
