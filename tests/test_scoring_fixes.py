"""Company-name folding, mixed size rules and region disqualifiers (final review, minor findings)."""

import pytest

from icp_chatbot.icp.scoring import (
    Criterion,
    MatchRule,
    ScoringModel,
    build_rule,
    company_match,
    make_disqualifier,
    score_lead,
)


@pytest.mark.parametrize(
    ("candidate", "avoided", "hit"),
    [
        ("Societe Generale", "Société Générale", True),
        ("Société Générale SA", "Societe Generale", True),
        ("AT&T", "AT and T", True),
        ("AT and T Inc", "AT&T", True),
        ("Johnson & Johnson", "Johnson and Johnson", True),
        ("Meta", "Metal Works", False),
        ("Metal Works", "Meta", False),
        ("Meta Platforms", "Meta", False),
    ],
)
def test_company_match_folds_accents_and_ampersands(candidate, avoided, hit):
    assert company_match(candidate, avoided) is hit


def _size_model(rule):
    criterion = Criterion(
        key="size", label="Size", weight=100, reads=["company_size"], rules={"company_size": rule}
    )
    return ScoringModel(criteria=[criterion])


def test_company_size_rule_keeps_non_numeric_values_alongside_ranges():
    rule = build_rule("company_size", ["Self-employed", "11-50"])
    assert rule.kind == "range" and rule.min == 11 and rule.max == 50
    assert rule.primary == ["Self-employed"]
    model = _size_model(rule)
    assert score_lead(model, {"company_size": ["Self-employed"]}).breakdown == {"size": 1.0}
    assert score_lead(model, {"company_size": ["20"]}).breakdown == {"size": 1.0}
    assert score_lead(model, {"company_size": ["5000"]}).breakdown == {"size": 0.0}


def test_purely_numeric_size_rule_is_unchanged():
    rule = build_rule("company_size", ["11-50", "51-200"])
    assert rule == MatchRule(kind="range", min=11, max=200, tolerance=0.2)
    assert rule.primary == []


def test_region_disqualifier_hits_countries_in_it_and_not_others():
    rule = make_disqualifier("lead_geographies", "in", ["Europe"])
    assert rule is not None
    model = ScoringModel(criteria=[], disqualifiers=[rule])
    assert score_lead(model, {"lead_geographies": ["Germany"]}).disqualified_by is not None
    assert score_lead(model, {"lead_geographies": ["Berlin, Germany"]}).disqualified_by is not None
    assert score_lead(model, {"lead_geographies": ["Japan"]}).disqualified_by is None
