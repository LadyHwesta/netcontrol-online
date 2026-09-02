"""
Tests for scheduled net reminders (send_reminders.py):
  Net.reminder_enabled / reminder_minutes_before round-trip via POST/PUT /nets
  send_due_reminders() window matching + idempotency
  Web push (issue follow-up): signup push alongside email, and activation
  Net Control rotation shift-change push alerts
"""

import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

sys.path.insert(0, ".")
from send_reminders import send_due_reminders  # noqa: E402

from models import NetControlShift, NetControlSignup  # noqa: E402


# A fixed "now" so reminder-window tests are deterministic regardless of when
# the suite actually runs.
NOW = datetime(2026, 8, 24, 19, 0, tzinfo=timezone.utc)


def _hhmm(dt):
    return dt.strftime("%H:%M")


def _reminder_net(client, headers, minutes_before=30, name="Reminder Net"):
    resp = client.post("/nets", json={
        "name": name, "is_ares": False,
        "reminder_enabled": True, "reminder_minutes_before": minutes_before,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _schedule_and_signup(client, headers, net_id, start_dt, email="op@example.com", role="net_control"):
    sched = client.post(f"/nets/{net_id}/schedules", json={
        "day_of_week": start_dt.weekday(), "start_time": _hhmm(start_dt), "timezone": "UTC",
    }, headers=headers)
    assert sched.status_code == 201, sched.text
    signup = client.post(f"/nets/{net_id}/signups", json={
        "schedule_id": sched.json()["id"], "slot_date": str(start_dt.date()), "role": role,
        "callsign": "W1AW", "name": "Alice", "email": email,
    }, headers=headers)
    assert signup.status_code == 201, signup.text
    return sched.json(), signup.json()


class TestNetReminderSettings:
    def test_create_net_with_reminder(self, client, admin_headers):
        net = _reminder_net(client, admin_headers, minutes_before=15)
        assert net["reminder_enabled"] is True
        assert net["reminder_minutes_before"] == 15

    def test_reminder_minutes_defaults_when_omitted(self, client, admin_headers):
        resp = client.post("/nets", json={"name": "X", "is_ares": False, "reminder_enabled": True}, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["reminder_minutes_before"] == 30

    def test_reminder_minutes_ignored_when_disabled(self, client, admin_headers):
        resp = client.post("/nets", json={
            "name": "X", "is_ares": False, "reminder_enabled": False, "reminder_minutes_before": 45,
        }, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["reminder_minutes_before"] is None

    def test_reminder_minutes_out_of_range_rejected(self, client, admin_headers):
        resp = client.post("/nets", json={
            "name": "X", "is_ares": False, "reminder_enabled": True, "reminder_minutes_before": 0,
        }, headers=admin_headers)
        assert resp.status_code == 422


class TestSendDueReminders:
    async def test_reminder_sent_within_window(self, client, admin_headers, db):
        net = _reminder_net(client, admin_headers, minutes_before=30)
        start = NOW + timedelta(minutes=10)   # inside the 30-minute window
        _schedule_and_signup(client, admin_headers, net["id"], start)

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 1

        row = (await db.execute(select(NetControlSignup).filter(NetControlSignup.net_id == net["id"]))).scalar_one_or_none()
        assert row.reminder_sent_at is not None

    async def test_no_reminder_before_window_opens(self, client, admin_headers, db):
        net = _reminder_net(client, admin_headers, minutes_before=5)
        start = NOW + timedelta(minutes=30)   # well outside a 5-minute window
        _schedule_and_signup(client, admin_headers, net["id"], start)

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 0

        row = (await db.execute(select(NetControlSignup).filter(NetControlSignup.net_id == net["id"]))).scalar_one_or_none()
        assert row.reminder_sent_at is None

    async def test_no_reminder_after_net_started(self, client, admin_headers, db):
        net = _reminder_net(client, admin_headers, minutes_before=30)
        start = NOW - timedelta(minutes=1)   # already started
        _schedule_and_signup(client, admin_headers, net["id"], start)

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 0

    async def test_reminder_not_sent_twice(self, client, admin_headers, db):
        net = _reminder_net(client, admin_headers, minutes_before=30)
        start = NOW + timedelta(minutes=10)
        _schedule_and_signup(client, admin_headers, net["id"], start)

        first = await send_due_reminders(db, now_utc=NOW)
        second = await send_due_reminders(db, now_utc=NOW + timedelta(minutes=2))
        assert first == 1
        assert second == 0

    async def test_no_reminder_when_disabled(self, client, admin_headers, db):
        net = client.post("/nets", json={"name": "No Reminders", "is_ares": False}, headers=admin_headers).json()
        start = NOW + timedelta(minutes=10)
        _schedule_and_signup(client, admin_headers, net["id"], start)

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 0

    async def test_no_reminder_without_email(self, client, admin_headers, db):
        net = _reminder_net(client, admin_headers, minutes_before=30)
        start = NOW + timedelta(minutes=10)
        _schedule_and_signup(client, admin_headers, net["id"], start, email=None)

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 0

    async def test_both_roles_remind_independently(self, client, admin_headers, db):
        net = client.post("/nets", json={
            "name": "Newsline Net", "is_ares": False, "has_broadcast": True,
            "reminder_enabled": True, "reminder_minutes_before": 30,
        }, headers=admin_headers).json()
        start = NOW + timedelta(minutes=10)
        sched, _ = _schedule_and_signup(client, admin_headers, net["id"], start, email="nc@example.com", role="net_control")
        client.post(f"/nets/{net['id']}/signups", json={
            "schedule_id": sched["id"], "slot_date": str(start.date()), "role": "broadcaster",
            "callsign": "K2ABC", "name": "Bob", "email": "bc@example.com",
        }, headers=admin_headers)

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 2


class TestSignupPushReminders:
    """Web push alongside email, for the exact same NetControlSignup
    reminder occasion above -- same due window, same reminder_sent_at
    gate, no separate config (issue follow-up)."""

    async def test_push_sent_alongside_email(self, client, admin_headers, db, vapid_configured, sent_pushes):
        net = _reminder_net(client, admin_headers, minutes_before=30)
        start = NOW + timedelta(minutes=10)
        client.post("/push/subscribe", json={
            "endpoint": "https://push.example.com/v1/signup-user",
            "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
        }, headers=admin_headers)
        _schedule_and_signup(client, admin_headers, net["id"], start, email="op@example.com")

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 2   # 1 email + 1 push
        assert len(sent_pushes) == 1

    async def test_push_only_when_no_email(self, client, admin_headers, db, vapid_configured, sent_pushes):
        net = _reminder_net(client, admin_headers, minutes_before=30)
        start = NOW + timedelta(minutes=10)
        client.post("/push/subscribe", json={
            "endpoint": "https://push.example.com/v1/push-only",
            "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
        }, headers=admin_headers)
        _schedule_and_signup(client, admin_headers, net["id"], start, email=None)

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 1   # push only -- self-signup always has user_id even with no email
        assert len(sent_pushes) == 1

    async def test_no_push_without_subscription(self, client, admin_headers, db, vapid_configured, sent_pushes):
        net = _reminder_net(client, admin_headers, minutes_before=30)
        start = NOW + timedelta(minutes=10)
        _schedule_and_signup(client, admin_headers, net["id"], start, email="op@example.com")

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 1   # email only -- no subscription to push to
        assert len(sent_pushes) == 0


def _ares_net(client, headers, name="ARES Reminder Net", minutes_before=10):
    resp = client.post("/nets", json={
        "name": name, "is_ares": True,
        "reminder_enabled": True, "reminder_minutes_before": minutes_before,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _activation_session(client, headers, net_id):
    resp = client.post(f"/nets/{net_id}/sessions", json={"is_activation": True}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _shift(client, headers, session_id, callsign, scheduled_start):
    resp = client.post(f"/sessions/{session_id}/net-control-shifts", json={
        "callsign": callsign, "scheduled_start": scheduled_start.isoformat(),
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestActivationShiftReminders:
    """Push-only alerts for an upcoming Net Control rotation shift during a
    live activation (issue follow-up) -- NetControlShift has always been
    free-text callsign/name (no user_id), so these are best-effort matched
    against a registered user's callsign at send time."""

    async def test_push_sent_to_matched_callsign(self, client, admin_headers, db, vapid_configured, sent_pushes):
        net = _ares_net(client, admin_headers, minutes_before=10)
        session = _activation_session(client, admin_headers, net["id"])
        client.post("/push/subscribe", json={
            "endpoint": "https://push.example.com/v1/shift-user",
            "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
        }, headers=admin_headers)
        # admin_headers' own callsign is W1ADMIN (see conftest's admin_token fixture)
        _shift(client, admin_headers, session["id"], "W1ADMIN", NOW + timedelta(minutes=5))

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 1
        assert len(sent_pushes) == 1

        shift = (await db.execute(select(NetControlShift).filter(NetControlShift.session_id == session["id"]))).scalar_one()
        assert shift.reminder_sent_at is not None

    async def test_unmatched_callsign_sets_reminder_sent_at_without_error(self, client, admin_headers, db, vapid_configured, sent_pushes):
        net = _ares_net(client, admin_headers, minutes_before=10)
        session = _activation_session(client, admin_headers, net["id"])
        _shift(client, admin_headers, session["id"], "N0NOBODY", NOW + timedelta(minutes=5))

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 0
        assert len(sent_pushes) == 0

        shift = (await db.execute(select(NetControlShift).filter(NetControlShift.session_id == session["id"]))).scalar_one()
        assert shift.reminder_sent_at is not None   # marked handled either way -- never rechecked forever

    async def test_no_alert_before_window_opens(self, client, admin_headers, db, vapid_configured, sent_pushes):
        net = _ares_net(client, admin_headers, minutes_before=5)
        session = _activation_session(client, admin_headers, net["id"])
        _shift(client, admin_headers, session["id"], "W1ADMIN", NOW + timedelta(minutes=30))

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 0

        shift = (await db.execute(select(NetControlShift).filter(NetControlShift.session_id == session["id"]))).scalar_one()
        assert shift.reminder_sent_at is None

    async def test_excluded_when_session_ended(self, client, admin_headers, db, vapid_configured, sent_pushes):
        net = _ares_net(client, admin_headers, minutes_before=10)
        session = _activation_session(client, admin_headers, net["id"])
        _shift(client, admin_headers, session["id"], "W1ADMIN", NOW + timedelta(minutes=5))
        client.patch(f"/sessions/{session['id']}/end", headers=admin_headers)

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 0

    async def test_excluded_when_reminder_disabled(self, client, admin_headers, db, vapid_configured, sent_pushes):
        net = client.post("/nets", json={"name": "No Reminder ARES", "is_ares": True}, headers=admin_headers).json()
        session = _activation_session(client, admin_headers, net["id"])
        _shift(client, admin_headers, session["id"], "W1ADMIN", NOW + timedelta(minutes=5))

        sent = await send_due_reminders(db, now_utc=NOW)
        assert sent == 0

    async def test_not_alerted_twice(self, client, admin_headers, db, vapid_configured, sent_pushes):
        net = _ares_net(client, admin_headers, minutes_before=10)
        session = _activation_session(client, admin_headers, net["id"])
        client.post("/push/subscribe", json={
            "endpoint": "https://push.example.com/v1/shift-user-2",
            "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
        }, headers=admin_headers)
        _shift(client, admin_headers, session["id"], "W1ADMIN", NOW + timedelta(minutes=5))

        first = await send_due_reminders(db, now_utc=NOW)
        second = await send_due_reminders(db, now_utc=NOW + timedelta(minutes=2))
        assert first == 1
        assert second == 0
