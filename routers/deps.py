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
limiter = Limiter(key_func=get_remote_address)


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
