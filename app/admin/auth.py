"""Admin panel authentication — session-based with bcrypt passwords."""

import bcrypt
from fastapi import Request, HTTPException, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session, get_session
from app.stats.models import AdminUser


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


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
