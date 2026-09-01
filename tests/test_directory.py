"""
Tests for the public net directory:
  Net.public_listed round-trip via POST/PUT /nets
  GET /public/directory
"""

from datetime import date


def _public_net(client, headers, name="Public Net", **extra):
    resp = client.post("/nets", json={
        "name": name, "is_ares": False, "public_listed": True, **extra,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestPublicListedSetting:
    def test_create_net_public_listed(self, client, admin_headers):
        net = _public_net(client, admin_headers)
        assert net["public_listed"] is True

    def test_create_net_defaults_unlisted(self, client, admin_headers):
        resp = client.post("/nets", json={"name": "Private Net", "is_ares": False}, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["public_listed"] is False

    def test_update_net_can_toggle_listing(self, client, admin_headers, net):
        resp = client.put(f"/nets/{net['id']}", json={
            "name": net["name"], "is_ares": False, "public_listed": True,
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["public_listed"] is True


class TestPublicDirectory:
    def test_directory_requires_no_auth(self, client):
        resp = client.get("/public/directory")
        assert resp.status_code == 200

    def test_directory_includes_listed_net(self, client, admin_headers):
        net = _public_net(client, admin_headers, frequency="146.520 MHz", description="Weekly chat net")
        resp = client.get("/public/directory")
        row = next((n for n in resp.json() if n["id"] == net["id"]), None)
        assert row is not None
        assert row["frequency"] == "146.520 MHz"
        assert row["description"] == "Weekly chat net"

    def test_directory_excludes_unlisted_net(self, client, admin_headers, net):
        """The `net` fixture is not public_listed by default."""
        resp = client.get("/public/directory")
        ids = [n["id"] for n in resp.json()]
        assert net["id"] not in ids

    def test_directory_shows_owner_callsign(self, client, admin_headers):
        net = _public_net(client, admin_headers)
        resp = client.get("/public/directory")
        row = next(n for n in resp.json() if n["id"] == net["id"])
        assert row["owner_callsign"] == "W1ADMIN"

    def test_directory_does_not_leak_owner_email(self, client, admin_headers):
        net = _public_net(client, admin_headers)
        resp = client.get("/public/directory")
        row = next(n for n in resp.json() if n["id"] == net["id"])
        assert "owner_email" not in row
        assert "email" not in row

    def test_directory_includes_schedules(self, client, admin_headers):
        net = _public_net(client, admin_headers)
        today = date.today()
        sched = client.post(f"/nets/{net['id']}/schedules", json={
            "day_of_week": today.weekday(), "start_time": "19:30", "timezone": "UTC",
        }, headers=admin_headers)
        assert sched.status_code == 201

        resp = client.get("/public/directory")
        row = next(n for n in resp.json() if n["id"] == net["id"])
        assert len(row["schedules"]) == 1
        assert row["schedules"][0]["start_time"] == "19:30"
        assert row["schedules"][0]["timezone"] == "UTC"
        assert row["schedules"][0]["day_name"] == today.strftime("%A")

    def test_directory_shows_gmrs_net_type(self, client, admin_headers):
        net = _public_net(client, admin_headers, name="GMRS Net", net_type="gmrs")
        resp = client.get("/public/directory")
        row = next(n for n in resp.json() if n["id"] == net["id"])
        assert row["net_type"] == "gmrs"

    def test_directory_shows_broadcast_info(self, client, admin_headers):
        net = _public_net(
            client, admin_headers, name="Newsline Net",
            has_broadcast=True, broadcast_label="Amateur Radio Newsline",
        )
        resp = client.get("/public/directory")
        row = next(n for n in resp.json() if n["id"] == net["id"])
        assert row["has_broadcast"] is True
        assert row["broadcast_label"] == "Amateur Radio Newsline"

    def test_directory_page_loads_without_auth(self, client):
        resp = client.get("/directory")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestPublicDirectoryOrgScoping:
    """Multi-tenancy (issue #1) — /directory and /live are per-org."""

    def test_directory_defaults_to_default_org(self, client, admin_headers):
        """Omitting ?org uses the "default" org — single-tenant backward compat."""
        net = _public_net(client, admin_headers)
        resp = client.get("/public/directory")
        ids = [n["id"] for n in resp.json()]
        assert net["id"] in ids

    def test_directory_org_param_isolates_other_orgs(self, client, admin_headers):
        _public_net(client, admin_headers)  # net in the "default" org

        # A second org's public net shouldn't show up under a different org slug
        second = client.post("/auth/register", json={
            "callsign": "W2SECOND", "name": "Second", "email": "second@example.com",
            "password": "testpass123", "org_slug": "second-org", "org_name": "Second Org",
            "org_website_url": "https://second.example.org",
        })
        assert second.status_code == 201
        # Founding a new org needs a super admin's sign-off before login (issue #1
        # follow-up) -- admin_headers' owner is the instance's first-ever user.
        approve = client.patch(f"/admin/users/{second.json()['id']}/approve", headers=admin_headers)
        assert approve.status_code == 200
        token = client.post("/auth/login", data={"username": "W2SECOND", "password": "testpass123"}).json()["access_token"]
        second_headers = {"Authorization": f"Bearer {token}"}
        second_net = _public_net(client, second_headers, name="Second Org Net")

        default_listing = client.get("/public/directory?org=default").json()
        second_listing = client.get("/public/directory?org=second-org").json()
        assert all(n["id"] != second_net["id"] for n in default_listing)
        assert any(n["id"] == second_net["id"] for n in second_listing)

    def test_directory_unknown_org_returns_empty_list(self, client):
        resp = client.get("/public/directory?org=does-not-exist")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_public_organizations_lists_orgs_with_listed_nets(self, client, admin_headers):
        _public_net(client, admin_headers)
        resp = client.get("/public/organizations")
        assert resp.status_code == 200
        assert any(o["slug"] == "default" for o in resp.json())

    def test_public_organization_by_slug_returns_branding(self, client, admin_headers):
        """GET /public/organizations/{slug} (issue follow-up — per-org
        branding) powers /directory/{slug} and /live/{slug}'s own header,
        independent of whether the org happens to have a public-listed net
        right now (unlike the picker endpoint above, which is filtered)."""
        org_id = client.get("/auth/me", headers=admin_headers).json()["current_org_id"]
        client.patch(f"/orgs/{org_id}", json={"name": "Default Org", "tagline": "Test Tagline"}, headers=admin_headers)
        resp = client.get("/public/organizations/default")
        assert resp.status_code == 200
        data = resp.json()
        assert data["slug"] == "default"
        assert data["tagline"] == "Test Tagline"
        assert data["has_logo"] is False

    def test_public_organization_by_slug_works_with_no_public_nets(self, client, admin_headers):
        """No public-listed net at all -- still resolves (the picker endpoint
        above would exclude it; this one intentionally doesn't)."""
        resp = client.get("/public/organizations/default")
        assert resp.status_code == 200
        assert resp.json()["slug"] == "default"

    def test_public_organization_by_slug_404s_for_unknown_slug(self, client):
        resp = client.get("/public/organizations/does-not-exist")
        assert resp.status_code == 404

    def test_directory_slug_route_loads_without_auth(self, client):
        resp = client.get("/directory/default")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_live_slug_route_loads_without_auth(self, client):
        resp = client.get("/live/default")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestSEO:
    """robots.txt / sitemap.xml / per-org meta tag injection (issue #1 follow-up)."""

    def test_robots_txt_allows_directory_and_live_disallows_everything_else(self, client):
        resp = client.get("/robots.txt")
        assert resp.status_code == 200
        body = resp.text
        assert "Allow: /directory" in body
        assert "Allow: /live" in body
        assert "Disallow: /" in body
        assert "Sitemap:" in body and "/sitemap.xml" in body

    def test_sitemap_lists_org_with_public_net(self, client, admin_headers):
        _public_net(client, admin_headers)  # in the "default" org
        resp = client.get("/sitemap.xml")
        assert resp.status_code == 200
        assert "application/xml" in resp.headers["content-type"]
        assert "/directory/default" in resp.text
        assert "/live/default" in resp.text

    def test_sitemap_excludes_org_with_no_public_nets(self, client, admin_headers, net):
        """The `net` fixture is not public_listed -- its org shouldn't appear."""
        resp = client.get("/sitemap.xml")
        assert "/directory/default" not in resp.text

    def test_directory_slug_page_injects_org_specific_title_and_description(self, client, admin_headers):
        _public_net(client, admin_headers, name="Weekly Chat Net")
        resp = client.get("/directory/default")
        assert 'id="seo-title"' in resp.text
        assert "<title" in resp.text
        # "default" org's display name defaults to the branding org_name (or
        # "Default Organization") -- just confirm the placeholder default was
        # actually overwritten, not left as the generic fallback.
        assert "Net Directory — NetControl Online" not in resp.text
        assert 'id="seo-canonical"' in resp.text
        assert "/directory/default" in resp.text

    def test_directory_slug_page_includes_organization_jsonld(self, client, admin_headers):
        resp = client.get("/directory/default")
        assert 'application/ld+json' in resp.text
        assert '"@type": "Organization"' in resp.text

    def test_directory_bare_page_has_no_jsonld(self, client):
        resp = client.get("/directory")
        assert "application/ld+json" not in resp.text

    def test_directory_page_with_unknown_slug_falls_back_to_generic_meta(self, client):
        resp = client.get("/directory/does-not-exist")
        assert resp.status_code == 200
        assert "Net Directory — NetControl Online" in resp.text

    def test_live_slug_page_has_noindex_but_directory_does_not(self, client):
        live_resp = client.get("/live/default")
        dir_resp = client.get("/directory/default")
        assert 'name="robots" content="noindex, follow"' in live_resp.text
        assert 'name="robots" content="noindex' not in dir_resp.text
