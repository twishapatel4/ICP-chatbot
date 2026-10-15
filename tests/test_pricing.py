from datetime import date
from types import SimpleNamespace

import pytest

from icp_chatbot.llm.pricing import UnknownModelError, cost_usd, price_for
from icp_chatbot.llm.types import Usage


def test_cost_of_one_million_input_and_output_tokens():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("gemini-3.1-flash-lite", usage, on=date(2026, 10, 15)) == pytest.approx(1.75)


def test_thinking_tokens_are_billed_as_output():
    usage = Usage(output_tokens=500_000, thought_tokens=500_000)
    assert cost_usd("gemini-2.5-pro", usage) == pytest.approx(10.0)


def test_cached_tokens_use_the_cached_rate_and_are_not_billed_twice():
    usage = Usage(input_tokens=1_000_000, cached_tokens=600_000)
    # 400k uncached at 1.25 + 600k cached at 0.125
    assert cost_usd("gemini-2.5-pro", usage) == pytest.approx(0.5 + 0.075)


def test_gemini_3_8_flash_price_changes_on_1_jan_2027():
    assert price_for("gemini-3.8-flash", date(2026, 12, 31)).input == 0.75
    assert price_for("gemini-3.8-flash", date(2027, 1, 1)).input == 1.50
    assert price_for("gemini-3.8-flash", date(2027, 1, 1)).output == 7.50


def test_model_names_with_a_models_prefix_are_accepted():
    assert price_for("models/gemini-2.5-pro").output == 10.0


def test_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        cost_usd("gemini-9-ultra", Usage(input_tokens=1))


def test_usage_from_metadata_tolerates_none_and_missing_fields():
    assert Usage.from_metadata(None) == Usage()
    meta = SimpleNamespace(
        prompt_token_count=100,
        candidates_token_count=20,
        thoughts_token_count=None,
        cached_content_token_count=40,
    )
    usage = Usage.from_metadata(meta)
    assert usage == Usage(input_tokens=100, output_tokens=20, thought_tokens=0, cached_tokens=40)
    assert (usage + usage).input_tokens == 200
    assert usage.total_tokens == 120
