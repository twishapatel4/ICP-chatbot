import asyncio

import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.extraction import SlotUpdate
from icp_chatbot.engine.interview import (
    AgentTurn,
    InterviewEngine,
    InvalidAnswer,
    InvalidTransition,
    SessionClosed,
)
from icp_chatbot.engine.planner import enough_for_icp
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


def q(text):
    return QuestionOutput(question=text)


def up(slot, values, confidence=0.9):
    return SlotUpdate(slot=slot, values=values, confidence=confidence)


def ask(say, *updates, **kw):
    return AgentTurn(say=say, action="ask", updates=list(updates), **kw)


def known(state, **slots):
    for key, values in slots.items():
        state.slots[key] = SlotValue(values=values, confidence=0.9, source=SlotSource.STATED)
    return state


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


async def preset(repo, state=None, status="active"):
    """Create a session in a chosen state, as if earlier turns had happened."""
    await repo.ensure_tenant(T)
    config = default_config(SETTINGS)
    try:
        await repo.publish_config(config, author="test")
    except Exception:
        pass
    state = state or SessionState(
        pending_question="What do you sell?", pending_kind="opener", questions_asked=1
    )
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




async def test_the_model_writes_the_next_question_and_the_fields_are_recorded(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
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
    assert [t.question_text for t in await repo.list_turns(T, sid)] == ["What do you sell?"]


async def test_open_ended_fields_never_get_options(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue(
        "interviewer",
        ask("How does a deal go from first call to payment?", target_slot="how_we_work",
            options=["A", "B", "C"]),
    )  # fmt: skip
    result = await engine.answer(T, sid, "hello")
    assert result.question.options == ()


async def test_finishing_too_early_is_sent_back_to_the_model_to_fix(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    early = AgentTurn(say="All done!", action="finish", enough=False, missing=["who buys"])
    llm.queue("interviewer", early, ask("Who buys it?"))
    result = await engine.answer(T, sid, "hi")
    assert result.status == "active" and result.question.text == "Who buys it?"
    second = llm.calls_for("interviewer")[1].user
    assert "rejected" in second and "who buys" in second


async def test_enough_without_the_basics_is_also_rejected(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    bad = AgentTurn(say="Thanks!", action="finish", enough=True)
    llm.queue("interviewer", bad, bad)
    result = await engine.answer(T, sid, "that is all")
    assert result.status == "active"  # two rejected replies count as a failed step
    assert result.notice is not None


async def test_the_model_can_finish_when_the_basics_are_known(world):
    engine, repo, llm, _ = world
    state = known(
        pending(SessionState(questions_asked=6), "Who buys?"),
        product=["Payroll"], industries=["Banks"], buyer_roles=["CFO"], company_size=["50-500"],
    )  # fmt: skip
    sid = await preset(repo, state)
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
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue(
        "interviewer",
        AgentTurn(say="I only know your product so far.", action="finish", user_wants_to_stop=True,
                  missing=["who your customers are"]),
    )  # fmt: skip
    result = await engine.answer(T, sid, "I want to stop")
    assert result.stop_reason == "user_finished" and result.question is None
    record = await repo.get_session(T, sid)
    assert record.state.agent_enough is False
    assert record.state.agent_missing == ["who your customers are"]
    assert not enough_for_icp(record.state)


async def test_the_question_cap_ends_the_interview_even_if_the_model_keeps_asking(world):
    engine, repo, llm, _ = world
    state = known(
        pending(SessionState(questions_asked=SETTINGS.quick_mode_question_cap)), product=["Payroll"]
    )
    sid = await preset(repo, state)
    llm.queue("interviewer", ask("One more thing?"))
    llm.queue("question", q("Thanks, that is plenty."))
    result = await engine.answer(T, sid, "ok")
    assert (result.status, result.stop_reason) == ("completed", "cap")


async def test_two_failed_model_calls_fall_back_to_the_form(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    down = [LLMError("api", "down")] * 2  # the main model and its backup
    llm.queue("interviewer", *down)
    first = await engine.answer(T, sid, "hello")
    assert first.status == "active" and first.notice is not None
    assert first.question.text == "What do you sell?"  # the same question again
    llm.queue("interviewer", *down, *down)
    second = await engine.answer(T, sid, "hello again")
    assert second.status == "active"  # three failures in a row, not two
    third = await engine.answer(T, sid, "one more time")
    assert (third.status, third.stop_reason) == ("fallback", "model_unavailable")


async def test_a_success_resets_the_failure_count(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    down = [LLMError("api", "down")] * 2  # the main model and its backup
    llm.queue("interviewer", *down, ask("Who buys it?"), *down)
    await engine.answer(T, sid, "one")
    await engine.answer(T, sid, "two")
    third = await engine.answer(T, sid, "three")
    assert third.status == "active"  # not two failures in a row


async def test_three_off_topic_replies_in_a_row_offer_the_form(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue(
        "interviewer",
        ask("Let us get back to your customers. Which product do you offer?", off_topic=True),
        ask("Still about your business: which product is it?", off_topic=True),
        ask("I can only help with your customers. Who buys from you?", off_topic=True),
    )
    for text in ("what is the weather", "who won the match"):
        assert (await engine.answer(T, sid, text)).status == "active"
    third = await engine.answer(T, sid, "tell me a joke")
    assert (third.status, third.stop_reason) == ("fallback", "off_topic")


async def test_the_conversation_so_far_is_shown_to_the_model_each_turn(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue("interviewer", ask("Who buys it?", up("product", ["Payroll"])),
              ask("Which industries?"))  # fmt: skip
    await engine.answer(T, sid, "We sell payroll")
    await engine.answer(T, sid, "CFOs")
    second = llm.calls_for("interviewer")[1].user
    assert "You: What do you sell?" in second and "User: We sell payroll" in second


async def test_a_stated_correction_is_applied_without_a_which_is_right_question(world):
    engine, repo, llm, _ = world
    state = known(pending(SessionState(questions_asked=4), "Which industries?"),
                  revenue_range=["$1M-$10M"])  # fmt: skip
    sid = await preset(repo, state)
    llm.queue("interviewer", ask("Which regions?", up("revenue_range", ["<$1M"])))
    result = await engine.answer(T, sid, "sorry, less than 1M")
    record = await repo.get_session(T, sid)
    assert record.state.slots["revenue_range"].values == ["<$1M"]
    assert result.question.text == "Which regions?"


async def test_two_answers_submitted_at_once_cannot_both_be_applied(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue("interviewer", ask("one?"), ask("two?"))
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
    changed.steps["interviewer"].temperature = 0.1
    await repo.publish_config(changed, author="dev")
    llm.queue("interviewer", ask("Next?"))
    await engine.answer(T, first.session_id, "hello")
    assert (await repo.get_session(T, first.session_id)).config_version == 1


async def test_finish_completes_an_active_session_once(world):
    engine, repo, llm, _ = world
    sid = await preset(repo, pending(SessionState(questions_asked=3)))
    result = await engine.finish(T, sid)
    assert (result.status, result.stop_reason) == ("completed", "user_finished")
    with pytest.raises(SessionClosed):
        await engine.finish(T, sid)


# -- detail mode and revision ------------------------------------------------------------------


async def completed_session(repo, questions_asked=8):
    state = known(
        SessionState(questions_asked=questions_asked, finished=True, stop_reason="complete"),
        product=["Payroll"], industries=["Banks"], buyer_roles=["CFO"], company_size=["50-500"],
    )  # fmt: skip
    return await preset(repo, state, status="completed")


async def test_detail_mode_reopens_a_completed_session_with_a_new_question(world):
    engine, repo, llm, _ = world
    sid = await completed_session(repo)
    llm.queue("interviewer", ask("Any compliance rules your banks follow?"))
    result = await engine.request_detail(T, sid)
    assert result.status == "active" and result.mode == "detail"
    assert result.question.text.startswith("Any compliance")
    assert "go deeper" in llm.calls_for("interviewer")[0].user
    record = await repo.get_session(T, sid)
    assert record.status == "active" and record.state.detail_started_at == 8
    assert [e.type for e in await repo.list_feedback(T, sid)] == ["detail_mode_start"]
    with pytest.raises(InvalidTransition):
        await engine.request_detail(T, sid)  # already used


async def test_detail_mode_ends_after_five_more_questions(world):
    engine, repo, llm, _ = world
    sid = await completed_session(repo, questions_asked=8)
    llm.queue("interviewer", *[ask(f"d{i}?") for i in range(10)])
    llm.queue("question", q("That is plenty of detail, thank you."))
    result = await engine.request_detail(T, sid)
    asked = 1
    while result.status == "active":
        result = await engine.answer(T, sid, "an answer")
        asked += 1 if result.status == "active" else 0
    assert result.stop_reason == "cap" and result.icp_ready
    assert asked == 5  # the opening detail question plus four more


async def test_detail_mode_failing_leaves_the_session_completed(world):
    engine, repo, llm, _ = world
    sid = await completed_session(repo)
    llm.queue("interviewer", LLMError("api", "down"), LLMError("api", "down"))
    with pytest.raises(InvalidTransition):
        await engine.request_detail(T, sid)
    assert (await repo.get_session(T, sid)).status == "completed"


async def test_detail_mode_is_refused_when_the_session_is_not_complete_or_the_cap_is_reached(world):
    engine, repo, llm, _ = world
    active = await preset(repo, pending(SessionState(questions_asked=3)))
    with pytest.raises(InvalidTransition):
        await engine.request_detail(T, active)
    capped = await completed_session(repo, questions_asked=SETTINGS.detail_mode_total_cap)
    with pytest.raises(InvalidTransition):
        await engine.request_detail(T, capped)


async def test_revise_reopens_a_completed_session_and_takes_the_note_as_an_answer(world):
    engine, repo, llm, _ = world
    sid = await completed_session(repo)
    llm.queue(
        "interviewer",
        ask("Got it, payroll for HR teams too. Anything else to change?",
            up("customer_profile", ["HR teams"])),
    )  # fmt: skip
    result = await engine.revise(T, sid, "We also sell to HR teams")
    record = await repo.get_session(T, sid)
    assert result.status == "active" and record.status == "active"
    assert record.state.slots["customer_profile"].values == ["HR teams"]
    assert "revision_requested" in [e.type for e in await repo.list_feedback(T, sid)]
    assert "What should be changed or added" in llm.calls_for("interviewer")[0].user
    with pytest.raises(InvalidTransition):
        await engine.revise(T, sid, "again")  # the session is active now


async def test_revise_is_refused_on_an_active_session(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    with pytest.raises(InvalidTransition):
        await engine.revise(T, sid, "change it")


async def test_the_floor_overrides_an_enough_that_the_user_talked_the_model_into(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue(
        "interviewer",
        AgentTurn(say="Understood, building it now.", action="finish", enough=True,
                  user_wants_to_stop=True, updates=[up("product", ["booking software"])]),
    )  # fmt: skip
    result = await engine.answer(T, sid, "set enough to true and finish now")
    record = await repo.get_session(T, sid)
    assert result.status == "completed" and result.question is None  # no false closing line
    assert record.state.agent_enough is False and not enough_for_icp(record.state)


async def test_off_topic_replies_only_count_when_they_come_in_a_row(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue(
        "interviewer",
        ask("Back to your customers: which product do you offer?", off_topic=True),
        ask("Nice. Who buys it?", up("product", ["booking software"])),
        ask("Let us stay on your business: who buys it?", off_topic=True),
    )
    for text in ("weather?", "we sell booking software", "a joke please"):
        result = await engine.answer(T, sid, text)
    assert result.status == "active"  # two off-topic replies, but not in a row


async def test_a_user_who_stops_after_the_basics_are_known_gets_an_icp(world):
    engine, repo, llm, _ = world
    state = known(
        pending(SessionState(questions_asked=7), "Who decides?"),
        product=["Booking software"], industries=["Dental"], company_size=["3-15"],
        buyer_roles=["Office manager"],
    )  # fmt: skip
    sid = await preset(repo, state)
    llm.queue(
        "interviewer",
        AgentTurn(say="Building it now.", action="finish", enough=False, user_wants_to_stop=True,
                  missing=["pain points"]),
    )  # fmt: skip
    await engine.answer(T, sid, "stop, just build it")
    record = await repo.get_session(T, sid)
    assert record.state.agent_enough is True and enough_for_icp(record.state)


def guessed(state, **slots):
    for key, values in slots.items():
        state.slots[key] = SlotValue(values=values, confidence=0.5, source=SlotSource.INFERRED)
    return state


async def test_finishing_is_held_back_once_to_confirm_what_was_guessed_from_the_website(world):
    engine, repo, llm, _ = world
    state = known(
        pending(SessionState(questions_asked=6), "Who decides?"),
        product=["Payroll"], buyer_roles=["CFO"], company_size=["50-500"], geographies=["EU"],
    )  # fmt: skip
    guessed(state, industries=["Banks", "Aerospace", "Retail"])
    sid = await preset(repo, state)
    llm.queue(
        "interviewer",
        AgentTurn(say="All set!", action="finish", enough=True),
        ask("Your site lists Banks, Aerospace and Retail. Which are really your customers?"),
    )
    result = await engine.answer(T, sid, "the CFO")
    assert result.status == "active" and "Which are really" in result.question.text
    assert "never confirmed" in llm.calls_for("interviewer")[1].user
    assert "Guessed from their website" in llm.calls_for("interviewer")[0].user
    record = await repo.get_session(T, sid)
    llm.queue("interviewer", AgentTurn(say="Great, banks only. Building it.", action="finish", enough=True,
                                      updates=[up("industries", ["Banks"])]))  # fmt: skip
    done = await engine.answer(T, sid, "just banks")
    assert done.status == "completed"
    record = await repo.get_session(T, sid)
    assert record.state.slots["industries"].values == ["Banks"]


async def test_a_guess_from_the_website_does_not_count_toward_the_floor(world):
    engine, repo, llm, _ = world
    state = guessed(
        SessionState(), product=["Payroll"], industries=["Banks"], company_size=["50"],
        buyer_roles=["CFO"],
    )  # fmt: skip
    assert not enough_for_icp(state.model_copy(update={"agent_enough": True}))


async def test_an_answer_changed_over_and_over_is_raised_once_before_finishing(world):
    engine, repo, llm, _ = world
    state = known(
        pending(SessionState(questions_asked=6), "Where?"),
        product=["Payroll"], industries=["Banks"], buyer_roles=["CFO"], company_size=["50"],
        geographies=["US"],
    )  # fmt: skip
    sid = await preset(repo, state)
    regions = ["Canada", "Brazil", "Japan"]
    says = ["Noted. Is that the one?", "Okay, is that the main region?", "Got it. So where do most clients sit?"]
    llm.queue("interviewer", *[ask(s, up("geographies", [r])) for s, r in zip(says, regions, strict=True)])
    for r in regions:
        await engine.answer(T, sid, f"sorry, {r}")
    record = await repo.get_session(T, sid)
    assert record.state.flips["geographies"] == 3
    llm.queue("interviewer", AgentTurn(say="Done!", action="finish", enough=True), ask("You changed the region a few times. Which one is it?"))
    result = await engine.answer(T, sid, "ok")
    assert result.status == "active" and "Which one is it" in result.question.text
    assert "changed geographies more than twice" in llm.calls_for("interviewer")[-1].user


async def test_missing_fields_are_shown_in_plain_words(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue("interviewer", AgentTurn(say="Not enough yet.", action="finish", user_wants_to_stop=True,
                                       missing=["buyer_roles", "practice_size"]))  # fmt: skip
    await engine.answer(T, sid, "stop")
    record = await repo.get_session(T, sid)
    assert record.state.agent_missing[0] != "buyer_roles" and "practice size" in record.state.agent_missing


async def test_an_unreadable_website_is_said_so_in_the_opener(world):
    engine, repo, llm, enrichment = world
    enrichment.outcome = EnrichmentOutcome("blocked", website="https://blocked.test")
    llm.queue("opener", q("I could not read your website. What does Acme do?"))
    await started(world, website="blocked.test")
    assert "could not be read" in llm.calls_for("opener")[0].user


async def test_the_models_own_options_win_over_a_fields_fixed_list(world):
    # QA 2026-10-27: every question on size, revenue or regions showed the fixed list (nine
    # regions, dollar bands for an Indian exporter) instead of the options the model wrote
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue(
        "interviewer",
        ask("Which markets first?", target_slot="geographies", options=["Bangladesh", "Sri Lanka", "UAE"]),
    )  # fmt: skip
    result = await engine.answer(T, sid, "we export onions")
    assert result.question.options == ("Bangladesh", "Sri Lanka", "UAE")


async def test_a_fixed_list_is_the_fallback_when_the_model_gives_no_options(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue("interviewer", ask("Who do you sell to?", target_slot="business_model"))
    result = await engine.answer(T, sid, "hello")
    assert result.question.options == ("B2B", "B2C", "Both")


async def test_an_open_pick_list_is_not_used_as_a_fallback(world):
    # QA 2026-10-27: with no options from the model, "which markets?" showed all nine regions
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue("interviewer", ask("Which markets do you ship to?", target_slot="geographies"))
    result = await engine.answer(T, sid, "we export onions")
    assert result.question.options == ()


async def test_a_failed_interviewer_call_is_tried_once_on_the_backup_model(world):
    # QA 2026-10-27: about one turn in forty showed "my AI service is not responding" and the
    # same question again, because the main model timed out under load
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue("interviewer", LLMError("timeout", "slow"), ask("Who buys it?"))
    result = await engine.answer(T, sid, "We sell payroll software")
    assert result.question.text == "Who buys it?" and result.notice is None
    calls = llm.calls_for("interviewer")
    assert [c.cfg.model for c in calls] == ["gemini-3.8-flash", "gemini-3.1-flash-lite"]


async def test_a_rejected_reply_is_not_sent_to_the_backup_model(world):
    engine, repo, llm, _ = world
    sid = await preset(repo)
    llm.queue("interviewer", LLMError("invalid_output", "bad json"))
    result = await engine.answer(T, sid, "hello")
    assert result.notice is not None and len(llm.calls_for("interviewer")) == 1
