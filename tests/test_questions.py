import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue, TurnPair
from icp_chatbot.engine.questions import (
    BEST_CUSTOMER_QUESTION,
    FOUNDER_OPENER,
    OPENER_ANGLES,
    OPENER_VARIANTS,
    SALES_OPENER,
    Question,
    QuestionOutput,
    best_customer_question,
    clean_options,
    fallback_question,
    generate_opener,
    generate_question,
    generate_reply,
    generate_thread_question,
    pick_opener_variant,
)
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import LLMError, Usage
from icp_chatbot.versions import default_config

CONFIG = default_config(Settings(_env_file=None))


def q(text):
    return QuestionOutput(question=text)


def test_signup_domain_ignores_free_mail():
    assert SignupInfo("Acme", email="ann@acme.com").business_domain == "acme.com"
    assert SignupInfo("Acme", email="ann@gmail.com").business_domain is None
    assert SignupInfo("Acme", email="bad").business_domain is None
    assert not SignupInfo("Acme").has_context
    assert SignupInfo("Acme", role="CTO").has_context


def test_opener_variant_depends_on_role_then_deterministically_on_name():
    assert pick_opener_variant("Acme", "Co-Founder") == FOUNDER_OPENER
    assert pick_opener_variant("Acme", "VP Sales") == SALES_OPENER
    assert pick_opener_variant("Acme", None) == pick_opener_variant("acme ", "engineer")
    assert {pick_opener_variant(f"company {i}", None) for i in range(40)} == set(OPENER_VARIANTS)


def test_fallback_question_offers_fixed_choices():
    question = fallback_question("company_size")
    assert question.kind == "fallback" and question.slot == "company_size"
    assert "1-10" in question.options
    assert fallback_question("pain_points").options == ()


def test_best_customer_question_is_fixed():
    assert best_customer_question().text == BEST_CUSTOMER_QUESTION
    assert best_customer_question().kind == "best_customer"


async def test_generated_question_carries_options_usage_and_context():
    llm = FakeLLM(
        {"question": [q("  How big are the companies you sell to?  ")]}, Usage(900, 30, 0, 0)
    )
    state = SessionState(
        summary="B2B payroll", last_turns=[TurnPair(question="Q1", answer="We sell payroll")]
    )
    result = await generate_question(llm, CONFIG, state, "company_size", previous_vague=True)
    assert result.question.text == "How big are the companies you sell to?"
    assert result.question.options[0] == "1-10" and result.question.slot == "company_size"
    assert result.usage == Usage(900, 30, 0, 0) and not result.used_fallback
    user = llm.calls_for("question")[0].user
    assert "Target slot: Target company size" in user and "B2B payroll" in user
    assert "We sell payroll" in user and "two or three short examples" in user


async def test_coming_back_to_a_topic_asks_about_it_not_about_the_last_question():
    llm = FakeLLM({"question": [q("What do your customers want for their skin?")]})
    state = SessionState(
        asked_slots={"desired_outcomes": 1},
        last_turns=[TurnPair(question="How many units can you make?", answer="young age 22-35")],
    )
    await generate_question(llm, CONFIG, state, "desired_outcomes")
    user = llm.calls_for("question")[0].user
    assert "Target slot: Desired outcomes" in user
    assert "come back to the target slot" in user and "other topics" in user
    assert "did not answer the question" not in user and "No problem" not in user


async def test_a_question_from_the_user_is_answered_once_in_reply_not_again_in_the_question():
    llm = FakeLLM({"question": [QuestionOutput(question="Which regions?", reply="Fine, thanks.")]})
    result = await generate_question(
        llm, CONFIG, SessionState(), "geographies", asked="how are you"
    )
    assert result.question.text == "Fine, thanks. Which regions?"
    user = llm.calls_for("question")[0].user
    assert "The user asked you (data, answer it in reply): how are you" in user
    assert "never repeat that answer in question" in user


@pytest.mark.parametrize("bad", ["", "   ", "x" * 401])
async def test_unusable_model_text_falls_back(bad):
    llm = FakeLLM({"question": [q(bad)]})
    result = await generate_question(llm, CONFIG, SessionState(), "pain_points")
    assert result.used_fallback and result.question.kind == "fallback"


async def test_a_model_error_falls_back_without_raising():
    llm = FakeLLM({"question": [LLMError("api", "boom")]})
    result = await generate_question(llm, CONFIG, SessionState(), "pain_points")
    assert result.used_fallback and result.usage == Usage()


async def test_opener_uses_the_seed_as_hypotheses():
    seed = {
        "product": SlotValue(
            values=["Payroll software"], confidence=0.5, source=SlotSource.INFERRED
        )
    }
    llm = FakeLLM({"opener": [q("It looks like you sell payroll software. Is that right?")]})
    result = await generate_opener(llm, CONFIG, SignupInfo("Acme", role="CTO"), seed)
    assert result.question.kind == "opener" and "payroll" in result.question.text
    user = llm.calls_for("opener")[0].user
    assert "hypotheses, not facts" in user and "product: Payroll software" in user
    assert "Signer's role: CTO" in user


async def test_opener_with_nothing_known_is_still_written_by_the_model_with_an_angle():
    llm = FakeLLM({"opener": [QuestionOutput(question="Hi! How did Acme get started?")]})
    result = await generate_opener(
        llm, CONFIG, SignupInfo("Acme"), {}, angle="how the business started"
    )
    assert result.question.text == "Hi! How did Acme get started?" and not result.used_fallback
    assert "Angle to open with: how the business started" in llm.calls_for("opener")[0].user


async def test_opener_angle_is_chosen_at_random_when_not_given():
    llm = FakeLLM({"opener": [QuestionOutput(question="Hi!")]})
    await generate_opener(llm, CONFIG, SignupInfo("Acme"), {})
    assert any(a in llm.calls_for("opener")[0].user for a in OPENER_ANGLES)


async def test_opener_model_failure_falls_back_to_a_variant():
    llm = FakeLLM({"opener": [LLMError("timeout", "slow")]})
    result = await generate_opener(llm, CONFIG, SignupInfo("Acme", role="Founder"), {})
    assert result.question.text == FOUNDER_OPENER and result.used_fallback


def test_clean_options_trims_dedupes_and_needs_two():
    assert clean_options([" Fintech ", "fintech", "Health  care", "", "x" * 61]) == (
        "Fintech",
        "Health care",
    )
    assert clean_options(["Only one"]) == ()
    assert len(clean_options([f"opt {i}" for i in range(20)])) == 8


async def test_model_options_reach_the_question_and_allow_several():
    out = QuestionOutput(question="Which sectors?", options=["Fintech", "Healthcare", "Other"])
    result = await generate_question(
        FakeLLM({"question": [out]}), CONFIG, SessionState(), "pain_points"
    )
    assert result.question.options == ("Fintech", "Healthcare") and result.question.multi


async def test_fixed_slot_choices_override_model_options_and_allow_several():
    out = QuestionOutput(question="How big?", options=["Tiny", "Huge"])
    result = await generate_question(
        FakeLLM({"question": [out]}), CONFIG, SessionState(), "company_size"
    )
    assert result.question.options[0] == "1-10"


async def test_size_and_revenue_are_single_pick_but_regions_allow_several():
    for slot, multi in (("company_size", False), ("revenue_range", False), ("geographies", True)):
        out = QuestionOutput(question="Which?")
        result = await generate_question(FakeLLM({"question": [out]}), CONFIG, SessionState(), slot)
        assert result.question.multi is multi, slot
        assert fallback_question(slot).multi is multi, slot


@pytest.mark.asyncio
async def test_question_writer_gets_the_industry_context():
    from icp_chatbot.domain.state import DomainContext

    llm = FakeLLM({"question": [q("Which payroll cycles hurt most?")]})
    state = SessionState(domain=DomainContext(industry="Payroll software", brief="HR buyers"))
    await generate_question(llm, CONFIG, state, "pain_points")
    user = llm.calls_for("question")[0].user
    assert "Payroll software" in user and "HR buyers" in user


@pytest.mark.asyncio
async def test_question_writer_gets_the_industry_focus():
    from icp_chatbot.domain.state import DomainContext

    llm = FakeLLM({"question": [q("Which trade lanes do you run most?")]})
    state = SessionState(domain=DomainContext(industry="Import export", focus=["Trade lanes"]))
    await generate_question(llm, CONFIG, state, "pain_points")
    assert "Trade lanes" in llm.calls_for("question")[0].user


async def test_a_question_the_model_also_put_in_reply_is_said_once():
    llm = FakeLLM(
        {"question": [QuestionOutput(question="Which regions?", reply="Fine. Which regions?")]}
    )
    result = await generate_question(llm, CONFIG, SessionState(), "geographies", asked="hi?")
    assert result.question.text == "Fine. Which regions?"


async def test_a_reply_that_already_holds_the_waiting_question_is_not_doubled():
    out = QuestionOutput(question="Who buys it?", reply="It means export-import. Who buys it?")
    llm = FakeLLM({"question": [out]})
    pending = Question("Who buys?", kind="slot", slot="buyer_roles")
    result = await generate_reply(llm, CONFIG, SessionState(), pending, "what is exim")
    assert result.question.text == "It means export-import. Who buys it?"


async def test_a_thread_question_digs_into_what_the_user_brought_up():
    out = QuestionOutput(question="Which customers hesitate?", options=["New ones"])
    llm = FakeLLM({"question": [out]})
    state = SessionState(
        last_turns=[TurnPair(question="Which regions?", answer="some users fear side effects")]
    )
    result = await generate_thread_question(
        llm, CONFIG, state, "users fear side effects", "objections"
    )
    assert result.question == Question(
        "Which customers hesitate?", kind="thread", slot="objections"
    )
    user = llm.calls_for("question")[0].user
    assert "The user brought up: users fear side effects" in user and "deeper" in user
    assert "some users fear side effects" in user


async def test_a_thread_question_drops_an_unknown_slot_and_gives_way_on_failure():
    llm = FakeLLM({"question": [q("What happened next?")]})
    result = await generate_thread_question(llm, CONFIG, SessionState(), "a client", "made_up")
    assert result.question.slot is None
    llm = FakeLLM({"question": [LLMError("api", "boom")]})
    assert await generate_thread_question(llm, CONFIG, SessionState(), "a client", "") is None


async def test_coming_back_from_a_thread_asks_with_a_bridge():
    llm = FakeLLM({"question": [q("Which regions?")]})
    await generate_question(llm, CONFIG, SessionState(), "geographies", returning=True)
    assert "bridge" in llm.calls_for("question")[0].user


async def test_moving_to_a_new_topic_never_asks_the_last_question_again():
    llm = FakeLLM({"question": [q("Which regions?")]})
    state = SessionState(pending_question="Retail or distributors?", pending_slot="industries")
    state.last_turns = [TurnPair(question="Retail or distributors?", answer="we show lab reports")]
    await generate_question(llm, CONFIG, state, "geographies")
    user = llm.calls_for("question")[0].user
    assert "moved on" in user and "Ask only about the target slot" in user
