"""Chooses the next slot to ask about, or says when to stop (spec sections 5.2 and 5.5).

Plain code, no model. Deterministic: ties resolve by registry order.
"""

import hashlib
from dataclasses import dataclass

from icp_chatbot.domain.slots import STOP_TOPICS, SlotDef, Tier
from icp_chatbot.domain.state import SessionState

COMPLETION_CONFIDENCE = 0.7  # a slot at or above this counts as known
REVISIT_BELOW = 0.4  # an asked slot is asked again only below this, or if contradicted
CONTRADICTED_CONFIDENCE = 0.2  # a contradicted slot is treated as barely known
MAX_ASKS_PER_SLOT = 2  # guard against loops
MIN_TOPICS = 3  # quick mode needs at least this many tier B topics asked

QUICK_WEIGHTS = {Tier.A: 1.0, Tier.B: 0.5}
DETAIL_WEIGHTS = {Tier.D: 1.0, Tier.A: 0.6, Tier.B: 0.4}


OWN_FIELD_BOOST = 1.3
LATE_SLOTS = frozenset({"disqualifiers"})  # too abrupt as an early question
LATE_AFTER = 5  # questions asked before a late slot is welcome
LATE_FACTOR = 0.3
JITTER_LOW, JITTER_SPAN = 0.6, 0.8  # a session's own shuffle scales each slot by 0.6 to 1.4


def order_jitter(state: SessionState, slot: SlotDef) -> float:
    """Stable per session and slot, so each interview visits its topics in its own order."""
    if not state.order_seed or slot.section == "seller_context":
        return 1.0  # what the company is and does always comes first
    early = slot.key in LATE_SLOTS and state.questions_asked < LATE_AFTER
    digest = hashlib.sha256(f"{state.order_seed}:{slot.key}".encode()).digest()
    return (JITTER_LOW + JITTER_SPAN * digest[0] / 255) * (LATE_FACTOR if early else 1.0)


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
    return [
        s
        for s in state.slot_defs()
        if s.blocks and state.is_applicable(s) and s.key not in state.skipped
    ]


def tier_a_complete(state: SessionState) -> bool:
    return all(state.confidence(s.key) >= COMPLETION_CONFIDENCE for s in blocking_slots_for(state))


def _askable(state: SessionState, key: str) -> bool:
    asked = state.asked_slots.get(key, 0)
    if key in state.skipped or asked >= MAX_ASKS_PER_SLOT:
        return False
    return not asked or state.confidence(key) < REVISIT_BELOW


def waits_on_unknown(state: SessionState, slot: SlotDef) -> bool:
    """A slot that may not apply (company size, if the customers turn out to be people) waits
    until what decides that is clearly known, not guessed, as long as it can still be asked."""
    return any(
        state.confidence(key) < COMPLETION_CONFIDENCE and _askable(state, key)
        for key, _ in slot.skip_if
    )


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
    for slot in state.slot_defs():
        weight = weights.get(slot.tier)
        if not weight or not state.is_applicable(slot) or slot.key in state.skipped:
            continue
        if waits_on_unknown(state, slot):
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
        score = weight * slot.importance * (1.0 - confidence) * order_jitter(state, slot)
        if slot.section == "industry_specifics":
            score *= OWN_FIELD_BOOST  # what this industry needs beats generic topics
        if asked and slot.key == state.pending_slot:
            score *= 0.4  # just asked and not answered: other topics go first, then come back
        if score > best_score:
            best, best_score = slot, score
    if best is None:
        return Stop("exhausted")
    return Ask(best.key)
