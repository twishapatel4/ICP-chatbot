"""Question limits and the "enough to build an ICP" check (spec sections 5.2 and 5.5).

Plain code, no model. The interviewer decides what to ask and when it has enough; this module
holds the caps and the floor under that decision."""

from dataclasses import dataclass

from icp_chatbot.domain.slots import SlotDef, Tier
from icp_chatbot.domain.state import SessionState, SlotSource

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
PRODUCT_SEEN_AFTER = 2  # replies after which a website description of the product counts
FLOOR_CUSTOMER_FACTS = 3  # what they sell plus this many facts about who buys it


def _told(state: SessionState, key: str) -> bool:
    """The user gave or confirmed this: a guess from their website does not count."""
    value = state.slots.get(key)
    if not value or value.confidence < FLOOR_CONFIDENCE:
        return False
    if value.source != SlotSource.INFERRED:
        return True
    # what they sell was put to them in the opener ("is that right?"): once they have answered a
    # couple of times without objecting, the description from their own website stands
    return key == "product" and state.answers_received >= PRODUCT_SEEN_AFTER


def icp_floor_met(state: SessionState) -> bool:
    """A sanity floor under the interviewer's own call: the user told us what they sell and at
    least three things about who buys it (B2B or B2C alone, or a guess from their website, does
    not count)."""
    known = [k for k in CUSTOMER_KEYS if _told(state, k)]
    return _told(state, "product") and len(known) >= FLOOR_CUSTOMER_FACTS


def enough_for_icp(state: SessionState) -> bool:
    if state.agent_enough is not None:  # the model-led interview judged it itself
        return state.agent_enough and icp_floor_met(state)
    core = [s for s in state.slot_defs() if s.blocks and state.is_applicable(s)]
    if not core:
        return True
    return 1 - len(missing_core_slots(state)) / len(core) >= MIN_KNOWN_SHARE


TIER_A_WEIGHT = 0.7  # the must-ask fields drive the bar; the optional ones fill the rest
PROGRESS_CAP = 90  # the bar waits here until the interview is enough


@dataclass(frozen=True)
class Progress:
    percent: int
    known: int  # must-ask fields settled so far
    total: int  # must-ask fields that apply to this company


def _credit(state: SessionState, slot: SlotDef) -> float:
    """How far along a field is: a declined one is settled (it will not be asked again), a guess
    from the website counts part-way, a confirmed answer counts fully."""
    if slot.key in state.skipped:
        return 1.0
    return min(state.confidence(slot.key) / COMPLETION_CONFIDENCE, 1.0)


def interview_progress(state: SessionState) -> Progress:
    """How much of the ICP is gathered, for the progress bar. Importance-weighted, must-ask fields
    count 70% and optional ones 30%; held at PROGRESS_CAP until enough_for_icp says the interview
    could stop, so the bar never reads 100% while the bot is still asking. It only reflects what is
    gathered: finishing or building the profile does not move it."""

    def share(slots: list[SlotDef]) -> float:
        total = sum(s.importance for s in slots)
        if not total:
            return 1.0
        return sum(s.importance * _credit(state, s) for s in slots) / total

    applicable = [s for s in state.slot_defs() if state.is_applicable(s)]
    tier_a = [s for s in applicable if s.tier == Tier.A and s.blocks]
    tier_b = [s for s in applicable if s.tier == Tier.B]
    raw = TIER_A_WEIGHT * share(tier_a) + (1 - TIER_A_WEIGHT) * share(tier_b)
    percent = round(raw * 100)
    if not enough_for_icp(state):
        percent = min(percent, PROGRESS_CAP)
    known = sum(1 for s in tier_a if _credit(state, s) >= 1.0)
    return Progress(percent=min(percent, 100), known=known, total=len(tier_a))
