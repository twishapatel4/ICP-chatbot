import asyncio
from datetime import timedelta

from icp_chatbot.cli import abandon
from icp_chatbot.config import get_settings
from icp_chatbot.domain.state import SessionState
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.models import utcnow
from icp_chatbot.store.repo import Repo


def test_the_abandon_command_marks_old_sessions_and_leaves_new_ones(tmp_path, monkeypatch, capsys):
    url = f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()

    async def seed() -> tuple[str, str]:
        engine = make_engine(url)
        await init_models(engine)
        factory = make_session_factory(engine)
        old = Repo(factory, clock=lambda: utcnow() - timedelta(hours=5))
        await old.ensure_tenant("t1")
        stale = await old.create_session("t1", 1, SessionState())
        fresh = await Repo(factory).create_session("t1", 1, SessionState())
        await engine.dispose()
        return stale, fresh

    stale, fresh = asyncio.run(seed())
    assert abandon.main(["--hours", "1"]) == 0
    assert "marked 1 session(s)" in capsys.readouterr().out

    async def statuses() -> dict[str, str]:
        engine = make_engine(url)
        repo = Repo(make_session_factory(engine))
        result = {sid: (await repo.get_session("t1", sid)).status for sid in (stale, fresh)}
        await engine.dispose()
        return result

    assert asyncio.run(statuses()) == {stale: "abandoned", fresh: "active"}
    get_settings.cache_clear()
