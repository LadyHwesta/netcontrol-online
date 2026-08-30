"""
Tests for GET /nets/{id}/history -- callsign check-in history/frequency
across all of a net's sessions.

No dedicated coverage existed for this endpoint before -- which is how a
real bug shipped: net_history()'s "most recent ended session" lookup used
scalar_one_or_none(), which raises MultipleResultsFound (-> 500) for any
net that's actually been run more than once, since any net used across
multiple sessions has more than one row matching "ended session for this
net" by design. Found live against a real deployment; every test in
TestMultipleEndedSessions below reproduces some version of that shape.
"""


def _net(client, headers, name="History Net"):
    resp = client.post("/nets", json={"name": name, "is_ares": False}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _run_ended_session(client, headers, net_id, callsign, name=None):
    """Start a session, check in one station, end it. Returns the session dict."""
    s = client.post(f"/nets/{net_id}/sessions", json={}, headers=headers).json()
    resp = client.post(f"/sessions/{s['id']}/checkins", json={
        "callsign": callsign, "name": name,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    end = client.patch(f"/sessions/{s['id']}/end", json={}, headers=headers)
    assert end.status_code == 200, end.text
    return s


class TestNetHistoryBasic:
    def test_empty_net_returns_empty_list(self, client, admin_headers):
        net = _net(client, admin_headers)
        resp = client.get(f"/nets/{net['id']}/history?limit=1000", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_single_session_single_checkin(self, client, admin_headers):
        net = _net(client, admin_headers)
        _run_ended_session(client, admin_headers, net["id"], "K1ABC", "Alice")
        resp = client.get(f"/nets/{net['id']}/history?limit=1000", headers=admin_headers)
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["callsign"] == "K1ABC"
        assert rows[0]["name"] == "Alice"
        assert rows[0]["total_checkins"] == 1
        assert rows[0]["checked_in_last_session"] is True

    def test_unauthenticated_cannot_view(self, client, admin_headers):
        net = _net(client, admin_headers)
        resp = client.get(f"/nets/{net['id']}/history")
        assert resp.status_code == 401


class TestMultipleEndedSessions:
    """The actual regression: net_history() 500ing for any net with more
    than one ended session, via a scalar_one_or_none() that should have
    been a take-the-first lookup."""

    def test_two_ended_sessions_does_not_500(self, client, admin_headers):
        net = _net(client, admin_headers)
        _run_ended_session(client, admin_headers, net["id"], "K1ABC", "Alice")
        _run_ended_session(client, admin_headers, net["id"], "K2XYZ", "Bob")

        resp = client.get(f"/nets/{net['id']}/history?limit=1000", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        callsigns = {r["callsign"] for r in resp.json()}
        assert callsigns == {"K1ABC", "K2XYZ"}

    def test_many_ended_sessions_does_not_500(self, client, admin_headers):
        net = _net(client, admin_headers)
        for i in range(5):
            _run_ended_session(client, admin_headers, net["id"], f"K{i}AAA", f"Op{i}")

        resp = client.get(f"/nets/{net['id']}/history?limit=1000", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 5

    def test_checked_in_last_session_reflects_most_recent(self, client, admin_headers):
        net = _net(client, admin_headers)
        _run_ended_session(client, admin_headers, net["id"], "K1ABC", "Alice")
        _run_ended_session(client, admin_headers, net["id"], "K2XYZ", "Bob")
        third = _run_ended_session(client, admin_headers, net["id"], "K3QQQ", "Carol")

        rows = client.get(f"/nets/{net['id']}/history?limit=1000", headers=admin_headers).json()
        by_call = {r["callsign"]: r for r in rows}
        assert by_call["K3QQQ"]["checked_in_last_session"] is True
        assert by_call["K1ABC"]["checked_in_last_session"] is False
        assert by_call["K2XYZ"]["checked_in_last_session"] is False

    def test_same_callsign_across_multiple_sessions_aggregates(self, client, admin_headers):
        net = _net(client, admin_headers)
        _run_ended_session(client, admin_headers, net["id"], "K1ABC", "Alice")
        _run_ended_session(client, admin_headers, net["id"], "K1ABC", "Alice")
        _run_ended_session(client, admin_headers, net["id"], "K1ABC", "Alice")

        rows = client.get(f"/nets/{net['id']}/history?limit=1000", headers=admin_headers).json()
        assert len(rows) == 1
        assert rows[0]["total_checkins"] == 3

    def test_history_still_works_with_one_active_and_several_ended_sessions(self, client, admin_headers):
        """A net can have sessions in flight at the same time as a long
        history of past (ended) ones -- history shouldn't care that one is
        still live."""
        net = _net(client, admin_headers)
        _run_ended_session(client, admin_headers, net["id"], "K1ABC", "Alice")
        _run_ended_session(client, admin_headers, net["id"], "K2XYZ", "Bob")
        # A currently-active (not ended) session on top
        client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers)

        resp = client.get(f"/nets/{net['id']}/history?limit=1000", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 2
