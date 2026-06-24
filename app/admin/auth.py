"""Admin panel authentication — session-based with bcrypt passwords."""

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Request, HTTPException, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session, get_session
from app.stats.models import AdminUser

# ---------------------------------------------------------------------------
# Brute-force protection
# ---------------------------------------------------------------------------

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
IP_RATE_LIMIT = 10          # max attempts per IP
IP_RATE_WINDOW = 60         # seconds


class _LoginRateLimiter:
    """Simple in-memory rate limiter keyed by IP address."""

    def __init__(self):
        self._attempts: defaultdict[str, list[float]] = defaultdict(list)

    def is_limited(self, ip: str) -> bool:
        self._prune(ip)
        return len(self._attempts[ip]) >= IP_RATE_LIMIT

    def record(self, ip: str) -> None:
        self._attempts[ip].append(time.time())

    def remaining(self, ip: str) -> int:
        self._prune(ip)
        return max(0, IP_RATE_LIMIT - len(self._attempts[ip]))

    def _prune(self, ip: str) -> None:
        cutoff = time.time() - IP_RATE_WINDOW
        self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]


_rate_limiter = _LoginRateLimiter()


def check_ip_rate_limit(ip: str) -> None:
    """Raise HTTPException if the IP has too many recent login attempts."""
    if _rate_limiter.is_limited(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Please wait a minute and try again.",
            headers={"Retry-After": "60"},
        )


async def check_account_locked(session: AsyncSession, username: str) -> AdminUser | None:
    """Return the user if they exist and are not locked; None if they don't exist.

    Raises HTTPException if the user exists but is currently locked out.
    """
    result = await session.execute(
        select(AdminUser).where(AdminUser.username == username)
    )
    user = result.scalar_one_or_none()

    if user is None:
        return None  # Don't reveal whether the username exists

    if user.locked_until and user.locked_until > datetime.now(timezone.utc):
        remaining = int((user.locked_until - datetime.now(timezone.utc)).total_seconds() / 60) + 1
        # Treat locked account same as bad credentials — don't leak info
        raise HTTPException(
            status_code=429,
            detail=f"Too many login attempts. Try again in {remaining} minute(s).",
            headers={"Retry-After": str(remaining * 60)},
        )

    return user


async def record_failed_attempt(session: AsyncSession, username: str) -> None:
    """Increment the failed-attempt counter and lock the account if needed."""
    result = await session.execute(
        select(AdminUser).where(AdminUser.username == username)
    )
    user = result.scalar_one_or_none()
    if user is None:
        return

    user.failed_attempts += 1
    user.last_attempt_at = datetime.now(timezone.utc)

    if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)

    await session.commit()


async def reset_failed_attempts(session: AsyncSession, user: AdminUser) -> None:
    """Clear the failed-attempt counters after a successful login."""
    user.failed_attempts = 0
    user.locked_until = None
    user.last_attempt_at = datetime.now(timezone.utc)
    await session.commit()

# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))

# ---------------------------------------------------------------------------
# Seeding & core auth
# ---------------------------------------------------------------------------


async def seed_admin_user():
    """Create the default admin user if it doesn't exist."""
    async with async_session() as session:
        result = await session.execute(
            select(AdminUser).where(AdminUser.username == settings.admin_username)
        )
        existing = result.scalar_one_or_none()

        if existing is None:
            user = AdminUser(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
            )
            session.add(user)
            await session.commit()


async def authenticate_user(session: AsyncSession, username: str, password: str) -> AdminUser | None:
    """Verify credentials and return the user if valid."""
    result = await session.execute(
        select(AdminUser).where(AdminUser.username == username)
    )
    user = result.scalar_one_or_none()

    if user and verify_password(password, user.password_hash):
        return user
    return None


async def get_current_admin(request: Request) -> AdminUser:
    """Dependency that requires a valid admin session."""
    username = request.session.get("admin_username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")

    async with async_session() as session:
        result = await session.execute(
            select(AdminUser).where(AdminUser.username == username)
        )
        user = result.scalar_one_or_none()

        if user is None:
            raise HTTPException(status_code=401, detail="Invalid session")

        return user
