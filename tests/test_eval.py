from pathlib import Path

import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.state import SlotSource
from icp_chatbot.engine.extraction import ExtractionResult, Flags, SlotUpdate
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
        expected={"industries": ["banks"], "geographies": ["Europe"]},
        forbidden={"product": ["pwned"]},
    )
    perfect = score_document(
        c, doc(industries=["Banks"], geographies=["Europe"], pain_points=["x"])
    )
    assert perfect == (1.0, 1.0, 1.0, 0)  # the extra pain_points field is not penalized
    half = score_document(c, doc(industries=["Banks", "Retail"], geographies=["Asia"]))
    precision, recall, f1, violations = half
    assert (precision, recall) == (1 / 3, 1 / 2) and violations == 0
    assert f1 == pytest.approx(2 * (1 / 3) * (1 / 2) / (1 / 3 + 1 / 2))
    assert score_document(c, doc(product=["pwned!"]))[3] == 1


def test_score_document_handles_cases_with_nothing_expected():
    empty = case(expected={})
    assert score_document(empty, doc())[:3] == (1.0, 1.0, 1.0)


# -- replaying a case ---------------------------------------------------------------------------------


def ext(*updates, flags=None):
    return ExtractionResult(updates=list(updates), flags=flags or Flags())


def up(slot, values, confidence=0.9):
    return SlotUpdate(slot=slot, values=values, confidence=confidence)


async def test_replay_runs_a_case_end_to_end_and_reports_metrics():
    c = case(
        name="mechanics",
        answers=["We sell payroll to banks", "Banks in Europe", "CFOs"],
        expected={"industries": ["banks"], "geographies": ["Europe"]},
    )
    llm = FakeLLM({
        "extraction": [ext(up("product", ["Payroll"])), ext(up("industries", ["Banks"]), up("geographies", ["Europe"])), ext(up("buyer_roles", ["CFO"]))],
        "question": [QuestionOutput(question="What problems do they have?"), QuestionOutput(question="Anything else?")],
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
        detail_answers=["compliance answer"],
        expected={"compliance": ["GDPR"]},
    )
    llm = FakeLLM({
        "extraction": [ext(), ext(), ext(up("compliance", ["GDPR"]))],
        "question": [QuestionOutput(question="q1?"), QuestionOutput(question="detail q?"), QuestionOutput(question="more?")],
        "final_icp": [ICPSynthesis()],
    })  # fmt: skip
    result = await replay_case(c, llm, SETTINGS)
    assert result.errors == [] and result.recall == 1.0


async def test_a_case_that_breaks_is_reported_without_stopping_the_run():
    broken = case(name="broken")  # the fake has no scripted extraction, so the engine fails
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
