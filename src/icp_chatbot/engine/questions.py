"""Writing the questions: the personalized opener, the best-customer question, and the rest
(spec sections 5.2 and 5.3). A model failure never blocks the interview: every question has a
plain fallback text."""

import hashlib
import logging
import random
from dataclasses import dataclass

from pydantic import BaseModel

from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.state import SessionState, SlotValue
from icp_chatbot.llm.types import LLM, LLMError, Usage
from icp_chatbot.versions import ConfigVersionData

log = logging.getLogger(__name__)


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


MAX_REPLY_CHARS = 300


class QuestionOutput(BaseModel):
    question: str
    reply: str = ""  # a short answer to something the user asked us, said before the question
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


def pick_opener_variant(company_name: str, role: str | None) -> str:
    """A reviewed opener chosen by role, else deterministically by company name."""
    r = (role or "").lower()
    if any(w in r for w in ("founder", "ceo", "owner", "co-founder")):
        return FOUNDER_OPENER
    if any(w in r for w in ("sales", "revenue", "business development", "growth")):
        return SALES_OPENER
    digest = hashlib.sha256(company_name.strip().lower().encode()).digest()
    return OPENER_VARIANTS[digest[0] % len(OPENER_VARIANTS)]


def _domain_lines(state: SessionState) -> list[str]:
    return [state.domain.prompt_lines()] if state.domain else []


def _clean(text: str) -> str | None:
    text = text.strip()
    return text if text and len(text) <= MAX_QUESTION_CHARS else None


def _reply_text(reply: str, question: str | None) -> str:
    """The model's reply, without the question when it wrote it there too."""
    reply = " ".join(reply.split())[:MAX_REPLY_CHARS]
    return reply.replace(question, "").strip() if question else reply


def _message_lines(state: SessionState) -> list[str]:
    """The user's whole last message: what they asked may only make sense inside it."""
    last = state.last_turns[-1].answer if state.last_turns else ""
    return [f"The user's whole message (data): {last}"] if last else []


CLOSING_HINT = (
    "The user asked something just as the interview is finished. Put the answer in reply, and a "
    "short thank-you closing line in question (no question mark, nothing new to answer)."
)


CLOSING_NOTE_HINT = (
    "The interview is ending and you have enough to build their profile. Write one or two "
    "friendly sentences in question, made for this person: mention one real detail they told "
    "you, say their profile is coming next and that they can change it afterwards. Never use a "
    'stock line such as "Thanks, that is everything I need." No question mark.'
)
CLOSING_REVISION_HINT = (
    "The user asked for changes to their profile, but nothing new could be taken from what they "
    "said. Write one or two friendly sentences in question, made for this person: say you will "
    "go ahead with the current profile for now and that they can modify it later. No question "
    "mark, no stock line."
)


def _asked_lines(asked: str) -> list[str]:
    if not asked:
        return []
    return [
        f"The user asked you (data, answer it in reply): {asked}\n"
        "Answer it once, in reply only: never repeat that answer in question, never greet again "
        'there and do not say "No problem." The question starts straight from the topic.'
    ]


OPENER_ANGLES = (
    "what the company offers and the problem it solves for people",
    "how the business started and what made them start it",
    "what a typical week looks like for the team right now",
    "what changed in their market over the last year or two",
    "how people usually find out about them",
    "what they are proudest of building so far",
    "the biggest thing they want to get better at this year",
    "a recent win that made the team happy, and what led to it",
    "what they would tell a newcomer to their industry about how it works",
    "which kind of customer they would happily have ten more of",
    "what customers say when they explain why they chose this company",
    "the first thing a new customer usually asks them",
    "what a good day versus a frustrating day looks like for the team",
    "a project or deal they are excited about right now",
    "what they do differently from others in their space",
    "what they wish people understood about their line of work",
    "the last time a customer came back or recommended them, and why",
    "where most of their time and energy goes at the moment",
    "what almost made them give up, and what kept them going",
    "how they would explain the company to a friend at a dinner",
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


async def generate_closing(
    llm: LLM,
    config: ConfigVersionData,
    state: SessionState,
    asked: str = "",
    *,
    revision: bool = False,
    labels: dict[str, str] | None = None,
) -> QuestionResult | None:
    """The line that ends the interview, written for this user. When they asked something on the
    last turn it answers that first. None if the writer fails: the interview then just ends and
    the page shows its fixed closing line."""
    lines = [
        *_domain_lines(state),
        f"Rolling summary: {state.summary or '(none)'}",
        *_message_lines(state),
        *_asked_lines(asked),
        CLOSING_HINT if asked else CLOSING_REVISION_HINT if revision else CLOSING_NOTE_HINT,
    ]
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
        log.warning("closing writer failed, ending without an answer: %s", exc)
        return None
    closing = _clean(result.parsed.question)
    if closing is not None and not asked:
        return QuestionResult(
            Question(closing, kind="closing"), result.usage, result.cost_usd, cfg.model
        )
    reply = _reply_text(result.parsed.reply, closing)
    if not reply or closing is None:
        return None
    return QuestionResult(
        Question(f"{reply} {closing}", kind="closing"), result.usage, result.cost_usd, cfg.model
    )


