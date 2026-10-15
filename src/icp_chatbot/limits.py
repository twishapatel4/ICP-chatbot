"""Cost and abuse controls (spec section 12): daily session and token caps, and a per-tenant
request rate limit."""

import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime

from icp_chatbot.config import Settings
from icp_chatbot.store.models import utcnow
from icp_chatbot.store.repo import Repo


class BudgetExceeded(Exception):
    """A daily cap was reached. scope names which one."""

    def __init__(self, scope: str):
        super().__init__(f"daily limit reached: {scope}")
        self.scope = scope


class BudgetGuard:
    """Refuses to start new interviews once a daily cap is reached. A session already under way
    is never cut off, so a user is not stopped halfway."""

    def __init__(self, repo: Repo, settings: Settings, clock: Callable[[], datetime] = utcnow):
        self._repo = repo
        self._settings = settings
        self._clock = clock

    async def check_start(self, tenant_id: str) -> None:
        now = self._clock()
        if now.tzinfo is None:  # a naive clock is taken to be UTC
            now = now.replace(tzinfo=UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        s = self._settings
        sessions, tokens = await self._repo.usage_since(day_start, tenant_id)
        if sessions >= s.daily_session_cap_per_tenant:
            raise BudgetExceeded("tenant_sessions")
        if tokens >= s.daily_token_cap_per_tenant:
            raise BudgetExceeded("tenant_tokens")
        sessions, tokens = await self._repo.usage_since(day_start)
        if sessions >= s.global_daily_session_cap:
            raise BudgetExceeded("global_sessions")
        if tokens >= s.global_daily_token_cap:
            raise BudgetExceeded("global_tokens")


class RateLimiter:
    """Sliding one-minute window per key, kept in memory (one process)."""

    def __init__(self, max_per_minute: int, clock: Callable[[], float] = time.monotonic):
        self._max = max_per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= 60.0:
            hits.popleft()
        if len(hits) >= self._max:
            return False
        hits.append(now)
        return True
