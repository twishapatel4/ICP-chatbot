"""The static questionnaire used when the interview cannot continue (spec section 12): a model
outage, repeated off-topic input, or a user who prefers a form. Signup is never blocked."""

from dataclasses import dataclass

from icp_chatbot.domain.slots import SLOT_LIST, SLOTS, Tier
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue, normalize_values

FORM_CONFIDENCE = 0.8
OPTIONAL_FORM_SLOTS = ("buying_triggers",)


@dataclass(frozen=True)
class FormField:
    key: str
    label: str
    description: str
    choices: tuple[str, ...]
    required: bool


def fallback_form() -> list[FormField]:
    """Every tier A slot that blocks completion (required), then a few tier B slots (optional)."""
    fields = [
        FormField(s.key, s.label, s.description, s.choices, required=True)
        for s in SLOT_LIST
        if s.tier == Tier.A and s.blocks
    ]
    for key in OPTIONAL_FORM_SLOTS:
        s = SLOTS[key]
        fields.append(FormField(s.key, s.label, s.description, s.choices, required=False))
    return fields


def missing_required(answers: dict[str, list[str]], state: SessionState) -> list[str]:
    """Required fields left empty, ignoring those that do not apply (buyer slots for B2C)."""
    have = {k for k, v in answers.items() if normalize_values(v)} | {
        k for k, v in state.slots.items() if v.values
    }
    return [
        f.key
        for f in fallback_form()
        if f.required and f.key not in have and state.is_applicable(SLOTS[f.key])
    ]


def apply_form(state: SessionState, answers: dict[str, list[str]], turn_no: int) -> list[str]:
    """Record form answers as stated values. Unknown fields are an error; empty ones are skipped."""
    unknown = [k for k in answers if k not in SLOTS]
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    applied = []
    for key, values in answers.items():
        values = normalize_values(values)
        if not values:
            continue
        state.slots[key] = SlotValue(
            values=values,
            confidence=FORM_CONFIDENCE,
            source=SlotSource.STATED,
            source_turn=turn_no,
            evidence="entered in the form",
        )
        applied.append(key)
    return applied
