from icp_chatbot.domain.state import resolve_slot_values
from icp_chatbot.engine.profile_form import validate_profile
from icp_chatbot.icp.scoring import build_rule, default_scoring_model, score_lead
from icp_chatbot.linkedin.catalog import default_catalog


def test_a_region_lists_its_countries_and_a_country_is_not_a_region():
    catalog = default_catalog()
    europe = {o.label for o in catalog.region_countries("Europe")}
    assert {"Germany", "France", "United Kingdom"} <= europe and "India" not in europe
    assert catalog.region_countries("Germany") is None
    assert catalog.region_countries("Narnia") is None


def test_every_country_belongs_to_one_region():
    catalog = default_catalog()
    regions = {o.label for o in catalog.options("country") if o.level == 1}
    countries = [o for o in catalog.options("country") if o.level == 2]
    assert len(countries) == 249 and all(o.parent in regions for o in countries)


def test_a_region_keeps_its_name_and_refs_all_its_countries():
    labels, refs, unmapped = resolve_slot_values("lead_geographies", ["Europe", "Mumbai"])
    assert labels == ["Europe", "Mumbai"] and unmapped == []
    assert len(refs) == len(default_catalog().region_countries("Europe"))
    assert {r.id for r in refs} >= {"DE", "FR"}


def test_a_region_alone_is_a_valid_geography_answer():
    assert "lead_geographies" not in validate_profile("sales", {"lead_geographies": ["Europe"]})
    assert "lead_geographies" in validate_profile("sales", {"lead_geographies": ["Narnia"]})


def test_a_region_scores_a_lead_in_any_of_its_countries():
    rule = build_rule("lead_geographies", ["Europe"])
    assert "Germany" in rule.primary and "India" not in rule.primary
    model = default_scoring_model({"lead_geographies": ["Europe"]})
    assert score_lead(model, {"lead_geographies": ["Germany"]}).score == 100.0
    assert score_lead(model, {"lead_geographies": ["India"]}).score == 0.0
