#!/usr/bin/env python3
"""
Demo Database Reset
===================
Wipes all application data and recreates a clean database with a single
demo admin account, then reloads the GMRS licence database from a local
archive (faster than re-downloading 54 MB each time).

Intended ONLY for the public demo site at demo.netcontrol.online -- this
does DROP SCHEMA public CASCADE, permanently erasing whatever database
DATABASE_URL points at. Refuses to run at all unless DEMO_INSTANCE=true is
set in the SAME checkout's .env (see below) -- a checkout used against the
wrong instance's .env by mistake (issue follow-up: this happened once,
against a live production database) simply won't run, no matter what
directory a human ran it from or believed they were in. When run from a
terminal (not cron), it also requires typing back the actual database name
it resolved before doing anything -- catches "right box, wrong terminal
session" even on a correctly-flagged instance. Neither check can be
bypassed with a flag; both exist because a human made exactly this mistake
once already.

Usage
-----
    python3 demo_reset.py

Cron (every 4 hours) -- no TTY, so the interactive confirmation above is
skipped automatically; DEMO_INSTANCE=true in .env is still required:
    0 */4 * * *  /opt/netcontrol-demo/venv/bin/python3 /opt/netcontrol-demo/demo_reset.py \\
                 >> /var/log/demo_reset.log 2>&1

Environment variables (read from .env)
---------------------------------------
    DATABASE_URL      PostgreSQL connection string (required)
    DEMO_INSTANCE     Must be exactly "true" -- required, no default. The
                      single opt-in gate: this checkout's .env has to say
                      so explicitly, not just "happen to be the demo".
    GMRS_ZIP_PATH     Path to the local l_gmrs.zip archive
                      Default: /opt/netcontrol-demo/data/l_gmrs.zip
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

# ── Bootstrap ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("python-dotenv not found — activate the virtualenv first.")

load_dotenv(os.path.join(_HERE, ".env"))

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("DATABASE_URL not set in .env")

# Fail-safe by default: an instance's .env has to explicitly opt in before
# this script will touch its database at all. This is the primary guard --
# see the module docstring above for why it exists. Deliberately not
# skippable via a CLI flag; the whole point is that nothing short of
# actually editing THIS checkout's .env should allow the wipe.
if os.getenv("DEMO_INSTANCE", "").strip().lower() not in ("true", "1", "yes"):
    sys.exit(
        "Refusing to run: DEMO_INSTANCE is not set to true in this checkout's .env.\n"
        "This script runs DROP SCHEMA public CASCADE, permanently erasing this\n"
        "database. It's meant only for the disposable public demo instance.\n\n"
        "If this really is the demo instance, add to its .env:\n"
        "    DEMO_INSTANCE=true\n"
        "and re-run. If you meant to target a different checkout, cd there instead --\n"
        "this script only ever acts on the database in ITS OWN directory's .env."
    )

GMRS_ZIP_PATH = os.getenv("GMRS_ZIP_PATH", "/opt/netcontrol-demo/data/l_gmrs.zip")
PYTHON = sys.executable   # same virtualenv python

# Demo admin credentials
DEMO_CALLSIGN = "D3MO"
DEMO_NAME     = "Demo Account"
DEMO_EMAIL    = "demo@demo.netcontrol.online"
DEMO_PASSWORD = "Abcd1234"


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def _target_db_name(url: str) -> str:
    """Just the database name portion of DATABASE_URL, for display and the
    typed confirmation below -- never the host, user, or password."""
    try:
        return urlparse(url).path.lstrip("/") or "(unnamed)"
    except Exception:
        return "(unparseable)"


def _confirm_target_interactively():
    """Second guard, independent of DEMO_INSTANCE above (issue follow-up):
    catches "right instance, wrong terminal session" -- a human who's SSH'd
    into the correct, correctly-flagged demo box but isn't actually looking
    at the shell they think they are. Skipped automatically when stdin
    isn't a TTY (cron has no terminal to confirm from; DEMO_INSTANCE=true
    is that path's only -- and sufficient -- gate)."""
    if not sys.stdin.isatty():
        return
    db_name = _target_db_name(DATABASE_URL)
    print(f"\nThis will PERMANENTLY ERASE the '{db_name}' database (DROP SCHEMA public CASCADE).")
    typed = input(f"Type the database name ({db_name}) to confirm: ").strip()
    if typed != db_name:
        sys.exit("Confirmation did not match — aborting. Nothing was touched.")


# ── Steps ─────────────────────────────────────────────────────────────────────

def drop_and_recreate_schema():
    """Nuke all tables by dropping and recreating the public schema."""
    import psycopg2

    log("Dropping public schema…")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE")
    cur.execute("CREATE SCHEMA public")
    cur.execute("GRANT ALL ON SCHEMA public TO PUBLIC")
    # Also grant to the DB user so the app can write
    db_user = urlparse(DATABASE_URL).username
    if db_user:
        cur.execute(f"GRANT ALL ON SCHEMA public TO {db_user}")
    cur.close()
    conn.close()
    log("Schema reset done")


async def create_tables():
    """Recreate all tables via SQLAlchemy models."""
    log("Creating tables…")
    from database import init_db
    await init_db()
    log("Tables created")


def run_migrations():
    """Apply ALTER TABLE migrations that create_all() cannot handle."""
    log("Running migrations…")
    result = subprocess.run(
        [PYTHON, os.path.join(_HERE, "migrate.py")],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"Migration error (non-fatal):\n{result.stdout}{result.stderr}")
    else:
        log("Migrations applied")


async def create_demo_user():
    """Insert the demo admin account."""
    log(f"Creating demo user {DEMO_CALLSIGN}…")

    from database import SessionLocal
    from models import User
    # Same hashing the app itself uses (routers/auth.py: plain bcrypt, not
    # passlib) -- this script used to bring in its own passlib dependency
    # for the exact same job, which was never actually added to
    # requirements.txt (issue follow-up: "passlib not found" on the demo
    # instance, where nothing had ever installed it). Reusing the app's own
    # function removes that phantom dependency entirely instead of adding
    # passlib to requirements.txt for a second, redundant bcrypt wrapper.
    from routers.auth import hash_password

    hashed = hash_password(DEMO_PASSWORD)

    async with SessionLocal() as db:
        try:
            user = User(
                callsign=DEMO_CALLSIGN,
                name=DEMO_NAME,
                email=DEMO_EMAIL,
                hashed_password=hashed,
                is_active=True,
                is_admin=True,
            )
            db.add(user)
            await db.commit()
            log(f"Demo user created — callsign: {DEMO_CALLSIGN} / email: {DEMO_EMAIL}")
        except Exception as exc:
            await db.rollback()
            log(f"ERROR creating demo user: {exc}")
            raise


def load_gmrs_data():
    """Load GMRS licence database from local zip archive."""
    if not os.path.exists(GMRS_ZIP_PATH):
        log(f"WARNING: GMRS zip not found at {GMRS_ZIP_PATH} — skipping GMRS load")
        log("  To create the archive: python3 gmrs_sync.py --mode full")
        log(f"  Then move l_gmrs.zip to {GMRS_ZIP_PATH}")
        return

    log(f"Loading GMRS data from {GMRS_ZIP_PATH} …")
    result = subprocess.run(
        [PYTHON, os.path.join(_HERE, "gmrs_sync.py"), "--zip", GMRS_ZIP_PATH],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"GMRS load failed:\n{result.stderr}")
    else:
        # Print gmrs_sync output (already timestamped)
        for line in result.stdout.strip().splitlines():
            print(line, flush=True)
        log("GMRS data loaded")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    log("=== Demo reset starting ===")
    _confirm_target_interactively()
    try:
        drop_and_recreate_schema()
        await create_tables()
        run_migrations()
        await create_demo_user()
        load_gmrs_data()
        log("=== Demo reset complete ===")
    except Exception as exc:
        log(f"FATAL: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
