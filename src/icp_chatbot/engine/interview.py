"""Runs the interview: start, answer, detail mode, finish (spec sections 5.2, 5.3 and 5.5).

All state lives in the database. Each call reads the session, does its work, and saves with an
optimistic version check, so two answers submitted at once cannot both be applied.
"""

import logging
import random
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.slots import SLOTS
from icp_chatbot.domain.state import (
    NONE_IS_AN_ANSWER,
    SessionState,
    SlotSource,
    SlotValue,
    is_non_answer,
)
from icp_chatbot.engine.extraction import ExtractionResult, Flags, apply_extraction, extract
from icp_chatbot.engine.planner import COMPLETION_CONFIDENCE, Ask, Caps, plan_next
from icp_chatbot.engine.questions import (
    Question,
    QuestionResult,
    generate_best_customer,
    generate_followup,
    generate_opener,
    generate_probe,
    generate_question,
    generate_reply,
)
from icp_chatbot.enrichment.service import EnrichmentOutcome
from icp_chatbot.llm.types import LLM, LLMError, Usage
from icp_chatbot.store.repo import Repo
from icp_chatbot.versions import ConfigVersionData, default_config

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 2
MAX_OFFTOPIC = 2
MAX_STALLS = 2  # clarifications in a row that bring nothing new, then the interview moves on
MAX_PROBES = 6  # absolute limit per slot, however well the user is engaging
PROGRESS = 0.05  # confidence a clarification must add to count as progress
PROBE_MIN_IMPORTANCE = 0.6
MAX_USER_QUESTIONS = 6  # past this, questions are treated like any other off-track reply
REDIRECT_PREFIX = "Let's get back to describing your ideal customer. "
RETRY_PREFIX = "Sorry, I didn't catch that. "
DECLINE_PREFIX = (
    "No worries, and thanks for being open with me. This one really does help us: it's how we "
    "shape your profile and find better-fit customers for you, nothing more. Could you give it "
    "a try? A few words is enough. "
)


class SessionClosed(Exception):
    """The session is no longer accepting answers."""


class InvalidAnswer(Exception):
    """The answer is empty or too long."""


class InvalidTransition(Exception):
    """The requested change is not allowed in the session's current state."""


class Guard(Protocol):
    async def check_start(self, tenant_id: str) -> None: ...


class NullGuard:
    async def check_start(self, tenant_id: str) -> None:
        return None


class Enrichment(Protocol):
    async def run(
        self, signup: SignupInfo, config: ConfigVersionData, labels: dict[str, str] | None = None
    ) -> EnrichmentOutcome: ...


@dataclass(frozen=True)
class TurnResult:
    session_id: str
    question: Question | None
    status: str  # active, completed or fallback
    stop_reason: str | None
    icp_ready: bool
    fallback_form: bool
    mode: str
    questions_asked: int


def _declined(
    state: SessionState, target: str | None, kind: str, text: str, applied: list[str]
) -> bool:
    """A bare "no" or "not sure" to a question that needs a real answer, not yet re-asked."""
    return (
        is_non_answer(text)
        and not applied
        and target not in NONE_IS_AN_ANSWER
        and not state.declines.get(target or kind)
    )


def _pure_question(flags: Flags, applied: list[str], state: SessionState) -> bool:
    """The user asked us something and gave no answer: reply, then ask again."""
    return bool(flags.asks_bot) and not applied and state.user_questions < MAX_USER_QUESTIONS


@dataclass
class _Spend:
    usage: Usage = Usage()
    cost: float = 0.0
    latency_ms: int = 0
    models: dict = field(default_factory=dict)

    def add(self, usage: Usage, cost: float, latency_ms: int = 0) -> None:
        self.usage = self.usage + usage
        self.cost += cost
        self.latency_ms += latency_ms


class InterviewEngine:
    def __init__(
        self,
        *,
        repo: Repo,
        llm: LLM,
        enrichment: Enrichment,
        settings: Settings,
        guard: Guard | None = None,
        vary_order: bool = True,
    ):
        self._repo = repo
        self._llm = llm
        self._enrichment = enrichment
        self._settings = settings
        self._guard = guard or NullGuard()
        self._vary_order = vary_order  # each session gets its own question order
        self._caps = Caps(
            quick_cap=settings.quick_mode_question_cap,
            detail_total_cap=settings.detail_mode_total_cap,
            detail_extra=settings.detail_mode_extra_questions,
        )

    # -- configuration -------------------------------------------------------------------

    async def ensure_live_config(self) -> ConfigVersionData:
        config = await self._repo.live_config()
        if config is None:
            config = default_config(self._settings)
            try:
                await self._repo.publish_config(config, author="system")
            except IntegrityError:  # another request published version 1 first
                config = await self._repo.live_config()
        return config

    @staticmethod
    def _labels(session_id: str, turn: int, version: int) -> dict[str, str]:
        return {"session_id": session_id, "turn": str(turn), "config_version": str(version)}

    # -- start -----------------------------------------------------------------------------

    async def start(self, tenant_id: str, signup: SignupInfo) -> TurnResult:
        await self._guard.check_start(tenant_id)
        await self._repo.ensure_tenant(tenant_id, signup.company_name)
        config = await self.ensure_live_config()

        state = SessionState()
        if self._vary_order:
            state.order_seed = random.randint(1, 2**31 - 1)
            state.best_customer_at = random.Random(state.order_seed).choice((2, 3, 4, 5))
        if signup.website:
            state.slots["online_presence"] = SlotValue(
                values=[signup.website],
                confidence=0.9,
                source=SlotSource.STATED,
                source_turn=0,
                evidence="given at signup",
            )
        session_id = await self._repo.create_session(tenant_id, config.version, state)
        labels = self._labels(session_id, 0, config.version)
        spend = _Spend()

        enrichment = await self._enrichment.run(signup, config, labels)
        spend.add(enrichment.usage, enrichment.cost_usd)
        state.seed = dict(enrichment.seed)
        for key, value in enrichment.seed.items():
            state.slots.setdefault(key, value)  # never overrides something the user stated
        await self._repo.save_enrichment(
            tenant_id,
            session_id,
            website=enrichment.website,
            pages=enrichment.pages,
            outcome=enrichment.status,
            seed={k: v.model_dump(mode="json") for k, v in enrichment.seed.items()},
            duration_ms=enrichment.duration_ms,
            usage=enrichment.usage,
        )

        opener = await generate_opener(self._llm, config, signup, enrichment.seed, labels=labels)
        spend.add(opener.usage, opener.cost_usd)
        state.questions_asked = 1
        state.pending_question = opener.question.text
        state.pending_kind = "opener"
        state.pending_slot = None
        state.pending_options, state.pending_multi = [], True
        await self._repo.save_state(
            tenant_id, session_id, state, expected_version=1, usage=spend.usage, cost_usd=spend.cost
        )
        return self._result(session_id, state, "active", opener.question)

    # -- answer ----------------------------------------------------------------------------

    def _validate(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise InvalidAnswer("the answer is empty")
        if len(text) > self._settings.max_answer_chars:
            raise InvalidAnswer(
                f"the answer is longer than {self._settings.max_answer_chars} characters"
            )
        return text

    async def answer(self, tenant_id: str, session_id: str, text: str) -> TurnResult:
        text = self._validate(text)
        record = await self._repo.get_session(tenant_id, session_id)
        if record.status != "active":
            raise SessionClosed(record.status)
        state = record.state
        config = await self._repo.get_config(record.config_version)
        turn_no = state.answers_received + 1
        labels = self._labels(session_id, turn_no, config.version)
        spend = _Spend()
        question_text, question_kind, target = (
            state.pending_question,
            state.pending_kind,
            state.pending_slot,
        )

        extraction: ExtractionResult | None = None
        try:
            result = await extract(self._llm, config, state, question_text, target, text, labels)
            extraction = result.parsed
            spend.add(result.usage, result.cost_usd, result.latency_ms)
            spend.models["extraction"] = result.model
            state.failures = 0
        except LLMError as exc:
            state.failures += 1
            log.warning("extraction failed for session %s: %s", session_id, exc)
        applied: list[str] = []
        if extraction:
            applied = apply_extraction(state, extraction, turn_no, target).applied
        flags = extraction.flags if extraction else Flags()
        state.answers_received = turn_no
        state.push_turn(question_text, text)

        status, stop_reason, question = "active", None, None
        if extraction is None and state.failures >= MAX_CONSECUTIVE_FAILURES:
            status, stop_reason = "fallback", "model_unavailable"
        elif extraction is None:
            question = Question(
                RETRY_PREFIX + question_text,
                kind=question_kind,
                slot=target,
                options=tuple(state.pending_options),
                multi=state.pending_multi,
            )
        elif _pure_question(flags, applied, state):
            state.user_questions += 1
            result = await generate_reply(
                self._llm,
                config,
                state,
                Question(
                    question_text,
                    kind=question_kind,
                    slot=target,
                    options=tuple(state.pending_options),
                    multi=state.pending_multi,
                ),
                flags.asks_bot,
                labels=labels,
            )
            spend.add(result.usage, result.cost_usd)
            if result.model:
                spend.models["question"] = result.model
            question = result.question  # the waiting question stays pending, nothing is consumed
        elif flags.off_topic and not flags.asks_bot:
            state.offtopic_count += 1
            if state.offtopic_count >= MAX_OFFTOPIC:
                status, stop_reason = "fallback", "off_topic"
            else:
                question = Question(
                    REDIRECT_PREFIX + question_text,
                    kind=question_kind,
                    slot=target,
                    options=tuple(state.pending_options),
                    multi=state.pending_multi,
                )
        elif flags.wants_to_finish:
            status, stop_reason = "completed", "user_finished"
        elif flags.refuses and not applied:
            # a firm no: respect it, never push, move on to the next topic
            if target:
                state.skipped.append(target) if target not in state.skipped else None
            if question_kind in ("opener", "best_customer"):
                state.followups[question_kind] = 1
            status, stop_reason, question = await self._next(
                state, config, False, labels, spend, flags.asks_bot, declined=True
            )
        elif _declined(state, target, question_kind, text, applied):
            state.declines[target or question_kind] = 1  # ask once more, then move on
            question = Question(
                DECLINE_PREFIX + question_text,
                kind=question_kind,
                slot=target,
                options=tuple(state.pending_options),
                multi=state.pending_multi,
            )
        else:
            unanswered = flags.vague or not applied
            if flags.asks_bot:
                state.user_questions += 1
            status, stop_reason, question = await self._next(
                state, config, unanswered, labels, spend, flags.asks_bot
            )

        if status != "active":
            state.finished = status == "completed"
            state.stop_reason = stop_reason
            state.pending_question, state.pending_kind, state.pending_slot = "", "", None
            state.pending_options = []
        await self._repo.save_state(
            tenant_id,
            session_id,
            state,
            expected_version=record.row_version,
            status=status if status != "active" else None,
            usage=spend.usage,
            cost_usd=spend.cost,
        )
        await self._repo.add_turn(
            tenant_id,
            session_id,
            turn_no=turn_no,
            question_text=question_text,
            question_kind=question_kind,
            slot_target=target,
            answer_text=text,
            extracted=extraction.model_dump(mode="json") if extraction else {},
            state_after=state.model_dump(mode="json"),
            step_models=spend.models or {},
            latency_ms=spend.latency_ms,
            usage=spend.usage,
            cost_usd=spend.cost,
        )
        return self._result(session_id, state, status, question, stop_reason)

    async def _next(self, state, config, previous_vague, labels, spend, asked="", declined=False):
        """Choose and write the next question. Returns (status, stop_reason, question)."""
        kind, question, user_asked = state.pending_kind, state.pending_question, asked
        if kind in ("opener", "best_customer") and previous_vague and not state.followups.get(kind):
            state.followups[kind] = 1  # one follow-up, then move on
            result = await generate_followup(
                self._llm, config, state, question, kind, asked=user_asked, labels=labels
            )
            spend.add(result.usage, result.cost_usd)
            if result.model:
                spend.models["question"] = result.model
            self._record_question(state, result.question)
            return "active", None, result.question
        if not state.best_customer_asked and state.questions_asked + 1 >= state.best_customer_at:
            state.best_customer_asked = True
            result = await generate_best_customer(
                self._llm, config, state, asked=user_asked, declined=declined, labels=labels
            )
            spend.add(result.usage, result.cost_usd)
            if result.model:
                spend.models["question"] = result.model
            self._record_question(state, result.question)
            return "active", None, result.question
        probe_slot = self._probe_slot(state, previous_vague)
        if probe_slot:  # understand what they said before moving on
            state.probes[probe_slot] = state.probes.get(probe_slot, 0) + 1
            result = await generate_probe(
                self._llm, config, state, probe_slot, asked=user_asked, labels=labels
            )
            spend.add(result.usage, result.cost_usd)
            if result.model:
                spend.models["question"] = result.model
            q = result.question  # a clarification: same slot, so no new question is counted
            state.pending_question, state.pending_options = q.text, list(q.options)
            state.pending_multi = q.multi
            return "active", None, q
        plan = plan_next(state, self._caps)
        if not isinstance(plan, Ask):
            return "completed", plan.reason, None
        result: QuestionResult = await generate_question(
            self._llm,
            config,
            state,
            plan.slot,
            previous_vague=previous_vague,
            asked=user_asked,
            declined=declined,
            labels=labels,
        )
        spend.add(result.usage, result.cost_usd)
        if result.model:
            spend.models["question"] = result.model
        self._record_question(state, result.question)
        return "active", None, result.question

    @staticmethod
    def _probe_slot(state: SessionState, previous_vague: bool) -> str | None:
        """The slot just asked about, if the answer was vague or partial and worth a follow-up."""
        key = state.pending_slot
        slot = SLOTS.get(key) if key and state.pending_kind == "slot" else None
        if slot is None or not (slot.blocks or slot.importance >= PROBE_MIN_IMPORTANCE):
            return None
        if key in state.skipped or state.mode != "quick":
            return None
        if state.last_turns and is_non_answer(state.last_turns[-1].answer):
            return None  # a bare "no" or "not sure" is handled by the decline path
        if key in state.probe_conf:  # they just answered a clarification: did it help?
            gained = state.confidence(key) > state.probe_conf[key] + PROGRESS
            state.stalls[key] = 0 if gained else state.stalls.get(key, 0) + 1
        if state.stalls.get(key, 0) >= MAX_STALLS or state.probes.get(key, 0) >= MAX_PROBES:
            return None  # the user is not getting anywhere with this one: let it go
        # vague or empty, or a partial answer to this very slot; an answer that filled other
        # slots and left this one untouched is not a reason to keep pressing
        if previous_vague or 0 < state.confidence(key) < COMPLETION_CONFIDENCE:
            state.probe_conf[key] = state.confidence(key)
            return key
        return None

    @staticmethod
    def _record_question(state: SessionState, question: Question) -> None:
        state.questions_asked += 1
        state.pending_question = question.text
        state.pending_kind = question.kind
        state.pending_slot = question.slot
        state.pending_options = list(question.options)
        state.pending_multi = question.multi
        if question.slot:
            state.asked_slots[question.slot] = state.asked_slots.get(question.slot, 0) + 1

    # -- finishing early and detail mode ------------------------------------------------------

    async def finish(self, tenant_id: str, session_id: str) -> TurnResult:
        record = await self._repo.get_session(tenant_id, session_id)
        if record.status != "active":
            raise SessionClosed(record.status)
        state = record.state
        state.finished, state.stop_reason = True, "user_finished"
        state.pending_question, state.pending_kind, state.pending_slot = "", "", None
        state.pending_options = []
        await self._repo.save_state(
            tenant_id, session_id, state, expected_version=record.row_version, status="completed"
        )
        return self._result(session_id, state, "completed", None, "user_finished")

    async def request_detail(self, tenant_id: str, session_id: str) -> TurnResult:
        record = await self._repo.get_session(tenant_id, session_id)
        state = record.state
        if record.status != "completed":
            raise InvalidTransition("detail mode is offered after the first ICP is complete")
        if state.mode == "detail":
            raise InvalidTransition("detail mode was already used")
        if state.questions_asked >= self._caps.detail_total_cap:
            raise InvalidTransition("the question limit has been reached")
        config = await self._repo.get_config(record.config_version)
        labels = self._labels(session_id, state.answers_received + 1, config.version)
        state.mode = "detail"
        state.detail_started_at = state.questions_asked
        state.finished, state.stop_reason = False, None
        spend = _Spend()
        status, stop_reason, question = await self._next(state, config, False, labels, spend)
        if status != "active":
            state.finished, state.stop_reason = True, stop_reason
        await self._repo.save_state(
            tenant_id,
            session_id,
            state,
            expected_version=record.row_version,
            status="active" if status == "active" else "completed",
            usage=spend.usage,
            cost_usd=spend.cost,
        )
        await self._repo.add_feedback(tenant_id, session_id, type="detail_mode_start")
        return self._result(session_id, state, status, question, stop_reason)

    # -- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _result(
        session_id: str,
        state: SessionState,
        status: str,
        question: Question | None,
        stop_reason: str | None = None,
    ) -> TurnResult:
        return TurnResult(
            session_id=session_id,
            question=question,
            status=status,
            stop_reason=stop_reason,
            icp_ready=status == "completed",
            fallback_form=status == "fallback",
            mode=state.mode,
            questions_asked=state.questions_asked,
        )
