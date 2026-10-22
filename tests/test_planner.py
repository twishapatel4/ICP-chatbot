from icp_chatbot.domain.slots import SLOT_LIST
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.planner import (
    Ask,
    Caps,
    Stop,
    blocking_slots_for,
    plan_next,
    tier_a_complete,
)

CAPS = Caps(quick_cap=10, detail_total_cap=15, detail_extra=5)


def known(state, key, confidence=0.9, values=("x",), source=SlotSource.STATED):
    state.slots[key] = SlotValue(values=list(values), confidence=confidence, source=source)


def fill_tier_a(state):
    for slot in blocking_slots_for(state):
        known(state, slot.key)


def test_empty_state_asks_the_most_important_tier_a_slot_first():
    assert plan_next(SessionState(), CAPS) == Ask("product")  # what the company does comes first


def test_known_slots_are_not_asked_and_lower_confidence_is_asked_sooner():
    state = SessionState()
    fill_tier_a(state)
    state.slots["pain_points"] = SlotValue(
        values=["slow"], confidence=0.2, source=SlotSource.INFERRED
    )
    assert plan_next(state, CAPS) == Ask("pain_points")  # the only tier A slot below 0.7 of note


def test_buyer_slots_are_skipped_for_b2c_companies():
    state = SessionState()
    known(state, "business_model", values=("B2C",))
    fill_tier_a_except_buyers = [
        s for s in blocking_slots_for(state) if s.key not in ("buyer_roles", "buyer_departments")
    ]
    assert not any(s.key.startswith("buyer_") for s in blocking_slots_for(state))
    for slot in fill_tier_a_except_buyers:
        if slot.key != "business_model":
            known(state, slot.key)
    assert tier_a_complete(state)


def test_quick_mode_stops_complete_after_tier_a_and_three_tier_b_topics():
    state = SessionState(questions_asked=6)
    fill_tier_a(state)
    state.asked_slots = {"tech_stack": 1, "deal_size": 1}  # two topics: tools, commercial
    assert isinstance(plan_next(state, CAPS), Ask)  # still needs a third topic
    state.asked_slots["sales_cycle"] = 1  # third topic: process
    assert plan_next(state, CAPS) == Stop("complete")


def test_quick_mode_stops_at_the_cap_even_when_incomplete():
    assert plan_next(SessionState(questions_asked=10), CAPS) == Stop("cap")


def test_a_slot_already_asked_is_not_asked_again_unless_low_or_contradicted():
    state = SessionState(asked_slots={"industries": 1})
    fill_tier_a(state)  # everything known, so industries is the only candidate left to compare
    state.slots["industries"] = SlotValue(values=["x"], confidence=0.5, source=SlotSource.STATED)
    assert plan_next(state, CAPS) != Ask("industries")  # asked once, 0.5 is above the 0.4 floor
    state.slots["industries"] = SlotValue(values=["x"], confidence=0.3, source=SlotSource.STATED)
    assert plan_next(state, CAPS) == Ask("industries")  # below the floor: ask again
    state.slots["industries"] = SlotValue(values=["x"], confidence=0.8, source=SlotSource.STATED)
    state.contradictions = ["industries"]
    assert plan_next(state, CAPS) == Ask("industries")  # contradicted: ask again


def test_a_slot_is_never_asked_more_than_three_times():
    state = SessionState(asked_slots={"industries": 3})
    assert plan_next(state, CAPS) != Ask("industries")


def test_tier_c_slots_are_never_asked():
    asked = set()
    state = SessionState(questions_asked=0)
    for _ in range(60):
        plan = plan_next(state, Caps(quick_cap=100, detail_total_cap=100, detail_extra=100))
        if isinstance(plan, Stop):
            break
        asked.add(plan.slot)
        state.asked_slots[plan.slot] = state.asked_slots.get(plan.slot, 0) + 1
        known(state, plan.slot)
    tiers = {s.key: s.tier.value for s in SLOT_LIST}
    assert {tiers[k] for k in asked} <= {"A", "B"}


def test_detail_mode_prioritizes_tier_d_then_unfinished_slots():
    state = SessionState(mode="detail", questions_asked=8, detail_started_at=8)
    fill_tier_a(state)
    plan = plan_next(state, CAPS)
    assert isinstance(plan, Ask)
    assert plan.slot in {s.key for s in SLOT_LIST if s.tier.value == "D"}


def test_detail_mode_stops_after_five_more_questions_or_the_total_cap():
    state = SessionState(mode="detail", questions_asked=13, detail_started_at=8)
    assert plan_next(state, CAPS) == Stop("detail_quota")
    state = SessionState(mode="detail", questions_asked=15, detail_started_at=14)
    assert plan_next(state, CAPS) == Stop("cap")


def test_planning_is_deterministic():
    state = SessionState()
    assert [plan_next(state, CAPS) for _ in range(5)] == [plan_next(state, CAPS)] * 5


def test_exhausted_when_nothing_is_left_to_ask():
    state = SessionState()
    for slot in SLOT_LIST:
        state.asked_slots[slot.key] = 3
    assert plan_next(state, CAPS) == Stop("exhausted")


def test_a_slot_just_asked_and_not_answered_waits_behind_other_topics():
    state = SessionState(asked_slots={"pain_points": 1}, pending_slot="pain_points")
    assert plan_next(state, CAPS) != Ask("pain_points")


def test_a_session_seed_changes_the_order_but_the_same_seed_gives_the_same_order():
    from icp_chatbot.domain.state import SessionState
    from icp_chatbot.engine.planner import Ask, Caps, plan_next

    def order(seed):
        state = SessionState(order_seed=seed)
        out = []
        for _ in range(6):
            plan = plan_next(state, Caps())
            assert isinstance(plan, Ask)
            out.append(plan.slot)
            state.asked_slots[plan.slot] = 1
            state.pending_slot = plan.slot
        return out

    orders = {tuple(order(seed)) for seed in range(1, 12)}
    assert len(orders) > 3  # sessions do not all ask in the same sequence
    assert order(7) == order(7)
    assert order(0) == order(0)


def test_exclusions_are_not_asked_early_in_a_shuffled_session():
    from icp_chatbot.domain.state import SessionState
    from icp_chatbot.engine.planner import Ask, Caps, plan_next

    for seed in range(1, 40):
        state = SessionState(order_seed=seed, questions_asked=2)
        plan = plan_next(state, Caps())
        assert isinstance(plan, Ask) and plan.slot != "disqualifiers"
