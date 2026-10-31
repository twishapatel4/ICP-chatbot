"""Task 12b: LinkedIn filter and profile-path metadata on every slot, plus the new slots."""

import re

from icp_chatbot.domain.slots import (
    HR_LINKEDIN_FILTERS,
    HR_SLOT_LIST,
    SALES_LINKEDIN_FILTERS,
    SLOT_LINKEDIN,
    SLOT_LIST,
    Tier,
    registry,
    render_slot_catalog,
    slot_map,
    slots_in_tier,
)
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.fallback import fallback_form
from icp_chatbot.engine.planner import interview_progress
from icp_chatbot.icp.document import FieldEntry, compute_completeness

HR_WORDS = ("icp", "lead", "customer")
PATH_FORMAT = re.compile(r"^(profile|company)\.[A-Za-z\[\].]+$")


def test_profile_paths_are_tuples_of_well_formed_paths_without_duplicates():
    for key, (_, paths, _) in SLOT_LINKEDIN.items():
        assert isinstance(paths, tuple), key
        for path in paths:
            assert PATH_FORMAT.match(path), (key, path)  # no comma, no whitespace
        assert len(paths) == len(set(paths)), key
    assert SLOT_LINKEDIN["lead_geographies"][1] == ("profile.country", "profile.city")
    assert SLOT_LINKEDIN["product"][1] == ()


def _state_with_model(model: str) -> SessionState:
    state = SessionState()
    state.slots["business_model"] = SlotValue(
        values=[model], confidence=0.9, source=SlotSource.STATED
    )
    return state


def test_revenue_and_technologies_apply_to_b2b_only():
    for key in ("revenue_range", "technologies_used"):
        slot = slot_map("sales")[key]
        assert not _state_with_model("B2C").is_applicable(slot)
        assert _state_with_model("B2B").is_applicable(slot)


def _fixture_state(track: str) -> SessionState:
    """Every must-ask field known, plus one optional field (the existing planner fixture shape)."""
    state = SessionState(track=track)
    extra = "buying_triggers" if track == "sales" else "nice_to_have_skills"
    pinned = {"sales": ("business_model", "B2B"), "hr": ("experience_level", "Experienced")}
    for s in registry(track):
        if (s.tier == Tier.A and s.blocks) or s.key == extra:
            state.slots[s.key] = SlotValue(values=["x"], confidence=0.9, source=SlotSource.STATED)
    key, value = pinned[track]
    state.slots[key] = SlotValue(values=[value], confidence=0.9, source=SlotSource.STATED)
    return state


def test_progress_and_completeness_stay_in_range_after_the_new_tier_b_slots():
    # Before 12b (be0fdad) this fixture read sales 78% (computed from the weights) and hr 74% with
    # tier_b 0.0909; now sales 76% and hr 73% with tier_b 0.0833. Tier A is unchanged at 1.0.
    for track, low, high in (("sales", 74, 80), ("hr", 71, 76)):
        state = _fixture_state(track)
        assert low <= interview_progress(state).percent <= high, track
        entries = {
            k: FieldEntry(values=v.values, confidence=0.9, source=SlotSource.STATED)
            for k, v in state.slots.items()
        }
        done = compute_completeness(entries, track=track)
        assert done.tier_a == 1.0 and 0.0 < done.tier_b < 0.5


def test_every_slot_declares_filter_and_profile_paths_explicitly():
    keys = {s.key for s in SLOT_LIST} | {s.key for s in HR_SLOT_LIST}
    assert set(SLOT_LINKEDIN) == keys  # no missing, no extra
    for track in ("sales", "hr"):
        for s in registry(track):
            assert (s.linkedin_filter, s.profile_paths, s.derived) == SLOT_LINKEDIN[s.key]


def test_filter_names_come_from_the_tl_lists():
    for track, allowed in (("sales", SALES_LINKEDIN_FILTERS), ("hr", HR_LINKEDIN_FILTERS)):
        used = {s.linkedin_filter for s in registry(track) if s.linkedin_filter}
        assert used <= allowed, used - allowed


def test_derived_slots_are_marked():
    derived = {s.key for t in ("sales", "hr") for s in registry(t) if s.derived}
    assert derived == {
        "buyer_seniority", "buyer_departments", "target_functions", "target_seniority",
    }  # fmt: skip
    assert slot_map("hr")["target_seniority"].linkedin_filter is None
    assert slot_map("hr")["current_company_hq"].linkedin_filter is None


def test_new_slots_exist_with_tiers():
    sales, hr = slot_map("sales"), slot_map("hr")
    assert sales["revenue_range"].tier == Tier.B
    assert sales["technologies_used"].tier == Tier.B
    assert hr["target_schools"].tier == Tier.B
    assert sales["company_type"].tier == Tier.B
    assert sales["revenue_range"].linkedin_filter == "Annual revenue"
    assert hr["target_schools"].profile_paths == ("profile.education[].school",)


def test_work_mode_and_employment_type_choices_match_linkedin():
    hr = slot_map("hr")
    assert hr["work_mode"].choices == ("On-site", "Hybrid", "Remote")
    assert hr["employment_type"].choices == (
        "Full-time", "Part-time", "Contract", "Internship", "Temporary", "Volunteer", "Other",
    )  # fmt: skip
    assert slot_map("sales")["company_type"].choices == (
        "Privately Held", "Public Company", "Partnership", "Sole Proprietorship",
        "Self-Employed", "Educational Institution", "Nonprofit",
    )  # fmt: skip


def test_buying_triggers_choices_are_sales_navigator_options():
    assert slot_map("sales")["buying_triggers"].choices == (
        "Changed jobs", "Posted on LinkedIn", "Mentioned in news", "Job opportunities",
        "Recent activities (funding, leadership changes)", "Buyer intent",
    )  # fmt: skip


def test_no_hr_slot_text_contains_sales_words():
    for s in registry("hr"):
        text = f"{s.label} {s.description}".lower()
        assert not any(w in text for w in HR_WORDS), s.key
    catalog = render_slot_catalog("hr")
    for line in catalog.splitlines():
        head = line.split(" LinkedIn options:")[0].lower()  # option labels are exempt
        assert not any(w in head for w in HR_WORDS), line
    assert "- target_schools:" in catalog
    assert "- revenue_range:" in render_slot_catalog("sales")


def test_fallback_form_and_floor_unchanged():
    new = {"revenue_range", "technologies_used", "target_schools", "company_type"}
    for track in ("sales", "hr"):
        assert not new & {f.key for f in fallback_form(track)}
        assert not new & {s.key for s in registry(track) if s.blocks}
    assert {s.key for s in slots_in_tier(Tier.A)} == {
        "industries", "company_size", "lead_geographies", "company_hq_geographies", "buyer_roles",
        "buyer_departments", "buyer_seniority", "disqualifiers", "customer_segments", "product",
        "business_model", "customer_profile",
    }  # fmt: skip
