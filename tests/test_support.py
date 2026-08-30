"""
Tests for the in-app support/report ticket endpoint:
  POST /support/ticket

Also covers the send_email() consolidation this touched: create_support_ticket
used to hand-roll its own SMTP send (duplicating send_email's logic) just to set
a Reply-To header; it now calls send_email(reply_to=...) instead.

This endpoint (and its SUPPORT_EMAIL setting) now lives in routers/support.py,
which calls routers/helpers.py's send_email() via qualified module access
(`helpers.send_email(...)`) specifically so a single monkeypatch on
routers.helpers.send_email is observed here regardless of which router the
call originates from -- see the sent_emails fixture in conftest.py.
"""
from routers import support
from helpers import register, login, auth


class TestSupportTicket:
    def test_503_when_smtp_not_configured(self, client, admin_headers):
        resp = client.post("/support/ticket", json={
            "type": "Bug Report", "subject": "Something broke", "body": "Details here",
        }, headers=admin_headers)
        assert resp.status_code == 503

    def test_503_when_support_email_not_set(self, client, admin_headers, smtp_configured, monkeypatch):
        monkeypatch.setattr(support, "SUPPORT_EMAIL", "")
        resp = client.post("/support/ticket", json={
            "type": "Bug Report", "subject": "Something broke", "body": "Details here",
        }, headers=admin_headers)
        assert resp.status_code == 503

    def test_400_on_blank_subject_or_body(self, client, admin_headers, smtp_configured, sent_emails, monkeypatch):
        monkeypatch.setattr(support, "SUPPORT_EMAIL", "support@example.com")
        resp = client.post("/support/ticket", json={
            "type": "Bug Report", "subject": "   ", "body": "Details here",
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_sends_with_reply_to_the_reporting_user(self, client, admin_headers, smtp_configured, sent_emails, monkeypatch):
        monkeypatch.setattr(support, "SUPPORT_EMAIL", "support@example.com")
        resp = client.post("/support/ticket", json={
            "type": "Enhancement Request", "subject": "Add dark mode", "body": "Please add it",
        }, headers=admin_headers)
        assert resp.status_code == 204

        assert len(sent_emails) == 1
        call = sent_emails[0]
        assert call["to"] == ["support@example.com"]
        assert "Add dark mode" in call["subject"]
        assert "W1ADMIN" in call["reply_to"] or "@" in call["reply_to"]

    def test_500_when_send_fails(self, client, admin_headers, smtp_configured, monkeypatch):
        monkeypatch.setattr(support, "SUPPORT_EMAIL", "support@example.com")
        monkeypatch.setattr(support.helpers, "send_email", lambda **kwargs: False)
        resp = client.post("/support/ticket", json={
            "type": "Bug Report", "subject": "Subject", "body": "Body",
        }, headers=admin_headers)
        assert resp.status_code == 500

    def test_requires_auth(self, client, smtp_configured, monkeypatch):
        monkeypatch.setattr(support, "SUPPORT_EMAIL", "support@example.com")
        resp = client.post("/support/ticket", json={
            "type": "Bug Report", "subject": "Subject", "body": "Body",
        })
        assert resp.status_code == 401
