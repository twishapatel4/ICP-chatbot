"""Step 2 of the profile builder: the card definitions, validation and seeding.

Cards are data only. Which slots a card shows follows the slot registry's skip rules, and a slot
whose controlling slot is still unanswered stays hidden."""

import re
from dataclasses import dataclass
from typing import Literal

from icp_chatbot.domain.slots import Track, slot_map
from icp_chatbot.domain.state import (
    ANY,
    SessionState,
    SlotSource,
    SlotValue,
    resolve_slot_values,
)
from icp_chatbot.linkedin.catalog import SLOT_FACETS, default_catalog

MAX_VALUES = 10
MAX_VALUE_CHARS = 80
FORM_EVIDENCE = "step 2 form"
FORM_CONFIDENCE = 0.9
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DASHES = str.maketrans({"–": "-", "—": "-"})

Input = Literal["single", "multi", "chips", "range", "geo"]


@dataclass(frozen=True)
class Card:
    id: str
    title: str
    slots: tuple[str, ...]
    input: Input
    facet: str | None
    required: bool = True
    optional: tuple[str, ...] = ()
    any_allowed: bool = False
    requires_catalog: tuple[str, ...] = ()


# How each slot is answered, where it differs from its card's input.
_SLOT_INPUT: dict[str, Input] = {
    "business_model": "single",
    "experience_level": "single",
    "buyer_roles": "chips",
    "target_roles": "chips",
    "required_skills": "chips",
    "years_experience": "range",
    "current_industries": "multi",
    "current_company_size": "multi",
    "graduation_year": "range",
    "lead_geographies": "geo",
    "company_hq_geographies": "geo",
    "candidate_locations": "geo",
    "current_company_hq": "geo",
}

_SALES_CARDS = (
    Card("who_you_sell_to", "Who you sell to", ("business_model",), "single", None),
    Card("target_industries", "Target industries", ("industries",), "multi", "industry",
         any_allowed=True),
    Card("target_company_size", "Target company size", ("company_size",), "multi",
         "company_size", any_allowed=True),
    Card("geography", "Geography", ("lead_geographies", "company_hq_geographies"), "geo",
         "country", any_allowed=True),
    Card("buyer_roles", "Buyer roles",
         ("buyer_seniority", "buyer_departments", "buyer_roles"), "multi", "seniority",
         optional=("buyer_roles",), any_allowed=True),
)  # fmt: skip

_HR_CARDS = (
    Card("role_hiring_for", "Role you're hiring for", ("target_functions", "target_roles"),
         "multi", "function", any_allowed=True),
    Card("experience_level", "Experience level", ("experience_level",), "single", None),
    Card("education", "Education",
         ("education_level", "field_of_study", "graduation_year"), "multi", "degree",
         requires_catalog=("degree", "field_of_study")),
    Card("background", "Background",
         ("years_experience", "current_industries", "current_company_size",
          "current_company_hq"), "range", "industry",
         optional=("current_company_hq",), any_allowed=True),
    Card("candidate_location", "Candidate location", ("candidate_locations",), "geo",
         "country", any_allowed=True),
    Card("must_have_skills", "Must-have skills", ("required_skills",), "chips", None),
)  # fmt: skip


def profile_cards(track: Track) -> list[Card]:
    return list(_HR_CARDS if track == "hr" else _SALES_CARDS)


def _slot_input(card: Card, slot: str) -> Input:
    return _SLOT_INPUT.get(slot, card.input)


def _clean(values) -> list[str]:
    """Trimmed, control characters removed, whitespace collapsed, empties and repeats dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        text = " ".join(_CONTROL.sub(" ", str(raw)).split())
        if text.lower() == ANY.lower():
            text = ANY
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _state_for(track: Track, answers: dict[str, list[str]]) -> SessionState:
    state = SessionState(track=track)
    for key, values in answers.items():
        clean = _clean(values)
        if clean:
            state.slots[key] = SlotValue(
                values=clean, confidence=FORM_CONFIDENCE, source=SlotSource.STATED
            )
    return state


def _catalog_ready(card: Card) -> bool:
    catalog = default_catalog()
    return all(catalog.has(facet) for facet in card.requires_catalog)


def _visible_slots(track: Track, card: Card, state: SessionState) -> list[str]:
    if not _catalog_ready(card):
        return []
    defs = slot_map(track)
    out = []
    for key in card.slots:
        slot = defs[key]
        if not state.is_applicable(slot):
            continue
        if any(not state.values(controller) for controller, _ in slot.skip_if):
            continue
        out.append(key)
    return out


def visible_cards(track: Track, answers: dict[str, list[str]]) -> list[Card]:
    state = _state_for(track, answers)
    return [c for c in profile_cards(track) if _visible_slots(track, c, state)]


def parse_years(value: str) -> tuple[int, int | None]:
    """ "3-8" gives (3, 8); "3+" gives (3, None). Lower is 1 to 12 and upper is above lower."""
    text = str(value).translate(_DASHES).replace(" ", "")
    match = re.fullmatch(r"(\d{1,2})(\+|-(\d{1,2}))", text)
    if not match:
        raise ValueError(f"not a years range: {value!r}")
    lower = int(match.group(1))
    if not 1 <= lower <= 12:
        raise ValueError(f"lower bound must be 1 to 12: {value!r}")
    if match.group(2) == "+":
        return lower, None
    upper = int(match.group(3))
    if upper <= lower:
        raise ValueError(f"upper bound must be above the lower bound: {value!r}")
    return lower, upper


def _graduation_ok(value: str) -> bool:
    match = re.fullmatch(r"(\d{4})(?:-(\d{4}))?", value.translate(_DASHES).replace(" ", ""))
    if not match:
        return False
    lower = int(match.group(1))
    upper = int(match.group(2) or lower)
    return 1990 <= lower <= upper <= 2035


def _catalog_backed(facet: str | None) -> bool:
    return bool(facet) and bool(default_catalog().options(facet))


def _slot_valid(card: Card, slot: str, values: list[str]) -> bool:
    kind = _slot_input(card, slot)
    any_ok = (
        card.any_allowed and kind in ("multi", "range", "geo") and slot not in ("target_roles",)
    )
    if not values:
        return False
    if ANY in values:
        return any_ok and values == [ANY]
    if len(values) > MAX_VALUES or any(len(v) > MAX_VALUE_CHARS for v in values):
        return False
    if kind == "single":
        return len(values) == 1 and _single_ok(slot, values[0])
    if kind == "range":
        if len(values) != 1:
            return False
        if slot == "graduation_year":
            return _graduation_ok(values[0])
        try:
            parse_years(values[0])
        except ValueError:
            return False
        return True
    if kind == "chips":
        return True
    facet = SLOT_FACETS.get(slot)
    catalog = default_catalog()
    if kind == "geo":
        resolved = [catalog.resolve("country", v) for v in values] if facet else []
        return any(r is not None for r in resolved)
    if _catalog_backed(facet):
        return all(catalog.resolve(facet, v) is not None for v in values)
    return True


def _single_ok(slot: str, value: str) -> bool:
    choices = slot_map("sales" if slot == "business_model" else "hr")[slot].choices
    return not choices or value.lower() in {c.lower() for c in choices}


def validate_profile(track: Track, answers: dict[str, list[str]]) -> list[str]:
    """The visible slots that are missing or invalid, in card order. Empty means valid."""
    state = _state_for(track, answers)
    problems: list[str] = []
    for card in profile_cards(track):
        for slot in _visible_slots(track, card, state):
            values = _clean(answers.get(slot))
            if not values and (slot in card.optional or not card.required):
                continue
            if not _slot_valid(card, slot, values):
                problems.append(slot)
    return problems


def apply_profile(
    state: SessionState, answers: dict[str, list[str]], turn_no: int = 0
) -> list[str]:
    """Seed the session with the step 2 answers. Returns the slot keys written."""
    problems = validate_profile(state.track, answers)
    if problems:
        raise ValueError(f"invalid profile answers: {', '.join(problems)}")
    form_state = _state_for(state.track, answers)
    visible: set[str] = set()
    written: list[str] = []
    for card in profile_cards(state.track):
        for slot in _visible_slots(state.track, card, form_state):
            visible.add(slot)
            values = _clean(answers.get(slot))
            if not values:
                _drop_form_value(state, slot)
                continue
            labels, refs, unmapped = resolve_slot_values(slot, values)
            state.slots[slot] = SlotValue(
                values=labels,
                refs=refs,
                unmapped=unmapped,
                confidence=FORM_CONFIDENCE,
                source=SlotSource.STATED,
                source_turn=turn_no,
                evidence=FORM_EVIDENCE,
            )
            written.append(slot)
    for card in profile_cards(state.track):
        for slot in card.slots:
            if slot not in visible:
                _drop_form_value(state, slot)
    return written


def _drop_form_value(state: SessionState, slot: str) -> None:
    current = state.slots.get(slot)
    if current is not None and current.evidence == FORM_EVIDENCE:
        del state.slots[slot]
