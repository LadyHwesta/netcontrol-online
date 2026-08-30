"""
Tests for the /dmr/push/raw endpoint.

Verifies that raw hotspot JSON is normalized server-side correctly,
so the relay script can be a pure proxy with no normalization logic.

WPSD/Pi-Star field names below match the REAL dashboard API response
shape (confirmed against the actual source of WPSD-M17/WPSD-WebCode and
f1rmb/Pi-Star_DV_Dash, not just docs) -- {time_utc, mode, callsign, name,
callsign_suffix, target, src, duration}. There is no top-level slot/dst/
country/start key in the real payload (issue #26 fix).
"""


RAW_WPSD_ENTRY = {
    "time_utc": "2026-08-17 12:00:00",
    "mode": "DMR Slot 2",
    "callsign": "w1aw",
    "name": "Hiram Maxim",
    "callsign_suffix": "3109999",
    "target": "3100",
    "src": "RF",
    "duration": "5",
}

RAW_WPSD_YSF_ENTRY = {
    "time_utc": "2026-08-17 12:05:00",
    "mode": "YSF",
    "callsign": "K1ABC",
    "name": "Jane Doe",
    "callsign_suffix": "",
    "target": "US Fusion",
    "src": "RF",
    "duration": "3",
}

RAW_WPSD_POCSAG_ENTRY = {
    "time_utc": "2026-08-17 12:06:00",
    "mode": "POCSAG",
    "callsign": "DAPNET",
    "name": "",
    "callsign_suffix": "",
    "target": "",
    "src": "Net",
    "duration": "POCSAG",
}

RAW_BRANDMEISTER_ENTRY = {
    "callsign": "W2XYZ",
    "SourceID": "3109998",
    "sourceName": "Jane Smith",
    "DestinationID": "3100",
    "slot": 1,
    "start": 1750000000,
    "stop": 1750000007,
    "sourceState": "CT",
    "sourceCountry": "US",
}


class TestDmrPushRaw:
    def test_wpsd_entries_normalized_correctly(self, client, admin_headers, net):
        # Configure DMR for this net
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd",
            "mode": "dmr",
            "hotspot_url": "http://wpsd.local/api",
            "direct_mode": False,
        }, headers=admin_headers)

        resp = client.post(f"/nets/{net['id']}/dmr/push/raw", json={
            "source": "wpsd",
            "entries": [RAW_WPSD_ENTRY],
        }, headers=admin_headers)
        assert resp.status_code == 204

        # Verify cached data is normalized
        cache = client.get(f"/nets/{net['id']}/dmr/cache", headers=admin_headers)
        assert cache.status_code == 200
        entry = cache.json()["entries"][0]
        assert entry["callsign"] == "W1AW"           # uppercased
        assert entry["dmr_id"] == "3109999"          # callsign_suffix → dmr_id
        assert entry["talk_group"] == "3100"         # target → talk_group
        assert entry["timeslot"] == "TS2"            # "DMR Slot 2" → TS2
        assert entry["region"] is None               # no such data in the real feed
        assert entry["heard_at"] == "2026-08-17 12:00:00"  # time_utc → heard_at
        assert entry["mode"] == "dmr"

    def test_ysf_entry_normalized_and_visible_when_mode_matches(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "mode": "ysf",
            "hotspot_url": "http://wpsd.local/api", "direct_mode": False,
        }, headers=admin_headers)

        resp = client.post(f"/nets/{net['id']}/dmr/push/raw", json={
            "source": "wpsd", "entries": [RAW_WPSD_YSF_ENTRY],
        }, headers=admin_headers)
        assert resp.status_code == 204

        cache = client.get(f"/nets/{net['id']}/dmr/cache", headers=admin_headers)
        entry = cache.json()["entries"][0]
        assert entry["callsign"] == "K1ABC"
        assert entry["talk_group"] == "US Fusion"
        assert entry["timeslot"] is None   # only DMR has timeslots
        assert entry["mode"] == "ysf"

    def test_pocsag_entries_dropped(self, client, admin_headers, net):
        """POCSAG is paging, not a voice check-in concern -- dropped entirely (issue #26)."""
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "mode": "dmr",
            "hotspot_url": "http://wpsd.local/api", "direct_mode": False,
        }, headers=admin_headers)

        resp = client.post(f"/nets/{net['id']}/dmr/push/raw", json={
            "source": "wpsd", "entries": [RAW_WPSD_ENTRY, RAW_WPSD_POCSAG_ENTRY],
        }, headers=admin_headers)
        assert resp.status_code == 204

        cache = client.get(f"/nets/{net['id']}/dmr/cache", headers=admin_headers)
        callsigns = [e["callsign"] for e in cache.json()["entries"]]
        assert "DAPNET" not in callsigns
        assert "W1AW" in callsigns

    def test_mixed_mode_push_filtered_to_configured_mode_on_read(self, client, admin_headers, net):
        """Push endpoints cache everything a relay sends regardless of mode;
        read endpoints (/dmr/cache, /dmr/lastheard) narrow to cfg.mode --
        so one relay on a mixed-mode hotspot can serve any net (issue #26)."""
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "mode": "dmr",
            "hotspot_url": "http://wpsd.local/api", "direct_mode": False,
        }, headers=admin_headers)

        resp = client.post(f"/nets/{net['id']}/dmr/push/raw", json={
            "source": "wpsd", "entries": [RAW_WPSD_ENTRY, RAW_WPSD_YSF_ENTRY],
        }, headers=admin_headers)
        assert resp.status_code == 204

        cache = client.get(f"/nets/{net['id']}/dmr/cache", headers=admin_headers)
        callsigns = [e["callsign"] for e in cache.json()["entries"]]
        assert "W1AW" in callsigns       # dmr entry -- matches configured mode
        assert "K1ABC" not in callsigns  # ysf entry -- filtered out at read time

    def test_brandmeister_entries_normalized_correctly(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "brandmeister",
            "mode": "dmr",
            "talkgroup_id": 3100,
            "direct_mode": False,
        }, headers=admin_headers)

        resp = client.post(f"/nets/{net['id']}/dmr/push/raw", json={
            "source": "brandmeister",
            "entries": [RAW_BRANDMEISTER_ENTRY],
        }, headers=admin_headers)
        assert resp.status_code == 204

        cache = client.get(f"/nets/{net['id']}/dmr/cache", headers=admin_headers)
        entry = cache.json()["entries"][0]
        assert entry["callsign"] == "W2XYZ"
        assert entry["dmr_id"] == "3109998"          # SourceID → dmr_id
        assert entry["talk_group"] == "3100"         # DestinationID → talk_group
        assert entry["timeslot"] == "TS1"
        assert entry["mode"] == "dmr"

    def test_unknown_source_returns_400(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd", "hotspot_url": "http://x", "direct_mode": False,
        }, headers=admin_headers)

        resp = client.post(f"/nets/{net['id']}/dmr/push/raw", json={
            "source": "invalid_source",
            "entries": [RAW_WPSD_ENTRY],
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_filter_callsign_applied_after_normalization(self, client, admin_headers, net):
        """NCS callsign should be filtered out even when sent as raw lowercase."""
        client.put(f"/nets/{net['id']}/dmr/config", json={
            "source_type": "wpsd",
            "mode": "dmr",
            "hotspot_url": "http://wpsd.local/api",
            "filter_callsign": "W1AW",
            "direct_mode": False,
        }, headers=admin_headers)

        resp = client.post(f"/nets/{net['id']}/dmr/push/raw", json={
            "source": "wpsd",
            "entries": [RAW_WPSD_ENTRY],  # callsign is "w1aw" (lowercase)
        }, headers=admin_headers)
        assert resp.status_code == 204

        cache = client.get(f"/nets/{net['id']}/dmr/cache", headers=admin_headers)
        assert cache.json()["entries"] == []

    def test_unauthenticated_cannot_push_raw(self, client, net):
        resp = client.post(f"/nets/{net['id']}/dmr/push/raw", json={
            "source": "wpsd", "entries": [],
        })
        assert resp.status_code == 401
