"""
Tests for incident reporting (issue #28):
  - incident_matching.py: recent_checkin_info_for_org(), scan_incident()
    (both matching signals -- EvacZone free-text roster match, and real
    point-in-polygon against Checkin.lat/lon).
  - routers/incidents.py: Incident/IncidentZone/IncidentStation CRUD, the
    scan endpoint, station status/notes updates, auth.
  - routers/public.py's GET /public/incidents: geometry + counts only,
    never a station list or callsign detail.

A small real square polygon (not a fixture copied from issue #27's real
zones) is used throughout so point-in-polygon behavior is exercised
directly and predictably, independent of any external data shape.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import incident_matching
from models import Checkin, EvacZoneBoundary, Incident, IncidentStation


def _square_geometry(min_lon=0.0, min_lat=0.0, max_lon=1.0, max_lat=1.0):
    return {"type": "Polygon", "coordinates": [[
        [min_lon, min_lat], [min_lon, max_lat], [max_lon, max_lat], [max_lon, min_lat], [min_lon, min_lat],
    ]]}


async def _add_boundary(db, net_id, name="Zone A", external_id="Z-1", geometry=None, status="Evacuation Warning"):
    boundary = EvacZoneBoundary(
        net_id=net_id, source="test_source", external_id=external_id, name=name,
        county="Test County", status=status, geometry=geometry or _square_geometry(),
    )
    db.add(boundary)
    await db.commit()
    await db.refresh(boundary)
    return boundary


def _create_incident(client, headers, net_id, zone_ids=None, title="Highway 12 Fire"):
    resp = client.post(f"/nets/{net_id}/incidents", json={
        "title": title, "description": "A test incident", "evac_zone_boundary_ids": zone_ids or [],
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# incident_matching.recent_checkin_info_for_org
# ---------------------------------------------------------------------------

class TestRecentCheckinInfoForOrg:
    async def test_only_most_recent_per_callsign_wins(self, client, admin_headers, net, session, db):
        r1 = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW", "name": "Old"}, headers=admin_headers)
        assert r1.status_code == 201, r1.text
        row = (await db.execute(select(Checkin).filter(Checkin.callsign == "W1AW"))).scalar_one()
        row.checked_in_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await db.commit()

        # A later check-in for the same callsign, in a separate session on
        # the same net -- recent_checkin_info_for_org looks across the
        # whole org's history, not just one session.
        session2 = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()
        r2 = client.post(f"/sessions/{session2['id']}/checkins", json={"callsign": "W1AW", "name": "New"}, headers=admin_headers)
        assert r2.status_code == 201, r2.text

        info = await incident_matching.recent_checkin_info_for_org(net["org_id"], db)
        assert info["W1AW"]["name"] == "New"

    async def test_ignores_checkins_outside_recency_window(self, client, admin_headers, net, session, db):
        client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "K1OLD"}, headers=admin_headers)
        row = (await db.execute(select(Checkin).filter(Checkin.callsign == "K1OLD"))).scalar_one()
        row.checked_in_at = datetime.now(timezone.utc) - timedelta(days=30)
        await db.commit()
        info = await incident_matching.recent_checkin_info_for_org(net["org_id"], db, days=14)
        assert "K1OLD" not in info

    async def test_includes_checkins_with_no_position(self, client, admin_headers, net, session, db):
        client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "N0POS"}, headers=admin_headers)
        info = await incident_matching.recent_checkin_info_for_org(net["org_id"], db)
        assert "N0POS" in info
        assert info["N0POS"]["lat"] is None


# ---------------------------------------------------------------------------
# incident_matching.scan_incident
# ---------------------------------------------------------------------------

class TestScanIncident:
    async def test_no_zones_selected_scans_to_zero(self, client, admin_headers, net, db):
        incident_data = _create_incident(client, admin_headers, net["id"])
        incident = (await db.execute(select(Incident).filter(Incident.id == incident_data["id"]))).scalar_one()
        added = await incident_matching.scan_incident(incident, db)
        assert added == 0

    async def test_zone_report_signal_matches_by_name(self, client, admin_headers, net, session, db):
        boundary = await _add_boundary(db, net["id"], name="Downtown")
        incident_data = _create_incident(client, admin_headers, net["id"], zone_ids=[boundary.id])
        client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW", "evac_zone": "Downtown"}, headers=admin_headers)

        incident = (await db.execute(select(Incident).filter(Incident.id == incident_data["id"]))).scalar_one()
        added = await incident_matching.scan_incident(incident, db)
        assert added == 1
        stations = (await db.execute(select(IncidentStation).filter(IncidentStation.incident_id == incident.id))).scalars().all()
        assert len(stations) == 1
        assert stations[0].callsign == "W1AW"
        assert stations[0].match_reason == "zone_report"

    async def test_zone_report_matches_case_insensitively(self, client, admin_headers, net, session, db):
        boundary = await _add_boundary(db, net["id"], name="Downtown")
        incident_data = _create_incident(client, admin_headers, net["id"], zone_ids=[boundary.id])
        client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW", "evac_zone": "DOWNTOWN"}, headers=admin_headers)

        incident = (await db.execute(select(Incident).filter(Incident.id == incident_data["id"]))).scalar_one()
        added = await incident_matching.scan_incident(incident, db)
        assert added == 1

    async def test_position_signal_matches_inside_polygon(self, client, admin_headers, net, session, db):
        boundary = await _add_boundary(db, net["id"], geometry=_square_geometry())
        incident_data = _create_incident(client, admin_headers, net["id"], zone_ids=[boundary.id])
        ci = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "K1ABC"}, headers=admin_headers).json()
        client.patch(f"/checkins/{ci['id']}/position", json={"lat": 0.5, "lon": 0.5}, headers=admin_headers)

        incident = (await db.execute(select(Incident).filter(Incident.id == incident_data["id"]))).scalar_one()
        added = await incident_matching.scan_incident(incident, db)
        assert added == 1
        station = (await db.execute(select(IncidentStation).filter(IncidentStation.incident_id == incident.id))).scalar_one()
        assert station.callsign == "K1ABC"
        assert station.match_reason == "position"
        assert station.last_position_lat == 0.5
        assert station.last_position_lon == 0.5

    async def test_position_outside_polygon_does_not_match(self, client, admin_headers, net, session, db):
        boundary = await _add_boundary(db, net["id"], geometry=_square_geometry())
        incident_data = _create_incident(client, admin_headers, net["id"], zone_ids=[boundary.id])
        ci = client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "K1FAR"}, headers=admin_headers).json()
        client.patch(f"/checkins/{ci['id']}/position", json={"lat": 45.0, "lon": -122.0}, headers=admin_headers)

        incident = (await db.execute(select(Incident).filter(Incident.id == incident_data["id"]))).scalar_one()
        added = await incident_matching.scan_incident(incident, db)
        assert added == 0

    async def test_rescan_never_overwrites_edited_station(self, client, admin_headers, net, session, db):
        boundary = await _add_boundary(db, net["id"], name="Downtown")
        incident_data = _create_incident(client, admin_headers, net["id"], zone_ids=[boundary.id])
        client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW", "evac_zone": "Downtown"}, headers=admin_headers)

        incident = (await db.execute(select(Incident).filter(Incident.id == incident_data["id"]))).scalar_one()
        await incident_matching.scan_incident(incident, db)

        station = (await db.execute(select(IncidentStation).filter(IncidentStation.incident_id == incident.id))).scalar_one()
        client.patch(f"/incidents/{incident.id}/stations/{station.id}", json={"status": "confirmed_safe", "notes": "All good"}, headers=admin_headers)

        added_second_time = await incident_matching.scan_incident(incident, db)
        assert added_second_time == 0
        await db.refresh(station)
        assert station.status == "confirmed_safe"
        assert station.notes == "All good"


# ---------------------------------------------------------------------------
# Router: Incident CRUD + auth
# ---------------------------------------------------------------------------

class TestIncidentCrud:
    def test_create_requires_auth(self, client, net):
        resp = client.post(f"/nets/{net['id']}/incidents", json={"title": "x"})
        assert resp.status_code == 401

    def test_create_requires_edit_rights(self, client, user_headers, net):
        resp = client.post(f"/nets/{net['id']}/incidents", json={"title": "x"}, headers=user_headers)
        assert resp.status_code == 403

    def test_create_requires_title(self, client, admin_headers, net):
        resp = client.post(f"/nets/{net['id']}/incidents", json={"title": "   "}, headers=admin_headers)
        assert resp.status_code == 400

    def test_create_and_get(self, client, admin_headers, net):
        incident = _create_incident(client, admin_headers, net["id"])
        assert incident["status"] == "active"
        assert incident["zone_ids"] == []
        assert incident["station_count"] == 0

        resp = client.get(f"/incidents/{incident['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["title"] == "Highway 12 Fire"

    def test_list_scoped_to_net(self, client, admin_headers, net):
        _create_incident(client, admin_headers, net["id"], title="One")
        n2 = client.post("/nets", json={"name": "Other Net", "is_ares": False}, headers=admin_headers).json()
        _create_incident(client, admin_headers, n2["id"], title="Two")

        resp = client.get(f"/nets/{net['id']}/incidents", headers=admin_headers)
        titles = {i["title"] for i in resp.json()}
        assert titles == {"One"}

    async def test_zone_selection_only_accepts_same_net_boundaries(self, client, admin_headers, net, db):
        other_net = client.post("/nets", json={"name": "Other Net 2", "is_ares": False}, headers=admin_headers).json()
        foreign_boundary = await _add_boundary(db, other_net["id"], name="Foreign Zone")
        incident = _create_incident(client, admin_headers, net["id"], zone_ids=[foreign_boundary.id])
        assert incident["zone_ids"] == []   # silently dropped, not an error

    async def test_update_title_description_status_zones(self, client, admin_headers, net, db):
        boundary = await _add_boundary(db, net["id"])
        incident = _create_incident(client, admin_headers, net["id"])
        resp = client.patch(f"/incidents/{incident['id']}", json={
            "title": "Renamed", "description": "Updated", "status": "resolved", "evac_zone_boundary_ids": [boundary.id],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["title"] == "Renamed"
        assert body["status"] == "resolved"
        assert body["resolved_at"] is not None
        assert body["zone_ids"] == [boundary.id]

    def test_update_rejects_invalid_status(self, client, admin_headers, net):
        incident = _create_incident(client, admin_headers, net["id"])
        resp = client.patch(f"/incidents/{incident['id']}", json={"status": "on_fire"}, headers=admin_headers)
        assert resp.status_code == 400

    def test_delete(self, client, admin_headers, net):
        incident = _create_incident(client, admin_headers, net["id"])
        resp = client.delete(f"/incidents/{incident['id']}", headers=admin_headers)
        assert resp.status_code == 204
        assert client.get(f"/incidents/{incident['id']}", headers=admin_headers).status_code == 404

    def test_nonexistent_incident_404s(self, client, admin_headers):
        resp = client.get("/incidents/999999", headers=admin_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Router: scan endpoint + station roster
# ---------------------------------------------------------------------------

class TestIncidentStations:
    def test_scan_endpoint_shape(self, client, admin_headers, net):
        """Real matching behavior (zone_report/position signals, add-only
        upsert) is covered directly against incident_matching.scan_incident()
        in TestScanIncident above -- this just confirms the endpoint wires
        up correctly for an incident with no zones selected."""
        incident = _create_incident(client, admin_headers, net["id"])
        resp = client.post(f"/incidents/{incident['id']}/scan", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == {"added": 0}

    def test_manual_add_list_update_remove_station(self, client, admin_headers, net):
        incident = _create_incident(client, admin_headers, net["id"])

        add_resp = client.post(f"/incidents/{incident['id']}/stations", json={"callsign": "w1aw", "name": "Alice"}, headers=admin_headers)
        assert add_resp.status_code == 201, add_resp.text
        station = add_resp.json()
        assert station["callsign"] == "W1AW"   # normalized uppercase
        assert station["match_reason"] == "manual"
        assert station["status"] == "not_contacted"

        list_resp = client.get(f"/incidents/{incident['id']}/stations", headers=admin_headers)
        assert len(list_resp.json()) == 1

        dup_resp = client.post(f"/incidents/{incident['id']}/stations", json={"callsign": "W1AW"}, headers=admin_headers)
        assert dup_resp.status_code == 409

        patch_resp = client.patch(f"/incidents/{incident['id']}/stations/{station['id']}", json={"status": "contacted", "notes": "Reached by phone"}, headers=admin_headers)
        assert patch_resp.status_code == 200
        assert patch_resp.json()["status"] == "contacted"
        assert patch_resp.json()["notes"] == "Reached by phone"

        bad_status_resp = client.patch(f"/incidents/{incident['id']}/stations/{station['id']}", json={"status": "on_fire"}, headers=admin_headers)
        assert bad_status_resp.status_code == 400

        del_resp = client.delete(f"/incidents/{incident['id']}/stations/{station['id']}", headers=admin_headers)
        assert del_resp.status_code == 204
        assert client.get(f"/incidents/{incident['id']}/stations", headers=admin_headers).json() == []

    def test_station_endpoints_require_edit_rights(self, client, user_headers, admin_headers, net):
        incident = _create_incident(client, admin_headers, net["id"])
        resp = client.post(f"/incidents/{incident['id']}/stations", json={"callsign": "W1AW"}, headers=user_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Public endpoint
# ---------------------------------------------------------------------------

class TestPublicIncidents:
    def test_no_org_returns_empty(self, client):
        resp = client.get("/public/incidents", params={"org": "no-such-org"})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_active_incident_shown_with_geometry_and_count_only(self, client, admin_headers, net, db):
        boundary = await _add_boundary(db, net["id"], name="Downtown")
        incident = _create_incident(client, admin_headers, net["id"], zone_ids=[boundary.id])
        client.post(f"/incidents/{incident['id']}/stations", json={"callsign": "W1AW", "name": "Alice"}, headers=admin_headers)

        orgs = client.get("/orgs/mine", headers=admin_headers).json()
        slug = orgs[0]["slug"]

        resp = client.get("/public/incidents", params={"org": slug})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["title"] == "Highway 12 Fire"
        assert body[0]["station_count"] == 1
        assert len(body[0]["zones"]) == 1
        assert body[0]["zones"][0]["geometry"]["type"] == "Polygon"
        # Never leaks a callsign or station list
        assert "W1AW" not in str(body)
        assert "stations" not in body[0]

    async def test_resolved_incident_excluded(self, client, admin_headers, net, db):
        incident = _create_incident(client, admin_headers, net["id"])
        client.patch(f"/incidents/{incident['id']}", json={"status": "resolved"}, headers=admin_headers)

        orgs = client.get("/orgs/mine", headers=admin_headers).json()
        slug = orgs[0]["slug"]
        resp = client.get("/public/incidents", params={"org": slug})
        assert resp.json() == []
