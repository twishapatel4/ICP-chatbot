"""The ICP scoring model and a reference scorer (spec section 6.2).

The builder emits a ScoringModel; LeadNest's matching service applies it to leads. score_lead is
the reference implementation of those semantics: it is what the tests pin down, and what the
matching service should reproduce.

    ICP score = sum(w_i * s_i), on a 0 to 100 scale, with the weights renormalized to sum to 1
    over the criteria that apply and for which the lead's data is known.
"""

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from icp_chatbot.domain.slots import Track, slot_map
from icp_chatbot.domain.state import ANY
from icp_chatbot.linkedin.catalog import SLOT_FACETS, default_catalog

RANGE_SLOTS = frozenset(
    {
        "company_size", "years_experience", "current_company_size", "graduation_year",
        "years_in_operation",
    }
)  # fmt: skip
# A graduation year one off is half a match, further off is none.
RANGE_TOLERANCE: dict[str, float] = {"graduation_year": 0.001}
DEFAULT_TOLERANCE = 0.2

Criteria = tuple[tuple[str, str, float, tuple[str, ...]], ...]

# (key, label, default weight, slots read). Illustrative defaults; users edit them.
# Criteria on a derived slot (guessed from a job title) weigh 5 at most.
SALES_CRITERIA: Criteria = (
    ("industry_fit", "Industry fit", 25.0, ("industries", "customer_segments")),
    ("company_size", "Company size", 15.0, ("company_size",)),
    ("company_type_fit", "Company type fit", 5.0, ("company_type",)),
    ("company_age_fit", "Company age fit", 5.0, ("years_in_operation",)),
    ("buying_signals", "Buying signals", 10.0, ("buying_triggers",)),
    ("geographic_fit", "Geographic fit", 20.0, ("lead_geographies", "company_hq_geographies")),
    ("buyer_fit", "Buyer fit", 10.0, ("buyer_roles",)),
    ("seniority_fit", "Seniority fit", 5.0, ("buyer_seniority",)),
    ("department_fit", "Department fit", 5.0, ("buyer_departments",)),
)
HR_CRITERIA: Criteria = (
    ("role_fit", "Role fit", 20.0, ("target_roles",)),
    ("function_fit", "Function fit", 5.0, ("target_functions",)),
    ("seniority_fit", "Seniority fit", 5.0, ("target_seniority",)),
    ("experience_fit", "Experience fit", 15.0, ("years_experience",)),
    ("skills_fit", "Skills fit", 15.0, ("required_skills", "nice_to_have_skills")),
    ("certifications_fit", "Certifications fit", 5.0, ("certifications",)),
    (
        "education_fit", "Education fit", 10.0,
        ("education_level", "field_of_study", "graduation_year"),
    ),
    ("school_fit", "School fit", 5.0, ("target_schools",)),
    ("location_fit", "Location fit", 10.0, ("candidate_locations",)),
    ("background_fit", "Background fit", 5.0, ("current_industries", "current_company_size")),
    ("employer_fit", "Employer fit", 5.0, ("source_companies",)),
)  # fmt: skip
# Criteria whose second read slot is not a rule of its own but the half-credit list of the first:
# {criterion key: {primary slot: secondary slot}}. The rule is keyed by the primary slot (the
# secondary slot stays in `reads`, so editing it refreshes the rule); with no primary values
# there is no rule, so a nice-to-have-only profile builds no rule and the criterion drops out.
# Keyed by criterion key, so a stored skills_fit that reads only required_skills also gets the
# half-credit list when its rules are rebuilt.
SECONDARY_READS: dict[str, dict[str, str]] = {
    "skills_fit": {"required_skills": "nice_to_have_skills"},
}
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


_LEGAL_SUFFIXES = frozenset(
    "inc incorporated ltd limited llc plc pvt private gmbh corp corporation co company "
    "sa ag bv srl".split()
)


def _company_tokens(name: str) -> list[str]:
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", name.casefold()) if not unicodedata.combining(c)
    )  # "Société Générale" is "Societe Generale"
    tokens = re.sub(r"\W+", " ", folded.replace("&", " and ")).split()  # "AT&T" is "AT and T"
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()  # "Acme Corp." and "Acme Corporation" are the same company
    return tokens


def company_match(candidate: str, avoided: str) -> bool:
    """Whole-name match of a company name against a listed one. Names are compared without case,
    punctuation and trailing legal suffixes (Inc, Ltd, Pvt, GmbH, ...). They match when equal, or
    when the listed name has two or more words and appears as a whole-word run inside the
    candidate ("Tata Consultancy" matches "Tata Consultancy Services Ltd"). A one-word listed
    name matches only exactly: "Meta" does not match "Metal Works" or "Meta Platforms"."""
    want, have = _company_tokens(avoided), _company_tokens(candidate)
    if not want or not have:
        return False
    if want == have:
        return True
    n = len(want)
    return n >= 2 and any(have[i : i + n] == want for i in range(len(have) - n + 1))


# Fields holding company names, matched by whole name; every other field uses loose_equal.
COMPANY_NAME_FIELDS = frozenset({"source_companies", "avoid_companies"})


# Geography fields: a value is a country (name, alias or ISO-2 code), a region, or free text
# such as a city. Matched by whole name, never by substring ("Oman" is not in "Romania").
COUNTRY_FIELDS = frozenset(
    {"lead_geographies", "company_hq_geographies", "candidate_locations", "current_company_hq"}
)


def country_match(candidate: object, wanted: object) -> bool:
    """Whole-name match of a lead's geography against a rule value. Both sides are resolved
    through the country catalog ("US", "United States" and "united states" are one option). They
    match when they resolve to the same option, or when `wanted` is a region and `candidate` is
    one of its countries. Values the catalog does not know (cities) match only when equal
    after case and whitespace normalisation. A location line such as "Berlin, Germany" matches
    as a whole value or by its last comma-separated part (the country position), so
    "Georgia, USA" is the United States, not the country Georgia. Non-text values never match."""
    if not isinstance(candidate, str) or not isinstance(wanted, str):
        return False
    if not candidate.strip() or not wanted.strip():
        return False
    if _country_match_one(candidate, wanted):
        return True
    if "," in candidate:
        last = candidate.rsplit(",", 1)[1].strip()
        return bool(last) and _country_match_one(last, wanted)
    return False


def _country_match_one(candidate: str, wanted: str) -> bool:
    catalog = default_catalog()
    have, want = catalog.resolve("country", candidate), catalog.resolve("country", wanted)
    if have is None and want is None:
        return _norm(candidate) == _norm(wanted)
    if have is None or want is None:
        return False
    return have.id == want.id or (want.level == 1 and have.level == 2 and have.parent == want.label)


def _matcher(slot: str):
    if slot in COMPANY_NAME_FIELDS:
        return company_match
    return country_match if slot in COUNTRY_FIELDS else loose_equal


def list_match(lead_values: list[str], rule: MatchRule, slot: str = "") -> float:
    same = _matcher(slot)
    if any(same(v, p) for v in lead_values for p in rule.primary):
        return 1.0
    if any(same(v, p) for v in lead_values for p in rule.secondary):
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


def _slot_score(rule: MatchRule, lead_values: list[str], slot: str = "") -> float | None:
    """Score for one slot, or None when the lead's data cannot be read."""
    if rule.kind == "list":
        return list_match(lead_values, rule, slot)
    # text values kept next to the range ("Self-employed") match the lead's text as a list would
    if rule.primary and list_match(lead_values, rule, slot):
        return 1.0
    for text in lead_values:
        bounds = parse_range(text)
        value = _midpoint(bounds) if bounds else None
        if value is not None:
            return range_match(value, rule)
    return None


# -- building and editing the model --------------------------------------------------------------


def build_rule(
    slot_key: str, values: list[str], secondary: list[str] | None = None
) -> MatchRule | None:
    """A rule for one slot; `secondary` (list rules only) earns half credit."""
    values = [v for v in values if v.strip()]
    if not values or any(v.strip().lower() == ANY.lower() for v in values):
        return None
    if slot_key in RANGE_SLOTS:
        ranges = [r for r in (parse_range(v) for v in values) if r]
        if ranges:
            lo = None if any(r[0] is None for r in ranges) else min(r[0] for r in ranges)
            hi = None if any(r[1] is None for r in ranges) else max(r[1] for r in ranges)
            words = [v for v in values if not parse_range(v)]  # kept, not silently dropped
            return MatchRule(
                kind="range",
                primary=words,
                min=lo,
                max=hi,
                tolerance=RANGE_TOLERANCE.get(slot_key, DEFAULT_TOLERANCE),
            )
    extra = [v for v in (secondary or []) if v.strip() and v.strip().lower() != ANY.lower()]
    return MatchRule(kind="list", primary=_with_region_countries(slot_key, values), secondary=extra)


def _with_region_countries(slot_key: str, values: list[str]) -> list[str]:
    """A region among a country slot's values ("Europe") also matches every country in it."""
    if SLOT_FACETS.get(slot_key) != "country":
        return values
    catalog = default_catalog()
    out = list(values)
    for v in values:
        for country in catalog.region_countries(v) or []:
            if country.label not in out:
                out.append(country.label)
    return out


def _rules_for(
    reads: tuple[str, ...] | list[str],
    slot_values: dict[str, list[str]],
    secondary: dict[str, str] | None = None,
):
    secondary = secondary or {}
    fed = set(secondary.values())  # slots that only feed another slot's half-credit list
    rules = {}
    for slot in reads:
        if slot in fed:
            continue
        extra = slot_values.get(secondary[slot], []) if slot in secondary else None
        rule = build_rule(slot, slot_values.get(slot, []), extra)
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
        rules = _rules_for(reads, slot_values, SECONDARY_READS.get(key))
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
        rules = _rules_for(c.reads, slot_values, SECONDARY_READS.get(c.key))
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


# -- priority questions -----------------------------------------------------------------------------

# "Which matters more, A or B?" pairs per track. The answers turn into weights (weights_from_priorities).
PRIORITY_DUELS: dict[Track, tuple[tuple[str, str], ...]] = {
    "sales": (
        ("industry_fit", "geographic_fit"),
        ("company_type_fit", "buying_signals"),
        ("buyer_fit", "company_size"),
        ("buyer_fit", "industry_fit"),
        ("company_type_fit", "industry_fit"),
    ),
    "hr": (
        ("role_fit", "skills_fit"),
        ("experience_fit", "education_fit"),
        ("role_fit", "experience_fit"),
        ("skills_fit", "location_fit"),
        ("background_fit", "employer_fit"),
    ),
}
PRIORITY_STEP = 0.5  # each step of preference multiplies a weight by e^0.5, about 1.65x
MAX_PREFERENCE = 2  # answers run from -2 (B much more) to +2 (A much more)


class PriorityQuestion(BaseModel):
    a: str
    b: str
    a_label: str
    b_label: str
    a_looks: str  # what the ICP looks for on this criterion, in the company's own values
    b_looks: str


class PriorityAnswer(BaseModel):
    a: str
    b: str
    value: int = Field(ge=-MAX_PREFERENCE, le=MAX_PREFERENCE)  # positive favours a


def _looks_for(criterion: Criterion) -> str:
    parts: list[str] = []
    for rule in criterion.rules.values():
        if rule.kind == "list":
            parts.extend(rule.primary)
        else:
            lo = "" if rule.min is None else f"{rule.min:g}"
            hi = "" if rule.max is None else f"{rule.max:g}"
            parts.append(f"{lo}-{hi}" if lo and hi else f"{lo}+" if lo else f"up to {hi}")
    return ", ".join(parts)


def priority_questions(model: ScoringModel, track: Track = "sales") -> list[PriorityQuestion]:
    """The pairs worth asking: both criteria apply, so the ICP has values for them."""
    by_key = {c.key: c for c in model.criteria if c.applicable}
    return [
        PriorityQuestion(
            a=a, b=b, a_label=by_key[a].label, b_label=by_key[b].label,
            a_looks=_looks_for(by_key[a]), b_looks=_looks_for(by_key[b]),
        )
        for a, b in PRIORITY_DUELS[track]
        if a in by_key and b in by_key
    ]  # fmt: skip


def weights_from_priorities(
    model: ScoringModel, answers: list[PriorityAnswer], track: Track = "sales"
) -> dict[str, float]:
    """Weights for every criterion, summing to 100. Criteria in no question keep their weight; the
    rest share what is left in proportion to e^(step * preference), as whole numbers."""
    asked_pairs = {(q.a, q.b) for q in priority_questions(model, track)}
    score: dict[str, float] = {k: 0.0 for pair in asked_pairs for k in pair}
    for ans in answers:
        if (ans.a, ans.b) not in asked_pairs:
            raise ValueError(f"not a priority question: {ans.a} vs {ans.b}")
        score[ans.a] += ans.value
        score[ans.b] -= ans.value
    weights = {c.key: c.weight for c in model.criteria}
    keys = sorted(score, key=lambda k: [c.key for c in model.criteria].index(k))
    pool = WEIGHT_TOTAL - sum(w for k, w in weights.items() if k not in score)
    raw = {k: math.exp(PRIORITY_STEP * score[k]) for k in keys}
    total = sum(raw.values())
    exact = {k: raw[k] * pool / total for k in keys}
    whole = {k: math.floor(exact[k] + EPSILON) for k in keys}
    leftover = round(pool - sum(whole.values()))
    for k in sorted(keys, key=lambda k: -(exact[k] - whole[k]))[:leftover]:
        whole[k] += 1  # largest remainders get the leftover points; ties keep the ICP's order
    return {**weights, **{k: float(v) for k, v in whole.items()}}


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
        "avoid_companies",
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
        same = _matcher(d.field)
        found = any(same(v, x) for v in values for x in d.values)
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
            if lead.get(slot) and (s := _slot_score(rule, lead[slot], slot)) is not None
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
