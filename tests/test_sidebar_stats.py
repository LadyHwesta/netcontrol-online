"""
Tests for GET /stats (issue follow-up) — feeds both the top #sidebar-stats
panel (index.html only) and the bottom #sidebar-summary panel (every page).
The pending_members field is the new piece: null for anyone who isn't an
org/super admin of their current org, a real count for anyone who is.
"""
from helpers import register, auth


class TestPendingMembersStat:
    def test_null_for_non_admin(self, client, admin_headers, user_headers):
        resp = client.get("/stats", headers=user_headers)
        assert resp.status_code == 200
        assert resp.json()["pending_members"] is None

    def test_counted_for_org_admin(self, client, admin_headers, user_headers):
        # A pending, not-yet-approved third registrant joining the same
        # (default) org as admin_headers/user_headers.
        register(client, "W3PEND", "Pending User", "pending@example.com")
        resp = client.get("/stats", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["pending_members"] == 1

    def test_approving_decrements_the_count(self, client, admin_headers, user_headers):
        pending = register(client, "W3PEND2", "Pending User", "pending2@example.com")
        client.patch(f"/admin/users/{pending.json()['id']}/approve", headers=admin_headers)
        resp = client.get("/stats", headers=admin_headers)
        assert resp.json()["pending_members"] == 0

    def test_zero_not_null_when_admin_has_no_pending(self, client, admin_headers):
        resp = client.get("/stats", headers=admin_headers)
        assert resp.json()["pending_members"] == 0


class TestActiveIncidentsStat:
    def test_zero_when_none(self, client, admin_headers):
        resp = client.get("/stats", headers=admin_headers)
        assert resp.json()["active_incidents"] == 0

    def test_counts_only_active_status(self, client, admin_headers, net):
        i1 = client.post(f"/nets/{net['id']}/incidents", json={"title": "Fire"}, headers=admin_headers).json()
        client.post(f"/nets/{net['id']}/incidents", json={"title": "Flood"}, headers=admin_headers)
        client.patch(f"/incidents/{i1['id']}", json={"status": "resolved"}, headers=admin_headers)

        resp = client.get("/stats", headers=admin_headers)
        assert resp.json()["active_incidents"] == 1
