"""Chooses the next slot to ask about, or says when to stop (spec sections 5.2 and 5.5).

Plain code, no model. Deterministic: ties resolve by registry order.
"""

from dataclasses import dataclass

from icp_chatbot.domain.slots import SLOT_LIST, STOP_TOPICS, SlotDef, Tier
from icp_chatbot.domain.state import SessionState

COMPLETION_CONFIDENCE = 0.7  # a slot at or above this counts as known
REVISIT_BELOW = 0.4  # an asked slot is asked again only below this, or if contradicted
CONTRADICTED_CONFIDENCE = 0.2  # a contradicted slot is treated as barely known
MAX_ASKS_PER_SLOT = 3  # guard against loops
MIN_TOPICS = 3  # quick mode needs at least this many tier B topics asked

QUICK_WEIGHTS = {Tier.A: 1.0, Tier.B: 0.5}
DETAIL_WEIGHTS = {Tier.D: 1.0, Tier.A: 0.6, Tier.B: 0.4}


@dataclass(frozen=True)
class Caps:
    quick_cap: int = 10
    detail_total_cap: int = 15
    detail_extra: int = 5


@dataclass(frozen=True)
class Ask:
    slot: str


@dataclass(frozen=True)
class Stop:
    reason: str  # complete, cap, detail_quota or exhausted


Plan = Ask | Stop


def blocking_slots_for(state: SessionState) -> list[SlotDef]:
    return [s for s in SLOT_LIST if s.blocks and state.is_applicable(s)]


def tier_a_complete(state: SessionState) -> bool:
    return all(state.confidence(s.key) >= COMPLETION_CONFIDENCE for s in blocking_slots_for(state))


def plan_next(state: SessionState, caps: Caps) -> Plan:
    if state.mode == "quick":
        if state.questions_asked >= caps.quick_cap:
            return Stop("cap")
        if tier_a_complete(state) and len(state.topics_asked() & STOP_TOPICS) >= MIN_TOPICS:
            return Stop("complete")
        weights = QUICK_WEIGHTS
    else:
        if state.questions_asked >= caps.detail_total_cap:
            return Stop("cap")
        started = state.detail_started_at or 0
        if state.questions_asked - started >= caps.detail_extra:
            return Stop("detail_quota")
        weights = DETAIL_WEIGHTS

    best: SlotDef | None = None
    best_score = 0.0
    for slot in SLOT_LIST:
        weight = weights.get(slot.tier)
        if not weight or not state.is_applicable(slot):
            continue
        contradicted = slot.key in state.contradictions
        confidence = state.confidence(slot.key)
        if contradicted:
            confidence = min(confidence, CONTRADICTED_CONFIDENCE)
        if (
            state.mode == "detail"
            and slot.tier in (Tier.A, Tier.B)
            and confidence >= COMPLETION_CONFIDENCE
        ):
            continue
        asked = state.asked_slots.get(slot.key, 0)
        if asked and confidence >= REVISIT_BELOW:
            continue
        if asked >= MAX_ASKS_PER_SLOT:
            continue
        score = weight * slot.importance * (1.0 - confidence)
        if score > best_score:
            best, best_score = slot, score
    if best is None:
        return Stop("exhausted")
    return Ask(best.key)
