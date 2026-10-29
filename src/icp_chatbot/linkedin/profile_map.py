"""Map a LinkedIn DB profile (and its company) to the scorer's `lead` dict.

The registry is the contract: every slot lists the profile paths its value comes from
(`SlotDef.profile_paths`), and this module reads those paths and nothing else. Anything a
path does not name (photo, premium flag, member id, names, about text) is never read, so it
can never influence a score. Paths look like `profile.title`, `profile.education[].school`,
`company.hqCountry`; `[]` means every element of a list.

Shapes (LinkedIn DB):
    ProfileDetails: title, city, country, skills[], currentPositions[title, companyName, industry,
        employeeRange], experience[title, companyName, period], education[school, degree, field,
        period], certifications[name]
    Company: industry, employeeRange, companyType, hqCountry, hqCity, foundedYear

Missing data yields no key for that slot (never an empty list or string).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from datetime import date
from typing import Any

from icp_chatbot.domain.slots import SlotDef, Track, registry
from icp_chatbot.linkedin.catalog import default_catalog

# -- path resolution (the only place profile data is read) -------------------------------------


def _walk(path: str, profile: Any, company: Any) -> list[Any]:
    root, _, rest = path.partition(".")
    nodes: list[Any] = [profile if root == "profile" else company]
    for part in rest.split("."):
        key, is_list = (part[:-2], True) if part.endswith("[]") else (part, False)
        found: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            value = node.get(key)
            if is_list:
                if isinstance(value, list):
                    found.extend(value)
            elif value is not None:
                found.append(value)
        nodes = found
    return nodes


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())
    return None


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for v in values:
        if v.casefold() not in seen:
            seen.add(v.casefold())
            out.append(v)
    return out


# -- catalog normalisation, chosen by the path's last segment ----------------------------------


def _country(text: str) -> str:
    catalog = default_catalog()
    option = catalog.resolve("country", text)
    if option is None and len(text) == 2:
        option = catalog.resolve("country", text.upper())  # DB codes may be lower case
    # a profile's country is a country: regions (level 1) are project groupings, not data
    return option.label if option is not None and option.level == 2 else text


def _facet(facet: str) -> Callable[[str], str]:
    def normalise(text: str) -> str:
        option = default_catalog().resolve(facet, text)
        return option.label if option else text  # unresolved values stay raw

    return normalise


_NORMALISERS: dict[str, Callable[[str], str]] = {
    "country": _country,
    "hqCountry": _country,
    "employeeRange": _facet("company_size"),
    "industry": _facet("industry"),
}


def _strings(slot: SlotDef, profile: Any, company: Any) -> list[str]:
    out = []
    for path in slot.profile_paths:
        normalise = _NORMALISERS.get(path.rsplit(".", 1)[-1], str)
        for node in _walk(path, profile, company):
            if (text := _text(node)) is not None:
                out.append(normalise(text))
    return _unique(out)


# -- computed values ------------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_START = re.compile(r"(?:\b([A-Za-z]{3})[A-Za-z]*\.?\s+)?\b((?:19|20)\d{2})\b")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_OPEN_ENDED = re.compile(r"\bpresent\b|\bcurrent\b|\bongoing\b|[-–—]\s*$", re.IGNORECASE)


def _period_start(period: str) -> tuple[int, int] | None:
    m = _START.search(period)
    if not m:
        return None
    return int(m.group(2)), _MONTHS.get((m.group(1) or "").lower(), 1)


def _years_experience(slot: SlotDef, profile: Any, company: Any, today: date) -> list[str]:
    starts = [
        s
        for path in slot.profile_paths
        for node in _walk(path, profile, company)
        if (text := _text(node)) and (s := _period_start(text))
    ]
    if not starts:
        return []
    year, month = min(starts)
    months = (today.year - year) * 12 + (today.month - month)
    return [str(max(0, months // 12))]


def _graduation_year(slot: SlotDef, profile: Any, company: Any, today: date) -> list[str]:
    years = []
    for path in slot.profile_paths:
        for node in _walk(path, profile, company):
            period = _text(node) or ""
            if _OPEN_ENDED.search(period):
                continue  # still studying: not a graduation year yet
            found = _YEAR.findall(period)
            if found:
                years.append(int(found[-1]))  # the last year of each period
    return [str(max(years))] if years else []


def _years_in_operation(slot: SlotDef, profile: Any, company: Any, today: date) -> list[str]:
    for path in slot.profile_paths:
        for node in _walk(path, profile, company):
            year = _whole_year(node)
            if year is not None and 1800 <= year <= today.year:
                return [str(today.year - year)]
    return []


def _whole_year(value: Any) -> int | None:
    """An int, a whole float or an ASCII digit string as an int; anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isascii() and text.isdecimal() else None
    return None


_COMPUTED: dict[str, Callable[[SlotDef, Any, Any, date], list[str]]] = {
    "years_experience": _years_experience,
    "graduation_year": _graduation_year,
    "years_in_operation": _years_in_operation,
}

# -- derived values: guessed from the job title by keyword ---------------------------------------
# Level rules are tried in this order and the first match wins, so a title gets its highest level
# ("Associate Director" is a Director). Matching is on whole words of the lower-cased title.
# Known soft spots: "HR Business Partner" reads as Partner; "Lead" outside "lead generation"
# reads as Senior.

_NOT_OWNER = re.compile(r"\b(?:product|process|data|service) owner\b")
# Phrases that hold a C-title word without being one: "Assistant to the CEO", "Chief of Staff".
_NOT_CHIEF = re.compile(r"\bassistant to(?: the)? \w+|\bchief of staff(?: to(?: the)? \w+)?")
SENIORITY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern))
    for label, pattern in (
        ("Chief X Officer (CxO)", r"\bchief\b|\b(?:ceo|cto|cfo|coo|cmo|cxo|cio|cpo|chro)\b"),
        ("Owner", r"\bowner\b|\bfounder\b"),
        ("Vice President (VP)", r"\bvice[\s-]president\b|\b[sa]?vp\b"),
        ("Director", r"\bdirector\b|\bhead of\b"),
        ("Manager", r"\bmanager\b"),
        ("Partner", r"\bpartner\b"),
        ("Senior", r"\bsenior\b|\bsr\b|\blead\b(?!\s+gen)|\bprincipal\b|\bstaff\b"),
        ("Training", r"\b(?:intern|interns|internship|trainee)\b"),
        ("Entry-level", r"\b(?:junior|associate|entry|graduate)\b"),
    )
)
SENIORITY_LABELS = tuple(label for label, _ in SENIORITY_RULES)

FUNCTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern))
    for label, pattern in (
        ("Engineering", r"\bengineers?\b|\bengineering\b|\bdevelopers?\b|\bprogrammers?\b"
                        r"|\bsoftware\b|\bdevops\b|\bsre\b|\bcto\b"),
        ("Information Technology", r"\bit\b|\binformation technology\b|\bsysadmin\b"
                                   r"|\bsystems? administrator\b|\bnetwork\b|\bhelp ?desk\b"
                                   r"|\bcyber ?security\b|\bcio\b"),
        ("Sales", r"\bsales\b|\baccount executive\b|\baccount manager\b"),
        ("Business Development", r"\bbusiness development\b"),
        ("Marketing", r"\bmarketing\b|\bgrowth\b|\bseo\b|\bbrand\b|\bdemand generation\b|\bcmo\b"),
        ("Human Resources", r"\bhr\b|\bhuman resources\b|\brecruit\w*\b|\btalent\b"
                            r"|\bpeople operations\b|\bchro\b"),
        ("Finance", r"\bfinance\b|\bfinancial\b|\bcfo\b|\bcontroller\b|\btreasur\w+\b"),
        ("Accounting / Auditing", r"\baccountants?\b|\baccounting\b|\baudit\w*\b"),
        ("General Business", r"\boperations?\b|\boperating\b|\bops\b|\bcoo\b"
                             r"|\bgeneral manager\b"),
        ("Product Management", r"\bproduct (?:manager|management|owner|lead)\b|\bcpo\b"
                               r"|\bchief product\b|\bhead of product\b"),
        ("Project Management", r"\bproject (?:manager|management)\b"
                               r"|\bprogram(?:me)? manager\b|\bscrum master\b"),
        ("Design", r"\bdesigner\b|\bdesign\b|\bux\b|\bui\b"),
        ("Customer Service", r"\bcustomer (?:service|support|success)\b"),
        ("Consulting", r"\bconsultants?\b|\bconsulting\b"),
        ("Legal", r"\blegal\b|\blawyer\b|\battorney\b|\bcounsel\b|\bparalegal\b"),
        ("Research", r"\bresearch\w*\b"),
        ("Education", r"\bteacher\b|\bprofessor\b|\blecturer\b"),
        ("QualityAssurance", r"\bqa\b|\bquality assurance\b|\btester\b"),
        ("Supply Chain", r"\bsupply chain\b|\blogistics\b"),
        ("Purchasing", r"\bprocurement\b|\bpurchasing\b"),
        ("Writing / Editing", r"\bwriter\b|\beditor\b|\bcopywriter\b"),
    )
)
FUNCTION_LABELS = tuple(label for label, _ in FUNCTION_RULES)


def _titles(slot: SlotDef, profile: Any, company: Any) -> list[str]:
    return [
        t
        for path in slot.profile_paths
        for node in _walk(path, profile, company)
        if (t := _text(node))
    ]


def _derived_seniority(titles: list[str]) -> list[str]:
    for title in titles:
        text = _NOT_CHIEF.sub("", _NOT_OWNER.sub("", title.lower()))
        for label, pattern in SENIORITY_RULES:
            if pattern.search(text):
                return [label]
    return []


def _derived_functions(titles: list[str]) -> list[str]:
    found = []
    for title in titles:
        text = title.lower()
        found += [label for label, pattern in FUNCTION_RULES if pattern.search(text)]
    return _unique(found)


def _derived(slot: SlotDef, profile: Any, company: Any) -> list[str]:
    titles = _titles(slot, profile, company)
    if slot.key in ("buyer_seniority", "target_seniority"):
        return _derived_seniority(titles)
    if slot.key in ("buyer_departments", "target_functions"):
        return _derived_functions(titles)
    return []


# -- the mapper ---------------------------------------------------------------------------------


def slots_from_profile(
    profile: dict | None, company: dict | None, track: Track, today: date | None = None
) -> dict[str, list[str]]:
    """The lead's value for every slot of `track` that has a profile path, keyed like the
    scorer's `lead` dict. Slots with no data are left out."""
    today = today or date.today()
    lead: dict[str, list[str]] = {}
    for slot in registry(track):
        if not slot.profile_paths:
            continue
        if slot.derived:
            values = _derived(slot, profile, company)
        elif slot.key in _COMPUTED:
            values = _COMPUTED[slot.key](slot, profile, company, today)
        else:
            values = _strings(slot, profile, company)
        if values:
            lead[slot.key] = values
    return lead
