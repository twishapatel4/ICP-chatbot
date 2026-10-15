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
    QuestionOutput,
    best_customer_question,
    fallback_question,
    generate_opener,
    generate_question,
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
