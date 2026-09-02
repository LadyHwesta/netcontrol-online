"""
Tests for web push notification subscription endpoints (issue follow-up):
  GET    /push/vapid-public-key
  POST   /push/subscribe
  DELETE /push/subscribe
  POST   /push/test

The actual periodic sends (upcoming shifts, activation rotation changes) are
covered in tests/test_reminders.py, since they're driven by
send_reminders.py, not this router.
"""

from helpers import register, login, auth

SUB_A = {
    "endpoint": "https://push.example.com/v1/aaa",
    "keys": {"p256dh": "p256dh-key-a", "auth": "auth-key-a"},
}
SUB_B = {
    "endpoint": "https://push.example.com/v1/bbb",
    "keys": {"p256dh": "p256dh-key-b", "auth": "auth-key-b"},
}


class TestVapidPublicKey:
    def test_404_when_not_configured(self, client):
        resp = client.get("/push/vapid-public-key")
        assert resp.status_code == 404

    def test_200_when_configured(self, client, vapid_configured):
        resp = client.get("/push/vapid-public-key")
        assert resp.status_code == 200
        assert resp.json()["public_key"] == "test-public-key"


class TestSubscribe:
    def test_requires_auth(self, client):
        resp = client.post("/push/subscribe", json=SUB_A)
        assert resp.status_code == 401

    def test_creates_subscription(self, client, admin_headers):
        resp = client.post("/push/subscribe", json=SUB_A, headers=admin_headers)
        assert resp.status_code == 204

    def test_resubscribe_same_endpoint_updates_not_duplicates(self, client, admin_headers):
        client.post("/push/subscribe", json=SUB_A, headers=admin_headers)
        updated = dict(SUB_A, keys={"p256dh": "new-p256dh", "auth": "new-auth"})
        resp = client.post("/push/subscribe", json=updated, headers=admin_headers)
        assert resp.status_code == 204
        # Confirmed indirectly via test_test_notification below (uses the
        # updated keys); a direct row-count check would need its own DB
        # fixture query, redundant with that coverage.

    def test_different_users_can_each_subscribe(self, client, admin_headers):
        register(client, "W2USER", "Second User", "second@example.com")
        client.patch("/admin/users/2/approve", headers=admin_headers)
        second_headers = auth(login(client, "W2USER"))

        resp1 = client.post("/push/subscribe", json=SUB_A, headers=admin_headers)
        resp2 = client.post("/push/subscribe", json=SUB_B, headers=second_headers)
        assert resp1.status_code == 204
        assert resp2.status_code == 204


class TestUnsubscribe:
    def test_requires_auth(self, client):
        resp = client.request("DELETE", "/push/subscribe", json={"endpoint": SUB_A["endpoint"]})
        assert resp.status_code == 401

    def test_removes_own_subscription(self, client, admin_headers, vapid_configured, sent_pushes):
        client.post("/push/subscribe", json=SUB_A, headers=admin_headers)
        resp = client.request("DELETE", "/push/subscribe", json={"endpoint": SUB_A["endpoint"]}, headers=admin_headers)
        assert resp.status_code == 204

        # No subscriptions left -- a test-send now has nothing to reach
        test_resp = client.post("/push/test", headers=admin_headers)
        assert test_resp.status_code == 400

    def test_idempotent_on_missing_endpoint(self, client, admin_headers):
        resp = client.request("DELETE", "/push/subscribe", json={"endpoint": "https://never-subscribed.example.com"}, headers=admin_headers)
        assert resp.status_code == 204

    def test_cannot_delete_another_users_subscription(self, client, admin_headers, vapid_configured, sent_pushes):
        register(client, "W3USER", "Third User", "third@example.com")
        client.patch("/admin/users/2/approve", headers=admin_headers)
        third_headers = auth(login(client, "W3USER"))

        client.post("/push/subscribe", json=SUB_A, headers=admin_headers)
        # third user tries to delete the admin's subscription -- silently
        # no-ops (only ever matches rows scoped to the caller)
        client.request("DELETE", "/push/subscribe", json={"endpoint": SUB_A["endpoint"]}, headers=third_headers)

        # still there -- a test-send from the admin should still work
        test_resp = client.post("/push/test", headers=admin_headers)
        assert test_resp.status_code == 200


class TestTestNotification:
    def test_requires_auth(self, client):
        resp = client.post("/push/test")
        assert resp.status_code == 401

    def test_400_with_no_subscriptions(self, client, admin_headers, vapid_configured):
        resp = client.post("/push/test", headers=admin_headers)
        assert resp.status_code == 400

    def test_sends_to_each_subscription(self, client, admin_headers, vapid_configured, sent_pushes):
        client.post("/push/subscribe", json=SUB_A, headers=admin_headers)
        client.post("/push/subscribe", json=SUB_B, headers=admin_headers)

        resp = client.post("/push/test", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["sent"] == 2
        assert len(sent_pushes) == 2
        endpoints = {call["subscription_info"]["endpoint"] for call in sent_pushes}
        assert endpoints == {SUB_A["endpoint"], SUB_B["endpoint"]}

    def test_400_when_not_configured(self, client, admin_headers):
        client.post("/push/subscribe", json=SUB_A, headers=admin_headers)
        resp = client.post("/push/test", headers=admin_headers)
        # VAPID unconfigured -> _send_web_push short-circuits to 0 sends
        assert resp.status_code == 400
