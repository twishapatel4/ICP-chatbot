from pathlib import Path

import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.state import SlotSource
from icp_chatbot.engine.extraction import SlotUpdate
from icp_chatbot.engine.interview import AgentTurn
from icp_chatbot.engine.questions import QuestionOutput
from icp_chatbot.eval.cases import (
    REQUIRED_TAGS,
    EvalCase,
    case_problems,
    check_launch_set,
    load_cases,
)
from icp_chatbot.eval.replay import CaseResult, replay_all, replay_case, score_document
from icp_chatbot.eval.report import Summary, compare, summarize
from icp_chatbot.icp.builder import ICPSynthesis
from icp_chatbot.icp.document import FieldEntry, ICPDocument, compute_completeness, group_by_section
from icp_chatbot.icp.scoring import default_scoring_model
from icp_chatbot.llm.fake import FakeLLM

SETTINGS = Settings(_env_file=None)
CASES_DIR = Path(__file__).parent.parent / "eval_cases"


def case(name="c", **kw):
    base = dict(name=name, tags=["clear_website"], signup={"company_name": "Acme"}, answers=["a"])
    return EvalCase(**{**base, **kw})


def doc(**slots):
    entries = {
        k: FieldEntry(values=v, confidence=0.9, source=SlotSource.STATED) for k, v in slots.items()
    }
    return ICPDocument(
        sections=group_by_section(entries),
        scoring_model=default_scoring_model({k: v for k, v in slots.items()}),
        completeness=compute_completeness(entries),
    )


# -- the shipped cases ------------------------------------------------------------------------------


def test_the_shipped_cases_load_are_valid_and_cover_every_required_tag():
    cases = load_cases(CASES_DIR)
    assert len(cases) >= 14
    assert len({c.name for c in cases}) == len(cases)
    for c in cases:
        assert case_problems(c) == [], c.name
    assert REQUIRED_TAGS <= {t for c in cases for t in c.tags}


def test_the_only_thing_missing_from_the_shipped_set_is_volume():
    problems = check_launch_set(load_cases(CASES_DIR))
    assert [p for p in problems if "cases; the launch set needs" not in p] == []


def test_check_launch_set_reports_every_kind_of_gap():
    cases = [case("dup"), case("dup", tags=["vague"]), case("bad", expected={"nope": ["x"]})]
    text = " | ".join(check_launch_set(cases))
    assert "only 3 cases" in text and "duplicate case name dup" in text
    assert "no case is tagged hostile" in text and "unknown slot nope" in text
    assert case_problems(case(answers=[])) == ["no answers"]


# -- scoring a document -----------------------------------------------------------------------------


def test_score_document_measures_precision_recall_and_forbidden_values():
    c = case(
        expected={"industries": ["banks"], "lead_geographies": ["Europe"]},
        forbidden={"product": ["pwned"]},
    )
    perfect = score_document(
        c, doc(industries=["Banks"], lead_geographies=["Europe"], buyer_roles=["CFO"])
    )
    assert perfect == (1.0, 1.0, 1.0, 0)  # the extra buyer_roles field is not penalized
    half = score_document(c, doc(industries=["Banks", "Retail"], lead_geographies=["Asia"]))
    precision, recall, f1, violations = half
    assert (precision, recall) == (1 / 3, 1 / 2) and violations == 0
    assert f1 == pytest.approx(2 * (1 / 3) * (1 / 2) / (1 / 3 + 1 / 2))
    assert score_document(c, doc(product=["pwned!"]))[3] == 1


def test_score_document_handles_cases_with_nothing_expected():
    empty = case(expected={})
    assert score_document(empty, doc())[:3] == (1.0, 1.0, 1.0)


# -- replaying a case ---------------------------------------------------------------------------------


def ask(say, *updates):
    return AgentTurn(say=say, action="ask", updates=list(updates))


def up(slot, values, confidence=0.9):
    return SlotUpdate(slot=slot, values=values, confidence=confidence)


async def test_replay_runs_a_case_end_to_end_and_reports_metrics():
    c = case(
        name="mechanics",
        answers=["We sell payroll to banks", "Banks in Europe", "CFOs"],
        expected={"industries": ["banks"], "lead_geographies": ["Europe"]},
    )
    llm = FakeLLM({
        "opener": [QuestionOutput(question="Hi!")],
        "interviewer": [ask("Who are your best customers?", up("product", ["Payroll"])), ask("What problems do they have?", up("industries", ["Banks"]), up("lead_geographies", ["Europe"])), ask("Anything else?", up("buyer_roles", ["CFO"]))],
        "final_icp": [ICPSynthesis()],
    })  # fmt: skip
    result = await replay_case(c, llm, SETTINGS)
    assert (
        result.errors == []
        and result.status == "completed"
        and result.stop_reason == "user_finished"
    )
    assert (result.precision, result.recall, result.f1, result.violations) == (1.0, 1.0, 1.0, 0)
    assert result.scoring_valid and result.questions_asked == 4


async def test_replay_with_a_detail_phase_asks_the_extra_questions():
    c = case(
        name="detail",
        answers=["a", "b"],
        detail_answers=["trigger answer"],
        expected={"buying_triggers": ["new regulation"]},
    )
    llm = FakeLLM({
        "opener": [QuestionOutput(question="Hi!")],
        "interviewer": [ask("best?"), ask("q1?"), ask("detail q?"), ask("more?", up("buying_triggers", ["new regulation"]))],
        "final_icp": [ICPSynthesis()],
    })  # fmt: skip
    result = await replay_case(c, llm, SETTINGS)
    assert result.errors == [] and result.recall == 1.0


async def test_a_case_that_breaks_is_reported_without_stopping_the_run():
    broken = case(name="broken")  # the fake has no scripted interviewer replies, so the engine fails
    result = await replay_case(broken, FakeLLM({}), SETTINGS)
    assert result.status == "error" and result.errors and result.f1 == 0.0


async def test_replay_all_runs_every_case():
    llm = FakeLLM({})
    results = await replay_all([case("one"), case("two")], llm, SETTINGS)
    assert [r.name for r in results] == ["one", "two"]


# -- summaries and the release gate ---------------------------------------------------------------------


def result(
    name="r",
    f1=0.8,
    status="completed",
    questions=8,
    cost=0.1,
    valid=True,
    violations=0,
    tags=("clear_website",),
):
    return CaseResult(
        name, list(tags), status, None, questions, f1, f1, f1, violations, valid, cost
    )


def test_summarize_aggregates_metrics_and_groups_f1_by_tag():
    s = summarize(
        [result(f1=1.0, tags=("a", "b")), result(f1=0.5, tags=("a",), status="error", valid=False)]
    )
    assert s.cases == 2 and s.mean_f1 == 0.75 and s.completion_rate == 0.5
    assert s.scoring_valid_rate == 0.5 and s.errors == 1
    assert s.f1_by_tag == {"a": 0.75, "b": 1.0}
    assert summarize([]).cases == 0


def base_summary(**kw):
    values = dict(cases=10, mean_f1=0.70, completion_rate=0.9, scoring_valid_rate=1.0, mean_questions=9.0,
                  mean_cost_usd=0.10, violations=0, errors=0, f1_by_tag={"vague": 0.5, "clear_website": 0.9})  # fmt: skip
    return Summary(**{**values, **kw})


def test_a_better_candidate_passes():
    verdict = compare(base_summary(), base_summary(mean_f1=0.78))
    assert verdict.passed and verdict.reasons == []


@pytest.mark.parametrize(
    "change,fragment",
    [
        ({"mean_f1": 0.70}, "did not improve"),
        ({"violations": 1, "mean_f1": 0.8}, "forbidden"),
        ({"errors": 1, "mean_f1": 0.8}, "failed to run"),
        ({"completion_rate": 0.8, "mean_f1": 0.8}, "completion_rate regressed"),
        ({"mean_questions": 10.0, "mean_f1": 0.8}, "mean_questions regressed"),
        ({"mean_cost_usd": 0.2, "mean_f1": 0.8}, "mean_cost_usd regressed"),
        (
            {"f1_by_tag": {"vague": 0.3, "clear_website": 0.95}, "mean_f1": 0.8},
            "'vague' cases regressed",
        ),
    ],
)
def test_a_candidate_is_rejected_for_each_kind_of_regression(change, fragment):
    verdict = compare(base_summary(), base_summary(**change))
    assert not verdict.passed and any(fragment in r for r in verdict.reasons), verdict.reasons


def test_a_small_regression_within_the_margin_is_tolerated():
    verdict = compare(base_summary(), base_summary(mean_f1=0.8, completion_rate=0.89))
    assert verdict.passed


def test_comparing_on_a_lower_is_better_target():
    verdict = compare(base_summary(), base_summary(mean_questions=8.0), target="mean_questions")
    assert verdict.passed
    with pytest.raises(ValueError):
        compare(base_summary(), base_summary(), target="nonsense")


# -- HR cases and the step 2 flow -------------------------------------------------------------------

HR_CASES = ("hr_fresher", "hr_experienced", "hr_both", "hr_vague")


def _shipped(name):
    return next(c for c in load_cases(CASES_DIR) if c.name == name)


def test_hr_cases_load_and_use_hr_slots():
    from icp_chatbot.domain.slots import slot_map
    from icp_chatbot.engine.profile_form import validate_profile

    cases = {c.name: c for c in load_cases(CASES_DIR)}
    assert set(HR_CASES) <= set(cases)
    for name in HR_CASES:
        c = cases[name]
        assert c.track == "hr", name
        assert case_problems(c) == [], name
        assert c.profile_answers is not None, name
        assert validate_profile("hr", c.profile_answers) == [], name  # the form would be accepted
        assert set(c.profile_answers) <= set(slot_map("hr")), name
        assert c.expected or c.forbidden, name  # a vague case may only forbid inventions
    levels = {n: cases[n].profile_answers["experience_level"] for n in HR_CASES}
    assert levels == {
        "hr_fresher": ["Fresher"],
        "hr_experienced": ["Experienced"],
        "hr_both": ["Both"],
        "hr_vague": ["Both"],
    }
    assert "years_experience" not in cases["hr_fresher"].profile_answers
    assert "years_experience" in cases["hr_both"].profile_answers


def test_hr_expectations_list_every_form_value_of_the_slots_they_score():
    # score_document judges precision over every value in an expected slot, so a form value
    # left out of `expected` (a city, say) would count as a wrong extra value
    for name in HR_CASES:
        c = _shipped(name)
        for slot, wanted in c.expected.items():
            given = c.profile_answers.get(slot)
            if given and given != ["Any"]:
                assert set(given) <= set(wanted), (name, slot)
    assert _shipped("hr_vague").expected  # not F1 1.0 by construction


def test_required_tags_still_covered():
    cases = load_cases(CASES_DIR)
    assert REQUIRED_TAGS <= {t for c in cases for t in c.tags}
    assert [p for p in check_launch_set(cases) if "cases; the launch set needs" not in p] == []
    assert {c.track for c in cases} == {"sales", "hr"}


def test_b2c_retail_stays_chat_only_and_has_no_form_version():
    chat = _shipped("b2c_retail")
    assert chat.profile_answers is None and "b2c" in chat.tags  # like for like with old runs
    # the Sales form no longer asks the business model, so B2C can only come from the chat
    assert not (CASES_DIR / "b2c_retail_form.json").exists()
    assert all(
        "business_model" not in (c.profile_answers or {}) for c in load_cases(CASES_DIR)
    )


def test_hr_eval_cases_do_not_carry_skills_in_profile_answers():
    hr_cases = [c for c in load_cases(CASES_DIR) if c.track == "hr"]
    assert hr_cases
    for c in hr_cases:
        for slot in ("required_skills", "candidate_locations"):  # asked in the chat now
            assert slot not in (c.profile_answers or {}), (c.name, slot)
        assert case_problems(c) == [], c.name
        # a location expectation must be stated literally in the scripted chat: the matcher
        # does not equate "UK" with "United Kingdom"
        said = " ".join(c.answers).lower()
        for value in c.expected.get("candidate_locations", []):
            assert value.lower() in said, (c.name, value)


def test_case_problems_flags_a_bad_step_two_form():
    ok = {
        "industries": ["Any"],
        "lead_geographies": ["United Kingdom"],
        "company_hq_geographies": ["Any"],
        "buyer_roles": ["VP Sales"],
    }
    assert case_problems(case(profile_answers=ok)) == []
    unknown = case_problems(case(profile_answers={**ok, "nope": ["x"]}))
    assert any("unknown slot nope" in p for p in unknown)
    for removed in ("business_model", "company_size"):  # real slots, but not asked by the form
        flagged = case_problems(case(profile_answers={**ok, removed: ["B2B"]}))
        assert any(f"unknown slot {removed}" in p for p in flagged)
    incomplete = case_problems(case(profile_answers={"industries": ["Any"]}))
    assert any("lead_geographies" in p for p in incomplete)
    unresolved = case_problems(
        case(profile_answers={**ok, "lead_geographies": ["Narnia"]})
    )
    assert any("lead_geographies" in p for p in unresolved)


async def test_replay_runs_an_hr_case_through_its_track_and_the_step_two_form():
    c = _shipped("hr_fresher")
    llm = FakeLLM({
        "opener": [QuestionOutput(question="Hi!")],
        "interviewer": [ask("Any internships?", up("work_mode", ["Hybrid"])), ask("More?")],
        "final_icp": [ICPSynthesis()],
    })  # fmt: skip
    result = await replay_case(c.model_copy(update={"answers": ["a", "b"]}), llm, SETTINGS)
    assert result.errors == [] and result.status == "completed"
    assert result.scoring_valid  # judged against the HR registry, not the Sales one
    assert result.recall > 0  # the form answers reached the document as stated slots


async def test_a_case_without_step_two_answers_still_uses_the_legacy_start():
    c = case(name="legacy", answers=["a"])
    assert c.profile_answers is None
    llm = FakeLLM({
        "opener": [QuestionOutput(question="Hi!")],
        "interviewer": [ask("q?")],
        "final_icp": [ICPSynthesis()],
    })  # fmt: skip
    assert (await replay_case(c, llm, SETTINGS)).errors == []
