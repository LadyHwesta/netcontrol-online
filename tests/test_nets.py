"""
Tests for net management endpoints:
  GET    /nets
  POST   /nets
  GET    /nets/{id}
  PUT    /nets/{id}
  DELETE /nets/{id}
  GET    /nets/{id}/shares
  PUT    /nets/{id}/shares
"""

from helpers import auth


class TestNetCRUD:
    def test_create_net(self, client, admin_headers):
        resp = client.post("/nets", json={
            "name": "2m Monday Net",
            "frequency": "146.520 MHz",
            "description": "Weekly Monday net",
            "is_ares": False,
        }, headers=admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "2m Monday Net"
        assert data["frequency"] == "146.520 MHz"
        assert "id" in data
        # Derived sharing fields must be genuinely computed, not left at the
        # NetOut schema's bare defaults (create_net previously returned the
        # raw ORM object instead of running it through _net_to_out -- correct
        # by coincidence for is_owner/shared_* on a brand new net, but wrong
        # for can_edit and owner_callsign).
        assert data["can_edit"] is True
        assert data["owner_callsign"] == "W1ADMIN"

    def test_create_net_with_script(self, client, admin_headers):
        resp = client.post("/nets", json={
            "name": "Scripted Net",
            "is_ares": False,
            "script": "1. Open with callsign\n2. Ask for check-ins",
        }, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["script"] == "1. Open with callsign\n2. Ask for check-ins"

    def test_create_net_without_script_defaults_to_none(self, client, admin_headers):
        resp = client.post("/nets", json={"name": "Scriptless Net", "is_ares": False}, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["script"] is None

    def test_create_net_with_broadcast(self, client, admin_headers):
        resp = client.post("/nets", json={
            "name": "Newsline Net", "is_ares": False,
            "has_broadcast": True, "broadcast_label": "Amateur Radio Newsline",
        }, headers=admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["has_broadcast"] is True
        assert data["broadcast_label"] == "Amateur Radio Newsline"

    def test_broadcast_label_ignored_when_broadcast_disabled(self, client, admin_headers):
        resp = client.post("/nets", json={
            "name": "Plain Net", "is_ares": False,
            "has_broadcast": False, "broadcast_label": "Should be dropped",
        }, headers=admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["has_broadcast"] is False
        assert data["broadcast_label"] is None

    def test_create_ares_net(self, client, admin_headers):
        resp = client.post("/nets", json={
            "name": "ARES Net",
            "is_ares": True,
        }, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["is_ares"] is True

    def test_list_nets_includes_owned(self, client, admin_headers, net):
        resp = client.get("/nets", headers=admin_headers)
        assert resp.status_code == 200
        ids = [n["id"] for n in resp.json()]
        assert net["id"] in ids

    def test_list_nets_empty(self, client, admin_headers):
        """Admin with no nets gets an empty list, not an error."""
        resp = client.get("/nets", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_net(self, client, admin_headers, net):
        resp = client.get(f"/nets/{net['id']}", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == net["id"]
        assert data["name"] == net["name"]

    def test_get_nonexistent_net_returns_404(self, client, admin_headers):
        resp = client.get("/nets/99999", headers=admin_headers)
        assert resp.status_code == 404

    def test_update_net(self, client, admin_headers, net):
        resp = client.put(f"/nets/{net['id']}", json={
            "name": "Updated Name",
            "frequency": "147.000 MHz",
            "description": "Now updated",
            "is_ares": True,
        }, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Updated Name"
        assert data["frequency"] == "147.000 MHz"
        assert data["is_ares"] is True

    def test_update_net_script(self, client, admin_headers, net):
        resp = client.put(f"/nets/{net['id']}", json={
            "name": net["name"],
            "is_ares": False,
            "script": "Updated script text",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["script"] == "Updated script text"

    def test_update_net_can_clear_script(self, client, admin_headers, net):
        client.put(f"/nets/{net['id']}", json={
            "name": net["name"], "is_ares": False, "script": "Some text",
        }, headers=admin_headers)
        resp = client.put(f"/nets/{net['id']}", json={
            "name": net["name"], "is_ares": False, "script": None,
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["script"] is None

    def test_delete_net(self, client, admin_headers, net):
        resp = client.delete(f"/nets/{net['id']}", headers=admin_headers)
        assert resp.status_code == 204
        # Confirm it's gone
        assert client.get(f"/nets/{net['id']}", headers=admin_headers).status_code == 404

    def test_delete_nonexistent_net_returns_404(self, client, admin_headers):
        resp = client.delete("/nets/99999", headers=admin_headers)
        assert resp.status_code == 404


class TestNetPermissions:
    def test_unauthenticated_cannot_list_nets(self, client):
        resp = client.get("/nets")
        assert resp.status_code == 401

    def test_unauthenticated_cannot_create_net(self, client):
        resp = client.post("/nets", json={"name": "Bad Net", "is_ares": False})
        assert resp.status_code == 401

    def test_user_cannot_see_others_private_net(self, client, admin_headers, user_headers, net):
        """A regular user should not be able to GET a net they don't own or share."""
        resp = client.get(f"/nets/{net['id']}", headers=user_headers)
        assert resp.status_code in (403, 404)

    def test_user_cannot_edit_others_net(self, client, admin_headers, user_headers, net):
        resp = client.put(f"/nets/{net['id']}", json={
            "name": "Hijacked", "is_ares": False,
        }, headers=user_headers)
        assert resp.status_code in (403, 404)

    def test_user_cannot_delete_others_net(self, client, admin_headers, user_headers, net):
        resp = client.delete(f"/nets/{net['id']}", headers=user_headers)
        assert resp.status_code in (403, 404)

    def test_admin_sees_all_nets(self, client, admin_headers, user_headers):
        """Admin should see nets owned by other users."""
        # user creates a net
        user_net = client.post("/nets", json={
            "name": "User's Net", "is_ares": False,
        }, headers=user_headers)
        assert user_net.status_code == 201

        # admin lists nets — should see user's net
        resp = client.get("/nets", headers=admin_headers)
        ids = [n["id"] for n in resp.json()]
        assert user_net.json()["id"] in ids


class TestNetSharing:
    """PUT /nets/{id}/shares had zero coverage until this bug was found: the
    net-edit form's own separate "Save Sharing" button was the only thing
    that ever called this endpoint -- the main Save button silently never
    did, so checking a share box and clicking the obvious Save button lost
    the change entirely. Fixed in nets.js's saveNet(); these lock in the
    endpoint's own behavior (share_with_all and individual user_ids both
    round-trip through GET, and non-owners are still excluded)."""

    def _other_user_id(self, client, admin_headers):
        users = client.get("/admin/users", headers=admin_headers).json()
        return next(u["id"] for u in users if u["callsign"] == "W2USER")

    def test_default_shares_empty(self, client, admin_headers, net):
        resp = client.get(f"/nets/{net['id']}/shares", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == {"share_with_all": False, "can_edit_all": False, "user_ids": [], "editor_user_ids": []}

    def test_share_with_specific_user_round_trips(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        put_resp = client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [other_id],
        }, headers=admin_headers)
        assert put_resp.status_code == 204

        get_resp = client.get(f"/nets/{net['id']}/shares", headers=admin_headers)
        assert get_resp.json() == {"share_with_all": False, "can_edit_all": False, "user_ids": [other_id], "editor_user_ids": []}

        # The shared user can now see the net in their own list
        listed = client.get("/nets", headers=user_headers).json()
        assert net["id"] in [n["id"] for n in listed]

    def test_share_with_all_round_trips(self, client, admin_headers, user_headers, net):
        put_resp = client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": True, "user_ids": [],
        }, headers=admin_headers)
        assert put_resp.status_code == 204

        get_resp = client.get(f"/nets/{net['id']}/shares", headers=admin_headers)
        assert get_resp.json()["share_with_all"] is True

        listed = client.get("/nets", headers=user_headers).json()
        assert net["id"] in [n["id"] for n in listed]

    def test_replacing_shares_removes_the_old_ones(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={"share_with_all": False, "user_ids": [other_id]}, headers=admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={"share_with_all": False, "user_ids": []}, headers=admin_headers)

        get_resp = client.get(f"/nets/{net['id']}/shares", headers=admin_headers)
        assert get_resp.json() == {"share_with_all": False, "can_edit_all": False, "user_ids": [], "editor_user_ids": []}

        listed = client.get("/nets", headers=user_headers).json()
        assert net["id"] not in [n["id"] for n in listed]

    def test_non_owner_cannot_view_or_edit_shares(self, client, admin_headers, user_headers, net):
        get_resp = client.get(f"/nets/{net['id']}/shares", headers=user_headers)
        assert get_resp.status_code in (403, 404)
        put_resp = client.put(f"/nets/{net['id']}/shares", json={"share_with_all": True, "user_ids": []}, headers=user_headers)
        assert put_resp.status_code in (403, 404)


class TestNetEditRights:
    """Sharing previously only ever granted view/check-in access -- no way
    to let a trusted co-operator also help maintain a net's details,
    schedule, or DMR config without handing them full ownership (issue
    follow-up). NetShare.can_edit / editor_user_ids / can_edit_all add that;
    delete_net and sharing management itself stay owner/admin-only
    regardless (see _get_owned_net vs _get_editable_net in main.py)."""

    def _other_user_id(self, client, admin_headers):
        users = client.get("/admin/users", headers=admin_headers).json()
        return next(u["id"] for u in users if u["callsign"] == "W2USER")

    def test_editor_share_can_edit_net_details(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [other_id], "editor_user_ids": [other_id],
        }, headers=admin_headers)

        resp = client.put(f"/nets/{net['id']}", json={"name": "Edited By Editor", "is_ares": False}, headers=user_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Edited By Editor"

    def test_view_only_share_cannot_edit_net_details(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [other_id], "editor_user_ids": [],
        }, headers=admin_headers)

        resp = client.put(f"/nets/{net['id']}", json={"name": "Hijacked", "is_ares": False}, headers=user_headers)
        assert resp.status_code in (403, 404)

    def test_editor_share_cannot_delete_net(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [other_id], "editor_user_ids": [other_id],
        }, headers=admin_headers)

        resp = client.delete(f"/nets/{net['id']}", headers=user_headers)
        assert resp.status_code in (403, 404)

    def test_editor_share_cannot_manage_sharing(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [other_id], "editor_user_ids": [other_id],
        }, headers=admin_headers)

        get_resp = client.get(f"/nets/{net['id']}/shares", headers=user_headers)
        assert get_resp.status_code in (403, 404)
        put_resp = client.put(f"/nets/{net['id']}/shares", json={"share_with_all": True, "user_ids": []}, headers=user_headers)
        assert put_resp.status_code in (403, 404)

    def test_editor_share_can_manage_schedule(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [other_id], "editor_user_ids": [other_id],
        }, headers=admin_headers)

        resp = client.post(f"/nets/{net['id']}/schedules", json={
            "day_of_week": 0, "start_time": "19:00", "timezone": "America/Los_Angeles",
        }, headers=user_headers)
        assert resp.status_code == 201, resp.text

    def test_can_edit_all_grants_edit_to_everyone_shared(self, client, admin_headers, user_headers, net):
        client.put(f"/nets/{net['id']}/shares", json={"share_with_all": True, "can_edit_all": True, "user_ids": []}, headers=admin_headers)

        resp = client.put(f"/nets/{net['id']}", json={"name": "Edited Via Share-All", "is_ares": False}, headers=user_headers)
        assert resp.status_code == 200, resp.text

    def test_share_with_all_without_edit_flag_stays_view_only(self, client, admin_headers, user_headers, net):
        client.put(f"/nets/{net['id']}/shares", json={"share_with_all": True, "can_edit_all": False, "user_ids": []}, headers=admin_headers)

        resp = client.put(f"/nets/{net['id']}", json={"name": "Hijacked", "is_ares": False}, headers=user_headers)
        assert resp.status_code in (403, 404)

    def test_removing_share_also_removes_edit_rights(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [other_id], "editor_user_ids": [other_id],
        }, headers=admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={"share_with_all": False, "user_ids": [], "editor_user_ids": []}, headers=admin_headers)

        resp = client.put(f"/nets/{net['id']}", json={"name": "Hijacked", "is_ares": False}, headers=user_headers)
        assert resp.status_code in (403, 404)

    def test_net_out_can_edit_reflects_permissions(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [other_id], "editor_user_ids": [other_id],
        }, headers=admin_headers)

        owner_view = next(n for n in client.get("/nets", headers=admin_headers).json() if n["id"] == net["id"])
        assert owner_view["can_edit"] is True

        editor_view = next(n for n in client.get("/nets", headers=user_headers).json() if n["id"] == net["id"])
        assert editor_view["can_edit"] is True
        assert editor_view["is_owner"] is False
        assert editor_view["editor_user_ids"] == [other_id]


class TestNetOwnershipTransfer:
    def _other_user_id(self, client, admin_headers):
        users = client.get("/admin/users", headers=admin_headers).json()
        return next(u["id"] for u in users if u["callsign"] == "W2USER")

    def test_owner_can_transfer_to_another_member(self, client, admin_headers, user_headers, net):
        # admin_headers here is this test DB's super admin (first-ever user)
        # AND net's current owner -- exercises the "current owner" path since
        # is_admin would bypass the ownership check regardless.
        other_id = self._other_user_id(client, admin_headers)
        resp = client.patch(f"/nets/{net['id']}/owner", json={"owner_id": other_id}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_id"] == other_id

        # New owner can now edit it themselves
        edit_resp = client.put(f"/nets/{net['id']}", json={"name": "New Owner Edit", "is_ares": False}, headers=user_headers)
        assert edit_resp.status_code == 200, edit_resp.text

    def test_cannot_transfer_to_unknown_user(self, client, admin_headers, net):
        resp = client.patch(f"/nets/{net['id']}/owner", json={"owner_id": 999999}, headers=admin_headers)
        assert resp.status_code == 404

    def test_non_owner_non_admin_cannot_transfer(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        resp = client.patch(f"/nets/{net['id']}/owner", json={"owner_id": other_id}, headers=user_headers)
        assert resp.status_code in (403, 404)
