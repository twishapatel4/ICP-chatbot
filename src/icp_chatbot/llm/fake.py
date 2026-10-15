"""A scripted stand-in for the model, used by tests and by dry-run evaluation."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from icp_chatbot.llm.types import LLMResult, StepConfig, T, Usage


@dataclass
class FakeCall:
    step: str
    cfg: StepConfig
    system: str
    user: str
    schema: type
    labels: dict[str, str] | None


class FakeLLM:
    """Returns queued responses per step.

    script maps a step name to a list. Each item is a pydantic instance of the step's schema, an
    Exception (raised), or a callable taking the user message and returning one of those.
    """

    def __init__(self, script: dict[str, list[Any]] | None = None, usage: Usage | None = None):
        self.script = {step: list(items) for step, items in (script or {}).items()}
        self.usage = usage or Usage()
        self.calls: list[FakeCall] = []

    def queue(self, step: str, *items: Any) -> None:
        self.script.setdefault(step, []).extend(items)

    def calls_for(self, step: str) -> list[FakeCall]:
        return [c for c in self.calls if c.step == step]

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
        self.calls.append(FakeCall(step, cfg, system, user, schema, labels))
        queue = self.script.get(step)
        if not queue:
            raise AssertionError(f"FakeLLM has no scripted response left for step {step!r}")
        item = queue.pop(0)
        if callable(item) and not isinstance(item, BaseModel):
            item = item(user)
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, schema):
            raise AssertionError(f"step {step!r}: expected {schema.__name__}, got {type(item)}")
        return LLMResult(item, self.usage, cfg.model, 1, 0.0)
