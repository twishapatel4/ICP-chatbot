import asyncio

import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.slots import SLOT_LIST, Tier
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.extraction import ExtractionResult, Flags, SlotUpdate
from icp_chatbot.engine.interview import (
    DECLINE_PREFIX,
    REDIRECT_PREFIX,
    RETRY_PREFIX,
    InterviewEngine,
    InvalidAnswer,
    InvalidTransition,
    SessionClosed,
)
from icp_chatbot.engine.questions import QuestionOutput
from icp_chatbot.enrichment.service import EnrichmentOutcome
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import LLMError, Usage
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.repo import Conflict, NotFound, Repo
from icp_chatbot.versions import default_config

SETTINGS = Settings(_env_file=None)
T = "tenant-1"


class FakeEnrichment:
    def __init__(self, outcome=None):
        self.outcome = outcome or EnrichmentOutcome("skipped")
        self.calls = 0

    async def run(self, signup, config, labels=None):
        self.calls += 1
        return self.outcome


def ext(*updates, flags=None, summary=""):
    return ExtractionResult(updates=list(updates), flags=flags or Flags(), summary=summary)


def up(slot, values, confidence=0.9):
    return SlotUpdate(slot=slot, values=values, confidence=confidence)


def q(text):
    return QuestionOutput(question=text)


@pytest.fixture
async def world():
    engine_db = make_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine_db)
    repo = Repo(make_session_factory(engine_db))
    llm = FakeLLM({}, Usage(100, 10, 0, 0))
    enrichment = FakeEnrichment()
    engine = InterviewEngine(repo=repo, llm=llm, enrichment=enrichment, settings=SETTINGS)
    yield engine, repo, llm, enrichment
    await engine_db.dispose()


async def started(world, **signup):
    engine, *_ = world
    return await engine.start(T, SignupInfo("Acme", **signup))


async def preset(repo, state, status="active"):
    """Create a session in a chosen state, as if earlier turns had happened."""
    await repo.ensure_tenant(T)
    config = default_config(SETTINGS)
    try:
        await repo.publish_config(config, author="test")
    except Exception:
        pass
    sid = await repo.create_session(T, 1, state)
    if status != "active":
        await repo.save_state(T, sid, state, expected_version=1, status=status)
    return sid


def pending(state, text="Q?", kind="slot", slot=None):
    state.pending_question, state.pending_kind, state.pending_slot = text, kind, slot
    return state


# -- start ---------------------------------------------------------------------------------


async def test_start_creates_a_pinned_session_with_the_opener(world):
    engine, repo, llm, _ = world
    llm.queue("opener", q("What do you sell, and to whom?"))
    result = await started(world, role="CTO")
    assert result.question.kind == "opener" and result.status == "active"
    assert result.questions_asked == 1 and not result.icp_ready
    record = await repo.get_session(T, result.session_id)
    assert record.config_version == 1 and record.state.pending_question == result.question.text
    assert record.usage.input_tokens == 100  # the opener call was counted


async def test_start_copies_the_seed_without_overriding_what_the_user_stated(world):
    engine, repo, llm, enrichment = world
    seed = {
        "product": SlotValue(values=["Payroll"], confidence=0.5, source=SlotSource.INFERRED),
        "online_presence": SlotValue(
            values=["from-seed"], confidence=0.5, source=SlotSource.INFERRED
        ),
    }
    enrichment.outcome = EnrichmentOutcome(
        "ok", website="https://acme.test", pages=["https://acme.test"], seed=seed
    )
    llm.queue("opener", q("It looks like you sell payroll. Right?"))
    result = await started(world, website="acme.test")
    state = (await repo.get_session(T, result.session_id)).state
    assert (
        state.slots["product"].confidence == 0.5
        and state.slots["product"].source == SlotSource.INFERRED
    )
    assert state.slots["online_presence"].values == ["acme.test"]  # stated at signup, not the seed
    assert state.seed.keys() == seed.keys()
    [run] = await repo.list_enrichment(T, result.session_id)
    assert run.outcome == "ok" and run.website == "https://acme.test"


async def test_a_guard_can_refuse_a_start(world):
    engine, *_ = world

    class Refuse:
        async def check_start(self, tenant_id):
            raise PermissionError("daily cap")

    engine._guard = Refuse()
    with pytest.raises(PermissionError):
        await started(world)


async def test_the_live_config_is_created_on_first_use_and_reused(world):
    engine, repo, llm, _ = world
    llm.queue("opener", q("a?"), q("b?"))
    await started(world)
    await started(world)
    assert (await repo.live_config()).version == 1


# -- answering -------------------------------------------------------------------------------


async def test_the_answer_to_the_opener_is_followed_by_the_best_customer_question(world):
    engine, repo, llm, _ = world
    llm.queue("opener", q("Opener?"))
    first = await started(world)
    llm.queue("extraction", ext(up("product", ["Payroll"]), summary="B2B payroll"))
    llm.queue("question", q("Payroll for startups, nice. Who has been your best customer?"))
    result = await engine.answer(T, first.session_id, "We sell payroll to startups")
    assert result.question.text.startswith("Payroll for startups") and (
        result.question.kind == "best_customer"
    )
    assert "best customers" in llm.calls_for("question")[0].user
    assert result.questions_asked == 2
    state = (await repo.get_session(T, first.session_id)).state
    assert state.slots["product"].values == ["Payroll"] and state.summary == "B2B payroll"
    assert state.best_customer_asked and state.answers_received == 1


async def test_a_later_answer_gets_a_planned_slot_question_and_a_turn_record(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=2, best_customer_asked=True),
        "Best customers?",
        "best_customer",
    )
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("industries", ["Fintech"]), up("geographies", ["Europe"])))
    llm.queue("question", q("What problems do these banks have?"))
    result = await engine.answer(T, sid, "Banks in Europe")
    # industries and geographies are known, so the most important unknown slot comes next
    assert result.question.kind == "slot" and result.question.slot == "pain_points"
    assert result.question.options == ()
    saved = (await repo.get_session(T, sid)).state
    assert saved.asked_slots == {"pain_points": 1} and saved.questions_asked == 3
    [turn] = await repo.list_turns(T, sid)
    assert (
        turn.turn_no == 1
        and turn.question_text == "Best customers?"
        and turn.answer_text == "Banks in Europe"
    )
    assert turn.extracted["updates"][0]["slot"] == "industries"
    assert turn.usage.input_tokens == 200  # extraction plus question generation


async def test_the_interview_completes_when_tier_a_and_three_topics_are_done(world):
    engine, repo, llm, _ = world
    state = SessionState(questions_asked=7, best_customer_asked=True)
    for slot in SLOT_LIST:
        if slot.blocks:
            state.slots[slot.key] = SlotValue(
                values=["x"], confidence=0.9, source=SlotSource.STATED
            )
    state.asked_slots = {"tech_stack": 1, "deal_size": 1, "sales_cycle": 1}
    sid = await preset(repo, pending(state))
    llm.queue("extraction", ext())
    result = await engine.answer(T, sid, "fine")
    assert (result.status, result.stop_reason, result.icp_ready) == ("completed", "complete", True)
    assert result.question is None
    record = await repo.get_session(T, sid)
    assert record.status == "completed" and record.state.finished and record.ended_at is not None


async def test_the_quick_cap_ends_the_interview(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=10, best_customer_asked=True)))
    llm.queue("extraction", ext())
    result = await engine.answer(T, sid, "ok")
    assert (result.status, result.stop_reason) == ("completed", "cap")


async def test_a_request_to_finish_completes_the_interview(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=4, best_customer_asked=True)))
    llm.queue("extraction", ext(flags=Flags(wants_to_finish=True)))
    result = await engine.answer(T, sid, "that's enough, thanks")
    assert (result.status, result.stop_reason) == ("completed", "user_finished")


async def test_vague_answers_ask_the_question_writer_for_examples(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=3, best_customer_asked=True)))
    llm.queue("extraction", ext(flags=Flags(vague=True)))
    llm.queue("question", q("Could you give examples?"))
    await engine.answer(T, sid, "anyone")
    assert "two or three short examples" in llm.calls_for("question")[0].user


async def test_a_question_writer_failure_uses_the_fallback_text_and_the_interview_continues(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=3, best_customer_asked=True)))
    llm.queue("extraction", ext())
    llm.queue("question", LLMError("api", "down"))
    result = await engine.answer(T, sid, "hello")
    assert result.status == "active" and result.question.kind == "fallback"


# -- failures and misuse ---------------------------------------------------------------------------


async def test_one_extraction_failure_asks_again_and_two_in_a_row_offer_the_form(world):
    engine, repo, llm, _ = world
    sid = await preset(
        repo,
        pending(
            SessionState(questions_asked=3, best_customer_asked=True),
            "Who buys?",
            "slot",
            "buyer_roles",
        ),
    )
    llm.queue("extraction", LLMError("api", "x"), LLMError("api", "y"))
    first = await engine.answer(T, sid, "CTOs")
    assert first.status == "active" and first.question.text == RETRY_PREFIX + "Who buys?"
    assert first.questions_asked == 3  # a retry is not a new question
    second = await engine.answer(T, sid, "CTOs")
    assert (second.status, second.fallback_form, second.stop_reason) == (
        "fallback",
        True,
        "model_unavailable",
    )
    assert not second.icp_ready


async def test_a_success_resets_the_failure_count(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=3, best_customer_asked=True)))
    llm.queue("extraction", LLMError("api", "x"), ext(), LLMError("api", "z"))
    llm.queue("question", q("Next?"))
    await engine.answer(T, sid, "a")
    await engine.answer(T, sid, "b")
    third = await engine.answer(T, sid, "c")
    assert third.status == "active"  # failure, success, failure: never two in a row


async def test_off_topic_is_redirected_once_then_the_form_is_offered(world):
    engine, repo, llm, _ = world
    sid = await preset(
        repo,
        pending(
            SessionState(questions_asked=3, best_customer_asked=True),
            "Who buys?",
            "slot",
            "buyer_roles",
        ),
    )
    llm.queue("extraction", ext(flags=Flags(off_topic=True)), ext(flags=Flags(off_topic=True)))
    first = await engine.answer(T, sid, "what's the weather")
    assert first.status == "active" and first.question.text == REDIRECT_PREFIX + "Who buys?"
    second = await engine.answer(T, sid, "tell me a joke")
    assert (second.status, second.stop_reason) == ("fallback", "off_topic")


async def test_a_bare_no_is_re_asked_once_politely_then_we_move_on(world):
    engine, repo, llm, _ = world
    sid = await preset(
        repo,
        pending(
            SessionState(questions_asked=3, best_customer_asked=True),
            "Who buys?",
            "slot",
            "buyer_roles",
        ),
    )
    llm.queue("extraction", ext(), ext())
    llm.queue("question", q("Next?"))
    first = await engine.answer(T, sid, "no")
    assert first.status == "active" and first.question.text == DECLINE_PREFIX + "Who buys?"
    second = await engine.answer(T, sid, "no")
    assert second.question.text != DECLINE_PREFIX + "Who buys?"  # asked once more, then moved on


@pytest.mark.parametrize("text", ["", "   ", "x" * 2001])
async def test_empty_and_overlong_answers_are_rejected_before_any_model_call(world, text):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=3)))
    with pytest.raises(InvalidAnswer):
        await engine.answer(T, sid, text)
    assert llm.calls == []


async def test_closed_sessions_and_other_tenants_are_refused(world):
    engine, repo, llm, _ = world
    done = await preset(repo, SessionState(finished=True), status="completed")
    with pytest.raises(SessionClosed):
        await engine.answer(T, done, "more")
    with pytest.raises(NotFound):
        await engine.answer("someone-else", done, "more")


async def test_two_answers_submitted_at_once_cannot_both_be_applied(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=3, best_customer_asked=True)))
    llm.queue("extraction", ext(), ext())
    llm.queue("question", q("one?"), q("two?"))
    results = await asyncio.gather(
        engine.answer(T, sid, "first"), engine.answer(T, sid, "second"), return_exceptions=True
    )
    assert sum(isinstance(r, Conflict) for r in results) == 1
    assert (await repo.get_session(T, sid)).state.answers_received == 1


async def test_a_session_keeps_the_config_version_it_started_with(world):
    engine, repo, llm, _ = world
    llm.queue("opener", q("Opener?"))
    first = await started(world)
    changed = default_config(SETTINGS, version=2)
    changed.prompts["question"] = "NEW PROMPT"
    await repo.publish_config(changed, author="dev")
    llm.queue("extraction", ext())
    llm.queue("question", q("Next?"))
    await engine.answer(
        T, first.session_id, "hello"
    )  # opener answer: best-customer question, no model call
    llm.queue("extraction", ext())
    llm.queue("question", q("Next?"))
    await engine.answer(T, first.session_id, "hello again")
    assert llm.calls_for("question")[0].system != "NEW PROMPT"
    assert (await repo.get_session(T, first.session_id)).config_version == 1


# -- detail mode and finishing ---------------------------------------------------------------------


async def completed_session(repo, questions_asked=8):
    state = SessionState(
        questions_asked=questions_asked,
        best_customer_asked=True,
        finished=True,
        stop_reason="complete",
    )
    for slot in SLOT_LIST:
        if slot.blocks:
            state.slots[slot.key] = SlotValue(
                values=["x"], confidence=0.9, source=SlotSource.STATED
            )
    return await preset(repo, state, status="completed")


async def test_detail_mode_reopens_a_completed_session_with_a_tier_d_question(world):
    engine, repo, llm, _ = world
    sid = await completed_session(repo)
    llm.queue("question", q("Any compliance needs?"))
    result = await engine.request_detail(T, sid)
    assert result.status == "active" and result.mode == "detail"
    tiers = {s.key: s.tier for s in SLOT_LIST}
    assert tiers[result.question.slot] == Tier.D
    record = await repo.get_session(T, sid)
    assert record.status == "active" and record.state.detail_started_at == 8
    assert [e.type for e in await repo.list_feedback(T, sid)] == ["detail_mode_start"]
    with pytest.raises(InvalidTransition):
        await engine.request_detail(T, sid)  # already active


async def test_detail_mode_ends_after_five_more_questions(world):
    engine, repo, llm, _ = world
    sid = await completed_session(repo, questions_asked=8)
    llm.queue("question", *[q(f"d{i}?") for i in range(10)])
    llm.queue("extraction", *[ext() for _ in range(10)])
    result = await engine.request_detail(T, sid)
    asked = 1
    while result.status == "active":
        result = await engine.answer(T, sid, "an answer")
        asked += 1 if result.question else 0
    assert result.stop_reason == "detail_quota" and result.icp_ready
    assert asked == 5  # the opening detail question plus four more answers' questions


async def test_detail_mode_is_refused_when_the_session_is_not_complete_or_the_cap_is_reached(world):
    engine, repo, llm, _ = world
    active = await preset(repo, pending(SessionState(questions_asked=3)))
    with pytest.raises(InvalidTransition):
        await engine.request_detail(T, active)
    capped = await completed_session(repo, questions_asked=15)
    with pytest.raises(InvalidTransition):
        await engine.request_detail(T, capped)


async def test_finish_completes_an_active_session_once(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=3)))
    result = await engine.finish(T, sid)
    assert (result.status, result.stop_reason) == ("completed", "user_finished")
    with pytest.raises(SessionClosed):
        await engine.finish(T, sid)


async def test_an_unanswered_opener_gets_one_follow_up_before_moving_on(world):
    engine, repo, llm, _ = world
    llm.queue("opener", q("Opener?"))
    first = await started(world)
    llm.queue("extraction", ext(flags=Flags(vague=True)))
    llm.queue("question", q("No problem. What do you sell, for example software or services?"))
    second = await engine.answer(T, first.session_id, "hard to say")
    assert second.question.kind == "opener" and "No problem" in second.question.text
    assert "did not answer" in llm.calls_for("question")[0].user
    llm.queue("extraction", ext(flags=Flags(vague=True)))
    llm.queue("question", q("Who are your best customers?"))
    third = await engine.answer(T, first.session_id, "hard to say")
    assert third.question.kind == "best_customer"  # only one follow-up per question


async def test_a_reply_that_fills_nothing_is_followed_up_not_skipped(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=3, best_customer_asked=True)))
    llm.queue("extraction", ext())
    llm.queue("question", q("Could you say a bit more?"))
    result = await engine.answer(T, sid, "hmm")
    assert result.question.text == "Could you say a bit more?"


async def test_a_slot_asked_again_is_written_as_a_follow_up_with_a_different_fallback(world):
    engine, repo, llm, _ = world
    state = pending(SessionState(questions_asked=3, best_customer_asked=True))
    state.asked_slots = {slot.key: 1 for slot in SLOT_LIST}
    sid = await preset(repo, state)
    llm.queue("extraction", ext(flags=Flags(vague=True)))
    llm.queue("question", LLMError("api", "down"))
    result = await engine.answer(T, sid, "hard to say")
    assert result.question.text.startswith("No problem, let me put it another way")


async def test_a_pain_point_volunteered_in_the_best_customer_answer_is_not_yet_known(world):
    engine, repo, llm, _ = world
    state = pending(SessionState(questions_asked=2, best_customer_asked=True), kind="best_customer")
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("pain_points", ["price sensitivity"], 0.9)))
    llm.queue("question", q("And who signs off?"))
    await engine.answer(T, sid, "the tiny shop kept haggling on price")
    recorded = (await repo.get_session(T, sid)).state.slots["pain_points"]
    assert recorded.confidence < 0.7


async def test_a_question_from_the_user_is_answered_and_the_waiting_question_is_asked_again(world):
    engine, repo, llm, _ = world
    sid = await preset(
        repo,
        pending(
            SessionState(questions_asked=3, best_customer_asked=True),
            "Who buys?",
            "slot",
            "buyer_roles",
        ),
    )
    llm.queue(
        "extraction",
        ext(flags=Flags(asks_bot="do you know GDPR?")),
        ext(flags=Flags(asks_bot="what is an ICP?")),
    )
    llm.queue(
        "question",
        QuestionOutput(question="Who usually buys it?", reply="Yes, it is the EU privacy law."),
    )
    result = await engine.answer(T, sid, "do you know GDPR?")
    assert result.status == "active"
    assert result.question.text == "Yes, it is the EU privacy law. Who usually buys it?"
    assert result.question.slot == "buyer_roles"
    assert result.questions_asked == 3  # the reply did not use up a question
    record = await repo.get_session(T, sid)
    assert record.state.pending_question == "Who buys?" and record.state.user_questions == 1
    assert "do you know GDPR?" in llm.calls_for("question")[0].user

    llm.queue("question", LLMError("timeout", "slow"))  # the model fails: fixed, polite text
    again = await engine.answer(T, sid, "what is an ICP?")
    assert again.status == "active" and again.question.text.endswith("Who buys?")


async def test_an_answer_that_also_asks_a_question_gets_the_reply_in_front_of_the_next_question(
    world,
):
    engine, repo, llm, _ = world
    sid = await preset(
        repo,
        pending(
            SessionState(questions_asked=3, best_customer_asked=True),
            "Who buys?",
            "slot",
            "industries",
        ),
    )
    llm.queue(
        "extraction",
        ext(up("industries", ["Banks"]), flags=Flags(asks_bot="do banks buy this a lot?")),
    )
    llm.queue("question", QuestionOutput(question="Which regions?", reply="Often, yes."))
    result = await engine.answer(T, sid, "Banks. Do banks buy this a lot?")
    assert result.question.text == "Often, yes. Which regions?"
