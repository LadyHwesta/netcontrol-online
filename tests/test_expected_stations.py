"""
Tests for GET /nets/{id}/expected and its "Almost There" companion:
  GET /nets/{id}/expected/almost

The almost-qualifying endpoint returns stations exactly one check-in short
of the min_checkins bar within the same window -- not on the main Expected
Stations list yet, but one more check-in to this net is what would put them
on it next time.
"""


def _checkin(client, admin_headers, net, callsign, name="Test Station"):
    """One check-in in its own (immediately ended) session."""
    s = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()
    client.post(f"/sessions/{s['id']}/checkins", json={
        "callsign": callsign, "name": name, "has_traffic": False,
    }, headers=admin_headers)
    client.patch(f"/sessions/{s['id']}/end", headers=admin_headers)


class TestExpectedStations:
    def test_below_threshold_not_listed(self, client, admin_headers, net):
        _checkin(client, admin_headers, net, "W7ABC")  # 1 checkin, default min_checkins=2

        resp = client.get(f"/nets/{net['id']}/expected", headers=admin_headers)
        assert resp.status_code == 200
        assert "W7ABC" not in {s["callsign"] for s in resp.json()}

    def test_at_threshold_listed(self, client, admin_headers, net):
        for _ in range(2):
            _checkin(client, admin_headers, net, "W7ABC")

        resp = client.get(f"/nets/{net['id']}/expected", headers=admin_headers)
        assert resp.status_code == 200
        station = next(s for s in resp.json() if s["callsign"] == "W7ABC")
        assert station["checkin_count"] == 2


class TestAlmostExpectedStations:
    def test_one_short_of_default_threshold_is_listed(self, client, admin_headers, net):
        _checkin(client, admin_headers, net, "W7ABC")  # 1 of 2 needed

        resp = client.get(f"/nets/{net['id']}/expected/almost", headers=admin_headers)
        assert resp.status_code == 200
        station = next(s for s in resp.json() if s["callsign"] == "W7ABC")
        assert station["checkin_count"] == 1

    def test_qualifying_station_not_in_almost_list(self, client, admin_headers, net):
        for _ in range(2):
            _checkin(client, admin_headers, net, "W7ABC")  # already qualifies

        resp = client.get(f"/nets/{net['id']}/expected/almost", headers=admin_headers)
        assert resp.status_code == 200
        assert "W7ABC" not in {s["callsign"] for s in resp.json()}

    def test_higher_threshold_needs_more_checkins_to_drop_off_almost_list(self, client, admin_headers, net):
        for _ in range(2):
            _checkin(client, admin_headers, net, "W7ABC")  # 2 of 3 needed

        resp = client.get(f"/nets/{net['id']}/expected/almost?min_checkins=3", headers=admin_headers)
        assert resp.status_code == 200
        station = next(s for s in resp.json() if s["callsign"] == "W7ABC")
        assert station["checkin_count"] == 2

    def test_min_checkins_of_one_returns_empty_list(self, client, admin_headers, net):
        """With min_checkins=1, "one short" would mean zero checkins -- not a
        meaningful list of specific stations, so this is defined as empty."""
        _checkin(client, admin_headers, net, "W7ABC")

        resp = client.get(f"/nets/{net['id']}/expected/almost?min_checkins=1", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_uses_preferred_name_like_main_list(self, client, admin_headers, net):
        _checkin(client, admin_headers, net, "W7ABC", name="Robert Smith")
        client.put(f"/nets/{net['id']}/stations/W7ABC/remark", json={
            "preferred_name": "Bob",
        }, headers=admin_headers)

        resp = client.get(f"/nets/{net['id']}/expected/almost", headers=admin_headers)
        assert resp.status_code == 200
        station = next(s for s in resp.json() if s["callsign"] == "W7ABC")
        assert station["name"] == "Bob"

    def test_requires_editable_net_access(self, client, user_headers, net):
        resp = client.get(f"/nets/{net['id']}/expected/almost", headers=user_headers)
        assert resp.status_code == 403
