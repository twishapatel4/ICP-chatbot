from datetime import date
from pathlib import Path

import pytest
from tests.test_api import world  # noqa: F401  (the API fixture)
from tests.test_document import _build, _filters_state

from icp_chatbot.domain.state import (
    SessionState,
    SlotSource,
    SlotValue,
    resolve_slot_values,
)
from icp_chatbot.engine.profile_form import apply_profile, validate_profile
from icp_chatbot.icp.document import (
    ICPDocument,
    build_linkedin_filters,
    with_slot_filter,
)
from icp_chatbot.icp.review import FieldEdit, apply_edit
from icp_chatbot.icp.scoring import (
    COUNTRY_FIELDS,
    MatchRule,
    _disqualifier_hit,
    build_rule,
    country_match,
    default_scoring_model,
    list_match,
    loose_equal,
    make_disqualifier,
    score_lead,
)
from icp_chatbot.linkedin.catalog import default_catalog
from icp_chatbot.linkedin.profile_map import _country


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


# -- exact country matching (no substring matches between country names) ------------------------


def _geo_score(rule_values, lead_values, slot="lead_geographies"):
    model = default_scoring_model({slot: rule_values})
    return score_lead(model, {slot: lead_values}).score


@pytest.mark.parametrize(
    ("region", "lead", "expected"),
    [
        ("Middle East", "Romania", 0.0),
        ("Middle East", "Oman", 100.0),
        ("Africa", "Papua New Guinea", 0.0),
        ("Africa", "Guinea", 100.0),
        ("Africa", "Somalia", 100.0),
        ("Africa", "Mali", 100.0),
        ("Latin America & Caribbean", "Dominica", 100.0),
        ("Asia-Pacific", "American Samoa", 100.0),
        ("Europe", "Germany", 100.0),
        ("Europe", "India", 0.0),
    ],
)
def test_a_region_never_matches_a_country_by_substring(region, lead, expected):
    assert _geo_score([region], [lead]) == expected


@pytest.mark.parametrize(
    ("rule", "lead"),
    [
        ("Niger", "Nigeria"), ("Nigeria", "Niger"), ("Mali", "Somalia"),
        ("Samoa", "American Samoa"), ("Dominica", "Dominican Republic"),
        ("Oman", "Romania"), ("Guinea", "Papua New Guinea"),
    ],
)  # fmt: skip
def test_a_country_never_matches_another_whose_name_contains_it(rule, lead):
    assert _geo_score([rule], [lead]) == 0.0
    assert _geo_score([rule], [rule]) == 100.0


def test_us_spellings_codes_and_case_all_match_each_other():
    spellings = ["US", "United States", "united states", "  UNITED   states "]
    for a in spellings:
        for b in spellings:
            assert country_match(a, b), (a, b)
    assert not country_match("US", "United Kingdom")


def test_a_city_equals_itself_only():
    assert country_match("Pune", "pune ") and not country_match("Pune", "Pun")
    assert not country_match("Pune", "Punekar") and not country_match("Pune", "India")
    assert _geo_score(["Pune"], ["Pune"]) == 100.0 and _geo_score(["Pune"], ["Punjab"]) == 0.0


def test_an_iso_code_matches_the_country_name_and_its_region():
    assert _geo_score(["Germany"], ["DE"]) == 100.0
    assert _geo_score(["Europe"], ["DE"]) == 100.0
    assert _geo_score(["Europe"], ["IN"]) == 0.0


def test_a_region_and_an_explicit_country_in_one_rule_add_no_duplicates():
    rule = build_rule("lead_geographies", ["Europe", "Germany", "India"])
    assert len(rule.primary) == len(set(rule.primary))
    assert rule.primary.count("Germany") == 1 and "India" in rule.primary
    assert _geo_score(["Europe", "India"], ["India"]) == 100.0


def test_country_matching_applies_to_every_geography_field_and_disqualifiers():
    assert COUNTRY_FIELDS == {
        "lead_geographies", "company_hq_geographies", "candidate_locations", "current_company_hq",
    }  # fmt: skip
    rule = build_rule("lead_geographies", ["Middle East"])
    for slot in sorted(COUNTRY_FIELDS):  # HR-only slots are checked on the matcher directly
        assert list_match(["Romania"], rule, slot) == 0.0
        assert list_match(["Oman"], rule, slot) == 1.0
    assert _geo_score(["Middle East"], ["Romania"], "company_hq_geographies") == 0.0
    assert list_match(["Romania"], MatchRule(kind="list", primary=["Oman"]), "industries") == 1.0
    d = make_disqualifier("lead_geographies", "in", ["Middle East"])
    assert not _disqualifier_hit(d, {"lead_geographies": ["Romania"]})
    assert _disqualifier_hit(d, {"lead_geographies": ["Oman"]})


def test_other_fields_keep_loose_matching():
    assert loose_equal("Software", "Software Development")


# -- the rest of the region path ----------------------------------------------------------------


def _europe_ids():
    return {o.id for o in default_catalog().region_countries("Europe")}


def test_apply_profile_seeds_a_region_from_the_form():
    state = SessionState(track="sales", step="profile")
    apply_profile(
        state,
        {
            "industries": ["Software Development"], "lead_geographies": ["Europe"],
            "company_hq_geographies": ["India"], "buyer_roles": ["Sales director"],
        },
    )  # fmt: skip
    geo = state.slots["lead_geographies"]
    assert geo.values == ["Europe"] and geo.evidence == "step 2 form"
    assert {r.id for r in geo.refs} == _europe_ids()
    assert all(r.level == 2 and r.parent == "Europe" for r in geo.refs)


def _geo_state(**slots):
    state = SessionState(track="sales")
    for key, values in slots.items():
        labels, refs, unmapped = resolve_slot_values(key, values)
        state.slots[key] = SlotValue(
            values=labels, refs=refs, unmapped=unmapped, confidence=0.9, source=SlotSource.STATED
        )
    return state


def test_linkedin_filters_for_a_region_list_its_countries_once():
    (entry,) = build_linkedin_filters(_geo_state(lead_geographies=["Europe", "Germany"]))
    assert entry.labels == ["Europe", "Germany"]
    assert set(entry.ids) == _europe_ids() and len(entry.ids) == len(set(entry.ids))
    (entry,) = build_linkedin_filters(_geo_state(lead_geographies=["Europe", "India"]))
    assert set(entry.ids) == _europe_ids() | {"IN"} and len(entry.ids) == len(_europe_ids()) + 1


def test_with_slot_filter_for_a_region():
    (entry,) = with_slot_filter([], "lead_geographies", ["Europe", "Mumbai"], "sales")
    assert entry.labels == ["Europe", "Mumbai"] and set(entry.ids) == _europe_ids()
    assert entry.unmapped == []


async def test_a_field_edit_with_a_region_and_a_document_round_trip():
    doc = await _build(_filters_state(lead_geographies=["India"]))
    edited, _ = apply_edit(
        doc, FieldEdit(slot="lead_geographies", values=["Europe", "Pune"]), date(2026, 10, 20)
    )
    (f,) = [f for f in edited.linkedin_filters if f.slot == "lead_geographies"]
    assert f.labels == ["Europe", "Pune"] and set(f.ids) == _europe_ids()
    criterion = next(c for c in edited.scoring_model.criteria if c.key == "geographic_fit")
    assert "Germany" in criterion.rules["lead_geographies"].primary
    dumped = edited.model_dump(mode="json")
    assert ICPDocument.model_validate(dumped).model_dump(mode="json") == dumped
    assert edited.sections["target_firmographics"]["lead_geographies"].values == ["Europe", "Pune"]


def test_profile_form_geo_rule_with_regions():
    answers = {
        "industries": ["Software Development"], "company_hq_geographies": ["India"],
        "buyer_roles": ["Sales director"],
    }  # fmt: skip

    def ok(geo):
        return "lead_geographies" not in validate_profile(
            "sales", {**answers, "lead_geographies": geo}
        )

    assert ok(["Europe"]) and ok(["Europe", "Pune"]) and ok(["Middle East", "India"])
    assert not ok(["Pune"]) and not ok(["Pune", "Mumbai"])
    assert ok(["Europe"] + [f"City {i}" for i in range(9)])  # 10 values
    assert ok(["Europe"] + [f"City {i}" for i in range(10)])  # no limit on picks


def test_profile_map_never_returns_a_region():
    assert default_catalog().resolve("country", "Europe").level == 1
    assert _country("Europe") == "Europe"  # free text stays raw, never a region id or label
    assert _country("region-europe") == "region-europe"
    assert _country("de") == "Germany" and _country("DE") == "Germany"
    assert _country("Germany") == "Germany"


def test_sync_script_writes_no_country_rows():
    path = Path(__file__).parent.parent / "scripts" / "sync_linkedin_catalogs.py"
    assert "countries.json" not in path.read_text(encoding="utf-8")


async def test_options_country_lists_regions_then_countries(world):  # noqa: F811
    w = await world()
    items = (await w.call("GET", "/options/country")).json()
    regions = [i for i in items if i["level"] == 1]
    countries = [i for i in items if i["level"] == 2]
    assert len(regions) == 7 and len(countries) == 249
    assert all(i["parent"] is None and i["id"].startswith("region-") for i in regions)
    labels = {r["label"] for r in regions}
    assert all(c["parent"] in labels and len(c["id"]) == 2 for c in countries)
    assert [i["level"] for i in items[:7]] == [1] * 7  # regions first
    assert [i["id"] for i in items] == [o.id for o in default_catalog().options("country")]


async def test_other_territories_is_not_a_quick_pick(world):  # noqa: F811
    w = await world()
    items = (await w.call("GET", "/options/country")).json()
    flags = {i["label"]: i["selectable"] for i in items if i["level"] == 1}
    assert flags.pop("Other territories") is False and all(flags.values())
    assert all(i["selectable"] for i in items if i["level"] == 2)
    assert default_catalog().resolve("country", "Other territories") is not None  # data kept
    assert "AQ" in {o.id for o in default_catalog().region_countries("Other territories")}
    found = (await w.call("GET", "/options/country?q=other")).json()
    assert found and found[0]["selectable"] is False


def test_a_location_line_matches_by_whole_comma_part_only():
    assert country_match("Muscat, Oman", "Middle East") and country_match("Berlin, Germany", "DE")
    assert not country_match("Bucharest, Romania", "Oman")
    assert not country_match("Bucharest, Romania", "Middle East")


def test_a_location_line_matches_only_whole_or_by_its_last_part():
    assert country_match("Berlin, Germany", "Germany")
    assert country_match("Berlin, Germany", "Europe")
    assert not country_match("Georgia, USA", "Georgia")
    assert country_match("Georgia, USA", "United States")
    assert country_match("Pune, Maharashtra, India", "India")
    assert not country_match("Pune, Maharashtra, India", "Maharashtra")
    assert country_match("Georgia", "Georgia")


BAD_VALUES = [None, 5, 2.5, [], ["Germany"], [["Germany"]], {}, "", "   ", ",", "x" * 100_000]


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_country_match_never_raises_on_bad_data(bad):
    from icp_chatbot.icp.scoring import MatchRule, list_match

    assert country_match(bad, "Germany") is False
    assert country_match("Germany", bad) is False
    rule = MatchRule(kind="list", primary=["Germany"])
    assert list_match([bad, "Germany"], rule, "lead_geographies") == 1.0
    assert list_match([bad], rule, "lead_geographies") == 0.0
    bad_rule = MatchRule.model_construct(kind="list", primary=[bad], secondary=[])
    assert list_match(["Germany"], bad_rule, "lead_geographies") == 0.0


@pytest.mark.parametrize(
    ("alias", "iso"),
    [
        ("USA", "US"), ("U.S.", "US"), ("U.S.A.", "US"), ("US", "US"), ("usa", "US"),
        ("United States of America", "US"), ("UK", "GB"), ("U.K.", "GB"),
        ("Great Britain", "GB"), ("Britain", "GB"), ("England", "GB"),
        ("Czech Republic", "CZ"), ("Turkey", "TR"), ("Turkiye", "TR"), ("Türkiye", "TR"),
        ("Ivory Coast", "CI"), ("Cote d'Ivoire", "CI"), ("Russia", "RU"),
        ("South Korea", "KR"), ("Korea, Republic of", "KR"), ("Viet Nam", "VN"),
        ("Vietnam", "VN"), ("UAE", "AE"),
    ],
)  # fmt: skip
def test_common_country_aliases_resolve_to_the_right_iso_id(alias, iso):
    from icp_chatbot.linkedin.catalog import default_catalog

    assert default_catalog().resolve("country", alias).id == iso


def test_ambiguous_or_near_names_do_not_resolve_or_collide():
    from icp_chatbot.linkedin.catalog import default_catalog

    assert default_catalog().resolve("country", "Korea") is None
    assert not country_match("Guinea", "Guinea-Bissau")
    assert not country_match("Guinea-Bissau", "Guinea")
    assert country_match("UK", "United Kingdom") and country_match("Great Britain", "GB")
def test_blank_never_matches_blank():
    assert country_match("", "") is False and country_match(" ", "  ") is False
