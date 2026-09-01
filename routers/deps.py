"""
Shared auth/db dependencies, used by every router module.
"""

import hashlib
import os
from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db  # noqa: F401  -- re-exported for `from routers.deps import get_db`
from models import ApiToken, User

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-use-a-long-random-string")
ALGORITHM = "HS256"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
# storage_uri=None (REDIS_URL unset, the default) makes slowapi/`limits` use
# its built-in in-memory storage -- exactly today's behavior, correct only
# for a single uvicorn worker process. Set REDIS_URL (and WORKERS > 1 in
# .env) to share rate-limit counters across workers instead -- without this,
# every configured limit is silently multiplied by the worker count, since
# each worker would otherwise count requests against its own private
# in-memory counter (see TECH_DEBT.md, resolved). `limits`'s Redis storage
# backend is synchronous (blocks the event loop for the round-trip) -- a
# pre-existing constraint of slowapi/`limits` itself, not something this app
# can avoid short of swapping rate-limiting libraries; negligible in
# practice against a local/same-network Redis for an app this size.
REDIS_URL = os.getenv("REDIS_URL")
limiter = Limiter(key_func=get_remote_address, storage_uri=REDIS_URL)


async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # --- Try long-lived API token first (format: "nt_<64 hex chars>") ---
    if token.startswith("nt_"):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        api_token = (await db.execute(select(ApiToken).filter(ApiToken.token_hash == token_hash))).scalar_one_or_none()
        if api_token is None:
            raise credentials_exception
        user = (await db.execute(select(User).filter(User.id == api_token.user_id))).scalar_one_or_none()
        if user is None or not user.is_active:
            raise credentials_exception
        # Update last_used_at (fire-and-forget; don't fail the request if this errors)
        try:
            api_token.last_used_at = datetime.now(timezone.utc)
            await db.commit()
        except Exception:
            await db.rollback()
        return user

    # --- Fall back to JWT ---
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    user = (await db.execute(select(User).filter(User.id == int(user_id)))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise credentials_exception
    return user


oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


async def get_current_user_optional(token: str | None = Depends(oauth2_scheme_optional), db: AsyncSession = Depends(get_db)) -> User | None:
    """Same identity resolution as get_current_user, but for endpoints that
    are public and must keep working for a logged-out caller -- returns None
    instead of 401ing when there's no token or it doesn't resolve to a user.
    First use: GET /i18n/languages, which serves anonymous visitors (the
    login screen) as well as logged-in ones (scoped to their current org)."""
    if not token:
        return None
    try:
        return await get_current_user(token=token, db=db)
    except HTTPException:
        return None
