"""SQLAlchemy async engine and session setup."""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


async def init_db():
    """Create all tables on startup."""
    from app.stats.models import (
        AdminUser, UsageLog, Client, ClientGroup, RoutingPolicy, Job,
        ModelConfigRow, RequestLog, GroupTaskRouting,
    )  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:  # type: ignore
    """Dependency that yields an async database session."""
    async with async_session() as session:
        yield session
