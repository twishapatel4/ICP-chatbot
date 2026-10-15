"""The ICP slot registry: every field the interview can fill (spec sections 5.5 and 6.1)."""

from dataclasses import dataclass
from enum import StrEnum


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
    blocks_completion: bool | None = None  # default: tier A blocks, others do not
    skip_if: tuple[
        tuple[str, str], ...
    ] = ()  # (slot key, value): slot does not apply if it matches

    @property
    def blocks(self) -> bool:
        if self.blocks_completion is None:
            return self.tier == Tier.A
        return self.blocks_completion


def _s(key, section, label, description, tier, importance=0.5, **kw) -> SlotDef:
    return SlotDef(key, section, label, description, Tier(tier), importance, **kw)


_B2C = (("business_model", "B2C"),)

SLOT_LIST: tuple[SlotDef, ...] = (
    # 1. Seller context (inferred from signup and the website, not asked)
    _s("product", "seller_context", "Product or offering", "What the company sells", "C", 0.9),
    _s(
        "business_model",
        "seller_context",
        "Business model",
        "B2B, B2C or both",
        "C",
        0.9,
        choices=BUSINESS_MODELS,
    ),
    _s(
        "value_proposition", "seller_context", "Value proposition", "Why customers buy it", "C", 0.5
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
        "industries",
        "target_firmographics",
        "Target industries and sub-industries",
        "Industries of the companies to sell to",
        "A",
        1.0,
    ),
    _s(
        "company_size",
        "target_firmographics",
        "Target company size",
        "Employee count of target companies",
        "A",
        0.9,
        choices=SIZE_BANDS,
    ),
    _s(
        "revenue_range",
        "target_firmographics",
        "Target revenue range",
        "Annual revenue of target companies",
        "A",
        0.6,
        choices=REVENUE_BANDS,
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
        if s.choices:
            line += " Choices: " + "; ".join(s.choices) + "."
        lines.append(line)
    return "\n".join(lines)
