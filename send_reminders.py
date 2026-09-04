#!/usr/bin/env python3
"""
Scheduled Net Reminders
========================
Reminds whoever is signed up as Net Control / Broadcaster for a net a
configurable number of minutes before their session starts -- by email, and
(issue follow-up) by web push if they've enabled it. Also push-alerts
whoever's up next in an ACTIVE activation's Net Control rotation queue that
their shift is starting soon (push-only -- there's no signup email address
to reach for that, and no existing email path for it; see
send_due_reminders()'s second half). Intended to be run frequently from
cron -- each signup/shift is only reminded once, tracked via
NetControlSignup.reminder_sent_at / NetControlShift.reminder_sent_at, so
re-running on a short interval is safe.

Usage
-----
    python3 send_reminders.py

Cron (every 5 minutes):
    */5 * * * *  /opt/netcontrol/venv/bin/python3 /opt/netcontrol/send_reminders.py \\
                 >> /var/log/nettracker/reminders.log 2>&1

Environment variables (read from .env)
---------------------------------------
    DATABASE_URL   PostgreSQL connection string (required)
    SMTP_*         Same SMTP settings the app uses for email — see .env.example.
                   Reminders are silently skipped if SMTP isn't configured.
    VAPID_*        Same Web Push settings the app uses — see .env.example.
                   Push is silently skipped if these aren't configured.
"""

import asyncio
import json
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select

# ── Bootstrap ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("python-dotenv not found — activate the virtualenv first.")

load_dotenv(os.path.join(_HERE, ".env"))

if not os.getenv("DATABASE_URL"):
    sys.exit("DATABASE_URL not set in .env")

from database import SessionLocal  # noqa: E402
from models import Net, NetControlShift, NetControlSignup, NetSchedule, NetSession, PushSubscription, User  # noqa: E402

SMTP_HOST     = os.getenv("SMTP_HOST", "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", "")
SMTP_USE_TLS  = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
SMTP_USE_SSL  = os.getenv("SMTP_USE_SSL", "false").lower() == "true"

VAPID_PUBLIC_KEY    = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY   = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CONTACT_EMAIL = os.getenv("VAPID_CONTACT_EMAIL", "")

ROLE_LABELS = {"net_control": "Net Control", "broadcaster": "Broadcaster", "both": "Net Control & Broadcaster"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def vapid_configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_CONTACT_EMAIL)


def send_email(to: str, subject: str, body_html: str, body_text: str) -> None:
    """Minimal standalone mailer — mirrors routers/helpers.py's
    send_email() (issue follow-up: including its From/Message-ID/Date
    fix, see that function's own comments for why) without needing to
    import the whole FastAPI app just to send a reminder."""
    if not smtp_configured():
        log(f"SMTP not configured — skipping: {subject}")
        return
    from_display, from_addr = parseaddr(SMTP_FROM) if SMTP_FROM else ("", "")
    if not from_addr:
        from_addr = SMTP_USER
    from_header = formataddr((from_display, from_addr)) if from_display else from_addr
    msg_id_domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else None

    msg = MIMEMultipart("alternative")
    msg["Subject"]    = subject
    msg["From"]       = from_header
    msg["To"]         = to
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=msg_id_domain)
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))
    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as srv:
                srv.login(SMTP_USER, SMTP_PASSWORD)
                srv.sendmail(from_addr, [to], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
                if SMTP_USE_TLS:
                    srv.starttls()
                srv.login(SMTP_USER, SMTP_PASSWORD)
                srv.sendmail(from_addr, [to], msg.as_string())
        log(f"Reminder sent to {to} — {subject}")
    except Exception as exc:
        log(f"Failed to send reminder to {to}: {exc}")


async def send_web_push(db, user_id: int, title: str, body: str, url: str = "/") -> int:
    """Own local copy of routers/helpers.py's _send_web_push -- this script
    deliberately never imports anything under routers/, matching
    send_email() above's existing standalone-mailer precedent (see its own
    docstring). Sends to every subscription this user has (one per
    browser/device); a subscription pywebpush reports as gone (404/410) is
    deleted rather than logged as a failure. Never raises. Returns how many
    sends actually succeeded."""
    if not vapid_configured():
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log("VAPID_* is set but the pywebpush package isn't installed — pip install pywebpush")
        return 0

    subs = (await db.execute(select(PushSubscription).filter(PushSubscription.user_id == user_id))).scalars().all()
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{VAPID_CONTACT_EMAIL}"},
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                # Expected cleanup, not a failure -- still logged (see
                # routers/helpers.py's _send_web_push, this function's own
                # twin, for why this used to be silent and shouldn't be).
                log(f"Push subscription for user {user_id} reported gone (HTTP {status}) -- removing it: {exc}")
                await db.delete(sub)
            else:
                log(f"Push send failed for user {user_id}: {exc}")
        except Exception as exc:
            log(f"Push send failed for user {user_id}: {exc}")
    return sent


def occurrence_start_utc(schedule: NetSchedule, occ_date: date):
    """The schedule's start_time/timezone on occ_date, converted to UTC.
    Returns None if the schedule has malformed time/timezone data."""
    try:
        tz = ZoneInfo(schedule.timezone or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    try:
        h, m = map(int, schedule.start_time.split(":"))
    except (ValueError, AttributeError):
        return None
    naive = datetime(occ_date.year, occ_date.month, occ_date.day, h, m)
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


# ── Core logic (importable for tests) ───────────────────────────────────────

async def send_due_reminders(db, now_utc: datetime = None) -> int:
    """Sends due reminders on two independent occasions, combined into one
    return count (email + push together -- see each section below for why
    that's safe): (1) any NetControlSignup whose reminder window has opened
    and hasn't been reminded yet, by email (if it has one) and by push (if
    the signed-up user has one enabled) -- both silently no-op if
    unconfigured/unset, so a deployment with neither still just no-ops
    without a special case. (2) any live (session-attached) NetControlShift
    in an active activation whose window has opened, push-only, best-effort
    matched to a registered user by callsign (issue follow-up -- see that
    section for why). `now_utc` is overridable for testing; defaults to the
    real current time."""
    now_utc = now_utc or datetime.now(timezone.utc)
    today = now_utc.date()   # UTC calendar date, kept consistent with now_utc for the window math below

    schedules = (await db.execute(
        select(NetSchedule)
        .join(Net, NetSchedule.net_id == Net.id)
        .filter(Net.reminder_enabled == True)
        .filter(Net.reminder_minutes_before.isnot(None))
        .filter(NetSchedule.day_of_week == today.weekday())
    )).scalars().all()

    sent = 0
    for sched in schedules:
        net = (await db.execute(select(Net).filter(Net.id == sched.net_id))).scalar_one_or_none()
        if not net:
            continue

        start_utc = occurrence_start_utc(sched, today)
        if start_utc is None:
            log(f"Schedule {sched.id} ({net.name}) has an invalid start_time/timezone — skipping")
            continue

        reminder_due_at = start_utc - timedelta(minutes=net.reminder_minutes_before)
        if not (reminder_due_at <= now_utc < start_utc):
            continue   # reminder window hasn't opened yet, or the net has already started

        signups = (await db.execute(
            select(NetControlSignup)
            .filter(
                NetControlSignup.schedule_id == sched.id,
                NetControlSignup.slot_date == today,
                NetControlSignup.reminder_sent_at.is_(None),
            )
        )).scalars().all()

        for signup in signups:
            # Nothing to reach them on at all -- leave reminder_sent_at unset
            # (same as the original email-only behavior) rather than mark a
            # signup "handled" when we couldn't actually notify anyone.
            if not signup.email and not signup.user_id:
                continue
            role_label = ROLE_LABELS.get(signup.role, "Net Control")
            local_time = start_utc.astimezone(ZoneInfo(sched.timezone or "UTC"))
            if signup.email:
                subject = f"[{net.name}] Reminder: you're {role_label} in {net.reminder_minutes_before} min"
                greeting = signup.name or signup.callsign
                body_text = (
                    f"Hi {greeting},\n\n"
                    f"Reminder — you're signed up as {role_label} for {net.name}, "
                    f"starting at {local_time.strftime('%-I:%M %p %Z')} "
                    f"(in about {net.reminder_minutes_before} minutes).\n"
                    + (f"Frequency: {net.frequency}\n" if net.frequency else "")
                    + "\n73 de NetControl Online"
                )
                body_html = f"""
<html><body style="font-family:sans-serif;color:#222;max-width:600px">
<h2 style="color:#1a6496">{net.name}</h2>
<p>Hi {greeting},</p>
<p>Reminder — you're signed up as <strong>{role_label}</strong>, starting at
<strong>{local_time.strftime('%-I:%M %p %Z')}</strong> (in about {net.reminder_minutes_before} minutes).</p>
{f'<p>Frequency: {net.frequency}</p>' if net.frequency else ''}
<p style="color:#666;font-size:12px">73 de NetControl Online</p>
</body></html>"""
                send_email(signup.email, subject, body_html, body_text)
                sent += 1
            # Push (issue follow-up) -- independent of email; a signup made
            # by a registered user always has user_id set (see
            # NetControlSignup's own docstring), regardless of whether an
            # email address was also given.
            if signup.user_id:
                push_title = f"{role_label} reminder"
                push_body = f"{net.name} starts in about {net.reminder_minutes_before} min"
                sent += await send_web_push(db, signup.user_id, push_title, push_body, url="/")
            signup.reminder_sent_at = now_utc

        await db.commit()

    # ── Activation Net Control rotation shift changes (issue follow-up,
    # push-only -- see this function's docstring) ──────────────────────────
    shift_rows = (await db.execute(
        select(NetControlShift, Net)
        .join(NetSession, NetControlShift.session_id == NetSession.id)
        .join(Net, NetControlShift.net_id == Net.id)
        .filter(
            NetControlShift.session_id.isnot(None),   # a live queue entry, not a template row
            NetControlShift.reminder_sent_at.is_(None),
            NetSession.ended_at.is_(None),
            NetSession.is_activation == True,
            Net.reminder_enabled == True,
            Net.reminder_minutes_before.isnot(None),
        )
    )).all()

    for shift, net in shift_rows:
        due_at = shift.scheduled_start - timedelta(minutes=net.reminder_minutes_before)
        if not (due_at <= now_utc < shift.scheduled_start):
            continue   # window hasn't opened yet, or the shift has already started

        # Best-effort match against a registered user -- NetControlShift has
        # always been free-text callsign/name (no user_id column; the
        # activation hand-off flow never resolves one either), so this is
        # the only way to know who to push to. No match = no push, silently
        # -- exactly as if that person isn't a registered user at all.
        callsign = (shift.callsign or "").strip().lower()
        user = (await db.execute(select(User).filter(or_(
            func.lower(User.callsign) == callsign,
            func.lower(User.gmrs_callsign) == callsign,
        )))).scalars().first()
        if user:
            sent += await send_web_push(
                db, user.id,
                title="Net Control shift starting soon",
                body=f"{net.name} — you're on in about {net.reminder_minutes_before} min",
                url="/",
            )
        # Set regardless of a match, so an unmatched free-text callsign
        # isn't rechecked every run forever.
        shift.reminder_sent_at = now_utc

    if shift_rows:
        await db.commit()

    return sent


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    async with SessionLocal() as db:
        sent = await send_due_reminders(db)
        log(f"Done — {sent} reminder(s) sent")


if __name__ == "__main__":
    asyncio.run(main())
