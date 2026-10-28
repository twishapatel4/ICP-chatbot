import pytest

from icp_chatbot.domain.slots import (
    SECTION_ORDER,
    SLOT_LIST,
    SLOTS,
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
        "industries", "company_size", "geographies", "buyer_roles", "buyer_departments", "disqualifiers",
        "customer_segments", "product", "business_model", "customer_profile",
    }  # fmt: skip


def test_customer_segments_does_not_block_completion():
    blocking = {s.key for s in SLOT_LIST if s.blocks}
    assert "customer_segments" not in blocking
    assert "industries" in blocking


def test_skip_rules_reference_real_slots():
    for slot in SLOT_LIST:
        for other_key, _ in slot.skip_if:
            assert other_key in SLOTS


def test_catalog_lists_every_slot_and_choices():
    catalog = render_slot_catalog()
    for slot in SLOT_LIST:
        assert f"- {slot.key}:" in catalog
    assert "Choices: B2B; B2C; Both" in catalog


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


def test_last_two_turns_and_round_trip():
    state = SessionState(asked_slots={"buying_triggers": 1, "industries": 1})
    for i in range(3):
        state.push_turn(f"q{i}", f"a{i}")
    assert [t.question for t in state.last_turns] == ["q1", "q2"]
    restored = SessionState.model_validate(state.model_dump(mode="json"))
    assert restored == state


def test_a_pick_list_slot_is_offered_as_examples_so_specific_answers_are_kept():
    from icp_chatbot.domain.slots import render_slot_catalog

    catalog = render_slot_catalog()
    line = next(x for x in catalog.splitlines() if x.startswith("- geographies:"))
    assert "not a closed list" in line and "never widened" in line and "Choices:" not in line
    assert "Choices: B2B; B2C; Both" in catalog  # a truly closed list stays choices


def test_size_bands_are_examples_so_the_users_exact_range_is_kept():
    from icp_chatbot.domain.slots import render_slot_catalog

    lines = render_slot_catalog().splitlines()
    for key in ("company_size",):
        line = next(x for x in lines if x.startswith(f"- {key}:"))
        assert "not a closed list" in line and "100-300 stays 100-300" in line
        assert "Choices:" not in line
