"""Database tables (spec section 8). turns, feedback_events and icp_versions are append-only."""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Plain JSON on SQLite (tests, local development), JSONB on Postgres.
JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    segment: Mapped[dict] = mapped_column(JsonType, default=dict)
    sharing_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InterviewSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    config_version: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(10), default="quick")
    status: Mapped[str] = mapped_column(String(12), default="active", index=True)
    state: Mapped[dict] = mapped_column(JsonType)
    row_version: Mapped[int] = mapped_column(Integer, default=1)  # optimistic locking
    question_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    thought_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    turn_no: Mapped[int] = mapped_column(Integer)
    question_text: Mapped[str] = mapped_column(Text)
    question_kind: Mapped[str] = mapped_column(String(20), default="")
    slot_target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answer_text: Mapped[str] = mapped_column(Text)
    extracted: Mapped[dict] = mapped_column(JsonType, default=dict)
    state_after: Mapped[dict] = mapped_column(JsonType, default=dict)
    step_models: Mapped[dict] = mapped_column(JsonType, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    thought_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IcpVersion(Base):
    __tablename__ = "icp_versions"
    __table_args__ = (UniqueConstraint("session_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    author: Mapped[str] = mapped_column(String(10))  # bot or company
    document: Mapped[dict] = mapped_column(JsonType)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_value: Mapped[dict | list | str | None] = mapped_column(JsonType, nullable=True)
    new_value: Mapped[dict | list | str | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConfigVersionRow(Base):
    __tablename__ = "config_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft/approved/live/retired
    author: Mapped[str] = mapped_column(String(64), default="")
    approver: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EnrichmentRun(Base):
    __tablename__ = "enrichment_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pages: Mapped[list] = mapped_column(JsonType, default=list)
    outcome: Mapped[str] = mapped_column(String(12))  # ok, partial, failed, blocked, skipped
    seed: Mapped[dict] = mapped_column(JsonType, default=dict)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def _forbid_update(mapper, connection, target):
    raise RuntimeError(f"{type(target).__name__} rows are append-only and cannot be updated")


for _model in (Turn, FeedbackEvent, IcpVersion):
    event.listen(_model, "before_update", _forbid_update)
