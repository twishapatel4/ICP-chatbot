"""The ICP scoring model and a reference scorer (spec section 6.2).

The builder emits a ScoringModel; LeadNest's matching service applies it to leads. score_lead is
the reference implementation of those semantics: it is what the tests pin down, and what the
matching service should reproduce.

    ICP score = sum(w_i * s_i), on a 0 to 100 scale, with the weights renormalized to sum to 1
    over the criteria that apply and for which the lead's data is known.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from icp_chatbot.domain.slots import Track, slot_map
from icp_chatbot.domain.state import ANY

RANGE_SLOTS = frozenset(
    {"company_size", "years_experience", "current_company_size", "graduation_year"}
)
# A graduation year one off is half a match, further off is none.
RANGE_TOLERANCE: dict[str, float] = {"graduation_year": 0.001}
DEFAULT_TOLERANCE = 0.2

Criteria = tuple[tuple[str, str, float, tuple[str, ...]], ...]

# (key, label, default weight, slots read). Illustrative defaults; users edit them.
SALES_CRITERIA: Criteria = (
    ("industry_fit", "Industry fit", 35.0, ("industries", "customer_segments")),
    ("company_size", "Company size", 25.0, ("company_size",)),
    ("buying_signals", "Buying signals", 15.0, ("buying_triggers",)),
    ("geographic_fit", "Geographic fit", 25.0, ("lead_geographies", "company_hq_geographies")),
)
HR_CRITERIA: Criteria = (
    ("role_fit", "Role fit", 25.0, ("target_functions", "target_roles")),
    ("experience_fit", "Experience fit", 20.0, ("years_experience",)),
    ("skills_fit", "Skills fit", 20.0, ("required_skills",)),
    (
        "education_fit", "Education fit", 15.0,
        ("education_level", "field_of_study", "graduation_year"),
    ),
    ("location_fit", "Location fit", 10.0, ("candidate_locations",)),
    ("background_fit", "Background fit", 10.0, ("current_industries", "current_company_size")),
)  # fmt: skip
CRITERIA_BY_TRACK: dict[Track, Criteria] = {"sales": SALES_CRITERIA, "hr": HR_CRITERIA}
CRITERIA_DEFS = SALES_CRITERIA  # kept for callers that predate tracks
WEIGHT_TOTAL = 100.0
EPSILON = 1e-6


class MatchRule(BaseModel):
    kind: Literal["list", "range"]
    primary: list[str] = Field(default_factory=list)  # list rules: full match scores 1
    secondary: list[str] = Field(default_factory=list)  # list rules: match scores 0.5
    min: float | None = None  # range rules: inside scores 1
    max: float | None = None
    tolerance: float = 0.2  # range rules: within this fraction outside scores 0.5


class Criterion(BaseModel):
    key: str
    label: str
    weight: float = Field(ge=0)
    reads: list[str]
    rules: dict[str, MatchRule] = Field(default_factory=dict)  # one rule per slot read
    requirement: Literal["required", "preferred"] = "preferred"
    applicable: bool = True


class Disqualifier(BaseModel):
    field: str
    operator: Literal["in", "not_in", "lt", "gt", "eq"]
    values: list[str]


class ScoringModel(BaseModel):
    criteria: list[Criterion]
    disqualifiers: list[Disqualifier] = Field(default_factory=list)


# -- parsing and matching ----------------------------------------------------------------------

_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)([kmb])?(?![a-z])")
_MULTIPLIER = {"k": 1e3, "m": 1e6, "b": 1e9}


def _numbers(text: str) -> list[float]:
    text = re.sub(r"(\d)\s*(million|mn)\b", r"\1m", text)
    text = re.sub(r"(\d)\s*(billion|bn)\b", r"\1b", text)
    text = re.sub(r"(\d)\s*thousand\b", r"\1k", text)
    return [
        float(m.group(1).replace(",", "")) * _MULTIPLIER.get(m.group(2) or "", 1.0)
        for m in _NUMBER.finditer(text)
    ]


def parse_range(text: str) -> tuple[float | None, float | None] | None:
    """Read a size or money range such as '50-500', '1,001-5,000', '$1M-$10M', '5,000+', '<$1M'."""
    t = text.lower()
    nums = _numbers(t)
    if not nums:
        return None
    if len(nums) == 1:
        if re.search(r"under|below|less than|up to|fewer than|at most|<", t):
            return (None, nums[0])
        if re.search(r"over|above|more than|at least|minimum|>|\+", t):
            return (nums[0], None)
        return (nums[0], nums[0])
    return (min(nums[0], nums[1]), max(nums[0], nums[1]))


def _midpoint(bounds: tuple[float | None, float | None]) -> float | None:
    lo, hi = bounds
    if lo is None:
        return hi
    if hi is None:
        return lo
    return (lo + hi) / 2


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def loose_equal(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)


def list_match(lead_values: list[str], rule: MatchRule) -> float:
    if any(loose_equal(v, p) for v in lead_values for p in rule.primary):
        return 1.0
    if any(loose_equal(v, p) for v in lead_values for p in rule.secondary):
        return 0.5
    return 0.0


def range_match(value: float, rule: MatchRule) -> float:
    lo, hi = rule.min, rule.max
    if lo is not None and value < lo:
        distance, reference = lo - value, abs(lo) or 1.0
    elif hi is not None and value > hi:
        distance, reference = value - hi, abs(hi) or 1.0
    else:
        return 1.0
    return 0.5 if distance <= rule.tolerance * reference else 0.0


def _slot_score(rule: MatchRule, lead_values: list[str]) -> float | None:
    """Score for one slot, or None when the lead's data cannot be read."""
    if rule.kind == "list":
        return list_match(lead_values, rule)
    for text in lead_values:
        bounds = parse_range(text)
        value = _midpoint(bounds) if bounds else None
        if value is not None:
            return range_match(value, rule)
    return None


# -- building and editing the model --------------------------------------------------------------


def build_rule(slot_key: str, values: list[str]) -> MatchRule | None:
    values = [v for v in values if v.strip()]
    if not values or any(v.strip().lower() == ANY.lower() for v in values):
        return None
    if slot_key in RANGE_SLOTS:
        ranges = [r for r in (parse_range(v) for v in values) if r]
        if ranges:
            lo = None if any(r[0] is None for r in ranges) else min(r[0] for r in ranges)
            hi = None if any(r[1] is None for r in ranges) else max(r[1] for r in ranges)
            return MatchRule(
                kind="range",
                min=lo,
                max=hi,
                tolerance=RANGE_TOLERANCE.get(slot_key, DEFAULT_TOLERANCE),
            )
    return MatchRule(kind="list", primary=values)


def _rules_for(reads: tuple[str, ...] | list[str], slot_values: dict[str, list[str]]):
    rules = {}
    for slot in reads:
        rule = build_rule(slot, slot_values.get(slot, []))
        if rule:
            rules[slot] = rule
    return rules


def default_scoring_model(
    slot_values: dict[str, list[str]],
    disqualifiers: list[Disqualifier] | None = None,
    track: Track = "sales",
) -> ScoringModel:
    criteria = []
    for key, label, weight, reads in CRITERIA_BY_TRACK[track]:
        rules = _rules_for(reads, slot_values)
        criteria.append(
            Criterion(
                key=key,
                label=label,
                weight=weight,
                reads=list(reads),
                rules=rules,
                applicable=bool(rules),
            )  # fmt: skip
        )
    return ScoringModel(criteria=criteria, disqualifiers=list(disqualifiers or []))


def refresh_model(model: ScoringModel, slot_values: dict[str, list[str]]) -> ScoringModel:
    """Rebuild rules and applicability after the ICP's values changed; keep weights, requirement
    flags and disqualifiers as the user set them."""
    criteria = []
    for c in model.criteria:
        rules = _rules_for(c.reads, slot_values)
        criteria.append(c.model_copy(update={"rules": rules, "applicable": bool(rules)}))
    return model.model_copy(update={"criteria": criteria})


def set_weights(model: ScoringModel, weights: dict[str, float]) -> ScoringModel:
    known = {c.key for c in model.criteria}
    unknown = set(weights) - known
    if unknown:
        raise ValueError(f"unknown criteria: {sorted(unknown)}")
    if any(w < 0 for w in weights.values()):
        raise ValueError("weights cannot be negative")
    criteria = [
        c.model_copy(update={"weight": weights.get(c.key, c.weight)}) for c in model.criteria
    ]
    total = sum(c.weight for c in criteria)
    if abs(total - WEIGHT_TOTAL) > EPSILON:
        raise ValueError(f"weights must sum to {WEIGHT_TOTAL:g}, got {total:g}")
    return model.model_copy(update={"criteria": criteria})


def set_requirement(model: ScoringModel, key: str, requirement: str) -> ScoringModel:
    if requirement not in ("required", "preferred"):
        raise ValueError(f"requirement must be required or preferred, got {requirement!r}")
    if key not in {c.key for c in model.criteria}:
        raise ValueError(f"unknown criterion: {key}")
    criteria = [
        c.model_copy(update={"requirement": requirement}) if c.key == key else c
        for c in model.criteria
    ]
    return model.model_copy(update={"criteria": criteria})


DISQUALIFIER_FIELDS = frozenset(
    {
        "industries", "lead_geographies", "company_hq_geographies", "company_size",
        "company_type", "candidate_locations", "current_industries", "current_company_size",
    }
)  # fmt: skip


def make_disqualifier(field_name: str, operator: str, values: list[str]) -> Disqualifier | None:
    """A valid rule, or None when the field, operator or values cannot be used."""
    values = [v.strip() for v in values if v and v.strip()]
    if field_name not in DISQUALIFIER_FIELDS or not values:
        return None
    if operator not in ("in", "not_in", "lt", "gt", "eq"):
        return None
    if operator in ("lt", "gt") and not _numbers(values[0].lower()):
        return None
    return Disqualifier(field=field_name, operator=operator, values=values)


def validate_model(model: ScoringModel, track: Track = "sales") -> list[str]:
    """Problems that make the model unusable; an empty list means it is valid."""
    known_slots = slot_map(track)
    errors: list[str] = []
    keys = [c.key for c in model.criteria]
    if len(keys) != len(set(keys)):
        errors.append("criterion keys must be unique")
    total = sum(c.weight for c in model.criteria)
    if abs(total - WEIGHT_TOTAL) > EPSILON:
        errors.append(f"weights sum to {total:g}, not {WEIGHT_TOTAL:g}")
    for c in model.criteria:
        for slot in c.reads:
            if slot not in known_slots:
                errors.append(f"{c.key} reads unknown slot {slot}")
        for slot, rule in c.rules.items():
            if slot not in c.reads:
                errors.append(f"{c.key} has a rule for {slot}, which it does not read")
            if rule.kind == "list" and not rule.primary:
                errors.append(f"{c.key}/{slot}: a list rule needs at least one primary value")
            if rule.kind == "range":
                if rule.min is None and rule.max is None:
                    errors.append(f"{c.key}/{slot}: a range rule needs a minimum or a maximum")
                elif rule.min is not None and rule.max is not None and rule.min > rule.max:
                    errors.append(f"{c.key}/{slot}: minimum is above maximum")
    for d in model.disqualifiers:
        if d.field not in known_slots:
            errors.append(f"disqualifier names unknown field {d.field}")
        if not d.values:
            errors.append(f"disqualifier on {d.field} has no values")
    return errors


# -- reference scorer ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreResult:
    score: float  # 0 to 100
    qualified: bool
    disqualified_by: str | None = None
    failed_required: list[str] = field(default_factory=list)
    completeness: float = 0.0  # share of applicable criteria that could be scored
    breakdown: dict[str, float] = field(default_factory=dict)  # criterion key -> s between 0 and 1


def _disqualifier_hit(d: Disqualifier, lead: dict[str, list[str]]) -> bool:
    values = lead.get(d.field)
    if not values:
        return False  # unknown data never disqualifies
    if d.operator in ("in", "not_in", "eq"):
        found = any(loose_equal(v, x) for v in values for x in d.values)
        return found if d.operator in ("in", "eq") else not found
    bounds = next((b for b in (parse_range(v) for v in values) if b), None)
    value = _midpoint(bounds) if bounds else None
    threshold = _numbers(d.values[0].lower())
    if value is None or not threshold:
        return False
    return value < threshold[0] if d.operator == "lt" else value > threshold[0]


def score_lead(model: ScoringModel, lead: dict[str, list[str]]) -> ScoreResult:
    for d in model.disqualifiers:
        if _disqualifier_hit(d, lead):
            return ScoreResult(0.0, False, disqualified_by=f"{d.field} {d.operator} {d.values}")

    applicable = [c for c in model.criteria if c.applicable and c.weight > 0]
    scored: list[tuple[Criterion, float]] = []
    for c in applicable:
        parts = [
            s
            for slot, rule in c.rules.items()
            if lead.get(slot) and (s := _slot_score(rule, lead[slot])) is not None
        ]
        if parts:
            scored.append((c, sum(parts) / len(parts)))

    known = {c.key for c, _ in scored}
    failed = []
    for c in applicable:
        if c.requirement == "required":
            s = next((s for k, s in scored if k.key == c.key), None)
            if s is None or s == 0.0:
                failed.append(c.key)
    total_weight = sum(c.weight for c, _ in scored)
    score = 100.0 * sum(c.weight * s for c, s in scored) / total_weight if total_weight > 0 else 0.0
    return ScoreResult(
        score=round(score, 4),
        qualified=not failed,
        failed_required=failed,
        completeness=len(known) / len(applicable) if applicable else 0.0,
        breakdown={c.key: s for c, s in scored},
    )
