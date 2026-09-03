"""
Tests for the role revamp (issue follow-up):
  - Org-level roles: OrganizationMembership's base role ('admin'/'member',
    displayed as "Net Control Op") plus OrganizationMembershipRole for the
    two additive self-service roles (Tactical Operator, Broadcaster).
  - Net-level grants: NetShare.can_edit (net_control_op, unchanged) plus
    NetShareRole for the same two extra roles -- only ever actually granted
    for a role the target user's org membership already holds (silently
    dropped otherwise).
  - The Broadcaster self-signup path on POST /nets/{id}/signups.
  - Tactical Operator identity enforcement on sign-on/off.

Registration's requested_roles is an informational hint only; the org
admin's approve/extra-roles endpoints are what actually grant anything.
"""

from helpers import register, login, auth


def _bootstrap_super_admin(client):
    register(client, "W0SUPER", "Super", "w0super@example.com")
    return login(client, "W0SUPER")


def _org_owner(client, super_token, callsign, org_slug, org_name, website="https://example.org"):
    """Founds a brand new org, has the super admin approve it, returns the JWT."""
    resp = client.post("/auth/register", json={
        "callsign": callsign, "name": callsign, "email": f"{callsign.lower()}@example.com",
        "password": "testpass123", "org_slug": org_slug, "org_name": org_name, "org_website_url": website,
    })
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["id"]
    approve = client.patch(f"/admin/users/{user_id}/approve", headers=auth(super_token))
    assert approve.status_code == 200, approve.text
    return login(client, callsign)


def _join_and_approve(client, owner_token, callsign, org_slug, requested_roles=None, granted_roles=None):
    """Registers a second user joining owner_token's org, then has the org
    admin approve them. `granted_roles`, when given explicitly, is exactly
    what gets granted (a test wanting a role deliberately absent passes
    granted_roles=[] or omits tactical_operator/broadcaster). Otherwise
    defaults to net_control_op + whatever was requested, matching the admin
    panel's own default (Net Control Op pre-checked, the normal case).
    Returns (jwt, user_id, org_id)."""
    body = {
        "callsign": callsign, "name": callsign, "email": f"{callsign.lower()}@example.com",
        "password": "testpass123", "org_slug": org_slug,
    }
    if requested_roles is not None:
        body["requested_roles"] = requested_roles
    resp = client.post("/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["id"]
    org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]
    if granted_roles is not None:
        roles = granted_roles
    else:
        roles = list(dict.fromkeys(["net_control_op"] + (requested_roles or [])))
    approve = client.patch(
        f"/orgs/{org_id}/members/{user_id}/approve", json={"roles": roles}, headers=auth(owner_token),
    )
    assert approve.status_code == 204, approve.text
    return login(client, callsign), user_id, org_id


def _member_row(client, owner_token, org_id, user_id):
    members = client.get(f"/orgs/{org_id}/members", headers=auth(owner_token)).json()
    return next(m for m in members if m["user_id"] == user_id)


class TestRequestedRoles:
    def test_admin_not_self_requestable(self, client):
        resp = client.post("/auth/register", json={
            "callsign": "W1BAD", "name": "Bad", "email": "bad@example.com",
            "password": "testpass123", "requested_roles": ["admin"],
        })
        assert resp.status_code == 422

    def test_requested_roles_shown_on_pending_queue_but_not_granted(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1OWN", "roleco", "Role Co")
        resp = client.post("/auth/register", json={
            "callsign": "W2REQ", "name": "Req", "email": "req@example.com",
            "password": "testpass123", "org_slug": "roleco", "requested_roles": ["tactical_operator"],
        })
        assert resp.status_code == 201, resp.text
        org_id = client.get("/auth/me", headers=auth(owner_token)).json()["current_org_id"]
        pending = client.get(f"/orgs/{org_id}/pending-members", headers=auth(owner_token)).json()
        row = next(m for m in pending if m["callsign"] == "W2REQ")
        assert row["requested_roles"] == ["tactical_operator"]
        # Nothing is actually granted until the admin approves with roles=[...]
        # -- role revamp (issue follow-up): net_control_op is no longer
        # automatic for an unapproved (or even approved-but-ungranted)
        # membership, so a still-pending row shows no roles at all yet.
        assert row["roles"] == []


class TestOrgApproveAndExtraRoles:
    def test_approve_grants_requested_roles(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1OWN2", "roleco2", "Role Co 2")
        _, uid, org_id = _join_and_approve(client, owner_token, "W2TAC", "roleco2", requested_roles=["tactical_operator"])
        row = _member_row(client, owner_token, org_id, uid)
        assert sorted(row["roles"]) == ["net_control_op", "tactical_operator"]

    def test_extra_roles_endpoint_replaces_the_set(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1OWN3", "roleco3", "Role Co 3")
        _, uid, org_id = _join_and_approve(client, owner_token, "W2BOTH", "roleco3", granted_roles=["tactical_operator"])

        resp = client.put(
            f"/orgs/{org_id}/members/{uid}/extra-roles", json={"roles": ["broadcaster"]}, headers=auth(owner_token),
        )
        assert resp.status_code == 200, resp.text
        row = _member_row(client, owner_token, org_id, uid)
        # A full replace -- tactical_operator (the only role originally
        # granted, net_control_op deliberately excluded) is gone, only
        # broadcaster remains.
        assert row["roles"] == ["broadcaster"]

    def test_extra_roles_rejects_admin(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1OWN4", "roleco4", "Role Co 4")
        _, uid, org_id = _join_and_approve(client, owner_token, "W2ADM", "roleco4")
        resp = client.put(
            f"/orgs/{org_id}/members/{uid}/extra-roles", json={"roles": ["admin"]}, headers=auth(owner_token),
        )
        assert resp.status_code == 422


class TestNetControlOpToggle:
    """net_control_op used to be automatic and non-revocable (implicit in
    OrganizationMembership.role == "member"); it's now a real, independently
    toggleable entry in extra_roles, symmetric with Tactical Operator/
    Broadcaster (issue found live: "not clickable like the others")."""

    def test_global_approve_grants_net_control_op_by_default(self, client, admin_token, user_headers):
        """user_headers goes through the plain super-admin /admin/users/{id}/
        approve path (no roles body of its own) -- it must still come out
        with net_control_op, same as before this whole revamp existed."""
        me = client.get("/auth/me", headers=user_headers).json()
        org_id = me["current_org_id"]
        row = _member_row(client, admin_token, org_id, me["id"])
        assert row["roles"] == ["net_control_op"]

    def test_toggle_off_and_on(self, client):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1NCOWN", "ncotoggle", "NCO Toggle Co")
        _, uid, org_id = _join_and_approve(client, owner_token, "W2NCO", "ncotoggle")
        assert _member_row(client, owner_token, org_id, uid)["roles"] == ["net_control_op"]

        # Revoke it (an admin deciding this person should be Tactical
        # Operator-only, say) via the same clickable-badge endpoint.
        resp = client.put(f"/orgs/{org_id}/members/{uid}/extra-roles", json={"roles": []}, headers=auth(owner_token))
        assert resp.status_code == 200, resp.text
        assert _member_row(client, owner_token, org_id, uid)["roles"] == []

        # Grant it back.
        resp = client.put(f"/orgs/{org_id}/members/{uid}/extra-roles", json={"roles": ["net_control_op"]}, headers=auth(owner_token))
        assert resp.status_code == 200, resp.text
        assert _member_row(client, owner_token, org_id, uid)["roles"] == ["net_control_op"]

    def test_admin_can_also_hold_it_as_an_extra_role(self, client):
        """An org founder (base role 'admin') doesn't get net_control_op
        automatically (admin stays orthogonal to net access), but can
        explicitly hold it too -- symmetric with Tactical Operator/
        Broadcaster, all three toggleable regardless of admin status."""
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, "W1ADMNCO", "adminnco", "Admin NCO Co")
        me = client.get("/auth/me", headers=auth(owner_token)).json()
        org_id = me["current_org_id"]
        # "admin" itself is always present in roles for an admin membership
        # (the base role, see _org_member_out_rows) -- it's net_control_op
        # specifically that isn't automatic for them.
        assert _member_row(client, owner_token, org_id, me["id"])["roles"] == ["admin"]

        resp = client.put(f"/orgs/{org_id}/members/{me['id']}/extra-roles", json={"roles": ["net_control_op"]}, headers=auth(owner_token))
        assert resp.status_code == 200, resp.text
        assert sorted(_member_row(client, owner_token, org_id, me["id"])["roles"]) == ["admin", "net_control_op"]


class TestNetShareRoleGate:
    def _owner_and_net(self, client, callsign="W1NOWN", slug="netroleco"):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, callsign, slug, "Net Role Co")
        net = client.post("/nets", json={"name": "Test Net"}, headers=auth(owner_token)).json()
        return owner_token, net

    def test_list_users_exposes_roles_for_the_share_picker(self, client):
        """Feeds the net-sharing UI's eligibility gate (issue found live) --
        GET /users must show each candidate's org role set so the picker can
        grey out a role they don't actually hold, instead of silently
        dropping the selection on save with no explanation."""
        owner_token, net = self._owner_and_net(client, "W1LISTU", "netrolelist")
        _, uid, _ = _join_and_approve(client, owner_token, "W2LISTU", "netrolelist", granted_roles=["broadcaster"])
        users = client.get(f"/users?net_id={net['id']}", headers=auth(owner_token)).json()
        row = next(u for u in users if u["id"] == uid)
        assert row["roles"] == ["broadcaster"]

    def test_multiple_broadcasters_on_one_net(self, client):
        """Regression (issue found live): sharing a net with more than one
        user as Broadcaster must grant it to ALL of them, not just one --
        the backend logic was always correct here; the actual live bug was
        the org-role gate silently dropping anyone the org admin hadn't
        separately granted Broadcaster to, with no visibility into why. This
        locks in the case where everyone selected legitimately holds it."""
        owner_token, net = self._owner_and_net(client, "W1MULTIBC", "netrolemultibc")
        _, uid1, _ = _join_and_approve(client, owner_token, "W2MULTIBC", "netrolemultibc", granted_roles=["broadcaster"])
        _, uid2, _ = _join_and_approve(client, owner_token, "W3MULTIBC", "netrolemultibc", granted_roles=["broadcaster"])

        resp = client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid1, uid2], "broadcaster_user_ids": [uid1, uid2],
        }, headers=auth(owner_token))
        assert resp.status_code == 204, resp.text
        shares = client.get(f"/nets/{net['id']}/shares", headers=auth(owner_token)).json()
        assert sorted(shares["broadcaster_user_ids"]) == sorted([uid1, uid2])

    def test_grants_role_the_user_holds(self, client):
        owner_token, net = self._owner_and_net(client)
        _, uid, _ = _join_and_approve(client, owner_token, "W2HOLD", "netroleco", granted_roles=["tactical_operator"])

        resp = client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "editor_user_ids": [],
            "tactical_operator_user_ids": [uid],
        }, headers=auth(owner_token))
        assert resp.status_code == 204, resp.text
        shares = client.get(f"/nets/{net['id']}/shares", headers=auth(owner_token)).json()
        assert uid in shares["tactical_operator_user_ids"]

    def test_silently_drops_role_the_user_lacks(self, client):
        owner_token, net = self._owner_and_net(client, "W1NOWN2", "netroleco2")
        _, uid, _ = _join_and_approve(client, owner_token, "W2LACK", "netroleco2")  # net_control_op only, no tactical_operator

        resp = client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "editor_user_ids": [],
            "tactical_operator_user_ids": [uid],
        }, headers=auth(owner_token))
        assert resp.status_code == 204, resp.text
        shares = client.get(f"/nets/{net['id']}/shares", headers=auth(owner_token)).json()
        assert uid not in shares["tactical_operator_user_ids"]
        # The share itself still exists (view access), just without the role.
        assert uid in shares["user_ids"]

    def test_resharing_with_roles_twice_does_not_error(self, client):
        """Regression: SQLite doesn't enforce ON DELETE CASCADE for the bulk
        `delete(NetShare)` this endpoint issues (no PRAGMA foreign_keys=ON),
        so a naive implementation orphans NetShareRole rows on the first PUT
        -- invisible until a *second* PUT recreates a NetShare row that
        happens to reuse a freed id, which then collides with the orphan on
        insert. update_net_shares explicitly cleans up NetShareRole first,
        so this must succeed both times."""
        owner_token, net = self._owner_and_net(client, "W1NOWN4", "netroleco4")
        _, uid, _ = _join_and_approve(client, owner_token, "W2TWICE", "netroleco4", granted_roles=["tactical_operator"])

        for _ in range(2):
            resp = client.put(f"/nets/{net['id']}/shares", json={
                "share_with_all": False, "user_ids": [uid], "tactical_operator_user_ids": [uid],
            }, headers=auth(owner_token))
            assert resp.status_code == 204, resp.text
        shares = client.get(f"/nets/{net['id']}/shares", headers=auth(owner_token)).json()
        assert uid in shares["tactical_operator_user_ids"]

    def test_share_with_all_extra_roles(self, client):
        owner_token, net = self._owner_and_net(client, "W1NOWN3", "netroleco3")
        resp = client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": True, "tactical_operator_all": True, "broadcaster_all": False,
        }, headers=auth(owner_token))
        assert resp.status_code == 204, resp.text
        shares = client.get(f"/nets/{net['id']}/shares", headers=auth(owner_token)).json()
        assert shares["tactical_operator_all"] is True
        assert shares["broadcaster_all"] is False


class TestBroadcasterSelfSignup:
    def _net_with_schedule(self, client, owner_token):
        net = client.post("/nets", json={"name": "Bcast Net", "has_broadcast": True}, headers=auth(owner_token)).json()
        sched = client.post(f"/nets/{net['id']}/schedules", json={
            "day_of_week": 0, "start_time": "19:00", "timezone": "UTC",
        }, headers=auth(owner_token)).json()
        slot = client.get(f"/nets/{net['id']}/upcoming", headers=auth(owner_token)).json()[0]
        return net, sched, slot

    def _setup(self, client, callsign="W1BOWN", slug="bcastco"):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, callsign, slug, "Bcast Co")
        net, sched, slot = self._net_with_schedule(client, owner_token)
        return owner_token, net, sched, slot, slug

    def test_broadcaster_share_can_read_and_self_signup(self, client):
        owner_token, net, sched, slot, slug = self._setup(client)
        bc_token, uid, _ = _join_and_approve(client, owner_token, "W2BCAST", slug, granted_roles=["broadcaster"])
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "broadcaster_user_ids": [uid],
        }, headers=auth(owner_token))

        # Read access loosened -- any share suffices now.
        resp = client.get(f"/nets/{net['id']}/schedules", headers=auth(bc_token))
        assert resp.status_code == 200, resp.text

        resp = client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": slot["slot_date"], "role": "broadcaster", "callsign": "W2BCAST",
        }, headers=auth(bc_token))
        assert resp.status_code == 201, resp.text

    def test_broadcaster_share_cannot_claim_net_control(self, client):
        owner_token, net, sched, slot, slug = self._setup(client, "W1BOWN2", "bcastco2")
        bc_token, uid, _ = _join_and_approve(client, owner_token, "W2BCAST2", slug, granted_roles=["broadcaster"])
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "broadcaster_user_ids": [uid],
        }, headers=auth(owner_token))

        resp = client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": slot["slot_date"], "role": "net_control", "callsign": "W2BCAST2",
        }, headers=auth(bc_token))
        assert resp.status_code == 403

    def test_broadcaster_share_cannot_assign_others(self, client):
        owner_token, net, sched, slot, slug = self._setup(client, "W1BOWN3", "bcastco3")
        bc_token, uid, org_id = _join_and_approve(client, owner_token, "W2BCAST3", slug, granted_roles=["broadcaster"])
        other_token, other_id, _ = _join_and_approve(client, owner_token, "W3OTHER", slug)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid, other_id], "broadcaster_user_ids": [uid],
        }, headers=auth(owner_token))

        resp = client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": slot["slot_date"], "role": "broadcaster", "assigned_user_id": other_id,
        }, headers=auth(bc_token))
        assert resp.status_code == 403

    def test_view_only_share_cannot_signup(self, client):
        owner_token, net, sched, slot, slug = self._setup(client, "W1BOWN4", "bcastco4")
        view_token, uid, _ = _join_and_approve(client, owner_token, "W2VIEW", slug)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid],
        }, headers=auth(owner_token))

        # Read still works (any share)...
        resp = client.get(f"/nets/{net['id']}/schedules", headers=auth(view_token))
        assert resp.status_code == 200, resp.text
        # ...but signing up does not, without net_control_op or broadcaster.
        resp = client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": slot["slot_date"], "role": "broadcaster", "callsign": "W2VIEW",
        }, headers=auth(view_token))
        assert resp.status_code == 403


class TestTacticalIdentityEnforcement:
    def _activation_with_position(self, client, owner_token):
        net = client.post("/nets", json={"name": "Tac Net", "is_ares": True}, headers=auth(owner_token)).json()
        session = client.post(f"/nets/{net['id']}/sessions", json={"is_activation": True}, headers=auth(owner_token)).json()
        position = client.post(f"/sessions/{session['id']}/tactical-positions", json={
            "tactical_callsign": "SHELTER 1",
        }, headers=auth(owner_token)).json()
        return net, session, position

    def _setup(self, client, callsign="W1TOWN", slug="tacco"):
        super_token = _bootstrap_super_admin(client)
        owner_token = _org_owner(client, super_token, callsign, slug, "Tac Co")
        net, session, position = self._activation_with_position(client, owner_token)
        return owner_token, net, session, position, slug

    def test_tactical_share_can_sign_on_own_callsign(self, client):
        owner_token, net, session, position, slug = self._setup(client)
        tac_token, uid, _ = _join_and_approve(client, owner_token, "W2TAC1", slug, granted_roles=["tactical_operator"])
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "tactical_operator_user_ids": [uid],
        }, headers=auth(owner_token))

        resp = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W2TAC1"}, headers=auth(tac_token))
        assert resp.status_code == 201, resp.text

    def test_tactical_share_cannot_sign_on_other_callsign(self, client):
        owner_token, net, session, position, slug = self._setup(client, "W1TOWN2", "tacco2")
        tac_token, uid, _ = _join_and_approve(client, owner_token, "W2TAC2", slug, granted_roles=["tactical_operator"])
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "tactical_operator_user_ids": [uid],
        }, headers=auth(owner_token))

        resp = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W9OTHER"}, headers=auth(tac_token))
        assert resp.status_code == 403

    def test_tactical_share_cannot_evict_other_occupant(self, client):
        owner_token, net, session, position, slug = self._setup(client, "W1TOWN3", "tacco3")
        client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ALREADY"}, headers=auth(owner_token))
        tac_token, uid, _ = _join_and_approve(client, owner_token, "W2TAC3", slug, granted_roles=["tactical_operator"])
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "tactical_operator_user_ids": [uid],
        }, headers=auth(owner_token))

        resp = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W2TAC3"}, headers=auth(tac_token))
        assert resp.status_code == 403

    def test_tactical_share_can_sign_self_off(self, client):
        owner_token, net, session, position, slug = self._setup(client, "W1TOWN4", "tacco4")
        tac_token, uid, _ = _join_and_approve(client, owner_token, "W2TAC4", slug, granted_roles=["tactical_operator"])
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "tactical_operator_user_ids": [uid],
        }, headers=auth(owner_token))
        client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W2TAC4"}, headers=auth(tac_token))

        resp = client.post(f"/tactical-positions/{position['id']}/sign-off", headers=auth(tac_token))
        assert resp.status_code == 200, resp.text

    def test_tactical_share_cannot_sign_off_other(self, client):
        owner_token, net, session, position, slug = self._setup(client, "W1TOWN5", "tacco5")
        client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W1ALREADY"}, headers=auth(owner_token))
        tac_token, uid, _ = _join_and_approve(client, owner_token, "W2TAC5", slug, granted_roles=["tactical_operator"])
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "tactical_operator_user_ids": [uid],
        }, headers=auth(owner_token))

        resp = client.post(f"/tactical-positions/{position['id']}/sign-off", headers=auth(tac_token))
        assert resp.status_code == 403

    def test_full_access_share_keeps_unrestricted_behavior(self, client):
        owner_token, net, session, position, slug = self._setup(client, "W1TOWN6", "tacco6")
        editor_token, uid, _ = _join_and_approve(client, owner_token, "W2EDIT", slug)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [uid], "editor_user_ids": [uid],
        }, headers=auth(owner_token))

        resp = client.post(f"/tactical-positions/{position['id']}/sign-on", json={"callsign": "W9ANYONE"}, headers=auth(editor_token))
        assert resp.status_code == 201, resp.text
