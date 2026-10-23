"""Versioned configuration: prompts and model settings per step (spec sections 4 and 9.3).

Sessions are pinned to the version that was live when they started.
"""

from pydantic import BaseModel

from icp_chatbot.config import Settings
from icp_chatbot.domain.slots import render_slot_catalog
from icp_chatbot.llm.types import StepConfig
from icp_chatbot.prompts import DEFAULT_PROMPTS

STEP_NAMES = ("extraction", "seed", "opener", "question", "final_icp")


class StepSettings(BaseModel):
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 1024  # includes thinking tokens
    thinking_level: str | None = None
    thinking_budget: int | None = None


class ConfigVersionData(BaseModel):
    version: int = 1
    prompts: dict[str, str]
    steps: dict[str, StepSettings]

    def with_code_prompts(self) -> "ConfigVersionData":
        """The prompts always come from prompts.py, so editing that file is all it takes. The
        stored version still decides the models and settings, and what a session is pinned to."""
        return self.model_copy(update={"prompts": {**self.prompts, **DEFAULT_PROMPTS}})

    def prompt(self, step: str) -> str:
        return self.prompts[step]

    def system_prompt(self, step: str) -> str:
        """The system prompt for a step. Extraction gets the slot catalog appended."""
        text = self.prompts[step]
        if step in ("extraction", "interviewer"):
            return text + render_slot_catalog()
        return text

    def step(self, name: str) -> StepConfig:
        s = self.steps[name]
        return StepConfig(
            model=s.model,
            temperature=s.temperature,
            max_output_tokens=s.max_output_tokens,
            thinking_level=s.thinking_level,
            thinking_budget=s.thinking_budget,
        )


_LEVEL_BUDGET = {"minimal": 0, "low": 512, "medium": 2048, "high": 8192}


def _thinking(model: str, level: str) -> dict:
    """Gemini 3 models take a thinking level, Gemini 2.5 models a token budget."""
    if model.startswith("gemini-2.5"):
        return {"thinking_budget": _LEVEL_BUDGET[level]}
    return {"thinking_level": level}


def default_config(settings: Settings, version: int = 1) -> ConfigVersionData:
    # Thinking levels follow spec section 5.4: minimal for extraction, low for questions,
    # moderate for the final ICP. Level support for gemini-3.1-flash-lite is unconfirmed;
    # scripts/smoke_live.py checks every step against the real model.
    return ConfigVersionData(
        version=version,
        prompts=dict(DEFAULT_PROMPTS),
        steps={
            "extraction": StepSettings(
                model=settings.model_extraction,
                temperature=0.0,
                max_output_tokens=2048,
                **_thinking(settings.model_extraction, "minimal"),
            ),
            "seed": StepSettings(
                model=settings.model_extraction,
                temperature=0.0,
                max_output_tokens=2048,
                **_thinking(settings.model_extraction, "minimal"),
            ),
            "opener": StepSettings(
                model=settings.model_question,
                temperature=0.7,
                max_output_tokens=2048,  # thinking shares this budget; 512 cut the JSON short
                **_thinking(settings.model_question, "low"),
            ),
            "question": StepSettings(
                model=settings.model_question,
                temperature=0.5,
                max_output_tokens=2048,
                **_thinking(settings.model_question, "low"),
            ),
            "final_icp": StepSettings(
                model=settings.model_final_icp,
                temperature=0.1,
                max_output_tokens=8192,
                **_thinking(settings.model_final_icp, "medium"),
            ),
        },
    )
