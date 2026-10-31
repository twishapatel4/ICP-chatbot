from datetime import date

import pytest

from icp_chatbot.domain.slots import registry, slot_map
from icp_chatbot.icp.scoring import (
    CRITERIA_DEFS,
    Criterion,
    Disqualifier,
    MatchRule,
    ScoringModel,
    build_rule,
    company_match,
    default_scoring_model,
    list_match,
    make_disqualifier,
    parse_range,
    range_match,
    refresh_model,
    score_lead,
    set_requirement,
    set_weights,
    validate_model,
)
from icp_chatbot.linkedin.profile_map import slots_from_profile


@pytest.mark.parametrize(
    "text,expected",
    [
        ("50-500 employees", (50, 500)), ("1,001-5,000", (1001, 5000)), ("201-1,000", (201, 1000)),
        ("$1M-$10M", (1e6, 1e7)), ("$250M-$1B", (2.5e8, 1e9)), ("5,000+", (5000, None)),
        ("<$1M", (None, 1e6)), (">$1B", (1e9, None)), ("under 100", (None, 100)),
        ("at least 50 employees", (50, None)), ("250", (250, 250)), ("5 million", (5e6, 5e6)),
        ("up to 2 million", (None, 2e6)), ("500 marketing staff", (500, 500)),
        # LinkedIn's company size bands, as labelled and as the LinkedIn DB stores them
        ("1-10", (1, 10)), ("2-10", (2, 10)), ("201-500", (201, 500)), ("1001-5000", (1001, 5000)),
        ("5001-10,000", (5001, 10000)), ("5001-10000", (5001, 10000)),
        ("10,001+", (10001, None)), ("10001+", (10001, None)), ("Self-employed", None),
    ],
)  # fmt: skip
def test_parse_range(text, expected):
    assert parse_range(text) == expected


def test_parse_range_returns_none_without_numbers():
    assert parse_range("mid-sized") is None


def test_range_match_is_one_inside_half_near_and_zero_far():
    rule = MatchRule(kind="range", min=50, max=500, tolerance=0.2)
    assert range_match(50, rule) == 1.0 and range_match(500, rule) == 1.0
    assert range_match(45, rule) == 0.5  # 5 below, within 20% of 50
    assert range_match(590, rule) == 0.5  # 90 above, within 20% of 500 (=100)
    assert range_match(10, rule) == 0.0 and range_match(1000, rule) == 0.0
    assert range_match(9999, MatchRule(kind="range", min=100)) == 1.0  # open-ended


def test_list_match_is_case_insensitive_and_loose():
    rule = MatchRule(kind="list", primary=["Financial Services"], secondary=["Insurance"])
    assert list_match(["financial services"], rule) == 1.0
    assert list_match(["Financial services and banking"], rule) == 1.0
    assert list_match(["insurance"], rule) == 0.5
    assert list_match(["retail"], rule) == 0.0
    assert (
        list_match(["IT"], MatchRule(kind="list", primary=["Fintech", "IT services"])) == 0.0
    )  # too short to match loosely


def test_build_rule_reads_linkedin_size_bands():
    assert build_rule("company_size", ["1-10", "5001-10,000"]) == MatchRule(
        kind="range", min=1, max=10000
    )
    assert build_rule("company_size", ["201-500", "10,001+"]) == MatchRule(
        kind="range", min=201, max=None
    )


def test_geographic_fit_reads_lead_and_hq_geographies():
    geo = next(d for d in CRITERIA_DEFS if d[0] == "geographic_fit")
    assert geo[2] == 20.0 and geo[3] == ("lead_geographies", "company_hq_geographies")
    model = default_scoring_model(
        {"lead_geographies": ["India"], "company_hq_geographies": ["United States"]}
    )
    criterion = next(c for c in model.criteria if c.key == "geographic_fit")
    assert set(criterion.rules) == {"lead_geographies", "company_hq_geographies"}
    assert criterion.applicable and validate_model(model) == []
    lead = {"lead_geographies": ["India"], "company_hq_geographies": ["Germany"]}
    assert score_lead(model, lead).breakdown["geographic_fit"] == 0.5  # one of two fits


def test_build_rule_uses_ranges_for_size_and_lists_for_the_rest():
    assert build_rule("company_size", ["51-200", "201-1,000"]) == MatchRule(
        kind="range", min=51, max=1000
    )
    assert build_rule("company_size", ["5,000+"]).max is None
    assert build_rule("company_size", ["mid-sized"]).kind == "list"  # unparseable: falls back
    assert build_rule("industries", ["Fintech"]).primary == ["Fintech"]
    assert build_rule("industries", ["  "]) is None


FULL = {
    "industries": ["Fintech"], "customer_segments": ["SMB"], "company_size": ["50-500"],
    "buying_triggers": ["new funding"], "lead_geographies": ["Europe"],
}  # fmt: skip
PERFECT_LEAD = {
    "industries": ["Fintech"], "customer_segments": ["SMB"], "company_size": ["120"],
    "buying_triggers": ["New funding round"], "lead_geographies": ["Europe"],
}  # fmt: skip


def test_default_model_has_the_spec_weights_and_is_valid():
    model = default_scoring_model(FULL)
    assert [(c.key, c.weight) for c in model.criteria] == [(k, w) for k, _, w, _ in CRITERIA_DEFS]
    assert [c.weight for c in model.criteria] == [25, 15, 5, 5, 5, 5, 20, 10, 5, 5]
    assert sum(c.weight for c in model.criteria) == 100
    assert validate_model(model) == []
    assert {c.key for c in model.criteria if c.applicable} == {
        "industry_fit", "company_size", "buying_signals", "geographic_fit"
    }


def test_a_criterion_without_icp_data_is_not_applicable():
    model = default_scoring_model({"industries": ["Fintech"]})
    applicable = {c.key for c in model.criteria if c.applicable}
    assert applicable == {"industry_fit"}


def test_a_perfect_lead_scores_100_and_a_poor_one_scores_low():
    model = default_scoring_model(FULL)
    perfect = score_lead(model, PERFECT_LEAD)
    assert perfect.score == 100.0 and perfect.qualified and perfect.completeness == 1.0
    poor = score_lead(model, {**PERFECT_LEAD, "industries": ["Retail"], "lead_geographies": ["Asia"]})
    assert poor.score == pytest.approx(100 * (25 / 2 + 15 + 5) / 65)  # segments half-fit industry


def test_unknown_lead_data_drops_the_criterion_and_renormalizes():
    model = default_scoring_model(FULL)
    lead = {"industries": ["Fintech"], "lead_geographies": ["Europe"]}  # only 25 + 20 weight known
    result = score_lead(model, lead)
    assert result.score == 100.0
    assert result.completeness == pytest.approx(2 / 4)
    half = score_lead(model, {"industries": ["Fintech"], "lead_geographies": ["Asia"]})
    assert half.score == pytest.approx(100 * 25 / 45)


def test_partial_slot_data_averages_within_a_criterion():
    model = default_scoring_model(FULL)
    lead = {"industries": ["Fintech"], "customer_segments": ["Enterprise"]}  # one fits, one not
    result = score_lead(model, lead)
    assert result.breakdown["industry_fit"] == 0.5


def test_a_lead_with_no_readable_data_scores_zero():
    result = score_lead(default_scoring_model(FULL), {})
    assert result.score == 0.0 and result.completeness == 0.0 and result.qualified


def test_required_criteria_decide_qualification_not_the_score():
    model = set_requirement(default_scoring_model(FULL), "geographic_fit", "required")
    wrong_place = score_lead(model, {**PERFECT_LEAD, "lead_geographies": ["Asia"]})
    assert not wrong_place.qualified and wrong_place.failed_required == ["geographic_fit"]
    assert wrong_place.score == pytest.approx(100 * 45 / 65)  # still scored, but not qualified
    unknown_place = {k: v for k, v in PERFECT_LEAD.items() if k != "lead_geographies"}
    assert score_lead(model, unknown_place).failed_required == ["geographic_fit"]
    assert score_lead(model, PERFECT_LEAD).qualified


def test_disqualifiers_exclude_a_lead_outright():
    model = default_scoring_model(FULL)
    model = model.model_copy(update={"disqualifiers": [
        Disqualifier(field="industries", operator="in", values=["Government"]),
        Disqualifier(field="lead_geographies", operator="not_in", values=["Europe", "North America"]),
        Disqualifier(field="company_size", operator="lt", values=["50"]),
    ]})  # fmt: skip
    assert score_lead(model, PERFECT_LEAD).qualified
    for bad in (
        {**PERFECT_LEAD, "industries": ["Government services"]},
        {**PERFECT_LEAD, "lead_geographies": ["Asia"]},
        {**PERFECT_LEAD, "company_size": ["30"]},
    ):
        result = score_lead(model, bad)
        assert result.score == 0.0 and not result.qualified and result.disqualified_by


def test_unknown_lead_data_never_disqualifies():
    model = ScoringModel(
        criteria=default_scoring_model(FULL).criteria,
        disqualifiers=[Disqualifier(field="lead_geographies", operator="not_in", values=["Europe"])],
    )
    assert score_lead(model, {"industries": ["Fintech"]}).disqualified_by is None


def test_make_disqualifier_rejects_unusable_rules():
    assert make_disqualifier("industries", "in", ["Government"]) is not None
    assert make_disqualifier("company_size", "lt", ["50"]) is not None
    assert make_disqualifier("favourite_colour", "in", ["blue"]) is None
    assert make_disqualifier("industries", "contains", ["x"]) is None
    assert make_disqualifier("industries", "in", ["  "]) is None
    assert make_disqualifier("company_size", "lt", ["small"]) is None


def test_validate_model_reports_every_kind_of_problem():
    good = default_scoring_model(FULL)
    broken = good.model_copy(update={"criteria": [
        good.criteria[0].model_copy(update={"weight": 50}),
        good.criteria[0].model_copy(update={"weight": 50}),
        Criterion(key="x", label="x", weight=0, reads=["nope"], rules={
            "industries": MatchRule(kind="list"),
            "company_size": MatchRule(kind="range"),
            "lead_geographies": MatchRule(kind="range", min=5, max=1),
        }),
    ], "disqualifiers": [Disqualifier(field="nope", operator="in", values=[])]})  # fmt: skip
    text = " | ".join(validate_model(broken))
    for fragment in (
        "keys must be unique", "unknown slot nope", "does not read", "at least one primary",
        "minimum or a maximum", "minimum is above maximum", "unknown field nope", "no values",
    ):  # fmt: skip
        assert fragment in text, fragment


def test_validate_model_reports_weights_that_do_not_sum_to_100():
    model = default_scoring_model(FULL)
    broken = model.model_copy(update={"criteria": model.criteria[:-1]})
    assert any("weights sum to 95" in e for e in validate_model(broken))


def test_set_weights_requires_a_total_of_100():
    model = default_scoring_model(FULL)
    updated = set_weights(model, {"industry_fit": 30, "geographic_fit": 15})
    assert {c.key: c.weight for c in updated.criteria}["industry_fit"] == 30
    with pytest.raises(ValueError, match="sum to 100"):
        set_weights(model, {"industry_fit": 30})
    with pytest.raises(ValueError, match="unknown"):
        set_weights(model, {"nope": 1})
    with pytest.raises(ValueError, match="negative"):
        set_weights(model, {"industry_fit": -5, "geographic_fit": 35})


def test_set_requirement_validates_its_input():
    model = default_scoring_model(FULL)
    with pytest.raises(ValueError):
        set_requirement(model, "industry_fit", "maybe")
    with pytest.raises(ValueError):
        set_requirement(model, "nope", "required")


def test_refresh_model_updates_rules_but_keeps_the_users_choices():
    model = default_scoring_model(FULL)
    model = set_requirement(
        set_weights(model, {"industry_fit": 30, "geographic_fit": 15}), "industry_fit", "required"
    )
    model = model.model_copy(
        update={
            "disqualifiers": [
                Disqualifier(field="industries", operator="in", values=["Government"])
            ]
        }
    )
    refreshed = refresh_model(model, {"industries": ["Retail"]})
    industry = next(c for c in refreshed.criteria if c.key == "industry_fit")
    assert industry.rules["industries"].primary == ["Retail"]
    assert industry.weight == 30 and industry.requirement == "required"
    assert [c.key for c in refreshed.criteria if c.applicable] == ["industry_fit"]
    assert refreshed.disqualifiers == model.disqualifiers


def test_model_round_trips_through_json():
    model = default_scoring_model(FULL)
    assert ScoringModel.model_validate_json(model.model_dump_json()) == model


# -- HR track -------------------------------------------------------------------------------------


def _hr_model(values, **kw):
    return default_scoring_model(values, track="hr", **kw)


def _crit(model, key):
    return next(c for c in model.criteria if c.key == key)


HR_WEIGHTS = [
    ("role_fit", 20.0), ("function_fit", 5.0), ("seniority_fit", 5.0), ("experience_fit", 15.0),
    ("skills_fit", 15.0), ("certifications_fit", 5.0), ("education_fit", 10.0),
    ("school_fit", 5.0), ("location_fit", 10.0), ("background_fit", 5.0), ("employer_fit", 5.0),
]
SALES_WEIGHTS = [
    ("industry_fit", 25.0), ("company_size", 15.0), ("company_type_fit", 5.0),
    ("company_age_fit", 5.0), ("services_fit", 5.0), ("buying_signals", 5.0),
    ("geographic_fit", 20.0),
    ("buyer_fit", 10.0), ("seniority_fit", 5.0), ("department_fit", 5.0),
]


def test_hr_default_weights_sum_to_100_and_use_the_eleven_criteria():
    model = _hr_model({})
    assert [(c.key, c.weight) for c in model.criteria] == HR_WEIGHTS
    assert sum(c.weight for c in model.criteria) == 100.0
    assert _crit(model, "education_fit").reads == [
        "education_level", "field_of_study", "graduation_year"
    ]
    assert _crit(model, "function_fit").reads == ["target_functions"]
    assert _crit(model, "seniority_fit").reads == ["target_seniority"]
    assert validate_model(model, track="hr") == []


def test_sales_default_weights_sum_to_100_and_use_the_ten_criteria():
    model = default_scoring_model({})
    assert [(c.key, c.weight) for c in model.criteria] == SALES_WEIGHTS
    assert sum(c.weight for c in model.criteria) == 100.0
    reads = {c.key: c.reads for c in model.criteria}
    assert reads["buyer_fit"] == ["buyer_roles"]
    assert reads["seniority_fit"] == ["buyer_seniority"]
    assert reads["department_fit"] == ["buyer_departments"]
    assert reads["company_type_fit"] == ["company_type"]
    assert reads["company_age_fit"] == ["years_in_operation"]
    assert reads["services_fit"] == ["services_offered"]
    assert validate_model(model) == []


def test_derived_criteria_never_weigh_more_than_five():
    for track, derived in (
        ("hr", ("target_functions", "target_seniority")),
        ("sales", ("buyer_seniority", "buyer_departments")),
    ):
        for c in default_scoring_model({}, track=track).criteria:
            if set(c.reads) & set(derived):
                assert c.weight <= 5.0, c.key


def test_criterion_labels_for_hr_avoid_sales_words():
    for c in _hr_model({}).criteria:
        assert not any(w in c.label.lower() for w in ("icp", "lead", "customer")), c.label


def test_nice_to_have_skills_give_half_credit_to_required_skills():
    model = _hr_model({"required_skills": ["Python"], "nice_to_have_skills": ["Docker"]})
    skills = _crit(model, "skills_fit")
    assert skills.applicable and validate_model(model, track="hr") == []
    assert skills.reads == ["required_skills", "nice_to_have_skills"]
    assert skills.rules["required_skills"] == MatchRule(
        kind="list", primary=["Python"], secondary=["Docker"]
    )
    assert set(skills.rules) == {"required_skills"}
    assert score_lead(model, {"required_skills": ["Python"]}).breakdown["skills_fit"] == 1.0
    assert score_lead(model, {"required_skills": ["Docker"]}).breakdown["skills_fit"] == 0.5
    assert score_lead(model, {"required_skills": ["Go"]}).breakdown["skills_fit"] == 0.0


def test_nice_to_have_alone_builds_no_skills_rule_and_any_builds_an_open_one():
    assert not _crit(_hr_model({"nice_to_have_skills": ["Docker"]}), "skills_fit").applicable
    open_skills = _crit(_hr_model({"required_skills": ["Any"]}), "skills_fit")
    assert open_skills.applicable and open_skills.rules["required_skills"].kind == "open"
    only_required = _crit(_hr_model({"required_skills": ["Python"]}), "skills_fit")
    assert only_required.rules["required_skills"].secondary == []


def test_refresh_keeps_the_secondary_skills_list():
    model = _hr_model({"required_skills": ["Python"]})
    refreshed = refresh_model(model, {"required_skills": ["Go"], "nice_to_have_skills": ["Rust"]})
    assert _crit(refreshed, "skills_fit").rules["required_skills"].secondary == ["Rust"]


@pytest.mark.parametrize(
    ("key", "values", "lead", "expected"),
    [
        ("role_fit", {"target_roles": ["Backend Developer"]},
         {"target_roles": ["Senior Backend Developer"]}, 1.0),
        ("function_fit", {"target_functions": ["Engineering"]},
         {"target_functions": ["Engineering"]}, 1.0),
        ("function_fit", {"target_functions": ["Engineering"]},
         {"target_functions": ["Sales"]}, 0.0),
        ("seniority_fit", {"target_seniority": ["Senior"]},
         {"target_seniority": ["Senior"]}, 1.0),
        ("certifications_fit", {"certifications": ["AWS Certified"]},
         {"certifications": ["AWS Certified Solutions Architect"]}, 1.0),
        ("school_fit", {"target_schools": ["IIT Bombay"]}, {"target_schools": ["IIT Bombay"]}, 1.0),
        ("school_fit", {"target_schools": ["IIT Bombay"]}, {"target_schools": ["COEP"]}, 0.0),
        ("employer_fit", {"source_companies": ["Infosys"]},
         {"source_companies": ["Infosys Ltd"]}, 1.0),
        ("background_fit", {"current_industries": ["Banking"]},
         {"current_industries": ["Banking"]}, 1.0),
        ("experience_fit", {"years_experience": ["3-8"]}, {"years_experience": ["5"]}, 1.0),
        ("education_fit", {"graduation_year": ["2020-2024"]}, {"graduation_year": ["2022"]}, 1.0),
        ("location_fit", {"candidate_locations": ["India"]},
         {"candidate_locations": ["India", "Pune"]}, 1.0),
    ],
)  # fmt: skip
def test_each_new_hr_criterion_scores_its_slot(key, values, lead, expected):
    model = _hr_model(values)
    assert _crit(model, key).applicable
    assert score_lead(model, lead).breakdown[key] == expected


@pytest.mark.parametrize(
    ("key", "values", "lead", "expected"),
    [
        ("company_type_fit", {"company_type": ["Privately Held"]},
         {"company_type": ["Privately Held"]}, 1.0),
        ("company_type_fit", {"company_type": ["Privately Held"]},
         {"company_type": ["Nonprofit"]}, 0.0),
        ("company_age_fit", {"years_in_operation": ["5-20"]}, {"years_in_operation": ["10"]}, 1.0),
        ("company_age_fit", {"years_in_operation": ["5-20"]}, {"years_in_operation": ["1"]}, 0.0),
        ("buyer_fit", {"buyer_roles": ["VP Sales"]}, {"buyer_roles": ["VP Sales, EMEA"]}, 1.0),
        ("seniority_fit", {"buyer_seniority": ["Director"]},
         {"buyer_seniority": ["Director"]}, 1.0),
        ("department_fit", {"buyer_departments": ["Sales"]},
         {"buyer_departments": ["Marketing"]}, 0.0),
    ],
)  # fmt: skip
def test_each_new_sales_criterion_scores_its_slot(key, values, lead, expected):
    model = default_scoring_model(values)
    assert _crit(model, key).applicable
    assert score_lead(model, lead).breakdown[key] == expected


def test_years_in_operation_is_a_range_slot():
    rule = build_rule("years_in_operation", ["5+"])
    assert rule.kind == "range" and rule.min == 5 and rule.max is None


def test_avoid_companies_is_a_hr_disqualifier():
    d = make_disqualifier("avoid_companies", "in", ["Competitor Inc"])
    assert d is not None
    model = _hr_model({"required_skills": ["Python"]}, disqualifiers=[d])
    assert validate_model(model, track="hr") == []
    result = score_lead(model, {"avoid_companies": ["Acme", "Competitor Inc"]})
    assert not result.qualified and result.score == 0.0
    assert score_lead(model, {"avoid_companies": ["Acme"]}).disqualified_by is None


UNSCORED_WITH_PATH = {
    "hr": {
        "current_company_hq": "no Lite filter and no criterion",
        "avoid_companies": "scored as a disqualifier, not a criterion",
    },
    "sales": {},
}


@pytest.mark.parametrize("track", ["sales", "hr"])
def test_every_criterion_reads_slots_of_its_track(track):
    reg = {s.key for s in registry(track)}
    for c in default_scoring_model({}, track=track).criteria:
        assert set(c.reads) <= reg, c.key


@pytest.mark.parametrize("track", ["sales", "hr"])
def test_every_slot_with_a_profile_path_is_scored_unless_allow_listed(track):
    read = {slot for c in default_scoring_model({}, track=track).criteria for slot in c.reads}
    for s in registry(track):
        if s.profile_paths and s.key not in UNSCORED_WITH_PATH[track]:
            assert s.key in read, s.key
    for key in UNSCORED_WITH_PATH[track]:
        assert key not in read and slot_map(track)[key].profile_paths


def test_slots_without_a_profile_path_are_unscored():
    read = {slot for c in _hr_model({}).criteria for slot in c.reads}
    for key in ("languages", "employment_type", "work_mode", "salary_range", "notice_period"):
        assert not slot_map("hr")[key].profile_paths and key not in read


def test_mapped_profile_scores_against_the_hr_defaults():
    profile = {
        "title": "Senior Backend Developer", "city": "Pune", "country": "IN",
        "skills": ["Python", "Docker"],
        "experience": [{"companyName": "Infosys", "period": "Jan 2018 – Present"}],
        "education": [
            {"school": "COEP", "degree": "BE", "field": "Computer Science",
             "period": "2014 – 2018"}
        ],
    }
    lead = slots_from_profile(profile, None, "hr", date(2026, 10, 29))
    model = _hr_model({
        "target_roles": ["Backend Developer"], "target_functions": ["Engineering"],
        "target_seniority": ["Senior"], "years_experience": ["5+"],
        "required_skills": ["Python"], "nice_to_have_skills": ["Go"],
        "candidate_locations": ["India"], "graduation_year": ["2016-2020"],
    })
    result = score_lead(model, lead)
    assert result.score == 100.0 and result.completeness == 1.0


def test_fresher_profile_scores_without_experience_and_background():
    model = _hr_model(
        {
            "target_functions": ["Engineering"],
            "required_skills": ["Python"],
            "education_level": ["Bachelor's"],
            "candidate_locations": ["India"],
        }
    )
    assert not _crit(model, "experience_fit").applicable
    assert not _crit(model, "background_fit").applicable
    lead = {
        "target_functions": ["Engineering"],
        "required_skills": ["Python"],
        "education_level": ["Bachelor's"],
        "candidate_locations": ["Germany"],
    }
    result = score_lead(model, lead)
    # function 5 + skills 15 + education 10 score 1, location 10 scores 0; 30 of 40 applicable
    assert result.score == pytest.approx(100 * 30 / 40, abs=1e-3)
    assert set(result.breakdown) == {"function_fit", "skills_fit", "education_fit", "location_fit"}
    assert result.completeness == 1.0


def test_years_experience_range_parses_3_dash_8_and_3_plus():
    rule = build_rule("years_experience", ["3-8"])
    assert rule.kind == "range" and (rule.min, rule.max) == (3, 8)
    model = _hr_model({"years_experience": ["3-8"]})
    assert score_lead(model, {"years_experience": ["6"]}).breakdown["experience_fit"] == 1.0
    assert score_lead(model, {"years_experience": ["1"]}).breakdown["experience_fit"] == 0.0
    open_rule = build_rule("years_experience", ["3+"])
    assert open_rule.min == 3 and open_rule.max is None


def test_graduation_year_far_outside_range_scores_zero():
    rule = build_rule("graduation_year", ["2024-2026"])
    assert rule.tolerance == 0.001
    assert range_match(2015, rule) == 0.0
    assert range_match(2027, rule) == 0.5
    assert range_match(2025, rule) == 1.0
    assert build_rule("company_size", ["50-500"]).tolerance == 0.2


def test_any_answer_builds_an_open_rule_that_keeps_its_priority():
    assert build_rule("industries", ["Any"]) == MatchRule(kind="open")
    assert build_rule("industries", ["Anyone"]) == MatchRule(kind="open")
    assert build_rule("industries", []) is None  # unknown is not open
    sales = default_scoring_model({"industries": ["Any"], "lead_geographies": ["India"]})
    assert _crit(sales, "industry_fit").applicable
    assert not _crit(sales, "company_size").applicable
    # an open criterion gives every lead full credit, even one with no data for it
    geo_miss = score_lead(sales, {"lead_geographies": ["Germany"]})
    geo_hit = score_lead(sales, {"lead_geographies": ["India"]})
    assert geo_hit.breakdown["industry_fit"] == 1.0 and geo_hit.score == 100.0
    assert geo_miss.breakdown["industry_fit"] == 1.0 and geo_miss.score < geo_hit.score


def test_hr_candidate_scores_skills_by_loose_match():
    model = _hr_model({"required_skills": ["Python"]})
    result = score_lead(model, {"required_skills": ["Python, Django"]})
    assert result.breakdown["skills_fit"] == 1.0 and result.score == 100.0


def test_validate_model_uses_the_tracks_registry():
    model = _hr_model({"required_skills": ["Python"]})
    assert validate_model(model, track="hr") == []
    problems = validate_model(model, track="sales")
    assert any("skills_fit reads unknown slot required_skills" in p for p in problems)


def test_hr_disqualifier_on_candidate_location():
    d = make_disqualifier("candidate_locations", "in", ["Germany"])
    assert d is not None
    assert make_disqualifier("current_industries", "in", ["Banking"]) is not None
    assert make_disqualifier("current_company_size", "lt", ["50"]) is not None
    model = _hr_model({"required_skills": ["Python"]}, disqualifiers=[d])
    assert validate_model(model, track="hr") == []
    result = score_lead(model, {"candidate_locations": ["Berlin, Germany"]})
    assert not result.qualified and result.score == 0.0
    assert result.disqualified_by is not None
    assert score_lead(model, {"candidate_locations": ["India"]}).disqualified_by is None


def test_required_hr_criterion_failing_makes_candidate_unqualified():
    model = set_requirement(
        _hr_model({"required_skills": ["Python"], "target_functions": ["Engineering"]}),
        "skills_fit",
        "required",
    )
    result = score_lead(model, {"required_skills": ["Java"], "target_functions": ["Engineering"]})
    assert not result.qualified and result.failed_required == ["skills_fit"]


# -- whole-name company matching (ruling R22) -----------------------------------------------------


@pytest.mark.parametrize(
    ("avoided", "candidate", "hit"),
    [
        ("Meta", "Metal Works", False),
        ("Meta", "Meta", True),
        ("Meta", "Meta Platforms", False),  # one-word names match exactly only
        ("Meta Platforms, Inc.", "meta platforms", True),
        ("Tata Consultancy", "Tata Consultancy Services Ltd", True),
        ("Apple", "Pineapple Labs", False),
        ("Acme Corp", "Acme Corporation", True),
        ("Tata Consultancy", "Tata Steel", False),
        ("", "Acme", False),
        ("Acme", "", False),
        ("Inc.", "Inc", False),  # only a legal suffix: nothing left to compare
    ],
)
def test_company_names_match_on_whole_names(avoided, candidate, hit):
    assert company_match(candidate, avoided) is hit


def test_avoid_companies_uses_whole_name_matching():
    d = make_disqualifier("avoid_companies", "in", ["Meta"])
    model = _hr_model({"required_skills": ["Python"]}, disqualifiers=[d])
    assert score_lead(model, {"avoid_companies": ["Metal Works"]}).disqualified_by is None
    assert score_lead(model, {"avoid_companies": ["Meta"]}).disqualified_by is not None
    assert score_lead(model, {"required_skills": ["Python"]}).disqualified_by is None  # unknown
    multi = make_disqualifier("avoid_companies", "in", ["Tata Consultancy"])
    model = _hr_model({}, disqualifiers=[multi])
    assert score_lead(model, {"avoid_companies": ["Tata Consultancy Services"]}).disqualified_by


def test_location_line_disqualifier_matches_by_whole_part():
    d = make_disqualifier("candidate_locations", "in", ["Germany"])
    model = _hr_model({}, disqualifiers=[d])
    assert score_lead(model, {"candidate_locations": ["Berlin, Germany"]}).disqualified_by


def test_employer_fit_matches_whole_company_names():
    model = _hr_model({"source_companies": ["Meta"]})
    assert score_lead(model, {"source_companies": ["Metal Works"]}).breakdown["employer_fit"] == 0
    assert score_lead(model, {"source_companies": ["Meta"]}).breakdown["employer_fit"] == 1.0
    model = _hr_model({"source_companies": ["Infosys"]})
    assert score_lead(model, {"source_companies": ["Infosys Ltd"]}).breakdown["employer_fit"] == 1.0


# -- models stored before Task 12c ----------------------------------------------------------------


def _old_model(rows, track):
    criteria = [Criterion(key=k, label=k, weight=w, reads=list(r)) for k, w, r in rows]
    return ScoringModel(criteria=criteria)


OLD_SALES = [
    ("industry_fit", 35.0, ("industries", "customer_segments")),
    ("company_size", 25.0, ("company_size",)),
    ("buying_signals", 15.0, ("buying_triggers",)),
    ("geographic_fit", 25.0, ("lead_geographies", "company_hq_geographies")),
]
OLD_HR = [
    ("role_fit", 25.0, ("target_functions", "target_roles")),
    ("experience_fit", 20.0, ("years_experience",)),
    ("skills_fit", 20.0, ("required_skills",)),
    ("education_fit", 15.0, ("education_level", "field_of_study", "graduation_year")),
    ("location_fit", 10.0, ("candidate_locations",)),
    ("background_fit", 10.0, ("current_industries", "current_company_size")),
]


@pytest.mark.parametrize(("rows", "track"), [(OLD_SALES, "sales"), (OLD_HR, "hr")])
def test_stored_pre_12c_models_keep_working_and_gain_no_criteria(rows, track):
    from icp_chatbot.icp.document import Completeness, ICPDocument
    from icp_chatbot.icp.review import carry_over_edits

    model = _old_model(rows, track)
    keys = [c.key for c in model.criteria]
    assert validate_model(model, track) == []
    values = {"industries": ["Fintech"], "required_skills": ["Python"],
              "target_roles": ["Engineer"]}
    refreshed = refresh_model(model, values)
    assert [c.key for c in refreshed.criteria] == keys
    assert validate_model(refreshed, track) == []
    first = keys[0]
    reweighted = set_weights(model, {first: model.criteria[0].weight - 5,
                                     keys[1]: model.criteria[1].weight + 5})
    assert [c.key for c in reweighted.criteria] == keys
    doc = ICPDocument(
        track=track, sections={}, scoring_model=model,
        completeness=Completeness(tier_a=0.0, tier_b=0.0),
    )
    carried = carry_over_edits(doc, doc)
    assert [c.key for c in carried.scoring_model.criteria] == keys


def test_an_old_hr_skills_criterion_picks_up_nice_to_have_skills_on_refresh():
    # SECONDARY_READS is keyed by criterion key, so a stored skills_fit that reads only
    # required_skills still gets the half-credit list when rules are rebuilt.
    model = _old_model(OLD_HR, "hr")
    refreshed = refresh_model(
        model, {"required_skills": ["Python"], "nice_to_have_skills": ["Docker"]}
    )
    skills = next(c for c in refreshed.criteria if c.key == "skills_fit")
    assert skills.reads == ["required_skills"]
    assert skills.rules["required_skills"].secondary == ["Docker"]
