from icp_chatbot.domain.slots import (
    HR_SECTION_ORDER,
    SECTION_ORDER,
    Tier,
    registry,
    section_order,
    slot_map,
)
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.fallback import fallback_form, missing_required
from icp_chatbot.engine.planner import customer_keys, enough_for_icp, icp_floor_met


def _hr(**slots: tuple[list[str], SlotSource]) -> SessionState:
    state = SessionState(track="hr")
    for key, (values, source) in slots.items():
        state.slots[key] = SlotValue(values=values, confidence=0.9, source=source)
    return state


def _stated(**slots: list[str]) -> SessionState:
    return _hr(**{k: (v, SlotSource.STATED) for k, v in slots.items()})


def _applicable(state: SessionState, key: str) -> bool:
    return state.is_applicable(slot_map("hr")[key])


def test_hr_registry_keys_are_unique_and_have_sections():
    slots = registry("hr")
    keys = [s.key for s in slots]
    assert len(keys) == len(set(keys)) == 26
    assert {s.section for s in slots} <= set(HR_SECTION_ORDER)
    assert all(s.tier in (Tier.A, Tier.B) for s in slots)
    assert section_order("hr") == HR_SECTION_ORDER
    assert section_order("sales") == SECTION_ORDER
    text = " ".join(f"{s.label} {s.description}" for s in slots).lower()
    assert not any(w in text for w in ("icp", "lead", "customer"))


def test_fresher_skips_experience_slots():
    state = _stated(experience_level=["Fresher"])
    for key in ("years_experience", "current_industries", "current_company_size",
                "current_company_hq"):  # fmt: skip
        assert not _applicable(state, key)
    for key in ("education_level", "field_of_study", "graduation_year"):
        assert _applicable(state, key)


def test_both_skips_field_of_study_and_graduation_year_but_keeps_education_level():
    state = _stated(experience_level=["Both"])
    assert not _applicable(state, "field_of_study")
    assert not _applicable(state, "graduation_year")
    assert _applicable(state, "education_level")
    assert _applicable(state, "years_experience")


def test_experienced_skips_all_education_slots():
    state = _stated(experience_level=["Experienced"])
    for key in ("education_level", "field_of_study", "graduation_year"):
        assert not _applicable(state, key)
    assert _applicable(state, "years_experience")


def test_hr_floor_needs_role_and_three_facts():
    assert customer_keys("hr") == ("target_roles", "target_functions", "required_skills",
                                   "candidate_locations", "education_level",
                                   "years_experience")  # fmt: skip
    facts = {"target_functions": ["Engineering"], "required_skills": ["Python"],
             "candidate_locations": ["India"]}  # fmt: skip
    assert not icp_floor_met(_stated(**facts))  # no role
    assert not icp_floor_met(_stated(target_roles=["Backend Engineer"]))
    assert not icp_floor_met(_stated(target_roles=["Backend Engineer"], required_skills=["Go"]))
    assert icp_floor_met(_stated(target_roles=["Backend Engineer"], **facts))
    guessed = _hr(
        target_roles=(["Backend Engineer"], SlotSource.INFERRED),
        **{k: (v, SlotSource.STATED) for k, v in facts.items()},
    )
    assert not icp_floor_met(guessed)  # a website guess is not "told", even for the role
    state = _stated(target_roles=["Backend Engineer"], **facts)
    state.agent_enough = True
    assert enough_for_icp(state)
    state.slots.pop("target_roles")
    assert not enough_for_icp(state)


def test_hr_fallback_form_lists_tier_a_blockers_with_branch_slots_optional():
    form = {f.key: f for f in fallback_form("hr")}
    assert form["target_roles"].required and form["disqualifiers"].required
    for key in ("education_level", "field_of_study", "graduation_year", "years_experience",
                "current_industries", "current_company_size"):  # fmt: skip
        assert not form[key].required
    assert "current_company_hq" not in form  # tier B
    missing = missing_required({"target_roles": ["Analyst"]}, SessionState(track="hr"))
    assert "target_roles" not in missing and "required_skills" in missing
    assert "years_experience" not in missing
