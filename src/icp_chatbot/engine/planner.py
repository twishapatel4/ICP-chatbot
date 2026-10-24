"""Question limits and the "enough to build an ICP" check (spec sections 5.2 and 5.5).

Plain code, no model. The interviewer decides what to ask and when it has enough; this module
holds the caps and the floor under that decision."""

from dataclasses import dataclass

from icp_chatbot.domain.slots import SlotDef
from icp_chatbot.domain.state import SessionState

COMPLETION_CONFIDENCE = 0.7  # a slot at or above this counts as known


@dataclass(frozen=True)
class Caps:
    quick_cap: int = 20
    detail_total_cap: int = 25
    detail_extra: int = 5


MIN_KNOWN_SHARE = 0.3  # below this share of core fields known, there is too little to build an ICP


def missing_core_slots(state: SessionState) -> list[SlotDef]:
    """Core fields still unknown, including ones the user declined to answer."""
    return [
        s
        for s in state.slot_defs()
        if s.blocks and state.is_applicable(s) and state.confidence(s.key) < COMPLETION_CONFIDENCE
    ]


FLOOR_CONFIDENCE = 0.5
CUSTOMER_KEYS = ("customer_profile", "industries", "company_size", "geographies", "pain_points",
                 "buyer_roles", "buyer_departments", "desired_outcomes")  # fmt: skip
FLOOR_CUSTOMER_FACTS = 3  # what they sell plus this many facts about who buys it


def icp_floor_met(state: SessionState) -> bool:
    """A sanity floor under the interviewer's own call: we know what they sell and at least
    three things about who buys it (B2B or B2C alone does not count)."""
    known = [k for k in CUSTOMER_KEYS if state.confidence(k) >= FLOOR_CONFIDENCE]
    return state.confidence("product") >= FLOOR_CONFIDENCE and len(known) >= FLOOR_CUSTOMER_FACTS


def enough_for_icp(state: SessionState) -> bool:
    if state.agent_enough is not None:  # the model-led interview judged it itself
        return state.agent_enough and icp_floor_met(state)
    core = [s for s in state.slot_defs() if s.blocks and state.is_applicable(s)]
    if not core:
        return True
    return 1 - len(missing_core_slots(state)) / len(core) >= MIN_KNOWN_SHARE
