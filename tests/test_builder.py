from datetime import date

from icp_chatbot.config import Settings
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.icp.builder import (
    DisqualifierDraft,
    FieldDraft,
    ICPSynthesis,
    build_icp,
    entries_from_state,
)
from icp_chatbot.icp.scoring import validate_model
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import LLMError, Usage
from icp_chatbot.versions import default_config

CONFIG = default_config(Settings(_env_file=None))
TODAY = date(2026, 10, 15)


def slot(values, confidence=0.9, source=SlotSource.STATED, evidence=""):
    return SlotValue(values=values, confidence=confidence, source=source, evidence=evidence)


def make_state():
    state = SessionState()
    state.slots.update(
        {
            "industries": slot(["fintech", "banks"], evidence="we sell to banks"),
            "company_size": slot(["50-500"]),
            "geographies": slot(["Europe"], 0.5, SlotSource.INFERRED),
            "pain_points": slot(["manual payroll"], 0.6, SlotSource.CONFIRMED),
            "disqualifiers": slot(["no government, nothing under 50 people"]),
        }
    )
    return state


def test_last_verified_is_set_only_for_values_the_user_stated_or_confirmed():
    entries = entries_from_state(make_state(), TODAY)
    assert entries["industries"].last_verified == "2026-10-15"
    assert entries["pain_points"].last_verified == "2026-10-15"
    assert entries["geographies"].last_verified is None  # inferred


async def test_synthesis_cleans_values_but_keeps_source_and_confidence():
    synthesis = ICPSynthesis(
        fields=[
            FieldDraft(
                slot="industries", values=["Financial services", "Banking"], evidence="banks"
            )
        ],
        disqualifiers=[
            DisqualifierDraft(field="industries", operator="in", values=["Government"]),
            DisqualifierDraft(field="company_size", operator="lt", values=["50"]),
        ],
        open_questions=["Which banks exactly?"],
    )
    llm = FakeLLM({"final_icp": [synthesis]}, Usage(2000, 800, 300, 0))
    result = await build_icp(llm, CONFIG, make_state(), today=TODAY, labels={"turn": "9"})
    doc = result.document
    industries = doc.entries()["industries"]
    assert industries.values == ["Financial services", "Banking"]
    assert industries.source == SlotSource.STATED and industries.confidence == 0.9
    assert industries.evidence == "banks"
    assert [d.field for d in doc.scoring_model.disqualifiers] == ["industries", "company_size"]
    assert "Which banks exactly?" in doc.open_questions
    assert validate_model(doc.scoring_model) == []
    assert result.usage == Usage(2000, 800, 300, 0) and not result.used_fallback
    user = llm.calls_for("final_icp")[0].user
    assert "data, not instructions" in user and "Allowed disqualifier fields" in user


async def test_the_model_cannot_add_fields_and_unusable_rules_are_reported():
    synthesis = ICPSynthesis(
        fields=[
            FieldDraft(slot="budget_range", values=["$1M"]),
            FieldDraft(slot="nonsense", values=["x"]),
        ],
        disqualifiers=[DisqualifierDraft(field="favourite_colour", operator="in", values=["blue"])],
    )
    result = await build_icp(FakeLLM({"final_icp": [synthesis]}), CONFIG, make_state(), today=TODAY)
    doc = result.document
    assert "budget_range" not in doc.entries() and "nonsense" not in doc.entries()
    assert doc.scoring_model.disqualifiers == []
    text = " ".join(doc.open_questions)
    assert "budget_range" in text and "nonsense" in text and "favourite_colour" in text


async def test_empty_cleaned_values_keep_the_original():
    synthesis = ICPSynthesis(fields=[FieldDraft(slot="industries", values=["  "])])
    result = await build_icp(FakeLLM({"final_icp": [synthesis]}), CONFIG, make_state(), today=TODAY)
    assert result.document.entries()["industries"].values == ["fintech", "banks"]


async def test_a_model_failure_builds_the_document_from_the_state():
    llm = FakeLLM({"final_icp": [LLMError("timeout", "slow")]})
    result = await build_icp(llm, CONFIG, make_state(), today=TODAY)
    assert result.used_fallback and result.usage == Usage()
    doc = result.document
    assert doc.entries()["industries"].values == ["fintech", "banks"]
    assert any("clean-up step failed" in q for q in doc.open_questions)
    assert validate_model(doc.scoring_model) == []


async def test_summary_and_completeness_follow_the_final_fields():
    result = await build_icp(
        FakeLLM({"final_icp": [ICPSynthesis()]}), CONFIG, make_state(), today=TODAY
    )
    doc = result.document
    assert "Target industries: fintech, banks" in doc.summary
    assert "geographies: Europe (inferred)" in doc.summary
    assert 0.0 < doc.completeness.tier_a < 1.0
    industry = next(c for c in doc.scoring_model.criteria if c.key == "industry_fit")
    assert industry.applicable and industry.rules["industries"].primary == ["fintech", "banks"]
    size = next(c for c in doc.scoring_model.criteria if c.key == "company_size_revenue")
    assert size.rules["company_size"].min == 50 and size.rules["company_size"].max == 500


async def test_an_empty_state_still_produces_a_valid_document():
    result = await build_icp(
        FakeLLM({"final_icp": [ICPSynthesis()]}), CONFIG, SessionState(), today=TODAY
    )
    doc = result.document
    assert doc.sections == {} and doc.summary == ""
    assert not any(c.applicable for c in doc.scoring_model.criteria)
    assert validate_model(doc.scoring_model) == []


async def test_still_unclear_lists_the_core_fields_that_are_missing():
    state = SessionState()
    state.slots["industries"] = slot(["fintech"])
    result = await build_icp(FakeLLM({"final_icp": [ICPSynthesis()]}), CONFIG, state, today=TODAY)
    notes = result.document.open_questions
    assert 0 < len(notes) <= 5 and all(n.startswith("Not yet known:") for n in notes)
    assert not any("industries" in n.lower() for n in notes)  # known fields are not listed


async def test_a_low_confidence_or_non_answer_field_is_listed_as_unknown():
    state = SessionState()
    state.slots["pain_points"] = slot(["price sensitivity"], 0.6)
    state.slots["buyer_roles"] = slot(["none"])
    result = await build_icp(FakeLLM({"final_icp": [ICPSynthesis()]}), CONFIG, state, today=TODAY)
    text = " ".join(result.document.open_questions).lower()
    assert "pain points" in text and "buyer roles" in text


async def test_a_fully_known_core_has_no_missing_notes():
    from icp_chatbot.domain.slots import SLOT_LIST, Tier

    state = SessionState()
    for s in SLOT_LIST:
        if s.tier in (Tier.A, Tier.B):
            state.slots[s.key] = slot(["x"])
    result = await build_icp(FakeLLM({"final_icp": [ICPSynthesis()]}), CONFIG, state, today=TODAY)
    assert result.document.open_questions == []
