from icp_chatbot.domain.state import (
    ANY,
    OptionRef,
    SessionState,
    SlotSource,
    SlotValue,
    real_values,
    resolve_slot_values,
)
from icp_chatbot.engine.extraction import ExtractionResult, SlotUpdate, apply_extraction


def test_resolve_industry_label_gives_ref_with_id():
    labels, refs, unmapped = resolve_slot_values("industries", ["Software Development"])
    assert labels == ["Software Development"]
    assert len(refs) == 1 and refs[0].facet == "industry" and refs[0].id
    assert refs[0].parent is not None
    assert unmapped == []


def test_resolve_size_range_string_uses_match():
    labels, refs, unmapped = resolve_slot_values("company_size", ["11-50", "2-10"])
    assert labels == ["11-50", "1-10"]
    assert [r.id for r in refs] == ["C", "B"]
    assert unmapped == []


def test_unresolvable_value_is_kept_as_unmapped():
    labels, refs, unmapped = resolve_slot_values("industries", ["Software Development", "Basket"])
    assert labels == ["Software Development", "Basket"]
    assert len(refs) == 1
    assert unmapped == ["Basket"]


def test_slot_without_facet_resolves_nothing():
    assert resolve_slot_values("product", ["CRM"]) == (["CRM"], [], [])


def test_no_catalog_for_facet_keeps_values():
    class Empty:
        def has(self, facet):
            return False

    assert resolve_slot_values("industries", ["x"], Empty()) == (["x"], [], [])


def test_any_is_a_real_answer():
    assert real_values("industries", [ANY]) == [ANY]
    assert resolve_slot_values("industries", [ANY]) == ([ANY], [], [])


def test_old_slot_value_without_refs_loads():
    sv = SlotValue.model_validate({"values": ["a"], "confidence": 0.5, "source": "stated"})
    assert sv.refs == [] and sv.unmapped == []
    assert OptionRef(facet="industry", id="1").level == 1


def test_country_resolves_and_cities_are_free_text():
    labels, refs, unmapped = resolve_slot_values("lead_geographies", ["India", "Pune, India", "US"])
    assert labels[0] == "India" and labels[1] == "Pune, India"
    assert [r.id for r in refs] == ["IN", "US"]
    assert unmapped == []


def test_extraction_fills_refs_and_unmapped():
    state = SessionState()
    result = ExtractionResult(
        updates=[
            SlotUpdate(
                slot="industries",
                values=["Software Development", "Basket"],
                confidence=0.8,
                evidence="x",
            )
        ]
    )
    apply_extraction(state, result, 1)
    sv = state.slots["industries"]
    assert sv.source == SlotSource.STATED
    assert len(sv.refs) == 1 and sv.unmapped == ["Basket"]


def test_aliases_of_one_option_give_one_label_and_one_ref():
    labels, refs, _ = resolve_slot_values("lead_geographies", ["US", "United States"])
    assert labels == ["United States"] and [r.id for r in refs] == ["US"]
    labels, refs, _ = resolve_slot_values("company_size", ["2-10", "1-10"])
    assert labels == ["1-10"] and [r.id for r in refs] == ["B"]


def test_same_unmapped_value_in_two_cases_appears_once():
    _, _, unmapped = resolve_slot_values("industries", ["Basket", "basket"])
    assert unmapped == ["Basket"]


def test_short_free_text_is_not_a_country_code():
    labels, refs, unmapped = resolve_slot_values("lead_geographies", ["in", "No", "IN", "india"])
    assert labels == ["in", "No", "India"]
    assert [r.id for r in refs] == ["IN"] and unmapped == []
    assert resolve_slot_values("company_size", ["b"])[1] == []
    assert resolve_slot_values("company_size", ["B"])[1][0].id == "B"
