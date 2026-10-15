"""Types shared by every model call: usage, step settings, results and errors."""

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """A model call failed. kind is one of: api, timeout, invalid_output, config."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # prompt tokens, including any cached ones
    output_tokens: int = 0  # visible output tokens
    thought_tokens: int = 0  # thinking tokens, billed as output
    cached_tokens: int = 0  # prompt tokens served from the cache

    @classmethod
    def from_metadata(cls, meta) -> "Usage":
        """Build from the SDK's usage_metadata; tolerates None and missing fields."""
        if meta is None:
            return cls()

        def num(name: str) -> int:
            return int(getattr(meta, name, 0) or 0)

        return cls(
            input_tokens=num("prompt_token_count"),
            output_tokens=num("candidates_token_count"),
            thought_tokens=num("thoughts_token_count"),
            cached_tokens=num("cached_content_token_count"),
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.thought_tokens + other.thought_tokens,
            self.cached_tokens + other.cached_tokens,
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thought_tokens


@dataclass(frozen=True)
class StepConfig:
    """Settings for one interview step (extraction, question, final ICP and so on)."""

    model: str
    temperature: float = 0.2
    max_output_tokens: int = 1024  # includes thinking tokens
    thinking_level: str | None = None  # Gemini 3.x models: minimal, low, medium, high
    thinking_budget: int | None = None  # Gemini 2.5 models: token budget


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    usage: Usage
    model: str
    latency_ms: int
    cost_usd: float


class LLM(Protocol):
    async def generate(
        self,
        *,
        step: str,
        cfg: StepConfig,
        system: str,
        user: str,
        schema: type[T],
        labels: dict[str, str] | None = None,
    ) -> LLMResult[T]: ...
