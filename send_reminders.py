#!/usr/bin/env python3
"""
Scheduled Net Reminders
========================
Emails whoever is signed up as Net Control / Broadcaster for a net a
configurable number of minutes before their session starts. Intended to be
run frequently from cron — each signup is only reminded once, tracked via
NetControlSignup.reminder_sent_at, so re-running on a short interval is safe.

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
"""

import asyncio
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

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
from models import Net, NetControlSignup, NetSchedule  # noqa: E402

SMTP_HOST     = os.getenv("SMTP_HOST", "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", "")
SMTP_USE_TLS  = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
SMTP_USE_SSL  = os.getenv("SMTP_USE_SSL", "false").lower() == "true"

ROLE_LABELS = {"net_control": "Net Control", "broadcaster": "Broadcaster", "both": "Net Control & Broadcaster"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def send_email(to: str, subject: str, body_html: str, body_text: str) -> None:
    """Minimal standalone mailer — mirrors main.py's send_email() without
    needing to import the whole FastAPI app just to send a reminder."""
    if not smtp_configured():
        log(f"SMTP not configured — skipping: {subject}")
        return
    from_addr = SMTP_FROM or SMTP_USER
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
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
    """Send reminder emails for any signup whose reminder window has opened
    but who hasn't been reminded yet. Returns the number of emails sent.
    `now_utc` is overridable for testing; defaults to the real current time."""
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
            if not signup.email:
                continue
            role_label = ROLE_LABELS.get(signup.role, "Net Control")
            local_time = start_utc.astimezone(ZoneInfo(sched.timezone or "UTC"))
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
            signup.reminder_sent_at = now_utc
            sent += 1

        await db.commit()

    return sent


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    async with SessionLocal() as db:
        sent = await send_due_reminders(db)
        log(f"Done — {sent} reminder(s) sent")


if __name__ == "__main__":
    asyncio.run(main())
