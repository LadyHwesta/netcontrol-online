"""
Tests for the per-account theme preference (issue #2):
  PATCH /auth/theme
  theme field on GET /auth/me, POST /auth/register, and POST /auth/login's nested user
"""
import pytest

from helpers import register


class TestThemeDefault:
    def test_new_user_defaults_to_lcars(self, client, user_headers):
        resp = client.get("/auth/me", headers=user_headers)
        assert resp.json()["theme"] == "lcars"

    def test_register_response_includes_default_theme(self, client):
        resp = client.post("/auth/register", json={
            "callsign": "W1NEW", "name": "Test", "email": "w1new@example.com", "password": "testpass123",
        })
        assert resp.json()["theme"] == "lcars"


class TestThemeUpdate:
    @pytest.mark.parametrize("theme", ["lcars", "dark", "light", "high-contrast", "pink", "purple", "blue", "matrix", "earth", "system"])
    def test_patch_each_valid_value(self, client, user_headers, theme):
        resp = client.patch("/auth/theme", json={"theme": theme}, headers=user_headers)
        assert resp.status_code == 200
        assert resp.json()["theme"] == theme

    def test_patch_invalid_value_rejected(self, client, user_headers):
        resp = client.patch("/auth/theme", json={"theme": "not-a-real-theme"}, headers=user_headers)
        assert resp.status_code == 422

    def test_patch_requires_auth(self, client):
        resp = client.patch("/auth/theme", json={"theme": "dark"})
        assert resp.status_code == 401

    def test_patch_persists(self, client, user_headers):
        client.patch("/auth/theme", json={"theme": "high-contrast"}, headers=user_headers)
        resp = client.get("/auth/me", headers=user_headers)
        assert resp.json()["theme"] == "high-contrast"

    def test_theme_present_in_login_response(self, client):
        register(client, "W1LOGIN", "Login Test", "w1login@example.com", "testpass123")
        resp = client.post("/auth/login", data={"username": "W1LOGIN", "password": "testpass123"})
        assert resp.json()["user"]["theme"] == "lcars"
