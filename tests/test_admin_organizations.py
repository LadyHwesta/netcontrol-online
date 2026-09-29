"""
Tests for the super admin role redefinition (issue follow-up):
  - GET /orgs/mine now returns EVERY organization for a super admin, not
    just ones they hold a real membership in -- the gap that made the org
    switcher (and everything driven off it: the Organization tab, per-org
    Fediverse/Languages cards) unreachable for any org a super admin
    wasn't already a member of, even though the underlying org-scoped
    endpoints already accepted an explicit org_id from them regardless.
  - New GET /admin/organizations -- every org on the instance with
    member/pending/net counts, the "All Organizations" control-panel tab's
    backend. Deliberately not filtered to orgs with an approved admin
    (unlike the public GET /orgs), so it also surfaces an orphaned org.
"""

from helpers import auth


def _found_org(client, callsign, org_slug, org_name, email=None):
    """Registers a founder for a brand new org; returns (user_id, org_id)."""
    resp = client.post("/auth/register", json={
        "callsign": callsign, "name": callsign, "email": email or f"{callsign.lower()}@example.com",
        "password": "testpass123", "org_slug": org_slug, "org_name": org_name, "org_website_url": "https://example.com",
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["id"], data["current_org_id"]


class TestOrgsMineForSuperAdmin:
    def test_super_admin_sees_every_org_including_ones_not_a_member_of(self, client, admin_headers, net):
        other_founder_id, _ = _found_org(client, "W9OTHER", "otherorg", "Other Org")
        client.patch(f"/admin/users/{other_founder_id}/approve", headers=admin_headers)

        mine = client.get("/orgs/mine", headers=admin_headers).json()
        slugs = {o["slug"] for o in mine}
        assert "otherorg" in slugs
        assert all(o["role"] == "admin" for o in mine)
        assert all(o["roles"] == ["admin"] for o in mine)

    def test_plain_user_still_scoped_to_their_own_orgs(self, client, user_headers, net):
        mine = client.get("/orgs/mine", headers=user_headers).json()
        assert [o["id"] for o in mine] == [net["org_id"]]


class TestAdminOrganizations:
    def test_requires_super_admin(self, client, user_headers, net):
        resp = client.get("/admin/organizations", headers=user_headers)
        assert resp.status_code == 403

    def test_lists_every_org_with_counts(self, client, admin_headers, net, user_headers):
        # admin_headers (super admin) + user_headers are both approved
        # members of `net`'s org; `net` itself is one net in that org.
        resp = client.get("/admin/organizations", headers=admin_headers)
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["id"] == net["org_id"])
        assert row["member_count"] == 2
        assert row["pending_count"] == 0
        assert row["net_count"] == 1

    def test_includes_orphaned_org_unlike_get_orgs(self, client, admin_headers):
        founder_id, org_id = _found_org(client, "W8ORPHAN", "orphanorg", "Orphan Org")
        # Demote the org's only admin to a plain member (a super admin can do
        # this to someone else's org; the endpoint only blocks self-demotion)
        # -- the membership row survives (nothing for _delete_orphaned_orgs
        # to clean up), but the org no longer has an approved ADMIN, which is
        # exactly what GET /orgs's filter excludes.
        resp = client.patch(
            f"/orgs/{org_id}/members/{founder_id}/role", json={"role": "member"}, headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text

        public_slugs = {o["slug"] for o in client.get("/orgs").json()}
        assert "orphanorg" not in public_slugs

        admin_slugs = {o["slug"] for o in client.get("/admin/organizations", headers=admin_headers).json()}
        assert "orphanorg" in admin_slugs
