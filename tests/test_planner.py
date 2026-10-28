from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.planner import (
    PROGRESS_CAP,
    enough_for_icp,
    icp_floor_met,
    interview_progress,
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


def test_the_product_described_on_their_website_counts_once_they_have_seen_it():
    state = SessionState(answers_received=1)
    for key in ("industries", "buyer_roles", "company_size"):
        known(state, key)
    state.slots["product"] = SlotValue(values=["LMS"], confidence=0.5, source=SlotSource.INFERRED)
    assert not icp_floor_met(state)  # one reply is too early to say they accepted it
    state.answers_received = 2
    assert icp_floor_met(state)
    state.slots["industries"] = SlotValue(values=["x"], confidence=0.5, source=SlotSource.INFERRED)
    assert not icp_floor_met(state)  # a guessed customer fact still never counts


# the progress bar


def test_an_empty_session_has_no_progress():
    progress = interview_progress(SessionState())
    assert progress.percent == 0 and progress.known == 0 and progress.total > 0


def test_progress_rises_as_must_ask_fields_become_known():
    state = SessionState()
    known(state, "product")
    first = interview_progress(state)
    known(state, "customer_profile")
    known(state, "industries")
    second = interview_progress(state)
    assert 0 < first.percent < second.percent
    assert (first.known, second.known) == (1, 3)


def test_a_guess_counts_part_way_and_a_confirmed_answer_counts_fully():
    guessed = known(SessionState(), "product", confidence=0.35)
    confirmed = known(SessionState(), "product", confidence=0.9)
    assert 0 < interview_progress(guessed).percent < interview_progress(confirmed).percent
    assert interview_progress(guessed).known == 0


def test_a_declined_field_counts_as_settled():
    state = SessionState()
    before = interview_progress(state)
    state.skipped.append("industries")
    after = interview_progress(state)
    assert after.percent > before.percent and after.known == before.known + 1


def test_progress_waits_below_the_cap_until_the_interview_is_enough():
    state = SessionState()
    for slot in state.slot_defs():
        if state.is_applicable(slot):
            known(state, slot.key)
    state.agent_enough = False
    assert interview_progress(state).percent == PROGRESS_CAP
    state.agent_enough = True
    assert interview_progress(state).percent == 100
