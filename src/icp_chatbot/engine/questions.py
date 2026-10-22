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
from icp_chatbot.domain.state import SessionState, SlotValue, is_non_answer
from icp_chatbot.llm.types import LLM, LLMError, Usage
from icp_chatbot.versions import ConfigVersionData

log = logging.getLogger(__name__)

BEST_CUSTOMER_QUESTION = (
    "Think of one customer you are really happy with. Who are they, and why do they work "
    "so well for you?"
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


BEST_CUSTOMER_VARIANTS = (
    BEST_CUSTOMER_QUESTION,
    "Which customer would you happily sign ten more like? Tell me who they are.",
    "Think of the last deal that went smoothly. Who was the buyer, and why was it easy?",
    "Who is a customer that surprised you in a good way, and what made them stand out?",
)


def best_customer_question(seed: int = 0) -> Question:
    """The fixed text used when the model fails; it varies by session like the written one."""
    text = (
        BEST_CUSTOMER_VARIANTS[seed % len(BEST_CUSTOMER_VARIANTS)]
        if seed
        else BEST_CUSTOMER_QUESTION
    )
    return Question(text, kind="best_customer")


def pick_opener_variant(company_name: str, role: str | None) -> str:
    """A reviewed opener chosen by role, else deterministically by company name."""
    r = (role or "").lower()
    if any(w in r for w in ("founder", "ceo", "owner", "co-founder")):
        return FOUNDER_OPENER
    if any(w in r for w in ("sales", "revenue", "business development", "growth")):
        return SALES_OPENER
    digest = hashlib.sha256(company_name.strip().lower().encode()).digest()
    return OPENER_VARIANTS[digest[0] % len(OPENER_VARIANTS)]


# Plain wording per topic for when the question writer is unavailable. {c} is "your customers"
# or, once the industry is known, "your <industry> customers".
FALLBACK_ASKS = {
    "industries": "What kinds of businesses buy from you? A few examples help.",
    "company_size": "Roughly how big are {c}? Think headcount.",
    "revenue_range": "Roughly how much revenue do {c} make in a year?",
    "geographies": "Which countries or regions do {c} mostly come from?",
    "company_type": "Are {c} mostly large enterprises, mid-size firms or small businesses?",
    "customer_segments": "Who do {c} themselves sell to?",
    "pain_points": "What problems do {c} run into that lead them to you?",
    "desired_outcomes": "What result do {c} hope to get from working with you?",
    "current_processes": "How do {c} handle this today, before they find you?",
    "use_cases": "What do {c} mainly use you for?",
    "tech_stack": "What tools or software do {c} already use?",
    "incumbents": "Who else do {c} usually consider or already work with?",
    "budget_range": "What kind of budget do {c} usually have for this?",
    "deal_size": "What does a typical deal with {c} look like in value?",
    "purchase_frequency": "How often do {c} buy from you?",
    "buyer_roles": "Who on the {c} side usually decides or has a say?",
    "sales_cycle": "How long does it usually take from first chat to a deal with {c}?",
    "objections": "What makes {c} hesitate or say no?",
    "buying_triggers": "What usually makes {c} reach out to you at that moment?",
    "disqualifiers": "Are there kinds of businesses you would rather not work with?",
    "success_definition": "What does a really successful customer look like to you?",
    "compliance": "Do {c} have rules or certifications you have to meet?",
    "market_position": "How would you say you compare with others who serve {c}?",
}


def fallback_question(slot_key: str, asked_before: int = 0, industry: str = "") -> Question:
    slot = SLOTS[slot_key]
    customers = f"your {industry} customers" if industry else "your customers"
    ask = FALLBACK_ASKS.get(slot_key)
    if ask is None:
        ask = f"What can you tell me about {slot.label.lower()}? {slot.description}."
    text = ask.format(c=customers)
    if asked_before:  # never repeat the same wording: rephrase and make answering easy
        text = f"Let me put that another way. {text} A few words or one example is enough."
    return Question(
        text,
        kind="fallback",
        slot=slot_key,
        options=slot.choices,
        multi=not slot.single_choice,
    )


FOLLOW_UP_HINT = (
    "The user's last reply did not answer the question. Write a follow-up of at most 20 words: "
    'if they said no or did not know, start with "No problem."; if they said something else, '
    "briefly reflect it in their own words instead. Then ask the same thing in simpler words, "
    "one question only, "
    "and put two or three short examples in options. Do not repeat the earlier wording."
)


def _industry(state: SessionState) -> str:
    return state.domain.industry if state.domain else ""


def _domain_lines(state: SessionState) -> list[str]:
    return [state.domain.prompt_lines()] if state.domain else []


PROBE_HINT = (
    "The user's last reply did not answer the question fully. Do not move on and do not repeat "
    "the question. In a few words show you understood what they said, using their own words, "
    "then ask one specific follow-up about the detail that is still missing. At most 30 words, "
    "one question only. Put two or three short examples in options if the answer is a pick from "
    'a list. Do not say "No problem." If they seem '
    "unsure or reluctant, say in a few words why it helps and that a rough guess is fine."
)

FOLLOW_UP_HINT_AFTER_QUESTION = (
    "The user's last reply did not answer the question, but it asked you something. Ask the "
    "same thing in simpler words, one question only, at most 20 words, and put two or three "
    'short examples in options. Do not say "No problem." and do not repeat the earlier wording.'
)


def _clean(text: str) -> str | None:
    text = text.strip()
    return text if text and len(text) <= MAX_QUESTION_CHARS else None


def _compose(output: QuestionOutput, asked: str = "") -> str | None:
    """The question, led by the answer to what the user asked us (only if they asked)."""
    text = _clean(output.question)
    reply = " ".join(output.reply.split())[:MAX_REPLY_CHARS] if asked else ""
    return f"{reply} {text}" if text and reply else text


DECLINED_NOTE = (
    "The user chose not to answer the last question. Accept that in one or two words "
    '("Understood.") and ask about the target slot; never ask about what they declined again.'
)


def _asked_lines(asked: str) -> list[str]:
    return [f"The user asked you (data, answer it in reply): {asked}"] if asked else []


async def generate_question(
    llm: LLM,
    config: ConfigVersionData,
    state: SessionState,
    slot_key: str,
    *,
    previous_vague: bool = False,
    asked: str = "",
    declined: bool = False,
    labels: dict[str, str] | None = None,
) -> QuestionResult:
    slot = SLOTS[slot_key]
    lines = [
        *_domain_lines(state),
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
    lines += _asked_lines(asked)
    if state.order_seed:
        lines.append(_style_line(state))
    if declined:
        lines.append(DECLINED_NOTE)
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
        return QuestionResult(
            fallback_question(slot_key, asked_before, _industry(state)), used_fallback=True
        )
    text = _compose(result.parsed, asked)
    if text is None:
        return QuestionResult(
            fallback_question(slot_key, asked_before, _industry(state)),
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

QUESTION_STYLES = (
    "link it to something specific they just said",
    "ask it through a concrete recent example from their work",
    "ask it casually, as a colleague would over coffee",
    "ask it by first saying in a few words what you picked up so far",
    "keep it very short and let the options carry the detail",
    "ask it from the point of view of the people they sell to",
)

BEST_CUSTOMER_HINTS = (
    "Ask about one customer they are really happy with: who they are and why they work well.",
    "Ask about a customer who surprised them in a good way, and what made the difference.",
    "Ask about the last deal that closed easily, and what the buyer was like.",
    "Ask about a customer they would happily clone, and what sets them apart.",
)


def _style_line(state: SessionState) -> str:
    """A different way of phrasing each question, fixed per session and turn."""
    rng = random.Random(f"{state.order_seed}:{state.questions_asked}")
    return f"Style for this question: {rng.choice(QUESTION_STYLES)}."


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
    asked: str = "",
    labels: dict[str, str] | None = None,
) -> QuestionResult:
    """A follow-up to a question that has no slot of its own (the opener, the best-customer
    question) when the reply did not answer it."""
    lines = [
        *_domain_lines(state),
        f"Rolling summary: {state.summary or '(none)'}",
        f"Question that was not answered: {question_text}",
    ]
    for turn in state.last_turns:
        lines.append(f"Earlier question: {turn.question}\nUser answer (data): {turn.answer}")
    said_something = bool(state.last_turns) and not is_non_answer(state.last_turns[-1].answer)
    lines.append(
        FOLLOW_UP_HINT_AFTER_QUESTION if asked else PROBE_HINT if said_something else FOLLOW_UP_HINT
    )
    lines += _asked_lines(asked)
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
    text = _compose(result.parsed, asked)
    if text is None:
        return QuestionResult(fallback, result.usage, result.cost_usd, cfg.model, True)
    question = Question(text, kind=kind, options=clean_options(result.parsed.options))
    return QuestionResult(question, result.usage, result.cost_usd, cfg.model)


BEST_CUSTOMER_HINT = (
    "Ask about their best customers by naming one customer they are really happy with: who they "
    "are and why they work well. "
)
BEST_CUSTOMER_LIMITS = (
    "Use their own words or product, at most 25 words, plain everyday words, one question only."
)


async def generate_best_customer(
    llm: LLM,
    config: ConfigVersionData,
    state: SessionState,
    *,
    asked: str = "",
    declined: bool = False,
    labels: dict[str, str] | None = None,
) -> QuestionResult:
    """The best-customer question (question 2), written around what the user just said."""
    lines = [*_domain_lines(state), f"Rolling summary: {state.summary or '(none)'}"]
    for turn in state.last_turns:
        lines.append(f"Earlier question: {turn.question}\nUser answer (data): {turn.answer}")
    hint = (
        random.Random(f"{state.order_seed}:bc").choice(BEST_CUSTOMER_HINTS)
        if state.order_seed
        else BEST_CUSTOMER_HINT
    )
    lines.append(f"{hint} {BEST_CUSTOMER_LIMITS}")
    lines += _asked_lines(asked)
    if declined:
        lines.append(DECLINED_NOTE)
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
        return QuestionResult(best_customer_question(state.order_seed), used_fallback=True)
    text = _compose(result.parsed, asked)
    if text is None:
        return QuestionResult(
            best_customer_question(state.order_seed), result.usage, result.cost_usd, cfg.model, True
        )
    return QuestionResult(
        Question(text, kind="best_customer"), result.usage, result.cost_usd, cfg.model
    )


REASK_HINT = (
    "The user asked you something instead of answering. Put your answer in reply, then ask the "
    "waiting question again in your own words, same meaning, at most 25 words, one question "
    'only. Do not use options. Do not say "No problem."'
)


async def generate_reply(
    llm: LLM,
    config: ConfigVersionData,
    state: SessionState,
    pending: Question,
    asked: str,
    *,
    labels: dict[str, str] | None = None,
) -> QuestionResult:
    """The user put a question to us instead of answering: answer it, then ask the waiting
    question again. The waiting question and its options stay as they were."""
    lines = [
        *_domain_lines(state),
        f"Rolling summary: {state.summary or '(none)'}",
        f"Question still waiting for an answer: {pending.text}",
        *_asked_lines(asked),
        REASK_HINT,
    ]
    cfg = config.step("question")
    fallback = Question(
        "Fair question, let me come back to that. " + pending.text,
        kind=pending.kind,
        slot=pending.slot,
        options=pending.options,
        multi=pending.multi,
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
        log.warning("reply writer failed, using the fixed text: %s", exc)
        return QuestionResult(fallback, used_fallback=True)
    reply = " ".join(result.parsed.reply.split())[:MAX_REPLY_CHARS]
    again = _clean(result.parsed.question)
    if not reply or again is None:
        return QuestionResult(fallback, result.usage, result.cost_usd, cfg.model, True)
    question = Question(
        f"{reply} {again}",
        kind=pending.kind,
        slot=pending.slot,
        options=pending.options,
        multi=pending.multi,
    )
    return QuestionResult(question, result.usage, result.cost_usd, cfg.model)


async def generate_probe(
    llm: LLM,
    config: ConfigVersionData,
    state: SessionState,
    slot_key: str,
    *,
    asked: str = "",
    labels: dict[str, str] | None = None,
) -> QuestionResult:
    """The answer to a slot question was vague or partial: show we understood it and ask for
    the missing detail, instead of moving on to another topic."""
    slot = SLOTS[slot_key]
    lines = [
        *_domain_lines(state),
        f"Target slot: {slot.label} ({slot.description})",
        f"Recorded for it so far: {'; '.join(state.values(slot_key)) or '(nothing usable)'}",
        f"Rolling summary: {state.summary or '(none)'}",
    ]
    for turn in state.last_turns:
        lines.append(f"Earlier question: {turn.question}\nUser answer (data): {turn.answer}")
    lines.append(PROBE_HINT)
    lines += _asked_lines(asked)
    cfg = config.step("question")
    fallback = fallback_question(slot_key, asked_before=1, industry=_industry(state))
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
        log.warning("probe writer failed, using the fixed text: %s", exc)
        return QuestionResult(fallback, used_fallback=True)
    text = _compose(result.parsed, asked)
    if text is None:
        return QuestionResult(fallback, result.usage, result.cost_usd, cfg.model, True)
    options = slot.choices or clean_options(result.parsed.options)
    question = Question(
        text, kind="slot", slot=slot_key, options=options, multi=not slot.single_choice
    )
    return QuestionResult(question, result.usage, result.cost_usd, cfg.model)
