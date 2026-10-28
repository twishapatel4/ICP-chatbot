"""Step 1 of every turn: turn the user's answer into slot updates (spec section 5.2)."""

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from icp_chatbot.domain.slots import SLOTS, CustomField
from icp_chatbot.domain.state import (
    CONFIRMED_CONFIDENCE,
    NONE_IS_AN_ANSWER,
    DomainContext,
    SessionState,
    SlotSource,
    SlotValue,
    is_non_answer,
    normalize_values,
    real_values,
)

EVIDENCE_LIMIT = 200
SUMMARY_LIMIT = 600
INDUSTRY_LIMIT = 80
FOCUS_LIMIT = 8
FIELD_LIMIT = 7
PROTECTED_SLOTS = frozenset({"product", "business_model"})
FOCUS_ITEM_LIMIT = 80
BRIEF_LIMIT = 700
STAYS_ON_CHANGE = frozenset({"online_presence"})  # facts about the user, not the old market


class SlotUpdate(BaseModel):
    slot: str
    values: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""


class Flags(BaseModel):
    contradiction_slots: list[str] = Field(default_factory=list)
    corrects: bool = False  # the user says an earlier answer was wrong and gives the right one


class DomainUpdate(BaseModel):
    industry: str = ""
    brief: str = ""
    focus: list[str] = Field(default_factory=list)
    fields: list[CustomField] = Field(default_factory=list)  # what this industry must know
    not_needed: list[str] = Field(default_factory=list)  # standard slot keys that do not matter
    changed_market: bool = False  # the user says the earlier industry was wrong or has changed


class ExtractionResult(BaseModel):
    updates: list[SlotUpdate] = Field(default_factory=list)
    domain: DomainUpdate | None = None
    flags: Flags = Field(default_factory=Flags)
    summary: str = ""


@dataclass
class ApplyOutcome:
    applied: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # unknown slot keys from the model
    # slot -> (what they said before, what they say now), when they changed their own answer
    changed: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)


def apply_extraction(state: SessionState, result: ExtractionResult, turn_no: int) -> ApplyOutcome:
    """Merge the model's updates into the state, enforcing the rules the model cannot be trusted
    with: only known slots, a stated value replaces an inferred one, and an inferred value the
    user agrees with becomes confirmed."""
    outcome = ApplyOutcome()
    own = {f.key for f in _custom(state)}
    if result.domain:  # fields defined in this very answer can already be filled by it
        own |= {f.key for f in _clean_fields(result.domain.fields)}
    for update in result.updates:
        if update.slot not in SLOTS and update.slot not in own:
            outcome.ignored.append(update.slot)
            continue
        values = real_values(update.slot, normalize_values(update.values))
        if update.slot not in NONE_IS_AN_ANSWER and is_non_answer(update.evidence):
            continue  # the quoted answer was "no" or "none": nothing was said about this slot
        if not values:
            continue
        existing = state.slots.get(update.slot)
        evidence = update.evidence[:EVIDENCE_LIMIT]

        if existing and existing.source == SlotSource.INFERRED:
            same = {v.lower() for v in existing.values} == {v.lower() for v in values}
            if same:  # the user agrees with the inference: confirmed
                state.slots[update.slot] = SlotValue(
                    values=existing.values,
                    confidence=max(update.confidence, CONFIRMED_CONFIDENCE),
                    source=SlotSource.CONFIRMED,
                    source_turn=turn_no,
                    evidence=evidence or existing.evidence,
                )
                outcome.confirmed.append(update.slot)
                outcome.applied.append(update.slot)
                continue
        elif existing and update.confidence < existing.confidence:
            continue  # a weaker statement does not replace a stronger one

        if (
            update.slot in result.flags.contradiction_slots
            and existing
            and existing.source in (SlotSource.STATED, SlotSource.CONFIRMED)
            and {v.lower() for v in existing.values} != {v.lower() for v in values}
        ):
            outcome.changed[update.slot] = (list(existing.values), list(values))
        state.slots[update.slot] = SlotValue(
            values=values,
            confidence=update.confidence,
            source=SlotSource.STATED,
            source_turn=turn_no,
            evidence=evidence,
        )
        outcome.applied.append(update.slot)
        if update.slot in state.contradictions:  # a new value settles an earlier contradiction
            state.contradictions.remove(update.slot)

    for slot in result.flags.contradiction_slots:
        if slot in outcome.applied:
            continue  # this very answer gave the new value: the contradiction is settled
        if (slot in SLOTS or slot in own) and slot not in state.contradictions:
            state.contradictions.append(slot)
    _apply_domain(state, result.domain, outcome.applied)
    if result.summary.strip():
        state.summary = result.summary.strip()[:SUMMARY_LIMIT]
    return outcome


def _apply_domain(state: SessionState, update: DomainUpdate | None, kept_slots: list[str]) -> None:
    """Keep the industry context. A new industry replaces the old one; a brief without an
    industry is ignored; a missing brief keeps the one we have for the same industry."""
    if update is None:
        return
    industry = " ".join(update.industry.split())[:INDUSTRY_LIMIT]
    if not industry:
        return
    brief = " ".join(update.brief.split())[:BRIEF_LIMIT]
    focus = normalize_values([" ".join(f.split())[:FOCUS_ITEM_LIMIT] for f in update.focus])[
        :FOCUS_LIMIT
    ]
    current = state.domain
    if update.changed_market and current and current.industry.casefold() != industry.casefold():
        # what was said about the old market no longer holds, except what was just said
        for key in [k for k in state.slots if k not in kept_slots and k not in STAYS_ON_CHANGE]:
            del state.slots[key]
            state.contradictions = [c for c in state.contradictions if c != key]
    fields = _clean_fields(update.fields)
    not_needed = [k for k in update.not_needed if k in SLOTS and k not in PROTECTED_SLOTS]
    if current and current.industry.casefold() == industry.casefold():
        known = {f.key for f in current.fields}
        added = [f for f in fields if f.key not in known]  # fields already asked about stay put
        state.domain = DomainContext(
            industry=current.industry,
            brief=brief or current.brief,
            focus=focus or current.focus,
            fields=[*current.fields, *added][:FIELD_LIMIT],
            not_needed=normalize_values([*current.not_needed, *not_needed]),
        )
        return
    state.domain = DomainContext(
        industry=industry,
        brief=brief,
        focus=focus,
        fields=fields,
        not_needed=normalize_values(not_needed),
    )


def _custom(state: SessionState) -> list[CustomField]:
    return state.domain.fields if state.domain else []


def _clean_fields(fields: list[CustomField]) -> list[CustomField]:
    """The industry's own fields from the model: readable snake_case keys that never clash with
    a standard slot, each with a label and a plain question."""
    out: list[CustomField] = []
    for f in fields:
        key = re.sub(r"[^a-z0-9]+", "_", f.key.lower()).strip("_")[:40]
        label, ask = " ".join(f.label.split())[:60], " ".join(f.ask.split())[:200]
        if not key or key in SLOTS or not label or not ask or any(o.key == key for o in out):
            continue
        out.append(f.model_copy(update={"key": key, "label": label, "ask": ask}))
    return out[:FIELD_LIMIT]
