"""Engine and session factory."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from icp_chatbot.store.models import Base


def make_engine(url: str) -> AsyncEngine:
    kwargs = {}
    if url.startswith("sqlite") and ":memory:" in url:
        # One shared connection, otherwise every connection would see its own empty database.
        kwargs["poolclass"] = StaticPool
    return create_async_engine(url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_models(engine: AsyncEngine) -> None:
    """Create all tables. Used by tests and local development; production uses Alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
