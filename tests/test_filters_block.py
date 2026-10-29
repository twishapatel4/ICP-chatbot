"""R30: every slot with a LinkedIn filter name gets an entry in the filters block, facet or not;
slots a skip rule hides (B2C) stay out of the document and the block."""

from datetime import date

from icp_chatbot.config import Settings
from icp_chatbot.domain.slots import registry
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue, resolve_slot_values
from icp_chatbot.icp.builder import ICPSynthesis, build_icp
from icp_chatbot.icp.document import FilterEntry, with_slot_filter
from icp_chatbot.icp.review import FieldEdit, apply_edit, carry_over_edits
from icp_chatbot.linkedin.catalog import default_catalog
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.versions import default_config


def _state(track="sales", **slots):
    state = SessionState(track=track)
    for key, values in slots.items():
        state.slots[key] = SlotValue(values=values, confidence=0.9, source=SlotSource.STATED)
    return state


def _resolved(slot_key, values):
    labels, refs, unmapped = resolve_slot_values(slot_key, values)
    return SlotValue(
        values=labels, refs=refs, unmapped=unmapped, confidence=0.9, source=SlotSource.STATED
    )


async def _build(state):
    llm = FakeLLM({"final_icp": [ICPSynthesis()]})
    config = default_config(Settings(_env_file=None))
    return (await build_icp(llm, config, state, today=date(2026, 10, 15))).document


def _by_slot(doc):
    return {f.slot: f for f in doc.linkedin_filters}


def _order(track):
    return [s.key for s in registry(track)]


async def _sales_doc():
    state = _state(lead_geographies=["Europe"], buyer_roles=["CFO"])
    state.slots["industries"] = _resolved("industries", ["Financial Services"])
    state.slots["company_size"] = _resolved("company_size", ["51-200"])
    return await _build(state)


async def test_sales_block_lists_facetless_slots_as_free_text():
    state = _state(
        buyer_roles=["CFO", "VP Finance"],
        company_type=["Privately Held"],
        revenue_range=["$10M-$50M"],
        technologies_used=["Salesforce"],
        buying_triggers=["Recently raised funding"],
        product=["Payroll software"],  # no LinkedIn filter: stays out
        years_in_operation=["5+ years"],  # scoring-only: stays out
    )
    state.slots["industries"] = _resolved("industries", ["Financial Services"])
    doc = await _build(state)
    by_slot = _by_slot(doc)
    expected = {
        "buyer_roles": "Current job title",
        "company_type": "Company type",
        "revenue_range": "Annual revenue",
        "technologies_used": "Technologies used",
        "buying_triggers": "Recent updates / Spotlight",
    }
    for slot, name in expected.items():
        entry = by_slot[slot]
        assert entry.facet == "free_text" and entry.filter_name == name
        assert entry.ids == [] and entry.unmapped == []
        assert entry.labels == state.slots[slot].values
    assert "product" not in by_slot and "years_in_operation" not in by_slot
    assert by_slot["industries"].facet == "industry"
    slots = [f.slot for f in doc.linkedin_filters]
    assert slots == sorted(slots, key=_order("sales").index)


async def test_hr_block_lists_facetless_slots_and_flags_absent_catalogs():
    state = _state(
        track="hr",
        target_roles=["Backend Engineer"],
        required_skills=["Python", "SQL"],
        years_experience=["3-5"],
        graduation_year=["2018"],
        target_schools=["IIT Bombay"],
        nice_to_have_skills=["Kafka"],
        certifications=["AWS"],
        languages=["English"],
        employment_type=["Full-time"],
        work_mode=["Remote"],
        source_companies=["Acme"],
        avoid_companies=["Globex"],
        target_seniority=["Senior"],
        current_company_hq=["India"],
        education_level=["Bachelor's"],
        field_of_study=["Computer Science"],
    )
    doc = await _build(state)
    by_slot = _by_slot(doc)
    for slot in (
        "target_roles", "required_skills", "years_experience", "graduation_year", "target_schools",
        "nice_to_have_skills", "certifications", "languages", "employment_type", "work_mode",
        "source_companies", "avoid_companies",
    ):  # fmt: skip
        assert by_slot[slot].facet == "free_text" and by_slot[slot].ids == []
        assert by_slot[slot].filter_name
    assert by_slot["required_skills"].filter_name == "Skills"
    assert by_slot["required_skills"].labels == ["Python", "SQL"]
    assert "target_seniority" not in by_slot and "current_company_hq" not in by_slot
    slots = [f.slot for f in doc.linkedin_filters]
    assert slots == sorted(slots, key=_order("hr").index)
    for slot, facet in (("education_level", "degree"), ("field_of_study", "field_of_study")):
        assert by_slot[slot].facet == facet
        if not default_catalog().has(facet):  # nothing is mapped until the sync has run
            assert by_slot[slot].ids == []
            assert by_slot[slot].unmapped == state.slots[slot].values


async def test_facetless_field_edit_adds_and_removes_only_its_entry():
    doc = await _sales_doc()
    before = _by_slot(doc)
    assert before["buyer_roles"].facet == "free_text"
    edited, _ = apply_edit(doc, FieldEdit(slot="buyer_roles", values=["CEO"]), date.today())
    after = _by_slot(edited)
    assert after["buyer_roles"].labels == ["CEO"] and after["buyer_roles"].ids == []
    assert {k: v for k, v in after.items() if k != "buyer_roles"} == {
        k: v for k, v in before.items() if k != "buyer_roles"
    }
    added, _ = apply_edit(doc, FieldEdit(slot="technologies_used", values=["HubSpot"]), date.today())
    assert _by_slot(added)["technologies_used"].labels == ["HubSpot"]
    assert len(added.linkedin_filters) == len(doc.linkedin_filters) + 1
    slots = [f.slot for f in added.linkedin_filters]
    assert slots == sorted(slots, key=_order("sales").index)
    for values in (["Any"], []):
        removed, _ = apply_edit(doc, FieldEdit(slot="buyer_roles", values=values), date.today())
        assert "buyer_roles" not in _by_slot(removed)
        assert len(removed.linkedin_filters) == len(doc.linkedin_filters) - 1


async def test_facetless_edit_preserves_handmade_entries_and_carry_over_applies_it():
    doc = await _sales_doc()
    handmade = [
        FilterEntry(
            facet="industry", slot="industries", ids=["x-custom"], labels=["Whatever"],
            unmapped=["foo"],
        ),
    ]  # fmt: skip
    doc = doc.model_copy(update={"linkedin_filters": handmade})
    edited, _ = apply_edit(doc, FieldEdit(slot="buyer_roles", values=["CEO"]), date.today())
    by_slot = _by_slot(edited)
    assert by_slot["industries"] == handmade[0] and by_slot["buyer_roles"].labels == ["CEO"]
    merged = carry_over_edits(await _sales_doc(), edited)
    assert _by_slot(merged)["buyer_roles"].labels == ["CEO"]


def test_with_slot_filter_handles_facetless_unknown_and_filterless_slots():
    (entry,) = with_slot_filter([], "required_skills", ["Go"], "hr")
    assert entry.facet == "free_text" and entry.labels == ["Go"] and entry.filter_name == "Skills"
    assert with_slot_filter([], "nonsense", ["x"], "hr") == []
    assert with_slot_filter([], "product", ["x"], "sales") == []
    assert with_slot_filter([], "required_skills", ["Any"], "hr") == []


async def test_legacy_document_with_a_stored_block_keeps_it_on_a_weight_edit():
    from icp_chatbot.icp.review import WeightEdit

    doc = await _sales_doc()
    stored = [f for f in doc.linkedin_filters if f.slot == "industries"]
    doc = doc.model_copy(update={"linkedin_filters": stored})
    weights = {c.key: c.weight for c in doc.scoring_model.criteria}
    first, second = list(weights)[:2]
    edited, _ = apply_edit(
        doc, WeightEdit(weights={first: weights[first] + 5, second: weights[second] - 5}),
        date.today(),
    )  # fmt: skip
    assert edited.linkedin_filters == stored


# -- B2C: slots a skip rule hides stay out of the document and the block ---------------------------


def _b2x_state(model):
    return _state(
        business_model=[model] if model else [],
        industries=["Financial Services"],
        company_hq_geographies=["India"],
        buyer_seniority=["Director"],
        lead_geographies=["India"],
    )


B2B_ONLY = {"industries", "company_hq_geographies", "buyer_seniority"}


async def test_b2c_leaves_b2b_only_slots_out_of_sections_summary_and_filters():
    state = _b2x_state("B2C")
    doc = await _build(state)
    fields = {k for section in doc.sections.values() for k in section}
    assert {"business_model", "lead_geographies"} <= fields
    assert not fields & B2B_ONLY
    assert not {f.slot for f in doc.linkedin_filters} & B2B_ONLY
    assert "lead_geographies" in {f.slot for f in doc.linkedin_filters}
    assert "Financial Services" not in doc.summary
    assert "industries" in state.slots  # the stored state keeps its values


async def test_b2b_and_unset_business_model_keep_every_slot():
    for model in ("B2B", None):
        doc = await _build(_b2x_state(model))
        fields = {k for section in doc.sections.values() for k in section}
        assert B2B_ONLY <= fields
        assert B2B_ONLY <= {f.slot for f in doc.linkedin_filters}
