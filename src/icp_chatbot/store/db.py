"""Engine and session factory."""

import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from icp_chatbot.store.models import Base


def make_engine(url: str) -> AsyncEngine:
    kwargs = {}
    if url.startswith("sqlite") and ":memory:" in url:
        # One shared connection, otherwise every connection would see its own empty database.
        kwargs["poolclass"] = StaticPool
    elif url.startswith("postgresql"):
        # Neon's pooler (pgbouncer) does not like server-side prepared statements.
        kwargs["connect_args"] = {"prepare_threshold": None}
        if os.environ.get("VERCEL"):
            # Serverless: no long-lived process to hold a pool; the Neon pooler does that.
            kwargs["poolclass"] = NullPool
    return create_async_engine(url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_models(engine: AsyncEngine) -> None:
    """Create all tables. Used by tests and local development; production uses Alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
