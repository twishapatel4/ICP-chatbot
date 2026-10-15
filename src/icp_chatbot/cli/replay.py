"""PYTHONPATH=src python -m icp_chatbot.cli.replay --cases eval_cases --out run.json [--baseline base.json]"""

import argparse
import asyncio
import sys

from icp_chatbot.config import get_settings
from icp_chatbot.eval.runner import run
from icp_chatbot.llm.factory import make_llm


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the evaluation cases against the real model"
    )
    parser.add_argument("--cases", default="eval_cases")
    parser.add_argument("--out")
    parser.add_argument("--baseline", help="a previous --out file to compare against")
    parser.add_argument("--target", default="mean_f1")
    parser.add_argument("--margin", type=float, default=0.02)
    args = parser.parse_args(argv)
    settings = get_settings()
    return asyncio.run(
        run(args.cases, make_llm(settings), settings, baseline_path=args.baseline,
            out_path=args.out, target=args.target, margin=args.margin)
    )  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
