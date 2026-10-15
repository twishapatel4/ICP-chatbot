import pytest

from icp_chatbot.domain.slots import (
    SECTION_ORDER,
    SLOT_LIST,
    SLOTS,
    STOP_TOPICS,
    Tier,
    render_slot_catalog,
    slots_in_tier,
)
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue


def test_registry_has_unique_keys_and_valid_sections():
    assert len({s.key for s in SLOT_LIST}) == len(SLOT_LIST)
    assert {s.section for s in SLOT_LIST} <= set(SECTION_ORDER)


def test_tier_a_slots_match_the_spec():
    keys = {s.key for s in slots_in_tier(Tier.A)}
    assert keys == {
        "industries", "company_size", "revenue_range", "geographies", "pain_points",
        "desired_outcomes", "buyer_roles", "buyer_departments", "disqualifiers",
        "customer_segments",
    }  # fmt: skip


def test_customer_segments_does_not_block_completion():
    blocking = {s.key for s in SLOT_LIST if s.blocks}
    assert "customer_segments" not in blocking
    assert "industries" in blocking


def test_topics_only_on_tier_b_and_cover_the_five_stop_topics():
    topics = {s.topic for s in SLOT_LIST if s.topic}
    assert STOP_TOPICS <= topics
    assert all(s.tier == Tier.B for s in SLOT_LIST if s.topic)


def test_skip_rules_reference_real_slots():
    for slot in SLOT_LIST:
        for other_key, _ in slot.skip_if:
            assert other_key in SLOTS


def test_catalog_lists_every_slot_and_choices():
    catalog = render_slot_catalog()
    for slot in SLOT_LIST:
        assert f"- {slot.key}:" in catalog
    assert "Choices: 1-10; 11-50" in catalog


def test_slot_value_normalizes_values():
    v = SlotValue(
        values=[" SaaS ", "saas", "", "Fintech"], confidence=0.8, source=SlotSource.STATED
    )
    assert v.values == ["SaaS", "Fintech"]


def test_confidence_must_be_between_zero_and_one():
    with pytest.raises(ValueError):
        SlotValue(values=["x"], confidence=1.5, source=SlotSource.STATED)


def test_state_confidence_defaults_to_zero_and_applicability_follows_business_model():
    state = SessionState()
    assert state.confidence("industries") == 0.0
    roles = SLOTS["buyer_roles"]
    assert state.is_applicable(roles)
    state.slots["business_model"] = SlotValue(
        values=["B2C"], confidence=0.9, source=SlotSource.CONFIRMED
    )
    assert not state.is_applicable(roles)


def test_topics_asked_and_last_two_turns_and_round_trip():
    state = SessionState(asked_slots={"tech_stack": 1, "industries": 1})
    assert state.topics_asked() == {"tools"}
    for i in range(3):
        state.push_turn(f"q{i}", f"a{i}")
    assert [t.question for t in state.last_turns] == ["q1", "q2"]
    restored = SessionState.model_validate(state.model_dump(mode="json"))
    assert restored == state
