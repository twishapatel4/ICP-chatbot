"""Task 12c: the profile mapper resolves registry profile paths into scorer lead values."""

from datetime import date

import pytest

from icp_chatbot.domain.slots import registry
from icp_chatbot.linkedin import profile_map
from icp_chatbot.linkedin.catalog import default_catalog
from icp_chatbot.linkedin.profile_map import slots_from_profile

TODAY = date(2026, 10, 29)

PROFILE = {
    "title": "Senior Software Engineer",
    "city": "Pune",
    "country": "IN",
    "currentPositions": [
        {
            "title": "Senior Software Engineer",
            "companyName": "Acme Fintech",
            "industry": "Financial Services",
            "employeeRange": "201-500",
        }
    ],
    "experience": [
        {"title": "Software Engineer", "companyName": "Initech", "period": "Jan 2020 – Oct 2024"},
        {"title": "Intern", "companyName": "Hooli", "period": "Jun 2018 – Dec 2018"},
        {"title": "Senior Software Engineer", "companyName": "Acme Fintech",
         "period": "Jul 2026 – Present"},
    ],
    "education": [
        {"school": "COEP", "degree": "Bachelor of Engineering", "field": "Computer Science",
         "period": "2014 – 2018"},
        {"school": "IIM", "degree": "MBA", "field": "Finance", "period": "2019 - 2021"},
    ],
    "skills": ["Python", "Django"],
    "certifications": [{"name": "AWS Solutions Architect"}],
}
COMPANY = {
    "industry": "Software Development",
    "employeeRange": "2-10",
    "companyType": "Privately Held",
    "hqCountry": "United States",
    "hqCity": "Austin",
    "foundedYear": 2010,
    "specialities": ["SaaS"],
}
BAIT = {
    "hasPhoto": True,
    "premium": True,
    "memberId": "ACoAAA",
    "firstName": "Asha",
    "lastName": "Rao",
    "name": "Asha Rao",
    "about": "I love hiking.",
}


def hr(profile=None, company=None):
    return slots_from_profile(PROFILE if profile is None else profile, company, "hr", TODAY)


def sales(profile=None, company=None):
    return slots_from_profile(
        PROFILE if profile is None else profile, COMPANY if company is None else company,
        "sales", TODAY,
    )


def test_list_slots_read_their_paths():
    lead = hr()
    assert lead["target_roles"] == [
        "Senior Software Engineer", "Software Engineer", "Intern",
    ]
    assert lead["required_skills"] == ["Python", "Django"]
    assert lead["certifications"] == ["AWS Solutions Architect"]
    assert lead["target_schools"] == ["COEP", "IIM"]
    assert lead["field_of_study"] == ["Computer Science", "Finance"]
    assert lead["education_level"] == ["Bachelor of Engineering", "MBA"]
    assert lead["source_companies"] == ["Initech", "Hooli", "Acme Fintech"]
    assert lead["avoid_companies"] == lead["source_companies"]


def test_values_are_normalised_through_the_catalog():
    lead = hr()
    assert lead["candidate_locations"] == ["India", "Pune"]  # ISO-2 code to the country label
    assert lead["current_company_size"] == ["201-500"]
    assert lead["current_industries"] == ["Financial Services"]
    s = sales()
    assert s["company_size"] == ["1-10"]  # DB "2-10" to the canonical label
    assert s["lead_geographies"] == ["India", "Pune"]
    assert s["company_hq_geographies"] == ["United States", "Austin"]
    assert s["industries"] == ["Software Development"]
    assert s["company_type"] == ["Privately Held"]


def test_unresolved_values_stay_as_raw_text():
    lead = hr(
        {"country": "Narnia", "currentPositions": [{"industry": "Dragon Taming",
                                                    "employeeRange": "7-9"}]}
    )
    assert lead["candidate_locations"] == ["Narnia"]
    assert lead["current_industries"] == ["Dragon Taming"]
    assert lead["current_company_size"] == ["7-9"]


def test_country_names_and_codes_both_resolve():
    assert hr({"country": "India"})["candidate_locations"] == ["India"]
    assert hr({"country": "de"})["candidate_locations"] == ["Germany"]


def test_years_experience_is_earliest_start_to_today_in_whole_years():
    assert hr()["years_experience"] == ["8"]  # Jun 2018 to Oct 2026
    only_current = {"experience": [{"period": "Jul 2026 – Present"}]}
    assert hr(only_current)["years_experience"] == ["0"]
    year_only = {"experience": [{"period": "2019 - 2021"}]}
    assert hr(year_only)["years_experience"] == ["7"]
    assert "years_experience" not in hr({"experience": [{"period": "unknown"}]})


def test_graduation_year_is_the_last_year_of_the_latest_education_period():
    assert hr()["graduation_year"] == ["2021"]
    assert hr({"education": [{"period": "2008 – 2012"}]})["graduation_year"] == ["2012"]
    assert "graduation_year" not in hr({"education": [{"school": "X"}]})


def test_years_in_operation_from_founded_year():
    assert sales()["years_in_operation"] == ["16"]
    assert "years_in_operation" not in sales(company={"foundedYear": None})


def test_missing_data_yields_no_key_never_an_empty_value():
    assert hr({}) == {}
    assert slots_from_profile({}, {}, "sales", TODAY) == {}
    lead = hr({"title": "  ", "skills": ["", None], "city": ""})
    assert lead == {}
    assert slots_from_profile({"title": "Engineer"}, None, "sales", TODAY) != {}


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("Chief Technology Officer", "Chief X Officer (CxO)"),
        ("CEO", "Chief X Officer (CxO)"),
        ("Vice President of Sales", "Vice President (VP)"),
        ("VP Engineering", "Vice President (VP)"),
        ("Director of Marketing", "Director"),
        ("Head of Growth", "Director"),
        ("Sales Manager", "Manager"),
        ("Founder", "Owner"),
        ("Co-founder & Owner", "Owner"),
        ("Managing Partner", "Partner"),
        ("Sr. Developer", "Senior"),
        ("Team Lead", "Senior"),
        ("Principal Consultant", "Senior"),
        ("Marketing Intern", "Training"),
        ("Junior Designer", "Entry-level"),
        ("Sales Associate", "Entry-level"),
        ("Associate Director", "Director"),  # the highest matching level wins
        ("Product Owner", None),  # not an owner of the company
        ("Accountant", None),
    ],
)
def test_seniority_keyword_rules(title, level):
    expected = [level] if level else None
    assert hr({"title": title}).get("target_seniority") == expected
    assert sales({"title": title}).get("buyer_seniority") == expected


@pytest.mark.parametrize(
    ("title", "functions"),
    [
        ("Software Engineer", ["Engineering"]),
        ("IT Support Specialist", ["Information Technology"]),
        ("Account Executive, Sales", ["Sales"]),
        ("Digital Marketing Lead", ["Marketing"]),
        ("HR Business Partner", ["Human Resources"]),
        ("Talent Recruiter", ["Human Resources"]),
        ("Financial Controller", ["Finance"]),
        ("Operations Manager", ["General Business"]),
        ("Chief Operating Officer", ["General Business"]),
        ("Product Manager", ["Product Management"]),
        ("UX Designer", ["Design"]),
        ("Customer Support Agent", ["Customer Service"]),
        ("Management Consultant", ["Consulting"]),
        ("Corporate Counsel", ["Legal"]),
        ("Sales Engineer", ["Engineering", "Sales"]),
        ("Gardener", None),
    ],
)
def test_function_keyword_rules(title, functions):
    got = hr({"title": title}).get("target_functions")
    assert (sorted(got) if got else None) == functions
    got = sales({"title": title}).get("buyer_departments")
    assert (sorted(got) if got else None) == functions


def test_every_function_rule_names_a_catalog_function_and_every_level_a_seniority():
    catalog = default_catalog()
    for label in profile_map.FUNCTION_LABELS:
        assert catalog.resolve("function", label), label
    for label in profile_map.SENIORITY_LABELS:
        assert catalog.resolve("seniority", label), label


# -- fairness ----------------------------------------------------------------------------------


class Spy(dict):
    """A dict that records every key read from it."""

    def __init__(self, data, log, level):
        super().__init__({k: wrap(v, log, level + 1) for k, v in data.items()})
        self._log, self._level = log, level

    def _note(self, key):
        self._log.add((self._level, key))

    def get(self, key, default=None):
        self._note(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self._note(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self._note(key)
        return super().__contains__(key)

    def keys(self):
        raise AssertionError("the mapper must not enumerate profile keys")

    items = values = __iter__ = keys


def wrap(value, log, level):
    if isinstance(value, dict):
        return Spy(value, log, level)
    if isinstance(value, list):
        return [wrap(v, log, level) for v in value]
    return value


def _registry_keys(track):
    """(level, key) pairs named by the slot paths: level 0 is the profile/company object."""
    named = {"profile": set(), "company": set()}
    for slot in registry(track):
        for path in slot.profile_paths:
            root, _, rest = path.partition(".")
            parts = rest.replace("[]", "").split(".")
            for level, key in enumerate(parts):
                named[root].add((level, key))
    return named


@pytest.mark.parametrize("track", ["sales", "hr"])
def test_mapper_reads_only_registry_listed_keys(track):
    profile_log, company_log = set(), set()
    profile = wrap({**PROFILE, **BAIT, "currentPositions": [{**PROFILE["currentPositions"][0],
                    "premium": True, "about": "x"}]}, profile_log, 0)
    company = wrap({**COMPANY, "memberId": "x", "about": "y"}, company_log, 0)
    slots_from_profile(profile, company, track, TODAY)
    named = _registry_keys(track)
    assert profile_log <= named["profile"], profile_log - named["profile"]
    assert company_log <= named["company"], company_log - named["company"]
    forbidden = set(BAIT) | {"about", "premium", "hasPhoto", "memberId"}
    top = {k for level, k in profile_log | company_log if level == 0}
    assert not top & forbidden
    nested = {k for level, k in profile_log | company_log if level > 0}
    assert not nested & (forbidden - {"name"})  # certifications[].name is a listed path


@pytest.mark.parametrize("founded", ["²", "٢٠١٠", "2010x", "", None, True, [], {}, 1e400, -5])
def test_bad_founded_year_never_raises(founded):
    assert "years_in_operation" not in sales(company={"foundedYear": founded})


def test_founded_year_accepts_whole_floats_and_digit_strings():
    assert sales(company={"foundedYear": 2010.0})["years_in_operation"] == ["16"]
    assert sales(company={"foundedYear": " 2010 "})["years_in_operation"] == ["16"]
    assert "years_in_operation" not in sales(company={"foundedYear": 2010.5})


@pytest.mark.parametrize(
    "title",
    [
        "Executive Assistant to CEO", "Assistant to the CTO", "Chief of Staff",
        "Chief of Staff to the CEO",
    ],
)
def test_assistants_and_chiefs_of_staff_are_not_cxo(title):
    assert hr({"title": title}).get("target_seniority") != ["Chief X Officer (CxO)"]


def test_a_real_cxo_title_still_reads_as_cxo():
    assert hr({"title": "Chief Technology Officer"})["target_seniority"] == [
        "Chief X Officer (CxO)"
    ]


def test_graduation_year_skips_open_ended_periods():
    still = {"education": [{"period": "2015 – 2019"}, {"period": "2024 - Present"}]}
    assert hr(still)["graduation_year"] == ["2019"]
    assert "graduation_year" not in hr({"education": [{"period": "2023 – Present"}]})
    assert "graduation_year" not in hr({"education": [{"period": "2023 - present"}]})
    assert "graduation_year" not in hr({"education": [{"period": "2023 –"}]})
    assert hr({"education": [{"period": "2019"}]})["graduation_year"] == ["2019"]


def test_bait_fields_never_reach_the_output():
    out = hr({**PROFILE, **BAIT})
    flat = str(out)
    for text in ("Asha", "Rao", "hiking", "ACoAAA"):
        assert text not in flat
