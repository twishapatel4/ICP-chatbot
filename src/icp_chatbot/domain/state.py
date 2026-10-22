"""Per-session working memory (spec section 5.1)."""

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from icp_chatbot.domain.slots import (
    SLOT_LIST,
    SLOTS,
    CustomField,
    SlotDef,
    with_custom,
)

SEED_CONFIDENCE_CAP = 0.5  # inferred values never exceed this until the user confirms them
CONFIRMED_CONFIDENCE = 0.9  # confidence once the user confirms a value


class SlotSource(StrEnum):
    STATED = "stated"  # the user said it
    CONFIRMED = "confirmed"  # the user confirmed an inferred value
    INFERRED = "inferred"  # inferred from the website or other context
    DEFAULT = "default"
    DERIVED = "derived"


# Slots where "none" is a real answer ("no exclusions"), not a missing one.
NONE_IS_AN_ANSWER = frozenset({"disqualifiers"})

_NON_ANSWER = re.compile(
    r"^(no|nope|nah|none|nothing|nil|n/?a|na|not sure|unsure|idk|i ?don'?t know|dont know|"
    r"don'?t know|unknown|skip|pass|no idea|not applicable|can we (just )?(onboard|skip|move on))"
    r"[\s.!?]*$",
    re.IGNORECASE,
)


def is_non_answer(text: str) -> bool:
    """True for replies like "no", "none" or "not sure": they say nothing about the slot."""
    return bool(_NON_ANSWER.match(text.strip()))


def real_values(slot: str, values: list[str]) -> list[str]:
    """The values that actually carry information for this slot."""
    if slot in NONE_IS_AN_ANSWER:
        return list(values)
    return [v for v in values if not is_non_answer(v)]


def normalize_values(values: list[str]) -> list[str]:
    """Strip, drop empties, and drop case-insensitive duplicates, keeping order."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        text = str(v).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


class SlotValue(BaseModel):
    values: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    source: SlotSource
    source_turn: int | None = None
    evidence: str = ""
    last_verified: str | None = None

    @field_validator("values")
    @classmethod
    def _normalize(cls, v: list[str]) -> list[str]:
        return normalize_values(v)


class TurnPair(BaseModel):
    question: str
    answer: str


class DomainContext(BaseModel):
    """The industry the user's company works in, and what is typical for it. Learned from the
    first answers, then used to shape every later question and the final ICP."""

    industry: str
    brief: str = ""  # buyer roles, pains, tools, triggers and vocabulary typical for the industry
    focus: list[str] = Field(default_factory=list)  # things that decide fit in this industry
    fields: list[CustomField] = Field(default_factory=list)  # what this industry needs to know
    not_needed: list[str] = Field(default_factory=list)  # standard slots that do not matter here

    def prompt_lines(self) -> str:
        lines = f"Industry of this company: {self.industry}"
        if self.brief:
            lines += f"\nWhat is typical in this industry (background, not facts about them): {self.brief}"
        if self.fields:
            lines += "\nFields this industry needs (asked about one by one): " + "; ".join(
                f"{f.key} = {f.label}" for f in self.fields
            )
        if self.focus:
            lines += (
                "\nWhat decides who is a good customer in this industry (build the question "
                "around these, in these words): " + "; ".join(self.focus)
            )
        return lines


class SessionState(BaseModel):
    mode: Literal["quick", "detail"] = "quick"
    domain: DomainContext | None = None
    slots: dict[str, SlotValue] = Field(default_factory=dict)
    seed: dict[str, SlotValue] = Field(default_factory=dict)  # raw enrichment output, kept apart
    summary: str = ""
    last_turns: list[TurnPair] = Field(default_factory=list)  # at most the last two
    asked_slots: dict[str, int] = Field(default_factory=dict)  # slot key -> times asked
    probes: dict[str, int] = Field(default_factory=dict)  # slot -> clarifying follow-ups asked
    probe_conf: dict[str, float] = Field(default_factory=dict)  # slot -> confidence when probed
    stalls: dict[str, int] = Field(default_factory=dict)  # slot -> probes in a row that got nowhere
    skipped: list[str] = Field(default_factory=list)  # slots the user firmly declined to answer
    followups: dict[str, int] = Field(default_factory=dict)  # question kind -> follow-ups asked
    contradictions: list[str] = Field(default_factory=list)
    best_customer_asked: bool = False
    best_customer_at: int = 2  # the question number at which the best-customer question is due
    order_seed: int = 0  # non-zero: this session asks its topics in its own, shuffled order
    questions_asked: int = 0
    answers_received: int = 0
    detail_started_at: int | None = None  # questions_asked when detail mode began
    pending_slot: str | None = None  # slot the last question targeted
    pending_question: str = ""
    pending_kind: str = ""  # opener, best_customer, slot, fallback
    pending_options: list[str] = Field(default_factory=list)  # suggested answers, if any
    pending_multi: bool = True  # more than one option may be picked
    segment: dict[str, str] = Field(default_factory=dict)
    failures: int = 0  # consecutive failed model steps
    offtopic_count: int = 0
    user_questions: int = 0  # questions the user put to the bot, answered along the way
    declines: dict[str, int] = Field(
        default_factory=dict
    )  # slot or kind -> times a bare "no" was re-asked
    finished: bool = False
    stop_reason: str | None = None

    def confidence(self, key: str) -> float:
        value = self.slots.get(key)
        return value.confidence if value else 0.0

    def values(self, key: str) -> list[str]:
        value = self.slots.get(key)
        return list(value.values) if value else []

    def slot_defs(self) -> list[SlotDef]:
        return with_custom(self.domain.fields) if self.domain else list(SLOT_LIST)

    def slot_def(self, key: str) -> SlotDef | None:
        if key in SLOTS:
            return SLOTS[key]
        return next((s for s in self.slot_defs() if s.key == key), None)

    def is_applicable(self, slot: SlotDef) -> bool:
        """A slot is skipped when this industry does not need it, or when a skip rule matches a
        value already known (e.g. B2C)."""
        if self.domain and slot.key in self.domain.not_needed:
            return False
        for other_key, other_value in slot.skip_if:
            if any(v.lower() == other_value.lower() for v in self.values(other_key)):
                return False
        return True

    def topics_asked(self) -> set[str]:
        return {
            SLOTS[k].topic for k in self.asked_slots if k in SLOTS and SLOTS[k].topic is not None
        }

    def push_turn(self, question: str, answer: str) -> None:
        self.last_turns = (self.last_turns + [TurnPair(question=question, answer=answer)])[-2:]
