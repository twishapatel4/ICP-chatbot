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
