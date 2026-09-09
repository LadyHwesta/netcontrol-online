"""
Tests for the instance-wide branding logo endpoints (routers/orgs.py):
  POST/DELETE /admin/branding/logo
  GET  /logo

The org-scoped counterparts (POST/DELETE /orgs/{id}/logo, GET /orgs/{id}/logo)
already have coverage in tests/test_organizations.py's TestOrgLogo, following
the exact same pattern -- this closes the gap TECH_DEBT.md called out for
these instance-wide ones (2026-09-08).
"""

import io


def _logo_file(filename="logo.png"):
    return {"file": (filename, io.BytesIO(b"not a real image, just bytes"), "image/png")}


def test_admin_can_upload_and_fetch_logo(client, admin_headers):
    resp = client.post("/admin/branding/logo", files=_logo_file(), headers=admin_headers)
    assert resp.status_code == 204, resp.text

    # Public -- no auth required to fetch it back
    img = client.get("/logo")
    assert img.status_code == 200
    assert img.content == b"not a real image, just bytes"
    assert img.headers["content-type"] == "image/png"

    assert client.get("/branding").json()["has_logo"] is True


def test_uploading_new_logo_replaces_old_one(client, admin_headers):
    client.post("/admin/branding/logo", files=_logo_file("first.png"), headers=admin_headers)
    client.post("/admin/branding/logo", files=_logo_file("second.jpg"), headers=admin_headers)

    img = client.get("/logo")
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/jpeg"  # the second (replacing) upload


def test_admin_can_delete_logo(client, admin_headers):
    client.post("/admin/branding/logo", files=_logo_file(), headers=admin_headers)

    resp = client.delete("/admin/branding/logo", headers=admin_headers)
    assert resp.status_code == 204

    assert client.get("/logo").status_code == 404
    assert client.get("/branding").json()["has_logo"] is False


def test_logo_fetch_404s_with_none_uploaded(client):
    resp = client.get("/logo")
    assert resp.status_code == 404


def test_rejects_unsupported_file_type(client, admin_headers):
    resp = client.post("/admin/branding/logo",
        files={"file": ("logo.exe", io.BytesIO(b"nope"), "application/octet-stream")},
        headers=admin_headers)
    assert resp.status_code == 400


def test_non_admin_cannot_upload_logo(client, user_headers):
    resp = client.post("/admin/branding/logo", files=_logo_file(), headers=user_headers)
    assert resp.status_code == 403


def test_non_admin_cannot_delete_logo(client, admin_headers, user_headers):
    client.post("/admin/branding/logo", files=_logo_file(), headers=admin_headers)

    resp = client.delete("/admin/branding/logo", headers=user_headers)
    assert resp.status_code == 403

    # Confirm it's still there -- the rejected delete had no effect
    assert client.get("/logo").status_code == 200
