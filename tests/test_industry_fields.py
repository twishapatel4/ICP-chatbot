from datetime import date

from icp_chatbot.config import Settings
from icp_chatbot.domain.slots import CustomField
from icp_chatbot.domain.state import DomainContext, SessionState, SlotSource, SlotValue
from icp_chatbot.engine.extraction import (
    DomainUpdate,
    ExtractionResult,
    SlotUpdate,
    apply_extraction,
)
from icp_chatbot.engine.planner import Caps
from icp_chatbot.icp.builder import ICPSynthesis, build_icp
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.versions import default_config

CONFIG = default_config(Settings(_env_file=None))
CAPS = Caps()


def field(key, label="Label", ask="Which one?", importance=0.8):
    return CustomField(key=key, label=label, ask=ask, importance=importance)


def domain_update(**kw):
    return ExtractionResult(domain=DomainUpdate(industry="Flat rentals", **kw))


def test_the_industry_fields_are_kept_cleaned_and_never_clash_with_standard_slots():
    state = SessionState()
    update = domain_update(
        fields=[field("Preferred Localities!"), field("company_size"), field("bhk_type")],
        not_needed=["company_size", "geographies", "product", "made_up"],
    )
    apply_extraction(state, update, 1)
    assert [f.key for f in state.domain.fields] == ["preferred_localities", "bhk_type"]
    assert state.domain.not_needed == ["company_size", "geographies"]  # product is protected


def test_later_updates_add_fields_but_do_not_replace_the_ones_already_asked():
    state = SessionState()
    apply_extraction(state, domain_update(fields=[field("rent_band", "Rent band")]), 1)
    apply_extraction(
        state, domain_update(fields=[field("rent_band", "Other"), field("move_in")]), 2
    )
    assert [(f.key, f.label) for f in state.domain.fields] == [
        ("rent_band", "Rent band"),
        ("move_in", "Label"),
    ]


def test_an_answer_can_fill_an_industry_field_and_unknown_keys_are_still_ignored():
    state = SessionState(domain=DomainContext(industry="Flat rentals", fields=[field("rent_band")]))
    updates = [
        SlotUpdate(slot="rent_band", values=["30-40k"], confidence=0.9, evidence="30 to 40k"),
        SlotUpdate(slot="nonsense", values=["x"], confidence=0.9, evidence="x"),
    ]
    outcome = apply_extraction(state, ExtractionResult(updates=updates), 2)
    assert state.values("rent_band") == ["30-40k"] and outcome.ignored == ["nonsense"]


async def test_the_icp_shows_the_industry_fields_and_ignores_what_is_not_needed():
    domain = DomainContext(
        industry="Flat rentals",
        fields=[field("rent_band", "Rent band")],
        not_needed=["company_size"],
    )
    state = SessionState(domain=domain)
    state.slots["rent_band"] = SlotValue(
        values=["30-40k"], confidence=0.9, source=SlotSource.STATED
    )
    result = await build_icp(
        FakeLLM({"final_icp": [ICPSynthesis()]}), CONFIG, state, today=date(2026, 10, 15)
    )
    document = result.document
    assert document.sections["industry_specifics"]["rent_band"].values == ["30-40k"]
    assert "rent band: 30-40k" in document.summary
    assert not any("company size" in q.lower() for q in document.open_questions)


def test_a_field_defined_in_an_answer_can_be_filled_by_that_same_answer():
    state = SessionState()
    update = domain_update(fields=[field("rent_band")])
    update.updates = [SlotUpdate(slot="rent_band", values=["30-40k"], confidence=0.9, evidence="x")]
    apply_extraction(state, update, 1)
    assert state.values("rent_band") == ["30-40k"]
