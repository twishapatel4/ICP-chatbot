import json
from pathlib import Path

from icp_chatbot.linkedin.catalog import (
    FACETS,
    SLOT_FACETS,
    OptionCatalog,
    default_catalog,
)

VOCAB = json.loads((Path(__file__).parent / "data" / "linkedin_db_vocab.json").read_text())
# "OO" appears in the pilot DB but is not an ISO 3166-1 code (bad source data).
KNOWN_BAD_COUNTRY_CODES = {"OO"}


def test_industry_snapshot_has_434_unique_ids():
    opts = default_catalog().options("industry")
    assert len(opts) == 434
    assert len({o.id for o in opts}) == 434


def test_every_db_industry_resolves():
    cat = default_catalog()
    for value in VOCAB["industry"]:
        assert cat.resolve("industry", value) is not None, value


def test_industry_label_variants_resolve_to_same_option():
    cat = default_catalog()
    for a, b in (
        ("Climate Technology Product Manufacturing", "Climate Technology Products Manufacturing"),
        ("Media & Telecommunications", "Media and Telecommunications"),
    ):
        assert cat.resolve("industry", a) is not None
        assert cat.resolve("industry", a) == cat.resolve("industry", b)


def test_every_db_size_range_resolves_to_a_company_size():
    cat = default_catalog()
    for value in VOCAB["employeeRange"]:
        assert cat.resolve("company_size", value) is not None, value
    assert "2-10" in cat.resolve("company_size", "1-10").match


def test_company_sizes_are_the_nine_linkedin_codes():
    opts = default_catalog().options("company_size")
    assert [o.id for o in opts] == list("ABCDEFGHI")
    assert [o.label for o in opts] == [
        "Self-employed", "1-10", "11-50", "51-200", "201-500",
        "501-1000", "1001-5000", "5001-10,000", "10,001+",
    ]  # fmt: skip


def test_seniority_has_10_and_function_35():
    cat = default_catalog()
    assert len(cat.options("seniority")) == 10
    assert len(cat.options("function")) == 35
    assert cat.resolve("function", "sale").label == "Sales"
    assert cat.resolve("seniority", "VP").label == "Vice President (VP)"


def test_every_db_country_code_resolves():
    cat = default_catalog()
    for code in VOCAB["hqCountry"]:
        if code in KNOWN_BAD_COUNTRY_CODES:
            assert cat.resolve("country", code) is None
        else:
            assert cat.resolve("country", code) is not None, code
    assert cat.resolve("country", "in").label == "India"


def test_search_is_case_insensitive_and_limited():
    cat = default_catalog()
    hits = cat.search("industry", "SOFTWARE", limit=3)
    assert 0 < len(hits) <= 3
    assert all("software" in o.label.casefold() for o in hits)


def test_missing_degree_catalog_reports_has_false(tmp_path):
    cat = default_catalog()
    assert not cat.has("degree")
    assert not cat.has("field_of_study")
    assert cat.options("degree") == []
    assert cat.has("industry")
    assert set(FACETS) >= set(SLOT_FACETS.values())
    (tmp_path / "degrees.json").write_text(json.dumps([{"id": "1", "label": "BSc"}]))
    assert OptionCatalog(tmp_path).has("degree")
