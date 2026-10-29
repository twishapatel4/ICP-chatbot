"""The ICP as a stored, versioned JSON document (spec section 6)."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from icp_chatbot.domain.slots import (
    CustomField,
    SlotDef,
    Tier,
    Track,
    registry,
    section_order,
    slot_map,
    with_custom,
)
from icp_chatbot.domain.state import (
    ANY,
    SessionState,
    SlotSource,
    real_values,
    rename_slot_items,
    rename_slot_keys,
    renamed_slot,
    resolve_slot_values,
)
from icp_chatbot.icp.scoring import ScoringModel
from icp_chatbot.linkedin.catalog import SLOT_FACETS

KNOWN_CONFIDENCE = 0.7


class FieldEntry(BaseModel):
    values: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    source: SlotSource
    evidence: str = ""
    last_verified: str | None = None  # ISO date, set when the user states, confirms or approves it
    requirement: Literal["required", "preferred", "none"] = "none"


class Completeness(BaseModel):
    tier_a: float  # share of applicable tier A slots known at confidence >= 0.7
    tier_b: float  # share of tier B slots known at confidence >= 0.7


class FilterEntry(BaseModel):
    """One LinkedIn search filter: the catalog ids of what the company asked for in one slot."""

    facet: str
    slot: str
    ids: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    unmapped: list[str] = Field(default_factory=list)  # values with no LinkedIn option


class ICPDocument(BaseModel):
    schema_version: Literal["1"] = "1"
    track: Track = "sales"  # documents stored before tracks existed are Sales documents
    sections: dict[str, dict[str, FieldEntry]]
    scoring_model: ScoringModel
    summary: str = ""
    completeness: Completeness
    open_questions: list[str] = Field(default_factory=list)
    built_at_turn: int = 0  # answers received when the bot built this version
    custom_fields: list[CustomField] = Field(default_factory=list)  # this industry's own fields
    not_needed: list[str] = Field(default_factory=list)  # standard fields this industry skips
    partial: bool = False  # built before the basics were known, because the user stopped early
    linkedin_filters: list[FilterEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _rename_legacy_slots(cls, data: Any) -> Any:
        """Stored versions may name slots by their old keys (see LEGACY_SLOT_KEYS): in the
        sections, in what each criterion reads and its rules, in exclusions and in not_needed."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if isinstance(data.get("sections"), dict):
            data["sections"] = {
                name: rename_slot_keys(fields) if isinstance(fields, dict) else fields
                for name, fields in data["sections"].items()
            }
        if isinstance(data.get("not_needed"), list):
            data["not_needed"] = rename_slot_items(data["not_needed"])
        if isinstance(data.get("scoring_model"), dict):
            data["scoring_model"] = _rename_in_scoring_model(data["scoring_model"])
        return data

    def slot_defs(self) -> list[SlotDef]:
        return with_custom(self.custom_fields, self.track)

    def needed_defs(self) -> list[SlotDef]:
        """The fields this industry cares about: completeness and gaps ignore the rest."""
        return [s for s in self.slot_defs() if s.key not in self.not_needed]

    def entries(self) -> dict[str, FieldEntry]:
        return {slot: e for section in self.sections.values() for slot, e in section.items()}

    def slot_values(self) -> dict[str, list[str]]:
        return {slot: e.values for slot, e in self.entries().items() if e.values}


def _rename_in_scoring_model(model: dict) -> dict:
    model = dict(model)
    if isinstance(model.get("criteria"), list):
        criteria = []
        for criterion in model["criteria"]:
            if isinstance(criterion, dict):
                criterion = dict(criterion)
                if isinstance(criterion.get("reads"), list):
                    criterion["reads"] = rename_slot_items(criterion["reads"])
                if isinstance(criterion.get("rules"), dict):
                    criterion["rules"] = rename_slot_keys(criterion["rules"])
            criteria.append(criterion)
        model["criteria"] = criteria
    if isinstance(model.get("disqualifiers"), list):
        model["disqualifiers"] = [
            {**d, "field": renamed_slot(d["field"])} if isinstance(d, dict) and "field" in d else d
            for d in model["disqualifiers"]
        ]
    return model


def group_by_section(
    entries: dict[str, FieldEntry], defs: list[SlotDef] | None = None, track: Track = "sales"
) -> dict[str, dict[str, FieldEntry]]:
    """Place entries in their sections, in registry order; empty sections are omitted."""
    defs = defs or with_custom([], track)
    grouped: dict[str, dict[str, FieldEntry]] = {}
    for section in section_order(track):
        fields = {
            s.key: entries[s.key]
            for s in defs
            if s.section == section and s.key in entries and entries[s.key].values
        }
        if fields:
            grouped[section] = fields
    return grouped


def _applicable(slot, entries: dict[str, FieldEntry]) -> bool:
    for other_key, other_value in slot.skip_if:
        other = entries.get(other_key)
        if other and any(v.lower() == other_value.lower() for v in other.values):
            return False
    return True


def _is_known(slot, entries: dict[str, FieldEntry]) -> bool:
    entry = entries.get(slot.key)
    return bool(
        entry and entry.confidence >= KNOWN_CONFIDENCE and real_values(slot.key, entry.values)
    )


def missing_slots(
    entries: dict[str, FieldEntry],
    limit: int = 5,
    defs: list[SlotDef] | None = None,
    track: Track = "sales",
) -> list[str]:
    """Plain-language notes for the tier A and B fields that are still unknown (not recorded,
    only a non-answer, or below the known confidence), most important first. Worked out from the
    data, so the list does not depend on what the model remembers to mention."""
    gaps = [
        s
        for s in defs or registry(track)
        if s.tier in (Tier.A, Tier.B) and _applicable(s, entries) and not _is_known(s, entries)
    ]
    gaps.sort(key=lambda s: (s.tier != Tier.A, -s.importance))
    return [_gap_note(s, entries) for s in gaps[:limit]]


def _gap_note(slot: SlotDef, entries: dict[str, FieldEntry]) -> str:
    """A field the card already shows is only unconfirmed, not unknown: saying "Not yet known"
    under its values reads as a contradiction."""
    entry = entries.get(slot.key)
    shown = real_values(slot.key, entry.values) if entry else []
    if shown:
        return f"Please confirm: {slot.label.lower()} ({', '.join(shown)}) is only a guess so far."
    return f"Not yet known: {slot.label.lower()}. {slot.description.rstrip('.')}."


def compute_completeness(
    entries: dict[str, FieldEntry], defs: list[SlotDef] | None = None, track: Track = "sales"
) -> Completeness:
    def share(slots) -> float:
        slots = [s for s in slots if _applicable(s, entries)]
        if not slots:
            return 1.0
        return round(sum(1 for s in slots if _is_known(s, entries)) / len(slots), 4)

    defs = defs or list(registry(track))
    tier_a = [s for s in defs if s.tier == Tier.A and s.blocks]
    tier_b = [s for s in defs if s.tier == Tier.B]
    return Completeness(tier_a=share(tier_a), tier_b=share(tier_b))


def render_summary(
    entries: dict[str, FieldEntry], defs: list[SlotDef] | None = None, track: Track = "sales"
) -> str:
    """A readable summary generated only from the fields, so the two cannot disagree. Values that
    were inferred rather than stated are marked."""

    def show(key: str) -> str | None:
        entry = entries.get(key)
        if not entry or not entry.values:
            return None
        text = ", ".join(entry.values)
        inferred = entry.source in (SlotSource.INFERRED, SlotSource.DEFAULT)
        return f"{text} (inferred)" if inferred else text

    if track == "hr":
        return _render_hr_summary(show, defs or list(registry("hr")), entries)
    lines = []
    about = [
        ("What they do", "product"),
        ("sells to", "business_model"),
        ("how they work", "how_we_work"),
        ("clients come from", "lead_sources"),
        ("chosen for", "value_proposition"),
    ]
    mine = "; ".join(f"{label}: {v}" for label, key in about if (v := show(key)))
    if mine:
        lines.append(f"About the company: {mine}.")
    parts = [
        ("Who they are", "customer_profile"),
        ("Target industries", "industries"),
        ("company size", "company_size"),
        ("based in", "lead_geographies"),
        ("headquartered in", "company_hq_geographies"),
    ]
    own = [s for s in defs or [] if s.section == "industry_specifics" and show(s.key)]
    parts += [(s.label.lower(), s.key) for s in own]
    firm = "; ".join(f"{label}: {v}" for label, key in parts if (v := show(key)))
    if firm:
        lines.append(f"Ideal customer: {firm}.")
    for label, keys in (
        ("Buyers", ("buyer_roles", "buyer_seniority", "buyer_departments")),
        ("Buying triggers", ("buying_triggers",)),
        ("Exclusions", ("disqualifiers",)),
    ):
        found = [v for k in keys if (v := show(k))]
        if found:
            lines.append(f"{label}: {'; '.join(found)}.")
    return "\n".join(lines)


def _render_hr_summary(show, defs: list[SlotDef], entries: dict[str, FieldEntry]) -> str:
    """The Ideal candidate summary: one line per section that has answers, in the HR order. The
    first section, who the role is for, opens the "Ideal candidate" line."""
    lines = []
    for section in section_order("hr"):
        parts = [
            f"{s.label.lower()}: {v}" for s in defs if s.section == section and (v := show(s.key))
        ]
        if not parts:
            continue
        body = "; ".join(parts)
        if section == "role_and_seniority":
            lines.append(f"Ideal candidate: {body}.")
        else:
            title = section.replace("_", " ").capitalize()
            lines.append(f"{title}: {body}.")
    return "\n".join(lines)


def sync_field_requirements(document: ICPDocument) -> ICPDocument:
    """A field takes the requirement of the criterion that reads it; fields no criterion reads
    stay "none". Keeps the per-field flags and the scoring model consistent."""
    requirement_by_slot: dict[str, str] = {}
    for criterion in document.scoring_model.criteria:
        if not criterion.applicable:
            continue
        for slot in criterion.rules:
            # required wins if two criteria read the same slot
            if requirement_by_slot.get(slot) != "required":
                requirement_by_slot[slot] = criterion.requirement
    sections = {
        name: {
            slot: entry.model_copy(update={"requirement": requirement_by_slot.get(slot, "none")})
            for slot, entry in fields.items()
        }
        for name, fields in document.sections.items()
    }
    return document.model_copy(update={"sections": sections})


def known_slot_keys(track: Track = "sales") -> set[str]:
    return set(slot_map(track))


def _filter_entry(
    slot: str, values: list[str], refs: list, unmapped: list[str]
) -> FilterEntry | None:
    # non-answers carry no filter, and "Any" (no preference) never travels with other values
    values = [v for v in real_values(slot, values) if v != ANY]
    if not values:
        return None
    if not refs and not unmapped:  # inferred or confirmed values were never matched to options
        values, refs, unmapped = resolve_slot_values(slot, values)
    ids: list[str] = []
    for ref in refs:
        if ref.id not in ids:
            ids.append(ref.id)
    return FilterEntry(
        facet=SLOT_FACETS[slot], slot=slot, ids=ids, labels=list(values), unmapped=list(unmapped)
    )


def build_linkedin_filters(state: SessionState) -> list[FilterEntry]:
    """One entry per answered slot that LinkedIn can filter on, in catalog order."""
    own = slot_map(state.track)
    found = []
    for slot in SLOT_FACETS:
        value = state.slots.get(slot)
        if slot not in own or value is None:
            continue
        entry = _filter_entry(slot, value.values, value.refs, value.unmapped)
        if entry:
            found.append(entry)
    return found


def with_slot_filter(
    filters: list[FilterEntry], slot: str, values: list[str], track: Track
) -> list[FilterEntry]:
    """The block after one slot was edited: only that slot's entry is replaced (or removed when
    the slot has no facet or the new values add no filter); every other entry stays as stored.
    Typed edits carry no refs, so the entry comes from catalog resolution."""
    kept = [f for f in filters if f.slot != slot]
    if slot not in SLOT_FACETS or slot not in slot_map(track):
        return kept
    entry = _filter_entry(slot, values, [], [])
    if entry is None:
        return kept
    merged = {f.slot: f for f in [*kept, entry]}
    return [merged[k] for k in SLOT_FACETS if k in merged]
