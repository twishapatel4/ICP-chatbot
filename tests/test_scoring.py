import pytest

from icp_chatbot.icp.scoring import (
    CRITERIA_DEFS,
    Criterion,
    Disqualifier,
    MatchRule,
    ScoringModel,
    build_rule,
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
    assert geo[2] == 25.0 and geo[3] == ("lead_geographies", "company_hq_geographies")
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
    assert [c.weight for c in model.criteria] == [35, 25, 15, 25]
    assert sum(c.weight for c in model.criteria) == 100
    assert validate_model(model) == []
    assert all(c.applicable for c in model.criteria)


def test_a_criterion_without_icp_data_is_not_applicable():
    model = default_scoring_model({"industries": ["Fintech"]})
    applicable = {c.key for c in model.criteria if c.applicable}
    assert applicable == {"industry_fit"}


def test_a_perfect_lead_scores_100_and_a_poor_one_scores_low():
    model = default_scoring_model(FULL)
    perfect = score_lead(model, PERFECT_LEAD)
    assert perfect.score == 100.0 and perfect.qualified and perfect.completeness == 1.0
    poor = score_lead(model, {**PERFECT_LEAD, "industries": ["Retail"], "lead_geographies": ["Asia"]})
    assert poor.score == pytest.approx(100 - 35 / 2 - 25)  # segments still fit: half of industry_fit


def test_unknown_lead_data_drops_the_criterion_and_renormalizes():
    model = default_scoring_model(FULL)
    lead = {"industries": ["Fintech"], "lead_geographies": ["Europe"]}  # only 35 + 25 weight is known
    result = score_lead(model, lead)
    assert result.score == 100.0
    assert result.completeness == pytest.approx(2 / 4)
    half = score_lead(model, {"industries": ["Fintech"], "lead_geographies": ["Asia"]})
    assert half.score == pytest.approx(100 * 35 / 60)


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
    assert wrong_place.score == pytest.approx(75.0)  # still scored, but not qualified
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
    assert any("weights sum to 75" in e for e in validate_model(broken))


def test_set_weights_requires_a_total_of_100():
    model = default_scoring_model(FULL)
    updated = set_weights(model, {"industry_fit": 40, "geographic_fit": 20})
    assert {c.key: c.weight for c in updated.criteria}["industry_fit"] == 40
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
        set_weights(model, {"industry_fit": 45, "geographic_fit": 15}), "industry_fit", "required"
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
    assert industry.weight == 45 and industry.requirement == "required"
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


def test_hr_default_weights_sum_to_100_and_use_the_six_criteria():
    model = _hr_model({})
    assert [(c.key, c.weight) for c in model.criteria] == [
        ("role_fit", 25.0), ("experience_fit", 20.0), ("skills_fit", 20.0),
        ("education_fit", 15.0), ("location_fit", 10.0), ("background_fit", 10.0),
    ]  # fmt: skip
    assert sum(c.weight for c in model.criteria) == 100.0
    assert _crit(model, "education_fit").reads == [
        "education_level", "field_of_study", "graduation_year"
    ]
    assert validate_model(model, track="hr") == []


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
    # role 25 + skills 20 + education 15 score 1, location 10 scores 0; 60 of 70 applicable weight
    assert result.score == pytest.approx(100 * 60 / 70, abs=1e-3)
    assert set(result.breakdown) == {"role_fit", "skills_fit", "education_fit", "location_fit"}
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


def test_any_answer_builds_no_rule():
    assert build_rule("industries", ["Any"]) is None
    assert build_rule("candidate_locations", ["Any"]) is None
    sales = default_scoring_model({"industries": ["Any"]})
    assert not _crit(sales, "industry_fit").applicable
    hr = _hr_model({"candidate_locations": ["Any"]})
    assert not _crit(hr, "location_fit").applicable


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
