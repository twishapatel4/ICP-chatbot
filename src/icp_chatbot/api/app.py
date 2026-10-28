"""The HTTP API (spec section 4, unit 1).

Authentication is not done here. LeadNest's gateway authenticates the user and sets the
X-Tenant-Id header; this service must not be reachable from the public internet.
"""

import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from icp_chatbot.api.schemas import (
    AnswerRequest,
    CardOut,
    FormFieldOut,
    FormRequest,
    ICPResponse,
    OptionOut,
    ProfileRequest,
    ProgressOut,
    QuestionOut,
    SessionCreated,
    SessionView,
    StartRequest,
    TranscribeResponse,
    TurnResponse,
    VerdictRequest,
)
from icp_chatbot.config import Settings, get_settings
from icp_chatbot.domain.slots import Track
from icp_chatbot.engine.fallback import apply_form, fallback_form, missing_required
from icp_chatbot.engine.interview import (
    InterviewEngine,
    InvalidAnswer,
    InvalidProfile,
    InvalidTransition,
    ProfileNotSubmitted,
    SessionClosed,
)
from icp_chatbot.engine.planner import enough_for_icp, interview_progress
from icp_chatbot.engine.profile_form import profile_cards
from icp_chatbot.engine.questions import Question
from icp_chatbot.engine.transcribe import normalize_mime, transcribe
from icp_chatbot.enrichment.fetcher import SafeFetcher
from icp_chatbot.enrichment.service import EnrichmentService
from icp_chatbot.icp.builder import build_icp
from icp_chatbot.icp.document import ICPDocument
from icp_chatbot.icp.review import Edit, apply_edit, approve, carry_over_edits
from icp_chatbot.limits import BudgetExceeded, BudgetGuard, RateLimiter
from icp_chatbot.linkedin.catalog import FACETS, default_catalog
from icp_chatbot.llm.factory import make_llm
from icp_chatbot.llm.types import LLM, LLMError
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.models import utcnow
from icp_chatbot.store.repo import Conflict, NotFound, Repo

TENANT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SHUTDOWN_READ_WAIT_S = 5.0  # website reads still running at shutdown get this long to finish


@dataclass
class Container:
    settings: Settings
    repo: Repo
    engine: InterviewEngine
    llm: LLM
    limiter: RateLimiter
    clock: Callable[[], datetime]


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLM | None = None,
    fetcher=None,
    db_engine=None,
    clock=utcnow,
) -> FastAPI:
    settings = settings or get_settings()
    db_engine = db_engine or make_engine(settings.database_url)
    repo = Repo(make_session_factory(db_engine), clock)
    llm = llm or make_llm(settings)
    fetcher = fetcher or SafeFetcher(timeout_s=settings.enrichment_timeout_seconds)
    enrichment = EnrichmentService(llm, fetcher, timeout_s=settings.enrichment_timeout_seconds)
    engine = InterviewEngine(
        repo=repo, llm=llm, enrichment=enrichment, settings=settings,
        guard=BudgetGuard(repo, settings, clock),
    )  # fmt: skip
    limiter = RateLimiter(settings.requests_per_minute_per_tenant)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_create_tables:
            await init_models(db_engine)
        yield
        await engine.drain(timeout=SHUTDOWN_READ_WAIT_S)
        await db_engine.dispose()

    app = FastAPI(title="ICP chatbot", lifespan=lifespan)
    app.state.container = Container(settings, repo, engine, llm, limiter, clock)

    # -- errors ------------------------------------------------------------------------------

    def problem(status: int, code: str):
        return JSONResponse(status_code=status, content={"detail": code})

    app.add_exception_handler(NotFound, lambda r, e: problem(404, "not_found"))
    app.add_exception_handler(SessionClosed, lambda r, e: problem(409, "session_closed"))
    app.add_exception_handler(ProfileNotSubmitted, lambda r, e: problem(409, "profile_incomplete"))
    app.add_exception_handler(  # the same body as the fallback form's 422
        InvalidProfile,
        lambda r, e: JSONResponse(
            status_code=422, content={"detail": {"missing_required": e.missing}}
        ),
    )
    app.add_exception_handler(InvalidTransition, lambda r, e: problem(409, f"not_allowed: {e}"))
    app.add_exception_handler(Conflict, lambda r, e: problem(409, "conflict"))
    app.add_exception_handler(InvalidAnswer, lambda r, e: problem(422, f"invalid_answer: {e}"))
    app.add_exception_handler(
        BudgetExceeded, lambda r, e: problem(429, f"limit_reached: {e.scope}")
    )

    # -- dependencies ----------------------------------------------------------------------------

    def tenant(request: Request, x_tenant_id: str | None = Header(default=None)) -> str:
        if not x_tenant_id or not TENANT_ID.match(x_tenant_id):
            raise HTTPException(401, "missing or invalid X-Tenant-Id")
        if not request.app.state.container.limiter.allow(x_tenant_id):
            raise HTTPException(429, "rate_limited")
        return x_tenant_id

    def c(request: Request) -> Container:
        return request.app.state.container

    def cards(track: Track) -> list[CardOut]:
        return [CardOut.of(card, track) for card in profile_cards(track)]

    # -- routes ------------------------------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/sessions", status_code=201, response_model=SessionCreated)
    async def start(
        body: StartRequest, tenant_id: str = Depends(tenant), ct: Container = Depends(c)
    ):
        """Step 1: the session and the step 2 cards. The first question comes from /profile."""
        session_id = await ct.engine.create(tenant_id, body.signup(), body.track)
        return SessionCreated(
            session_id=session_id, step="profile", track=body.track, cards=cards(body.track)
        )

    @app.get("/profile-form", response_model=list[CardOut])
    async def profile_form(track: Track = "sales", tenant_id: str = Depends(tenant)):
        return cards(track)

    @app.get("/options/{facet}", response_model=list[OptionOut])
    async def options(
        facet: str,
        q: str = Query("", max_length=100),
        limit: int = Query(500, ge=1, le=1000),
        tenant_id: str = Depends(tenant),
    ):
        """A facet's LinkedIn options, matching q if given, in catalog order otherwise. A facet
        whose catalog is not in place yet has none."""
        if facet not in FACETS:
            raise HTTPException(404, "unknown_facet")
        return [OptionOut.of(o) for o in default_catalog().search(facet, q, limit)]

    @app.post("/sessions/{session_id}/profile", response_model=TurnResponse)
    async def profile(
        session_id: str,
        body: ProfileRequest,
        tenant_id: str = Depends(tenant),
        ct: Container = Depends(c),
    ):
        """Step 2: the cards are checked and seed the session, then the chat begins."""
        return TurnResponse.of(await ct.engine.begin_chat(tenant_id, session_id, body.answers))

    @app.post("/sessions/{session_id}/answers", response_model=TurnResponse)
    async def answer(
        session_id: str,
        body: AnswerRequest,
        tenant_id: str = Depends(tenant),
        ct: Container = Depends(c),
    ):
        return TurnResponse.of(await ct.engine.answer(tenant_id, session_id, body.text))

    @app.post("/sessions/{session_id}/transcribe", response_model=TranscribeResponse)
    async def transcribe_answer(
        session_id: str,
        request: Request,
        tenant_id: str = Depends(tenant),
        ct: Container = Depends(c),
    ):
        """Voice answer, step 1: the raw audio is the request body. Returns the text only; the
        client sends it to /answers (after the user has looked at it, or at once)."""
        mime = normalize_mime(request.headers.get("content-type"))
        if mime is None:
            raise HTTPException(415, "unsupported_audio_type")
        limit = ct.settings.max_audio_bytes
        if int(request.headers.get("content-length") or 0) > limit:
            raise HTTPException(413, "audio_too_large")
        audio = bytearray()
        async for chunk in request.stream():  # content-length can lie: count what arrives
            audio += chunk
            if len(audio) > limit:
                raise HTTPException(413, "audio_too_large")
        if not audio:
            raise HTTPException(422, "empty_audio")
        record = await ct.repo.get_session(tenant_id, session_id)  # 404 for another tenant's
        if record.state.step == "profile":
            raise ProfileNotSubmitted()
        if record.status != "active":
            raise SessionClosed()
        try:
            text = await transcribe(ct.llm, ct.settings, bytes(audio), mime)
        except LLMError:
            raise HTTPException(502, "transcription_failed") from None
        return TranscribeResponse(text=text)

    @app.post("/sessions/{session_id}/detail", response_model=TurnResponse)
    async def detail(session_id: str, tenant_id: str = Depends(tenant), ct: Container = Depends(c)):
        return TurnResponse.of(await ct.engine.request_detail(tenant_id, session_id))

    @app.post("/sessions/{session_id}/revise", response_model=TurnResponse)
    async def revise(
        session_id: str,
        body: AnswerRequest,
        tenant_id: str = Depends(tenant),
        ct: Container = Depends(c),
    ):
        return TurnResponse.of(await ct.engine.revise(tenant_id, session_id, body.text))

    @app.post("/sessions/{session_id}/finish", response_model=TurnResponse)
    async def finish(session_id: str, tenant_id: str = Depends(tenant), ct: Container = Depends(c)):
        return TurnResponse.of(await ct.engine.finish(tenant_id, session_id))

    @app.get("/sessions/{session_id}", response_model=SessionView)
    async def view(session_id: str, tenant_id: str = Depends(tenant), ct: Container = Depends(c)):
        record = await ct.repo.get_session(tenant_id, session_id)
        state = record.state
        pending = None
        if record.status == "active" and state.pending_question:
            pending = QuestionOut.of(
                Question(
                    state.pending_question,
                    state.pending_kind,
                    state.pending_slot,
                    tuple(state.pending_options),
                    state.pending_multi,
                )
            )
        in_profile = state.step == "profile"  # resuming lands on step 2, filled in as left
        return SessionView(
            session_id=session_id,
            status=record.status,
            mode=record.mode,
            questions_asked=state.questions_asked,
            question=pending,
            stop_reason=state.stop_reason,
            has_icp=await ct.repo.latest_icp(tenant_id, session_id) is not None,
            progress=ProgressOut.of(interview_progress(state)),
            step=state.step,
            track=state.track,
            cards=cards(state.track) if in_profile else None,
            answers=dict(state.profile_answers) if in_profile else None,
        )

    @app.get("/fallback-form", response_model=list[FormFieldOut])
    async def form(track: Track = "sales", tenant_id: str = Depends(tenant)):
        return [FormFieldOut.of(f) for f in fallback_form(track)]

    @app.post("/sessions/{session_id}/fallback", response_model=TurnResponse)
    async def submit_form(
        session_id: str,
        body: FormRequest,
        tenant_id: str = Depends(tenant),
        ct: Container = Depends(c),
    ):
        record = await ct.repo.get_session(tenant_id, session_id)
        if record.status not in ("active", "fallback"):
            raise SessionClosed(record.status)
        state = record.state
        missing = missing_required(body.answers, state)
        if missing:
            raise HTTPException(422, {"missing_required": missing})
        try:
            apply_form(state, body.answers, state.answers_received + 1)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        state.finished, state.stop_reason = True, "form_submitted"
        state.step = "chat"  # a form sent instead of step 2 covers it too
        state.pending_question, state.pending_kind, state.pending_slot = "", "", None
        await ct.repo.save_state(
            tenant_id, session_id, state, expected_version=record.row_version, status="completed"
        )
        await ct.repo.add_feedback(tenant_id, session_id, type="form_submitted")
        return TurnResponse(
            session_id=session_id, status="completed", mode=state.mode,
            questions_asked=state.questions_asked, stop_reason="form_submitted", icp_ready=True,
        )  # fmt: skip

    @app.post("/sessions/{session_id}/icp", response_model=ICPResponse)
    async def build(session_id: str, tenant_id: str = Depends(tenant), ct: Container = Depends(c)):
        record = await ct.repo.get_session(tenant_id, session_id)
        if record.state.step == "profile":
            raise ProfileNotSubmitted()
        if record.status != "completed":
            raise HTTPException(409, "not_ready")
        # too little to go on still gets a profile: the user asked to stop, so show what they gave
        # and let the gap notes say what is missing (QA 2026-10-27: a refusal showed nothing)
        partial = not enough_for_icp(record.state)
        latest = await ct.repo.latest_icp(tenant_id, session_id)
        bot = await ct.repo.latest_icp(tenant_id, session_id, author="bot")
        if bot and bot.document.get("built_at_turn") == record.state.answers_received:
            return ICPResponse(
                session_id=session_id,
                version=latest.version,
                author=latest.author,
                document=ICPDocument.model_validate(latest.document),
            )
        config = await ct.repo.get_config(record.config_version)
        labels = {"session_id": session_id, "turn": "final", "config_version": str(config.version)}
        today = ct.clock().date()
        built = await build_icp(ct.llm, config, record.state, today=today, labels=labels)
        document = built.document.model_copy(
            update={"built_at_turn": record.state.answers_received, "partial": partial}
        )
        if latest:  # a rebuild (for example after detail mode) must not discard the company's edits
            document = carry_over_edits(document, ICPDocument.model_validate(latest.document))
        await ct.repo.save_state(
            tenant_id, session_id, record.state, expected_version=record.row_version,
            usage=built.usage, cost_usd=built.cost_usd,
        )  # fmt: skip
        version = await ct.repo.add_icp_version(
            tenant_id, session_id, document=document.model_dump(mode="json"),
            summary=document.summary, author="bot",
        )  # fmt: skip
        return ICPResponse(session_id=session_id, version=version, author="bot", document=document)

    @app.get("/sessions/{session_id}/icp", response_model=ICPResponse)
    async def current_icp(
        session_id: str, tenant_id: str = Depends(tenant), ct: Container = Depends(c)
    ):
        latest = await ct.repo.latest_icp(tenant_id, session_id)
        if latest is None:
            raise NotFound("icp")
        return ICPResponse(
            session_id=session_id,
            version=latest.version,
            author=latest.author,
            document=ICPDocument.model_validate(latest.document),
        )

    async def _save_company_version(ct, tenant_id, session_id, document, specs):
        version = await ct.repo.add_icp_version(
            tenant_id, session_id, document=document.model_dump(mode="json"),
            summary=document.summary, author="company",
        )  # fmt: skip
        for spec in specs:
            await ct.repo.add_feedback(
                tenant_id, session_id, type=spec.type, field=spec.field, old=spec.old, new=spec.new
            )
        return ICPResponse(
            session_id=session_id, version=version, author="company", document=document
        )

    @app.patch("/sessions/{session_id}/icp", response_model=ICPResponse)
    async def edit_icp(
        session_id: str, edit: Edit, tenant_id: str = Depends(tenant), ct: Container = Depends(c)
    ):
        latest = await ct.repo.latest_icp(tenant_id, session_id)
        if latest is None:
            raise NotFound("icp")
        try:
            document, specs = apply_edit(
                ICPDocument.model_validate(latest.document), edit, ct.clock().date()
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return await _save_company_version(ct, tenant_id, session_id, document, specs)

    @app.post("/sessions/{session_id}/approve", response_model=ICPResponse)
    async def approve_icp(
        session_id: str, tenant_id: str = Depends(tenant), ct: Container = Depends(c)
    ):
        latest = await ct.repo.latest_icp(tenant_id, session_id)
        if latest is None:
            raise NotFound("icp")
        try:
            document, specs = approve(
                ICPDocument.model_validate(latest.document), ct.clock().date()
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return await _save_company_version(ct, tenant_id, session_id, document, specs)

    @app.post("/sessions/{session_id}/verdict", status_code=204)
    async def verdict(
        session_id: str,
        body: VerdictRequest,
        tenant_id: str = Depends(tenant),
        ct: Container = Depends(c),
    ):
        """The company's judgement of the ICP as a whole: correct, or what is wrong with it."""
        latest = await ct.repo.latest_icp(tenant_id, session_id)
        if latest is None:
            raise NotFound("icp")
        await ct.repo.add_feedback(
            tenant_id,
            session_id,
            type="icp_verdict",
            old=latest.version,
            new={"correct": body.correct, "comment": body.comment.strip()},
        )

    return app
