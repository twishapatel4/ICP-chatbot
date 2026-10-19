"""Runs the interview: start, answer, detail mode, finish (spec sections 5.2, 5.3 and 5.5).

All state lives in the database. Each call reads the session, does its work, and saves with an
optimistic version check, so two answers submitted at once cannot both be applied.
"""

import logging
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.state import (
    NONE_IS_AN_ANSWER,
    SessionState,
    SlotSource,
    SlotValue,
    is_non_answer,
)
from icp_chatbot.engine.extraction import ExtractionResult, Flags, apply_extraction, extract
from icp_chatbot.engine.planner import Ask, Caps, plan_next
from icp_chatbot.engine.questions import (
    Question,
    QuestionResult,
    generate_best_customer,
    generate_followup,
    generate_opener,
    generate_question,
)
from icp_chatbot.enrichment.service import EnrichmentOutcome
from icp_chatbot.llm.types import LLM, LLMError, Usage
from icp_chatbot.store.repo import Repo
from icp_chatbot.versions import ConfigVersionData, default_config

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 2
MAX_OFFTOPIC = 2
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
    ):
        self._repo = repo
        self._llm = llm
        self._enrichment = enrichment
        self._settings = settings
        self._guard = guard or NullGuard()
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
        elif flags.off_topic:
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
            status, stop_reason, question = await self._next(
                state, config, unanswered, labels, spend
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

    async def _next(self, state, config, previous_vague, labels, spend):
        """Choose and write the next question. Returns (status, stop_reason, question)."""
        kind, asked = state.pending_kind, state.pending_question
        if kind in ("opener", "best_customer") and previous_vague and not state.followups.get(kind):
            state.followups[kind] = 1  # one follow-up, then move on
            result = await generate_followup(self._llm, config, state, asked, kind, labels=labels)
            spend.add(result.usage, result.cost_usd)
            if result.model:
                spend.models["question"] = result.model
            self._record_question(state, result.question)
            return "active", None, result.question
        if state.pending_kind == "opener" and not state.best_customer_asked:
            state.best_customer_asked = True
            result = await generate_best_customer(self._llm, config, state, labels=labels)
            spend.add(result.usage, result.cost_usd)
            if result.model:
                spend.models["question"] = result.model
            self._record_question(state, result.question)
            return "active", None, result.question
        plan = plan_next(state, self._caps)
        if not isinstance(plan, Ask):
            return "completed", plan.reason, None
        result: QuestionResult = await generate_question(
            self._llm, config, state, plan.slot, previous_vague=previous_vague, labels=labels
        )
        spend.add(result.usage, result.cost_usd)
        if result.model:
            spend.models["question"] = result.model
        self._record_question(state, result.question)
        return "active", None, result.question

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
