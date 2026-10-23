import asyncio

import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.slots import SLOT_LIST, STOP_TOPICS, Tier
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue, TurnPair
from icp_chatbot.engine.extraction import ExtractionResult, Flags, SlotUpdate, Thread
from icp_chatbot.engine.interview import (
    DECLINE_PREFIX,
    DEGRADED_NOTICE,
    REDIRECT_PREFIX,
    RETRY_NOTICE,
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


def ext(*updates, flags=None, summary="", thread=None):
    return ExtractionResult(
        updates=list(updates), flags=flags or Flags(), summary=summary, thread=thread
    )


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
    engine = InterviewEngine(
        repo=repo, llm=llm, enrichment=enrichment, settings=SETTINGS, vary_order=False
    )
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


async def test_a_site_read_before_is_reused_not_fetched_again(world):
    engine, repo, llm, enrichment = world
    seed = {"product": SlotValue(values=["Payroll"], confidence=0.5, source=SlotSource.INFERRED)}
    enrichment.outcome = EnrichmentOutcome(
        "ok",
        website="https://acme.test",
        pages=["https://acme.test"],
        seed=seed,
        usage=Usage(500, 50, 0, 0),
    )
    llm.queue("opener", q("It looks like you sell payroll. Right?"))
    llm.queue("opener", q("You sell payroll, correct?"))
    await started(world, website="acme.test")
    second = await started(world, website="https://acme.test")
    assert enrichment.calls == 1  # the second signup used the saved read
    record = await repo.get_session(T, second.session_id)
    assert record.state.slots["product"].values == ["Payroll"]
    assert record.usage.input_tokens == 100  # only the opener was paid for, not the site read
    [run] = await repo.list_enrichment(T, second.session_id)
    assert run.outcome == "ok" and run.seed["product"]["values"] == ["Payroll"]


async def test_another_site_or_a_failed_read_is_not_reused(world):
    engine, repo, llm, enrichment = world
    enrichment.outcome = EnrichmentOutcome("failed", website="https://acme.test", detail="timeout")
    llm.queue("opener", q("What do you sell?"))
    llm.queue("opener", q("What do you sell?"))
    llm.queue("opener", q("What do you sell?"))
    await started(world, website="acme.test")
    await started(world, website="acme.test")  # the first read failed: try the site again
    await started(world, website="other.test")
    assert enrichment.calls == 3


async def test_a_read_without_data_is_tried_again_and_a_partial_one_with_data_is_reused(world):
    engine, repo, llm, enrichment = world
    seed = {"product": SlotValue(values=["Payroll"], confidence=0.5, source=SlotSource.INFERRED)}
    for outcome in (
        EnrichmentOutcome("ok", website="https://acme.test", seed={}),  # read, found nothing
        EnrichmentOutcome("partial", website="https://acme.test", seed=seed),
    ):
        enrichment.outcome = outcome
        llm.queue("opener", q("What do you sell?"))
        await started(world, website="acme.test")
    assert enrichment.calls == 2
    llm.queue("opener", q("You sell payroll?"))
    third = await started(world, website="acme.test")
    assert enrichment.calls == 2  # the partial read with data was reused
    assert (await repo.get_session(T, third.session_id)).state.slots["product"].values == [
        "Payroll"
    ]


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
    llm.queue(
        "extraction",
        ext(
            up("industries", ["Fintech"]),
            up("geographies", ["Europe"]),
            up("product", ["Payments"]),
            up("business_model", ["B2B"]),
        ),
    )
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
    llm.queue("question", q("Thanks, I have what I need to build your profile."))
    result = await engine.answer(T, sid, "fine")
    assert (result.status, result.stop_reason, result.icp_ready) == ("completed", "complete", True)
    assert result.question.kind == "closing"  # written for this user, not a fixed line
    record = await repo.get_session(T, sid)
    assert record.status == "completed" and record.state.finished and record.ended_at is not None


async def test_the_quick_cap_ends_the_interview(world):
    engine, repo, llm, _ = world
    sid = await preset(
        repo,
        pending(
            SessionState(questions_asked=SETTINGS.quick_mode_question_cap, best_customer_asked=True)
        ),
    )
    llm.queue("extraction", ext())
    llm.queue("question", q("Thanks, I have what I need to build your profile."))
    result = await engine.answer(T, sid, "ok")
    assert (result.status, result.stop_reason) == ("completed", "cap")


async def test_a_request_to_finish_completes_the_interview(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=4, best_customer_asked=True)))
    llm.queue("extraction", ext(flags=Flags(wants_to_finish=True)))
    llm.queue("question", q("Thanks, I have what I need to build your profile."))
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
    assert first.status == "active" and first.question.text == "Who buys?"
    assert first.notice == RETRY_NOTICE and "not about what you wrote" in RETRY_NOTICE
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
    first = await engine.answer(T, sid, "I had pizza for lunch")
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
    llm.queue(
        "question", q("Understood. Who usually signs off on a project like yours?"), q("Next?")
    )
    first = await engine.answer(T, sid, "no")
    assert first.status == "active" and first.question.slot == "buyer_roles"
    assert first.question.text == "Understood. Who usually signs off on a project like yours?"
    assert DECLINE_PREFIX not in first.question.text  # written for this user, not canned
    second = await engine.answer(T, sid, "no")
    assert second.question.text == "Next?"  # asked once more, then moved on


async def test_the_re_ask_falls_back_to_the_fixed_text_when_the_model_fails(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True),
        "Who buys?",
        "slot",
        "buyer_roles",
    )
    sid = await preset(repo, state)
    llm.queue("extraction", ext())
    llm.queue("question", LLMError("api", "down"))
    first = await engine.answer(T, sid, "no")  # the writer fails
    assert first.question.text == DECLINE_PREFIX + "Who buys?"


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
        asked += 1 if result.question and result.status == "active" else 0
    assert result.stop_reason == "detail_quota" and result.icp_ready
    assert asked == 5  # the opening detail question plus four more answers' questions


async def test_detail_mode_is_refused_when_the_session_is_not_complete_or_the_cap_is_reached(world):
    engine, repo, llm, _ = world
    active = await preset(repo, pending(SessionState(questions_asked=3)))
    with pytest.raises(InvalidTransition):
        await engine.request_detail(T, active)
    capped = await completed_session(repo, questions_asked=SETTINGS.detail_mode_total_cap)
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
    assert result.question.text.startswith("Let me put that another way")


async def test_a_pain_point_volunteered_in_the_best_customer_answer_is_not_yet_known(world):
    engine, repo, llm, _ = world
    state = pending(SessionState(questions_asked=2, best_customer_asked=True), kind="best_customer")
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("pain_points", ["price sensitivity"], 0.9)))
    llm.queue("question", q("And who signs off?"))
    await engine.answer(T, sid, "the tiny shop kept haggling on price")
    recorded = (await repo.get_session(T, sid)).state.slots["pain_points"]
    assert recorded.confidence < 0.7


async def test_a_question_from_the_user_is_answered_and_the_waiting_question_asked_once_more(world):
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

    # asked again instead of answering: answer, then move on rather than repeat a third time
    llm.queue("question", QuestionOutput(question="What do you sell?", reply="It is a profile."))
    again = await engine.answer(T, sid, "what is an ICP?")
    assert again.status == "active" and again.question.text == "It is a profile. What do you sell?"
    assert again.question.slot != "buyer_roles" and again.questions_asked == 4
    record = await repo.get_session(T, sid)
    assert "buyer_roles" in record.state.skipped and record.state.user_questions == 2
    assert "what is an ICP?" in llm.calls_for("question")[1].user


async def test_the_reply_writer_failing_gives_a_fixed_polite_text_and_the_same_question(world):
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
    llm.queue("extraction", ext(flags=Flags(asks_bot="what is an ICP?")))
    llm.queue("question", LLMError("timeout", "slow"))
    again = await engine.answer(T, sid, "what is an ICP?")
    assert again.status == "active" and again.question.text.endswith("Who buys?")


async def test_a_question_the_model_marks_off_topic_is_still_answered_and_never_ends_the_chat(
    world,
):
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
    # the extraction model misses the question and calls it off topic, as seen live
    llm.queue("extraction", *[ext(flags=Flags(off_topic=True, vague=True))] * 3)
    llm.queue(
        "question",
        QuestionOutput(question="Who usually buys it?", reply="EXIM means export-import."),
        QuestionOutput(question="Who signs off?", reply="Moving goods across borders."),
        QuestionOutput(question="Who decides?", reply="Doing well, thanks."),
    )
    first = await engine.answer(T, sid, 'our name has "Exim" in it, let me know, what is the Exim')
    second = await engine.answer(T, sid, "what is the import export?")
    third = await engine.answer(T, sid, "how are you")
    assert [r.status for r in (first, second, third)] == ["active"] * 3
    assert first.question.text == "EXIM means export-import. Who usually buys it?"
    assert second.question.text == "Moving goods across borders. Who signs off?"
    assert third.question.text == "Doing well, thanks. Who decides?"
    asked = [c.user for c in llm.calls_for("question")]
    assert "The user asked you (data, answer it in reply): what is the Exim" in asked[0]
    assert "what is the import export?" in asked[1] and "how are you" in asked[2]
    record = await repo.get_session(T, sid)
    assert record.state.offtopic_count == 0 and record.state.user_questions == 3
    assert record.state.pending_question == second.question.text  # moved on once, no further


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


async def test_a_reply_that_skips_the_question_but_tells_us_something_else_moves_on(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True), "Which regions?", "slot",
        "geographies",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue(
        "extraction",
        ext(up("objections", ["fear of side effects"]), flags=Flags(vague=True)),
    )
    llm.queue("question", QuestionOutput(question="What do you sell?"))
    result = await engine.answer(T, sid, "some users fear side effects, so they do not trust us")
    assert result.question.slot != "geographies"  # no clarification of the skipped question
    record = await repo.get_session(T, sid)
    assert record.state.probes == {}
    assert "did not answer the question fully" not in llm.calls_for("question")[0].user


async def test_a_vague_answer_gets_clarifying_follow_ups_while_the_user_keeps_giving_more(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True), "Which industries?", "slot",
        "industries",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("industries", ["bigger companies"], 0.3)))
    llm.queue("question", QuestionOutput(question="Bigger how? Roughly how many people?"))
    result = await engine.answer(T, sid, "bigger companies mostly")
    assert result.question.slot == "industries" and "Bigger how" in result.question.text
    assert result.questions_asked == 3  # a clarification does not use up a question
    assert "did not answer the question fully" in llm.calls_for("question")[0].user

    for reply, confidence in (("banks i guess", 0.45), ("retail banks", 0.6)):  # still improving
        llm.queue("extraction", ext(up("industries", [reply], confidence)))
        llm.queue("question", q("Which kind exactly?"))
        result = await engine.answer(T, sid, reply)
        assert result.question.slot == "industries"
    assert (await repo.get_session(T, sid)).state.probes["industries"] == 3


async def test_clarifying_stops_when_the_user_gets_nowhere_twice(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True), "Which industries?", "slot",
        "industries",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("industries", ["companies"], 0.3)))
    llm.queue("question", q("Which kind of companies?"))
    await engine.answer(T, sid, "companies")  # probe 1
    llm.queue("extraction", ext(up("industries", ["companies"], 0.3)))
    llm.queue("question", q("Can you name one?"))
    result = await engine.answer(T, sid, "companies, you know")  # nothing new: one more try
    assert result.question.slot == "industries"
    llm.queue("extraction", ext(up("industries", ["companies"], 0.3)))
    llm.queue("question", q("Which regions?"))
    result = await engine.answer(T, sid, "just companies")  # nothing new again: let it go
    assert result.question.slot != "industries"


async def test_a_firm_refusal_moves_on_at_once_and_is_never_asked_again(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True), "Which industries?", "slot",
        "industries",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("extraction", ext(flags=Flags(refuses=True)))
    llm.queue("question", q("Understood. Which regions?"))
    result = await engine.answer(T, sid, "I'd rather not say")
    assert result.question.slot not in (None, "industries")
    assert "they declined, they do not know" in llm.calls_for("question")[0].user
    record = await repo.get_session(T, sid)
    assert record.state.skipped == ["industries"] and not record.state.probes
    assert "industries" not in {a for a in record.state.asked_slots if a != "industries"}
    from icp_chatbot.engine.planner import Ask, Caps, plan_next

    for _ in range(5):  # the planner never comes back to it
        record.state.questions_asked = 3
        plan = plan_next(record.state, Caps())
        assert not (isinstance(plan, Ask) and plan.slot == "industries")
        if isinstance(plan, Ask):
            record.state.asked_slots[plan.slot] = record.state.asked_slots.get(plan.slot, 0) + 1


async def test_a_refusal_of_the_opener_skips_its_follow_up(world):
    engine, repo, llm, _ = world
    llm.queue("opener", q("Opener?"))
    first = await started(world)
    llm.queue("extraction", ext(flags=Flags(refuses=True)))
    llm.queue("question", q("Understood. Who is a customer you like?"))
    second = await engine.answer(T, first.session_id, "skip this")
    assert second.question.kind == "best_customer"


async def test_an_answer_that_filled_other_slots_is_not_pressed_on_the_asked_one(world):
    engine, repo, llm, _ = world
    sid = await preset(
        repo,
        pending(SessionState(questions_asked=3, best_customer_asked=True), "Pains?", "slot", "pain_points"),
    )  # fmt: skip
    llm.queue("extraction", ext(up("buyer_roles", ["CFO"])))
    llm.queue("question", q("Which regions?"))
    result = await engine.answer(T, sid, "CFOs")
    assert result.question.slot != "pain_points"


async def test_the_best_customer_question_is_due_at_the_sessions_own_position(world):
    engine, repo, llm, _ = world
    state = pending(SessionState(questions_asked=1, best_customer_at=3), "Opener?", "opener")
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("product", ["Payroll"])))
    llm.queue("question", q("Which regions?"))
    second = await engine.answer(T, sid, "payroll")
    assert second.question.kind != "best_customer"  # not yet due: a planned topic comes first
    llm.queue("extraction", ext(up("geographies", ["Europe"])))
    llm.queue("question", q("Who is your best customer?"))
    third = await engine.answer(T, sid, "Europe")
    assert third.question.kind == "best_customer"


# -- following what the user brings up ------------------------------------------------------


def on_thread(run, question="Which customers hesitate?"):
    state = pending(
        SessionState(questions_asked=4, best_customer_asked=True), question, "thread", "objections"
    )
    state.thread_run = run
    state.thread_topic, state.thread_slot = TRUST.topic, TRUST.slot
    return state


TRUST = Thread(topic="some customers do not trust the products", slot="objections")


async def test_something_the_user_brings_up_about_customers_is_followed_with_curiosity(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True), "Which regions?", "slot",
        "geographies",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("objections", ["fear of side effects"]), thread=TRUST))
    llm.queue(
        "question",
        QuestionOutput(question="Which customers are the ones who hesitate?", options=["New"]),
    )
    result = await engine.answer(T, sid, "some users don't trust us, they fear side effects")
    assert result.question.kind == "thread" and result.question.slot == "objections"
    assert result.question.text == "Which customers are the ones who hesitate?"
    assert result.question.options == () and result.questions_asked == 4  # counts toward the cap
    record = await repo.get_session(T, sid)
    assert record.state.thread_run == 1 and "objections" not in record.state.asked_slots
    user = llm.calls_for("question")[0].user
    assert "The user brought up: some customers do not trust the products" in user


async def test_a_thread_is_followed_while_it_teaches_us_then_the_bot_bridges_back(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, on_thread(run=2))
    llm.queue(
        "extraction",
        ext(up("customer_profile", ["first-time buyers"]), thread=TRUST),
        ext(up("disqualifiers", ["skipped patch test"]), thread=TRUST),
    )
    llm.queue("question", q("What worries first-time buyers most?"), q("Which regions?"))
    second = await engine.answer(T, sid, "mostly first-time buyers")
    assert second.question.kind == "thread"
    third = await engine.answer(T, sid, "they never did a patch test")  # three in a row: enough
    assert third.question.kind == "slot" and third.question.text == "Which regions?"
    record = await repo.get_session(T, sid)
    assert record.state.thread_run == 0
    assert "bridge" in llm.calls_for("question")[1].user


async def test_a_thread_that_runs_dry_goes_back_to_our_own_questions(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, on_thread(run=1))
    llm.queue("extraction", ext(thread=TRUST))  # the answer taught us nothing new
    llm.queue("question", q("Which regions?"))
    result = await engine.answer(T, sid, "it varies a lot")
    assert result.question.kind == "slot"


async def test_a_bare_no_to_a_curious_follow_up_is_not_pushed(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, on_thread(run=1))
    llm.queue("extraction", ext())
    llm.queue("question", q("Which regions?"))
    result = await engine.answer(T, sid, "no")
    assert result.question.kind == "slot" and not result.question.text.startswith(DECLINE_PREFIX)
    record = await repo.get_session(T, sid)
    assert "objections" not in record.state.skipped


async def test_when_the_thread_writer_fails_the_interview_simply_goes_on(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True), "Which regions?", "slot",
        "geographies",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("objections", ["fear of side effects"]), thread=TRUST))
    llm.queue("question", LLMError("timeout", "slow"), q("Which industries?"))
    result = await engine.answer(T, sid, "some users don't trust us")
    assert result.question.kind == "slot" and result.question.text == "Which industries?"


async def test_a_thread_keeps_going_while_answers_teach_us_even_if_the_model_marks_none(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, on_thread(run=1))
    llm.queue("extraction", ext(up("customer_profile", ["new buyers with sensitive skin"])))
    llm.queue("question", q("What do sensitive-skin buyers ask before buying?"))
    result = await engine.answer(T, sid, "mostly new customers with sensitive skin")
    assert result.question.kind == "thread" and result.question.slot == "objections"
    assert f"The user brought up: {TRUST.topic}" in llm.calls_for("question")[0].user
    record = await repo.get_session(T, sid)
    assert record.state.thread_run == 2


async def test_coming_back_from_a_thread_forgets_it(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, on_thread(run=3))
    llm.queue("extraction", ext(up("customer_profile", ["new buyers"])))
    llm.queue("question", q("Which regions?"))
    result = await engine.answer(T, sid, "new buyers")
    assert result.question.kind == "slot"
    record = await repo.get_session(T, sid)
    assert (record.state.thread_run, record.state.thread_topic) == (0, "")


async def test_a_failed_question_writer_tells_the_user_it_is_a_service_problem(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True), "Which regions?", "slot",
        "geographies",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("geographies", ["Europe"])), ext(up("industries", ["Banks"])))
    llm.queue("question", LLMError("timeout", "slow"), q("Which roles buy?"))
    broken = await engine.answer(T, sid, "Europe")
    assert broken.question.kind == "fallback" and broken.notice == DEGRADED_NOTICE
    fine = await engine.answer(T, sid, "banks")
    assert fine.notice is None  # the notice only shows on the turn that was affected


async def test_a_healthy_turn_has_no_notice(world):
    engine, repo, llm, _ = world
    llm.queue("opener", q("What do you sell?"))
    assert (await started(world)).notice is None


async def test_a_user_who_does_not_know_is_not_asked_again_and_the_interview_moves_on(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True), "What hurdles do they face?",
        "slot", "pain_points",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("extraction", ext(flags=Flags(vague=True, unsure=True)))
    llm.queue("question", q("Which regions?"))
    result = await engine.answer(T, sid, "even I am not aware about this")
    assert result.question.slot != "pain_points" and result.questions_asked == 4
    record = await repo.get_session(T, sid)
    assert "pain_points" in record.state.skipped and record.state.probes == {}
    user = llm.calls_for("question")[0].user
    assert "they do not know" in user and "did not answer the question fully" not in user


@pytest.mark.parametrize("text", ["not sure", "I don't know", "no idea", "idk"])
async def test_a_bare_dont_know_moves_on_without_a_second_ask(world, text):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=3, best_customer_asked=True), "Who buys?", "slot",
        "buyer_roles",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("extraction", ext(flags=Flags(vague=True)))  # even if the model marks nothing
    llm.queue("question", q("Which regions?"))
    result = await engine.answer(T, sid, text)
    assert result.question.slot != "buyer_roles"
    assert not result.question.text.startswith(DECLINE_PREFIX)


async def test_a_question_asked_as_the_interview_ends_is_still_answered(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=SETTINGS.quick_mode_question_cap, best_customer_asked=True), "Batch sizes?", "slot",
        "geographies",
    )  # fmt: skip
    state.last_turns = [TurnPair(question="Batch sizes?", answer="what is MOQ?")]
    sid = await preset(repo, state)  # the same question was already put once: we move on
    llm.queue("extraction", ext(flags=Flags(asks_bot="what is MOQ?")))
    llm.queue(
        "question",
        QuestionOutput(question="Thanks, that is all I need.", reply="MOQ is the smallest order."),
    )
    result = await engine.answer(T, sid, "what is MOQ?")
    assert result.status == "completed" and result.icp_ready
    assert result.question.text == "MOQ is the smallest order. Thanks, that is all I need."
    assert "what is MOQ?" in llm.calls_for("question")[0].user


async def test_finishing_gets_a_closing_line_written_for_the_user(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=SETTINGS.quick_mode_question_cap, best_customer_asked=True), "Who buys?", "slot",
        "geographies",
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("geographies", ["Europe"])))
    llm.queue("question", q("Thanks, I have what I need to build your profile."))
    result = await engine.answer(T, sid, "Europe")
    assert result.status == "completed" and result.question.kind == "closing"


async def test_the_interview_ends_when_the_details_are_known_long_before_the_cap(world):
    engine, repo, llm, _ = world
    state = pending(SessionState(questions_asked=5, best_customer_asked=True), "Q?", "slot", "x")
    for slot in SLOT_LIST:
        if slot.tier == Tier.A and not slot.skip_if:
            state.slots[slot.key] = SlotValue(
                values=["x"], confidence=0.9, source=SlotSource.STATED
            )
    state.slots["business_model"] = SlotValue(
        values=["B2B"], confidence=0.9, source=SlotSource.STATED
    )
    for slot in SLOT_LIST:
        if slot.skip_if:
            state.slots[slot.key] = SlotValue(
                values=["x"], confidence=0.9, source=SlotSource.STATED
            )
    topics = {}  # one slot for each of three stop topics counts as asked
    for slot in SLOT_LIST:
        if slot.topic in STOP_TOPICS and slot.topic not in topics:
            topics[slot.topic] = slot.key
    state.asked_slots = {key: 1 for key in list(topics.values())[:3]}
    sid = await preset(repo, state)
    llm.queue("extraction", ext(up("product", ["payroll"])))
    llm.queue("question", q("Thanks, I have what I need to build your profile."))
    result = await engine.answer(T, sid, "payroll")
    assert (result.status, result.stop_reason) == ("completed", "complete")
    assert result.questions_asked < SETTINGS.quick_mode_question_cap


def stated(state, key, *values, confidence=0.9, source=SlotSource.STATED):
    state.slots[key] = SlotValue(values=list(values), confidence=confidence, source=source)


async def test_a_user_who_contradicts_themselves_is_asked_which_one_is_right(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=4, best_customer_asked=True), "Who buys?", "slot",
        "buyer_roles",
    )  # fmt: skip
    stated(state, "business_model", "B2C", confidence=0.6)
    sid = await preset(repo, state)
    flags = Flags(contradiction_slots=["business_model"])
    llm.queue("extraction", ext(up("business_model", ["B2B"]), flags=flags))
    llm.queue("question", q("You first said B2C and now B2B. Which is right?"))
    result = await engine.answer(T, sid, "we sell to hospitals, so B2B")
    assert result.question.kind == "clarify" and result.question.slot == "business_model"
    assert result.question.options == ("B2C", "B2B") and result.questions_asked == 4
    user = llm.calls_for("question")[0].user
    assert "said earlier: B2C" in user and "says now: B2B" in user
    record = await repo.get_session(T, sid)
    assert record.state.clarified == ["business_model"]
    assert record.state.pending_kind == "clarify"

    # their answer settles it, and even if the model flags it again we do not ask twice
    llm.queue("extraction", ext(up("business_model", ["B2B"]), flags=flags))
    llm.queue("question", q("Which regions?"))
    after = await engine.answer(T, sid, "B2B")
    assert after.question.kind != "clarify"


async def test_correcting_a_value_we_only_guessed_is_not_a_contradiction(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=4, best_customer_asked=True), "Who buys?", "slot",
        "buyer_roles",
    )  # fmt: skip
    stated(state, "business_model", "B2C", confidence=0.5, source=SlotSource.INFERRED)
    sid = await preset(repo, state)
    flags = Flags(contradiction_slots=["business_model"])
    llm.queue("extraction", ext(up("business_model", ["B2B"]), flags=flags))
    llm.queue("question", q("Which regions?"))
    result = await engine.answer(T, sid, "B2B")
    assert result.question.kind != "clarify"


async def test_revise_reopens_a_completed_session_and_takes_the_note_as_an_answer(world):
    engine, repo, llm, _ = world
    sid = await completed_session(repo)
    llm.queue("extraction", ext(up("product", ["Payroll"])))
    llm.queue("question", q("Anything else to add?"))
    result = await engine.revise(T, sid, "We sell payroll, not HR software")
    record = await repo.get_session(T, sid)
    assert record.state.slots["product"].values == ["Payroll"]
    assert result.status in ("active", "completed")
    assert "revision_requested" in [e.type for e in await repo.list_feedback(T, sid)]


async def test_a_confidential_refusal_of_the_best_customer_asks_for_an_overview_once(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=2, best_customer_asked=True),
        "Who is one client you would clone?",
        "best_customer",
    )
    sid = await preset(repo, state)
    llm.queue("extraction", ext(flags=Flags(refuses=True)))
    llm.queue("question", q("Understood. Just tell me the kind of client you like."))
    result = await engine.answer(T, sid, "it is confidential and I cannot share it")
    assert result.status == "active" and result.question.kind == "best_customer"
    assert "confidential" in llm.calls_for("question")[0].user
    llm.queue("extraction", ext(flags=Flags(refuses=True)))
    llm.queue("question", q("Fine. Which industries?"))
    again = await engine.answer(T, sid, "still no")
    assert again.question.text == "Fine. Which industries?"  # asked once, then it moves on


async def test_a_vague_revision_note_gets_one_clarifying_question_before_rebuilding(world):
    engine, repo, llm, _ = world
    sid = await completed_session(repo)
    llm.queue("extraction", ext())
    result = await engine.revise(T, sid, "the ICP did not include every needed aspect")
    assert result.status == "active" and result.question.kind == "revision"
    llm.queue("question", q("Anything else to add?"))
    llm.queue("extraction", ext(up("product", ["Payroll"])))
    done = await engine.answer(T, sid, "add our payroll product")
    record = await repo.get_session(T, sid)
    assert record.state.slots["product"].values == ["Payroll"]
    assert done.status in ("active", "completed")


async def test_refusing_an_exact_figure_asks_for_a_rough_range_once(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=4, best_customer_asked=True),
        "What is the typical contract value?",
        "slot",
        "deal_size",
    )
    sid = await preset(repo, state)
    llm.queue("extraction", ext(flags=Flags(refuses=True)), ext(flags=Flags(refuses=True)))
    llm.queue("question", q("No exact number needed. Roughly which range?"), q("Next?"))
    first = await engine.answer(T, sid, "cannot say this")
    assert first.question.slot == "deal_size" and "range" in first.question.text
    assert "rough range" in llm.calls_for("question")[0].user
    second = await engine.answer(T, sid, "still no")
    assert second.question.text == "Next?"  # asked for a range once, then moved on


async def test_a_stated_correction_replaces_the_value_without_asking_which_is_right(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=4, best_customer_asked=True), "Which industries?", "slot",
        "industries",
    )  # fmt: skip
    state.slots["revenue_range"] = SlotValue(
        values=["$1M-$10M"], confidence=0.9, source=SlotSource.STATED
    )
    sid = await preset(repo, state)
    llm.queue(
        "extraction",
        ext(
            up("revenue_range", ["<$1M"]),
            flags=Flags(contradiction_slots=["revenue_range"]),
        ),
    )
    llm.queue("question", q("Which regions?"))
    result = await engine.answer(T, sid, "sorry, less than 1M")
    record = await repo.get_session(T, sid)
    assert record.state.slots["revenue_range"].values == ["<$1M"]
    assert result.question.kind != "clarify" and not record.state.clarified


async def test_an_ambiguous_change_still_asks_which_value_is_right(world):
    engine, repo, llm, _ = world
    state = pending(
        SessionState(questions_asked=4, best_customer_asked=True), "Which industries?", "slot",
        "industries",
    )  # fmt: skip
    state.slots["revenue_range"] = SlotValue(
        values=["$1M-$10M"], confidence=0.9, source=SlotSource.STATED
    )
    sid = await preset(repo, state)
    llm.queue(
        "extraction",
        ext(up("revenue_range", ["<$1M"]), flags=Flags(contradiction_slots=["revenue_range"])),
    )
    llm.queue("question", q("Earlier you said $1M-$10M, now under $1M. Which is right?"))
    result = await engine.answer(T, sid, "actually under 1M for some of them")
    assert result.question.kind == "clarify"
