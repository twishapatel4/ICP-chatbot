"""Catalog-aware comparison of an extracted slot value with a case's expected value.

The product stores LinkedIn canonical values (countries, industry labels), so a free-text
expectation such as "UK" or "law firms" cannot be compared by substring. `slot_matcher` picks the
comparison per slot, reusing the production helpers for geography and company names:

- company-name slots: whole-name match (`company_match`);
- geography slots: `country_match` in both directions (a region matches a country inside it,
  and a country matches a region that contains it; aliases such as UK resolve);
- industry: same catalog option, or one is an ancestor of the other (Banking in Financial
  Services), else `loose_equal` on the raw text;
- seniority and function: same catalog option (aliases included), else `loose_equal`;
- company size: same size option or overlapping numeric range, else `loose_equal`;
- every other slot, and anything unresolved: `loose_equal` (the original behaviour).
"""

from collections.abc import Callable

from icp_chatbot.icp.scoring import (
    COMPANY_NAME_FIELDS,
    COUNTRY_FIELDS,
    company_match,
    country_match,
    loose_equal,
    parse_range,
)
from icp_chatbot.linkedin.catalog import SLOT_FACETS, Option, default_catalog

Matcher = Callable[[object, object], bool]
Bounds = tuple[float | None, float | None]


def _texts(a: object, b: object) -> tuple[str, str] | None:
    if isinstance(a, str) and isinstance(b, str) and a.strip() and b.strip():
        return a, b
    return None


def _resolve(facet: str, text: str) -> Option | None:
    catalog = default_catalog()
    return catalog.resolve(facet, text) if catalog.has(facet) else None


def _ancestors(option: Option) -> list[str]:
    """Labels above an industry option, nearest first (from its `parent` chain)."""
    out: list[str] = []
    current = option
    while current.parent and current.parent not in out:
        out.append(current.parent)
        parent = _resolve("industry", current.parent)
        if parent is None:
            break
        current = parent
    return out


def industry_match(actual: object, expected: object) -> bool:
    """Same industry option, or one is an ancestor of the other; else loose text match."""
    pair = _texts(actual, expected)
    if pair is None:
        return False
    have, want = _resolve("industry", pair[0]), _resolve("industry", pair[1])
    if have is not None and want is not None:
        return (
            have.id == want.id or want.label in _ancestors(have) or have.label in _ancestors(want)
        )
    return loose_equal(*pair)


def _facet_match(facet: str) -> Matcher:
    def match(actual: object, expected: object) -> bool:
        pair = _texts(actual, expected)
        if pair is None:
            return False
        have, want = _resolve(facet, pair[0]), _resolve(facet, pair[1])
        if have is not None and want is not None:
            return have.id == want.id
        return loose_equal(*pair)

    return match


def _overlap(a: Bounds, b: Bounds) -> bool:
    inf = float("inf")
    a_lo, a_hi = (-inf if a[0] is None else a[0]), (inf if a[1] is None else a[1])
    b_lo, b_hi = (-inf if b[0] is None else b[0]), (inf if b[1] is None else b[1])
    return a_lo <= b_hi and b_lo <= a_hi


def _size_range(text: str) -> Bounds | None:
    option = _resolve("company_size", text)
    return parse_range(option.label if option else text)


def size_match(actual: object, expected: object) -> bool:
    """Same size option, or overlapping ranges; else loose text match."""
    pair = _texts(actual, expected)
    if pair is None:
        return False
    a, b = _size_range(pair[0]), _size_range(pair[1])
    if a is not None and b is not None:
        return _overlap(a, b)
    have, want = _resolve("company_size", pair[0]), _resolve("company_size", pair[1])
    if have is not None and want is not None:
        return have.id == want.id
    return loose_equal(*pair)


def geography_match(actual: object, expected: object) -> bool:
    return country_match(actual, expected) or country_match(expected, actual)


def company_name_match(actual: object, expected: object) -> bool:
    pair = _texts(actual, expected)
    return pair is not None and (company_match(*pair) or company_match(pair[1], pair[0]))


def _loose(actual: object, expected: object) -> bool:
    pair = _texts(actual, expected)
    return pair is not None and loose_equal(*pair)


_FACET_MATCHERS: dict[str, Matcher] = {
    "industry": industry_match,
    "seniority": _facet_match("seniority"),
    "function": _facet_match("function"),
    "company_size": size_match,
}


def slot_matcher(slot: str) -> Matcher:
    """The comparison for one slot: (actual, expected) -> bool. Safe on non-text values."""
    if slot in COMPANY_NAME_FIELDS:
        return company_name_match
    if slot in COUNTRY_FIELDS:
        return geography_match
    return _FACET_MATCHERS.get(SLOT_FACETS.get(slot, ""), _loose)


def forbidden_match(slot: str, actual: object, forbidden: object) -> bool:
    """Conservative: a forbidden value counts when the raw text OR the canonical value matches."""
    return _loose(actual, forbidden) or slot_matcher(slot)(actual, forbidden)
