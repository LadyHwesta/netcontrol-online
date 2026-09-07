"""
Tests for GMRS net support (issue #6).

Covers:
- net_type field on create/update
- GMRS nets allow duplicate callsigns (shared family licence)
- GMRS nets block DMR endpoints
- Activation & Incident Response (is_ares) works the same for GMRS nets as
  ham nets (issue follow-up -- previously ham-only, forced False for GMRS)
"""

from helpers import auth


def make_gmrs_net(client, headers):
    resp = client.post("/nets", json={
        "name": "Family GMRS Net",
        "frequency": "462.550 MHz",
        "net_type": "gmrs",
        "is_ares": False,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_ham_net(client, headers):
    resp = client.post("/nets", json={
        "name": "Monday Ham Net",
        "frequency": "146.520 MHz",
        "net_type": "ham",
        "is_ares": False,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestNetType:
    def test_create_ham_net_has_ham_type(self, client, admin_headers):
        resp = client.post("/nets", json={"name": "Ham Net", "net_type": "ham"}, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["net_type"] == "ham"

    def test_create_gmrs_net_has_gmrs_type(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        assert n["net_type"] == "gmrs"

    def test_default_net_type_is_ham(self, client, admin_headers):
        """Net created without net_type defaults to ham."""
        resp = client.post("/nets", json={"name": "No Type Net"}, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["net_type"] == "ham"

    def test_invalid_net_type_defaults_to_ham(self, client, admin_headers):
        """Unknown net_type values are silently coerced to ham."""
        resp = client.post("/nets", json={"name": "Bad Type", "net_type": "frs"}, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["net_type"] == "ham"

    def test_update_ham_net_to_gmrs(self, client, admin_headers):
        n = make_ham_net(client, admin_headers)
        resp = client.put(f"/nets/{n['id']}", json={
            "name": n["name"], "net_type": "gmrs",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["net_type"] == "gmrs"

    def test_gmrs_net_can_enable_activation_and_incident_response(self, client, admin_headers):
        """is_ares works the same for GMRS nets as ham nets (issue
        follow-up) -- previously forced False, now honored on create."""
        resp = client.post("/nets", json={
            "name": "GMRS Activation Net",
            "net_type": "gmrs",
            "is_ares": True,
        }, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["is_ares"] is True

    def test_gmrs_net_can_enable_it_via_update_too(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        assert n["is_ares"] is False
        resp = client.put(f"/nets/{n['id']}", json={
            "name": n["name"], "net_type": "gmrs", "is_ares": True,
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["is_ares"] is True


class TestGmrsDuplicateCallsigns:
    def _start_session(self, client, headers, net_id):
        resp = client.post(f"/nets/{net_id}/sessions", json={"name": "Test", "notes": None}, headers=headers)
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_gmrs_allows_same_callsign_twice(self, client, admin_headers):
        """Two stations from the same GMRS family licence can both check in."""
        n = make_gmrs_net(client, admin_headers)
        sid = self._start_session(client, admin_headers, n["id"])

        r1 = client.post(f"/sessions/{sid}/checkins", json={
            "callsign": "WQXH777", "name": "Dad",
        }, headers=admin_headers)
        assert r1.status_code == 201

        r2 = client.post(f"/sessions/{sid}/checkins", json={
            "callsign": "WQXH777", "name": "Mom",
        }, headers=admin_headers)
        assert r2.status_code == 201, f"Expected 201, got {r2.status_code}: {r2.text}"

    def test_gmrs_multiple_stations_all_appear_in_list(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        sid = self._start_session(client, admin_headers, n["id"])

        for name in ("Dad", "Mom", "Kid"):
            client.post(f"/sessions/{sid}/checkins", json={
                "callsign": "WQXH777", "name": name,
            }, headers=admin_headers)

        checkins = client.get(f"/sessions/{sid}/checkins", headers=admin_headers).json()
        assert len(checkins) == 3
        names = {c["name"] for c in checkins}
        assert names == {"Dad", "Mom", "Kid"}

    def test_ham_net_still_rejects_duplicate_callsign(self, client, admin_headers):
        n = make_ham_net(client, admin_headers)
        sid = self._start_session(client, admin_headers, n["id"])

        client.post(f"/sessions/{sid}/checkins", json={"callsign": "W1AW"}, headers=admin_headers)
        r2 = client.post(f"/sessions/{sid}/checkins", json={"callsign": "W1AW"}, headers=admin_headers)
        assert r2.status_code == 409


class TestGmrsDmrBlocked:
    def test_dmr_config_get_blocked_for_gmrs(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.get(f"/nets/{n['id']}/dmr/config", headers=admin_headers)
        assert resp.status_code == 400

    def test_dmr_config_put_blocked_for_gmrs(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.put(f"/nets/{n['id']}/dmr/config", json={
            "source_type": "wpsd", "hotspot_url": "http://x", "direct_mode": False,
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_dmr_push_blocked_for_gmrs(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.post(f"/nets/{n['id']}/dmr/push", json={"entries": []}, headers=admin_headers)
        assert resp.status_code == 400

    def test_dmr_push_raw_blocked_for_gmrs(self, client, admin_headers):
        n = make_gmrs_net(client, admin_headers)
        resp = client.post(f"/nets/{n['id']}/dmr/push/raw", json={
            "source": "wpsd", "entries": [],
        }, headers=admin_headers)
        assert resp.status_code == 400
