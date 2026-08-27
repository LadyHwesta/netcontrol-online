"""
Tests for bulk check-in CSV import (issue #26):
  POST /sessions/{id}/checkins/import
  GET  /checkins/import-sample
"""

import io


def _csv_file(content: str, filename: str = "checkins.csv"):
    return {"file": (filename, io.BytesIO(content.encode()), "text/csv")}


class TestImportCheckinsCsv:
    def test_basic_import(self, client, admin_headers, session):
        csv_body = (
            "Callsign,Name,Signal Report,Comments\n"
            "W1AW,Hiram Maxim,59,First check-in\n"
            "KJ7ABC,Jane Doe,55,\n"
        )
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file(csv_body), headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["imported"] == 2
        assert data["skipped"] == 0
        assert data["errors"] == []

        checkins = client.get(f"/sessions/{session['id']}/checkins", headers=admin_headers).json()
        by_call = {c["callsign"]: c for c in checkins}
        assert by_call["W1AW"]["name"] == "Hiram Maxim"
        assert by_call["W1AW"]["signal_report"] == "59"
        assert by_call["W1AW"]["comments"] == "First check-in"
        assert by_call["KJ7ABC"]["name"] == "Jane Doe"

    def test_header_aliases_are_case_and_punctuation_insensitive(self, client, admin_headers, session):
        csv_body = "callsign,SIGNAL_REPORT,comment\nW1AW,59,hi\n"
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file(csv_body), headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["imported"] == 1
        checkins = client.get(f"/sessions/{session['id']}/checkins", headers=admin_headers).json()
        assert checkins[0]["signal_report"] == "59"
        assert checkins[0]["comments"] == "hi"

    def test_lowercase_callsign_normalized_uppercase(self, client, admin_headers, session):
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file("callsign\nw1aw\n"), headers=admin_headers,
        )
        assert resp.json()["imported"] == 1
        checkins = client.get(f"/sessions/{session['id']}/checkins", headers=admin_headers).json()
        assert checkins[0]["callsign"] == "W1AW"

    def test_missing_callsign_column_rejected(self, client, admin_headers, session):
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file("Name,Comments\nJane,hi\n"), headers=admin_headers,
        )
        assert resp.status_code == 400
        assert "callsign" in resp.json()["detail"].lower()

    def test_empty_csv_rejected(self, client, admin_headers, session):
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file(""), headers=admin_headers,
        )
        assert resp.status_code == 400

    def test_blank_rows_skipped_silently(self, client, admin_headers, session):
        csv_body = "Callsign,Name\nW1AW,Test\n,,\n\nKJ7ABC,Two\n"
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file(csv_body), headers=admin_headers,
        )
        data = resp.json()
        assert data["imported"] == 2
        assert data["skipped"] == 0

    def test_row_missing_callsign_value_recorded_as_error(self, client, admin_headers, session):
        csv_body = "Callsign,Name\nW1AW,Good\n,Missing callsign\n"
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file(csv_body), headers=admin_headers,
        )
        data = resp.json()
        assert data["imported"] == 1
        assert data["skipped"] == 1
        assert data["errors"][0]["reason"] == "Missing callsign"
        assert data["errors"][0]["row"] == 3  # header is row 1, first data row is 2

    def test_duplicate_callsign_within_csv_recorded_as_error(self, client, admin_headers, session):
        csv_body = "Callsign\nW1AW\nW1AW\n"
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file(csv_body), headers=admin_headers,
        )
        data = resp.json()
        assert data["imported"] == 1
        assert data["skipped"] == 1
        assert "already checked in" in data["errors"][0]["reason"].lower()
        assert data["errors"][0]["callsign"] == "W1AW"

    def test_duplicate_against_existing_checkin_recorded_as_error(self, client, admin_headers, session):
        client.post(f"/sessions/{session['id']}/checkins", json={"callsign": "W1AW", "has_traffic": False}, headers=admin_headers)
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file("Callsign\nW1AW\n"), headers=admin_headers,
        )
        data = resp.json()
        assert data["imported"] == 0
        assert data["skipped"] == 1

    def test_gmrs_net_allows_duplicate_callsign_import(self, client, admin_headers):
        net = client.post("/nets", json={"name": "GMRS Net", "net_type": "gmrs"}, headers=admin_headers).json()
        sess = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()
        resp = client.post(
            f"/sessions/{sess['id']}/checkins/import",
            files=_csv_file("Callsign\nKJ7FAM\nKJ7FAM\n"), headers=admin_headers,
        )
        data = resp.json()
        assert data["imported"] == 2
        assert data["skipped"] == 0

    def test_cannot_import_into_ended_session(self, client, admin_headers, session):
        client.patch(f"/sessions/{session['id']}/end", headers=admin_headers)
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file("Callsign\nW1AW\n"), headers=admin_headers,
        )
        data = resp.json()
        assert data["imported"] == 0
        assert data["skipped"] == 1
        assert "ended session" in data["errors"][0]["reason"].lower()

    def test_import_into_offline_session_stamps_reported_time(self, client, admin_headers, net):
        resp = client.post(f"/nets/{net['id']}/sessions", json={
            "is_offline": True, "occurred_at": "2026-08-01T19:00:00Z",
        }, headers=admin_headers)
        offline_session = resp.json()
        assert offline_session["is_offline"] is True

        import_resp = client.post(
            f"/sessions/{offline_session['id']}/checkins/import",
            files=_csv_file("Callsign\nW1AW\n"), headers=admin_headers,
        )
        assert import_resp.json()["imported"] == 1

        checkins = client.get(f"/sessions/{offline_session['id']}/checkins", headers=admin_headers).json()
        assert checkins[0]["checked_in_at"].startswith("2026-08-01T19:00:00")

    def test_cannot_import_into_locked_offline_session(self, client, admin_headers, net):
        resp = client.post(f"/nets/{net['id']}/sessions", json={
            "is_offline": True, "occurred_at": "2026-08-01T19:00:00Z",
        }, headers=admin_headers)
        offline_session = resp.json()
        client.patch(f"/sessions/{offline_session['id']}/end", headers=admin_headers)  # locks it

        import_resp = client.post(
            f"/sessions/{offline_session['id']}/checkins/import",
            files=_csv_file("Callsign\nW1AW\n"), headers=admin_headers,
        )
        data = import_resp.json()
        assert data["imported"] == 0
        assert data["skipped"] == 1
        assert "closed" in data["errors"][0]["reason"].lower()

    def test_has_traffic_parsed_from_common_truthy_values(self, client, admin_headers, session):
        csv_body = "Callsign,Has Traffic\nW1AW,yes\nKJ7ABC,no\nW9XYZ,\n"
        client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file(csv_body), headers=admin_headers,
        )
        checkins = client.get(f"/sessions/{session['id']}/checkins", headers=admin_headers).json()
        by_call = {c["callsign"]: c for c in checkins}
        assert by_call["W1AW"]["has_traffic"] is True
        assert by_call["KJ7ABC"]["has_traffic"] is False
        assert by_call["W9XYZ"]["has_traffic"] is False

    def test_evac_zone_column_creates_zone_record(self, client, admin_headers):
        net = client.post("/nets", json={"name": "ARES Net", "is_ares": True}, headers=admin_headers).json()
        sess = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers).json()
        client.post(
            f"/sessions/{sess['id']}/checkins/import",
            files=_csv_file("Callsign,Evac Zone\nW1AW,Zone 3\n"), headers=admin_headers,
        )
        zones = client.get(f"/nets/{net['id']}/evac-zones", headers=admin_headers).json()
        assert any(z["callsign"] == "W1AW" and z["zone"] == "Zone 3" for z in zones)

    def test_requires_session_access(self, client, admin_headers, user_token, session):
        from helpers import auth
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file("Callsign\nW1AW\n"), headers=auth(user_token),
        )
        assert resp.status_code in (403, 404)


class TestImportSample:
    def test_returns_csv_with_expected_columns(self, client, admin_headers):
        resp = client.get("/checkins/import-sample", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        body = resp.text
        header = body.splitlines()[0]
        assert "Callsign" in header
        assert "Signal Report" in header

    def test_requires_auth(self, client):
        resp = client.get("/checkins/import-sample")
        assert resp.status_code == 401

    def test_sample_columns_round_trip_through_import(self, client, admin_headers, session):
        sample = client.get("/checkins/import-sample", headers=admin_headers).text
        resp = client.post(
            f"/sessions/{session['id']}/checkins/import",
            files=_csv_file(sample), headers=admin_headers,
        )
        data = resp.json()
        assert data["imported"] == 2, data
        assert data["skipped"] == 0
