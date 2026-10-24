from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.planner import (
    enough_for_icp,
    icp_floor_met,
    missing_core_slots,
)


def known(state, key, confidence=0.9, values=("x",)):
    state.slots[key] = SlotValue(
        values=list(values), confidence=confidence, source=SlotSource.STATED
    )
    return state


def test_an_empty_state_has_every_core_field_missing_and_is_not_enough():
    state = SessionState()
    assert missing_core_slots(state)
    assert not icp_floor_met(state) and not enough_for_icp(state)


def test_the_floor_needs_the_product_and_three_customer_facts():
    state = known(SessionState(), "product")
    assert not icp_floor_met(state)
    known(state, "industries")
    known(state, "buyer_roles")
    assert not icp_floor_met(state)
    known(state, "company_size")
    assert icp_floor_met(state)


def test_b2b_or_b2c_alone_is_not_a_customer_fact():
    state = known(known(known(SessionState(), "product"), "business_model"), "industries")
    assert not icp_floor_met(state)


def test_a_weakly_known_field_does_not_count_toward_the_floor():
    state = known(SessionState(), "product", confidence=0.3)
    known(state, "industries")
    known(state, "buyer_roles")
    assert not icp_floor_met(state)


def test_known_core_fields_are_no_longer_missing():
    state = SessionState()
    before = {s.key for s in missing_core_slots(state)}
    known(state, "product")
    assert {s.key for s in missing_core_slots(state)} == before - {"product"}


def test_without_the_interviewers_call_a_third_of_the_core_fields_is_enough():
    state = SessionState()
    for key in [s.key for s in missing_core_slots(state)][:4]:
        known(state, key)
    assert enough_for_icp(state)
    assert not enough_for_icp(SessionState())


def test_the_interviewers_call_decides_but_the_floor_still_applies():
    state = SessionState(agent_enough=True)
    assert not enough_for_icp(state)  # it said enough, but we know nothing
    known(known(known(known(state, "product"), "industries"), "buyer_roles"), "company_size")
    assert enough_for_icp(state)
    state.agent_enough = False
    assert not enough_for_icp(state)
