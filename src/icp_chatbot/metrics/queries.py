"""Numbers for the dashboards (spec sections 9.2 and 13), computed from the database."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from icp_chatbot.icp.diff import edit_rate
from icp_chatbot.icp.document import ICPDocument
from icp_chatbot.store import models as m


def percentile(values: list[int], p: float) -> int:
    """Nearest-rank percentile; 0 for no data."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, round(p / 100 * len(ordered) + 0.4999999))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class Dashboard:
    sessions: int = 0
    completed: int = 0
    abandoned: int = 0
    fallback: int = 0
    completion_rate: float = 0.0
    mean_questions: float = 0.0  # over completed sessions
    mean_cost_usd: float = 0.0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    mean_edit_rate: float | None = None  # over sessions whose ICP was approved
    approved_sessions: int = 0
    dropoff_by_slot: dict[str, int] = field(default_factory=dict)
    field_edits: dict[str, int] = field(default_factory=dict)
    weight_edits: dict[str, int] = field(default_factory=dict)
    requirement_toggles: dict[str, int] = field(default_factory=dict)


def _count(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        if item:
            counts[item] = counts.get(item, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


async def dashboard(factory: async_sessionmaker[AsyncSession], since: datetime) -> Dashboard:
    async with factory() as s:
        sessions = list(
            (
                await s.scalars(
                    select(m.InterviewSession).where(m.InterviewSession.started_at >= since)
                )
            ).all()
        )
        ids = [x.id for x in sessions]
        if not ids:
            return Dashboard()
        latencies = list(
            (await s.scalars(select(m.Turn.latency_ms).where(m.Turn.session_id.in_(ids)))).all()
        )
        events = list(
            (
                await s.scalars(select(m.FeedbackEvent).where(m.FeedbackEvent.session_id.in_(ids)))
            ).all()
        )
        icps = list(
            (
                await s.scalars(
                    select(m.IcpVersion)
                    .where(m.IcpVersion.session_id.in_(ids))
                    .order_by(m.IcpVersion.version)
                )
            ).all()
        )

    by_status = _count(x.status for x in sessions)
    completed = [x for x in sessions if x.status == "completed"]
    approved_ids = {e.session_id for e in events if e.type == "approve"}
    rates = []
    for sid in approved_ids:
        versions = [i for i in icps if i.session_id == sid]
        bots = [i for i in versions if i.author == "bot"]
        if bots and versions:
            rates.append(
                edit_rate(
                    ICPDocument.model_validate(bots[-1].document),
                    ICPDocument.model_validate(versions[-1].document),
                )
            )
    return Dashboard(
        sessions=len(sessions),
        completed=len(completed),
        abandoned=by_status.get("abandoned", 0),
        fallback=by_status.get("fallback", 0),
        completion_rate=len(completed) / len(sessions),
        mean_questions=sum(x.question_count for x in completed) / len(completed)
        if completed
        else 0.0,
        mean_cost_usd=sum(x.cost_usd for x in sessions) / len(sessions),
        p50_latency_ms=percentile(latencies, 50),
        p95_latency_ms=percentile(latencies, 95),
        mean_edit_rate=sum(rates) / len(rates) if rates else None,
        approved_sessions=len(approved_ids),
        dropoff_by_slot=_count(e.field for e in events if e.type == "abandon"),
        field_edits=_count(e.field for e in events if e.type == "field_edit"),
        weight_edits=_count(e.field for e in events if e.type == "weight_edit"),
        requirement_toggles=_count(e.field for e in events if e.type == "requirement_toggle"),
    )
