"""
Tests for manually-reported GPS checkin positions (issue follow-up):
  PATCH /checkins/{id}/position
  GET   /nets/{id}/aprs/positions   -- merge behavior with automated APRS data

For an operator with no APRS capability who can read off their own
coordinates over the air. Deliberately independent of AprsConfig -- works
on a ham net with zero APRS setup at all, not just as a supplement to it.
"""

import pytest

from routers import aprs


@pytest.fixture(autouse=True)
def _clear_aprs_memory_cache():
    """_aprs_push_cache is a process-global in-memory dict, not covered by
    conftest's per-test DB row wipe -- same reasoning as test_aprs.py's own
    copy of this fixture, needed here too since a couple of the merge tests
    below also push relay data."""
    aprs._aprs_push_cache.clear()
    yield
    aprs._aprs_push_cache.clear()


def _net_and_active_session(client, headers, name="Position Test Net"):
    net = client.post("/nets", json={"name": name, "is_ares": False}, headers=headers).json()
    session = client.post(f"/nets/{net['id']}/sessions", json={}, headers=headers).json()
    return net, session


class TestSetCheckinPosition:
    def test_requires_auth(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        resp = client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3})
        assert resp.status_code == 401

    def test_set_and_read_back(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        resp = client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6062, "lon": -122.3321}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["lat"] == 47.6062
        assert resp.json()["lon"] == -122.3321

        listed = client.get(f"/sessions/{session['id']}/checkins", headers=admin_headers).json()
        assert listed[0]["lat"] == 47.6062
        assert listed[0]["lon"] == -122.3321

    def test_defaults_to_null(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        assert checkin["lat"] is None
        assert checkin["lon"] is None

    def test_clear_position(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3}, headers=admin_headers)
        resp = client.patch(f"/checkins/{checkin['id']}/position", json={"lat": None, "lon": None}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["lat"] is None
        assert resp.json()["lon"] is None

    def test_lat_without_lon_rejected(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        resp = client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6}, headers=admin_headers)
        assert resp.status_code == 400

    def test_lat_out_of_range_rejected(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        resp = client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 95, "lon": 0}, headers=admin_headers)
        assert resp.status_code == 422

    def test_lon_out_of_range_rejected(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        resp = client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 0, "lon": 200}, headers=admin_headers)
        assert resp.status_code == 422

    def test_requires_net_access(self, client, admin_headers, user_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        resp = client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3}, headers=user_headers)
        assert resp.status_code == 403

    def test_404_for_missing_checkin(self, client, admin_headers):
        resp = client.patch("/checkins/999999/position", json={"lat": 47.6, "lon": -122.3}, headers=admin_headers)
        assert resp.status_code == 404


class TestManualPositionsOnMap:
    """GET /nets/{id}/aprs/positions merges manually-reported checkin
    positions in, even with zero AprsConfig on the net at all -- the whole
    point (not all operators have APRS capability)."""

    def test_manual_position_appears_with_no_aprs_config_at_all(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3}, headers=admin_headers)

        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        assert len(positions) == 1
        assert positions[0]["callsign"] == "W1AW"
        assert positions[0]["lat"] == 47.6
        assert positions[0]["lon"] == -122.3
        assert positions[0]["source"] == "manual"

    def test_manual_position_includes_comment_and_heard_at(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W1AW", "comments": "On the summit",
        }, headers=admin_headers).json()
        client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3}, headers=admin_headers)

        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        assert positions[0]["comment"] == "On the summit"
        assert positions[0]["heard_at"] is not None

    def test_checkin_without_position_not_included(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers)

        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        assert positions == []

    def test_manual_position_merges_with_automated_relay_positions(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        client.post(f"/nets/{net['id']}/aprs/push", json={"entries": [
            {"callsign": "K1ABC", "lat": 41.7, "lon": -72.7},
        ]}, headers=admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3}, headers=admin_headers)

        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        by_callsign = {p["callsign"]: p for p in positions}
        assert by_callsign["K1ABC"]["source"] == "relay"
        assert by_callsign["W1AW"]["source"] == "manual"

    def test_real_aprs_position_wins_over_the_same_callsigns_manual_one(self, client, admin_headers):
        """A callsign already reporting via real APRS shouldn't also show a
        second, overlapping manual pin for itself."""
        net, session = _net_and_active_session(client, admin_headers)
        client.put(f"/nets/{net['id']}/aprs/config", json={"source_type": "relay"}, headers=admin_headers)
        client.post(f"/nets/{net['id']}/aprs/push", json={"entries": [
            {"callsign": "W1AW", "lat": 41.7, "lon": -72.7},
        ]}, headers=admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3}, headers=admin_headers)

        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        assert len(positions) == 1
        assert positions[0]["source"] == "relay"
        assert positions[0]["lat"] == 41.7   # the real APRS position, not the manual one

    def test_manual_position_respects_filter_callsign(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        client.put(f"/nets/{net['id']}/aprs/config", json={
            "source_type": "relay", "filter_callsign": "W1NCS",
        }, headers=admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "w1ncs"}, headers=admin_headers).json()
        client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3}, headers=admin_headers)

        positions = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers).json()
        assert positions == []

    def test_manual_position_shown_on_public_page_when_map_enabled(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        client.put(f"/nets/{net['id']}", json={"name": net["name"], "aprs_map_enabled": True}, headers=admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3}, headers=admin_headers)

        resp = client.get("/public/active")
        row = next(r for r in resp.json() if r["net_name"] == net["name"])
        assert len(row["aprs_positions"]) == 1
        assert row["aprs_positions"][0]["source"] == "manual"
        assert row["aprs_source"] is None   # no AprsConfig at all -- no aprs.fi credit owed

    def test_manual_position_hidden_on_public_page_when_map_disabled(self, client, admin_headers):
        net, session = _net_and_active_session(client, admin_headers)
        checkin = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW"}, headers=admin_headers).json()
        client.patch(f"/checkins/{checkin['id']}/position", json={"lat": 47.6, "lon": -122.3}, headers=admin_headers)

        resp = client.get("/public/active")
        row = next(r for r in resp.json() if r["net_name"] == net["name"])
        assert row["aprs_positions"] == []

    def test_gmrs_net_still_blocked_even_with_manual_positions(self, client, admin_headers):
        """Manual positions don't create a GMRS loophole around
        _assert_ham_net -- GMRS still has no allocation for a station map."""
        net = client.post("/nets", json={
            "name": "GMRS Net", "net_type": "gmrs", "is_ares": False,
        }, headers=admin_headers).json()
        resp = client.get(f"/nets/{net['id']}/aprs/positions", headers=admin_headers)
        assert resp.status_code == 400
