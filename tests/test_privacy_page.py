"""
Test for GET /privacy -- the one thing worth locking in here is that it
stays reachable with no auth at all, since it's specifically linked from
the login/register screen for a visitor who hasn't created an account yet.
"""


def test_privacy_page_is_public(client):
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert "Privacy Policy" in resp.text
