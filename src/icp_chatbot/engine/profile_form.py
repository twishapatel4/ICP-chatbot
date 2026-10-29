"""Step 2 of the profile builder: the card definitions, validation and seeding.

Cards are data only. Each CardField carries how its slot is answered; which fields show follows
the slot registry's skip rules, and a slot whose controlling slot is still unanswered stays
hidden. The Sales form does not ask the business model: an unanswered one counts as B2B, and
apply_profile seeds it as a default the chat can still change. Answers come from the browser, so nothing here raises on malformed input: a bad value is
reported against its slot key."""

import re
from dataclasses import dataclass
from typing import Any, Literal

from icp_chatbot.domain.slots import Track, slot_map
from icp_chatbot.domain.state import (
    ANY,
    SessionState,
    SlotSource,
    SlotValue,
    resolve_slot_values,
)
from icp_chatbot.linkedin.catalog import default_catalog

MAX_VALUES = 10
MAX_VALUE_CHARS = 80
MAX_RAW_ITEMS = 50  # more than this is rejected without looking at the items
MAX_RAW_CHARS = 1000  # a longer raw string is rejected without cleaning it
FORM_EVIDENCE = "step 2 form"
FORM_CONFIDENCE = 0.9
DEFAULT_BUSINESS_MODEL = "B2B"  # LinkedIn search targets businesses
DEFAULT_CONFIDENCE = 0.5
DEFAULT_EVIDENCE = "step 2 form default"
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DASHES = str.maketrans({"–": "-", "—": "-"})

Input = Literal["single", "multi", "chips", "range", "geo"]


@dataclass(frozen=True)
class CardField:
    slot: str
    input: Input
    facet: str | None = None
    optional: bool = False
    any_allowed: bool = False


@dataclass(frozen=True)
class Card:
    id: str
    title: str
    fields: tuple[CardField, ...]
    required: bool = True
    requires_catalog: tuple[str, ...] = ()

    @property
    def slots(self) -> tuple[str, ...]:
        return tuple(f.slot for f in self.fields)


_SALES_CARDS = (
    Card("target_industries", "Target industries",
         (CardField("industries", "multi", "industry", any_allowed=True),)),
    Card("geography", "Geography", (
        CardField("lead_geographies", "geo", "country", any_allowed=True),
        CardField("company_hq_geographies", "geo", "country", any_allowed=True),
    )),
    Card("buyer_roles", "Buyer roles", (
        CardField("buyer_seniority", "multi", "seniority", any_allowed=True),
        CardField("buyer_departments", "multi", "function", any_allowed=True),
        CardField("buyer_roles", "chips", optional=True),
    )),
)  # fmt: skip

_HR_CARDS = (
    Card("role_hiring_for", "Role you're hiring for", (
        CardField("target_functions", "multi", "function", any_allowed=True),
        CardField("target_roles", "chips"),
    )),
    Card("experience_level", "Experience level", (CardField("experience_level", "single"),)),
    Card("education", "Education", (
        CardField("education_level", "multi", "degree"),
        CardField("field_of_study", "multi", "field_of_study"),
        CardField("graduation_year", "range"),
    ), requires_catalog=("degree", "field_of_study")),
    Card("background", "Background", (
        CardField("years_experience", "range", any_allowed=True),
        CardField("current_industries", "multi", "industry", any_allowed=True),
        CardField("current_company_size", "multi", "company_size", any_allowed=True),
        CardField("current_company_hq", "geo", "country", optional=True, any_allowed=True),
    )),
)  # fmt: skip
# Neither the must-have skills nor the candidate location is a card: required_skills and
# candidate_locations are asked in the chat like any other unknown must-ask slot.


def profile_cards(track: Track) -> list[Card]:
    return list(_HR_CARDS if track == "hr" else _SALES_CARDS)


def _clean(raw: Any, any_allowed: bool = False) -> list[str] | None:
    """The answer as trimmed text values (control characters removed, whitespace collapsed,
    empties and repeats dropped), or None when it is not a list of strings. Only lists and
    tuples are answers; numbers, booleans, bare strings, dicts and nested lists are invalid."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_RAW_ITEMS:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or len(item) > MAX_RAW_CHARS:
            return None
        text = " ".join(_CONTROL.sub(" ", item).split())
        if any_allowed and text.lower() == ANY.lower():
            text = ANY
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _as_dict(answers: Any) -> dict:
    return answers if isinstance(answers, dict) else {}


def _answer(answers: dict, field: CardField) -> list[str] | None:
    return _clean(answers.get(field.slot), field.any_allowed)


def _default_business_model() -> SlotValue:
    return SlotValue(
        values=[DEFAULT_BUSINESS_MODEL],
        confidence=DEFAULT_CONFIDENCE,
        source=SlotSource.DEFAULT,
        evidence=DEFAULT_EVIDENCE,
    )


def _state_for(track: Track, answers: Any, business_model: SlotValue | None = None) -> SessionState:
    """A session holding the answers to known slots, for the registry's skip rules. For Sales
    the business model is the one given, else B2B: the form never asks it."""
    answers = _as_dict(answers)
    state = SessionState(track=track)
    if track != "hr":
        state.slots["business_model"] = business_model or _default_business_model()
    known = {f.slot: f for c in profile_cards(track) for f in c.fields}
    for key, field in known.items():
        values = _answer(answers, field)
        if values:
            state.slots[key] = SlotValue(
                values=values, confidence=FORM_CONFIDENCE, source=SlotSource.STATED
            )
    return state


def _catalog_ready(card: Card) -> bool:
    catalog = default_catalog()
    return all(catalog.has(facet) for facet in card.requires_catalog)


def _visible_fields(track: Track, card: Card, state: SessionState) -> list[CardField]:
    if not _catalog_ready(card):
        return []
    defs = slot_map(track)
    out = []
    for field in card.fields:
        slot = defs[field.slot]
        if not state.is_applicable(slot):
            continue
        if any(not state.values(controller) for controller, _ in slot.skip_if):
            continue
        out.append(field)
    return out


def visible_cards(track: Track, answers: Any, business_model: SlotValue | None = None) -> list[Card]:
    state = _state_for(track, answers, business_model)
    return [c for c in profile_cards(track) if _visible_fields(track, c, state)]


_YEARS = re.compile(r"([0-9]{1,2})(?:(\+)|-([0-9]{1,2}))", re.ASCII)


def parse_years(value: str) -> tuple[int, int | None]:
    """ "3-8" gives (3, 8); "3+" gives (3, None). Lower is 1 to 12 and upper is above lower."""
    text = str(value).translate(_DASHES).replace(" ", "")
    match = _YEARS.fullmatch(text)
    if not match:
        raise ValueError(f"not a years range: {value!r}")
    lower = int(match.group(1))
    if not 1 <= lower <= 12:
        raise ValueError(f"lower bound must be 1 to 12: {value!r}")
    if match.group(2):
        return lower, None
    upper = int(match.group(3))
    if upper <= lower:
        raise ValueError(f"upper bound must be above the lower bound: {value!r}")
    return lower, upper


def _years_text(value: str) -> str:
    lower, upper = parse_years(value)
    return f"{lower}+" if upper is None else f"{lower}-{upper}"


_GRAD = re.compile(r"([0-9]{4})(?:-([0-9]{4}))?", re.ASCII)


def _graduation_text(value: str) -> str:
    match = _GRAD.fullmatch(value.translate(_DASHES).replace(" ", ""))
    if not match:
        raise ValueError(f"not a graduation year: {value!r}")
    lower = int(match.group(1))
    upper = int(match.group(2) or lower)
    if not 1990 <= lower <= upper <= 2035:
        raise ValueError(f"graduation years out of range: {value!r}")
    return str(lower) if lower == upper else f"{lower}-{upper}"


def _range_text(slot: str, value: str) -> str:
    return _graduation_text(value) if slot == "graduation_year" else _years_text(value)


def _field_valid(track: Track, field: CardField, values: list[str]) -> bool:
    if not values:
        return False
    if ANY in values:
        return field.any_allowed and values == [ANY]
    if len(values) > MAX_VALUES or any(len(v) > MAX_VALUE_CHARS for v in values):
        return False
    if field.input == "chips":
        return True
    if field.input == "range":
        if len(values) != 1:
            return False
        try:
            _range_text(field.slot, values[0])
        except ValueError:
            return False
        return True
    catalog = default_catalog()
    if field.input == "geo":
        # cities are free text, but at least one value must be a country
        return field.facet is not None and any(
            catalog.resolve(field.facet, v) is not None for v in values
        )
    if field.input == "single" and len(values) != 1:
        return False
    choices = slot_map(track)[field.slot].choices
    if field.facet is None:
        return not choices or all(v.lower() in {c.lower() for c in choices} for v in values)
    if not catalog.options(field.facet):
        return False  # no catalog to check against: fail closed
    return all(catalog.resolve(field.facet, v) is not None for v in values)


def validate_profile(
    track: Track, answers: Any, business_model: SlotValue | None = None
) -> list[str]:
    """The visible slots that are missing or invalid, in card order. Empty means valid. Never
    raises on malformed answers."""
    answers = _as_dict(answers)
    state = _state_for(track, answers, business_model)
    problems: list[str] = []
    for card in profile_cards(track):
        for field in _visible_fields(track, card, state):
            values = _answer(answers, field)
            if values == [] and field.optional:
                continue
            if values is None or not _field_valid(track, field, values):
                problems.append(field.slot)
    return problems


def clean_answers(track: Track, answers: Any) -> dict[str, list[str]]:
    """The answers worth keeping for a form that comes back: the track's card slots only, as
    cleaned text, within the value limits. Malformed answers are dropped, never raised on."""
    answers = _as_dict(answers)
    out: dict[str, list[str]] = {}
    for card in profile_cards(track):
        for field in card.fields:
            values = [v for v in _answer(answers, field) or [] if len(v) <= MAX_VALUE_CHARS]
            if values:
                out[field.slot] = values[:MAX_VALUES]
    return out


def apply_profile(state: SessionState, answers: Any, turn_no: int = 0) -> list[str]:
    """Seed the session with the step 2 answers. Returns the slot keys written."""
    model = state.slots.get("business_model") if state.track != "hr" else None
    if model is not None and not model.values:
        model = None
    problems = validate_profile(state.track, answers, model)
    if problems:
        raise ValueError(f"invalid profile answers: {', '.join(problems)}")
    answers = _as_dict(answers)
    form_state = _state_for(state.track, answers, model)
    visible: set[str] = set()
    written: list[str] = []
    for card in profile_cards(state.track):
        for field in _visible_fields(state.track, card, form_state):
            visible.add(field.slot)
            values = _answer(answers, field) or []
            if not values:
                _drop_form_value(state, field.slot)
                continue
            if field.input == "range" and values != [ANY]:
                values = [_range_text(field.slot, values[0])]
            labels, refs, unmapped = resolve_slot_values(field.slot, values)
            state.slots[field.slot] = SlotValue(
                values=labels,
                refs=refs,
                unmapped=unmapped,
                confidence=FORM_CONFIDENCE,
                source=SlotSource.STATED,
                source_turn=turn_no,
                evidence=FORM_EVIDENCE,
            )
            written.append(field.slot)
    for card in profile_cards(state.track):
        for slot in card.slots:
            if slot not in visible:
                _drop_form_value(state, slot)
    if model is None and state.track != "hr":
        state.slots["business_model"] = _default_business_model()
    return written


def _drop_form_value(state: SessionState, slot: str) -> None:
    current = state.slots.get(slot)
    if current is not None and current.evidence == FORM_EVIDENCE:
        del state.slots[slot]
