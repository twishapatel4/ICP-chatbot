"""Housekeeping jobs, run on a schedule."""

import logging
from datetime import UTC, datetime, timedelta

from icp_chatbot.store.repo import Conflict, NotFound, Repo

log = logging.getLogger(__name__)


async def abandon_stale_sessions(repo: Repo, older_than: timedelta, now: datetime) -> int:
    """Mark active sessions that were not updated for a while as abandoned and record where the
    user stopped (the slot the last question was about). Returns how many were marked."""
    if now.tzinfo is None:  # a naive moment is taken to be UTC
        now = now.replace(tzinfo=UTC)
    marked = 0
    for tenant_id, session_id in await repo.list_stale_active(now - older_than):
        try:
            record = await repo.get_session(tenant_id, session_id)
            if record.status != "active":
                continue
            await repo.save_state(
                tenant_id, session_id, record.state,
                expected_version=record.row_version, status="abandoned",
            )  # fmt: skip
            await repo.add_feedback(
                tenant_id,
                session_id,
                type="abandon",
                field=record.state.pending_slot or record.state.pending_kind or None,
                old=record.state.questions_asked,
            )
            marked += 1
        except (Conflict, NotFound):
            log.info("session %s changed while being abandoned; skipped", session_id)
    return marked
