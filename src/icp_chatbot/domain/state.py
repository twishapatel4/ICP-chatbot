"""Per-session working memory (spec section 5.1)."""

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from icp_chatbot.domain.slots import SLOTS, SlotDef

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


class SessionState(BaseModel):
    mode: Literal["quick", "detail"] = "quick"
    slots: dict[str, SlotValue] = Field(default_factory=dict)
    seed: dict[str, SlotValue] = Field(default_factory=dict)  # raw enrichment output, kept apart
    summary: str = ""
    last_turns: list[TurnPair] = Field(default_factory=list)  # at most the last two
    asked_slots: dict[str, int] = Field(default_factory=dict)  # slot key -> times asked
    followups: dict[str, int] = Field(default_factory=dict)  # question kind -> follow-ups asked
    contradictions: list[str] = Field(default_factory=list)
    best_customer_asked: bool = False
    questions_asked: int = 0
    answers_received: int = 0
    detail_started_at: int | None = None  # questions_asked when detail mode began
    pending_slot: str | None = None  # slot the last question targeted
    pending_question: str = ""
    pending_kind: str = ""  # opener, best_customer, slot, fallback
    segment: dict[str, str] = Field(default_factory=dict)
    failures: int = 0  # consecutive failed model steps
    offtopic_count: int = 0
    finished: bool = False
    stop_reason: str | None = None

    def confidence(self, key: str) -> float:
        value = self.slots.get(key)
        return value.confidence if value else 0.0

    def values(self, key: str) -> list[str]:
        value = self.slots.get(key)
        return list(value.values) if value else []

    def is_applicable(self, slot: SlotDef) -> bool:
        """A slot is skipped when a skip rule matches a value already known (e.g. B2C)."""
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
