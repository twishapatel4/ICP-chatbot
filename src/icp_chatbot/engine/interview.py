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
from icp_chatbot.domain.state import (
    NONE_IS_AN_ANSWER,
    SessionState,
    SlotSource,
    SlotValue,
    is_non_answer,
    is_unsure,
)
from icp_chatbot.engine.extraction import (
    ExtractionResult,
    Flags,
    Thread,
    apply_extraction,
    extract,
    missed_question,
)
from icp_chatbot.engine.planner import COMPLETION_CONFIDENCE, Ask, Caps, plan_next
from icp_chatbot.engine.questions import (
    Question,
    QuestionResult,
    generate_best_customer,
    generate_clarify,
    generate_closing,
    generate_followup,
    generate_opener,
    generate_reask,
    generate_probe,
    generate_question,
    generate_reply,
    generate_thread_question,
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
MAX_THREAD_QUESTIONS = 3  # curious follow-ups in a row on what the user brought up
REVISION_QUESTION = "What should be changed or added in this profile?"
REVISION_CLARIFY = (
    "Which part is missing or not right? For example the industries, company size, regions, "
    "buyers, pain points or something else. Tell me what to add or change."
)
REDIRECT_PREFIX ="Let's get back to describing your ideal customer. "
RETRY_NOTICE = (
    "I could not process your last message because my AI service is not responding right now. "
    "This is not about what you wrote. Please send it again."
)
DEGRADED_NOTICE = (
    "My AI service is slow or unavailable right now, so this is a simpler standard question. "
    "It is not about your answer, which was saved."
)
DECLINE_PREFIX = (
    "No worries, and thanks for being open with me. This one really does help us: it's how we "
    "shape your profile and find better-fit customers for you, nothing more. Could you give it "
    "a try? "
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
    notice: str | None = None  # a plain-words note when a model step failed: not the user's fault


def _declined(
    state: SessionState, target: str | None, kind: str, text: str, applied: list[str]
) -> bool:
    """A bare "no" or "not sure" to a question that needs a real answer, not yet re-asked. A
    curious follow-up on something the user brought up never needs one."""
    return (
        kind != "thread"
        and is_non_answer(text)
        and not applied
        and target not in NONE_IS_AN_ANSWER
        and not state.declines.get(target or kind)
    )


def _set_aside(state: SessionState, target: str | None, kind: str) -> None:
    """The user will not answer this question: never ask it, or a follow-up to it, again. For a
    curious follow-up that only ends the detour; the topic itself stays open."""
    if target and kind != "thread" and target not in state.skipped:
        state.skipped.append(target)
    if kind in ("opener", "best_customer"):
        state.followups[kind] = 1


def _thread(
    extraction: ExtractionResult | None, state: SessionState, kind: str, unanswered: bool
) -> Thread | None:
    """Something the user brought up that is worth a curious follow-up. On a detour we keep
    digging while the answers teach us about their customers, whether or not the model marks it."""
    new = extraction.thread if extraction else None
    new = new if new and new.topic.strip() else None
    if state.thread_run >= MAX_THREAD_QUESTIONS:
        return None
    if kind != "thread":
        return new
    if unanswered:
        return None  # the detour ran dry: back to our own questions
    return new or Thread(topic=state.thread_topic, slot=state.thread_slot)


def _unflagged_question(flags: Flags, applied: list[str], text: str) -> bool:
    """A reply that recorded nothing and holds a question the model did not flag."""
    if flags.asks_bot or flags.refuses or flags.wants_to_finish or applied:
        return False
    return not is_non_answer(text) and bool(missed_question(text))


def _pure_question(flags: Flags, applied: list[str], state: SessionState) -> bool:
    """The user asked us something and gave no answer: reply, then ask again."""
    return bool(flags.asks_bot) and not applied and state.user_questions < MAX_USER_QUESTIONS


@dataclass
class _Spend:
    usage: Usage = Usage()
    cost: float = 0.0
    latency_ms: int = 0
    models: dict = field(default_factory=dict)
    degraded: bool = False  # a model step failed and the user got fixed text instead

    def add(self, usage: Usage, cost: float, latency_ms: int = 0, fallback: bool = False) -> None:
        self.usage = self.usage + usage
        self.cost += cost
        self.latency_ms += latency_ms
        self.degraded = self.degraded or fallback


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
        spend.add(opener.usage, opener.cost_usd, fallback=opener.used_fallback)
        state.questions_asked = 1
        state.pending_question = opener.question.text
        state.pending_kind = "opener"
        state.pending_slot = None
        state.pending_options, state.pending_multi = [], True
        await self._repo.save_state(
            tenant_id, session_id, state, expected_version=1, usage=spend.usage, cost_usd=spend.cost
        )
        return self._result(session_id, state, "active", opener.question, notice=spend.degraded)

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
        changed: dict[str, tuple[list[str], list[str]]] = {}
        if extraction:
            outcome = apply_extraction(state, extraction, turn_no, target)
            applied = outcome.applied
            changed = {k: v for k, v in outcome.changed.items() if k not in state.clarified}
        flags = extraction.flags if extraction else Flags()
        if extraction and _unflagged_question(flags, applied, text):
            # the model often misses a question, or calls it off topic: answer it all the same
            flags = flags.model_copy(update={"asks_bot": missed_question(text)})
        # the last reply left this same question waiting, so the user has seen it twice already
        put_again = bool(state.last_turns) and state.last_turns[-1].question == question_text
        state.answers_received = turn_no
        state.push_turn(question_text, text)

        status, stop_reason, question = "active", None, None
        retry = extraction is None and state.failures < MAX_CONSECUTIVE_FAILURES
        if extraction is None and state.failures >= MAX_CONSECUTIVE_FAILURES:
            status, stop_reason = "fallback", "model_unavailable"
        elif extraction is None:
            spend.degraded = True
            question = Question(
                question_text,
                kind=question_kind,
                slot=target,
                options=tuple(state.pending_options),
                multi=state.pending_multi,
            )
        elif _pure_question(flags, applied, state) and put_again:
            # asked us something again instead of answering: answer, then move on, never repeat
            state.user_questions += 1
            _set_aside(state, target, question_kind)
            status, stop_reason, question = await self._next(
                state, config, False, labels, spend, flags.asks_bot, declined=True
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
            spend.add(result.usage, result.cost_usd, fallback=result.used_fallback)
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
        elif (
            question_kind == "revision"
            and not applied
            and not state.declines.get("revision_clarify")
        ):
            # "it is missing things" says nothing we can use: ask which part, once
            state.declines["revision_clarify"] = 1
            question = Question(REVISION_CLARIFY, kind="revision")
            state.pending_question, state.pending_kind = question.text, question.kind
            state.pending_slot, state.pending_options, state.pending_multi = None, [], True
        elif (
            flags.refuses
            and not applied
            and question_kind == "best_customer"
            and not state.declines.get("best_customer_overview")
        ):
            # they will not name a client: respect that, and ask only for the kind of client
            state.declines["best_customer_overview"] = 1
            result = await generate_followup(
                self._llm, config, state, question_text, question_kind, privacy=True, labels=labels
            )
            spend.add(result.usage, result.cost_usd, fallback=result.used_fallback)
            if result.model:
                spend.models["question"] = result.model
            self._record_question(state, result.question)
            question = result.question
        elif (flags.refuses or flags.unsure or is_unsure(text)) and not applied:
            # a firm no, or "I don't know": respect it, never push, move on to the next topic
            _set_aside(state, target, question_kind)
            status, stop_reason, question = await self._next(
                state, config, False, labels, spend, flags.asks_bot, declined=True
            )
        elif _declined(state, target, question_kind, text, applied):
            state.declines[target or question_kind] = 1  # ask once more, then move on
            result = await generate_reask(
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
                DECLINE_PREFIX,
                labels=labels,
            )
            spend.add(result.usage, result.cost_usd, fallback=result.used_fallback)
            if result.model:
                spend.models["question"] = result.model
            question = result.question
            state.pending_question, state.pending_options = question.text, list(question.options)
        elif changed:
            # they now say something different from what they said before: ask which is right
            slot_key, (before, now) = next(iter(changed.items()))
            state.clarified.append(slot_key)
            result = await generate_clarify(
                self._llm, config, state, slot_key, before, now, labels=labels
            )
            spend.add(result.usage, result.cost_usd, fallback=result.used_fallback)
            if result.model:
                spend.models["question"] = result.model
            q = result.question  # a clarification: no new question is counted
            state.pending_question, state.pending_kind, state.pending_slot = q.text, q.kind, q.slot
            state.pending_options, state.pending_multi = list(q.options), q.multi
            question = q
        else:
            # vague about the question but clear about something else: an answer, so move on
            told_more = any(
                state.confidence(k) >= COMPLETION_CONFIDENCE for k in applied if k != target
            )
            unanswered = not applied or (flags.vague and not told_more)
            if flags.asks_bot:
                state.user_questions += 1
            thread = _thread(extraction, state, question_kind, unanswered)
            status, stop_reason, question = await self._next(
                state, config, unanswered, labels, spend, flags.asks_bot, thread=thread
            )

        if status == "completed" and question is None and stop_reason != "user_finished":
            if flags.asks_bot:  # they asked as we finished: answer before we close
                closing = await generate_closing(
                    self._llm, config, state, flags.asks_bot, labels=labels
                )
                if closing is not None:
                    spend.add(closing.usage, closing.cost_usd)
                    spend.models["question"] = closing.model
                    question = closing.question

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
        return self._result(
            session_id, state, status, question, stop_reason, notice=spend.degraded, retry=retry
        )

    async def _next(
        self, state, config, previous_vague, labels, spend, asked="", declined=False, thread=None
    ):
        """Choose and write the next question. Returns (status, stop_reason, question)."""
        kind, question, user_asked = state.pending_kind, state.pending_question, asked
        if thread:  # follow the user where it teaches us about their customers
            result = await generate_thread_question(
                self._llm, config, state, thread.topic, thread.slot, asked=user_asked, labels=labels
            )
            if result is not None:
                spend.add(result.usage, result.cost_usd, fallback=result.used_fallback)
                spend.models["question"] = result.model
                self._record_question(state, result.question)
                state.thread_run += 1
                state.thread_topic, state.thread_slot = thread.topic, thread.slot
                return "active", None, result.question
        returning, state.thread_run = state.thread_run > 0, 0
        state.thread_topic, state.thread_slot = "", ""
        if kind in ("opener", "best_customer") and previous_vague and not state.followups.get(kind):
            state.followups[kind] = 1  # one follow-up, then move on
            result = await generate_followup(
                self._llm, config, state, question, kind, asked=user_asked, labels=labels
            )
            spend.add(result.usage, result.cost_usd, fallback=result.used_fallback)
            if result.model:
                spend.models["question"] = result.model
            self._record_question(state, result.question)
            return "active", None, result.question
        if not state.best_customer_asked and state.questions_asked + 1 >= state.best_customer_at:
            state.best_customer_asked = True
            result = await generate_best_customer(
                self._llm,
                config,
                state,
                asked=user_asked,
                declined=(question or True) if declined else False,
                labels=labels,
            )
            spend.add(result.usage, result.cost_usd, fallback=result.used_fallback)
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
            spend.add(result.usage, result.cost_usd, fallback=result.used_fallback)
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
            declined=(question or True) if declined else False,
            returning=returning,
            labels=labels,
        )
        spend.add(result.usage, result.cost_usd, fallback=result.used_fallback)
        if result.model:
            spend.models["question"] = result.model
        self._record_question(state, result.question)
        return "active", None, result.question

    @staticmethod
    def _probe_slot(state: SessionState, previous_vague: bool) -> str | None:
        """The slot just asked about, if the answer was vague or partial and worth a follow-up."""
        key = state.pending_slot
        slot = state.slot_def(key) if key and state.pending_kind == "slot" else None
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
        if question.slot and question.kind != "thread":  # a detour is the user's, not our topic
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
        return self._result(session_id, state, status, question, stop_reason, notice=spend.degraded)

    async def revise(self, tenant_id: str, session_id: str, text: str) -> TurnResult:
        """The company read the ICP description and says what is off. Reopen the finished chat and
        take the note as an answer, so the usual follow-ups run and the ICP is rebuilt after."""
        text = self._validate(text)
        record = await self._repo.get_session(tenant_id, session_id)
        if record.status != "completed":
            raise InvalidTransition("changes are asked for after the first ICP is complete")
        state = record.state
        state.finished, state.stop_reason = False, None
        state.pending_question, state.pending_kind = REVISION_QUESTION, "revision"
        state.pending_slot, state.pending_options, state.pending_multi = None, [], True
        await self._repo.save_state(
            tenant_id, session_id, state, expected_version=record.row_version, status="active"
        )
        await self._repo.add_feedback(tenant_id, session_id, type="revision_requested", new=text)
        return await self.answer(tenant_id, session_id, text)

    # -- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _result(
        session_id: str,
        state: SessionState,
        status: str,
        question: Question | None,
        stop_reason: str | None = None,
        *,
        notice: bool = False,
        retry: bool = False,
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
            notice=RETRY_NOTICE if retry else DEGRADED_NOTICE if notice else None,
        )
