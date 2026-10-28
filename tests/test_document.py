from icp_chatbot.domain.state import SlotSource
from icp_chatbot.icp.document import (
    FieldEntry,
    ICPDocument,
    compute_completeness,
    group_by_section,
    render_summary,
)
from icp_chatbot.icp.scoring import default_scoring_model


def entry(values, confidence=0.9, source=SlotSource.STATED):
    return FieldEntry(values=values, confidence=confidence, source=source)


def test_entries_are_grouped_by_section_in_registry_order_and_empty_ones_dropped():
    entries = {
        "buyer_roles": entry(["CFO"]), "industries": entry(["Fintech"]),
        "product": entry(["Payroll"]), "geographies": entry([]),
    }  # fmt: skip
    grouped = group_by_section(entries)
    assert list(grouped) == ["seller_context", "target_firmographics", "buying_structure"]
    assert list(grouped["target_firmographics"]) == ["industries"]  # empty geographies dropped


def test_completeness_counts_known_slots_and_skips_inapplicable_ones():
    nothing = compute_completeness({})
    assert nothing.tier_a == 0.0 and nothing.tier_b == 0.0
    tier_a = [
        "industries", "company_size", "geographies", "buyer_roles", "buyer_departments",
        "disqualifiers", "product",
    ]  # fmt: skip
    entries = {k: entry(["x"]) for k in tier_a}
    entries["business_model"] = entry(["B2B"])
    assert compute_completeness(entries).tier_a == 1.0
    entries["industries"] = entry(["x"], confidence=0.5)  # below the 0.7 bar
    assert compute_completeness(entries).tier_a == round(7 / 8, 4)
    b2c = {k: entry(["x"]) for k in tier_a if not k.startswith("buyer_")}
    b2c["business_model"] = entry(["B2C"])
    b2c["customer_profile"] = entry(["x"])
    assert compute_completeness(b2c).tier_a == 1.0  # buyer slots do not apply to B2C


def test_summary_is_built_only_from_the_fields_and_marks_inferences():
    entries = {
        "industries": entry(["Fintech"]),
        "geographies": entry(["Europe"], source=SlotSource.INFERRED, confidence=0.5),
        "buyer_departments": entry(["Finance"]),
        "buyer_roles": entry(["CFO", "COO"]),
    }
    summary = render_summary(entries)
    assert "Target industries: Fintech" in summary
    assert "geographies: Europe (inferred)" in summary
    assert "Buyers: CFO, COO; Finance." in summary
    assert "pain points" not in summary.lower()  # nothing is invented for missing fields
    assert render_summary({}) == ""


def test_document_helpers_and_json_round_trip():
    entries = {"industries": entry(["Fintech"]), "buyer_roles": entry(["CFO"])}
    document = ICPDocument(
        sections=group_by_section(entries),
        scoring_model=default_scoring_model({"industries": ["Fintech"]}),
        completeness=compute_completeness(entries),
    )
    assert document.slot_values() == {"industries": ["Fintech"], "buyer_roles": ["CFO"]}
    assert set(document.entries()) == {"industries", "buyer_roles"}
    assert ICPDocument.model_validate_json(document.model_dump_json()) == document


def test_completeness_ignores_fields_whose_values_are_not_real_answers():
    from icp_chatbot.domain.slots import SLOT_LIST, Tier

    key = next(
        s.key for s in SLOT_LIST if s.tier == Tier.A and s.blocks and s.key != "disqualifiers"
    )
    empty = compute_completeness(
        {key: FieldEntry(values=["none"], confidence=0.9, source=SlotSource.STATED)}
    )
    real = compute_completeness(
        {key: FieldEntry(values=["Fintech"], confidence=0.9, source=SlotSource.STATED)}
    )
    assert real.tier_a > empty.tier_a
