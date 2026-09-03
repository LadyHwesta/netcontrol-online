"""
Tests for the live hazard feed on the Incidents page (issue follow-up to
#28/#27):
  - incident_feed_sources.py: fetch_*() parsing (real field shapes,
    confirmed by querying each live source while building this feature),
    significance filters, list_feed_items_for_net()'s county resolution,
    concurrent-fetch resilience (one source failing doesn't blank the
    rest), suggested_zone_ids via shapely, dismissal filtering.
  - routers/incident_feed.py: GET /nets/{id}/incident-feed,
    POST /nets/{id}/incident-feed/dismiss.
"""

import httpx
import pytest

import incident_feed_sources
from models import EvacZoneBoundary

SAMPLE_USGS = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "nc75429277",
            "properties": {
                "mag": 3.54, "place": "3 km ESE of Larkfield-Wikiup, CA",
                "time": 1788456807360, "title": "M 3.5 - 3 km ESE of Larkfield-Wikiup, CA",
                "url": "https://earthquake.usgs.gov/earthquakes/eventpage/nc75429277",
            },
            "geometry": {"type": "Point", "coordinates": [-122.7, 38.55, 6.9]},
        },
    ],
}

SAMPLE_CALFIRE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.75, 38.5]},
            "properties": {
                "Name": "Test Fire ", "County": "Sonoma", "IsActive": True,
                "AcresBurned": 1200.0, "PercentContained": 40.0,
                "Started": "2026-08-09T03:35:00Z", "Location": "Near Test Rd",
                "UniqueId": "calfire-test-1", "Url": "https://www.fire.ca.gov/incidents/2026/test-fire/",
            },
        },
        {
            # Inactive -- filtered out by IsActive check.
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.75, 38.5]},
            "properties": {"Name": "Old Fire", "County": "Sonoma", "IsActive": False, "UniqueId": "calfire-old"},
        },
        {
            # Different county -- filtered out.
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-121.0, 36.0]},
            "properties": {"Name": "Monterey Fire", "County": "Monterey", "IsActive": True, "UniqueId": "calfire-other"},
        },
    ],
}

SAMPLE_NWS = {
    "type": "FeatureCollection",
    "features": [
        {
            "id": "urn:oid:test-severe",
            "geometry": {"type": "Polygon", "coordinates": [[[-122.8, 38.4], [-122.8, 38.6], [-122.6, 38.6], [-122.6, 38.4], [-122.8, 38.4]]]},
            "properties": {
                "severity": "Severe", "headline": "Red Flag Warning", "event": "Red Flag Warning",
                "description": "Critical fire weather.", "sent": "2026-09-03T06:40:00-07:00",
                "@id": "https://api.weather.gov/alerts/urn:oid:test-severe",
            },
        },
        {
            # Minor severity -- filtered out.
            "id": "urn:oid:test-minor",
            "geometry": None,
            "properties": {"severity": "Minor", "headline": "Small Craft Advisory", "sent": "2026-09-03T06:40:00-07:00"},
        },
    ],
}

SAMPLE_CALOES = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.62, 38.45]},
            "properties": {
                "County": "SONOMA", "ImpactedCustomers": 1200, "OutageStatus": "Active",
                "UtilityCompany": "PGE", "Cause": None, "StartDate": 1788457989000,
                "IncidentId": "347333",
            },
        },
        {
            # Below MIN_OUTAGE_CUSTOMERS -- filtered out.
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.62, 38.45]},
            "properties": {"County": "SONOMA", "ImpactedCustomers": 1, "OutageStatus": "Active", "IncidentId": "1"},
        },
    ],
}


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def mock_feed_fetch(monkeypatch):
    """Monkeypatches httpx.AsyncClient so all four fetch_*() functions
    hit canned responses instead of the real network, routed by URL --
    same pattern as tests/test_evac_zone_sync.py's mock_ca_fetch. Any of
    the four `.responses[...]` can be swapped to an Exception instance
    mid-test to simulate that one source failing."""
    responses = {
        incident_feed_sources.USGS_QUERY_URL: SAMPLE_USGS,
        incident_feed_sources.CALFIRE_INCIDENTS_URL: SAMPLE_CALFIRE,
        incident_feed_sources.CALOES_POWER_OUTAGES_URL: SAMPLE_CALOES,
    }
    nws_url = incident_feed_sources.NWS_ALERTS_URL.format(ugc="CAC097")
    responses[nws_url] = SAMPLE_NWS

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None, headers=None):
            data = responses[url]
            if isinstance(data, Exception):
                raise data
            return FakeResponse(data)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return responses


def _sonoma_net(client, headers, name="Sonoma ARES Net", is_ares=True):
    resp = client.post("/nets", json={"name": name, "is_ares": is_ares, "state": "CA", "region": "Sonoma County"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# fetch_*() parsing (no HTTP, direct unit tests against real-shaped fixtures)
# ---------------------------------------------------------------------------

class TestFetchParsing:
    async def test_fetch_usgs_earthquakes(self, mock_feed_fetch):
        items = await incident_feed_sources.fetch_usgs_earthquakes("SONOMA")
        assert len(items) == 1
        item = items[0]
        assert item["source"] == "usgs_earthquakes"
        assert item["external_id"] == "nc75429277"
        assert item["category"] == "earthquake"
        assert item["severity"] == "M3.5"
        assert item["lat"] == 38.55 and item["lon"] == -122.7

    async def test_fetch_calfire_incidents_filters_inactive_and_other_county(self, mock_feed_fetch):
        items = await incident_feed_sources.fetch_calfire_incidents("SONOMA")
        assert len(items) == 1
        item = items[0]
        assert item["external_id"] == "calfire-test-1"
        assert item["title"] == "Test Fire"
        assert item["category"] == "wildfire"
        assert "1,200 ac" in item["severity"]
        assert "40% contained" in item["severity"]

    async def test_fetch_nws_alerts_filters_minor_severity(self, mock_feed_fetch):
        items = await incident_feed_sources.fetch_nws_alerts("SONOMA")
        assert len(items) == 1
        item = items[0]
        assert item["external_id"] == "urn:oid:test-severe"
        assert item["category"] == "weather_alert"
        assert item["severity"] == "Severe"
        assert item["geometry"]["type"] == "Polygon"

    async def test_fetch_caloes_power_outages_filters_small_outages(self, mock_feed_fetch):
        items = await incident_feed_sources.fetch_caloes_power_outages("SONOMA")
        assert len(items) == 1
        item = items[0]
        assert item["external_id"] == "347333"
        assert item["category"] == "power_outage"
        assert "1,200 customers" in item["title"]


# ---------------------------------------------------------------------------
# list_feed_items_for_net() -- orchestration
# ---------------------------------------------------------------------------

class TestListFeedItemsForNet:
    async def test_unconfigured_region_via_api(self, client, admin_headers, mock_feed_fetch):
        net = client.post("/nets", json={"name": "Nowhere Net", "is_ares": True, "region": "Nowhere County"}, headers=admin_headers).json()
        resp = client.get(f"/nets/{net['id']}/incident-feed", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["county"] is None
        assert body["items"] == []

    async def test_all_four_sources_combined(self, client, admin_headers, mock_feed_fetch):
        net = _sonoma_net(client, admin_headers)
        resp = client.get(f"/nets/{net['id']}/incident-feed", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["county"] == "SONOMA"
        assert body["sources_failed"] == []
        categories = {i["category"] for i in body["items"]}
        assert categories == {"earthquake", "wildfire", "weather_alert", "power_outage"}
        assert len(body["items"]) == 4

    async def test_one_source_failing_does_not_blank_the_rest(self, client, admin_headers, mock_feed_fetch):
        mock_feed_fetch[incident_feed_sources.CALOES_POWER_OUTAGES_URL] = httpx.ConnectError("boom")
        net = _sonoma_net(client, admin_headers)
        resp = client.get(f"/nets/{net['id']}/incident-feed", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sources_failed"] == ["caloes_power_outages"]
        categories = {i["category"] for i in body["items"]}
        assert categories == {"earthquake", "wildfire", "weather_alert"}

    async def test_suggested_zone_ids_from_synced_boundaries(self, client, admin_headers, mock_feed_fetch, db):
        net = _sonoma_net(client, admin_headers)
        # A boundary polygon that contains the sample earthquake's point
        # (-122.7, 38.55) but not the wildfire's (-122.75, 38.5).
        boundary_geo = {"type": "Polygon", "coordinates": [[[-122.72, 38.5], [-122.72, 38.6], [-122.68, 38.6], [-122.68, 38.5], [-122.72, 38.5]]]}
        db.add(EvacZoneBoundary(
            net_id=net["id"], source="test", external_id="Z1", name="Test Zone",
            county="Sonoma", status=None, geometry=boundary_geo,
        ))
        await db.commit()

        resp = client.get(f"/nets/{net['id']}/incident-feed", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        items = {i["category"]: i for i in resp.json()["items"]}
        assert items["earthquake"]["suggested_zone_ids"] != []
        assert items["wildfire"]["suggested_zone_ids"] == []

    async def test_dismissed_item_excluded_from_next_fetch(self, client, admin_headers, mock_feed_fetch):
        net = _sonoma_net(client, admin_headers)
        resp = client.get(f"/nets/{net['id']}/incident-feed", headers=admin_headers)
        earthquake = next(i for i in resp.json()["items"] if i["category"] == "earthquake")

        dismiss_resp = client.post(
            f"/nets/{net['id']}/incident-feed/dismiss",
            json={"source": earthquake["source"], "external_id": earthquake["external_id"]},
            headers=admin_headers,
        )
        assert dismiss_resp.status_code == 204, dismiss_resp.text

        resp2 = client.get(f"/nets/{net['id']}/incident-feed", headers=admin_headers)
        categories = {i["category"] for i in resp2.json()["items"]}
        assert "earthquake" not in categories

    async def test_dismiss_upsert_same_item_twice(self, client, admin_headers, mock_feed_fetch):
        net = _sonoma_net(client, admin_headers)
        resp = client.get(f"/nets/{net['id']}/incident-feed", headers=admin_headers)
        earthquake = next(i for i in resp.json()["items"] if i["category"] == "earthquake")
        body = {"source": earthquake["source"], "external_id": earthquake["external_id"]}

        assert client.post(f"/nets/{net['id']}/incident-feed/dismiss", json=body, headers=admin_headers).status_code == 204
        # Same item, now with an incident_id -- should update in place, not conflict.
        body2 = dict(body, incident_id=1)
        assert client.post(f"/nets/{net['id']}/incident-feed/dismiss", json=body2, headers=admin_headers).status_code == 204

    async def test_non_editor_cannot_view_feed(self, client, admin_headers, user_headers, mock_feed_fetch):
        net = _sonoma_net(client, admin_headers)
        resp = client.get(f"/nets/{net['id']}/incident-feed", headers=user_headers)
        assert resp.status_code in (403, 404)
