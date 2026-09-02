"""
Tests for evacuation zone boundary syncing from external GIS APIs (issue #27):
  - evac_zone_sources.py: fetch_data_ca_gov() parsing, select_source_for_state(),
    sync_net_evac_zones() replace-on-resync behavior.
  - routers/evac_zones.py: POST /nets/{id}/evac-zone-sync,
    GET /nets/{id}/evac-zone-boundaries.

The fixture data below mirrors the real data.ca.gov ArcGIS FeatureServer's
actual field shapes, confirmed by querying it live while planning this
feature (ZONE_NAME is frequently null in practice; ZONE_ID is always
populated).
"""

import httpx
import pytest

import evac_zone_sources

SAMPLE_FEATURE_COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "COUNTY": "SAN LUIS OBISPO", "ZONE_NAME": None, "ZONE_ID": "US-CA-SLC-002",
                "STATUS": "Evacuation Warning", "EVENT_TYPE": None, "EDIT_DATE": None,
            },
            "geometry": {"type": "Polygon", "coordinates": [[[-120.6, 35.3], [-120.6, 35.4], [-120.5, 35.4], [-120.5, 35.3], [-120.6, 35.3]]]},
        },
        {
            "type": "Feature",
            "properties": {
                "COUNTY": "SAN LUIS OBISPO", "ZONE_NAME": "Downtown", "ZONE_ID": "US-CA-SLC-001",
                "STATUS": "Evacuation Order", "EVENT_TYPE": "Fire", "EDIT_DATE": "2026-08-30T12:00:00.000Z",
            },
            "geometry": {"type": "Polygon", "coordinates": [[[-120.7, 35.3], [-120.7, 35.4], [-120.6, 35.4], [-120.6, 35.3], [-120.7, 35.3]]]},
        },
    ],
}


class CallList(list):
    """Plain list can't take arbitrary attributes -- subclass so a
    swappable `.response` can be attached, matching this test suite's
    existing pushed_nets_and_stats fixture pattern in conftest.py."""
    pass


@pytest.fixture
def mock_ca_fetch(monkeypatch):
    """Monkeypatches httpx.AsyncClient so fetch_data_ca_gov never hits the
    real network -- records each request's params and returns
    `.response` (swappable mid-test for a resync scenario)."""
    calls = CallList()
    calls.response = SAMPLE_FEATURE_COLLECTION

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return calls.response

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            calls.append({"url": url, "params": params})
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return calls


def _ca_net(client, headers, region="San Luis Obispo", name="CA ARES Net"):
    resp = client.post("/nets", json={"name": name, "is_ares": True, "state": "CA", "region": region}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# fetch_data_ca_gov / select_source_for_state (no HTTP, direct unit tests)
# ---------------------------------------------------------------------------

class TestFetchDataCaGov:
    async def test_parses_features_into_normalized_dicts(self, mock_ca_fetch):
        zones = await evac_zone_sources.fetch_data_ca_gov(None)
        assert len(zones) == 2
        assert zones[0]["external_id"] == "US-CA-SLC-002"
        assert zones[0]["name"] is None   # ZONE_NAME null in real data -- must not crash
        assert zones[0]["status"] == "Evacuation Warning"
        assert zones[0]["geometry"]["type"] == "Polygon"
        assert zones[1]["name"] == "Downtown"
        assert zones[1]["source_updated_at"] is not None

    async def test_county_filter_in_query_params(self, mock_ca_fetch):
        await evac_zone_sources.fetch_data_ca_gov("San Luis Obispo")
        assert len(mock_ca_fetch) == 1
        assert mock_ca_fetch[0]["params"]["where"] == "COUNTY='SAN LUIS OBISPO'"

    async def test_no_county_queries_everything(self, mock_ca_fetch):
        await evac_zone_sources.fetch_data_ca_gov(None)
        assert mock_ca_fetch[0]["params"]["where"] == "1=1"

    async def test_county_filter_escapes_quotes(self, mock_ca_fetch):
        await evac_zone_sources.fetch_data_ca_gov("O'Brien")
        assert mock_ca_fetch[0]["params"]["where"] == "COUNTY='O''BRIEN'"

    async def test_features_missing_zone_id_are_skipped(self, mock_ca_fetch):
        mock_ca_fetch.response = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"ZONE_ID": None}, "geometry": {"type": "Polygon", "coordinates": []}}],
        }
        zones = await evac_zone_sources.fetch_data_ca_gov(None)
        assert zones == []


class TestSelectSourceForState:
    def test_matches_ca_case_insensitively(self):
        assert evac_zone_sources.select_source_for_state("ca") == "data_ca_gov"
        assert evac_zone_sources.select_source_for_state("California") == "data_ca_gov"
        assert evac_zone_sources.select_source_for_state("CA") == "data_ca_gov"

    def test_no_match_for_unsupported_or_missing_state(self):
        assert evac_zone_sources.select_source_for_state("WA") is None
        assert evac_zone_sources.select_source_for_state(None) is None
        assert evac_zone_sources.select_source_for_state("") is None


# ---------------------------------------------------------------------------
# Router: POST /nets/{id}/evac-zone-sync, GET /nets/{id}/evac-zone-boundaries
# ---------------------------------------------------------------------------

class TestSyncEndpoint:
    def test_requires_auth(self, client):
        resp = client.post("/nets/1/evac-zone-sync")
        assert resp.status_code == 401

    def test_non_ares_net_400s(self, client, admin_headers, net):
        resp = client.post(f"/nets/{net['id']}/evac-zone-sync", headers=admin_headers)
        assert resp.status_code == 400

    def test_unsupported_state_400s(self, client, admin_headers):
        n = client.post("/nets", json={"name": "WA Net", "is_ares": True, "state": "WA"}, headers=admin_headers).json()
        resp = client.post(f"/nets/{n['id']}/evac-zone-sync", headers=admin_headers)
        assert resp.status_code == 400
        assert "WA" in resp.json()["detail"]

    def test_no_state_set_400s(self, client, admin_headers):
        n = client.post("/nets", json={"name": "No State Net", "is_ares": True}, headers=admin_headers).json()
        resp = client.post(f"/nets/{n['id']}/evac-zone-sync", headers=admin_headers)
        assert resp.status_code == 400

    def test_successful_sync_creates_boundaries(self, client, admin_headers, mock_ca_fetch):
        n = _ca_net(client, admin_headers)
        resp = client.post(f"/nets/{n['id']}/evac-zone-sync", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 2

        list_resp = client.get(f"/nets/{n['id']}/evac-zone-boundaries", headers=admin_headers)
        assert list_resp.status_code == 200
        boundaries = list_resp.json()
        assert len(boundaries) == 2
        assert {b["external_id"] for b in boundaries} == {"US-CA-SLC-001", "US-CA-SLC-002"}
        downtown = next(b for b in boundaries if b["external_id"] == "US-CA-SLC-001")
        assert downtown["name"] == "Downtown"
        assert downtown["status"] == "Evacuation Order"
        assert downtown["geometry"]["type"] == "Polygon"

    def test_resync_replaces_stale_rows(self, client, admin_headers, mock_ca_fetch):
        n = _ca_net(client, admin_headers)
        client.post(f"/nets/{n['id']}/evac-zone-sync", headers=admin_headers)

        mock_ca_fetch.response = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {
                    "COUNTY": "SAN LUIS OBISPO", "ZONE_NAME": "New Zone", "ZONE_ID": "US-CA-NEW-999",
                    "STATUS": "Normal", "EVENT_TYPE": None, "EDIT_DATE": None,
                },
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
            }],
        }
        resp = client.post(f"/nets/{n['id']}/evac-zone-sync", headers=admin_headers)
        assert resp.json()["count"] == 1

        boundaries = client.get(f"/nets/{n['id']}/evac-zone-boundaries", headers=admin_headers).json()
        assert len(boundaries) == 1
        assert boundaries[0]["external_id"] == "US-CA-NEW-999"

    def test_fetch_failure_returns_502(self, client, admin_headers, monkeypatch):
        n = _ca_net(client, admin_headers)

        async def raising_fetch(county):
            raise RuntimeError("simulated network failure")

        monkeypatch.setattr(evac_zone_sources, "SOURCES", {"data_ca_gov": raising_fetch})
        resp = client.post(f"/nets/{n['id']}/evac-zone-sync", headers=admin_headers)
        assert resp.status_code == 502

    def test_boundaries_scoped_per_net(self, client, admin_headers, mock_ca_fetch):
        n1 = _ca_net(client, admin_headers, name="Net One")
        n2 = _ca_net(client, admin_headers, name="Net Two")
        client.post(f"/nets/{n1['id']}/evac-zone-sync", headers=admin_headers)

        assert len(client.get(f"/nets/{n1['id']}/evac-zone-boundaries", headers=admin_headers).json()) == 2
        assert len(client.get(f"/nets/{n2['id']}/evac-zone-boundaries", headers=admin_headers).json()) == 0


class TestBoundariesListEndpoint:
    def test_requires_auth(self, client, net):
        resp = client.get(f"/nets/{net['id']}/evac-zone-boundaries")
        assert resp.status_code == 401

    def test_empty_before_any_sync(self, client, admin_headers, net):
        resp = client.get(f"/nets/{net['id']}/evac-zone-boundaries", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []
