import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config

from icp_chatbot.config import Settings
from icp_chatbot.domain.state import SessionState
from icp_chatbot.llm.types import Usage
from icp_chatbot.store import models as m
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.repo import Conflict, NotFound, Repo
from icp_chatbot.versions import default_config


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
async def env():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine)
    clock = Clock()
    factory = make_session_factory(engine)
    yield Repo(factory, clock), clock, factory
    await engine.dispose()


async def new_session(repo, tenant="t1"):
    await repo.ensure_tenant(tenant, "Acme")
    return await repo.create_session(tenant, 1, SessionState())


async def test_session_round_trip_keeps_the_state(env):
    repo, _, _ = env
    sid = await new_session(repo)
    record = await repo.get_session("t1", sid)
    assert record.status == "active" and record.row_version == 1
    assert record.state == SessionState()


async def test_another_tenant_cannot_read_or_change_a_session(env):
    repo, _, _ = env
    sid = await new_session(repo, "t1")
    await repo.ensure_tenant("t2")
    for call in (
        repo.get_session("t2", sid),
        repo.list_turns("t2", sid),
        repo.latest_icp("t2", sid),
        repo.add_feedback("t2", sid, type="approve"),
        repo.save_state("t2", sid, SessionState(), expected_version=1),
    ):
        with pytest.raises(NotFound):
            await call
    with pytest.raises(NotFound):
        await repo.get_session("t1", "does-not-exist")


async def test_save_state_accumulates_usage_and_bumps_the_version(env):
    repo, clock, _ = env
    sid = await new_session(repo)
    state = SessionState(questions_asked=2)
    v = await repo.save_state(
        "t1", sid, state, expected_version=1, usage=Usage(100, 20, 5, 0), cost_usd=0.01
    )
    assert v == 2
    v = await repo.save_state(
        "t1", sid, state, expected_version=2, usage=Usage(50, 10, 0, 0), cost_usd=0.02
    )
    record = await repo.get_session("t1", sid)
    assert record.row_version == 3 and record.question_count == 2
    assert record.usage == Usage(150, 30, 5, 0)
    assert record.cost_usd == pytest.approx(0.03)


async def test_a_stale_version_is_rejected_as_a_conflict(env):
    repo, _, _ = env
    sid = await new_session(repo)
    await repo.save_state("t1", sid, SessionState(), expected_version=1)
    with pytest.raises(Conflict):
        await repo.save_state("t1", sid, SessionState(), expected_version=1)


async def test_status_changes_set_the_end_time(env):
    repo, _, _ = env
    sid = await new_session(repo)
    await repo.save_state("t1", sid, SessionState(), expected_version=1, status="completed")
    record = await repo.get_session("t1", sid)
    assert record.status == "completed" and record.ended_at is not None


async def add_turn(repo, sid, n):
    await repo.add_turn(
        "t1", sid, turn_no=n, question_text=f"q{n}", question_kind="slot", slot_target="industries",
        answer_text=f"a{n}", extracted={}, state_after={}, step_models={"extraction": "m"},
        latency_ms=5, usage=Usage(10, 2, 0, 0), cost_usd=0.001,
    )  # fmt: skip


async def test_turns_are_listed_in_order(env):
    repo, _, _ = env
    sid = await new_session(repo)
    await add_turn(repo, sid, 2)
    await add_turn(repo, sid, 1)
    assert [t.turn_no for t in await repo.list_turns("t1", sid)] == [1, 2]


async def test_append_only_tables_reject_updates(env):
    repo, _, factory = env
    sid = await new_session(repo)
    await add_turn(repo, sid, 1)
    with pytest.raises(RuntimeError, match="append-only"):
        async with factory() as s, s.begin():
            turn = await s.get(m.Turn, 1)
            turn.answer_text = "edited"


async def test_icp_versions_increment_and_can_be_filtered_by_author(env):
    repo, _, _ = env
    sid = await new_session(repo)
    assert await repo.latest_icp("t1", sid) is None
    assert await repo.add_icp_version("t1", sid, document={"a": 1}, summary="s1", author="bot") == 1
    assert (
        await repo.add_icp_version("t1", sid, document={"a": 2}, summary="s2", author="company")
        == 2
    )
    assert (await repo.latest_icp("t1", sid)).version == 2
    assert (await repo.latest_icp("t1", sid, author="bot")).document == {"a": 1}
    assert [r.version for r in await repo.list_icps("t1", sid)] == [1, 2]


async def test_feedback_and_enrichment_are_recorded(env):
    repo, _, _ = env
    sid = await new_session(repo)
    await repo.add_feedback("t1", sid, type="field_edit", field="industries", old=["a"], new=["b"])
    [event] = await repo.list_feedback("t1", sid)
    assert (event.type, event.field, event.old_value, event.new_value) == (
        "field_edit", "industries", ["a"], ["b"],
    )  # fmt: skip
    await repo.save_enrichment(
        "t1", sid, website="https://acme.test", pages=["https://acme.test"], outcome="ok",
        seed={}, duration_ms=120, usage=Usage(5000, 500, 0, 0),
    )  # fmt: skip


async def test_publishing_a_config_retires_the_previous_live_one(env):
    repo, _, _ = env
    assert await repo.live_config() is None
    settings = Settings(_env_file=None)
    await repo.publish_config(default_config(settings, 1), author="system")
    await repo.publish_config(default_config(settings, 2), author="dev")
    assert (await repo.live_config()).version == 2
    assert (await repo.get_config(1)).version == 1
    with pytest.raises(NotFound):
        await repo.get_config(99)


async def test_usage_since_counts_sessions_and_tokens_per_tenant_and_overall(env):
    repo, clock, _ = env
    sid = await new_session(repo, "t1")
    await repo.save_state(
        "t1", sid, SessionState(), expected_version=1, usage=Usage(600, 300, 100, 0)
    )
    other = await new_session(repo, "t2")
    await repo.save_state(
        "t2", other, SessionState(), expected_version=1, usage=Usage(10, 10, 0, 0)
    )
    day_start = clock.now - timedelta(hours=1)
    assert await repo.usage_since(day_start, "t1") == (1, 1000)
    assert await repo.usage_since(day_start) == (2, 1020)
    assert await repo.usage_since(clock.now + timedelta(hours=1)) == (0, 0)


async def test_stale_active_sessions_are_found_across_tenants(env):
    repo, clock, _ = env
    old = await new_session(repo, "t1")
    clock.advance(hours=50)
    fresh = await new_session(repo, "t2")
    stale = await repo.list_stale_active(clock.now - timedelta(hours=48))
    assert stale == [("t1", old)]
    assert fresh not in [s for _, s in stale]


def test_the_alembic_migration_creates_every_table(tmp_path):
    db = tmp_path / "migrated.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "head")
    tables = {r[0] for r in sqlite3.connect(db).execute("select name from sqlite_master")}
    assert {
        "tenants", "sessions", "turns", "icp_versions", "feedback_events", "config_versions",
        "enrichment_runs", "alembic_version",
    } <= tables  # fmt: skip


async def test_deleting_a_tenant_removes_everything_it_stored_and_nothing_else(env):
    repo, _, _ = env
    mine = await new_session(repo, "t1")
    other = await new_session(repo, "t2")
    for sid, tenant in ((mine, "t1"), (other, "t2")):
        await add_turn(repo, sid, 1) if tenant == "t1" else None
        await repo.add_icp_version(tenant, sid, document={}, summary="", author="bot")
        await repo.add_feedback(tenant, sid, type="approve")
        await repo.save_enrichment(
            tenant,
            sid,
            website=None,
            pages=[],
            outcome="skipped",
            seed={},
            duration_ms=0,
            usage=Usage(),
        )
    assert await repo.delete_tenant("t1") == 1
    with pytest.raises(NotFound):
        await repo.get_session("t1", mine)
    assert (await repo.get_session("t2", other)).status == "active"
    assert len(await repo.list_icps("t2", other)) == 1
    assert await repo.delete_tenant("t1") == 0  # nothing left; safe to repeat
    assert await repo.delete_tenant("never-existed") == 0


async def test_stored_prompts_are_overridden_by_prompts_py(env):
    repo, _, _ = env
    from icp_chatbot.config import Settings
    from icp_chatbot.prompts import DEFAULT_PROMPTS
    from icp_chatbot.versions import default_config

    config = default_config(Settings(_env_file=None))
    config.prompts["opener"] = "an old prompt stored in the database"
    await repo.publish_config(config, author="test")
    assert (await repo.live_config()).prompts["opener"] == DEFAULT_PROMPTS["opener"]
    assert (await repo.get_config(1)).prompts["opener"] == DEFAULT_PROMPTS["opener"]


async def test_sessions_for_a_new_tenant_can_start_at_the_same_moment(env):
    import asyncio

    repo, _, _ = env
    await asyncio.gather(*[repo.ensure_tenant("brand-new", "Acme") for _ in range(8)])
    await repo.ensure_tenant("brand-new", "Acme")  # and again afterwards
