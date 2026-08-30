"""
Tests for check-in endpoints:
  GET    /sessions/{id}/checkins
  POST   /sessions/{id}/checkins
  DELETE /checkins/{id}
  PATCH  /checkins/{id}/traffic
  PATCH  /checkins/{id}/traffic-called
"""


def make_gmrs_net(client, headers):
    resp = client.post("/nets", json={
        "name": "Family GMRS Net", "frequency": "462.550 MHz",
        "net_type": "gmrs", "is_ares": False,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestAddCheckin:
    def test_add_basic_checkin(self, client, admin_headers, session):
        resp = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7KOL",
            "name": "Test Station",
            "signal_report": "59",
            "has_traffic": False,
        }, headers=admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["callsign"] == "W7KOL"
        assert data["name"] == "Test Station"
        assert data["signal_report"] == "59"
        assert data["has_traffic"] is False

    def test_callsign_stored_uppercase(self, client, admin_headers, session):
        resp = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "w7kol", "has_traffic": False,
        }, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["callsign"] == "W7KOL"

    def test_add_checkin_with_traffic(self, client, admin_headers, session):
        resp = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7TRF", "has_traffic": True,
        }, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["has_traffic"] is True

    def test_duplicate_callsign_rejected(self, client, admin_headers, session):
        """Same callsign cannot check in twice to the same session."""
        client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7DUP", "has_traffic": False,
        }, headers=admin_headers)
        resp = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7DUP", "has_traffic": False,
        }, headers=admin_headers)
        assert resp.status_code == 409

    def test_same_callsign_different_sessions(self, client, admin_headers, net):
        """The same callsign can appear in different sessions of the same net."""
        s1 = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()
        s2 = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()

        r1 = client.post(f"/sessions/{s1['id']}/checkins", json={
            "callsign": "W7REP", "has_traffic": False,
        }, headers=admin_headers)
        r2 = client.post(f"/sessions/{s2['id']}/checkins", json={
            "callsign": "W7REP", "has_traffic": False,
        }, headers=admin_headers)

        assert r1.status_code == 201
        assert r2.status_code == 201

    def test_cannot_checkin_ended_session(self, client, admin_headers, session):
        client.patch(f"/sessions/{session['id']}/end", headers=admin_headers)
        resp = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7LATE", "has_traffic": False,
        }, headers=admin_headers)
        assert resp.status_code == 400


class TestListCheckins:
    def test_list_checkins(self, client, admin_headers, session):
        client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7AAA", "has_traffic": False,
        }, headers=admin_headers)
        client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7BBB", "has_traffic": False,
        }, headers=admin_headers)

        resp = client.get(f"/sessions/{session['id']}/checkins", headers=admin_headers)
        assert resp.status_code == 200
        callsigns = [c["callsign"] for c in resp.json()]
        assert "W7AAA" in callsigns
        assert "W7BBB" in callsigns

    def test_empty_session_returns_empty_list(self, client, admin_headers, session):
        resp = client.get(f"/sessions/{session['id']}/checkins", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_checkin_count_increments(self, client, admin_headers, session):
        client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7CNT", "has_traffic": False,
        }, headers=admin_headers)
        resp = client.get(f"/sessions/{session['id']}", headers=admin_headers)
        assert resp.json()["checkin_count"] == 1


class TestRemoveCheckin:
    def test_remove_checkin(self, client, admin_headers, session):
        add = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7DEL", "has_traffic": False,
        }, headers=admin_headers)
        checkin_id = add.json()["id"]

        resp = client.delete(f"/checkins/{checkin_id}", headers=admin_headers)
        assert resp.status_code == 204

    def test_remove_checkin_reduces_count(self, client, admin_headers, session):
        add = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7DEL", "has_traffic": False,
        }, headers=admin_headers)
        checkin_id = add.json()["id"]

        client.delete(f"/checkins/{checkin_id}", headers=admin_headers)
        resp = client.get(f"/sessions/{session['id']}", headers=admin_headers)
        assert resp.json()["checkin_count"] == 0

    def test_remove_nonexistent_checkin_returns_404(self, client, admin_headers):
        resp = client.delete("/checkins/99999", headers=admin_headers)
        assert resp.status_code == 404


class TestTrafficToggle:
    def test_toggle_traffic_on(self, client, admin_headers, session):
        add = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7TRF", "has_traffic": False,
        }, headers=admin_headers)
        checkin_id = add.json()["id"]

        resp = client.patch(f"/checkins/{checkin_id}/traffic", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["has_traffic"] is True

    def test_toggle_traffic_off(self, client, admin_headers, session):
        """Toggling again should turn traffic flag off."""
        add = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7TRF", "has_traffic": True,
        }, headers=admin_headers)
        checkin_id = add.json()["id"]

        resp = client.patch(f"/checkins/{checkin_id}/traffic", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["has_traffic"] is False


class TestTrafficCalledToggle:
    """issue #15 -- the traffic "called" checkbox must persist server-side so it
    survives a session close/reopen, unlike the old client-side-only Set."""

    def test_new_checkin_defaults_uncalled(self, client, admin_headers, session):
        add = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7TRF", "has_traffic": True,
        }, headers=admin_headers)
        assert add.json()["traffic_called"] is False

    def test_toggle_called_on(self, client, admin_headers, session):
        add = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7TRF", "has_traffic": True,
        }, headers=admin_headers)
        checkin_id = add.json()["id"]

        resp = client.patch(f"/checkins/{checkin_id}/traffic-called", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["traffic_called"] is True

    def test_toggle_called_off(self, client, admin_headers, session):
        add = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7TRF", "has_traffic": True,
        }, headers=admin_headers)
        checkin_id = add.json()["id"]
        client.patch(f"/checkins/{checkin_id}/traffic-called", headers=admin_headers)

        resp = client.patch(f"/checkins/{checkin_id}/traffic-called", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["traffic_called"] is False

    def test_called_state_survives_session_end_and_refetch(self, client, admin_headers, session):
        """The actual bug in #15: reopening a closed session should still show
        the checkbox checked."""
        add = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7TRF", "has_traffic": True,
        }, headers=admin_headers)
        checkin_id = add.json()["id"]
        client.patch(f"/checkins/{checkin_id}/traffic-called", headers=admin_headers)

        client.patch(f"/sessions/{session['id']}/end", json={}, headers=admin_headers)

        resp = client.get(f"/sessions/{session['id']}/checkins", headers=admin_headers)
        assert resp.status_code == 200
        checkin = next(c for c in resp.json() if c["id"] == checkin_id)
        assert checkin["traffic_called"] is True


class TestFirstCheckin:
    """Welcome first-time operators: a station's very first check-in to a
    given net (across all its sessions) is flagged is_first_checkin."""

    def test_first_ever_checkin_flagged(self, client, admin_headers, session):
        resp = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7NEW", "has_traffic": False,
        }, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["is_first_checkin"] is True

    def test_second_checkin_same_session_not_flagged(self, client, admin_headers, net):
        """GMRS allows the same callsign multiple times in one session --
        only the first occurrence should be flagged."""
        gnet = make_gmrs_net(client, admin_headers)
        s = client.post(f"/nets/{gnet['id']}/sessions", json={}, headers=admin_headers).json()

        r1 = client.post(f"/sessions/{s['id']}/checkins", json={
            "callsign": "WQXH1234", "name": "Dad", "has_traffic": False,
        }, headers=admin_headers)
        r2 = client.post(f"/sessions/{s['id']}/checkins", json={
            "callsign": "WQXH1234", "name": "Mom", "has_traffic": False,
        }, headers=admin_headers)
        assert r1.json()["is_first_checkin"] is True
        assert r2.json()["is_first_checkin"] is False

    def test_return_visitor_in_later_session_not_flagged(self, client, admin_headers, net):
        s1 = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()
        s2 = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()

        r1 = client.post(f"/sessions/{s1['id']}/checkins", json={
            "callsign": "W7RET", "has_traffic": False,
        }, headers=admin_headers)
        r2 = client.post(f"/sessions/{s2['id']}/checkins", json={
            "callsign": "W7RET", "has_traffic": False,
        }, headers=admin_headers)
        assert r1.json()["is_first_checkin"] is True
        assert r2.json()["is_first_checkin"] is False

    def test_same_callsign_new_to_a_different_net(self, client, admin_headers, net):
        """History is scoped per-net -- a station's history on one net doesn't
        make them a "return visitor" on an unrelated net."""
        other_net = client.post("/nets", json={"name": "Other Net"}, headers=admin_headers).json()
        s1 = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()
        s2 = client.post(f"/nets/{other_net['id']}/sessions", json={}, headers=admin_headers).json()

        client.post(f"/sessions/{s1['id']}/checkins", json={
            "callsign": "W7TWO", "has_traffic": False,
        }, headers=admin_headers)
        resp = client.post(f"/sessions/{s2['id']}/checkins", json={
            "callsign": "W7TWO", "has_traffic": False,
        }, headers=admin_headers)
        assert resp.json()["is_first_checkin"] is True

    def test_flag_persists_through_list_checkins(self, client, admin_headers, session):
        add = client.post(f"/sessions/{session['id']}/checkins", json={
            "callsign": "W7LST", "has_traffic": False,
        }, headers=admin_headers)
        checkin_id = add.json()["id"]

        resp = client.get(f"/sessions/{session['id']}/checkins", headers=admin_headers)
        checkin = next(c for c in resp.json() if c["id"] == checkin_id)
        assert checkin["is_first_checkin"] is True
