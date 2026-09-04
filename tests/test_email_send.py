"""
Tests for send_email()'s low-level SMTP mechanics (as opposed to the
higher-level flows in test_email_verification.py / test_reminders.py, which
monkeypatch send_email() itself and never touch smtplib).

send_email() and its SMTP config live in routers/helpers.py (shared by
every router that sends email) -- not main.py -- since the main.py ->
routers/ split, so this module patches routers.helpers directly rather
than main.
"""

from email import message_from_string

import pytest

from routers import helpers


class TestSMTPTimeout:
    """send_email() previously called smtplib.SMTP()/SMTP_SSL() with no
    timeout at all -- a dead/unreachable SMTP host would hang the request
    that triggered it (e.g. registration) indefinitely instead of failing
    fast (issue #1 follow-up)."""

    def test_smtp_call_passes_a_timeout(self, monkeypatch, smtp_configured):
        captured = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                captured["host"], captured["port"], captured["timeout"] = host, port, timeout

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                pass

            def login(self, user, password):
                pass

            def sendmail(self, from_addr, to, msg):
                pass

        monkeypatch.setattr(helpers.smtplib, "SMTP", FakeSMTP)
        ok = helpers.send_email(to=["x@example.com"], subject="Test", body_html="<p>hi</p>")
        assert ok is True
        assert captured["timeout"] == helpers.SMTP_TIMEOUT_SECONDS
        assert captured["timeout"] is not None

    def test_smtp_ssl_call_passes_a_timeout(self, monkeypatch, smtp_configured):
        captured = {}

        class FakeSMTPSSL:
            def __init__(self, host, port, timeout=None):
                captured["timeout"] = timeout

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, user, password):
                pass

            def sendmail(self, from_addr, to, msg):
                pass

        monkeypatch.setattr(helpers, "SMTP_USE_SSL", True)
        monkeypatch.setattr(helpers.smtplib, "SMTP_SSL", FakeSMTPSSL)
        ok = helpers.send_email(to=["x@example.com"], subject="Test", body_html="<p>hi</p>")
        assert ok is True
        assert captured["timeout"] == helpers.SMTP_TIMEOUT_SECONDS
        assert captured["timeout"] is not None


class TestMessageHeaders:
    """send_email() previously set only Subject/From/To -- no Message-ID or
    Date, both of which smtplib/the email package never add on their own,
    and whose absence is itself a spam signal independent of SPF/DKIM/DMARC
    (issue follow-up, found investigating a real deliverability report)."""

    @pytest.fixture
    def captured_message(self, monkeypatch):
        """Patches smtplib.SMTP so send_email() runs its full message-
        building path and hands back the actual built email.message.Message
        (parsed back from the wire string sendmail() would have sent) for
        header assertions, plus the raw envelope from_addr sendmail() got."""
        captured = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                pass

            def login(self, user, password):
                pass

            def sendmail(self, from_addr, to, msg):
                captured["envelope_from"] = from_addr
                captured["message"] = message_from_string(msg)

        monkeypatch.setattr(helpers.smtplib, "SMTP", FakeSMTP)
        return captured

    def test_message_id_and_date_are_set(self, monkeypatch, smtp_configured, captured_message):
        ok = helpers.send_email(to=["x@example.com"], subject="Test", body_html="<p>hi</p>")
        assert ok is True
        msg = captured_message["message"]
        assert msg["Message-ID"], "Message-ID header must be set"
        assert msg["Message-ID"].startswith("<") and msg["Message-ID"].endswith(">")
        assert msg["Date"], "Date header must be set"

    def test_bare_smtp_from_used_as_is(self, monkeypatch, smtp_configured, captured_message):
        monkeypatch.setattr(helpers, "SMTP_FROM", "noreply@example.com")
        helpers.send_email(to=["x@example.com"], subject="Test", body_html="<p>hi</p>")
        assert captured_message["message"]["From"] == "noreply@example.com"
        assert captured_message["envelope_from"] == "noreply@example.com"

    def test_display_name_smtp_from_kept_in_header_but_stripped_from_envelope(self, monkeypatch, smtp_configured, captured_message):
        # This is the actual bug: a "Name <addr>" SMTP_FROM (which the
        # setting's own .env.example comment documents as valid) used to be
        # passed to sendmail()'s envelope-sender argument unchanged --
        # malformed for the raw SMTP MAIL FROM command, which only ever
        # accepts a bare address.
        monkeypatch.setattr(helpers, "SMTP_FROM", "NetControl Online <noreply@example.com>")
        helpers.send_email(to=["x@example.com"], subject="Test", body_html="<p>hi</p>")
        assert captured_message["message"]["From"] == "NetControl Online <noreply@example.com>"
        assert captured_message["envelope_from"] == "noreply@example.com"

    def test_message_id_domain_matches_from_address(self, monkeypatch, smtp_configured, captured_message):
        monkeypatch.setattr(helpers, "SMTP_FROM", "NetControl Online <noreply@example.com>")
        helpers.send_email(to=["x@example.com"], subject="Test", body_html="<p>hi</p>")
        assert captured_message["message"]["Message-ID"].endswith("@example.com>")
