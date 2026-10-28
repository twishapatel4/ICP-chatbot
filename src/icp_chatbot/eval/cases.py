"""Evaluation cases: scripted conversations with the ICP they should produce (spec section 9.4)."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from icp_chatbot.domain.slots import Track, slot_map

MIN_LAUNCH_CASES = 30

# Personas and situations the launch set must cover before release (spec sections 9.4 and 14).
REQUIRED_TAGS = frozenset(
    {
        "clear_website", "empty_website", "misleading_website", "hidden_instructions_website",
        "no_website", "free_mail", "vague", "verbose", "one_long_reply", "short_answers",
        "contradictory", "off_topic", "hostile", "injection", "non_english", "detail_mode",
        "no_tech", "poor_fit_example", "b2c",
    }
)  # fmt: skip


class EvalCase(BaseModel):
    name: str
    origin: Literal["handwritten", "simulated", "real"] = "handwritten"
    track: Track = "sales"  # whose profile the conversation builds: its slots come from this track
    tags: list[str]
    signup: dict[str, str]
    website_text: str | None = None  # what the company's website says, for enrichment
    answers: list[str]
    detail_answers: list[str] = Field(
        default_factory=list
    )  # if set, the user opts into detail mode
    expected: dict[str, list[str]] = Field(default_factory=dict)  # slot -> values the ICP must hold
    forbidden: dict[str, list[str]] = Field(default_factory=dict)  # slot -> values it must not hold
    notes: str = ""


def load_cases(directory: str | Path) -> list[EvalCase]:
    return [
        EvalCase.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(Path(directory).glob("*.json"))
    ]


def case_problems(case: EvalCase) -> list[str]:
    problems = []
    if not case.answers:
        problems.append("no answers")
    known = slot_map(case.track)
    for slot in [*case.expected, *case.forbidden]:
        if slot not in known:
            problems.append(f"unknown slot {slot}")
    return problems


def check_launch_set(cases: list[EvalCase]) -> list[str]:
    """Reasons the set is not ready for launch; an empty list means it is."""
    problems = []
    if len(cases) < MIN_LAUNCH_CASES:
        problems.append(
            f"only {len(cases)} cases; the launch set needs at least {MIN_LAUNCH_CASES}"
        )
    names = [c.name for c in cases]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"duplicate case name {name}")
    covered = {tag for c in cases for tag in c.tags}
    for tag in sorted(REQUIRED_TAGS - covered):
        problems.append(f"no case is tagged {tag}")
    for case in cases:
        problems.extend(f"{case.name}: {p}" for p in case_problems(case))
    return problems
