import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.agent import AgentEngine, AgentTurn
from icp_chatbot.engine.extraction import SlotUpdate
from icp_chatbot.engine.planner import enough_for_icp
from icp_chatbot.engine.questions import QuestionOutput
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import LLMError, Usage
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.repo import Repo
from icp_chatbot.versions import default_config

SETTINGS = Settings(_env_file=None)
T = "tenant-1"


class NoEnrichment:
    async def run(self, signup, config, labels=None):
        raise AssertionError("not used")


@pytest.fixture
async def world():
    db = make_engine("sqlite+aiosqlite:///:memory:")
    await init_models(db)
    repo = Repo(make_session_factory(db))
    llm = FakeLLM({}, Usage(100, 10, 0, 0))
    engine = AgentEngine(
        repo=repo, llm=llm, enrichment=NoEnrichment(), settings=SETTINGS, vary_order=False
    )
    yield engine, repo, llm
    await db.dispose()


def up(slot, values, confidence=0.9):
    return SlotUpdate(slot=slot, values=values, confidence=confidence)


def ask(say, *updates, **kw):
    return AgentTurn(say=say, action="ask", updates=list(updates), **kw)


async def session(repo, state=None, status="active"):
    await repo.ensure_tenant(T)
    try:
        await repo.publish_config(default_config(SETTINGS), author="test")
    except Exception:
        pass
    state = state or SessionState(
        pending_question="What do you sell?", pending_kind="opener", questions_asked=1
    )
    sid = await repo.create_session(T, 1, state)
    if status != "active":
        await repo.save_state(T, sid, state, expected_version=1, status=status)
    return sid


def known(state, **slots):
    for key, values in slots.items():
        state.slots[key] = SlotValue(values=values, confidence=0.9, source=SlotSource.STATED)
    return state


async def test_the_model_writes_the_next_question_and_the_fields_are_recorded(world):
    engine, repo, llm = world
    sid = await session(repo)
    llm.queue(
        "interviewer",
        ask("Payroll, nice. Who usually buys it?", up("product", ["Payroll software"]),
            target_slot="buyer_roles", options=["CFO", "HR head", "Owner"]),
    )  # fmt: skip
    result = await engine.answer(T, sid, "We sell payroll software")
    assert result.status == "active" and result.question.text.startswith("Payroll, nice")
    assert result.question.slot == "buyer_roles" and result.question.options == (
        "CFO", "HR head", "Owner",
    )  # fmt: skip
    record = await repo.get_session(T, sid)
    assert record.state.slots["product"].values == ["Payroll software"]
    assert record.state.questions_asked == 2
    assert "We sell payroll software" in llm.calls_for("interviewer")[0].user


async def test_open_ended_fields_never_get_options(world):
    engine, repo, llm = world
    sid = await session(repo)
    llm.queue(
        "interviewer",
        ask("How does a deal go from first call to payment?", target_slot="how_we_work",
            options=["A", "B", "C"]),
    )  # fmt: skip
    result = await engine.answer(T, sid, "hello")
    assert result.question.options == ()


async def test_finishing_too_early_is_sent_back_to_the_model_to_fix(world):
    engine, repo, llm = world
    sid = await session(repo)
    early = AgentTurn(say="All done!", action="finish", enough=False, missing=["who buys"])
    llm.queue("interviewer", early, ask("Who buys it?"))
    result = await engine.answer(T, sid, "hi")
    assert result.status == "active" and result.question.text == "Who buys it?"
    second = llm.calls_for("interviewer")[1].user
    assert "rejected" in second and "who buys" in second


async def test_enough_without_the_basics_is_also_rejected(world):
    engine, repo, llm = world
    sid = await session(repo)
    bad = AgentTurn(say="Thanks!", action="finish", enough=True)
    llm.queue("interviewer", bad, bad)
    result = await engine.answer(T, sid, "that is all")
    assert result.status == "active"  # two rejected replies count as a failed step
    assert result.notice is not None


async def test_the_model_can_finish_when_the_basics_are_known(world):
    engine, repo, llm = world
    state = known(
        SessionState(pending_question="Who buys?", pending_kind="slot", questions_asked=6),
        product=["Payroll"], industries=["Banks"], buyer_roles=["CFO"],
    )  # fmt: skip
    sid = await session(repo, state)
    llm.queue(
        "interviewer",
        AgentTurn(say="Great, I have what I need for banks and CFOs.", action="finish", enough=True),
    )
    result = await engine.answer(T, sid, "the CFO")
    assert (result.status, result.stop_reason, result.icp_ready) == ("completed", "complete", True)
    assert "banks" in result.question.text
    record = await repo.get_session(T, sid)
    assert record.status == "completed" and enough_for_icp(record.state)


async def test_a_user_who_stops_early_gets_a_completed_session_marked_not_enough(world):
    engine, repo, llm = world
    sid = await session(repo)
    llm.queue(
        "interviewer",
        AgentTurn(say="I only know your product so far.", action="finish", user_wants_to_stop=True,
                  missing=["who your customers are"]),
    )  # fmt: skip
    result = await engine.answer(T, sid, "I want to stop")
    assert result.stop_reason == "user_finished"
    record = await repo.get_session(T, sid)
    assert record.state.agent_enough is False
    assert record.state.agent_missing == ["who your customers are"]
    assert not enough_for_icp(record.state)


async def test_the_question_cap_ends_the_interview_even_if_the_model_keeps_asking(world):
    engine, repo, llm = world
    state = known(
        SessionState(pending_question="Q?", pending_kind="slot",
                     questions_asked=SETTINGS.quick_mode_question_cap),
        product=["Payroll"],
    )  # fmt: skip
    sid = await session(repo, state)
    llm.queue("interviewer", ask("One more thing?"))
    llm.queue("question", QuestionOutput(question="Thanks, that is plenty."))
    result = await engine.answer(T, sid, "ok")
    assert (result.status, result.stop_reason) == ("completed", "cap")


async def test_two_failed_model_calls_fall_back_to_the_form(world):
    engine, repo, llm = world
    sid = await session(repo)
    llm.queue("interviewer", LLMError("api", "down"))
    first = await engine.answer(T, sid, "hello")
    assert first.status == "active" and first.notice is not None
    llm.queue("interviewer", LLMError("api", "down"))
    second = await engine.answer(T, sid, "hello again")
    assert (second.status, second.stop_reason) == ("fallback", "model_unavailable")


async def test_the_conversation_so_far_is_shown_to_the_model_each_turn(world):
    engine, repo, llm = world
    sid = await session(repo)
    llm.queue("interviewer", ask("Who buys it?", up("product", ["Payroll"])),
              ask("Which industries?"))  # fmt: skip
    await engine.answer(T, sid, "We sell payroll")
    await engine.answer(T, sid, "CFOs")
    second = llm.calls_for("interviewer")[1].user
    assert "You: What do you sell?" in second and "User: We sell payroll" in second
