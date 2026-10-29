import pytest

from icp_chatbot.icp.review import PriorityEdit, apply_edit
from icp_chatbot.icp.scoring import (
    PriorityAnswer,
    default_scoring_model,
    priority_questions,
    weights_from_priorities,
)
from tests.test_review import TODAY, make_doc

VALUES = {
    "industries": ["Fintech"],
    "company_size": ["50-500"],
    "lead_geographies": ["Europe"],
    "buyer_roles": ["CFO"],
    "company_type": ["B2B"],
    "buying_triggers": ["new funding"],
}


def model():
    return default_scoring_model(VALUES)


def answers(value):
    return [PriorityAnswer(a=q.a, b=q.b, value=value) for q in priority_questions(model())]


def test_only_questions_about_criteria_the_icp_has_values_for_are_asked():
    pairs = {(q.a, q.b) for q in priority_questions(model())}
    assert ("industry_fit", "geographic_fit") in pairs
    sparse = default_scoring_model({"industries": ["Fintech"], "lead_geographies": ["UK"]})
    assert [(q.a, q.b) for q in priority_questions(sparse)] == [("industry_fit", "geographic_fit")]


def test_a_question_shows_what_the_icp_looks_for():
    q = next(q for q in priority_questions(model()) if q.a == "buyer_fit" and q.b == "company_size")
    assert q.a_looks == "CFO" and q.b_looks == "50-500"


def test_weights_are_whole_numbers_that_sum_to_100():
    w = weights_from_priorities(model(), answers(1))
    assert sum(w.values()) == 100 and all(v == int(v) for v in w.values())


def test_equal_answers_keep_the_asked_criteria_even_and_unasked_ones_untouched():
    m = model()
    w = weights_from_priorities(m, answers(0))
    defaults = {c.key: c.weight for c in m.criteria}
    for key in ("company_age_fit", "seniority_fit", "department_fit"):  # in no question
        assert w[key] == defaults[key]
    asked = {"industry_fit", "geographic_fit", "company_type_fit", "buying_signals", "buyer_fit", "company_size"}
    assert max(w[k] for k in asked) - min(w[k] for k in asked) <= 1


def test_the_preferred_side_gets_more_weight():
    qs = priority_questions(model())
    base = weights_from_priorities(model(), [PriorityAnswer(a=q.a, b=q.b, value=0) for q in qs])
    first = qs[0]
    pushed = weights_from_priorities(
        model(), [PriorityAnswer(a=q.a, b=q.b, value=2 if q is first else 0) for q in qs]
    )
    assert pushed[first.a] > base[first.a] and pushed[first.b] < base[first.b]


def test_an_unknown_pair_is_rejected():
    with pytest.raises(ValueError):
        weights_from_priorities(model(), [PriorityAnswer(a="industry_fit", b="buyer_fit", value=1)])


def test_an_answer_outside_the_scale_is_rejected():
    with pytest.raises(ValueError):
        PriorityAnswer(a="industry_fit", b="geographic_fit", value=3)


def test_a_priority_edit_sets_the_documents_weights_and_records_the_changes():
    doc = make_doc()
    qs = priority_questions(doc.scoring_model, doc.track)
    edit = PriorityEdit(answers=[PriorityAnswer(a=q.a, b=q.b, value=2) for q in qs])
    new, specs = apply_edit(doc, edit, TODAY)
    assert sum(c.weight for c in new.scoring_model.criteria) == 100
    assert specs and all(s.type == "weight_edit" for s in specs)
