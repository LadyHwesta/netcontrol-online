"""
Tests for evacuation zone boundary syncing from external GIS APIs (issue #27):
  - evac_zone_sources.py: fetch_data_ca_gov()/fetch_sonoma_county_gov()/
    fetch_santa_rosa_ca_gov() parsing, select_source_for_state()/
    select_sources_for_county(), sync_net_evac_zones() multi-source +
    replace-on-resync behavior.
  - routers/evac_zones.py: POST /nets/{id}/evac-zone-sync,
    GET /nets/{id}/evac-zone-boundaries.

The fixture data below mirrors each real ArcGIS FeatureServer's actual
field shapes, confirmed by querying both live while building this feature
(data.ca.gov's ZONE_NAME is frequently null in practice, ZONE_ID always
populated; Sonoma County's own catalog carries a zone_status of "Normal"
on every zone outside an active incident, unlike the statewide feed which
never contains a "Normal" row at all -- see evac_zone_sources.py's module
docstring for why both exist).
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

SAMPLE_SONOMA_COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"Jurisdiction": "City of Sonoma", "ZoneNumber": "SO-C01", "zone_status": "Normal", "Summary": "Southeast City of Sonoma"},
            "geometry": {"type": "Polygon", "coordinates": [[[-122.5, 38.2], [-122.5, 38.3], [-122.4, 38.3], [-122.4, 38.2], [-122.5, 38.2]]]},
        },
        {
            "type": "Feature",
            "properties": {"Jurisdiction": "City of Rohnert Park", "ZoneNumber": "RP-001", "zone_status": "Normal", "Summary": "Northwest Rohnert Park"},
            "geometry": {"type": "Polygon", "coordinates": [[[-122.7, 38.3], [-122.7, 38.4], [-122.6, 38.4], [-122.6, 38.3], [-122.7, 38.3]]]},
        },
    ],
}

SAMPLE_SANTA_ROSA_COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"Jurisdiction": "City of Santa Rosa", "ZoneNumber": "SRS-Southeast2", "Zone_Status": None, "ShortName": "Southeast2"},
            "geometry": {"type": "Polygon", "coordinates": [[[-122.65, 38.42], [-122.65, 38.43], [-122.64, 38.43], [-122.64, 38.42], [-122.65, 38.42]]]},
        },
        {
            "type": "Feature",
            "properties": {"Jurisdiction": "City of Santa Rosa", "ZoneNumber": "SRS-Fountaingrove1", "Zone_Status": None, "ShortName": "Fountaingrove1"},
            "geometry": {"type": "Polygon", "coordinates": [[[-122.68, 38.47], [-122.68, 38.48], [-122.67, 38.48], [-122.67, 38.47], [-122.68, 38.47]]]},
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
    """Monkeypatches httpx.AsyncClient so neither fetch_data_ca_gov nor
    fetch_sonoma_county_gov ever hits the real network -- records each
    request's params and routes the response by URL, so a single fixture
    covers a net that syncs from both sources at once. `.response`/
    `.sonoma_response` are swappable mid-test (e.g. for a resync
    scenario)."""
    calls = CallList()
    calls.response = SAMPLE_FEATURE_COLLECTION
    calls.sonoma_response = SAMPLE_SONOMA_COLLECTION
    calls.santa_rosa_response = SAMPLE_SANTA_ROSA_COLLECTION

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            calls.append({"url": url, "params": params})
            if url == evac_zone_sources.SONOMA_FEATURE_SERVER_QUERY_URL:
                return FakeResponse(calls.sonoma_response)
            if url == evac_zone_sources.SANTA_ROSA_FEATURE_SERVER_QUERY_URL:
                return FakeResponse(calls.santa_rosa_response)
            return FakeResponse(calls.response)

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

    async def test_county_filter_strips_county_suffix(self, mock_ca_fetch):
        """Net.region's own established placeholder ("Snohomish County",
        predating this feature) actively invites typing the county WITH
        "County" on the end -- the source's COUNTY field never has that
        suffix (e.g. "SONOMA", not "SONOMA COUNTY"), so left unstripped
        this silently matched nothing for every net that followed the
        field's own hint. Found on a real deploy (region set to "Sonoma
        County") before this fix."""
        await evac_zone_sources.fetch_data_ca_gov("Sonoma County")
        assert mock_ca_fetch[0]["params"]["where"] == "COUNTY='SONOMA'"

    async def test_county_filter_suffix_strip_is_case_insensitive(self, mock_ca_fetch):
        await evac_zone_sources.fetch_data_ca_gov("sonoma county")
        assert mock_ca_fetch[0]["params"]["where"] == "COUNTY='SONOMA'"

    async def test_features_missing_zone_id_are_skipped(self, mock_ca_fetch):
        mock_ca_fetch.response = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"ZONE_ID": None}, "geometry": {"type": "Polygon", "coordinates": []}}],
        }
        zones = await evac_zone_sources.fetch_data_ca_gov(None)
        assert zones == []


class TestFetchSonomaCountyGov:
    async def test_parses_features_into_normalized_dicts(self, mock_ca_fetch):
        zones = await evac_zone_sources.fetch_sonoma_county_gov(None)
        assert len(zones) == 2
        assert zones[0]["external_id"] == "SO-C01"
        assert zones[0]["name"] == "Southeast City of Sonoma"
        assert zones[0]["county"] == "City of Sonoma"
        assert zones[0]["status"] == "Normal"   # confirmed live: the whole catalog, not an active-only feed
        assert zones[0]["geometry"]["type"] == "Polygon"

    async def test_no_county_filter_applied(self, mock_ca_fetch):
        """Unlike fetch_data_ca_gov, this service is already scoped to
        Sonoma -- the county argument is accepted (registry signature
        consistency) but never used to filter."""
        await evac_zone_sources.fetch_sonoma_county_gov("anything at all")
        assert mock_ca_fetch[0]["params"]["where"] == "1=1"

    async def test_features_missing_zone_number_are_skipped(self, mock_ca_fetch):
        mock_ca_fetch.sonoma_response = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"ZoneNumber": None}, "geometry": {"type": "Polygon", "coordinates": []}}],
        }
        zones = await evac_zone_sources.fetch_sonoma_county_gov(None)
        assert zones == []


class TestFetchSantaRosaCaGov:
    async def test_parses_features_into_normalized_dicts(self, mock_ca_fetch):
        zones = await evac_zone_sources.fetch_santa_rosa_ca_gov(None)
        assert len(zones) == 2
        assert zones[0]["external_id"] == "SRS-Southeast2"
        assert zones[0]["name"] == "Southeast2"   # the user's own zone, confirmed live
        assert zones[0]["county"] == "City of Santa Rosa"
        assert zones[0]["status"] is None   # confirmed live: Zone_Status is null on every row here

    async def test_features_missing_zone_number_are_skipped(self, mock_ca_fetch):
        mock_ca_fetch.santa_rosa_response = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"ZoneNumber": None}, "geometry": {"type": "Polygon", "coordinates": []}}],
        }
        zones = await evac_zone_sources.fetch_santa_rosa_ca_gov(None)
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


class TestSelectSourcesForCounty:
    def test_sonoma_matches_both_the_county_and_santa_rosa_sources(self):
        """Sonoma County's own layer explicitly excludes the City of
        Santa Rosa -- a net whose region is just "Sonoma County" needs
        BOTH sources to actually see every jurisdiction in the county,
        so both must match, not just one."""
        assert set(evac_zone_sources.select_sources_for_county("Sonoma")) == {"sonoma_county_gov", "santa_rosa_ca_gov"}
        assert set(evac_zone_sources.select_sources_for_county("Sonoma County")) == {"sonoma_county_gov", "santa_rosa_ca_gov"}
        assert set(evac_zone_sources.select_sources_for_county("sonoma county")) == {"sonoma_county_gov", "santa_rosa_ca_gov"}

    def test_santa_rosa_alone_matches_only_the_santa_rosa_source(self):
        assert evac_zone_sources.select_sources_for_county("Santa Rosa") == ["santa_rosa_ca_gov"]

    def test_no_match_for_unsupported_or_missing_region(self):
        assert evac_zone_sources.select_sources_for_county("Marin") == []
        assert evac_zone_sources.select_sources_for_county(None) == []
        assert evac_zone_sources.select_sources_for_county("") == []


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

    def test_county_source_works_even_with_unsupported_state(self, client, admin_headers, mock_ca_fetch):
        """A net whose State doesn't match any state-level source can
        still sync purely from county-level sources -- these match
        independently. Region "Sonoma County" matches BOTH
        sonoma_county_gov and santa_rosa_ca_gov (see COUNTY_ALIASES's own
        comment for why -- Sonoma County's own layer excludes Santa
        Rosa's zones)."""
        n = client.post("/nets", json={
            "name": "Sonoma-only Net", "is_ares": True, "state": "OR", "region": "Sonoma County",
        }, headers=admin_headers).json()
        resp = client.post(f"/nets/{n['id']}/evac-zone-sync", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 4   # 2 from sonoma_county_gov + 2 from santa_rosa_ca_gov

        boundaries = client.get(f"/nets/{n['id']}/evac-zone-boundaries", headers=admin_headers).json()
        assert {b["source"] for b in boundaries} == {"sonoma_county_gov", "santa_rosa_ca_gov"}

    def test_syncs_state_and_all_matching_county_sources_at_once(self, client, admin_headers, mock_ca_fetch):
        """A CA net whose Region also matches registered county sources
        pulls from all of them at once -- the statewide active-incidents
        feed AND every full catalog for that region (here: the county's
        own plus Santa Rosa's, since the county's excludes Santa Rosa) --
        coexisting via the (net_id, source, external_id) uniqueness
        EvacZoneBoundary was designed for."""
        n = client.post("/nets", json={
            "name": "Sonoma ARES Net", "is_ares": True, "state": "CA", "region": "Sonoma County",
        }, headers=admin_headers).json()
        resp = client.post(f"/nets/{n['id']}/evac-zone-sync", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 6   # 2 data_ca_gov + 2 sonoma_county_gov + 2 santa_rosa_ca_gov

        boundaries = client.get(f"/nets/{n['id']}/evac-zone-boundaries", headers=admin_headers).json()
        assert len(boundaries) == 6
        assert {b["source"] for b in boundaries} == {"data_ca_gov", "sonoma_county_gov", "santa_rosa_ca_gov"}
        assert any(b["external_id"] == "SRS-Southeast2" for b in boundaries)   # the user's own zone, confirmed live

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
