"""Writing the questions: the personalized opener, the best-customer question, and the rest
(spec sections 5.2 and 5.3). A model failure never blocks the interview: every question has a
plain fallback text."""

import hashlib
import logging
import random
from dataclasses import dataclass

from pydantic import BaseModel

from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.slots import SLOTS
from icp_chatbot.domain.state import SessionState, SlotValue
from icp_chatbot.llm.types import LLM, LLMError, Usage
from icp_chatbot.versions import ConfigVersionData

log = logging.getLogger(__name__)

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


MAX_OPTIONS = 8
MAX_OPTION_CHARS = 60
FILLER_OPTIONS = frozenset({"other", "others", "something else", "none", "n/a", "all of the above"})


class QuestionOutput(BaseModel):
    question: str
    options: list[str] = []  # short suggested answers, only when the answer is a pick from a list


@dataclass(frozen=True)
class Question:
    text: str
    kind: str  # opener, best_customer, slot, fallback
    slot: str | None = None
    options: tuple[str, ...] = ()
    multi: bool = True  # the user may pick several options


def clean_options(raw: list[str]) -> tuple[str, ...]:
    """Suggested answers from the model: trimmed, deduplicated, short, at most MAX_OPTIONS.
    The user can always type their own answer, so "Other" style filler is dropped. One option
    alone is not a choice, so fewer than two means none."""
    unique: dict[str, str] = {}
    for item in raw:
        item = " ".join(item.split())
        if item and len(item) <= MAX_OPTION_CHARS and item.casefold() not in FILLER_OPTIONS:
            unique.setdefault(item.casefold(), item)
    options = tuple(unique.values())[:MAX_OPTIONS]
    return options if len(options) >= 2 else ()


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


def fallback_question(slot_key: str, asked_before: int = 0) -> Question:
    slot = SLOTS[slot_key]
    if asked_before:  # never repeat the same wording: rephrase and make answering easy
        text = (
            f"No problem, let me put it another way. {slot.description.rstrip('.')}? "
            "A few words or one example is enough."
        )
    else:
        text = f"Next, could you tell me about this: {slot.label.lower()}? {slot.description}."
    return Question(
        text,
        kind="fallback",
        slot=slot_key,
        options=slot.choices,
        multi=not slot.single_choice,
    )


FOLLOW_UP_HINT = (
    "The user's last reply did not answer the question. Write a short, friendly follow-up on the "
    "same topic: acknowledge them in a few words, put the question differently (do not repeat "
    "the earlier wording), and give two or three short examples to choose from. Say a brief "
    "answer is fine."
)


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
    asked_before = state.asked_slots.get(slot_key, 0)
    if asked_before:
        lines.append(f"This topic was already asked {asked_before} time(s). " + FOLLOW_UP_HINT)
    elif previous_vague:
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
    except LLMError as exc:
        log.warning("question writer failed, using the fixed text: %s", exc)
        return QuestionResult(fallback_question(slot_key, asked_before), used_fallback=True)
    text = _clean(result.parsed.question)
    if text is None:
        return QuestionResult(
            fallback_question(slot_key, asked_before),
            result.usage,
            result.cost_usd,
            cfg.model,
            True,
        )
    if slot.choices:  # fixed wording of the choices wins over the model's suggestions
        options = slot.choices
    else:
        options = clean_options(result.parsed.options)
    # most slots hold a list of values, so several picks are fine; size and revenue are one band
    question = Question(
        text, kind="slot", slot=slot_key, options=options, multi=not slot.single_choice
    )
    return QuestionResult(question, result.usage, result.cost_usd, cfg.model)


OPENER_ANGLES = (
    "what the company offers and the problem it solves for people",
    "the most recent customer they were glad to win, and what they did for them",
    "how the business started and who they originally built it for",
    "what customers say when they explain why they chose this company",
    "which kind of customer they would happily have ten more of",
)


async def generate_opener(
    llm: LLM,
    config: ConfigVersionData,
    signup: SignupInfo,
    seed: dict[str, SlotValue],
    *,
    labels: dict[str, str] | None = None,
    angle: str | None = None,
) -> QuestionResult:
    """Always written by the model from what is known: the website seed (confirm-and-extend),
    the signup details, or just the company name with a randomly chosen angle so openers vary.
    A reviewed variant is the fallback if the model fails."""
    variant = Question(pick_opener_variant(signup.company_name, signup.role), kind="opener")
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
    else:
        lines.append(f"Angle to open with: {angle or random.choice(OPENER_ANGLES)}")
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
    except LLMError as exc:
        log.warning("question writer failed, using the fixed text: %s", exc)
        return QuestionResult(variant, used_fallback=True)
    text = _clean(result.parsed.question)
    if text is None:
        return QuestionResult(variant, result.usage, result.cost_usd, cfg.model, True)
    return QuestionResult(Question(text, kind="opener"), result.usage, result.cost_usd, cfg.model)


async def generate_followup(
    llm: LLM,
    config: ConfigVersionData,
    state: SessionState,
    question_text: str,
    kind: str,
    *,
    labels: dict[str, str] | None = None,
) -> QuestionResult:
    """A follow-up to a question that has no slot of its own (the opener, the best-customer
    question) when the reply did not answer it."""
    lines = [
        f"Rolling summary: {state.summary or '(none)'}",
        f"Question that was not answered: {question_text}",
    ]
    for turn in state.last_turns:
        lines.append(f"Earlier question: {turn.question}\nUser answer (data): {turn.answer}")
    lines.append(FOLLOW_UP_HINT)
    cfg = config.step("question")
    fallback = Question(
        "No problem, let me put it another way. What do you sell, and what kind of company "
        "buys it? A few words is enough."
        if kind == "opener"
        else "No worries. Can you name one customer you were happy with, and why they worked "
        "well? One example is enough.",
        kind=kind,
    )
    try:
        result = await llm.generate(
            step="question",
            cfg=cfg,
            system=config.system_prompt("question"),
            user="\n".join(lines),
            schema=QuestionOutput,
            labels=labels,
        )
    except LLMError as exc:
        log.warning("question writer failed, using the fixed text: %s", exc)
        return QuestionResult(fallback, used_fallback=True)
    text = _clean(result.parsed.question)
    if text is None:
        return QuestionResult(fallback, result.usage, result.cost_usd, cfg.model, True)
    return QuestionResult(Question(text, kind=kind), result.usage, result.cost_usd, cfg.model)


BEST_CUSTOMER_HINT = (
    "Ask about their best customers and one that turned out to be a poor fit: who they are and "
    "what made each a good or bad fit. Build on what they have already told you, mention their "
    "own words or product, and keep it to one or two sentences. Do not use a fixed script."
)


async def generate_best_customer(
    llm: LLM,
    config: ConfigVersionData,
    state: SessionState,
    *,
    labels: dict[str, str] | None = None,
) -> QuestionResult:
    """The best-customer question (question 2), written around what the user just said."""
    lines = [f"Rolling summary: {state.summary or '(none)'}"]
    for turn in state.last_turns:
        lines.append(f"Earlier question: {turn.question}\nUser answer (data): {turn.answer}")
    lines.append(BEST_CUSTOMER_HINT)
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
    except LLMError as exc:
        log.warning("question writer failed, using the fixed text: %s", exc)
        return QuestionResult(best_customer_question(), used_fallback=True)
    text = _clean(result.parsed.question)
    if text is None:
        return QuestionResult(
            best_customer_question(), result.usage, result.cost_usd, cfg.model, True
        )
    return QuestionResult(
        Question(text, kind="best_customer"), result.usage, result.cost_usd, cfg.model
    )
