import pytest

from icp_chatbot.config import Settings
from icp_chatbot.llm.adapter import thinking_config
from icp_chatbot.versions import STEP_NAMES, ConfigVersionData, default_config


@pytest.fixture
def config():
    return default_config(Settings(_env_file=None))


def test_every_step_has_a_prompt_and_settings(config):
    for step in STEP_NAMES:
        assert config.prompt(step).strip()
        assert config.step(step).model


def test_models_come_from_settings():
    config = default_config(Settings(_env_file=None, model_question="gemini-3.5-flash"))
    assert config.step("question").model == "gemini-3.5-flash"
    assert config.step("opener").model == "gemini-3.5-flash"


def test_thinking_settings_are_valid_for_each_models_family(config):
    for step in STEP_NAMES:
        thinking_config(config.step(step))  # raises LLMError("config") if the combination is bad


def test_interviewer_system_prompt_includes_the_slot_catalog(config):
    system = config.system_prompt("interviewer")
    assert "- industries:" in system and "- buyer_roles:" in system
    assert config.system_prompt("question") == config.prompt("question")


def test_config_round_trips_through_json(config):
    restored = ConfigVersionData.model_validate_json(config.model_dump_json())
    assert restored == config


def test_unknown_step_raises(config):
    with pytest.raises(KeyError):
        config.step("nope")
