"""The ICP as a stored, versioned JSON document (spec section 6)."""

from typing import Literal

from pydantic import BaseModel, Field

from icp_chatbot.domain.slots import (
    SECTION_ORDER,
    SLOT_LIST,
    SLOTS,
    CustomField,
    SlotDef,
    Tier,
    with_custom,
)
from icp_chatbot.domain.state import SlotSource, real_values
from icp_chatbot.icp.scoring import ScoringModel

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


class ICPDocument(BaseModel):
    schema_version: Literal["1"] = "1"
    sections: dict[str, dict[str, FieldEntry]]
    scoring_model: ScoringModel
    summary: str = ""
    completeness: Completeness
    open_questions: list[str] = Field(default_factory=list)
    built_at_turn: int = 0  # answers received when the bot built this version
    custom_fields: list[CustomField] = Field(default_factory=list)  # this industry's own fields
    not_needed: list[str] = Field(default_factory=list)  # standard fields this industry skips
    partial: bool = False  # built before the basics were known, because the user stopped early

    def slot_defs(self) -> list[SlotDef]:
        return with_custom(self.custom_fields)

    def needed_defs(self) -> list[SlotDef]:
        """The fields this industry cares about: completeness and gaps ignore the rest."""
        return [s for s in self.slot_defs() if s.key not in self.not_needed]

    def entries(self) -> dict[str, FieldEntry]:
        return {slot: e for section in self.sections.values() for slot, e in section.items()}

    def slot_values(self) -> dict[str, list[str]]:
        return {slot: e.values for slot, e in self.entries().items() if e.values}


def group_by_section(
    entries: dict[str, FieldEntry], defs: list[SlotDef] | None = None
) -> dict[str, dict[str, FieldEntry]]:
    """Place entries in their sections, in registry order; empty sections are omitted."""
    defs = defs or with_custom([])
    grouped: dict[str, dict[str, FieldEntry]] = {}
    for section in SECTION_ORDER:
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
    entries: dict[str, FieldEntry], limit: int = 5, defs: list[SlotDef] | None = None
) -> list[str]:
    """Plain-language notes for the tier A and B fields that are still unknown (not recorded,
    only a non-answer, or below the known confidence), most important first. Worked out from the
    data, so the list does not depend on what the model remembers to mention."""
    gaps = [
        s
        for s in defs or SLOT_LIST
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
    entries: dict[str, FieldEntry], defs: list[SlotDef] | None = None
) -> Completeness:
    def share(slots) -> float:
        slots = [s for s in slots if _applicable(s, entries)]
        if not slots:
            return 1.0
        return round(sum(1 for s in slots if _is_known(s, entries)) / len(slots), 4)

    defs = defs or list(SLOT_LIST)
    tier_a = [s for s in defs if s.tier == Tier.A and s.blocks]
    tier_b = [s for s in defs if s.tier == Tier.B]
    return Completeness(tier_a=share(tier_a), tier_b=share(tier_b))


def render_summary(entries: dict[str, FieldEntry], defs: list[SlotDef] | None = None) -> str:
    """A readable summary generated only from the fields, so the two cannot disagree. Values that
    were inferred rather than stated are marked."""

    def show(key: str) -> str | None:
        entry = entries.get(key)
        if not entry or not entry.values:
            return None
        text = ", ".join(entry.values)
        inferred = entry.source in (SlotSource.INFERRED, SlotSource.DEFAULT)
        return f"{text} (inferred)" if inferred else text

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


def known_slot_keys() -> set[str]:
    return set(SLOTS)
