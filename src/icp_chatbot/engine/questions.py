"""Writing the questions: the personalized opener, the best-customer question, and the rest
(spec sections 5.2 and 5.3). A model failure never blocks the interview: every question has a
plain fallback text."""

import hashlib
from dataclasses import dataclass

from pydantic import BaseModel

from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.slots import SLOTS
from icp_chatbot.domain.state import SessionState, SlotValue
from icp_chatbot.llm.types import LLM, LLMError, Usage
from icp_chatbot.versions import ConfigVersionData

BEST_CUSTOMER_QUESTION = (
    "Think of two or three of your best customers, and one that turned out to be a poor fit. "
    "Who are they, and what made each a good or bad fit?"
)

OPENER_VARIANTS = (
    "What does your company sell, and who are the customers you most want more of?",
    "To start: in a sentence or two, what do you sell and to whom?",
    "Tell me about your business: what do you offer, and who buys it?",
)
FOUNDER_OPENER = (
    "You know your customers better than anyone. What do you sell, and which companies have "
    "been the best fit so far?"
)
SALES_OPENER = (
    "From the sales side: which kinds of companies close fastest for you, and what do you sell "
    "them?"
)

MAX_QUESTION_CHARS = 400


class QuestionOutput(BaseModel):
    question: str


@dataclass(frozen=True)
class Question:
    text: str
    kind: str  # opener, best_customer, slot, fallback
    slot: str | None = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuestionResult:
    question: Question
    usage: Usage = Usage()
    cost_usd: float = 0.0
    model: str | None = None
    used_fallback: bool = False


def best_customer_question() -> Question:
    return Question(BEST_CUSTOMER_QUESTION, kind="best_customer")


def pick_opener_variant(company_name: str, role: str | None) -> str:
    """A reviewed opener chosen by role, else deterministically by company name."""
    r = (role or "").lower()
    if any(w in r for w in ("founder", "ceo", "owner", "co-founder")):
        return FOUNDER_OPENER
    if any(w in r for w in ("sales", "revenue", "business development", "growth")):
        return SALES_OPENER
    digest = hashlib.sha256(company_name.strip().lower().encode()).digest()
    return OPENER_VARIANTS[digest[0] % len(OPENER_VARIANTS)]


def fallback_question(slot_key: str) -> Question:
    slot = SLOTS[slot_key]
    text = f"Next, could you tell me about this: {slot.label.lower()}? {slot.description}."
    return Question(text, kind="fallback", slot=slot_key, options=slot.choices)


def _clean(text: str) -> str | None:
    text = text.strip()
    return text if text and len(text) <= MAX_QUESTION_CHARS else None


async def generate_question(
    llm: LLM,
    config: ConfigVersionData,
    state: SessionState,
    slot_key: str,
    *,
    previous_vague: bool = False,
    labels: dict[str, str] | None = None,
) -> QuestionResult:
    slot = SLOTS[slot_key]
    lines = [
        f"Target slot: {slot.label} ({slot.description})",
        f"Rolling summary: {state.summary or '(none)'}",
    ]
    for turn in state.last_turns:
        lines.append(f"Earlier question: {turn.question}\nUser answer (data): {turn.answer}")
    if previous_vague:
        lines.append("The previous answer was vague: include two or three short examples.")
    cfg = config.step("question")
    try:
        result = await llm.generate(
            step="question",
            cfg=cfg,
            system=config.system_prompt("question"),
            user="\n".join(lines),
            schema=QuestionOutput,
            labels=labels,
        )
    except LLMError:
        return QuestionResult(fallback_question(slot_key), used_fallback=True)
    text = _clean(result.parsed.question)
    if text is None:
        return QuestionResult(
            fallback_question(slot_key), result.usage, result.cost_usd, cfg.model, True
        )
    question = Question(text, kind="slot", slot=slot_key, options=slot.choices)
    return QuestionResult(question, result.usage, result.cost_usd, cfg.model)


async def generate_opener(
    llm: LLM,
    config: ConfigVersionData,
    signup: SignupInfo,
    seed: dict[str, SlotValue],
    *,
    labels: dict[str, str] | None = None,
) -> QuestionResult:
    """Tier 1: website seed, confirm-and-extend. Tier 2: signup details. Tier 3: a reviewed
    variant, chosen by role or company name."""
    variant = Question(pick_opener_variant(signup.company_name, signup.role), kind="opener")
    if not seed and not signup.has_context:
        return QuestionResult(variant)

    lines = [f"Company: {signup.company_name}"]
    for label, value in (
        ("Signer's role", signup.role),
        ("Industry", signup.industry),
        ("Company size", signup.company_size),
        ("Referral source", signup.source),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if seed:
        lines.append("Website findings (hypotheses, not facts):")
        for key, value in seed.items():
            lines.append(f"- {key}: {'; '.join(value.values)}")
    cfg = config.step("opener")
    try:
        result = await llm.generate(
            step="opener",
            cfg=cfg,
            system=config.system_prompt("opener"),
            user="\n".join(lines),
            schema=QuestionOutput,
            labels=labels,
        )
    except LLMError:
        return QuestionResult(variant, used_fallback=True)
    text = _clean(result.parsed.question)
    if text is None:
        return QuestionResult(variant, result.usage, result.cost_usd, cfg.model, True)
    return QuestionResult(Question(text, kind="opener"), result.usage, result.cost_usd, cfg.model)
