import pytest

from icp_chatbot.icp.review import PriorityEdit, apply_edit
from icp_chatbot.icp.scoring import (
    PriorityAnswer,
    default_scoring_model,
    priority_questions,
    weights_from_priorities,
)
from tests.test_review import TODAY, make_doc

RICH = {
    "industries": ["Fintech"],
    "company_size": ["50-500"],
    "lead_geographies": ["Europe"],
    "buyer_roles": ["CFO"],
    "company_type": ["B2B"],
    "buying_triggers": ["new funding"],
    "years_in_operation": ["5-20"],
    "buyer_seniority": ["VP"],
    "buyer_departments": ["Finance"],
}
THIN = {"industries": ["Fintech"], "company_size": ["50-500"], "buyer_roles": ["CFO"]}


def answers(model, value=0):
    return [PriorityAnswer(a=q.a, b=q.b, value=value) for q in priority_questions(model)]


def applicable(model):
    return {c.key for c in model.criteria if c.applicable}


@pytest.mark.parametrize("values", [RICH, THIN, {"industries": ["Fintech"], "buyer_roles": ["CFO"]}])
def test_every_factor_the_icp_has_is_asked_about(values):
    m = default_scoring_model(values)
    asked = {k for q in priority_questions(m) for k in (q.a, q.b)}
    assert asked == applicable(m)


def test_the_number_of_questions_follows_the_icp_and_stays_small():
    counts = [len(priority_questions(default_scoring_model(v))) for v in (THIN, RICH)]
    assert counts[0] < counts[1] <= 6


def test_one_or_no_factor_means_no_questions():
    assert priority_questions(default_scoring_model({"industries": ["Fintech"]})) == []
    assert priority_questions(default_scoring_model({})) == []


def test_a_question_shows_what_the_icp_looks_for():
    q = next(q for q in priority_questions(default_scoring_model(THIN)) if "buyer_fit" in (q.a, q.b))
    looks = {q.a: q.a_looks, q.b: q.b_looks}
    assert looks["buyer_fit"] == "CFO"


@pytest.mark.parametrize("values", [RICH, THIN])
def test_weights_are_whole_numbers_summing_to_100_over_the_factors_that_apply(values):
    m = default_scoring_model(values)
    w = weights_from_priorities(m, answers(m, 1))
    assert sum(w.values()) == 100 and all(v == int(v) for v in w.values())
    assert all(w[c.key] == 0 for c in m.criteria if not c.applicable)


def test_the_preferred_side_gets_more_weight():
    m = default_scoring_model(RICH)
    qs = priority_questions(m)
    base = weights_from_priorities(m, answers(m))
    pushed = weights_from_priorities(
        m, [PriorityAnswer(a=q.a, b=q.b, value=2 if i == 0 else 0) for i, q in enumerate(qs)]
    )
    assert pushed[qs[0].a] > base[qs[0].a] and pushed[qs[0].b] < base[qs[0].b]


def test_an_unknown_pair_is_rejected():
    m = default_scoring_model(THIN)
    with pytest.raises(ValueError):
        weights_from_priorities(m, [PriorityAnswer(a="industry_fit", b="geographic_fit", value=1)])


def test_an_answer_outside_the_scale_is_rejected():
    with pytest.raises(ValueError):
        PriorityAnswer(a="industry_fit", b="geographic_fit", value=3)


def test_a_priority_edit_sets_the_documents_weights_and_records_the_changes():
    doc = make_doc()
    qs = priority_questions(doc.scoring_model, doc.track)
    new, specs = apply_edit(doc, PriorityEdit(answers=[PriorityAnswer(a=q.a, b=q.b, value=2) for q in qs]), TODAY)
    assert sum(c.weight for c in new.scoring_model.criteria) == 100
    assert specs and all(s.type == "weight_edit" for s in specs)


def test_the_threshold_defaults_to_70_and_can_be_changed():
    from icp_chatbot.icp.review import ThresholdEdit

    doc = make_doc()
    assert doc.scoring_model.threshold == 70
    new, specs = apply_edit(doc, ThresholdEdit(threshold=55), TODAY)
    assert new.scoring_model.threshold == 55
    assert [(s.type, s.old, s.new) for s in specs] == [("threshold_edit", 70, 55)]
    with pytest.raises(ValueError):
        apply_edit(doc, ThresholdEdit(threshold=120), TODAY)


def test_a_lead_meets_the_threshold_only_at_or_above_it():
    from icp_chatbot.icp.scoring import score_lead, set_threshold

    m = default_scoring_model(THIN)
    lead = {"industries": ["Fintech"], "company_size": ["50"], "buyer_roles": ["CFO"]}
    assert score_lead(m, lead).meets_threshold
    assert not score_lead(set_threshold(m, 100), {**lead, "industries": ["Retail"]}).meets_threshold
