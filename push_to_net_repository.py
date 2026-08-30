#!/usr/bin/env python3
"""
Net Repository Backfill
========================
One-off (but safely re-runnable) script that pushes every already-public net
to the central Net Repository directory. Intended to be run once after
NET_REPOSITORY_URL/NET_REPOSITORY_API_KEY are configured, to submit nets that
were already public before the integration existed — new nets push
automatically going forward (see net_repository.py).

Safe to re-run: Net Repository matches submissions by (API key's
instance_url, source_net_id), so a net already submitted there just gets its
existing entry updated in place rather than duplicated — handy for pushing
local edits, not only for the original backfill.

Usage
-----
    python3 push_to_net_repository.py

Environment variables (read from .env)
---------------------------------------
    DATABASE_URL              PostgreSQL connection string (required)
    NET_REPOSITORY_URL        Base URL of the Net Repository instance
    NET_REPOSITORY_API_KEY    nr_-prefixed API key issued by that instance's admin
"""

import asyncio
import os
import sys

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
from models import Net  # noqa: E402
from net_repository import net_repository_configured, push_net  # noqa: E402


async def run():
    async with SessionLocal() as db:
        if not await net_repository_configured(db):
            sys.exit(
                "NET_REPOSITORY_URL isn't set, and no API key is available (neither "
                "NET_REPOSITORY_API_KEY in .env nor a self-service key obtained via "
                "Admin > Net Repository) — nothing to do. See .env.example."
            )

        nets = (await db.execute(select(Net).filter(Net.public_listed == True))).scalars().all()  # noqa: E712
        if not nets:
            print("No public nets found.")
            return

        pushed = 0
        for net in nets:
            print(f"Pushing '{net.name}' (id={net.id})... ", end="", flush=True)
            if await push_net(net, db):
                print("done")
                pushed += 1
            else:
                print("FAILED (see log)")

        print(f"\n{pushed}/{len(nets)} nets pushed (or updated) at Net Repository.")


if __name__ == "__main__":
    asyncio.run(run())
