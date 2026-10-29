from pathlib import Path

import pytest

from icp_chatbot.eval.cases import case_problems, load_cases
from icp_chatbot.eval.match import forbidden_match, industry_match, slot_matcher

CASES_DIR = Path(__file__).parent.parent / "eval_cases"


def test_geography_alias_and_region_both_ways():
    m = slot_matcher("lead_geographies")
    assert m("United Kingdom", "UK")
    assert m("UK", "United Kingdom")
    assert m("United States", "North America")  # country inside an expected region
    assert m("Canada", "North America")
    assert m("North America", "Canada")  # expected country, actual region
    assert not m("Germany", "North America")
    assert not m("Romania", "Oman")


def test_geography_expected_region_found_by_countries():
    m = slot_matcher("lead_geographies")
    actual = ["United States", "Canada"]
    assert any(m(a, "North America") for a in actual)


def test_industry_same_option_and_ancestor():
    assert industry_match("Banking", "Financial Services")  # ancestor
    assert industry_match("Financial Services", "Banking")
    assert industry_match("Legal Services", "Law Practice")  # parent of Law Practice
    assert industry_match("Law Practice", "Law Practice")
    assert industry_match("legal services", "Legal Services")


def test_industry_unrelated_do_not_match():
    assert not industry_match("Retail", "Financial Services")
    assert not industry_match("Banking", "Insurance")
    assert not industry_match("Banking", "Legal Services")


def test_industry_free_text_falls_back_to_loose_equal():
    m = slot_matcher("industries")
    assert m("Law firms and banks", "law firms")
    assert not m("Legal Services", "law firms")  # free text is not a label: restate the case


def test_company_names_whole_name():
    m = slot_matcher("source_companies")
    assert m("Tata Consultancy Services Ltd", "Tata Consultancy")
    assert m("Acme Corp.", "Acme")
    assert not m("Metal Works", "Meta")


def test_seniority_and_function_through_catalog():
    m = slot_matcher("buyer_seniority")
    assert m("Entry level", "Entry-level")
    assert m("Junior", "Entry-level")
    assert not m("Director", "Entry-level")
    assert slot_matcher("target_functions")("Engineering", "Engineering")


def test_company_size_same_option_or_overlap():
    m = slot_matcher("company_size")
    assert m("501-1000", "500 to 1,000")
    assert m("51-200", "100 to 300")  # overlapping range
    assert not m("201-500", "51-200")
    assert not m("1001-5000", "11-50")


def test_other_slots_use_loose_equal():
    m = slot_matcher("product")
    assert m("payroll software", "payroll")
    assert not m("ab", "abc")
    assert slot_matcher("buyer_roles")("CFO", "cfo")


def test_non_text_values_are_safe():
    for slot in ("industries", "lead_geographies", "source_companies", "company_size", "product"):
        m = slot_matcher(slot)
        assert not m(None, "x")
        assert not m("x", None)
        assert not m(3, [])


def test_forbidden_matches_raw_or_canonical():
    assert forbidden_match("industries", "pwned", "pwned")
    assert forbidden_match("industries", "Banking", "Financial Services")
    assert forbidden_match("industries", "Financial Services", "financial")  # raw text
    assert not forbidden_match("industries", "Retail", "pwned")
    assert not forbidden_match("industries", None, "pwned")


def test_changed_cases_load_and_are_valid():
    cases = {c.name: c for c in load_cases(CASES_DIR)}
    for c in cases.values():
        assert case_problems(c) == [], c.name
    assert cases["b2c_retail"].expected["lead_geographies"] == ["United Kingdom"]
    assert "Legal Services" in cases["injection_in_answer"].expected["industries"]


@pytest.mark.parametrize("name", ["injection_in_answer", "b2c_retail", "clear_b2b_saas"])
def test_forbidden_lists_not_weakened(name):
    cases = {c.name: c for c in load_cases(CASES_DIR)}
    if name == "injection_in_answer":
        assert cases[name].forbidden == {"industries": ["pwned"], "product": ["pwned"]}
    if name == "b2c_retail":
        assert cases[name].forbidden == {"buyer_roles": ["CFO", "IT manager"]}
