"""
Database connection and session management (async SQLAlchemy).
"""

import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from dotenv import load_dotenv
from models import Base

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/ham_net_tracker"
)


def _async_url(url: str) -> str:
    """Inject the async driver into a plain postgresql://... or sqlite://...
    URL, so existing deployments' .env files (which use the bare scheme,
    no +driver) keep working unchanged after the async migration. A URL
    that already names an explicit driver is left alone."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


engine = create_async_engine(_async_url(DATABASE_URL), pool_pre_ping=True)

# expire_on_commit=False: without this, every attribute read on an object
# after db.commit() would trigger an implicit lazy-refresh SELECT -- fine
# synchronously, but a sync (non-awaited) attribute access can't issue an
# async query, so it raises MissingGreenlet. Keeping already-loaded values
# in memory after commit instead avoids that whole class of failure; this
# is the standard recommendation for async SQLAlchemy + FastAPI.
SessionLocal = async_sessionmaker(engine, autocommit=False, autoflush=False, expire_on_commit=False)


async def init_db():
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """FastAPI dependency: yields a DB session and closes it when done."""
    async with SessionLocal() as db:
        yield db
