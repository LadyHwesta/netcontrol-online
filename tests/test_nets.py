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
        assert resp.json() == {"share_with_all": False, "user_ids": []}

    def test_share_with_specific_user_round_trips(self, client, admin_headers, user_headers, net):
        other_id = self._other_user_id(client, admin_headers)
        put_resp = client.put(f"/nets/{net['id']}/shares", json={
            "share_with_all": False, "user_ids": [other_id],
        }, headers=admin_headers)
        assert put_resp.status_code == 204

        get_resp = client.get(f"/nets/{net['id']}/shares", headers=admin_headers)
        assert get_resp.json() == {"share_with_all": False, "user_ids": [other_id]}

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
        assert get_resp.json() == {"share_with_all": False, "user_ids": []}

        listed = client.get("/nets", headers=user_headers).json()
        assert net["id"] not in [n["id"] for n in listed]

    def test_non_owner_cannot_view_or_edit_shares(self, client, admin_headers, user_headers, net):
        get_resp = client.get(f"/nets/{net['id']}/shares", headers=user_headers)
        assert get_resp.status_code in (403, 404)
        put_resp = client.put(f"/nets/{net['id']}/shares", json={"share_with_all": True, "user_ids": []}, headers=user_headers)
        assert put_resp.status_code in (403, 404)
