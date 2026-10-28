import pytest

from icp_chatbot.domain.slots import slot_map
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.profile_form import (
    apply_profile,
    parse_years,
    profile_cards,
    validate_profile,
    visible_cards,
)
from icp_chatbot.linkedin.catalog import OptionCatalog

B2B = {
    "business_model": ["B2B"],
    "industries": ["Software Development"],
    "company_size": ["11-50"],
    "lead_geographies": ["India"],
    "company_hq_geographies": ["India"],
    "buyer_seniority": ["Entry-level"],
    "buyer_departments": ["Sales"],
}

HR_EXPERIENCED = {
    "target_functions": ["Engineering"],
    "target_roles": ["Backend Engineer"],
    "experience_level": ["Experienced"],
    "years_experience": ["3-8"],
    "current_industries": ["Software Development"],
    "current_company_size": ["11-50"],
    "candidate_locations": ["India"],
    "required_skills": ["Python"],
}


def ids(cards):
    return [c.id for c in cards]


@pytest.fixture
def catalog_has_degrees(monkeypatch):
    monkeypatch.setattr(OptionCatalog, "has", lambda self, facet: True)


def test_sales_b2b_cards_in_order():
    assert ids(visible_cards("sales", B2B)) == [
        "who_you_sell_to",
        "target_industries",
        "target_company_size",
        "geography",
        "buyer_roles",
    ]
    assert validate_profile("sales", B2B) == []


def test_unanswered_business_model_hides_dependent_cards():
    assert ids(visible_cards("sales", {})) == ["who_you_sell_to", "geography"]
    assert "company_hq_geographies" not in validate_profile("sales", {})


def test_b2c_shows_only_model_and_geography_and_hq_hidden():
    answers = {"business_model": ["B2C"], "lead_geographies": ["India"]}
    assert ids(visible_cards("sales", answers)) == ["who_you_sell_to", "geography"]
    assert validate_profile("sales", answers) == []
    assert validate_profile("sales", {"business_model": ["B2C"]}) == ["lead_geographies"]


def test_missing_required_card_is_reported():
    answers = {k: v for k, v in B2B.items() if k != "industries"}
    assert validate_profile("sales", answers) == ["industries"]


def test_any_cannot_mix_with_other_values():
    assert validate_profile("sales", {**B2B, "industries": ["Any"]}) == []
    assert validate_profile("sales", {**B2B, "industries": ["Any", "Software Development"]}) == [
        "industries"
    ]


def test_any_rejected_on_business_model():
    assert validate_profile("sales", {**B2B, "business_model": ["Any"]}) == ["business_model"]


def test_title_chips_optional_for_sales():
    assert validate_profile("sales", B2B) == []
    assert validate_profile("sales", {**B2B, "buyer_roles": ["VP Sales"]}) == []


def test_unresolvable_catalog_value_is_invalid():
    assert validate_profile("sales", {**B2B, "industries": ["Not An Industry"]}) == ["industries"]


def test_geo_needs_a_country_with_free_text_cities():
    ok = {**B2B, "lead_geographies": ["India", "Pune"]}
    assert validate_profile("sales", ok) == []
    only_city = {**B2B, "lead_geographies": ["Pune"]}
    assert validate_profile("sales", only_city) == ["lead_geographies"]


def test_values_longer_than_80_chars_or_more_than_10_are_rejected():
    long = {**B2B, "buyer_roles": ["x" * 81]}
    assert validate_profile("sales", long) == ["buyer_roles"]
    many = {**B2B, "buyer_roles": [f"title {i}" for i in range(11)]}
    assert validate_profile("sales", many) == ["buyer_roles"]
    ten = {**B2B, "buyer_roles": [f"title {i}" for i in range(10)]}
    assert validate_profile("sales", ten) == []
    spaced = {**B2B, "buyer_roles": ["  a \n  b\t" + "c" * 70]}
    assert validate_profile("sales", spaced) == []


def test_hr_experienced_cards_and_validation():
    assert ids(visible_cards("hr", HR_EXPERIENCED)) == [
        "role_hiring_for",
        "experience_level",
        "background",
        "candidate_location",
        "must_have_skills",
    ]
    assert validate_profile("hr", HR_EXPERIENCED) == []
    assert ids(visible_cards("hr", {})) == ["role_hiring_for", "experience_level",
                                            "candidate_location", "must_have_skills"]  # fmt: skip


def test_education_card_hidden_without_catalog():
    answers = {**HR_EXPERIENCED, "experience_level": ["Fresher"]}
    assert "education" not in ids(visible_cards("hr", answers))
    problems = validate_profile("hr", answers)
    assert "education_level" not in problems and "graduation_year" not in problems


def test_hr_both_requires_education_level_and_experienced_set(catalog_has_degrees):
    answers = {**HR_EXPERIENCED, "experience_level": ["Both"]}
    cards = visible_cards("hr", answers)
    assert ids(cards)[:3] == ["role_hiring_for", "experience_level", "education"]
    assert validate_profile("hr", answers) == ["education_level"]
    ok = {**answers, "education_level": ["Bachelor's"]}
    assert validate_profile("hr", ok) == []
    no_exp = {k: v for k, v in ok.items() if k != "years_experience"}
    assert validate_profile("hr", no_exp) == ["years_experience"]


def test_hr_fresher_requires_graduation_year_when_catalog_present(catalog_has_degrees):
    answers = {
        **HR_EXPERIENCED,
        "experience_level": ["Fresher"],
        "education_level": ["Bachelor's"],
        "field_of_study": ["Computer Science"],
    }
    assert validate_profile("hr", answers) == ["graduation_year"]
    assert "background" not in ids(visible_cards("hr", answers))
    assert validate_profile("hr", {**answers, "graduation_year": ["2024-2026"]}) == []
    assert validate_profile("hr", {**answers, "graduation_year": ["2040"]}) == ["graduation_year"]


def test_parse_years():
    assert parse_years("3-8") == (3, 8)
    assert parse_years("3+") == (3, None)
    assert parse_years("12+") == (12, None)
    for bad in ("0-3", "8-3", "3-3", "13+", "x", "", "3-"):
        with pytest.raises(ValueError):
            parse_years(bad)


def test_years_experience_validation():
    assert validate_profile("hr", {**HR_EXPERIENCED, "years_experience": ["3+"]}) == []
    bad = {**HR_EXPERIENCED, "years_experience": ["8-3"]}
    assert validate_profile("hr", bad) == ["years_experience"]
    assert validate_profile("hr", {**HR_EXPERIENCED, "years_experience": ["Any"]}) == []


def test_hr_card_titles_avoid_sales_words():
    for card in profile_cards("hr"):
        assert not any(w in card.title.lower() for w in ("icp", "lead", "customer"))


def test_apply_profile_writes_stated_0_9_with_refs():
    state = SessionState(track="sales", step="profile")
    written = apply_profile(state, B2B)
    assert set(written) == set(B2B)
    v = state.slots["industries"]
    assert v.source == SlotSource.STATED and v.confidence == 0.9
    assert v.source_turn == 0 and v.evidence == "step 2 form"
    assert v.refs and v.refs[0].facet == "industry"
    assert state.slots["company_size"].refs[0].id == "C"
    assert apply_profile(state, B2B) == written  # idempotent
    assert len(state.slots) == len(B2B)


def test_apply_profile_rejects_invalid():
    state = SessionState(track="sales", step="profile")
    with pytest.raises(ValueError):
        apply_profile(state, {**B2B, "industries": []})
    assert state.slots == {}


def test_apply_profile_drops_hidden_form_values_only():
    state = SessionState(track="sales", step="profile")
    apply_profile(state, B2B)
    state.slots["customer_profile"] = SlotValue(
        values=["x"], confidence=0.5, source=SlotSource.INFERRED, evidence="chat"
    )
    apply_profile(state, {"business_model": ["B2C"], "lead_geographies": ["India"]})
    assert set(state.slots) == {"business_model", "lead_geographies", "customer_profile"}


def test_company_size_slots_are_not_single_choice():
    assert not slot_map("sales")["company_size"].single_choice
    assert not slot_map("hr")["current_company_size"].single_choice
