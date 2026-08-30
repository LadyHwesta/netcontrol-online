"""
Tests for pushing public nets to the central Net Repository directory
(issue #12): net_repository.push_net(), and its wiring into
POST /nets and PUT /nets/{id}.

NET_REPOSITORY_URL/NET_REPOSITORY_API_KEY are forced empty for the whole
suite (see conftest.py) so a real .env can't make these tests hit the live
service. The net_repo_configured fixture turns push_net "on" for tests that
need it, and pushed_nets intercepts httpx.post so no real network call is
made.
"""
import net_repository


class TestCreateNetPush:
    def test_no_push_when_not_configured(self, client, admin_headers, pushed_nets):
        client.post("/nets", json={"name": "Test Net", "public_listed": True}, headers=admin_headers)
        assert pushed_nets == []

    def test_no_push_when_not_public(self, client, admin_headers, net_repo_configured, pushed_nets):
        client.post("/nets", json={"name": "Private Net", "public_listed": False}, headers=admin_headers)
        assert pushed_nets == []

    def test_pushes_public_net_on_create(self, client, admin_headers, net_repo_configured, pushed_nets):
        resp = client.post("/nets", json={
            "name": "Monday Night Net", "frequency": "146.520 MHz", "description": "Weekly check-in",
            "net_type": "ham", "is_ares": False, "dmr_talkgroup": "3117", "public_listed": True,
        }, headers=admin_headers)
        net_id = resp.json()["id"]

        assert len(pushed_nets) == 1
        call = pushed_nets[0]
        assert call["url"] == "https://netrepo.example.com/nets/submit"
        assert call["headers"] == {"Authorization": "Bearer nr_testkey"}
        payload = call["json"]
        assert payload["name"] == "Monday Night Net"
        assert payload["frequency"] == "146.520 MHz"
        assert payload["description"] == "Weekly check-in"
        assert payload["net_type"] == "ham"
        assert payload["is_ares"] is False
        assert payload["dmr_talkgroup"] == "3117"
        assert payload["source_net_id"] == net_id
        assert payload["contact_callsign"] == "W1ADMIN"
        assert payload["submitted_by_callsign"] == "W1ADMIN"
        assert payload["schedules"] == []

    def test_pushes_optional_directory_metadata(self, client, admin_headers, net_repo_configured, pushed_nets):
        client.post("/nets", json={
            "name": "Metadata Net", "public_listed": True,
            "band": "2m", "mode": "FM", "ctcss_tone": "100.0",
            "region": "Snohomish County", "state": "WA", "website": "https://net.example.org",
        }, headers=admin_headers)

        payload = pushed_nets[0]["json"]
        assert payload["band"] == "2m"
        assert payload["mode"] == "FM"
        assert payload["ctcss_tone"] == "100.0"
        assert payload["region"] == "Snohomish County"
        assert payload["state"] == "WA"
        assert payload["website"] == "https://net.example.org"
        assert payload["country"] == "US"

    def test_directory_metadata_defaults_to_none(self, client, admin_headers, net_repo_configured, pushed_nets):
        """None of the new fields are required — a net that doesn't set them still pushes fine."""
        client.post("/nets", json={"name": "Bare Net", "public_listed": True}, headers=admin_headers)

        payload = pushed_nets[0]["json"]
        assert payload["band"] is None
        assert payload["mode"] is None
        assert payload["ctcss_tone"] is None
        assert payload["region"] is None
        assert payload["state"] is None
        assert payload["country"] == "US"

    def test_website_falls_back_to_branding(self, client, admin_headers, net_repo_configured, pushed_nets):
        client.put("/admin/branding", json={"website_url": "https://club.example.org"}, headers=admin_headers)
        client.post("/nets", json={"name": "No Website Net", "public_listed": True}, headers=admin_headers)

        assert pushed_nets[0]["json"]["website"] == "https://club.example.org"

    def test_net_website_takes_precedence_over_branding(self, client, admin_headers, net_repo_configured, pushed_nets):
        client.put("/admin/branding", json={"website_url": "https://club.example.org"}, headers=admin_headers)
        client.post("/nets", json={
            "name": "Own Website Net", "public_listed": True, "website": "https://net-specific.example.org",
        }, headers=admin_headers)

        assert pushed_nets[0]["json"]["website"] == "https://net-specific.example.org"


class TestUpdateNetPush:
    def test_pushes_when_toggled_public(self, client, admin_headers, net_repo_configured, pushed_nets):
        resp = client.post("/nets", json={"name": "Test Net", "public_listed": False}, headers=admin_headers)
        net_id = resp.json()["id"]
        assert pushed_nets == []  # not pushed while private

        client.put(f"/nets/{net_id}", json={"name": "Test Net", "public_listed": True}, headers=admin_headers)
        assert len(pushed_nets) == 1
        assert pushed_nets[0]["json"]["source_net_id"] == net_id

    def test_no_push_when_staying_private(self, client, admin_headers, net_repo_configured, pushed_nets):
        resp = client.post("/nets", json={"name": "Test Net", "public_listed": False}, headers=admin_headers)
        net_id = resp.json()["id"]

        client.put(f"/nets/{net_id}", json={"name": "Renamed Net", "public_listed": False}, headers=admin_headers)
        assert pushed_nets == []

    def test_edit_while_staying_public_pushes_again(self, client, admin_headers, net_repo_configured, pushed_nets):
        """Net Repository now applies a re-submission as an update to the existing
        listing, so every edit to an already-public net should push again — not
        just the initial create or the moment public_listed flips on."""
        resp = client.post("/nets", json={
            "name": "Test Net", "description": "Original", "public_listed": True,
        }, headers=admin_headers)
        net_id = resp.json()["id"]
        assert len(pushed_nets) == 1

        client.put(f"/nets/{net_id}", json={
            "name": "Test Net", "description": "Updated", "public_listed": True,
        }, headers=admin_headers)
        assert len(pushed_nets) == 2
        assert pushed_nets[1]["json"]["description"] == "Updated"
        assert pushed_nets[1]["json"]["source_net_id"] == net_id


class TestPushNetFunction:
    def test_push_fails_gracefully_on_http_error(self, client, admin_headers, net_repo_configured, monkeypatch):
        """A Net Repository outage must not break creating a net locally."""
        import httpx

        def fake_post(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", fake_post)

        resp = client.post("/nets", json={"name": "Test Net", "public_listed": True}, headers=admin_headers)
        assert resp.status_code == 201  # net creation still succeeds

    async def test_not_configured_returns_false(self, db):
        assert await net_repository.net_repository_configured(db) is False


class TestSessionStatsPush:
    """Ending a session on a public-listed net logs its stats to Net
    Repository via POST /nets/stats (matched by source_net_id, same as
    push_net()) — session count/avg check-ins/last-session-date roll up
    into that net's directory listing there."""

    def test_pushes_stats_on_session_end(self, client, admin_headers, net_repo_configured, pushed_nets_and_stats):
        net_resp = client.post("/nets", json={"name": "Stats Net", "public_listed": True}, headers=admin_headers)
        net_id = net_resp.json()["id"]
        session_resp = client.post(f"/nets/{net_id}/sessions", json={}, headers=admin_headers)
        session_id = session_resp.json()["id"]
        client.post(f"/sessions/{session_id}/checkins", json={"callsign": "W1AAA"}, headers=admin_headers)
        client.post(f"/sessions/{session_id}/checkins", json={"callsign": "W2BBB"}, headers=admin_headers)

        resp = client.patch(f"/sessions/{session_id}/end", headers=admin_headers)
        assert resp.status_code == 200

        stats_calls = [c for c in pushed_nets_and_stats if c["url"].endswith("/nets/stats")]
        assert len(stats_calls) == 1
        payload = stats_calls[0]["json"]
        assert payload["source_net_id"] == net_id
        assert payload["checkin_count"] == 2
        assert "session_date" in payload
        assert stats_calls[0]["headers"] == {"Authorization": "Bearer nr_testkey"}

    def test_no_stats_push_when_not_configured(self, client, admin_headers, pushed_nets_and_stats):
        net_resp = client.post("/nets", json={"name": "Stats Net", "public_listed": True}, headers=admin_headers)
        net_id = net_resp.json()["id"]
        session_resp = client.post(f"/nets/{net_id}/sessions", json={}, headers=admin_headers)
        client.patch(f"/sessions/{session_resp.json()['id']}/end", headers=admin_headers)
        assert pushed_nets_and_stats == []

    def test_no_stats_push_when_net_not_public(self, client, admin_headers, net_repo_configured, pushed_nets_and_stats):
        net_resp = client.post("/nets", json={"name": "Private Net", "public_listed": False}, headers=admin_headers)
        net_id = net_resp.json()["id"]
        session_resp = client.post(f"/nets/{net_id}/sessions", json={}, headers=admin_headers)
        client.patch(f"/sessions/{session_resp.json()['id']}/end", headers=admin_headers)
        assert pushed_nets_and_stats == []

    def test_session_end_succeeds_even_when_net_not_yet_published(
        self, client, admin_headers, net_repo_configured, pushed_nets_and_stats,
    ):
        """A 404 from Net Repository (net not approved/published there yet)
        must not disrupt ending the session locally."""
        net_resp = client.post("/nets", json={"name": "Stats Net", "public_listed": True}, headers=admin_headers)
        net_id = net_resp.json()["id"]
        session_resp = client.post(f"/nets/{net_id}/sessions", json={}, headers=admin_headers)
        session_id = session_resp.json()["id"]

        pushed_nets_and_stats.set_status(404)
        resp = client.patch(f"/sessions/{session_id}/end", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["checkin_count"] == 0

    def test_stats_push_fails_gracefully_on_http_error(self, client, admin_headers, net_repo_configured, monkeypatch):
        """A Net Repository outage must not break ending a session locally."""
        import httpx

        net_resp = client.post("/nets", json={"name": "Stats Net", "public_listed": True}, headers=admin_headers)
        net_id = net_resp.json()["id"]
        session_resp = client.post(f"/nets/{net_id}/sessions", json={}, headers=admin_headers)
        session_id = session_resp.json()["id"]

        def fake_post(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", fake_post)
        resp = client.patch(f"/sessions/{session_id}/end", headers=admin_headers)
        assert resp.status_code == 200


class TestAdminStatusEndpoint:
    def test_status_when_nothing_configured(self, client, admin_headers):
        resp = client.get("/admin/net-repository/status", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "url_configured": False, "has_key": False, "key_source": None, "request_status": "none",
        }

    def test_status_reflects_env_key(self, client, admin_headers, net_repo_configured):
        resp = client.get("/admin/net-repository/status", headers=admin_headers)
        data = resp.json()
        assert data["url_configured"] is True
        assert data["has_key"] is True
        assert data["key_source"] == "env"

    def test_status_requires_admin(self, client, user_headers):
        resp = client.get("/admin/net-repository/status", headers=user_headers)
        assert resp.status_code == 403


class TestRequestKeyEndpoint:
    def test_request_key_sends_correct_payload(self, client, admin_headers, net_repo_url_only, monkeypatch):
        calls = []

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"request_id": 7, "claim_token": "nrc_abc123", "message": "Store this claim token securely."}

        def fake_post(url, json=None, timeout=None):
            calls.append({"url": url, "json": json})
            return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "post", fake_post)

        resp = client.post("/admin/net-repository/request-key", json={
            "name": "My NetControl Online Instance",
            "contact_callsign": "W7XYZ",
            "instance_url": "https://mytracker.example.com",
            "request_notes": "Please approve!",
        }, headers=admin_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["request_id"] == 7

        assert calls[0]["url"] == "https://netrepo.example.com/keys/request"
        assert calls[0]["json"]["name"] == "My NetControl Online Instance"
        assert calls[0]["json"]["contact_callsign"] == "W7XYZ"

        status_resp = client.get("/admin/net-repository/status", headers=admin_headers)
        assert status_resp.json()["request_status"] == "pending"

    def test_request_key_requires_url_configured(self, client, admin_headers):
        resp = client.post("/admin/net-repository/request-key", json={"name": "Test"}, headers=admin_headers)
        data = resp.json()
        assert data["ok"] is False

    def test_request_key_requires_admin(self, client, user_headers, net_repo_url_only):
        resp = client.post("/admin/net-repository/request-key", json={"name": "Test"}, headers=user_headers)
        assert resp.status_code == 403


class TestCheckStatusEndpoint:
    def test_no_pending_request(self, client, admin_headers, net_repo_url_only):
        resp = client.post("/admin/net-repository/check-status", headers=admin_headers)
        data = resp.json()
        assert data == {
            "ok": True, "status": "none", "message": "No pending request.", "request_id": None, "api_key": None,
        }

    async def test_still_pending(self, client, admin_headers, net_repo_url_only, monkeypatch, db):
        await net_repository._set_setting(net_repository._SETTING_CLAIM_TOKEN, "nrc_abc123", db)
        await db.commit()

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "pending", "message": "Your request is awaiting admin review."}

        import httpx
        monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeResponse())

        resp = client.post("/admin/net-repository/check-status", headers=admin_headers)
        data = resp.json()
        assert data["status"] == "pending"

        # Claim token must still be on file -- a pending request needs to be pollable again.
        status_resp = client.get("/admin/net-repository/status", headers=admin_headers)
        assert status_resp.json()["request_status"] == "pending"

    async def test_approved_and_claimed_stores_key(self, client, admin_headers, net_repo_url_only, monkeypatch, db):
        await net_repository._set_setting(net_repository._SETTING_CLAIM_TOKEN, "nrc_abc123", db)
        await db.commit()

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "claimed", "message": "Here you go.", "api_key": "nr_freshlyissuedkey"}

        import httpx
        monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeResponse())

        resp = client.post("/admin/net-repository/check-status", headers=admin_headers)
        data = resp.json()
        assert data["status"] == "claimed"
        # The whole point of the fix: the admin needs a chance to see/copy the
        # key, exactly like any other API key/token in this app -- it's only
        # ever handed back on this one response, the poll that claims it fresh.
        assert data["api_key"] == "nr_freshlyissuedkey"

        # A second check-status call (as if the admin clicked the button again)
        # must not still be handing out the key -- Net Repository itself only
        # returns it once, and there's nothing left locally to re-reveal either.
        resp2 = client.post("/admin/net-repository/check-status", headers=admin_headers)
        assert resp2.json().get("api_key") is None

        status_resp = client.get("/admin/net-repository/status", headers=admin_headers)
        status_data = status_resp.json()
        assert status_data["has_key"] is True
        assert status_data["key_source"] == "self-service"
        assert status_data["request_status"] == "claimed"

        # The general status endpoint never exposes the key, unlike the
        # one-time check-status response above.
        assert "api_key" not in status_data
        assert "nr_freshlyissuedkey" not in str(status_data)

    async def test_rejected_clears_claim_token(self, client, admin_headers, net_repo_url_only, monkeypatch, db):
        await net_repository._set_setting(net_repository._SETTING_CLAIM_TOKEN, "nrc_abc123", db)
        await db.commit()

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "rejected", "message": "Not approved.", "notes": "Try again later."}

        import httpx
        monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeResponse())

        resp = client.post("/admin/net-repository/check-status", headers=admin_headers)
        assert resp.json()["status"] == "rejected"

        status_resp = client.get("/admin/net-repository/status", headers=admin_headers)
        assert status_resp.json()["request_status"] == "rejected"

    async def test_key_actually_usable_after_claim(self, client, admin_headers, net_repo_url_only, monkeypatch, db, pushed_nets):
        """The whole point: once claimed, pushes start working without a restart."""
        await net_repository._set_setting(net_repository._SETTING_CLAIM_TOKEN, "nrc_abc123", db)
        await db.commit()

        class FakeStatusResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "claimed", "message": "Here you go.", "api_key": "nr_freshlyissuedkey"}

        import httpx
        monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeStatusResponse())
        client.post("/admin/net-repository/check-status", headers=admin_headers)

        client.post("/nets", json={"name": "Test Net", "public_listed": True}, headers=admin_headers)
        assert len(pushed_nets) == 1
        assert pushed_nets[0]["headers"] == {"Authorization": "Bearer nr_freshlyissuedkey"}


class TestClearKeyEndpoint:
    def test_clear_key_requires_admin(self, client, user_headers):
        resp = client.delete("/admin/net-repository/key", headers=user_headers)
        assert resp.status_code == 403

    async def test_clear_key_resets_status(self, client, admin_headers, net_repo_url_only, monkeypatch, db):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "claimed", "message": "Here you go.", "api_key": "nr_freshlyissuedkey"}

        import httpx
        await net_repository._set_setting(net_repository._SETTING_CLAIM_TOKEN, "nrc_abc123", db)
        await db.commit()
        monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeResponse())
        client.post("/admin/net-repository/check-status", headers=admin_headers)

        resp = client.delete("/admin/net-repository/key", headers=admin_headers)
        assert resp.status_code == 204

        status_resp = client.get("/admin/net-repository/status", headers=admin_headers)
        data = status_resp.json()
        assert data["has_key"] is False
        assert data["request_status"] == "none"
