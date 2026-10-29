"""The ICP slot registries, one per track: every field the interview can fill (spec sections
5.5 and 6.1)."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from icp_chatbot.linkedin.catalog import default_catalog

Track = Literal["sales", "hr"]  # who the profile is for: sales leads or job candidates


class Tier(StrEnum):
    A = "A"  # must be asked
    B = "B"  # asked if relevant and time allows
    C = "C"  # inferred or defaulted, never asked


SECTION_ORDER = (
    "seller_context",
    "target_firmographics",
    "business_profile",
    "buying_structure",
    "intent_and_timing",
    "success_and_exclusions",
    "industry_specifics",  # fields that only this company's industry needs
)

# LinkedIn's nine company headcount bands (codes A to I), as the option catalog labels them
COMPANY_SIZES: tuple[str, ...] = tuple(o.label for o in default_catalog().options("company_size"))
BUSINESS_MODELS = ("B2B", "B2C", "Both")


@dataclass(frozen=True)
class SlotDef:
    key: str
    section: str
    label: str
    description: str
    tier: Tier
    importance: float = 0.5
    choices: tuple[str, ...] = ()
    single_choice: bool = False  # one answer only: the user picks one option, not several
    open_ended: bool = False  # described in the user's own words: never offer suggested answers
    exact_range: bool = False  # the choices are example bands: the user's own range is kept
    blocks_completion: bool | None = None  # default: tier A blocks, others do not
    skip_if: tuple[
        tuple[str, str], ...
    ] = ()  # (slot key, value): slot does not apply if it matches
    linkedin_filter: str | None = None  # the LinkedIn search filter this slot maps to (table below)
    profile_paths: tuple[str, ...] = ()  # where the value is read from the LinkedIn DB profile/company
    derived: bool = False  # the value is guessed from other data (seniority from the title)

    @property
    def blocks(self) -> bool:
        if self.blocks_completion is None:
            return self.tier == Tier.A
        return self.blocks_completion


class CustomField(BaseModel):
    """A field this industry needs, chosen during the interview rather than fixed in advance."""

    key: str  # snake_case, never one of the standard keys
    label: str
    ask: str  # a plain question for someone in this industry, used if the writer is unavailable
    importance: float = Field(0.7, ge=0.0, le=1.0)
    choices: list[str] = Field(default_factory=list)


def custom_slot_def(field: CustomField, important: bool) -> SlotDef:
    return SlotDef(
        field.key,
        "industry_specifics",
        field.label,
        field.ask,
        Tier.A if important else Tier.B,
        field.importance,
        choices=tuple(field.choices),
    )


CUSTOM_BLOCKING = 3  # how many of an industry's own fields must be known before we can stop


def with_custom(fields: list[CustomField], track: Track = "sales") -> list[SlotDef]:
    """The track's standard slots plus the fields this industry needs (the strongest ones
    block)."""
    ranked = sorted(fields, key=lambda f: -f.importance)
    important = {f.key for f in ranked[:CUSTOM_BLOCKING]}
    return [*registry(track), *(custom_slot_def(f, f.key in important) for f in fields)]


def _s(key, section, label, description, tier, importance=0.5, **kw) -> SlotDef:
    return SlotDef(key, section, label, description, Tier(tier), importance, **kw)


# Single source of truth for the LinkedIn alignment (Task 12b): slot key ->
# (linkedin_filter, profile_paths, derived). Sales filters come from Sales Navigator (lead and
# account filters), HR filters from Recruiter Lite. Profile paths use the DB shapes
# (`profile.*` is ProfileDetails, `company.*` is Company); one tuple element per path.
# Old stored values "Onsite" (work_mode) will not match the renamed "On-site" choice.
# Every slot of both registries must appear here, deliberately, even with (None, (), False).
_NONE: tuple[str | None, tuple[str, ...], bool] = (None, (), False)
SLOT_LINKEDIN: dict[str, tuple[str | None, tuple[str, ...], bool]] = {
    # Sales (Sales Navigator)
    "industries": ("Industry", ("company.industry",), False),
    "company_size": ("Company headcount", ("company.employeeRange",), False),
    "company_type": ("Company type", ("company.companyType",), False),
    "lead_geographies": ("Geography", ("profile.country", "profile.city"), False),
    "company_hq_geographies": (
        "Company headquarters location", ("company.hqCountry", "company.hqCity"), False,
    ),
    "buyer_seniority": ("Seniority level", ("profile.title",), True),
    "buyer_departments": ("Function", ("profile.title",), True),
    "buyer_roles": (
        "Current job title", ("profile.title", "profile.currentPositions[].title"), False,
    ),
    "buying_triggers": ("Recent updates / Spotlight", (), False),
    "years_in_operation": (None, ("company.foundedYear",), False),
    "revenue_range": ("Annual revenue", (), False),
    "technologies_used": ("Technologies used", (), False),
    "how_we_work": _NONE,
    "lead_sources": _NONE,
    "value_proposition": _NONE,
    "online_presence": _NONE,
    "customer_profile": _NONE,
    "customer_segments": _NONE,
    "product": _NONE,
    "business_model": _NONE,
    # HR (Recruiter Lite)
    "target_roles": ("Job titles", ("profile.title", "profile.experience[].title"), False),
    "target_functions": ("Job functions", ("profile.title",), True),
    "years_experience": ("Years of experience", ("profile.experience[].period",), False),
    "education_level": ("Degrees", ("profile.education[].degree",), False),
    "field_of_study": ("Fields of study", ("profile.education[].field",), False),
    "graduation_year": ("Year of graduation", ("profile.education[].period",), False),
    "target_schools": ("Schools", ("profile.education[].school",), False),
    "required_skills": ("Skills", ("profile.skills[]",), False),
    "nice_to_have_skills": ("Keywords", ("profile.skills[]",), False),
    "certifications": ("Keywords", ("profile.certifications[].name",), False),
    "languages": ("Spoken languages", (), False),  # not in the DB
    "candidate_locations": ("Locations", ("profile.country", "profile.city"), False),
    "current_industries": ("Industries", ("profile.currentPositions[].industry",), False),
    "current_company_size": ("Company size", ("profile.currentPositions[].employeeRange",), False),
    "current_company_hq": (None, ("company.hqCountry", "company.hqCity"), False),  # no Lite filter (R19)
    "source_companies": (
        "Current companies / Past companies", ("profile.experience[].companyName",), False,
    ),
    "avoid_companies": (
        "Current companies / Past companies (excluded)", ("profile.experience[].companyName",), False,
    ),
    "target_seniority": (None, ("profile.title",), True),  # not in Lite (R19): scoring only
    "employment_type": ("Employment type", (), False),
    "work_mode": ("Workplace type", (), False),
    "salary_range": _NONE,
    "notice_period": _NONE,
    "relocation": _NONE,
    "urgency": _NONE,
    "headcount": _NONE,
    "disqualifiers": _NONE,
    "experience_level": _NONE,
}  # fmt: skip

# The filter names the TL listed, per track; a test pins every used name to these.
SALES_LINKEDIN_FILTERS = frozenset(
    {
        "Industry", "Company headcount", "Company type", "Geography",
        "Company headquarters location", "Seniority level", "Function", "Current job title",
        "Recent updates / Spotlight", "Annual revenue", "Technologies used",
    }
)  # fmt: skip
HR_LINKEDIN_FILTERS = frozenset(
    {
        "Job titles", "Job functions", "Years of experience", "Degrees", "Fields of study",
        "Year of graduation", "Schools", "Skills", "Keywords", "Spoken languages", "Locations",
        "Industries", "Company size", "Current companies / Past companies",
        "Current companies / Past companies (excluded)", "Employment type", "Workplace type",
    }
)  # fmt: skip


def _linked(slots: tuple[SlotDef, ...]) -> tuple[SlotDef, ...]:
    """Attach the LinkedIn metadata from SLOT_LINKEDIN to each slot."""
    out = []
    for s in slots:
        flt, paths, derived = SLOT_LINKEDIN[s.key]
        out.append(replace(s, linkedin_filter=flt, profile_paths=paths, derived=derived))
    return tuple(out)


_B2C = (("business_model", "B2C"),)
_B2B = (("business_model", "B2B"),)

SLOT_LIST: tuple[SlotDef, ...] = _linked((
    # 1. Seller context (inferred from signup and the website, not asked)
    _s(
        "product",
        "seller_context",
        "What the company does",
        "What the company does or sells, and the role it plays (for example broker, maker, agency)",
        "A",
        1.0,
    ),
    _s(
        "business_model",
        "seller_context",
        "Who they sell to",
        "Businesses (B2B), individuals or families (B2C), or both",
        "A",
        0.95,
        choices=BUSINESS_MODELS,
        single_choice=True,
    ),
    _s(
        "how_we_work",
        "seller_context",
        "How the company works",
        "How the company delivers and earns: its steps from first contact to a closed deal, and how it is paid",
        "B",
        0.85,
        open_ended=True,
    ),
    _s(
        "lead_sources",
        "seller_context",
        "How customers find them",
        "Where new customers come from today (referrals, ads, walk-ins, partners, outreach)",
        "B",
        0.6,
    ),
    _s(
        "value_proposition",
        "seller_context",
        "Why customers choose them",
        "Why customers pick this company over others",
        "B",
        0.6,
    ),
    _s(
        "online_presence",
        "seller_context",
        "Online presence",
        "Website and social profiles",
        "C",
        0.2,
    ),
    # 2. Target firmographics
    _s(
        "customer_profile",
        "target_firmographics",
        "Who the customers are",
        "For individual or family customers: who they are (life stage, household, income, where they live, what they want)",
        "A",
        1.0,
        skip_if=_B2B,
    ),
    _s(
        "industries",
        "target_firmographics",
        "Target industries and sub-industries",
        "Industries of the companies to sell to",
        "A",
        1.0,
        skip_if=_B2C,
    ),
    _s(
        "company_size",
        "target_firmographics",
        "Target company size",
        "Employee count of target companies",
        "A",
        0.9,
        choices=COMPANY_SIZES,
        exact_range=True,
        skip_if=_B2C,
    ),
    # geography has two levels, country then city: the pickers come from the country catalog
    _s(
        "lead_geographies",
        "target_firmographics",
        "Where the customers are",
        "Countries, and cities if any, where the people they sell to are based",
        "A",
        0.9,
    ),
    _s(
        "company_hq_geographies",
        "target_firmographics",
        "Where target companies are headquartered",
        "Countries, and cities if any, where the companies they sell to have their head office",
        "A",
        0.9,
        skip_if=_B2C,
    ),
    # Provisional (ruling R20): the options are the LinkedIn company types seen in the DB;
    # Sales Navigator's own list is pending confirmation from the TL. Open to free text.
    _s(
        "company_type",
        "target_firmographics",
        "Company type",
        "The kind of company: privately held, public, partnership, nonprofit and so on",
        "B",
        0.3,
        choices=(
            "Privately Held", "Public Company", "Partnership", "Sole Proprietorship",
            "Self-Employed", "Educational Institution", "Nonprofit",
        ),
    ),
    _s(
        "years_in_operation",
        "target_firmographics",
        "Years in operation",
        "Age of target companies",
        "C",
        0.1,
    ),
    # Annual revenue has no LinkedIn option list: free text, range-parsed like company_size.
    _s(
        "revenue_range",
        "target_firmographics",
        "Target annual revenue",
        "Annual revenue of target companies, as a range with the currency",
        "B",
        0.3,
        skip_if=_B2C,
    ),
    _s(
        "technologies_used",
        "target_firmographics",
        "Technologies used",
        "Software or tools the target companies already use",
        "B",
        0.3,
        skip_if=_B2C,
    ),
    # 3. Business profile of the target
    _s(
        "customer_segments",
        "business_profile",
        "Customer segments served",
        "Who the target companies themselves sell to",
        "A",
        0.5,
        blocks_completion=False,
    ),
    # 4. Buying structure
    _s(
        "buyer_roles",
        "buying_structure",
        "Buyer roles",
        "Decision makers and influencers",
        "A",
        0.9,
        skip_if=_B2C,
    ),
    _s(
        "buyer_departments",
        "buying_structure",
        "Buying departments",
        "Departments involved in buying",
        "A",
        0.6,
        skip_if=_B2C,
    ),
    _s(
        "buyer_seniority",
        "buying_structure",
        "Buyer seniority",
        "How senior the buyers are (for example manager, director, VP, owner)",
        "A",
        0.8,
        skip_if=_B2C,
    ),
    # 5. Intent and timing
    _s(
        "buying_triggers",
        "intent_and_timing",
        "Buying triggers",
        "Events that make a company ready to buy",
        "B",
        0.8,
        choices=(
            "Changed jobs", "Posted on LinkedIn", "Mentioned in news", "Job opportunities",
            "Recent activities (funding, leadership changes)", "Buyer intent",
        ),  # Sales Navigator options; the user may add their own events
    ),
    # 6. Exclusions
    _s(
        "disqualifiers",
        "success_and_exclusions",
        "Exclusions and disqualifiers",
        "Companies to avoid",
        "A",
        0.9,
    ),
))

SLOTS: dict[str, SlotDef] = {s.key: s for s in SLOT_LIST}
assert len(SLOTS) == len(SLOT_LIST), "duplicate slot keys"

HR_SECTION_ORDER = (
    "role_and_seniority",
    "background",
    "skills",
    "location_and_terms",
    "compensation_and_timing",
    "sourcing",
    "exclusions",
)

_EXPERIENCED = (("experience_level", "Experienced"),)
_EXPERIENCED_OR_BOTH = (("experience_level", "Experienced"), ("experience_level", "Both"))
_FRESHER = (("experience_level", "Fresher"),)

HR_SLOT_LIST: tuple[SlotDef, ...] = _linked((
    # 1. Role and seniority
    _s(
        "target_functions",
        "role_and_seniority",
        "Job functions",
        "The job functions the hire belongs to (for example engineering, sales, finance)",
        "A",
        0.85,
    ),
    _s(
        "target_roles",
        "role_and_seniority",
        "Job titles",
        "The job titles of the people to hire",
        "A",
        1.0,
    ),
    _s(
        "experience_level",
        "role_and_seniority",
        "Fresher or experienced",
        "Whether the hire is a fresher, an experienced professional, or either",
        "A",
        0.95,
        choices=("Fresher", "Experienced", "Both"),
        single_choice=True,
    ),
    _s(
        "target_seniority",
        "role_and_seniority",
        "Seniority level",
        "How senior the hire should be (for example entry, senior, manager, director)",
        "B",
        0.5,
    ),
    # 2. Background
    _s(
        "education_level",
        "background",
        "Education level",
        "The minimum education the candidates should have",
        "A",
        0.7,
        skip_if=_EXPERIENCED,
    ),
    _s(
        "field_of_study",
        "background",
        "Field of study",
        "The subjects or degrees the candidates should have studied",
        "A",
        0.7,
        skip_if=_EXPERIENCED_OR_BOTH,
    ),
    _s(
        "graduation_year",
        "background",
        "Graduation year",
        "The graduation years of the candidates (for example 2024 to 2026)",
        "A",
        0.7,
        skip_if=_EXPERIENCED_OR_BOTH,
    ),
    _s(
        "years_experience",
        "background",
        "Years of experience",
        "How many years of work experience the candidates should have (for example 3 to 8)",
        "A",
        0.85,
        skip_if=_FRESHER,
    ),
    _s(
        "current_industries",
        "background",
        "Current industries",
        "The industries the candidates work in today",
        "A",
        0.7,
        skip_if=_FRESHER,
    ),
    _s(
        "current_company_size",
        "background",
        "Current company size",
        "The size of the companies the candidates work at today",
        "A",
        0.7,
        choices=COMPANY_SIZES,
        exact_range=True,
        skip_if=_FRESHER,
    ),
    _s(
        "current_company_hq",
        "background",
        "Current company headquarters",
        "Countries, and cities if any, where the candidates' current companies are headquartered",
        "B",
        0.3,
        skip_if=_FRESHER,
    ),
    _s(
        "target_schools",
        "background",
        "Schools",
        "The schools or universities the candidates studied at",
        "B",
        0.3,
    ),
    # 3. Skills
    _s(
        "required_skills",
        "skills",
        "Required skills",
        "The skills and tools the candidates must have",
        "A",
        0.9,
    ),
    _s(
        "nice_to_have_skills",
        "skills",
        "Nice-to-have skills",
        "Skills that are a plus but not required",
        "B",
        0.5,
    ),
    _s(
        "certifications",
        "skills",
        "Certifications",
        "Certifications or licences the candidates should hold",
        "B",
        0.3,
    ),
    _s(
        "languages",
        "skills",
        "Languages",
        "Languages the candidates should speak",
        "B",
        0.3,
    ),
    # 4. Location and terms
    _s(
        "candidate_locations",
        "location_and_terms",
        "Where the candidates are",
        "Countries, and cities if any, where the candidates should be based",
        "A",
        0.9,
    ),
    _s(
        "employment_type",
        "location_and_terms",
        "Employment type",
        "Whether the role is full-time, part-time, a contract or an internship",
        "A",
        0.8,
        choices=(
            "Full-time", "Part-time", "Contract", "Internship", "Temporary", "Volunteer", "Other",
        ),
    ),
    _s(
        "work_mode",
        "location_and_terms",
        "Work mode",
        "Whether the role is on-site, hybrid or remote",
        "A",
        0.8,
        choices=("On-site", "Hybrid", "Remote"),
    ),
    _s(
        "relocation",
        "location_and_terms",
        "Relocation",
        "Whether candidates from other places can be asked to relocate",
        "B",
        0.4,
    ),
    _s(
        "headcount",
        "location_and_terms",
        "Number of hires",
        "How many people are to be hired",
        "B",
        0.4,
    ),
    # 5. Compensation and timing
    _s(
        "salary_range",
        "compensation_and_timing",
        "Salary range",
        "The pay on offer, with the currency and whether it is monthly or yearly",
        "A",
        0.8,
    ),
    _s(
        "notice_period",
        "compensation_and_timing",
        "Notice period",
        "The longest notice period the company can wait for",
        "B",
        0.4,
    ),
    _s(
        "urgency",
        "compensation_and_timing",
        "Hiring urgency",
        "How soon the role needs to be filled",
        "B",
        0.4,
    ),
    # 6. Sourcing
    _s(
        "source_companies",
        "sourcing",
        "Companies to hire from",
        "Companies where good candidates are likely to work today",
        "B",
        0.4,
    ),
    _s(
        "avoid_companies",
        "sourcing",
        "Companies to avoid",
        "Companies the candidates should not be hired from",
        "B",
        0.3,
    ),
    # 7. Exclusions
    _s(
        "disqualifiers",
        "exclusions",
        "Exclusions and disqualifiers",
        "Candidates to rule out",
        "A",
        0.9,
    ),
))

_REGISTRIES: dict[str, tuple[SlotDef, ...]] = {"sales": SLOT_LIST, "hr": HR_SLOT_LIST}
_SLOT_MAPS: dict[str, dict[str, SlotDef]] = {
    "sales": SLOTS,
    "hr": {s.key: s for s in HR_SLOT_LIST},
}
assert all(len(_SLOT_MAPS[t]) == len(slots) for t, slots in _REGISTRIES.items()), "duplicate keys"


def section_order(track: Track) -> tuple[str, ...]:
    """The sections of this track's profile, in display order."""
    return HR_SECTION_ORDER if track == "hr" else SECTION_ORDER


def registry(track: Track) -> tuple[SlotDef, ...]:
    """Every standard slot of this track, in interview order."""
    return _REGISTRIES[track]


def slot_map(track: Track) -> dict[str, SlotDef]:
    """This track's standard slots by key."""
    return _SLOT_MAPS[track]


def get_slot(key: str) -> SlotDef:
    return SLOTS[key]


def slots_in_tier(tier: Tier) -> list[SlotDef]:
    return [s for s in SLOT_LIST if s.tier == tier]


FACET_LIST_LIMIT = 40  # a facet with more options than this is not listed in full
_FACET_NOTES = {
    "industry": "Use the exact LinkedIn industry label (the top-level groups are {groups}); "
                "the profile maps a close wording to the matching label.",
    "country": "Use the English country name.",
}  # fmt: skip


def _facet_note(facet: str) -> str:
    """What the extraction prompt says about a slot tied to a LinkedIn option list: every label
    for a short list, the top-level groups for industries, nothing for a list with no catalog."""
    from icp_chatbot.linkedin.catalog import default_catalog  # the catalog needs none of this

    catalog = default_catalog()
    options = catalog.options(facet)
    if not options:
        return ""
    if facet == "industry":
        groups = "; ".join(o.label for o in options if o.level == 1)
        return " " + _FACET_NOTES["industry"].format(groups=groups)
    if facet in _FACET_NOTES:
        return " " + _FACET_NOTES[facet]
    if len(options) <= FACET_LIST_LIMIT:
        return " LinkedIn options: " + "; ".join(o.label for o in options) + "."
    return ""


def render_slot_catalog(slots: Iterable[SlotDef] | Track = SLOT_LIST) -> str:
    """Slot list for the extraction prompt: key, label, description, fixed choices. Given a
    track, it lists that track's slots and adds the LinkedIn options of the slots that have them."""
    facets: dict[str, str] = {}
    if isinstance(slots, str):
        from icp_chatbot.linkedin.catalog import SLOT_FACETS

        facets = SLOT_FACETS
        slots = registry(slots)
    lines = []
    for s in slots:
        line = f"- {s.key}: {s.label}. {s.description}."
        if s.choices and s.exact_range:  # QA 2026-10-27: "100-300" was snapped to "51-200"
            line += (
                " Common bands, not a closed list: " + "; ".join(s.choices) + ". Record the "
                "user's own range and currency as said (100-300 stays 100-300, rupees stay rupees)."
            )
        elif s.choices and s.single_choice:
            line += " Choices: " + "; ".join(s.choices) + "."
        elif s.choices:  # a pick-list in the chat, but the user may be more specific
            line += (
                " Common values, not a closed list: " + "; ".join(s.choices) + ". Record what the "
                "user says in full names (a country as a country, never widened to a region)."
            )
        if s.key in facets:
            line += _facet_note(facets[s.key])
        lines.append(line)
    return "\n".join(lines)
