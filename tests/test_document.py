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
        "product": entry(["Payroll"]), "lead_geographies": entry([]),
    }  # fmt: skip
    grouped = group_by_section(entries)
    assert list(grouped) == ["seller_context", "target_firmographics", "buying_structure"]
    assert list(grouped["target_firmographics"]) == ["industries"]  # empty lead_geographies dropped


def test_completeness_counts_known_slots_and_skips_inapplicable_ones():
    nothing = compute_completeness({})
    assert nothing.tier_a == 0.0 and nothing.tier_b == 0.0
    tier_a = [
        "industries", "company_size", "lead_geographies", "company_hq_geographies", "buyer_roles",
        "buyer_departments", "buyer_seniority", "disqualifiers", "product",
    ]  # fmt: skip
    entries = {k: entry(["x"]) for k in tier_a}
    entries["business_model"] = entry(["B2B"])
    assert compute_completeness(entries).tier_a == 1.0
    entries["industries"] = entry(["x"], confidence=0.5)  # below the 0.7 bar
    assert compute_completeness(entries).tier_a == round(9 / 10, 4)
    b2c = {k: entry(["x"]) for k in tier_a if not k.startswith("buyer_")}
    b2c["business_model"] = entry(["B2C"])
    b2c["customer_profile"] = entry(["x"])
    assert compute_completeness(b2c).tier_a == 1.0  # buyer slots do not apply to B2C


def test_summary_is_built_only_from_the_fields_and_marks_inferences():
    entries = {
        "industries": entry(["Fintech"]),
        "lead_geographies": entry(["Europe"], source=SlotSource.INFERRED, confidence=0.5),
        "company_hq_geographies": entry(["United States"]),
        "buyer_departments": entry(["Finance"]),
        "buyer_roles": entry(["CFO", "COO"]),
        "buyer_seniority": entry(["Director"]),
    }
    summary = render_summary(entries)
    assert "Target industries: Fintech" in summary
    assert "based in: Europe (inferred); headquartered in: United States." in summary
    assert "Buyers: CFO, COO; Director; Finance." in summary
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


# -- documents stored before geography was split --------------------------------------------------


def stored(values, **kw):
    return {"values": values, "confidence": 0.9, "source": "stated", **kw}


def legacy_document() -> dict:
    """An ICP version as saved before `geographies` became `lead_geographies`."""
    return {
        "schema_version": "1",
        "sections": {
            "target_firmographics": {
                "industries": stored(["Fintech"], requirement="preferred"),
                "geographies": stored(["Europe"], requirement="required"),
            }
        },
        "scoring_model": {
            "criteria": [
                {"key": "industry_fit", "label": "Industry fit", "weight": 35,
                 "reads": ["industries", "customer_segments"],
                 "rules": {"industries": {"kind": "list", "primary": ["Fintech"]}}},
                {"key": "company_size", "label": "Company size", "weight": 25,
                 "reads": ["company_size"], "applicable": False},
                {"key": "buying_signals", "label": "Buying signals", "weight": 15,
                 "reads": ["buying_triggers"], "applicable": False},
                {"key": "geographic_fit", "label": "Geographic fit", "weight": 25,
                 "reads": ["geographies"], "requirement": "required",
                 "rules": {"geographies": {"kind": "list", "primary": ["Europe"]}}},
            ],
            "disqualifiers": [{"field": "geographies", "operator": "in", "values": ["Asia"]}],
        },
        "completeness": {"tier_a": 0.25, "tier_b": 0.0},
        "not_needed": ["geographies"],
    }  # fmt: skip


def test_a_stored_document_with_legacy_geographies_loads_with_the_new_key():
    doc = ICPDocument.model_validate(legacy_document())
    assert list(doc.sections["target_firmographics"]) == ["industries", "lead_geographies"]
    moved = doc.entries()["lead_geographies"]
    assert moved.values == ["Europe"] and moved.requirement == "required"
    geo = next(c for c in doc.scoring_model.criteria if c.key == "geographic_fit")
    assert geo.reads == ["lead_geographies"] and list(geo.rules) == ["lead_geographies"]
    assert geo.rules["lead_geographies"].primary == ["Europe"] and geo.requirement == "required"
    assert [d.field for d in doc.scoring_model.disqualifiers] == ["lead_geographies"]
    assert doc.not_needed == ["lead_geographies"]
    assert "geographies" not in doc.model_dump_json().replace("lead_geographies", "")
    assert ICPDocument.model_validate_json(doc.model_dump_json()) == doc  # idempotent


def test_a_legacy_document_validates_and_can_still_be_scored_edited_and_approved():
    from datetime import date

    from icp_chatbot.icp.review import FieldEdit, apply_edit, approve
    from icp_chatbot.icp.scoring import score_lead, validate_model

    doc = ICPDocument.model_validate(legacy_document())
    assert validate_model(doc.scoring_model) == []
    assert score_lead(doc.scoring_model, {"lead_geographies": ["Asia"]}).disqualified_by
    edited, _ = apply_edit(doc, FieldEdit(slot="lead_geographies", values=["India"]), date.today())
    assert edited.entries()["lead_geographies"].values == ["India"]
    approved, _ = approve(edited, date.today())
    assert approved.entries()["lead_geographies"].values == ["India"]


def test_the_new_key_wins_when_a_stored_document_has_both():
    legacy = legacy_document()
    legacy["sections"]["target_firmographics"]["lead_geographies"] = stored(["India"])
    geo = legacy["scoring_model"]["criteria"][3]
    geo["reads"] = ["geographies", "lead_geographies"]
    geo["rules"]["lead_geographies"] = {"kind": "list", "primary": ["India"]}
    doc = ICPDocument.model_validate(legacy)
    assert doc.entries()["lead_geographies"].values == ["India"]
    assert "geographies" not in doc.entries()
    criterion = next(c for c in doc.scoring_model.criteria if c.key == "geographic_fit")
    assert criterion.reads == ["lead_geographies"]
    assert criterion.rules["lead_geographies"].primary == ["India"]


def test_a_document_with_the_new_keys_is_unchanged_by_the_legacy_hook():
    entries = {
        "industries": entry(["Fintech"]),
        "lead_geographies": entry(["Europe"]),
        "company_hq_geographies": entry(["United States"]),
    }
    doc = ICPDocument(
        sections=group_by_section(entries),
        scoring_model=default_scoring_model({k: e.values for k, e in entries.items()}),
        completeness=compute_completeness(entries),
        not_needed=["company_size"],
    )
    dumped = doc.model_dump(mode="json")
    assert ICPDocument.model_validate(dumped).model_dump(mode="json") == dumped


# -- the LinkedIn filters block and the document's track ---------------------------------------


def _filters_state(track="sales", **slots):
    from icp_chatbot.domain.state import SessionState, SlotValue

    state = SessionState(track=track)
    for key, values in slots.items():
        state.slots[key] = SlotValue(values=values, confidence=0.9, source=SlotSource.STATED)
    return state


async def _build(state):
    from datetime import date

    from icp_chatbot.config import Settings
    from icp_chatbot.icp.builder import ICPSynthesis, build_icp
    from icp_chatbot.llm.fake import FakeLLM
    from icp_chatbot.versions import default_config

    llm = FakeLLM({"final_icp": [ICPSynthesis()]})
    config = default_config(Settings(_env_file=None))
    return (await build_icp(llm, config, state, today=date(2026, 10, 15))).document


def _resolved(slot_key, values):
    from icp_chatbot.domain.state import SlotValue, resolve_slot_values

    labels, refs, unmapped = resolve_slot_values(slot_key, values)
    return SlotValue(
        values=labels, refs=refs, unmapped=unmapped, confidence=0.9, source=SlotSource.STATED
    )


async def test_linkedin_filters_block_lists_ids_for_industries_and_size():
    state = _filters_state(lead_geographies=["Europe"])
    state.slots["industries"] = _resolved("industries", ["Financial Services"])
    state.slots["company_size"] = _resolved("company_size", ["51-200"])
    doc = await _build(state)
    by_slot = {f.slot: f for f in doc.linkedin_filters}
    industries, size = by_slot["industries"], by_slot["company_size"]
    assert industries.facet == "industry" and size.facet == "company_size"
    assert industries.ids == [r.id for r in state.slots["industries"].refs] and industries.ids
    assert size.ids == [r.id for r in state.slots["company_size"].refs] and size.ids
    assert industries.labels == state.slots["industries"].values
    assert industries.unmapped == [] and size.unmapped == []
    assert "lead_geographies" in by_slot and set(by_slot) <= set(state.slots)


async def test_any_adds_no_filter():
    state = _filters_state(industries=["Any"], company_size=["Any"], lead_geographies=["Any"])
    doc = await _build(state)
    assert doc.linkedin_filters == []


async def test_unmapped_values_are_flagged():
    state = _filters_state()
    state.slots["industries"] = _resolved("industries", ["Financial Services", "Zorblax Farming"])
    doc = await _build(state)
    (entry,) = [f for f in doc.linkedin_filters if f.slot == "industries"]
    assert entry.unmapped == ["Zorblax Farming"]
    assert len(entry.ids) == 1 and "Zorblax Farming" in entry.labels


async def test_missing_refs_are_resolved_at_build_time():
    state = _filters_state(industries=["Financial Services"])  # inferred or confirmed: no refs
    assert state.slots["industries"].refs == []
    doc = await _build(state)
    (entry,) = [f for f in doc.linkedin_filters if f.slot == "industries"]
    assert len(entry.ids) == 1 and entry.labels
    assert state.slots["industries"].refs == []  # the stored state is not changed


async def test_filters_block_dedupes_ids():
    from icp_chatbot.domain.state import SlotValue

    one = _resolved("industries", ["Financial Services"])
    ref = one.refs[0]
    state = _filters_state()
    state.slots["industries"] = SlotValue(
        values=one.values, refs=[ref, ref], confidence=0.9, source=SlotSource.STATED
    )
    doc = await _build(state)
    (entry,) = [f for f in doc.linkedin_filters if f.slot == "industries"]
    assert entry.ids == [ref.id]


async def test_sales_document_unchanged_apart_from_filters_block():
    state = _filters_state(
        product=["Payroll software"], industries=["Fintech"], buyer_roles=["CFO"]
    )
    doc = await _build(state)
    assert doc.track == "sales"
    assert list(doc.sections) == ["seller_context", "target_firmographics", "buying_structure"]
    assert doc.summary == (
        "About the company: What they do: Payroll software.\n"
        "Ideal customer: Target industries: Fintech.\nBuyers: CFO."
    )


def test_legacy_sales_document_loads_with_track_sales_and_empty_filters():
    doc = ICPDocument.model_validate(legacy_document())
    assert doc.track == "sales" and doc.linkedin_filters == []


# -- the filters block survives review edits (ruling R15) ------------------------------------------


async def _filtered_doc():
    state = _filters_state(lead_geographies=["Europe"], buyer_roles=["CFO"])
    state.slots["industries"] = _resolved("industries", ["Financial Services"])
    state.slots["company_size"] = _resolved("company_size", ["51-200"])
    return await _build(state)


def _blocks(doc):
    return [f.model_dump() for f in doc.linkedin_filters]


async def test_weight_requirement_and_disqualifier_edits_leave_filters_identical():
    from datetime import date

    from icp_chatbot.icp.review import (
        DisqualifierEdit,
        RequirementEdit,
        WeightEdit,
        apply_edit,
    )
    from icp_chatbot.icp.scoring import Disqualifier

    doc = await _filtered_doc()
    before = _blocks(doc)
    assert before
    weights = {c.key: c.weight for c in doc.scoring_model.criteria}
    first, second = list(weights)[:2]
    edits = [
        WeightEdit(weights={first: weights[first] + 5, second: weights[second] - 5}),
        RequirementEdit(criterion=first, requirement="required"),
        DisqualifierEdit(
            disqualifiers=[Disqualifier(field="industries", operator="in", values=["Government"])]
        ),
    ]
    for edit in edits:
        edited, _ = apply_edit(doc, edit, date(2026, 10, 20))
        assert _blocks(edited) == before


async def test_field_edit_replaces_only_that_slots_entry():
    from datetime import date

    from icp_chatbot.icp.review import FieldEdit, apply_edit

    doc = await _filtered_doc()
    edited, _ = apply_edit(doc, FieldEdit(slot="industries", values=["Insurance"]), date.today())
    old = {f.slot: f for f in doc.linkedin_filters}
    new = {f.slot: f for f in edited.linkedin_filters}
    assert new["industries"].labels == ["Insurance"] and new["industries"].ids
    assert new["industries"].ids != old["industries"].ids
    assert {k: v for k, v in new.items() if k != "industries"} == {
        k: v for k, v in old.items() if k != "industries"
    }
    assert [f.slot for f in edited.linkedin_filters] == [f.slot for f in doc.linkedin_filters]


async def test_field_edit_to_any_or_empty_removes_the_entry_and_non_facet_edit_changes_nothing():
    from datetime import date

    from icp_chatbot.icp.review import FieldEdit, apply_edit

    doc = await _filtered_doc()
    for values in (["Any"], []):
        edited, _ = apply_edit(doc, FieldEdit(slot="industries", values=values), date.today())
        assert "industries" not in {f.slot for f in edited.linkedin_filters}
        assert len(edited.linkedin_filters) == len(doc.linkedin_filters) - 1
    edited, _ = apply_edit(doc, FieldEdit(slot="buyer_roles", values=["CEO"]), date.today())
    assert _blocks(edited) == _blocks(doc)


async def test_carry_over_keeps_new_ids_for_untouched_slots_and_recomputes_edited_ones():
    from datetime import date

    from icp_chatbot.icp.review import FieldEdit, apply_edit, carry_over_edits

    old = await _filtered_doc()
    edited, _ = apply_edit(old, FieldEdit(slot="industries", values=["Insurance"]), date.today())
    rebuilt_state = _filters_state(lead_geographies=["Europe"], buyer_roles=["CFO"])
    rebuilt_state.slots["industries"] = _resolved("industries", ["Financial Services"])
    rebuilt_state.slots["company_size"] = _resolved("company_size", ["201-500"])
    fresh = await _build(rebuilt_state)
    merged = carry_over_edits(fresh, edited)
    by_slot = {f.slot: f for f in merged.linkedin_filters}
    fresh_by_slot = {f.slot: f for f in fresh.linkedin_filters}
    assert by_slot["company_size"] == fresh_by_slot["company_size"]
    assert (
        by_slot["company_size"].ids != {f.slot: f for f in old.linkedin_filters}["company_size"].ids
    )
    assert by_slot["industries"].labels == ["Insurance"]


async def test_legacy_document_stays_unfiltered_after_a_weight_edit_until_a_facet_edit():
    from datetime import date

    from icp_chatbot.icp.review import FieldEdit, WeightEdit, apply_edit

    legacy = ICPDocument.model_validate(legacy_document())
    weights = {c.key: c.weight for c in legacy.scoring_model.criteria}
    first, second = list(weights)[:2]
    edited, _ = apply_edit(
        legacy, WeightEdit(weights={first: weights[first] + 5, second: weights[second] - 5}),
        date.today(),
    )  # fmt: skip
    assert edited.linkedin_filters == []
    edited, _ = apply_edit(legacy, FieldEdit(slot="industries", values=["Insurance"]), date.today())
    assert [f.slot for f in edited.linkedin_filters] == ["industries"]


async def test_built_and_edited_ids_agree_for_an_unambiguous_label():
    from datetime import date

    from icp_chatbot.icp.review import FieldEdit, apply_edit

    doc = await _filtered_doc()
    edited, _ = apply_edit(
        doc, FieldEdit(slot="industries", values=["Financial Services"]), date.today()
    )
    ids = {f.slot: f.ids for f in doc.linkedin_filters}["industries"]
    assert {f.slot: f.ids for f in edited.linkedin_filters}["industries"] == ids


async def test_a_geography_with_a_city_is_not_flagged_unmapped():
    doc = await _build(_filters_state(lead_geographies=["Pune"]))
    (entry,) = [f for f in doc.linkedin_filters if f.slot == "lead_geographies"]
    assert entry.unmapped == [] and entry.facet == "country"


async def test_non_answers_and_any_mixed_with_values_never_enter_the_filter():
    doc = await _build(
        _filters_state(industries=["Any", "Financial Services"], company_size=["not sure"])
    )
    by_slot = {f.slot: f for f in doc.linkedin_filters}
    assert "company_size" not in by_slot
    assert "Any" not in by_slot["industries"].labels and by_slot["industries"].ids


# -- preserved, not recomputed: hand-made entries that label resolution cannot reproduce -------------


def _handmade(doc):
    from icp_chatbot.icp.document import FilterEntry

    custom = [
        FilterEntry(
            facet="industry", slot="industries", ids=["x-custom"], labels=["Whatever"],
            unmapped=["foo"],
        ),
        FilterEntry(
            facet="company_size", slot="company_size", ids=["y-custom"], labels=["Big"],
            unmapped=[],
        ),
    ]  # fmt: skip
    return doc.model_copy(update={"linkedin_filters": custom})


async def test_non_field_edits_and_edits_of_other_slots_preserve_handmade_entries():
    from datetime import date

    from icp_chatbot.icp.review import (
        DisqualifierEdit,
        FieldEdit,
        RequirementEdit,
        WeightEdit,
        apply_edit,
    )
    from icp_chatbot.icp.scoring import Disqualifier

    doc = _handmade(await _filtered_doc())
    before = _blocks(doc)
    weights = {c.key: c.weight for c in doc.scoring_model.criteria}
    first, second = list(weights)[:2]
    for edit in (
        WeightEdit(weights={first: weights[first] + 5, second: weights[second] - 5}),
        RequirementEdit(criterion=first, requirement="required"),
        DisqualifierEdit(
            disqualifiers=[Disqualifier(field="industries", operator="in", values=["Government"])]
        ),
        FieldEdit(slot="buyer_roles", values=["CEO"]),
        FieldEdit(slot="lead_geographies", values=["France"]),
    ):
        edited, _ = apply_edit(doc, edit, date.today())
        kept = [f for f in _blocks(edited) if f["slot"] in ("industries", "company_size")]
        assert kept == before
    edited, _ = apply_edit(doc, FieldEdit(slot="lead_geographies", values=["France"]), date.today())
    assert [f.slot for f in edited.linkedin_filters if f.slot == "lead_geographies"]


async def test_field_edit_of_the_handmade_slot_replaces_it_by_catalog_resolution():
    from datetime import date

    from icp_chatbot.icp.review import FieldEdit, apply_edit

    doc = _handmade(await _filtered_doc())
    edited, _ = apply_edit(doc, FieldEdit(slot="industries", values=["Insurance"]), date.today())
    by_slot = {f.slot: f for f in edited.linkedin_filters}
    assert by_slot["industries"].ids and "x-custom" not in by_slot["industries"].ids
    assert by_slot["industries"].unmapped == []
    assert by_slot["company_size"] == {f.slot: f for f in doc.linkedin_filters}["company_size"]


async def test_carry_over_keeps_fresh_handmade_ids_and_recomputes_only_the_edited_slot():
    from datetime import date

    from icp_chatbot.icp.review import FieldEdit, apply_edit, carry_over_edits

    edited, _ = apply_edit(
        await _filtered_doc(), FieldEdit(slot="company_size", values=["201-500"]), date.today()
    )
    fresh = _handmade(await _filtered_doc())  # industries custom id; company_size custom too
    merged = carry_over_edits(fresh, edited)
    by_slot = {f.slot: f for f in merged.linkedin_filters}
    assert by_slot["industries"].ids == ["x-custom"] and by_slot["industries"].unmapped == ["foo"]
    assert by_slot["company_size"].ids and "y-custom" not in by_slot["company_size"].ids
    assert by_slot["company_size"].labels == ["201-500"]


# -- Task 12c: each entry names its LinkedIn filter; scoring-only slots are not filters --


async def test_filter_entries_carry_the_linkedin_filter_name():
    state = _filters_state(lead_geographies=["India"], company_type=["Privately Held"])
    state.slots["industries"] = _resolved("industries", ["Financial Services"])
    state.slots["company_size"] = _resolved("company_size", ["51-200"])
    state.slots["buyer_seniority"] = _resolved("buyer_seniority", ["Director"])
    doc = await _build(state)
    names = {f.slot: f.filter_name for f in doc.linkedin_filters}
    assert names["industries"] == "Industry"
    assert names["company_size"] == "Company headcount"
    assert names["lead_geographies"] == "Geography"
    assert names["buyer_seniority"] == "Seniority level"


async def test_scoring_only_slots_stay_out_of_the_filters_block_but_in_the_document():
    state = _filters_state(
        track="hr",
        target_seniority=["Senior"],
        current_company_hq=["India"],
        candidate_locations=["India"],
    )
    doc = await _build(state)
    by_slot = {f.slot: f for f in doc.linkedin_filters}
    assert "target_seniority" not in by_slot and "current_company_hq" not in by_slot
    assert by_slot["candidate_locations"].filter_name == "Locations"
    fields = {k for section in doc.sections.values() for k in section}
    assert {"target_seniority", "current_company_hq"} <= fields


def test_editing_a_scoring_only_slot_adds_no_filter():
    from icp_chatbot.icp.document import with_slot_filter

    assert with_slot_filter([], "target_seniority", ["Senior"], "hr") == []
    (entry,) = with_slot_filter([], "candidate_locations", ["India"], "hr")
    assert entry.filter_name == "Locations"
