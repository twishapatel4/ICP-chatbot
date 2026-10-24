
from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.questions import (
    FOUNDER_OPENER,
    OPENER_ANGLES,
    OPENER_VARIANTS,
    SALES_OPENER,
    QuestionOutput,
    clean_options,
    generate_closing,
    generate_opener,
    pick_opener_variant,
)
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import LLMError
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


async def test_a_closing_answer_replies_then_ends_with_a_thank_you_not_a_question():
    out = QuestionOutput(question="Thanks, that is all I need.", reply="MOQ is the smallest order.")
    llm = FakeLLM({"question": [out]})
    result = await generate_closing(llm, CONFIG, SessionState(), "what is MOQ")
    assert result.question.text == "MOQ is the smallest order. Thanks, that is all I need."
    assert result.question.kind == "closing"
    assert "interview is finished" in llm.calls_for("question")[0].user
    failing = FakeLLM({"question": [LLMError("api", "boom")]})
    assert await generate_closing(failing, CONFIG, SessionState(), "what is MOQ") is None


