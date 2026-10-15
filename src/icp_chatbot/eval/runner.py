"""Runs an evaluation: replay every case, summarize, optionally compare with a saved baseline."""

import json
from dataclasses import asdict
from pathlib import Path

from icp_chatbot.config import Settings
from icp_chatbot.eval.cases import load_cases
from icp_chatbot.eval.replay import replay_all
from icp_chatbot.eval.report import Summary, compare, summarize
from icp_chatbot.llm.types import LLM
from icp_chatbot.versions import ConfigVersionData


async def run(
    cases_dir: str | Path,
    llm: LLM,
    settings: Settings,
    *,
    config: ConfigVersionData | None = None,
    baseline_path: str | Path | None = None,
    out_path: str | Path | None = None,
    target: str = "mean_f1",
    margin: float = 0.02,
) -> int:
    """Returns a process exit code: 0 when the run is acceptable, 1 otherwise."""
    cases = load_cases(cases_dir)
    results = await replay_all(cases, llm, settings, config)
    summary = summarize(results)
    if out_path:
        payload = {"summary": summary.model_dump(), "results": [asdict(r) for r in results]}
        Path(out_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"cases={summary.cases} mean_f1={summary.mean_f1:.3f} completion={summary.completion_rate:.2f} "
          f"questions={summary.mean_questions:.1f} cost=${summary.mean_cost_usd:.4f} "
          f"violations={summary.violations} errors={summary.errors}")  # fmt: skip
    for r in results:
        if r.status == "error" or r.violations:
            print(f"  ! {r.name}: status={r.status} violations={r.violations} {r.errors}")

    if baseline_path:
        baseline = Summary.model_validate(json.loads(Path(baseline_path).read_text())["summary"])
        verdict = compare(baseline, summary, target=target, margin=margin)
        print("PASS: the candidate beats the baseline" if verdict.passed else "FAIL:")
        for reason in verdict.reasons:
            print(f"  - {reason}")
        return 0 if verdict.passed else 1
    return 0 if summary.violations == 0 and summary.errors == 0 else 1
