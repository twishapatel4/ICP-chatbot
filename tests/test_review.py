from datetime import date

import pytest

from icp_chatbot.domain.state import SlotSource
from icp_chatbot.icp.diff import changed_fields, edit_rate
from icp_chatbot.icp.document import (
    FieldEntry,
    ICPDocument,
    compute_completeness,
    group_by_section,
    render_summary,
    sync_field_requirements,
)
from icp_chatbot.icp.review import (
    DisqualifierEdit,
    FieldEdit,
    RequirementEdit,
    WeightEdit,
    apply_edit,
    approve,
)
from icp_chatbot.icp.scoring import Disqualifier, default_scoring_model, score_lead

TODAY = date(2026, 10, 20)


def make_doc():
    entries = {
        "industries": FieldEntry(values=["Fintech"], confidence=0.9, source=SlotSource.STATED),
        "company_size": FieldEntry(values=["50-500"], confidence=0.9, source=SlotSource.STATED),
        "lead_geographies": FieldEntry(values=["Europe"], confidence=0.5, source=SlotSource.INFERRED),
        "buyer_roles": FieldEntry(
            values=["CFO"], confidence=0.9, source=SlotSource.STATED
        ),
    }
    values = {k: e.values for k, e in entries.items()}
    doc = ICPDocument(
        sections=group_by_section(entries),
        scoring_model=default_scoring_model(values),
        summary=render_summary(entries),
        completeness=compute_completeness(entries),
    )
    return sync_field_requirements(doc)


def test_fields_read_by_criteria_start_as_preferred_and_others_as_none():
    entries = make_doc().entries()
    assert entries["industries"].requirement == "preferred"
    doc = make_doc()
    doc = doc.model_copy(update={"sections": {**doc.sections, "seller_context": {
        "product": FieldEntry(values=["Payroll"], confidence=0.9, source=SlotSource.STATED)}}})  # fmt: skip
    assert sync_field_requirements(doc).entries()["product"].requirement == "none"


def test_a_field_edit_is_stated_verified_and_refreshes_the_model_and_summary():
    doc, specs = apply_edit(
        make_doc(), FieldEdit(slot="industries", values=["Insurance", "Banking"]), TODAY
    )
    entry = doc.entries()["industries"]
    assert entry.values == ["Insurance", "Banking"] and entry.source == SlotSource.STATED
    assert entry.confidence == 1.0 and entry.last_verified == "2026-10-20"
    assert "Insurance, Banking" in doc.summary
    industry = next(c for c in doc.scoring_model.criteria if c.key == "industry_fit")
    assert industry.rules["industries"].primary == ["Insurance", "Banking"]
    assert [(s.type, s.field, s.old, s.new) for s in specs] == [
        ("field_edit", "industries", ["Fintech"], ["Insurance", "Banking"])
    ]


def test_adding_a_field_makes_its_criterion_applicable_and_removing_it_does_not():
    doc, _ = apply_edit(
        make_doc(), FieldEdit(slot="buying_triggers", values=["new funding"]), TODAY
    )
    assert next(c for c in doc.scoring_model.criteria if c.key == "buying_signals").applicable
    doc, specs = apply_edit(doc, FieldEdit(slot="buying_triggers", values=[]), TODAY)
    assert "buying_triggers" not in doc.entries()
    assert not next(c for c in doc.scoring_model.criteria if c.key == "buying_signals").applicable
    assert specs[0].old == ["new funding"] and specs[0].new == []


def test_editing_an_unknown_field_is_rejected():
    with pytest.raises(ValueError, match="unknown field"):
        apply_edit(make_doc(), FieldEdit(slot="favourite_colour", values=["blue"]), TODAY)


def test_weight_edits_must_total_100_and_record_only_changed_criteria():
    doc, specs = apply_edit(
        make_doc(), WeightEdit(weights={"industry_fit": 45, "geographic_fit": 15}), TODAY
    )
    assert {c.key: c.weight for c in doc.scoring_model.criteria}["industry_fit"] == 45
    assert [(s.type, s.field, s.old, s.new) for s in specs] == [
        ("weight_edit", "industry_fit", 35.0, 45.0), ("weight_edit", "geographic_fit", 25.0, 15.0),
    ]  # fmt: skip
    with pytest.raises(ValueError, match="sum to 100"):
        apply_edit(make_doc(), WeightEdit(weights={"industry_fit": 45}), TODAY)


def test_a_requirement_toggle_updates_the_criterion_and_the_fields_it_reads():
    doc, specs = apply_edit(
        make_doc(), RequirementEdit(criterion="geographic_fit", requirement="required"), TODAY
    )
    assert doc.entries()["lead_geographies"].requirement == "required"
    assert doc.entries()["industries"].requirement == "preferred"
    assert (specs[0].type, specs[0].old, specs[0].new) == (
        "requirement_toggle",
        "preferred",
        "required",
    )
    lead = {"industries": ["Fintech"], "lead_geographies": ["Asia"]}
    assert not score_lead(doc.scoring_model, lead).qualified


def test_disqualifier_edits_replace_the_list_and_reject_unusable_rules():
    edit = DisqualifierEdit(
        disqualifiers=[Disqualifier(field="industries", operator="in", values=["Government"])]
    )
    doc, specs = apply_edit(make_doc(), edit, TODAY)
    assert [d.values for d in doc.scoring_model.disqualifiers] == [["Government"]]
    assert specs[0].type == "disqualifier_edit" and specs[0].old == []
    bad = DisqualifierEdit(
        disqualifiers=[Disqualifier(field="favourite_colour", operator="in", values=["x"])]
    )
    with pytest.raises(ValueError, match="unusable"):
        apply_edit(doc, bad, TODAY)


def test_edits_never_modify_the_original_document():
    original = make_doc()
    snapshot = original.model_dump()
    apply_edit(original, FieldEdit(slot="industries", values=["Retail"]), TODAY)
    assert original.model_dump() == snapshot


def test_approval_verifies_every_field_and_records_an_event():
    doc, specs = approve(make_doc(), TODAY)
    assert all(e.last_verified == "2026-10-20" for e in doc.entries().values())
    assert [s.type for s in specs] == ["approve"]


def test_approval_is_refused_when_the_scoring_model_is_invalid():
    doc = make_doc()
    broken = doc.scoring_model.model_copy(update={"criteria": doc.scoring_model.criteria[:-1]})
    with pytest.raises(ValueError, match="weights sum"):
        approve(doc.model_copy(update={"scoring_model": broken}), TODAY)


def test_edit_rate_compares_the_draft_with_the_final_version():
    draft = make_doc()
    assert edit_rate(draft, draft) == 0.0
    final, _ = apply_edit(draft, FieldEdit(slot="industries", values=["fintech"]), TODAY)
    assert changed_fields(draft, final) == {}  # same value, different case: not a change
    final, _ = apply_edit(draft, FieldEdit(slot="industries", values=["Retail"]), TODAY)
    final, _ = apply_edit(final, FieldEdit(slot="buying_triggers", values=["funding"]), TODAY)
    changes = changed_fields(draft, final)
    assert set(changes) == {"industries", "buying_triggers"}
    assert edit_rate(draft, final) == 2 / 4
