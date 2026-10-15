"""PYTHONPATH=src python -m icp_chatbot.cli.abandon [--hours 48]

Marks interviews with no activity for that long as abandoned and records where each user stopped.
Run it on a schedule (for example hourly)."""

import argparse
import asyncio
import sys
from datetime import timedelta

from icp_chatbot.config import get_settings
from icp_chatbot.maintenance import abandon_stale_sessions
from icp_chatbot.store.db import make_engine, make_session_factory
from icp_chatbot.store.models import utcnow
from icp_chatbot.store.repo import Repo


async def _run(hours: int) -> int:
    engine = make_engine(get_settings().database_url)
    try:
        repo = Repo(make_session_factory(engine))
        return await abandon_stale_sessions(repo, timedelta(hours=hours), utcnow())
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mark stale interviews as abandoned")
    parser.add_argument("--hours", type=int, default=get_settings().abandon_after_hours)
    args = parser.parse_args(argv)
    marked = asyncio.run(_run(args.hours))
    print(f"marked {marked} session(s) as abandoned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
