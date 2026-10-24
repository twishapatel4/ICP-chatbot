"""All database access. Every per-session method takes the tenant id and refuses sessions that
belong to another tenant (spec section 11.4)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from icp_chatbot.domain.state import SessionState
from icp_chatbot.llm.types import Usage
from icp_chatbot.store import models as m
from icp_chatbot.versions import ConfigVersionData


class NotFound(Exception):
    """The session does not exist, or belongs to a different tenant."""


class Conflict(Exception):
    """The session changed since it was read (for example two answers submitted at once)."""


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; everything is stored as UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class SessionRecord:
    id: str
    tenant_id: str
    config_version: int
    mode: str
    status: str
    state: SessionState
    row_version: int
    question_count: int
    usage: Usage
    cost_usd: float
    started_at: datetime
    ended_at: datetime | None


@dataclass
class TurnRecord:
    turn_no: int
    question_text: str
    question_kind: str
    slot_target: str | None
    answer_text: str
    extracted: dict
    latency_ms: int
    usage: Usage
    cost_usd: float


@dataclass
class IcpRecord:
    id: int
    session_id: str
    version: int
    author: str
    document: dict
    summary: str
    created_at: datetime


@dataclass
class FeedbackRecord:
    type: str
    field: str | None
    old_value: object
    new_value: object
    created_at: datetime


@dataclass
class EnrichmentRecord:
    website: str | None
    pages: list
    outcome: str
    seed: dict
    duration_ms: int


class Repo:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] = m.utcnow,
    ):
        self._factory = factory
        self._clock = clock

    # -- tenants and sessions -------------------------------------------------------------

    async def ensure_tenant(self, tenant_id: str, name: str = "") -> None:
        try:
            async with self._factory() as s, s.begin():
                if await s.get(m.Tenant, tenant_id) is None:
                    s.add(m.Tenant(id=tenant_id, name=name, created_at=self._clock()))
        except IntegrityError:  # a session for the same new tenant started at the same moment
            pass

    async def create_session(self, tenant_id: str, config_version: int, state: SessionState) -> str:
        session_id = uuid4().hex
        now = self._clock()
        async with self._factory() as s, s.begin():
            s.add(
                m.InterviewSession(
                    id=session_id,
                    tenant_id=tenant_id,
                    config_version=config_version,
                    mode=state.mode,
                    status="active",
                    state=state.model_dump(mode="json"),
                    question_count=state.questions_asked,
                    started_at=now,
                    updated_at=now,
                )
            )
        return session_id

    @staticmethod
    async def _owned(s: AsyncSession, tenant_id: str, session_id: str) -> m.InterviewSession:
        row = await s.get(m.InterviewSession, session_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFound(session_id)
        return row

    @staticmethod
    def _record(row: m.InterviewSession) -> SessionRecord:
        return SessionRecord(
            id=row.id,
            tenant_id=row.tenant_id,
            config_version=row.config_version,
            mode=row.mode,
            status=row.status,
            state=SessionState.model_validate(row.state),
            row_version=row.row_version,
            question_count=row.question_count,
            usage=Usage(row.input_tokens, row.output_tokens, row.thought_tokens, 0),
            cost_usd=row.cost_usd,
            started_at=_aware(row.started_at),
            ended_at=_aware(row.ended_at),
        )

    async def get_session(self, tenant_id: str, session_id: str) -> SessionRecord:
        async with self._factory() as s:
            return self._record(await self._owned(s, tenant_id, session_id))

    async def save_state(
        self,
        tenant_id: str,
        session_id: str,
        state: SessionState,
        *,
        expected_version: int,
        status: str | None = None,
        usage: Usage | None = None,
        cost_usd: float = 0.0,
    ) -> int:
        """Save the state if nobody else has since the caller read it. Returns the new version."""
        usage = usage or Usage()
        now = self._clock()
        values: dict = {
            "state": state.model_dump(mode="json"),
            "mode": state.mode,
            "question_count": state.questions_asked,
            "row_version": expected_version + 1,
            "updated_at": now,
            "input_tokens": m.InterviewSession.input_tokens + usage.input_tokens,
            "output_tokens": m.InterviewSession.output_tokens + usage.output_tokens,
            "thought_tokens": m.InterviewSession.thought_tokens + usage.thought_tokens,
            "cost_usd": m.InterviewSession.cost_usd + cost_usd,
        }
        if status is not None:
            values["status"] = status
            values["ended_at"] = now if status != "active" else None
        async with self._factory() as s, s.begin():
            await self._owned(s, tenant_id, session_id)
            result = await s.execute(
                update(m.InterviewSession)
                .where(
                    m.InterviewSession.id == session_id,
                    m.InterviewSession.tenant_id == tenant_id,
                    m.InterviewSession.row_version == expected_version,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise Conflict(session_id)
        return expected_version + 1

    # -- turns, ICP versions, feedback -----------------------------------------------------

    async def add_turn(
        self,
        tenant_id: str,
        session_id: str,
        *,
        turn_no: int,
        question_text: str,
        question_kind: str,
        slot_target: str | None,
        answer_text: str,
        extracted: dict,
        state_after: dict,
        step_models: dict,
        latency_ms: int,
        usage: Usage,
        cost_usd: float,
    ) -> None:
        async with self._factory() as s, s.begin():
            await self._owned(s, tenant_id, session_id)
            s.add(
                m.Turn(
                    session_id=session_id,
                    turn_no=turn_no,
                    question_text=question_text,
                    question_kind=question_kind,
                    slot_target=slot_target,
                    answer_text=answer_text,
                    extracted=extracted,
                    state_after=state_after,
                    step_models=step_models,
                    latency_ms=latency_ms,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    thought_tokens=usage.thought_tokens,
                    cost_usd=cost_usd,
                    created_at=self._clock(),
                )
            )

    async def list_turns(self, tenant_id: str, session_id: str) -> list[TurnRecord]:
        async with self._factory() as s:
            await self._owned(s, tenant_id, session_id)
            rows = await s.scalars(
                select(m.Turn).where(m.Turn.session_id == session_id).order_by(m.Turn.turn_no)
            )
            return [
                TurnRecord(
                    r.turn_no, r.question_text, r.question_kind, r.slot_target, r.answer_text,
                    r.extracted, r.latency_ms,
                    Usage(r.input_tokens, r.output_tokens, r.thought_tokens, 0), r.cost_usd,
                )
                for r in rows
            ]  # fmt: skip

    async def add_icp_version(
        self, tenant_id: str, session_id: str, *, document: dict, summary: str, author: str
    ) -> int:
        async with self._factory() as s, s.begin():
            await self._owned(s, tenant_id, session_id)
            current = await s.scalar(
                select(func.max(m.IcpVersion.version)).where(m.IcpVersion.session_id == session_id)
            )
            version = (current or 0) + 1
            s.add(
                m.IcpVersion(
                    session_id=session_id,
                    version=version,
                    author=author,
                    document=document,
                    summary=summary,
                    created_at=self._clock(),
                )
            )
        return version

    @staticmethod
    def _icp(r: m.IcpVersion) -> IcpRecord:
        return IcpRecord(
            r.id, r.session_id, r.version, r.author, r.document, r.summary, _aware(r.created_at)
        )

    async def latest_icp(
        self, tenant_id: str, session_id: str, author: str | None = None
    ) -> IcpRecord | None:
        async with self._factory() as s:
            await self._owned(s, tenant_id, session_id)
            query = select(m.IcpVersion).where(m.IcpVersion.session_id == session_id)
            if author:
                query = query.where(m.IcpVersion.author == author)
            row = await s.scalar(query.order_by(m.IcpVersion.version.desc()).limit(1))
            return self._icp(row) if row else None

    async def list_icps(self, tenant_id: str, session_id: str) -> list[IcpRecord]:
        async with self._factory() as s:
            await self._owned(s, tenant_id, session_id)
            rows = await s.scalars(
                select(m.IcpVersion)
                .where(m.IcpVersion.session_id == session_id)
                .order_by(m.IcpVersion.version)
            )
            return [self._icp(r) for r in rows]

    async def add_feedback(
        self,
        tenant_id: str,
        session_id: str,
        *,
        type: str,
        field: str | None = None,
        old=None,
        new=None,
    ) -> None:
        async with self._factory() as s, s.begin():
            await self._owned(s, tenant_id, session_id)
            s.add(
                m.FeedbackEvent(
                    session_id=session_id,
                    type=type,
                    field=field,
                    old_value=old,
                    new_value=new,
                    created_at=self._clock(),
                )
            )

    async def list_feedback(self, tenant_id: str, session_id: str) -> list[FeedbackRecord]:
        async with self._factory() as s:
            await self._owned(s, tenant_id, session_id)
            rows = await s.scalars(
                select(m.FeedbackEvent)
                .where(m.FeedbackEvent.session_id == session_id)
                .order_by(m.FeedbackEvent.id)
            )
            return [
                FeedbackRecord(r.type, r.field, r.old_value, r.new_value, _aware(r.created_at))
                for r in rows
            ]

    async def save_enrichment(
        self,
        tenant_id: str,
        session_id: str,
        *,
        website: str | None,
        pages: list[str],
        outcome: str,
        seed: dict,
        duration_ms: int,
        usage: Usage,
    ) -> None:
        async with self._factory() as s, s.begin():
            await self._owned(s, tenant_id, session_id)
            s.add(
                m.EnrichmentRun(
                    session_id=session_id,
                    website=website,
                    pages=pages,
                    outcome=outcome,
                    seed=seed,
                    duration_ms=duration_ms,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    created_at=self._clock(),
                )
            )

    async def cached_enrichment(self, website: str, max_age_days: int) -> EnrichmentRecord | None:
        """The newest read of this website within max_age_days that produced data, from any
        session: a public site gives the same seed again, so it need not be read twice. Reads
        that failed, were blocked or found nothing are not reused, so those are tried again."""
        if max_age_days <= 0:
            return None
        oldest = self._clock() - timedelta(days=max_age_days)
        async with self._factory() as s:
            rows = await s.scalars(
                select(m.EnrichmentRun)
                .where(
                    m.EnrichmentRun.website == website,
                    m.EnrichmentRun.outcome.in_(("ok", "partial")),
                    m.EnrichmentRun.created_at >= oldest,
                )
                .order_by(m.EnrichmentRun.id.desc())
            )
            for row in rows:
                if row.seed:
                    return EnrichmentRecord(
                        row.website, row.pages, row.outcome, row.seed, row.duration_ms
                    )
        return None

    async def list_enrichment(self, tenant_id: str, session_id: str) -> list[EnrichmentRecord]:
        async with self._factory() as s:
            await self._owned(s, tenant_id, session_id)
            rows = await s.scalars(
                select(m.EnrichmentRun)
                .where(m.EnrichmentRun.session_id == session_id)
                .order_by(m.EnrichmentRun.id)
            )
            return [
                EnrichmentRecord(r.website, r.pages, r.outcome, r.seed, r.duration_ms) for r in rows
            ]

    # -- configuration versions -------------------------------------------------------------

    async def live_config(self) -> ConfigVersionData | None:
        async with self._factory() as s:
            row = await s.scalar(
                select(m.ConfigVersionRow)
                .where(m.ConfigVersionRow.status == "live")
                .order_by(m.ConfigVersionRow.version.desc())
                .limit(1)
            )
            return ConfigVersionData.model_validate(row.data).with_code_prompts() if row else None

    async def get_config(self, version: int) -> ConfigVersionData:
        async with self._factory() as s:
            row = await s.get(m.ConfigVersionRow, version)
            if row is None:
                raise NotFound(f"config version {version}")
            return ConfigVersionData.model_validate(row.data).with_code_prompts()

    async def publish_config(self, data: ConfigVersionData, *, author: str) -> None:
        """Make this version live and retire the previously live one."""
        async with self._factory() as s, s.begin():
            await s.execute(
                update(m.ConfigVersionRow)
                .where(m.ConfigVersionRow.status == "live")
                .values(status="retired")
            )
            s.add(
                m.ConfigVersionRow(
                    version=data.version,
                    data=data.model_dump(mode="json"),
                    status="live",
                    author=author,
                    created_at=self._clock(),
                )
            )

    async def delete_tenant(self, tenant_id: str) -> int:
        """Delete a tenant and everything stored from its sessions (spec section 11.4).
        Returns the number of sessions removed. Other tenants are untouched."""
        async with self._factory() as s, s.begin():
            ids = list(
                (
                    await s.scalars(
                        select(m.InterviewSession.id).where(
                            m.InterviewSession.tenant_id == tenant_id
                        )
                    )
                ).all()
            )
            if ids:
                for model in (m.Turn, m.IcpVersion, m.FeedbackEvent, m.EnrichmentRun):
                    await s.execute(delete(model).where(model.session_id.in_(ids)))
                await s.execute(delete(m.InterviewSession).where(m.InterviewSession.id.in_(ids)))
            await s.execute(delete(m.Tenant).where(m.Tenant.id == tenant_id))
        return len(ids)

    # -- limits and maintenance -------------------------------------------------------------

    async def usage_since(self, since: datetime, tenant_id: str | None = None) -> tuple[int, int]:
        """(sessions started, tokens used) since a moment, for one tenant or for everyone."""
        query = select(
            func.count(m.InterviewSession.id),
            func.coalesce(
                func.sum(
                    m.InterviewSession.input_tokens
                    + m.InterviewSession.output_tokens
                    + m.InterviewSession.thought_tokens
                ),
                0,
            ),
        ).where(m.InterviewSession.started_at >= since)
        if tenant_id:
            query = query.where(m.InterviewSession.tenant_id == tenant_id)
        async with self._factory() as s:
            sessions, tokens = (await s.execute(query)).one()
            return int(sessions), int(tokens)

    async def list_stale_active(self, cutoff: datetime) -> list[tuple[str, str]]:
        """(tenant id, session id) of active sessions not updated since the cutoff.

        Cross-tenant on purpose: used only by the maintenance job.
        """
        async with self._factory() as s:
            rows = await s.execute(
                select(m.InterviewSession.tenant_id, m.InterviewSession.id).where(
                    m.InterviewSession.status == "active",
                    m.InterviewSession.updated_at < cutoff,
                )
            )
            return [(t, i) for t, i in rows.all()]
