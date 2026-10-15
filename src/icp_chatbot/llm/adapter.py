"""Gemini adapter: one place that knows the SDK. Everything else depends on the LLM protocol."""

import asyncio
import logging
import time

from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from icp_chatbot.llm.pricing import UnknownModelError, cost_usd
from icp_chatbot.llm.types import LLMError, LLMResult, StepConfig, T, Usage

log = logging.getLogger(__name__)

_VALID_LEVELS = {"MINIMAL", "LOW", "MEDIUM", "HIGH"}


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


class GeminiLLM:
    def __init__(
        self,
        client,
        *,
        timeout_s: float = 20.0,
        invalid_output_retries: int = 1,
        send_labels: bool = False,
    ):
        self._client = client
        self._timeout_s = timeout_s
        self._invalid_output_retries = invalid_output_retries
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
        config = self._config(cfg, system, schema, labels)
        spent = Usage()
        for attempt in range(self._invalid_output_retries + 1):
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=cfg.model, contents=user, config=config
                    ),
                    timeout=self._timeout_s,
                )
            except TimeoutError as exc:
                raise LLMError("timeout", f"{step}: no response in {self._timeout_s}s") from exc
            except genai_errors.APIError as exc:
                raise LLMError("api", f"{step}: {exc.code} {exc.message}") from exc
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
            log.warning("invalid structured output from %s (attempt %d)", step, attempt + 1)
        raise LLMError("invalid_output", f"{step}: response did not match {schema.__name__}")
