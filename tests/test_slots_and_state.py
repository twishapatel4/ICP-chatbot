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
        "industries", "company_size", "lead_geographies", "company_hq_geographies", "buyer_roles",
        "buyer_departments", "buyer_seniority", "disqualifiers", "customer_segments", "product",
        "business_model", "customer_profile",
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
    from icp_chatbot.domain.slots import SlotDef, render_slot_catalog

    markets = SlotDef("markets", "target_firmographics", "Markets", "Where", Tier.A,
                      choices=("Europe", "Asia"))  # fmt: skip
    line = render_slot_catalog([markets])
    assert "not a closed list" in line and "never widened" in line and "Choices:" not in line
    assert "Choices: B2B; B2C; Both" in render_slot_catalog()  # a truly closed list stays choices


def test_geography_slots_have_no_fixed_list_in_the_extraction_catalog():
    lines = render_slot_catalog().splitlines()
    for key in ("lead_geographies", "company_hq_geographies"):
        line = next(x for x in lines if x.startswith(f"- {key}:"))
        assert "Countries" in line and "Choices:" not in line and "not a closed list" not in line


B2C = (("business_model", "B2C"),)


def test_sales_registry_has_split_geography_and_no_geographies():
    from icp_chatbot.domain.slots import registry, slot_map

    sales = slot_map("sales")
    assert registry("sales") == SLOT_LIST and sales == SLOTS  # the Sales registry is the default
    assert "geographies" not in sales
    lead, hq = sales["lead_geographies"], sales["company_hq_geographies"]
    seniority = sales["buyer_seniority"]
    assert (lead.tier, lead.importance, lead.skip_if) == (Tier.A, 0.9, ())
    assert (hq.tier, hq.importance, hq.skip_if) == (Tier.A, 0.9, B2C)
    assert (seniority.tier, seniority.importance, seniority.skip_if) == (Tier.A, 0.8, B2C)
    assert lead.choices == () and hq.choices == ()  # pickers come from the country catalog
    assert isinstance(registry("hr"), tuple)  # the HR registry resolves (filled in later)


def test_company_size_offers_linkedins_nine_bands():
    from icp_chatbot.domain import slots
    from icp_chatbot.linkedin.catalog import default_catalog

    assert not hasattr(slots, "SIZE_BANDS")
    choices = SLOTS["company_size"].choices
    assert choices == tuple(o.label for o in default_catalog().options("company_size"))
    assert choices == (
        "Self-employed", "1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000",
        "5001-10,000", "10,001+",
    )  # fmt: skip


def test_company_hq_skipped_for_b2c():
    state = SessionState()
    hq, lead = state.slot_def("company_hq_geographies"), state.slot_def("lead_geographies")
    seniority = state.slot_def("buyer_seniority")
    assert state.is_applicable(hq) and state.is_applicable(lead) and state.is_applicable(seniority)
    state.slots["business_model"] = SlotValue(
        values=["B2C"], confidence=0.9, source=SlotSource.CONFIRMED
    )
    assert not state.is_applicable(hq) and not state.is_applicable(seniority)
    assert state.is_applicable(lead)  # where the customers live is still asked


def test_legacy_state_with_geographies_loads_into_lead_geographies():
    europe = {"values": ["Europe"], "confidence": 0.9, "source": "stated", "evidence": "EU"}
    guess = {"values": ["India"], "confidence": 0.5, "source": "inferred"}
    legacy = {
        "slots": {"geographies": europe, "industries": {**europe, "values": ["Banks"]}},
        "seed": {"geographies": guess},
        "asked_slots": {"geographies": 2, "industries": 1},
        "probes": {"geographies": 1},
        "probe_conf": {"geographies": 0.4},
        "stalls": {"geographies": 1},
        "declines": {"geographies": 1, "best_customer": 1},
        "flips": {"geographies": 3},
        "skipped": ["geographies"],
        "clarified": ["industries", "geographies"],
        "contradictions": ["geographies"],
        "pending_slot": "geographies",
    }
    state = SessionState.model_validate(legacy)
    assert state.values("lead_geographies") == ["Europe"]
    assert state.slots["lead_geographies"].evidence == "EU"
    assert "geographies" not in state.slots and "company_hq_geographies" not in state.slots
    assert state.values("industries") == ["Banks"]
    assert state.seed["lead_geographies"].values == ["India"] and "geographies" not in state.seed
    assert state.asked_slots == {"lead_geographies": 2, "industries": 1}
    assert state.probes == {"lead_geographies": 1} and state.probe_conf == {"lead_geographies": 0.4}
    assert state.stalls == {"lead_geographies": 1} and state.flips == {"lead_geographies": 3}
    assert state.declines == {"lead_geographies": 1, "best_customer": 1}
    assert state.skipped == ["lead_geographies"] and state.contradictions == ["lead_geographies"]
    assert state.clarified == ["industries", "lead_geographies"]
    assert state.pending_slot == "lead_geographies"
    assert state.track == "sales" and state.step == "chat"
    assert SessionState.model_validate(state.model_dump(mode="json")) == state  # stable


def test_legacy_geographies_in_the_industry_context_and_threads_are_renamed_too():
    newer = {"values": ["India"], "confidence": 0.9, "source": "stated"}
    older = {"values": ["Asia"], "confidence": 0.9, "source": "stated"}
    state = SessionState.model_validate({
        "slots": {"geographies": older, "lead_geographies": newer},  # the new key wins
        "domain": {"industry": "Logistics", "not_needed": ["geographies", "company_size"]},
        "thread_slot": "geographies",
        "agent_missing": ["geographies", "Buyer roles"],  # raw keys were stored before 3d6abd9
    })  # fmt: skip
    assert list(state.slots) == ["lead_geographies"] and state.values("lead_geographies") == [
        "India"
    ]
    assert state.domain.not_needed == ["lead_geographies", "company_size"]
    assert state.thread_slot == "lead_geographies"
    assert state.agent_missing == ["lead_geographies", "Buyer roles"]


def test_state_without_track_defaults_to_sales():
    state = SessionState.model_validate({"slots": {}})
    assert state.track == "sales" and state.step == "chat"
    assert [s.key for s in state.slot_defs()] == [s.key for s in SLOT_LIST]
    hr = SessionState(track="hr", step="profile")
    assert SessionState.model_validate(hr.model_dump(mode="json")).track == "hr"
    with pytest.raises(ValueError):
        SessionState(track="marketing")


def test_size_bands_are_examples_so_the_users_exact_range_is_kept():
    from icp_chatbot.domain.slots import render_slot_catalog

    lines = render_slot_catalog().splitlines()
    for key in ("company_size",):
        line = next(x for x in lines if x.startswith(f"- {key}:"))
        assert "not a closed list" in line and "100-300 stays 100-300" in line
        assert "Choices:" not in line
