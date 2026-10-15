"""Environment guard: fails early if the installed SDKs no longer expose what the design relies on.

These checks do not call any network service.
"""

import inspect

from google import genai
from google.genai import errors, types


def test_client_supports_both_backends():
    params = inspect.signature(genai.Client.__init__).parameters
    # Developer API uses api_key; Vertex AI uses vertexai/enterprise + project + location.
    for name in ("api_key", "vertexai", "enterprise", "project", "location", "http_options"):
        assert name in params, f"genai.Client no longer accepts {name!r}"


def test_generate_content_config_has_fields_the_adapter_uses():
    fields = types.GenerateContentConfig.model_fields
    for name in (
        "system_instruction",
        "temperature",
        "max_output_tokens",
        "response_mime_type",
        "response_schema",
        "thinking_config",
        "labels",
        "http_options",
    ):
        assert name in fields, f"GenerateContentConfig lost {name!r}"


def test_thinking_config_supports_level_and_budget():
    fields = types.ThinkingConfig.model_fields
    assert "thinking_level" in fields  # Gemini 3.x models
    assert "thinking_budget" in fields  # Gemini 2.5 models
    levels = {level.value for level in types.ThinkingLevel}
    assert {"MINIMAL", "LOW", "MEDIUM", "HIGH"} <= levels


def test_usage_metadata_has_token_fields_for_cost_accounting():
    fields = types.GenerateContentResponseUsageMetadata.model_fields
    for name in (
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "cached_content_token_count",
        "total_token_count",
    ):
        assert name in fields, f"usage metadata lost {name!r}"


def test_error_classes_for_retry_logic_exist():
    for name in ("APIError", "ClientError", "ServerError"):
        assert hasattr(errors, name)


def test_http_retry_options_exist():
    fields = types.HttpRetryOptions.model_fields
    for name in ("attempts", "initial_delay", "max_delay", "http_status_codes"):
        assert name in fields
