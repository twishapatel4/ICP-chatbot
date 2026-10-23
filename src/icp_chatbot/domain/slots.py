"""The ICP slot registry: every field the interview can fill (spec sections 5.5 and 6.1)."""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field


class Tier(StrEnum):
    A = "A"  # must be asked
    B = "B"  # asked if relevant and time allows
    C = "C"  # inferred or defaulted, never asked
    D = "D"  # detail mode only


SECTION_ORDER = (
    "seller_context",
    "target_firmographics",
    "business_profile",
    "problems_and_needs",
    "technology_profile",
    "commercial_profile",
    "buying_structure",
    "intent_and_timing",
    "success_and_exclusions",
    "industry_specifics",  # fields that only this company's industry needs
)

SIZE_BANDS = ("1-10", "11-50", "51-200", "201-1,000", "1,001-5,000", "5,000+")
REVENUE_BANDS = ("<$1M", "$1M-$10M", "$10M-$50M", "$50M-$250M", "$250M-$1B", ">$1B")
REGIONS = (
    "North America",
    "Europe",
    "Middle East",
    "South Asia",
    "Southeast Asia",
    "East Asia",
    "Latin America",
    "Africa",
    "Oceania",
)
BUSINESS_MODELS = ("B2B", "B2C", "Both")

# Tier B topics. The quick-mode stop rule needs at least three of these asked.
STOP_TOPICS = frozenset({"tools", "commercial", "process", "triggers", "success"})


@dataclass(frozen=True)
class SlotDef:
    key: str
    section: str
    label: str
    description: str
    tier: Tier
    importance: float = 0.5
    topic: str | None = None
    choices: tuple[str, ...] = ()
    single_choice: bool = False  # one answer only: the user picks one option, not several
    open_ended: bool = False  # described in the user's own words: never offer suggested answers
    blocks_completion: bool | None = None  # default: tier A blocks, others do not
    skip_if: tuple[
        tuple[str, str], ...
    ] = ()  # (slot key, value): slot does not apply if it matches

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


def with_custom(fields: list[CustomField]) -> list[SlotDef]:
    """The standard slots plus the fields this industry needs (the strongest ones block)."""
    ranked = sorted(fields, key=lambda f: -f.importance)
    important = {f.key for f in ranked[:CUSTOM_BLOCKING]}
    return [*SLOT_LIST, *(custom_slot_def(f, f.key in important) for f in fields)]


def _s(key, section, label, description, tier, importance=0.5, **kw) -> SlotDef:
    return SlotDef(key, section, label, description, Tier(tier), importance, **kw)


_B2C = (("business_model", "B2C"),)
_B2B = (("business_model", "B2B"),)

SLOT_LIST: tuple[SlotDef, ...] = (
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
        choices=SIZE_BANDS,
        single_choice=True,
        skip_if=_B2C,
    ),
    _s(
        "revenue_range",
        "target_firmographics",
        "Target revenue range",
        "Annual revenue of target companies",
        "A",
        0.6,
        choices=REVENUE_BANDS,
        single_choice=True,
        skip_if=_B2C,
    ),
    _s(
        "geographies",
        "target_firmographics",
        "Target geographies",
        "Regions or countries to sell in",
        "A",
        0.9,
        choices=REGIONS,
    ),
    _s(
        "company_type",
        "target_firmographics",
        "Company type",
        "Enterprise, SME or startup",
        "C",
        0.3,
    ),
    _s("ownership", "target_firmographics", "Ownership", "Private, public or government", "C", 0.2),
    _s(
        "years_in_operation",
        "target_firmographics",
        "Years in operation",
        "Age of target companies",
        "C",
        0.1,
    ),
    _s(
        "funding_stage",
        "target_firmographics",
        "Funding stage",
        "Funding stage of targets",
        "C",
        0.3,
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
    _s("sites", "business_profile", "Number of sites", "Offices or facilities", "C", 0.2),
    _s(
        "operational_complexity",
        "business_profile",
        "Operational complexity",
        "How complex target operations are",
        "C",
        0.2,
    ),
    _s("growth_stage", "business_profile", "Growth stage", "Growth stage of targets", "C", 0.2),
    _s(
        "org_structure",
        "business_profile",
        "Organizational structure",
        "Departments, locations, subsidiaries",
        "C",
        0.1,
    ),
    # 4. Problems and needs
    _s(
        "pain_points",
        "problems_and_needs",
        "Pain points",
        "Problems the target companies have",
        "A",
        1.0,
    ),
    _s(
        "desired_outcomes",
        "problems_and_needs",
        "Desired outcomes",
        "Business outcomes they want",
        "A",
        0.8,
    ),
    _s(
        "current_processes",
        "problems_and_needs",
        "Current manual processes",
        "Inefficient processes in use today",
        "B",
        0.4,
        topic="needs",
    ),
    _s(
        "use_cases",
        "problems_and_needs",
        "Use cases",
        "How the product is used",
        "B",
        0.4,
        topic="needs",
    ),
    # 5. Technology profile
    _s(
        "tech_stack",
        "technology_profile",
        "Technology stack",
        "Software the targets use",
        "B",
        0.6,
        topic="tools",
    ),
    _s(
        "incumbents",
        "technology_profile",
        "Incumbent tools and competitors",
        "Existing vendors or competing solutions",
        "B",
        0.7,
        topic="tools",
    ),
    _s(
        "integration_needs",
        "technology_profile",
        "Integration needs",
        "Systems the product must work with",
        "B",
        0.4,
        topic="tools",
    ),
    _s("infrastructure", "technology_profile", "Infrastructure", "Cloud or on-premises", "D", 0.3),
    _s(
        "digital_maturity",
        "technology_profile",
        "Digital maturity",
        "How digitally mature targets are",
        "D",
        0.3,
    ),
    # 6. Commercial profile
    _s(
        "budget_range",
        "commercial_profile",
        "Budget range",
        "Budget available to targets",
        "B",
        0.7,
        topic="commercial",
    ),
    _s(
        "deal_size",
        "commercial_profile",
        "Typical deal size",
        "Typical contract value",
        "B",
        0.8,
        topic="commercial",
    ),
    _s(
        "roi_expectation",
        "commercial_profile",
        "ROI expectation",
        "Return targets expect",
        "D",
        0.3,
    ),
    _s(
        "purchase_frequency",
        "commercial_profile",
        "Purchase frequency",
        "How often they buy",
        "D",
        0.2,
    ),
    _s(
        "expansion_potential",
        "commercial_profile",
        "Expansion potential",
        "Upsell and expansion potential",
        "D",
        0.3,
    ),
    # 7. Buying structure
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
        "sales_cycle",
        "buying_structure",
        "Sales cycle",
        "Typical length of the sales cycle",
        "B",
        0.6,
        topic="process",
    ),
    _s(
        "procurement",
        "buying_structure",
        "Procurement process",
        "How purchases are procured",
        "B",
        0.5,
        topic="process",
    ),
    _s(
        "approvals",
        "buying_structure",
        "Approval requirements",
        "Approvals needed to buy",
        "B",
        0.3,
        topic="process",
    ),
    _s(
        "contracting",
        "buying_structure",
        "Contracting requirements",
        "Contract terms they require",
        "D",
        0.3,
    ),
    _s(
        "objections",
        "buying_structure",
        "Objections and barriers",
        "Common reasons they do not buy",
        "D",
        0.5,
    ),
    # 8. Intent and timing
    _s(
        "buying_triggers",
        "intent_and_timing",
        "Buying triggers",
        "Events that make a company ready to buy",
        "B",
        0.8,
        topic="triggers",
    ),
    # 9. Success and exclusions
    _s(
        "disqualifiers",
        "success_and_exclusions",
        "Exclusions and disqualifiers",
        "Companies to avoid",
        "A",
        0.9,
    ),
    _s(
        "success_definition",
        "success_and_exclusions",
        "What success looks like",
        "What a successful customer achieves",
        "B",
        0.6,
        topic="success",
    ),
    _s(
        "time_to_value",
        "success_and_exclusions",
        "Time to value",
        "How fast customers see value",
        "D",
        0.3,
    ),
    _s(
        "implementation_complexity",
        "success_and_exclusions",
        "Implementation complexity",
        "Effort to deploy",
        "D",
        0.3,
    ),
    _s(
        "adoption_readiness",
        "success_and_exclusions",
        "Adoption readiness",
        "How ready targets are to adopt",
        "D",
        0.3,
    ),
    _s(
        "retention_likelihood",
        "success_and_exclusions",
        "Retention likelihood",
        "Likelihood they stay customers",
        "D",
        0.2,
    ),
    _s(
        "reference_potential",
        "success_and_exclusions",
        "Reference potential",
        "Likelihood to become references",
        "D",
        0.2,
    ),
    _s(
        "compliance",
        "success_and_exclusions",
        "Compliance and data residency",
        "Regulations, certifications and residency needs",
        "D",
        0.4,
    ),
    _s(
        "switching_costs",
        "success_and_exclusions",
        "Switching costs",
        "Cost of leaving an incumbent",
        "D",
        0.4,
    ),
    _s(
        "market_position",
        "success_and_exclusions",
        "Market position",
        "Competitors and market standing",
        "D",
        0.3,
    ),
)

SLOTS: dict[str, SlotDef] = {s.key: s for s in SLOT_LIST}
assert len(SLOTS) == len(SLOT_LIST), "duplicate slot keys"


def get_slot(key: str) -> SlotDef:
    return SLOTS[key]


def slots_in_tier(tier: Tier) -> list[SlotDef]:
    return [s for s in SLOT_LIST if s.tier == tier]


def render_slot_catalog() -> str:
    """Slot list for the extraction prompt: key, label, description, fixed choices."""
    lines = []
    for s in SLOT_LIST:
        line = f"- {s.key}: {s.label}. {s.description}."
        if s.choices and s.single_choice:
            line += " Choices: " + "; ".join(s.choices) + "."
        elif s.choices:  # a pick-list in the chat, but the user may be more specific
            line += (
                " Common values, not a closed list: " + "; ".join(s.choices) + ". Record what the "
                "user says in full names (a country as a country, never widened to a region)."
            )
        lines.append(line)
    return "\n".join(lines)
