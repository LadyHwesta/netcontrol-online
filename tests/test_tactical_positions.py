"""
Tests for ARES/ACES tactical positions and shift sign-on/off (issue #21).

Gated on the SESSION's is_activation flag, not just net.is_ares -- a
routine session on an ARES net must reject these exactly like a
non-ARES net would, and its own behavior (evac zone, expected stations,
checkins) must stay byte-for-byte unaffected.
"""


def _ares_net(client, headers, name="ARES Test Net"):
    resp = client.post("/nets", json={"name": name, "is_ares": True}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _activation_session(client, headers, net_id):
    resp = client.post(f"/nets/{net_id}/sessions", json={"is_activation": True}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _routine_session(client, headers, net_id):
    resp = client.post(f"/nets/{net_id}/sessions", json={}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _position(client, headers, tactical_callsign="SHELTER 1", **extra):
    anet = _ares_net(client, headers)
    activation = _activation_session(client, headers, anet["id"])
    resp = client.post(
        f"/sessions/{activation['id']}/tactical-positions",
        json={"tactical_callsign": tactical_callsign, **extra},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return anet, activation, resp.json()


class TestActivationFlag:
    def test_session_create_defaults_to_not_activation(self, client, admin_headers, net):
        resp = client.post(f"/nets/{net['id']}/sessions", json={}, headers=admin_headers)
        assert resp.json()["is_activation"] is False

    def test_activation_forced_false_on_non_ares_net(self, client, admin_headers, net):
        resp = client.post(f"/nets/{net['id']}/sessions", json={"is_activation": True}, headers=admin_headers)
        assert resp.json()["is_activation"] is False

    def test_activation_can_be_set_on_ares_net(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        resp = client.post(f"/nets/{anet['id']}/sessions", json={"is_activation": True}, headers=admin_headers)
        assert resp.json()["is_activation"] is True


class TestTacticalPositionCreation:
    def test_create_position_requires_activation_session(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        routine = _routine_session(client, admin_headers, anet["id"])
        resp = client.post(
            f"/sessions/{routine['id']}/tactical-positions",
            json={"tactical_callsign": "SHELTER 1"},
            headers=admin_headers,
        )
        assert resp.status_code == 400

    def test_create_position_requires_ares_net(self, client, admin_headers, net):
        activation = client.post(
            f"/nets/{net['id']}/sessions", json={"is_activation": True}, headers=admin_headers
        ).json()
        resp = client.post(
            f"/sessions/{activation['id']}/tactical-positions",
            json={"tactical_callsign": "SHELTER 1"},
            headers=admin_headers,
        )
        assert resp.status_code == 400

    def test_create_position_fields(self, client, admin_headers):
        _anet, _activation, position = _position(
            client, admin_headers,
            tactical_callsign="shelter 1", location="123 Main St",
            assigned_callsign="w1abc", assigned_name="Alice",
        )
        assert position["tactical_callsign"] == "SHELTER 1"
        assert position["location"] == "123 Main St"
        assert position["assigned_callsign"] == "W1ABC"
        assert position["assigned_name"] == "Alice"
        assert position["current_callsign"] is None  # vacant until someone signs on

    def test_tactical_callsign_required(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        resp = client.post(
            f"/sessions/{activation['id']}/tactical-positions",
            json={"tactical_callsign": "   "},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_update_position_fields(self, client, admin_headers):
        _anet, _activation, position = _position(client, admin_headers)
        resp = client.patch(
            f"/tactical-positions/{position['id']}",
            json={
                "location": "456 Oak St", "assigned_callsign": "w4new", "assigned_name": "Bob",
                "scheduled_start": "2026-09-01T14:00:00Z",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        updated = resp.json()
        assert updated["location"] == "456 Oak St"
        assert updated["assigned_callsign"] == "W4NEW"
        assert updated["assigned_name"] == "Bob"
        assert updated["scheduled_start"].startswith("2026-09-01T14:00:00")
        assert updated["tactical_callsign"] == "SHELTER 1"  # identity unchanged

    def test_update_position_clears_fields_when_blank(self, client, admin_headers):
        _anet, _activation, position = _position(
            client, admin_headers, location="123 Main St", assigned_callsign="W1ABC",
        )
        resp = client.patch(f"/tactical-positions/{position['id']}", json={}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        updated = resp.json()
        assert updated["location"] is None
        assert updated["assigned_callsign"] is None
        assert updated["scheduled_start"] is None

    def test_update_net_control_position_plan(self, client, admin_headers):
        # Net Control has no creation form of its own -- editing is the only way to
        # plan ahead for it, same as any tactical station (issue #21 follow-up).
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        nc = positions[0]
        resp = client.patch(
            f"/tactical-positions/{nc['id']}",
            json={"assigned_callsign": "w5next", "assigned_name": "Next NCS", "scheduled_start": "2026-09-01T18:00:00Z"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        updated = resp.json()
        assert updated["is_net_control"] is True
        assert updated["tactical_callsign"] == "NET CONTROL"
        assert updated["assigned_callsign"] == "W5NEXT"
        assert updated["scheduled_start"].startswith("2026-09-01T18:00:00")

    def test_non_member_cannot_update_position(self, client, admin_headers, user_headers):
        _anet, _activation, position = _position(client, admin_headers)
        resp = client.patch(
            f"/tactical-positions/{position['id']}", json={"location": "X"}, headers=user_headers,
        )
        assert resp.status_code == 403

    def test_list_positions_ordered_by_creation(self, client, admin_headers):
        # NET CONTROL is auto-created at session start and always sorts first
        # (issue #21 follow-up) -- user-created positions follow in creation order.
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        client.post(f"/sessions/{activation['id']}/tactical-positions", json={"tactical_callsign": "COMMAND"}, headers=admin_headers)
        client.post(f"/sessions/{activation['id']}/tactical-positions", json={"tactical_callsign": "SHELTER 1"}, headers=admin_headers)
        resp = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers)
        assert resp.status_code == 200
        assert [p["tactical_callsign"] for p in resp.json()] == ["NET CONTROL", "COMMAND", "SHELTER 1"]


class TestSignOnOff:
    def test_sign_on_creates_checkin_and_sets_current_occupant(self, client, admin_headers):
        _anet, activation, position = _position(client, admin_headers)
        resp = client.post(
            f"/tactical-positions/{position['id']}/sign-on",
            json={"callsign": "w1abc", "name": "Alice"},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        checkin = resp.json()
        assert checkin["callsign"] == "W1ABC"
        assert checkin["tactical_position_id"] == position["id"]
        assert checkin["tactical_callsign"] == "SHELTER 1"
        assert checkin["signed_off_at"] is None

        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        shelter1 = next(p for p in positions if p["id"] == position["id"])
        assert shelter1["current_callsign"] == "W1ABC"
        assert shelter1["current_checkin_id"] == checkin["id"]

    def test_signing_on_again_signs_off_previous_occupant(self, client, admin_headers):
        _anet, activation, position = _position(client, admin_headers)
        first = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers).json()
        second = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W2DEF"}, headers=admin_headers).json()

        checkins = client.get(f"/sessions/{activation['id']}/checkins", headers=admin_headers).json()
        first_after = next(c for c in checkins if c["id"] == first["id"])
        assert first_after["signed_off_at"] is not None
        second_after = next(c for c in checkins if c["id"] == second["id"])
        assert second_after["signed_off_at"] is None

        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        shelter1 = next(p for p in positions if p["id"] == position["id"])
        assert shelter1["current_callsign"] == "W2DEF"

    def test_sign_off_vacates_with_no_new_checkin(self, client, admin_headers):
        _anet, activation, position = _position(client, admin_headers)
        client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers)
        before_count = len(client.get(f"/sessions/{activation['id']}/checkins", headers=admin_headers).json())

        resp = client.post(f"/tactical-positions/{position['id']}/sign-off", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["signed_off_at"] is not None

        after_count = len(client.get(f"/sessions/{activation['id']}/checkins", headers=admin_headers).json())
        assert after_count == before_count  # no new checkin created on sign-off

        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        shelter1 = next(p for p in positions if p["id"] == position["id"])
        assert shelter1["current_callsign"] is None

    def test_sign_off_when_already_vacant_404s(self, client, admin_headers):
        _anet, _activation, position = _position(client, admin_headers)
        resp = client.post(f"/tactical-positions/{position['id']}/sign-off", headers=admin_headers)
        assert resp.status_code == 404

    def test_same_callsign_can_hold_two_positions(self, client, admin_headers):
        anet, activation, position1 = _position(client, admin_headers)
        position2 = client.post(
            f"/sessions/{activation['id']}/tactical-positions", json={"tactical_callsign": "COMMAND"}, headers=admin_headers
        ).json()
        r1 = client.post(f"/tactical-positions/{position1['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers)
        r2 = client.post(f"/tactical-positions/{position2['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers)
        assert r1.status_code == 201
        assert r2.status_code == 201  # duplicate-callsign block on ham nets is bypassed here

    def test_same_callsign_can_resign_onto_same_position_later(self, client, admin_headers):
        _anet, _activation, position = _position(client, admin_headers)
        client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers)
        client.post(f"/tactical-positions/{position['id']}/sign-off", headers=admin_headers)
        resp = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers)
        assert resp.status_code == 201

    def test_sign_on_rejected_on_ended_session(self, client, admin_headers):
        _anet, activation, position = _position(client, admin_headers)
        client.patch(f"/sessions/{activation['id']}/end", headers=admin_headers)
        resp = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers)
        assert resp.status_code == 400

    async def test_two_simultaneously_open_checkins_does_not_500(self, client, admin_headers, db):
        """Normal sign-on flow always closes the prior occupant first, so
        this shouldn't happen -- but two operators racing to sign on to the
        same vacant position concurrently could still produce it momentarily.
        _current_occupant() used to be scalar_one_or_none(), which would
        raise MultipleResultsFound (-> 500) here; seed the race directly to
        prove the fix (take the most recent via .order_by + .limit(1))
        holds regardless of how two open rows happened."""
        import models
        _anet, activation, position = _position(client, admin_headers)
        db.add(models.Checkin(session_id=activation["id"], callsign="W1ABC", tactical_position_id=position["id"]))
        db.add(models.Checkin(session_id=activation["id"], callsign="W2DEF", tactical_position_id=position["id"]))
        await db.commit()

        # Sign-on internally calls _current_occupant() to close out whoever's
        # currently open before creating the new checkin.
        resp = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W3GHI"}, headers=admin_headers)
        assert resp.status_code == 201, resp.text


class TestShiftHistory:
    def test_shift_history_ordering_and_denormalized_callsign(self, client, admin_headers):
        _anet, _activation, position = _position(client, admin_headers)
        client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers)
        client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W2DEF"}, headers=admin_headers)
        client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W3GHI"}, headers=admin_headers)

        resp = client.get(f"/tactical-positions/{position['id']}/shifts", headers=admin_headers)
        assert resp.status_code == 200
        shifts = resp.json()
        assert [s["callsign"] for s in shifts] == ["W1ABC", "W2DEF", "W3GHI"]
        assert shifts[0]["signed_off_at"] is not None
        assert shifts[1]["signed_off_at"] is not None
        assert shifts[2]["signed_off_at"] is None
        assert all(s["tactical_callsign"] == "SHELTER 1" for s in shifts)


class TestDeletePosition:
    def test_delete_position_keeps_checkin_history(self, client, admin_headers):
        _anet, activation, position = _position(client, admin_headers)
        checkin = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers).json()

        resp = client.delete(f"/tactical-positions/{position['id']}", headers=admin_headers)
        assert resp.status_code == 204

        # The checkin itself is not cascade-deleted with the position (its
        # tactical_position_id is nulled via ON DELETE SET NULL in Postgres,
        # which the SQLite test DB doesn't enforce -- untested here for the
        # same reason no other ondelete="SET NULL" column is in this suite).
        checkins = client.get(f"/sessions/{activation['id']}/checkins", headers=admin_headers).json()
        kept = next(c for c in checkins if c["id"] == checkin["id"])
        assert kept["callsign"] == "W1ABC"

        # NET CONTROL (auto-created) remains -- only the user-created position was removed.
        resp = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers)
        remaining = resp.json()
        assert len(remaining) == 1
        assert remaining[0]["is_net_control"] is True


class TestListCheckinsIncludesTactical:
    def test_list_checkins_includes_tactical_callsign(self, client, admin_headers):
        _anet, activation, position = _position(client, admin_headers)
        client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ABC"}, headers=admin_headers)

        resp = client.get(f"/sessions/{activation['id']}/checkins", headers=admin_headers)
        checkin = resp.json()[0]
        assert checkin["tactical_callsign"] == "SHELTER 1"

    def test_plain_checkin_has_no_tactical_callsign(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        client.post(f"/sessions/{activation['id']}/checkins", json={"callsign": "W1XYZ"}, headers=admin_headers)
        resp = client.get(f"/sessions/{activation['id']}/checkins", headers=admin_headers)
        assert resp.json()[0]["tactical_callsign"] is None


class TestRoutineAresSessionUnaffected:
    """A routine (non-activation) session on an ARES net must behave exactly
    as it did before this feature -- issue #21's core constraint."""

    def test_expected_endpoint_unaffected(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        routine = _routine_session(client, admin_headers, anet["id"])
        client.post(
            f"/sessions/{routine['id']}/checkins",
            json={"callsign": "W1ABC", "evac_zone": "Zone A"},
            headers=admin_headers,
        )
        resp = client.get(f"/nets/{anet['id']}/expected?min_checkins=1", headers=admin_headers)
        assert resp.status_code == 200

    def test_checkin_has_no_tactical_fields_set(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        routine = _routine_session(client, admin_headers, anet["id"])
        client.post(
            f"/sessions/{routine['id']}/checkins",
            json={"callsign": "W1ABC", "evac_zone": "Zone A"},
            headers=admin_headers,
        )
        resp = client.get(f"/sessions/{routine['id']}/checkins", headers=admin_headers)
        checkin = resp.json()[0]
        assert checkin["tactical_position_id"] is None
        assert checkin["tactical_callsign"] is None
        assert checkin["evac_zone"] == "Zone A"


class TestPermissions:
    def test_non_member_cannot_create_position(self, client, admin_headers, user_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        resp = client.post(
            f"/sessions/{activation['id']}/tactical-positions",
            json={"tactical_callsign": "SHELTER 1"},
            headers=user_headers,
        )
        assert resp.status_code == 403

    def test_non_member_cannot_sign_on(self, client, admin_headers, user_headers):
        _anet, _activation, position = _position(client, admin_headers)
        resp = client.post(
            f"/tactical-positions/{position['id']}/sign-on",
            json={"callsign": "W1ABC"},
            headers=user_headers,
        )
        assert resp.status_code == 403

    def test_non_member_cannot_list_positions(self, client, admin_headers, user_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        resp = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=user_headers)
        assert resp.status_code == 403


class TestScheduledStart:
    def test_scheduled_start_stored_and_returned(self, client, admin_headers):
        _anet, _activation, position = _position(
            client, admin_headers, scheduled_start="2026-09-01T14:00:00Z",
        )
        assert position["scheduled_start"] is not None
        assert position["scheduled_start"].startswith("2026-09-01T14:00:00")

    def test_scheduled_start_optional(self, client, admin_headers):
        _anet, _activation, position = _position(client, admin_headers)
        assert position["scheduled_start"] is None


class TestNetControlPosition:
    """Net Control is auto-created as a tactical position at activation session
    start and hands off through the same sign-on/off flow as any other position
    (issue #21 follow-up: a single day-level NCS wasn't enough once Net Control
    itself rotates mid-activation)."""

    def test_auto_created_on_activation_session_start(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        assert len(positions) == 1
        nc = positions[0]
        assert nc["is_net_control"] is True
        assert nc["tactical_callsign"] == "NET CONTROL"

    def test_auto_signed_on_from_session_starter_when_no_schedule_signup(self, client, admin_headers):
        # No schedule sign-up exists, so _duty_labels_for_session falls back to
        # whoever started the session (W1ADMIN) -- Net Control should already be
        # live the moment the activation begins, not sitting vacant.
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        nc = positions[0]
        assert nc["current_callsign"] == "W1ADMIN"
        assert nc["current_checkin_id"] is not None

    def test_not_created_for_routine_session(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        routine = _routine_session(client, admin_headers, anet["id"])
        # No tactical-positions access at all on a routine session (400), so there's
        # no way a NET CONTROL position could have been created for it either.
        resp = client.get(f"/sessions/{routine['id']}/tactical-positions", headers=admin_headers)
        assert resp.status_code == 400

    def test_cannot_be_deleted(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        nc = positions[0]
        resp = client.delete(f"/tactical-positions/{nc['id']}", headers=admin_headers)
        assert resp.status_code == 400

    def test_sorted_first_regardless_of_creation_order(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        client.post(f"/sessions/{activation['id']}/tactical-positions", json={"tactical_callsign": "SHELTER 1"}, headers=admin_headers)
        client.post(f"/sessions/{activation['id']}/tactical-positions", json={"tactical_callsign": "COMMAND"}, headers=admin_headers)
        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        assert positions[0]["is_net_control"] is True
        assert [p["tactical_callsign"] for p in positions[1:]] == ["SHELTER 1", "COMMAND"]

    def test_hands_off_via_sign_on_like_any_other_position(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        nc = positions[0]
        assert nc["current_callsign"] == "W1ADMIN"

        resp = client.post(f"/tactical-positions/{nc['id']}/sign-on", json={"callsign": "W2NEXT"}, headers=admin_headers)
        assert resp.status_code == 201

        positions = client.get(f"/sessions/{activation['id']}/tactical-positions", headers=admin_headers).json()
        nc = positions[0]
        assert nc["current_callsign"] == "W2NEXT"

        shifts = client.get(f"/tactical-positions/{nc['id']}/shifts", headers=admin_headers).json()
        assert [s["callsign"] for s in shifts] == ["W1ADMIN", "W2NEXT"]
        assert shifts[0]["signed_off_at"] is not None
        assert shifts[1]["signed_off_at"] is None

    def test_list_checkins_includes_net_control_signon(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        checkins = client.get(f"/sessions/{activation['id']}/checkins", headers=admin_headers).json()
        assert len(checkins) == 1
        assert checkins[0]["callsign"] == "W1ADMIN"
        assert checkins[0]["tactical_callsign"] == "NET CONTROL"


class TestNetControlShifts:
    """A forward-looking rotation queue for Net Control specifically, separate
    from a tactical station's single planned-operator field (issue #21 follow-up:
    Net Control classically hands off on its own cadence, so it gets its own
    schedule the frontend auto-fills the next handoff from)."""

    def test_create_shift(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        resp = client.post(
            f"/sessions/{activation['id']}/net-control-shifts",
            json={"callsign": "w2next", "name": "Next NCS", "scheduled_start": "2026-09-01T14:00:00Z"},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        shift = resp.json()
        assert shift["callsign"] == "W2NEXT"
        assert shift["name"] == "Next NCS"
        assert shift["scheduled_start"].startswith("2026-09-01T14:00:00")

    def test_scheduled_start_required(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        resp = client.post(
            f"/sessions/{activation['id']}/net-control-shifts",
            json={"callsign": "W2NEXT"},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_create_shift_requires_activation_session(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        routine = _routine_session(client, admin_headers, anet["id"])
        resp = client.post(
            f"/sessions/{routine['id']}/net-control-shifts",
            json={"callsign": "W2NEXT", "scheduled_start": "2026-09-01T14:00:00Z"},
            headers=admin_headers,
        )
        assert resp.status_code == 400

    def test_list_shifts_ordered_by_scheduled_start(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        client.post(
            f"/sessions/{activation['id']}/net-control-shifts",
            json={"callsign": "W3THIRD", "scheduled_start": "2026-09-01T18:00:00Z"},
            headers=admin_headers,
        )
        client.post(
            f"/sessions/{activation['id']}/net-control-shifts",
            json={"callsign": "W1FIRST", "scheduled_start": "2026-09-01T10:00:00Z"},
            headers=admin_headers,
        )
        client.post(
            f"/sessions/{activation['id']}/net-control-shifts",
            json={"callsign": "W2SECOND", "scheduled_start": "2026-09-01T14:00:00Z"},
            headers=admin_headers,
        )
        resp = client.get(f"/sessions/{activation['id']}/net-control-shifts", headers=admin_headers)
        assert resp.status_code == 200
        assert [s["callsign"] for s in resp.json()] == ["W1FIRST", "W2SECOND", "W3THIRD"]

    def test_delete_shift(self, client, admin_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        shift = client.post(
            f"/sessions/{activation['id']}/net-control-shifts",
            json={"callsign": "W2NEXT", "scheduled_start": "2026-09-01T14:00:00Z"},
            headers=admin_headers,
        ).json()
        resp = client.delete(f"/net-control-shifts/{shift['id']}", headers=admin_headers)
        assert resp.status_code == 204
        remaining = client.get(f"/sessions/{activation['id']}/net-control-shifts", headers=admin_headers).json()
        assert remaining == []

    def test_non_member_cannot_create_shift(self, client, admin_headers, user_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        resp = client.post(
            f"/sessions/{activation['id']}/net-control-shifts",
            json={"callsign": "W2NEXT", "scheduled_start": "2026-09-01T14:00:00Z"},
            headers=user_headers,
        )
        assert resp.status_code == 403

    def test_non_member_cannot_list_shifts(self, client, admin_headers, user_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        resp = client.get(f"/sessions/{activation['id']}/net-control-shifts", headers=user_headers)
        assert resp.status_code == 403

    def test_non_member_cannot_delete_shift(self, client, admin_headers, user_headers):
        anet = _ares_net(client, admin_headers)
        activation = _activation_session(client, admin_headers, anet["id"])
        shift = client.post(
            f"/sessions/{activation['id']}/net-control-shifts",
            json={"callsign": "W2NEXT", "scheduled_start": "2026-09-01T14:00:00Z"},
            headers=admin_headers,
        ).json()
        resp = client.delete(f"/net-control-shifts/{shift['id']}", headers=user_headers)
        assert resp.status_code == 403
