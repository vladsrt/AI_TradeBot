"""
Async SQLAlchemy engine and session for Signal Bot.

Uses SQLite via aiosqlite — zero setup, single file.
Swap DATABASE_URL in .env when you want PostgreSQL later.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)

from app.config import config
from app.db.models import Base


# ---------------------------------------------------------------------------
# Engine — created once, reused everywhere
# ---------------------------------------------------------------------------


engine = create_async_engine(
    config.DATABASE_URL,
    echo=config.DATABASE_ECHO,
    pool_size=5,
)


# ---------------------------------------------------------------------------
# Session factory — call async_session() to get a new session
# ---------------------------------------------------------------------------


async_session = async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def init_db():
    """
    Create all tables if they do not exist yet.
    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS.
    """

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    """
    Yield a new database session.
    Used as a FastAPI dependency for request-scoped sessions.
    """

    async with async_session() as session:
        yield session
