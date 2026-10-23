"""Gemini adapter: one place that knows the SDK. Everything else depends on the LLM protocol."""

import asyncio
import logging
import time
from dataclasses import replace

from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from icp_chatbot.llm.pricing import UnknownModelError, cost_usd
from icp_chatbot.llm.types import LLMError, LLMResult, StepConfig, T, Usage

log = logging.getLogger(__name__)

_VALID_LEVELS = {"MINIMAL", "LOW", "MEDIUM", "HIGH"}
_TRANSIENT_CODES = {429, 500, 502, 503, 504}
_MAX_TOKEN_CEILING = 8192  # a cut-off answer is retried with a bigger budget, up to this
_TIMEOUT_RETRIES = 1  # a slow call is retried once: each try already waits the full timeout


def thinking_config(cfg: StepConfig) -> types.ThinkingConfig | None:
    """Map step settings to the SDK. The SDK only warns on an invalid level, so we validate."""
    if cfg.thinking_level is not None and cfg.thinking_budget is not None:
        raise LLMError("config", "set either thinking_level or thinking_budget, not both")
    if cfg.thinking_level is not None:
        level = cfg.thinking_level.upper()
        if level not in _VALID_LEVELS:
            raise LLMError("config", f"unknown thinking level {cfg.thinking_level!r}")
        if cfg.model.startswith("gemini-2.5"):
            raise LLMError("config", "Gemini 2.5 models take thinking_budget, not thinking_level")
        return types.ThinkingConfig(thinking_level=level)
    if cfg.thinking_budget is not None:
        if cfg.model.startswith("gemini-3"):
            raise LLMError("config", "Gemini 3 models take thinking_level, not thinking_budget")
        return types.ThinkingConfig(thinking_budget=cfg.thinking_budget)
    return None


def _cut_off(response) -> bool:
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    return "MAX_TOKENS" in str(reason)


class GeminiLLM:
    def __init__(
        self,
        client,
        *,
        timeout_s: float = 20.0,
        retries: int = 3,
        retry_delay_s: float = 1.0,
        send_labels: bool = False,
    ):
        self._client = client
        self._timeout_s = timeout_s
        self._retries = retries  # extra tries after a failed or unusable answer
        self._retry_delay_s = retry_delay_s
        # Request labels are only sent to Vertex AI; the Developer API may reject them.
        self._send_labels = send_labels

    def _config(self, cfg, system, schema, labels) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=cfg.temperature,
            max_output_tokens=cfg.max_output_tokens,
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=thinking_config(cfg),
            labels=labels if (self._send_labels and labels) else None,
        )

    @staticmethod
    def _parse(response, schema: type[T]) -> T | None:
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed
        text = getattr(response, "text", None)
        if not text:
            return None
        try:
            return schema.model_validate_json(text)
        except (ValidationError, ValueError, TypeError):
            return None

    async def generate(
        self,
        *,
        step: str,
        cfg: StepConfig,
        system: str,
        user: str,
        schema: type[T],
        labels: dict[str, str] | None = None,
    ) -> LLMResult[T]:
        spent = Usage()
        timeouts = 0
        for attempt in range(self._retries + 1):
            config = self._config(cfg, system, schema, labels)
            last = attempt == self._retries
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=cfg.model, contents=user, config=config
                    ),
                    timeout=self._timeout_s,
                )
            except TimeoutError as exc:
                timeouts += 1
                if last or timeouts > _TIMEOUT_RETRIES:
                    raise LLMError("timeout", f"{step}: no response in {self._timeout_s}s") from exc
                log.warning("%s timed out (attempt %d), retrying", step, attempt + 1)
                continue
            except genai_errors.APIError as exc:
                if exc.code not in _TRANSIENT_CODES or last:
                    raise LLMError("api", f"{step}: {exc.code} {exc.message}") from exc
                log.warning("%s: api %s (attempt %d), retrying", step, exc.code, attempt + 1)
                await asyncio.sleep(self._retry_delay_s * 2**attempt)
                continue
            latency_ms = int((time.monotonic() - started) * 1000)
            spent = spent + Usage.from_metadata(getattr(response, "usage_metadata", None))
            parsed = self._parse(response, schema)
            if parsed is not None:
                try:
                    cost = cost_usd(cfg.model, spent)
                except UnknownModelError:
                    log.warning("no price known for model %s; cost recorded as 0", cfg.model)
                    cost = 0.0
                log.info(
                    "llm_call step=%s model=%s in=%d out=%d thought=%d cost=%.6f ms=%d",
                    step, cfg.model, spent.input_tokens, spent.output_tokens,
                    spent.thought_tokens, cost, latency_ms,
                )  # fmt: skip
                return LLMResult(parsed, spent, cfg.model, latency_ms, cost)
            if _cut_off(response) and cfg.max_output_tokens < _MAX_TOKEN_CEILING:
                # thinking used up the budget before the answer was written: give it more room
                cfg = replace(
                    cfg, max_output_tokens=min(cfg.max_output_tokens * 2, _MAX_TOKEN_CEILING)
                )
            log.warning("invalid structured output from %s (attempt %d)", step, attempt + 1)
        raise LLMError("invalid_output", f"{step}: response did not match {schema.__name__}")
