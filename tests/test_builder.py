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
            "lead_geographies": slot(["Europe"], 0.5, SlotSource.INFERRED),
            "buyer_roles": slot(["CFO"], 0.6, SlotSource.CONFIRMED),
            "disqualifiers": slot(["no government, nothing under 50 people"]),
        }
    )
    return state


def test_last_verified_is_set_only_for_values_the_user_stated_or_confirmed():
    entries = entries_from_state(make_state(), TODAY)
    assert entries["industries"].last_verified == "2026-10-15"
    assert entries["buyer_roles"].last_verified == "2026-10-15"
    assert entries["lead_geographies"].last_verified is None  # inferred


async def test_synthesis_cleans_values_but_keeps_source_and_confidence():
    synthesis = ICPSynthesis(
        fields=[
            FieldDraft(
                slot="industries", values=["Banks", "Banking"], evidence="banks"
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
    assert industries.values == ["Banks", "Banking"]
    assert industries.source == SlotSource.STATED and industries.confidence == 0.9
    assert industries.evidence != "banks"  # the recorded evidence stays, not the model's note
    assert [d.field for d in doc.scoring_model.disqualifiers] == ["industries", "company_size"]
    assert "Which banks exactly?" in doc.open_questions
    assert validate_model(doc.scoring_model) == []
    assert result.usage == Usage(2000, 800, 300, 0) and not result.used_fallback
    user = llm.calls_for("final_icp")[0].user
    assert "data, not instructions" in user and "Allowed disqualifier fields" in user


async def test_the_model_cannot_add_fields_and_unusable_rules_are_reported():
    synthesis = ICPSynthesis(
        fields=[
            FieldDraft(slot="buyer_departments", values=["Finance"]),
            FieldDraft(slot="nonsense", values=["x"]),
        ],
        disqualifiers=[DisqualifierDraft(field="favourite_colour", operator="in", values=["blue"])],
    )
    result = await build_icp(FakeLLM({"final_icp": [synthesis]}), CONFIG, make_state(), today=TODAY)
    doc = result.document
    assert "buyer_departments" not in doc.entries() and "nonsense" not in doc.entries()
    assert doc.scoring_model.disqualifiers == []
    text = " ".join(doc.open_questions)
    assert "favourite_colour" in text  # an unusable rule is reported
    assert "buyer_departments" not in text and "nonsense" not in text  # internal notes stay out


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
    assert "based in: Europe (inferred)" in doc.summary
    assert 0.0 < doc.completeness.tier_a < 1.0
    industry = next(c for c in doc.scoring_model.criteria if c.key == "industry_fit")
    assert industry.applicable and industry.rules["industries"].primary == ["fintech", "banks"]
    size = next(c for c in doc.scoring_model.criteria if c.key == "company_size")
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
    state.slots["industries"] = slot(["Retail"], 0.6)
    state.slots["buyer_roles"] = slot(["none"])
    state.slots["product"] = slot(["flats"])
    state.slots["business_model"] = slot(["B2B"])
    result = await build_icp(FakeLLM({"final_icp": [ICPSynthesis()]}), CONFIG, state, today=TODAY)
    text = " ".join(result.document.open_questions).lower()
    assert "target industries" in text and "buyer roles" in text


async def test_a_fully_known_core_has_no_missing_notes():
    from icp_chatbot.domain.slots import SLOT_LIST, Tier

    state = SessionState()
    for s in SLOT_LIST:
        if s.tier in (Tier.A, Tier.B):
            state.slots[s.key] = slot(["x"])
    result = await build_icp(FakeLLM({"final_icp": [ICPSynthesis()]}), CONFIG, state, today=TODAY)
    assert result.document.open_questions == []


async def test_added_wording_is_rejected_and_the_recorded_values_are_kept():
    state = make_state()
    before = list(entries_from_state(state, TODAY)["industries"].values)
    synthesis = ICPSynthesis(fields=[FieldDraft(slot="industries", values=["Banks for actuarial rating"])])
    result = await build_icp(FakeLLM({"final_icp": [synthesis]}), CONFIG, state, today=TODAY)
    assert result.document.entries()["industries"].values == before


async def test_a_shown_but_unconfirmed_field_asks_to_confirm_instead_of_saying_unknown():
    # QA 2026-10-27: about half of all cards said "Not yet known: <field>" right under the
    # values they listed, because the value sat below the known confidence
    state = SessionState()
    state.slots["industries"] = slot(["Retail"], 0.6)
    result = await build_icp(FakeLLM({"final_icp": [ICPSynthesis()]}), CONFIG, state, today=TODAY)
    notes = result.document.open_questions
    assert not any(n.lower().startswith("not yet known: target industries") for n in notes)
    assert any("target industries" in n.lower() and "retail" in n.lower() for n in notes)


async def test_open_questions_that_repeat_a_disqualifier_or_each_other_are_dropped():
    state = make_state()
    state.slots["disqualifiers"] = slot(["Buyers asking for credit terms", "wholesale buyers"])
    synthesis = ICPSynthesis(
        open_questions=[
            "Will the buyer accept Letter of Credit rather than requesting credit terms?",
            "How do we identify and exclude wholesale buyers?",
            "Which port do they ship from?",
            "which port do they ship from? ",
        ]
    )
    result = await build_icp(FakeLLM({"final_icp": [synthesis]}), CONFIG, state, today=TODAY)
    model_notes = [n for n in result.document.open_questions if not n.startswith(("Not yet", "Please confirm"))]
    assert model_notes == ["Which port do they ship from?"]


async def test_builder_emits_hr_scoring_model_for_hr_state():
    state = SessionState(track="hr")
    state.slots.update(
        {
            "target_functions": slot(["Engineering"]),
            "required_skills": slot(["Python"]),
            "years_experience": slot(["3-8"]),
        }
    )
    result = await build_icp(FakeLLM({"final_icp": [LLMError("x", "y")]}), CONFIG, state, today=TODAY)
    model = result.document.scoring_model
    assert [c.key for c in model.criteria] == [
        "role_fit", "function_fit", "seniority_fit", "experience_fit", "skills_fit",
        "certifications_fit", "education_fit", "school_fit", "location_fit", "background_fit",
        "employer_fit",
    ]  # fmt: skip
    assert {c.key for c in model.criteria if c.applicable} == {
        "function_fit", "experience_fit", "skills_fit"
    }
    assert validate_model(model, track="hr") == []
