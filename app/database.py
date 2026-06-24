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

    # Add columns added after initial deployment (SQLite doesn't ALTER via create_all)
    async with engine.connect() as conn:
        new_columns = {
            "admin_users": [
                ("failed_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("locked_until", "DATETIME"),
                ("last_attempt_at", "DATETIME"),
            ],
        }
        for table, columns in new_columns.items():
            for col_name, col_def in columns:
                try:
                    await conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"
                    )
                except Exception:
                    pass  # Column already exists — safe to ignore


async def get_session() -> AsyncSession:  # type: ignore
    """Dependency that yields an async database session."""
    async with async_session() as session:
        yield session
