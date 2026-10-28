from datetime import UTC, datetime, timedelta

import pytest

from icp_chatbot.domain.state import SessionState, SlotSource
from icp_chatbot.icp.document import FieldEntry, ICPDocument, compute_completeness, group_by_section
from icp_chatbot.icp.scoring import default_scoring_model
from icp_chatbot.llm.types import Usage
from icp_chatbot.metrics.queries import dashboard, percentile
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.repo import Repo


def test_percentile_uses_the_nearest_rank():
    assert percentile([], 50) == 0
    assert percentile([10], 95) == 10
    values = list(range(1, 101))
    assert percentile(values, 50) == 50 and percentile(values, 95) == 95


def doc(**slots):
    entries = {
        k: FieldEntry(values=v, confidence=0.9, source=SlotSource.STATED) for k, v in slots.items()
    }
    return ICPDocument(sections=group_by_section(entries),
                       scoring_model=default_scoring_model(slots), completeness=compute_completeness(entries))  # fmt: skip


@pytest.fixture
async def repo_and_factory():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine)
    factory = make_session_factory(engine)
    yield Repo(factory), factory
    await engine.dispose()


async def add_session(repo, status, questions, cost, latencies=(), tenant="t1", pending_slot=None):
    await repo.ensure_tenant(tenant)
    state = SessionState(questions_asked=questions, pending_slot=pending_slot)
    sid = await repo.create_session(tenant, 1, state)
    await repo.save_state(tenant, sid, state, expected_version=1, usage=Usage(10, 10, 0, 0), cost_usd=cost,
                          status=None if status == "active" else status)  # fmt: skip
    for n, ms in enumerate(latencies, 1):
        await repo.add_turn(tenant, sid, turn_no=n, question_text="q", question_kind="slot", slot_target=None,
                            answer_text="a", extracted={}, state_after={}, step_models={}, latency_ms=ms,
                            usage=Usage(), cost_usd=0.0)  # fmt: skip
    return sid


async def test_the_dashboard_summarizes_sessions_latency_and_feedback(repo_and_factory):
    repo, factory = repo_and_factory
    since = datetime.now(UTC) - timedelta(days=1)
    approved = await add_session(repo, "completed", 8, 0.10, latencies=(100, 200, 300))
    await add_session(repo, "completed", 10, 0.20, latencies=(400,))
    dropped = await add_session(repo, "abandoned", 4, 0.05, pending_slot="buyer_roles")
    await add_session(repo, "fallback", 3, 0.02)
    await add_session(repo, "active", 2, 0.01)

    draft = doc(
        industries=["Fintech"],
        buyer_roles=["CFO"],
        geographies=["Europe"],
        company_size=["50-500"],
    )
    await repo.add_icp_version(
        "t1", approved, document=draft.model_dump(mode="json"), summary="", author="bot"
    )
    final = doc(
        industries=["Fintech"], buyer_roles=["CFO"], geographies=["Asia"], company_size=["50-500"]
    )
    await repo.add_icp_version(
        "t1", approved, document=final.model_dump(mode="json"), summary="", author="company"
    )
    await repo.add_feedback("t1", approved, type="field_edit", field="geographies")
    await repo.add_feedback("t1", approved, type="weight_edit", field="industry_fit")
    await repo.add_feedback("t1", approved, type="requirement_toggle", field="geographic_fit")
    await repo.add_feedback("t1", approved, type="approve")
    await repo.add_feedback("t1", dropped, type="abandon", field="buyer_roles")

    d = await dashboard(factory, since)
    assert (d.sessions, d.completed, d.abandoned, d.fallback) == (5, 2, 1, 1)
    assert d.completion_rate == pytest.approx(0.4) and d.mean_questions == 9.0
    assert d.mean_cost_usd == pytest.approx(0.076)
    assert (d.p50_latency_ms, d.p95_latency_ms) == (200, 400)
    assert d.approved_sessions == 1 and d.mean_edit_rate == pytest.approx(1 / 4)
    assert d.dropoff_by_slot == {"buyer_roles": 1} and d.field_edits == {"geographies": 1}
    assert d.weight_edits == {"industry_fit": 1} and d.requirement_toggles == {"geographic_fit": 1}


async def test_the_dashboard_ignores_older_sessions_and_handles_no_data(repo_and_factory):
    repo, factory = repo_and_factory
    await add_session(repo, "completed", 8, 0.10)
    assert (await dashboard(factory, datetime.now(UTC) + timedelta(days=1))).sessions == 0
    empty = await dashboard(factory, datetime.now(UTC) + timedelta(days=1))
    assert empty.mean_edit_rate is None and empty.completion_rate == 0.0
