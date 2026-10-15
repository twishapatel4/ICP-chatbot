"""PYTHONPATH=src python -m icp_chatbot.cli.smoke_live [--backend developer|vertex]

Calls every interview step once with its configured model and thinking setting, to confirm that
the model names, thinking levels and credentials really work (Phase 0 verification). Needs a real
API key or Google Cloud login, and costs a few cents."""

import argparse
import asyncio
import sys
from dataclasses import dataclass

from pydantic import BaseModel

from icp_chatbot.config import Settings, get_settings
from icp_chatbot.llm.factory import make_llm
from icp_chatbot.llm.types import LLM, LLMError, Usage
from icp_chatbot.versions import STEP_NAMES, ConfigVersionData, default_config


class Ping(BaseModel):
    ok: bool


@dataclass
class SmokeRow:
    step: str
    model: str
    ok: bool
    detail: str = ""
    usage: Usage = Usage()
    cost_usd: float = 0.0
    latency_ms: int = 0


async def run_smoke(llm: LLM, config: ConfigVersionData) -> list[SmokeRow]:
    rows = []
    for step in STEP_NAMES:
        cfg = config.step(step)
        try:
            result = await llm.generate(
                step=step, cfg=cfg, system="Reply with ok=true.", user="ping", schema=Ping
            )
        except LLMError as exc:
            rows.append(SmokeRow(step, cfg.model, False, str(exc)))
            continue
        rows.append(
            SmokeRow(step, cfg.model, True, "", result.usage, result.cost_usd, result.latency_ms)
        )
    return rows


def format_rows(rows: list[SmokeRow]) -> str:
    lines = []
    for r in rows:
        status = "OK  " if r.ok else "FAIL"
        extra = (
            f"{r.latency_ms} ms, {r.usage.total_tokens} tokens, ${r.cost_usd:.5f}"
            if r.ok
            else r.detail
        )
        lines.append(f"{status} {r.step:<10} {r.model:<24} {extra}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live smoke test of every model step")
    parser.add_argument("--backend", choices=["developer", "vertex"])
    args = parser.parse_args(argv)
    settings: Settings = get_settings()
    if args.backend:
        settings = settings.model_copy(update={"llm_backend": args.backend})
    rows = asyncio.run(run_smoke(make_llm(settings), default_config(settings)))
    print(format_rows(rows))
    return 0 if all(r.ok for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
