"""Summarizing a run and deciding whether a candidate beats the live version (spec section 9.4)."""

from dataclasses import dataclass, field

from pydantic import BaseModel

from icp_chatbot.eval.replay import CaseResult

HIGHER_IS_BETTER = ("mean_f1", "completion_rate", "scoring_valid_rate")
LOWER_IS_BETTER = ("mean_questions", "mean_cost_usd")


class Summary(BaseModel):
    cases: int
    mean_f1: float
    completion_rate: float  # cases that reached a completed ICP
    scoring_valid_rate: float
    mean_questions: float
    mean_cost_usd: float
    violations: int  # forbidden values found, over all cases
    errors: int
    f1_by_tag: dict[str, float]


def summarize(results: list[CaseResult]) -> Summary:
    n = len(results) or 1

    def mean(values) -> float:
        values = list(values)
        return round(sum(values) / len(values), 6) if values else 0.0

    by_tag: dict[str, list[float]] = {}
    for r in results:
        for tag in r.tags:
            by_tag.setdefault(tag, []).append(r.f1)
    return Summary(
        cases=len(results),
        mean_f1=mean(r.f1 for r in results),
        completion_rate=sum(r.status == "completed" for r in results) / n,
        scoring_valid_rate=sum(r.scoring_valid for r in results) / n,
        mean_questions=mean(r.questions_asked for r in results),
        mean_cost_usd=mean(r.cost_usd for r in results),
        violations=sum(r.violations for r in results),
        errors=sum(r.status == "error" for r in results),
        f1_by_tag={tag: mean(v) for tag, v in sorted(by_tag.items())},
    )


@dataclass
class Verdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def compare(
    baseline: Summary, candidate: Summary, *, target: str = "mean_f1", margin: float = 0.02
) -> Verdict:
    """A candidate passes if it beats the baseline on the target metric, regresses on no other
    metric by more than the margin (a fraction), has no forbidden values and no broken cases."""
    reasons = []
    if candidate.violations:
        reasons.append(f"{candidate.violations} forbidden value(s) appeared in an ICP")
    if candidate.errors:
        reasons.append(f"{candidate.errors} case(s) failed to run")
    base, cand = baseline.model_dump(), candidate.model_dump()
    if target not in (*HIGHER_IS_BETTER, *LOWER_IS_BETTER):
        raise ValueError(f"unknown target metric {target}")
    better = (
        cand[target] > base[target] if target in HIGHER_IS_BETTER else cand[target] < base[target]
    )
    if not better:
        reasons.append(f"{target} did not improve ({base[target]} -> {cand[target]})")
    for metric in HIGHER_IS_BETTER:
        if metric != target and cand[metric] < base[metric] - margin:
            reasons.append(f"{metric} regressed ({base[metric]} -> {cand[metric]})")
    for metric in LOWER_IS_BETTER:
        if metric != target and cand[metric] > base[metric] * (1 + margin) + 1e-12:
            reasons.append(f"{metric} regressed ({base[metric]} -> {cand[metric]})")
    for tag, f1 in base["f1_by_tag"].items():
        if cand["f1_by_tag"].get(tag, 0.0) < f1 - margin:
            reasons.append(
                f"f1 for '{tag}' cases regressed ({f1} -> {cand['f1_by_tag'].get(tag, 0.0)})"
            )
    return Verdict(passed=not reasons, reasons=reasons)
