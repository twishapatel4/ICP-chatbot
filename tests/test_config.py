import pytest
from pydantic import ValidationError

from icp_chatbot.config import Settings


def make(**env):
    # _env_file=None keeps a developer's local .env from leaking into tests.
    return Settings(_env_file=None, **env)


def test_defaults_match_the_spec():
    s = make()
    assert s.llm_backend == "developer"
    assert s.google_cloud_location == "global"  # decision D3
    assert s.model_extraction == "gemini-3.1-flash-lite"
    assert s.model_question == "gemini-3.8-flash"
    assert s.model_final_icp == "gemini-2.5-pro"  # decision D7: GA model for the final step
    assert s.quick_mode_question_cap == 10
    assert s.detail_mode_total_cap == 15
    assert s.detail_mode_extra_questions == 5
    assert s.max_answer_chars == 2000


def test_environment_variables_override_defaults(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("QUICK_MODE_QUESTION_CAP", "8")
    s = make()
    assert s.llm_backend == "vertex"
    assert s.quick_mode_question_cap == 8


def test_unknown_backend_is_rejected():
    with pytest.raises(ValidationError):
        make(llm_backend="openai")


def test_detail_cap_must_not_be_below_quick_cap():
    with pytest.raises(ValidationError):
        make(quick_mode_question_cap=12, detail_mode_total_cap=10)


@pytest.mark.parametrize("field", ["max_answer_chars", "quick_mode_question_cap"])
def test_non_positive_limits_are_rejected(field):
    with pytest.raises(ValidationError):
        make(**{field: 0})
