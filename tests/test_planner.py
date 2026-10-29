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


def test_without_the_interviewers_call_a_third_of_the_core_fields_and_the_floor_are_enough():
    state = SessionState()
    for key in ("business_model", "industries", "company_size", "lead_geographies"):
        known(state, key)  # what the step 2 cards give: well over a third of the core fields
    assert not enough_for_icp(state)  # but not what they sell
    known(state, "product")
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


def b2c(*keys):
    state = known(SessionState(), "business_model", values=("B2C",))
    for key in keys:
        known(state, key)
    return state


def test_b2c_floor_met_with_product_and_both_applicable_customer_keys():
    state = b2c("product", "customer_profile", "lead_geographies")
    assert icp_floor_met(state)


def test_b2c_floor_not_met_with_one_customer_key():
    assert not icp_floor_met(b2c("product", "customer_profile"))
    assert not icp_floor_met(b2c("customer_profile", "lead_geographies"))  # no product


def test_b2c_skipping_one_customer_key_still_reaches_the_floor_with_the_other():
    for skipped, other in (
        ("customer_profile", "lead_geographies"),
        ("lead_geographies", "customer_profile"),
    ):
        state = b2c("product", other)
        state.skipped.append(skipped)
        assert icp_floor_met(state), skipped
    state = b2c("lead_geographies")  # the anchor is still required
    state.skipped.append("customer_profile")
    assert not icp_floor_met(state)


def test_b2b_skips_lower_the_bar_only_by_what_cannot_be_asked():
    state = known(SessionState(), "business_model", values=("B2B",))
    known(known(known(state, "product"), "industries"), "buyer_roles")
    state.skipped.append("company_size")
    assert not icp_floor_met(state)  # four other keys remain applicable: three are still needed


def test_b2b_floor_unchanged_needs_three():
    for model in ("B2B", "Both"):
        state = known(SessionState(), "business_model", values=(model,))
        known(known(known(state, "product"), "industries"), "buyer_roles")
        assert not icp_floor_met(state), model
        known(state, "company_size")
        assert icp_floor_met(state), model


def test_hr_fresher_floor_ignores_experience_keys():
    state = known(SessionState(track="hr"), "experience_level", values=("Fresher",))
    known(known(known(state, "target_roles"), "target_functions"), "years_experience")
    known(state, "required_skills")
    assert not icp_floor_met(state)  # years_experience does not apply to freshers
    known(state, "candidate_locations")
    assert icp_floor_met(state)


def test_filled_b2c_fallback_form_is_not_partial():
    from icp_chatbot.engine.fallback import apply_form, fallback_form, missing_required

    state = SessionState()
    answers = {"business_model": ["B2C"]}
    state.slots["business_model"] = SlotValue(
        values=["B2C"], confidence=0.9, source=SlotSource.STATED
    )
    missing = missing_required(answers, state)
    answers.update({key: [f"value for {key}"] for key in missing})
    apply_form(state, answers, 1)
    required = {f.key for f in fallback_form("sales") if f.required}
    assert "product" in required and "customer_profile" in required
    assert enough_for_icp(state)
