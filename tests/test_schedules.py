"""
Tests for scheduling and Net Control / Broadcaster sign-ups:
  POST   /nets/{id}/schedules
  POST   /nets/{id}/signups
  GET    /nets/{id}/signups
  GET    /nets/{id}/upcoming
  GET    /sessions/{id}          — scheduled duty display
  GET    /public/active          — scheduled duty display (public)
"""

from datetime import datetime, timedelta, timezone


def _schedule_for_today(client, headers, net_id):
    """Create a weekly schedule matching today's weekday, so `today` is a valid slot_date.
    Uses the UTC calendar date -- matching the app's own date.today() usage for
    schedule/session matching (see _next_occurrences in main.py) -- so this stays
    correct regardless of the test runner's local timezone."""
    today = datetime.now(timezone.utc).date()
    resp = client.post(f"/nets/{net_id}/schedules", json={
        "day_of_week": today.weekday(),
        "start_time": "19:30",
        "timezone": "UTC",
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json(), today


def _gmrs_net(client, headers, name="Family GMRS Net"):
    resp = client.post("/nets", json={
        "name": name, "net_type": "gmrs", "is_ares": False,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _broadcast_net(client, headers, label="Amateur Radio Newsline"):
    resp = client.post("/nets", json={
        "name": "Newsline Net", "is_ares": False,
        "has_broadcast": True, "broadcast_label": label,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestSignupRoles:
    def test_signup_defaults_to_net_control(self, client, admin_headers, net):
        sched, today = _schedule_for_today(client, admin_headers, net["id"])
        resp = client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "callsign": "W1AW",
        }, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        assert resp.json()["role"] == "net_control"

    def test_broadcaster_rejected_when_net_has_no_broadcast(self, client, admin_headers, net):
        sched, today = _schedule_for_today(client, admin_headers, net["id"])
        resp = client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "broadcaster", "callsign": "W1AW",
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_net_control_and_broadcaster_fill_independently(self, client, admin_headers):
        bnet = _broadcast_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, bnet["id"])
        r1 = client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "net_control", "callsign": "W1AW",
        }, headers=admin_headers)
        assert r1.status_code == 201, r1.text
        r2 = client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "broadcaster", "callsign": "K2ABC",
        }, headers=admin_headers)
        assert r2.status_code == 201, r2.text
        assert r2.json()["role"] == "broadcaster"

    def test_duplicate_role_on_same_date_conflicts(self, client, admin_headers, net):
        sched, today = _schedule_for_today(client, admin_headers, net["id"])
        client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "callsign": "W1AW",
        }, headers=admin_headers)
        resp = client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "callsign": "K2ABC",
        }, headers=admin_headers)
        assert resp.status_code == 409

    def test_both_role_blocks_subsequent_single_role_signup(self, client, admin_headers):
        bnet = _broadcast_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, bnet["id"])
        r1 = client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "both", "callsign": "W1AW",
        }, headers=admin_headers)
        assert r1.status_code == 201, r1.text
        r2 = client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "broadcaster", "callsign": "K2ABC",
        }, headers=admin_headers)
        assert r2.status_code == 409

    def test_both_role_rejected_when_a_role_already_taken(self, client, admin_headers):
        bnet = _broadcast_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, bnet["id"])
        client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "net_control", "callsign": "W1AW",
        }, headers=admin_headers)
        resp = client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "both", "callsign": "K2ABC",
        }, headers=admin_headers)
        assert resp.status_code == 409

    def test_upcoming_lists_both_role_signups(self, client, admin_headers):
        bnet = _broadcast_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, bnet["id"])
        client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "net_control", "callsign": "W1AW",
        }, headers=admin_headers)
        client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "broadcaster", "callsign": "K2ABC",
        }, headers=admin_headers)
        resp = client.get(f"/nets/{bnet['id']}/upcoming?weeks=1", headers=admin_headers)
        assert resp.status_code == 200
        slot = next(s for s in resp.json() if s["slot_date"] == str(today))
        roles = {sig["role"] for sig in slot["signups"]}
        assert roles == {"net_control", "broadcaster"}


class TestSignupPhone:
    """SignupOut.phone (issue follow-up) -- live-looked-up from the signed-up
    user's own account, not a snapshot like callsign/name/email, so whoever's
    coordinating an activation can call their *current* number."""

    def test_phone_appears_in_upcoming_and_signups(self, client, admin_headers, net):
        client.patch("/auth/profile", json={
            "name": "Admin User", "email": "admin@example.com", "callsign": "W1ADMIN", "phone": "555-1234",
        }, headers=admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, net["id"])
        client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "callsign": "W1AW",
        }, headers=admin_headers)

        upcoming = client.get(f"/nets/{net['id']}/upcoming?weeks=1", headers=admin_headers).json()
        slot = next(s for s in upcoming if s["slot_date"] == str(today))
        assert slot["signups"][0]["phone"] == "555-1234"

        signups = client.get(f"/nets/{net['id']}/signups", headers=admin_headers).json()
        assert signups[0]["phone"] == "555-1234"

    def test_phone_absent_when_signed_up_user_has_none_set(self, client, admin_headers, net):
        sched, today = _schedule_for_today(client, admin_headers, net["id"])
        client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "callsign": "W1AW",
        }, headers=admin_headers)

        signups = client.get(f"/nets/{net['id']}/signups", headers=admin_headers).json()
        assert signups[0]["phone"] is None


class TestDutyDisplay:
    def test_session_shows_scheduled_net_control(self, client, admin_headers, net):
        sched, today = _schedule_for_today(client, admin_headers, net["id"])
        client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "callsign": "W1AW", "name": "Alice",
        }, headers=admin_headers)
        s = client.post(f"/nets/{net['id']}/sessions", json={"name": "Test"}, headers=admin_headers).json()
        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ncs_callsign"] == "W1AW"
        assert resp.json()["ncs_name"] == "Alice"

    def test_session_falls_back_to_operator_when_no_signup(self, client, admin_headers, session):
        resp = client.get(f"/sessions/{session['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ncs_callsign"] == "W1ADMIN"

    def test_ncs_user_id_resolves_for_photo_lookup(self, client, admin_headers, session):
        """ncs_user_id/broadcaster_user_id (issue follow-up) -- the frontend
        builds /users/{id}/photo from these; no separate has_photo flag."""
        me = client.get("/auth/me", headers=admin_headers).json()
        resp = client.get(f"/sessions/{session['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ncs_user_id"] == me["id"]

    def test_ncs_user_id_resolves_from_schedule_signup(self, client, admin_headers, net):
        sched, today = _schedule_for_today(client, admin_headers, net["id"])
        client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "callsign": "W1AW",
        }, headers=admin_headers)
        me = client.get("/auth/me", headers=admin_headers).json()
        s = client.post(f"/nets/{net['id']}/sessions", json={"name": "Test"}, headers=admin_headers).json()
        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ncs_user_id"] == me["id"]

    def test_broadcaster_user_id_resolves_from_signup(self, client, admin_headers):
        bnet = _broadcast_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, bnet["id"])
        client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "broadcaster", "callsign": "K2ABC",
        }, headers=admin_headers)
        me = client.get("/auth/me", headers=admin_headers).json()
        s = client.post(f"/nets/{bnet['id']}/sessions", json={}, headers=admin_headers).json()
        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["broadcaster_user_id"] == me["id"]

    def test_manual_override_leaves_user_id_none(self, client, admin_headers):
        """A manual text override (issue #17/#20) has no account behind it --
        no photo to show, so *_user_id must stay None even though a callsign
        is displayed."""
        bnet = _broadcast_net(client, admin_headers)
        s = client.post(f"/nets/{bnet['id']}/sessions", json={
            "broadcaster_override_callsign": "K3XYZ", "broadcaster_override_name": "Carol",
        }, headers=admin_headers).json()
        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["broadcaster_callsign"] == "K3XYZ"
        assert data["broadcaster_user_id"] is None

    def test_gmrs_net_fallback_uses_gmrs_callsign_when_set(self, client, admin_headers):
        """issue #23: on a GMRS net, the "whoever started the session" fallback
        should prefer the operator's separate GMRS callsign over their amateur one."""
        client.patch("/auth/gmrs-callsign", json={"gmrs_callsign": "WQXH7777"}, headers=admin_headers)
        gnet = _gmrs_net(client, admin_headers)
        s = client.post(f"/nets/{gnet['id']}/sessions", json={}, headers=admin_headers).json()
        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ncs_callsign"] == "WQXH7777"

    def test_gmrs_net_fallback_uses_amateur_callsign_when_no_gmrs_set(self, client, admin_headers):
        gnet = _gmrs_net(client, admin_headers)
        s = client.post(f"/nets/{gnet['id']}/sessions", json={}, headers=admin_headers).json()
        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ncs_callsign"] == "W1ADMIN"

    def test_ham_net_fallback_ignores_gmrs_callsign(self, client, admin_headers, session):
        """A GMRS callsign set on the profile must not leak into a ham net's duty display."""
        client.patch("/auth/gmrs-callsign", json={"gmrs_callsign": "WQXH7777"}, headers=admin_headers)
        resp = client.get(f"/sessions/{session['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ncs_callsign"] == "W1ADMIN"

    def test_gmrs_net_assign_uses_assigned_users_gmrs_callsign(self, client, admin_headers, user_headers):
        """The net-owner "assign another operator" sign-up path should also prefer
        that operator's GMRS callsign on a GMRS net."""
        client.patch("/auth/gmrs-callsign", json={"gmrs_callsign": "WQXH9999"}, headers=user_headers)
        assigned_user_id = client.get("/auth/me", headers=user_headers).json()["id"]
        gnet = _gmrs_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, gnet["id"])
        resp = client.post(f"/nets/{gnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "assigned_user_id": assigned_user_id,
        }, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        assert resp.json()["callsign"] == "WQXH9999"

    def test_ham_net_assign_ignores_gmrs_callsign(self, client, admin_headers, user_headers, net):
        client.patch("/auth/gmrs-callsign", json={"gmrs_callsign": "WQXH9999"}, headers=user_headers)
        assigned_user_id = client.get("/auth/me", headers=user_headers).json()["id"]
        sched, today = _schedule_for_today(client, admin_headers, net["id"])
        resp = client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "assigned_user_id": assigned_user_id,
        }, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        assert resp.json()["callsign"] == "W2USER"

    def test_gmrs_net_scheduled_signup_takes_precedence_over_gmrs_callsign(self, client, admin_headers):
        client.patch("/auth/gmrs-callsign", json={"gmrs_callsign": "WQXH7777"}, headers=admin_headers)
        gnet = _gmrs_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, gnet["id"])
        client.post(f"/nets/{gnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "callsign": "WQXH1234", "name": "Alice",
        }, headers=admin_headers)
        s = client.post(f"/nets/{gnet['id']}/sessions", json={}, headers=admin_headers).json()
        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ncs_callsign"] == "WQXH1234"

    def test_public_active_shows_broadcaster(self, client, admin_headers):
        bnet = _broadcast_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, bnet["id"])
        client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "broadcaster",
            "callsign": "K2ABC", "name": "Bob",
        }, headers=admin_headers)
        client.post(f"/nets/{bnet['id']}/sessions", json={"name": "Live"}, headers=admin_headers)
        resp = client.get("/public/active")
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["net_name"] == "Newsline Net")
        assert row["broadcaster_callsign"] == "K2ABC"
        assert row["broadcaster_name"] == "Bob"
        assert row["broadcast_label"] == "Amateur Radio Newsline"

    def test_broadcaster_override_takes_precedence_over_signup(self, client, admin_headers):
        """issue #17: a broadcaster not known until the net is about to begin can be
        set at session start, overriding the schedule sign-up for that date."""
        bnet = _broadcast_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, bnet["id"])
        client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "broadcaster",
            "callsign": "K2ABC", "name": "Bob",
        }, headers=admin_headers)
        s = client.post(f"/nets/{bnet['id']}/sessions", json={
            "broadcaster_override_callsign": "k3xyz", "broadcaster_override_name": "Carol",
        }, headers=admin_headers).json()

        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["broadcaster_callsign"] == "K3XYZ"  # uppercased
        assert data["broadcaster_name"] == "Carol"
        assert data["broadcast_label"] == "Amateur Radio Newsline"

    def test_broadcaster_override_works_with_no_signup_at_all(self, client, admin_headers):
        bnet = _broadcast_net(client, admin_headers)
        s = client.post(f"/nets/{bnet['id']}/sessions", json={
            "broadcaster_override_callsign": "K3XYZ", "broadcaster_override_name": "Carol",
        }, headers=admin_headers).json()

        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["broadcaster_callsign"] == "K3XYZ"
        assert data["broadcast_label"] == "Amateur Radio Newsline"

    def test_no_override_falls_back_to_signup_as_before(self, client, admin_headers):
        bnet = _broadcast_net(client, admin_headers)
        sched, today = _schedule_for_today(client, admin_headers, bnet["id"])
        client.post(f"/nets/{bnet['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(today), "role": "broadcaster",
            "callsign": "K2ABC", "name": "Bob",
        }, headers=admin_headers)
        s = client.post(f"/nets/{bnet['id']}/sessions", json={}, headers=admin_headers).json()

        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["broadcaster_callsign"] == "K2ABC"
        assert data["broadcaster_name"] == "Bob"

    def test_no_override_and_no_signup_leaves_broadcaster_blank(self, client, admin_headers):
        bnet = _broadcast_net(client, admin_headers)
        s = client.post(f"/nets/{bnet['id']}/sessions", json={}, headers=admin_headers).json()

        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["broadcaster_callsign"] is None
        assert data["broadcast_label"] is None

    def test_session_shows_next_week_signup(self, client, admin_headers, net):
        sched, today = _schedule_for_today(client, admin_headers, net["id"])
        next_week = today + timedelta(days=7)
        client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(next_week), "callsign": "K2NEXT", "name": "Next Op",
        }, headers=admin_headers)
        s = client.post(f"/nets/{net['id']}/sessions", json={"name": "Test"}, headers=admin_headers).json()
        resp = client.get(f"/sessions/{s['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["next_ncs_callsign"] == "K2NEXT"
        assert resp.json()["next_ncs_name"] == "Next Op"

    def test_session_next_week_has_no_operator_fallback(self, client, admin_headers, session):
        """Unlike this week, next week has no signup yet -- it should stay empty rather
        than fall back to anyone, since no one has started that session yet."""
        resp = client.get(f"/sessions/{session['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["next_ncs_callsign"] is None
