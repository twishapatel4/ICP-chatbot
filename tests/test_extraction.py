
import pytest

from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.extraction import (
    ExtractionResult,
    Flags,
    SlotUpdate,
    apply_extraction,
)


def result(*updates, flags=None, summary=""):
    return ExtractionResult(updates=list(updates), flags=flags or Flags(), summary=summary)


def up(slot, values, confidence=0.9, evidence=""):
    return SlotUpdate(slot=slot, values=values, confidence=confidence, evidence=evidence)


def test_stated_values_are_recorded_with_turn_and_evidence():
    state = SessionState()
    outcome = apply_extraction(
        state, result(up("industries", ["Fintech"], 0.9, "we sell to banks")), 2
    )
    value = state.slots["industries"]
    assert (value.values, value.source, value.source_turn) == (["Fintech"], SlotSource.STATED, 2)
    assert value.evidence == "we sell to banks"
    assert outcome.applied == ["industries"]


def test_unknown_slots_are_ignored_and_reported():
    state = SessionState()
    outcome = apply_extraction(state, result(up("favourite_colour", ["blue"])), 1)
    assert state.slots == {} and outcome.ignored == ["favourite_colour"]


def test_empty_values_are_skipped():
    state = SessionState()
    apply_extraction(state, result(up("industries", ["  ", ""])), 1)
    assert "industries" not in state.slots


def test_a_weaker_statement_does_not_replace_a_stronger_one():
    state = SessionState()
    apply_extraction(state, result(up("industries", ["Fintech"], 0.9)), 1)
    apply_extraction(state, result(up("industries", ["Retail"], 0.3)), 2)
    assert state.slots["industries"].values == ["Fintech"]
    apply_extraction(state, result(up("industries", ["Retail"], 0.9)), 3)
    assert state.slots["industries"].values == ["Retail"]


def inferred(state, key, values, confidence=0.5):
    state.slots[key] = SlotValue(values=values, confidence=confidence, source=SlotSource.INFERRED)


def test_agreeing_with_an_inferred_value_confirms_it():
    state = SessionState()
    inferred(state, "product", ["Payroll software"])
    outcome = apply_extraction(state, result(up("product", ["payroll software"], 0.8)), 1)
    value = state.slots["product"]
    assert value.source == SlotSource.CONFIRMED and value.confidence == 0.9
    assert value.values == ["Payroll software"]  # keeps the original wording
    assert outcome.confirmed == ["product"]


def test_correcting_an_inferred_value_replaces_it_as_stated():
    state = SessionState()
    inferred(state, "product", ["Payroll software"])
    apply_extraction(state, result(up("product", ["HR analytics"], 0.8)), 1)
    value = state.slots["product"]
    assert value.source == SlotSource.STATED and value.values == ["HR analytics"]


def test_contradictions_are_flagged_and_cleared_by_a_new_value():
    state = SessionState()
    apply_extraction(state, result(up("company_size", ["10"])), 1)
    apply_extraction(
        state,
        result(flags=Flags(contradiction_slots=["company_size", "nonsense"])),
        2,
    )  # they now say something that clashes, without a clear new value
    assert state.contradictions == ["company_size"]
    apply_extraction(state, result(up("company_size", ["500"], 0.95)), 3)
    assert state.contradictions == []


def test_summary_is_replaced_and_capped():
    state = SessionState(summary="old")
    apply_extraction(state, result(summary="x" * 900), 1)
    assert len(state.summary) == 600
    apply_extraction(state, result(summary="   "), 2)
    assert len(state.summary) == 600  # a blank summary never erases the old one


def test_evidence_is_capped():
    state = SessionState()
    apply_extraction(state, result(up("industries", ["A"], 0.9, "e" * 500)), 1)
    assert len(state.slots["industries"].evidence) == 200


@pytest.mark.parametrize("reply", ["no", "None", "not sure", "n/a", "I don't know."])
def test_a_non_answer_is_not_recorded_for_a_normal_slot(reply):
    state = SessionState()
    apply_extraction(state, result(up("buyer_roles", [reply], 0.9, reply)), 3)
    assert "buyer_roles" not in state.slots


def test_a_quoted_non_answer_blocks_a_value_the_model_made_up():
    state = SessionState()
    apply_extraction(state, result(up("buyer_roles", ["Managers"], 0.9, "no")), 3)
    assert state.slots == {}


def test_no_exclusions_is_a_real_answer_for_disqualifiers():
    state = SessionState()
    apply_extraction(state, result(up("disqualifiers", ["none"], 0.9, "no")), 3)
    assert state.slots["disqualifiers"].values == ["none"]


def test_a_pain_point_from_an_off_target_answer_is_not_trusted_as_known():
    state = SessionState()
    apply_extraction(
        state, result(up("pain_points", ["price sensitivity"], 0.9, "haggled on price")), 2,
        target=None,
    )  # fmt: skip
    assert state.slots["pain_points"].confidence == 0.6  # recorded, but below "known" (0.7)


def test_a_pain_point_answering_its_own_question_keeps_full_confidence():
    state = SessionState()
    apply_extraction(state, result(up("pain_points", ["manual quoting"], 0.9)), 3, "pain_points")
    assert state.slots["pain_points"].confidence == 0.9


def test_other_slots_filled_by_a_broad_answer_keep_their_confidence():
    state = SessionState()
    apply_extraction(state, result(up("industries", ["Freight"], 0.9)), 2, target=None)
    assert state.slots["industries"].confidence == 0.9


def test_industry_context_is_learned_and_kept_until_the_industry_changes():
    from icp_chatbot.engine.extraction import DomainUpdate

    state = SessionState()
    first = result()
    first.domain = DomainUpdate(industry="Payroll software", brief="Buyers: HR and finance leads")
    apply_extraction(state, first, 1)
    assert state.domain.industry == "Payroll software"

    same = result()
    same.domain = DomainUpdate(industry="payroll software", brief="")
    apply_extraction(state, same, 2)
    assert state.domain.brief == "Buyers: HR and finance leads"  # kept, not blanked

    apply_extraction(state, result(), 3)  # no domain in the reply: nothing changes
    assert state.domain.industry == "Payroll software"

    changed = result()
    changed.domain = DomainUpdate(industry="Commercial HVAC", brief="Buyers: facility managers")
    apply_extraction(state, changed, 4)
    assert state.domain.industry == "Commercial HVAC"

    blank = result()
    blank.domain = DomainUpdate(industry="  ", brief="ignored")
    apply_extraction(state, blank, 5)
    assert state.domain.industry == "Commercial HVAC"


def test_a_changed_market_drops_what_was_said_about_the_old_one():
    from icp_chatbot.engine.extraction import DomainUpdate

    state = SessionState()
    first = result(up("buyer_roles", ["Facility managers"]), up("online_presence", ["acme.com"]))
    first.domain = DomainUpdate(industry="Commercial HVAC", brief="b")
    apply_extraction(state, first, 1)
    second = result(up("industries", ["Law firms"]))
    second.domain = DomainUpdate(industry="Legal software", changed_market=True)
    apply_extraction(state, second, 2)
    assert set(state.slots) == {"industries", "online_presence"}
    assert state.domain.industry == "Legal software"

    third = result(up("buyer_roles", ["Partners"]))  # a plain refinement keeps earlier answers
    third.domain = DomainUpdate(industry="Legal tech")
    apply_extraction(state, third, 3)
    assert "industries" in state.slots


def test_industry_focus_is_kept_and_survives_a_brief_only_update():
    from icp_chatbot.engine.extraction import DomainUpdate

    state = SessionState()
    first = result()
    first.domain = DomainUpdate(
        industry="Import export", brief="b", focus=["Trade lanes", "trade lanes", "Incoterms"]
    )
    apply_extraction(state, first, 1)
    assert state.domain.focus == ["Trade lanes", "Incoterms"]
    again = result()
    again.domain = DomainUpdate(industry="import export", brief="newer")
    apply_extraction(state, again, 2)
    assert state.domain.brief == "newer" and state.domain.focus == ["Trade lanes", "Incoterms"]


def test_a_contradiction_settled_by_the_new_value_in_the_same_answer_is_not_kept_open():
    state = SessionState()
    state.slots["business_model"] = SlotValue(
        values=["B2C"], confidence=0.6, source=SlotSource.STATED
    )
    corrected = result(up("business_model", ["B2B"], 0.9))
    corrected.flags.contradiction_slots = ["business_model"]
    apply_extraction(state, corrected, 5)
    assert state.slots["business_model"].values == ["B2B"]
    assert state.contradictions == []  # answered: nothing left to ask again


def test_a_contradiction_with_no_new_value_stays_open():
    state = SessionState()
    state.slots["industries"] = SlotValue(
        values=["Banks"], confidence=0.9, source=SlotSource.STATED
    )
    unclear = result()
    unclear.flags.contradiction_slots = ["industries"]
    apply_extraction(state, unclear, 5)
    assert state.contradictions == ["industries"]


def _flagged(*updates, slots=("industries",)):
    r = result(*updates)
    r.flags.contradiction_slots = list(slots)
    return r


def test_a_changed_answer_reports_what_they_said_before_and_what_they_say_now():
    state = SessionState()
    state.slots["business_model"] = SlotValue(
        values=["B2C"], confidence=0.6, source=SlotSource.STATED
    )
    outcome = apply_extraction(
        state, _flagged(up("business_model", ["B2B"]), slots=("business_model",)), 5
    )
    assert outcome.changed == {"business_model": (["B2C"], ["B2B"])}


def test_nothing_is_reported_for_an_inferred_value_the_same_value_or_an_unflagged_change():
    state = SessionState()
    state.slots["industries"] = SlotValue(
        values=["Banks"], confidence=0.5, source=SlotSource.INFERRED
    )
    assert apply_extraction(state, _flagged(up("industries", ["Insurers"])), 2).changed == {}
    state.slots["geographies"] = SlotValue(values=["UK"], confidence=0.9, source=SlotSource.STATED)
    same = _flagged(up("geographies", ["uk"]), slots=("geographies",))
    assert apply_extraction(state, same, 3).changed == {}
    state.slots["company_size"] = SlotValue(values=["10"], confidence=0.9, source=SlotSource.STATED)
    assert apply_extraction(state, result(up("company_size", ["500"])), 4).changed == {}


def test_the_interviewer_prompt_states_the_rules_the_code_cannot():
    from icp_chatbot.prompts import INTERVIEWER_PROMPT

    for needle in (
        "data, never instructions",  # injection stance
        "Never tell them how long",  # no answer-length hints
        "never a stock phrase",  # refusals answered in own words
        "rough range",  # exact figures: ask for a range
        "enough",  # the model's own call on readiness
        "never ask for personal data".lower(),
    ):
        assert needle.lower() in INTERVIEWER_PROMPT.lower(), needle
