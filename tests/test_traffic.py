"""
Tests for traffic message logging + its ICS-213/Winlink export (issue
follow-up):
  POST/GET/PATCH/DELETE /sessions/{id}/traffic-messages, /traffic-messages/{id}
  GET /traffic-messages/{id}/ics213
"""
from helpers import auth


def _create_message(client, headers, session_id, **overrides):
    body = {
        "origin_callsign": "W1AW",
        "dest_info": "K2ABC",
        "msg_number": "NTS-001",
        "subject": "Shelter status update",
        "msg_type": "formal",
        "notes": "Shelter at capacity, requesting additional cots.",
    }
    body.update(overrides)
    resp = client.post(f"/sessions/{session_id}/traffic-messages", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestSubjectField:
    def test_subject_round_trips_on_create(self, client, admin_headers, session):
        msg = _create_message(client, admin_headers, session["id"])
        assert msg["subject"] == "Shelter status update"

    def test_subject_appears_in_list(self, client, admin_headers, session):
        _create_message(client, admin_headers, session["id"])
        resp = client.get(f"/sessions/{session['id']}/traffic-messages", headers=admin_headers)
        assert resp.json()[0]["subject"] == "Shelter status update"

    def test_subject_updatable_via_patch(self, client, admin_headers, session):
        msg = _create_message(client, admin_headers, session["id"])
        resp = client.patch(f"/traffic-messages/{msg['id']}", json={"subject": "Updated subject"}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["subject"] == "Updated subject"

    def test_subject_optional(self, client, admin_headers, session):
        msg = _create_message(client, admin_headers, session["id"], subject=None)
        assert msg["subject"] is None


class TestIcs213Export:
    def test_export_returns_plain_text_attachment(self, client, admin_headers, session):
        msg = _create_message(client, admin_headers, session["id"])
        resp = client.get(f"/traffic-messages/{msg['id']}/ics213", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/plain")
        assert "ICS213_NTS-001" in resp.headers["content-disposition"]

    def test_export_contains_ics213_fields(self, client, admin_headers, session, net):
        msg = _create_message(client, admin_headers, session["id"])
        resp = client.get(f"/traffic-messages/{msg['id']}/ics213", headers=admin_headers)
        text = resp.text
        assert "ICS-213" in text
        assert f"Incident Name : {net['name']}" in text
        assert "To            : K2ABC" in text
        assert "From          : W1AW" in text
        assert "Subject       : Shelter status update" in text
        assert "Shelter at capacity, requesting additional cots." in text

    def test_export_falls_back_to_message_id_when_no_msg_number(self, client, admin_headers, session):
        msg = _create_message(client, admin_headers, session["id"], msg_number=None)
        resp = client.get(f"/traffic-messages/{msg['id']}/ics213", headers=admin_headers)
        assert resp.status_code == 200
        assert f"ICS213_msg{msg['id']}" in resp.headers["content-disposition"]

    def test_export_handles_missing_message_text(self, client, admin_headers, session):
        msg = _create_message(client, admin_headers, session["id"], notes=None)
        resp = client.get(f"/traffic-messages/{msg['id']}/ics213", headers=admin_headers)
        assert resp.status_code == 200
        assert "(no message text logged)" in resp.text

    def test_export_includes_approved_by_when_net_control_known(self, client, admin_headers, session):
        # admin_headers's user started the session, so they're the fallback
        # Net Control (routers/schedules.py's _duty_labels_for_session).
        msg = _create_message(client, admin_headers, session["id"])
        resp = client.get(f"/traffic-messages/{msg['id']}/ics213", headers=admin_headers)
        assert "Approved by" in resp.text
        assert "W1ADMIN" in resp.text

    def test_non_member_cannot_export(self, client, admin_headers, user_headers, session):
        msg = _create_message(client, admin_headers, session["id"])
        # user_headers has no share on this net at all.
        resp = client.get(f"/traffic-messages/{msg['id']}/ics213", headers=user_headers)
        assert resp.status_code in (403, 404)

    def test_export_404_for_unknown_message(self, client, admin_headers):
        resp = client.get("/traffic-messages/999999/ics213", headers=admin_headers)
        assert resp.status_code == 404
