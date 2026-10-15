"""PYTHONPATH=src python -m icp_chatbot.cli.metrics [--days 30]"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import timedelta

from icp_chatbot.config import get_settings
from icp_chatbot.metrics.queries import dashboard
from icp_chatbot.store.db import make_engine, make_session_factory
from icp_chatbot.store.models import utcnow


async def _run(days: int) -> dict:
    engine = make_engine(get_settings().database_url)
    try:
        return asdict(
            await dashboard(make_session_factory(engine), utcnow() - timedelta(days=days))
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the interview metrics as JSON")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(_run(args.days)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
