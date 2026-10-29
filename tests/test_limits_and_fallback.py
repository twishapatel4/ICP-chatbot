from datetime import UTC, datetime, timedelta

import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.fallback import apply_form, fallback_form, missing_required
from icp_chatbot.limits import BudgetExceeded, BudgetGuard, RateLimiter
from icp_chatbot.llm.types import Usage
from icp_chatbot.maintenance import abandon_stale_sessions
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.repo import Repo


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
async def world():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine)
    clock = Clock()
    yield Repo(make_session_factory(engine), clock), clock
    await engine.dispose()


async def session(repo, tenant="t1", tokens=0):
    await repo.ensure_tenant(tenant)
    sid = await repo.create_session(tenant, 1, SessionState())
    if tokens:
        await repo.save_state(
            tenant, sid, SessionState(), expected_version=1, usage=Usage(tokens, 0, 0, 0)
        )
    return sid


def guard(repo, clock, **limits):
    return BudgetGuard(repo, Settings(_env_file=None, **limits), clock)


async def test_the_guard_allows_work_below_every_cap(world):
    repo, clock = world
    await session(repo)
    await guard(repo, clock).check_start("t1")


@pytest.mark.parametrize(
    "limits,scope",
    [
        ({"daily_session_cap_per_tenant": 1}, "tenant_sessions"),
        ({"daily_token_cap_per_tenant": 500}, "tenant_tokens"),
        ({"global_daily_session_cap": 2}, "global_sessions"),
        ({"global_daily_token_cap": 900}, "global_tokens"),
    ],
)
async def test_each_cap_refuses_a_new_interview(world, limits, scope):
    repo, clock = world
    await session(repo, "t1", tokens=500)
    await session(repo, "t2", tokens=400)
    with pytest.raises(BudgetExceeded) as err:
        await guard(repo, clock, **limits).check_start("t1")
    assert err.value.scope == scope


async def test_another_tenants_usage_does_not_count_against_the_tenant_cap(world):
    repo, clock = world
    await session(repo, "t2")
    await guard(repo, clock, daily_session_cap_per_tenant=1).check_start("t1")


async def test_yesterdays_usage_is_not_counted(world):
    repo, clock = world
    await session(repo)
    clock.now += timedelta(days=1)
    await guard(repo, clock, daily_session_cap_per_tenant=1).check_start("t1")


def test_the_rate_limiter_blocks_after_the_limit_and_recovers():
    now = [0.0]
    limiter = RateLimiter(3, clock=lambda: now[0])
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("b")  # keys are independent
    now[0] = 61.0
    assert limiter.allow("a")


def test_the_form_asks_every_blocking_tier_a_slot_as_required():
    form = fallback_form()
    required = {f.key for f in form if f.required}
    assert required == {
        "industries", "company_size", "lead_geographies", "company_hq_geographies", "buyer_roles",
        "buyer_departments", "buyer_seniority", "disqualifiers", "product", "business_model",
        "customer_profile",
    }  # fmt: skip
    size = next(f for f in form if f.key == "company_size")
    assert "1-10" in size.choices and "10,001+" in size.choices
    assert any(not f.required for f in form)


def test_missing_required_ignores_inapplicable_and_already_known_fields():
    state = SessionState()
    state.slots["industries"] = SlotValue(values=["x"], confidence=0.9, source=SlotSource.STATED)
    missing = missing_required({"company_size": ["11-50"]}, state)
    assert (
        "industries" not in missing
        and "company_size" not in missing
        and "lead_geographies" in missing
        and "company_hq_geographies" in missing
    )
    state.slots["business_model"] = SlotValue(
        values=["B2C"], confidence=0.9, source=SlotSource.STATED
    )
    b2c_missing = missing_required({}, state)
    assert "buyer_roles" not in b2c_missing and "company_hq_geographies" not in b2c_missing
    assert "lead_geographies" in b2c_missing


def test_apply_form_records_stated_values_and_rejects_unknown_fields():
    state = SessionState()
    applied = apply_form(state, {"industries": ["Fintech"], "disqualifiers": ["  "]}, turn_no=3)
    assert applied == ["industries"]
    value = state.slots["industries"]
    assert (value.confidence, value.source, value.source_turn) == (0.8, SlotSource.STATED, 3)
    with pytest.raises(ValueError, match="unknown"):
        apply_form(state, {"favourite_colour": ["blue"]}, 4)


async def test_stale_active_sessions_are_marked_abandoned_with_the_stopping_point(world):
    repo, clock = world
    await repo.ensure_tenant("t1")
    state = SessionState(questions_asked=4, pending_slot="buyer_roles", pending_kind="slot")
    stale = await repo.create_session("t1", 1, state)
    done = await repo.create_session("t1", 1, SessionState())
    await repo.save_state("t1", done, SessionState(), expected_version=1, status="completed")
    clock.now += timedelta(hours=50)
    fresh = await repo.create_session("t1", 1, SessionState())

    assert await abandon_stale_sessions(repo, timedelta(hours=48), clock.now) == 1
    assert (await repo.get_session("t1", stale)).status == "abandoned"
    assert (await repo.get_session("t1", done)).status == "completed"
    assert (await repo.get_session("t1", fresh)).status == "active"
    [event] = await repo.list_feedback("t1", stale)
    assert (event.type, event.field, event.old_value) == ("abandon", "buyer_roles", 4)
    assert await abandon_stale_sessions(repo, timedelta(hours=48), clock.now) == 0  # idempotent
