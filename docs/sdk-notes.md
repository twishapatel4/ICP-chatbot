# google-genai SDK notes (verified 2026-10-15)

Verified by installing `google-genai==2.31.0` and inspecting it (no network calls). These are
the facts the LLM adapter relies on; `tests/test_environment.py` fails if any of them change.

- `genai.Client(...)` accepts: `enterprise`, `vertexai`, `api_key`, `credentials`, `project`,
  `location`, `debug_config`, `http_options`.
  - Developer API: `genai.Client(api_key=...)`.
  - Vertex AI: Google's current docs show `enterprise=True, project=..., location=...`; the older
    `vertexai=True` flag is still accepted by this version. The adapter must pick the flag that the
    installed SDK accepts. Whether `location="global"` is accepted is checked when the first real
    Vertex call is made (Phase 0 task), not assumed.
- `types.GenerateContentConfig` fields used: `system_instruction`, `temperature`,
  `max_output_tokens`, `response_mime_type`, `response_schema`, `response_json_schema`, `labels`,
  `thinking_config`, `http_options`.
- `types.ThinkingConfig` fields: `include_thoughts`, `thinking_budget`, `thinking_level`.
  `types.ThinkingLevel` values: `MINIMAL`, `LOW`, `MEDIUM`, `HIGH`.
  Google's thinking page lists `thinking_level` per model: `gemini-3.8-flash` supports low, medium
  and high (medium default); `gemini-3.6-flash` and `gemini-3-flash-preview` also support minimal;
  `gemini-3.5-flash-lite` supports minimal to high (minimal default); `gemini-3-pro-preview` supports
  only low and high. Thinking tokens are billed as output, and `max_output_tokens` includes them.
  Level support for `gemini-3.1-flash-lite` and `gemini-2.5-pro` was not confirmed, so the adapter
  must treat an unsupported level as a configuration error surfaced in tests, not a silent default.
- Token accounting: `response.usage_metadata` has `prompt_token_count`, `candidates_token_count`,
  `thoughts_token_count`, `cached_content_token_count`, `total_token_count`.
- Errors: `google.genai.errors.APIError`, `ClientError`, `ServerError`.
- Retries: `types.HttpOptions(retry_options=types.HttpRetryOptions(attempts, initial_delay,
  max_delay, exp_base, jitter, http_status_codes))`; timeout is `HttpOptions.timeout`.
- Structured output: `response_mime_type="application/json"` with `response_schema=<pydantic model>`;
  parsed result is `response.parsed`.
- Request `labels` exist on the config. Whether Vertex request-response logs keep them is untested.
