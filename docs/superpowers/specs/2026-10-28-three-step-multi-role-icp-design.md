# Three-step, multi-role profile builder, aligned to LinkedIn: design

Date: 2026-10-28. Status: draft for approval. Supersedes the earlier draft of this file (UN M49 geography and our own option lists are gone).

## Intent

Today the ICP bot is one open chat. It spends its first turns on questions that have fixed answers: who the company sells to, target industries, company size, geography, buyer roles. A form answers those faster and more reliably than chat. The chat is better used on open-ended things (pain points, outcomes, exclusions).

The finished profile will be used to find people and companies on LinkedIn. So every option the user can pick must be a LinkedIn option, and every stored answer must map directly to a LinkedIn filter, with no translation layer of our own.

Three changes ship together:

1. **Three steps.** Step 1 collects who the user is. Step 2 is a short form of 5 core cards. Step 3 is the existing bot conversation, which starts knowing the step 2 answers.
2. **Two roles on the platform.** The user picks a role in step 1. Sales/BDE teams build an **Ideal Customer Profile** (find leads, projects, clients). HR teams build an **Ideal Candidate Profile** (find people to hire for their own office). The role decides the step 2 cards, the chat's slot list, prompts, labels and output document.
3. **LinkedIn-aligned options.** Option lists come from LinkedIn's published reference tables and from the vocabulary the LinkedIn database (`Linkedin-BE`) already stores. We keep our current filters. Sales Navigator-only filters are added when Sales Navigator API access exists.

## Goals

- Step 2 answers seed the session as known facts; the chat never re-asks them.
- One engine for both roles. Per-role differences are data (slot registry, form definition, prompts, labels), not forked code.
- Every option in every step 2 card is a LinkedIn option with a stable id. Stored answers can be turned into LinkedIn filters, and matched against the LinkedIn DB, without guessing.
- Adding Sales Navigator later means adding option catalogs and filter mappings, not redoing the form.

## Non-goals

- Calling Sales Navigator or any LinkedIn API. That comes once access exists.
- Our own taxonomies. No UN M49 geography, no custom industry or role lists.
- Sales Navigator-only facets in this release: geographic divisions (EMEA, APAC), region/state, postal code, annual revenue, department headcount, and Sales Navigator's own seniority and function lists.
- Any role beyond Sales/BDE and HR.
- Job-description output for HR.
- A separate engine for HR.

## Anti-goals (failures even if it technically works)

- Step 2 feeling like a long form. Five cards, mostly taps.
- The chat re-asking something already answered in step 2.
- A profile that looks right but cannot be mapped to a filter (an industry that is not a LinkedIn industry, a size band that matches no LinkedIn bucket).
- HR users seeing sales vocabulary ("ICP", "leads", "customers") anywhere in the UI, chat or output.
- A pre-release session breaking on load.

## LinkedIn alignment

### Where the options come from

Two sources, both read-only references:

1. **LinkedIn's published reference tables** (Microsoft Learn, LinkedIn API docs): industry codes v2, seniority codes, job function codes, company size codes. Saved as versioned JSON snapshots in the repo, one file per facet.
2. **The LinkedIn database** (`Linkedin-BE`, Prisma schema `prisma/schema.prisma`). It is the system the finished profile will be matched against, so its stored vocabulary is the check on our option lists. Pilot data: 310 companies.

A thin `OptionCatalog` interface (facet in, list of options out) wraps the snapshots. When Sales Navigator API access exists, a second catalog implementation supplies Sales Navigator's lists and the extra facets. Nothing else changes.

Every option is `{facet, id, label, level, parent, match}`:
- `id`: LinkedIn's code where one exists (industry id, size code letter, seniority code, function code, ISO country code).
- `match`: the exact value(s) stored in the LinkedIn DB for that option, because the DB does not always use the documented label (see company size).

### Field map

| Our slot | LinkedIn concept | Option source | Matched against in the LinkedIn DB |
|---|---|---|---|
| `industries`, `current_industries` | Industry | Industry codes v2 (hierarchy; groups such as Manufacturing, Financial Services contain sub-industries) | `Company.industry`, `CurrentPosition.industry` (label strings such as "IT Services and IT Consulting", "Software Development") |
| `company_size`, `current_company_size` | Company headcount | Company size codes A to I | `Company.employeeRange`, `CurrentPosition.employeeRange` |
| `company_hq_geographies`, `current_company_hq` | Headquarter location | ISO country list, then city | `Company.hqCountry` (ISO-2, e.g. IN, US), `Company.hqCity` |
| `lead_geographies`, `candidate_locations` | Geography | ISO country list, then city | `LinkedInProfile.details.country` and `.city`; `Lead.location` (free-text line) |
| `buyer_seniority` (new) | Seniority level | Seniority codes (10) | not stored; kept for the Sales Navigator filter |
| `buyer_departments` (Sales), `target_functions` (HR, new) | Function | Job function codes (35) | not stored; kept for the Sales Navigator filter |
| `buyer_roles` (Sales), `target_roles` (HR) | Title | no static list; free-text chips | `Lead.headline`, `details.title`, `currentPositions[].title` |
| `required_skills` | Skills | no static list; free-text chips | `details.skills[]` |
| `education_level` | Degrees (`urn:li:degree:N`, with a `rollup` category such as Bachelor's) | Standardized Data API `GET /v2/degrees` (API only, see below) | `details.education[].degree` (free text, matched via the degree's aliases) |
| `field_of_study` | Fields of study (`urn:li:fieldOfStudy:N`, with `rollupIds`) | Standardized Data API `GET /v2/fieldsOfStudy` (API only) | `details.education[].field` |
| `graduation_year` | Graduation year | year range | `details.education[].period` |
| `years_experience` | Years of experience ranges (`urn:li:yearsOfExperience:N`) | LinkedIn facet `yearsOfExperienceRanges`: 1 to 12+ years, a lower and an optional upper bound | derived from `details.experience[].period` |
| `current_company_hq` | Company HQ | as above | via `currentPositions[].companyId` to `Company` |

### Option lists

**Company size** (LinkedIn codes; the DB stores the range string shown in the last column):

| Code | LinkedIn label | Stored in DB as |
|---|---|---|
| A | Self-employed | no range stored |
| B | 1-10 | `2-10` |
| C | 11-50 | `11-50` |
| D | 51-200 | `51-200` |
| E | 201-500 | `201-500` |
| F | 501-1000 | `501-1000` |
| G | 1001-5000 | `1001-5000` |
| H | 5001-10,000 | `5001-10000` |
| I | 10,001+ | `10001+` |

The old bands (`201-1,000`, `1,001-5,000`, `5,000+`) are replaced by these nine. The `match` field carries the DB string, so "1-10" matches stored `2-10`.

**Seniority** (official codes): Unpaid, Training, Entry-level, Senior, Manager, Director, Vice President (VP), Chief X Officer (CxO), Partner, Owner. Sales Navigator's own list differs (it adds Entry Level Manager and Strategic); the later Sales Navigator catalog replaces this one.

**Function** (official codes, 35): Accounting / Auditing, Administrative, Advertising, Analyst, Art / Creative, Business Development, Consulting, Customer Service, Distribution, Design, Education, Engineering, Finance, General Business, HealthCare Provider, Human Resources, Information Technology, Legal, Management, Manufacturing, Marketing, Other, Public Relations, Purchasing, Product Management, Project Management, Production, QualityAssurance, Research, Sales, Science, Strategy / Planning, Supply Chain, Training, Writing / Editing.

**Industry:** the full LinkedIn industry codes v2 list, saved with this spec: `data/linkedin-industries-v2.md` (readable, indented by hierarchy) and `data/linkedin-industries-v2.json` (id, label, hierarchy, level, parent). It has 434 active industries in 20 top-level groups (Accommodation Services, Administrative and Support Services, Construction, Consumer Services, Education, Entertainment Providers, Farming Ranching Forestry, Financial Services, Government Administration, Holding Companies, Hospitals and Health Care, Manufacturing, Oil Gas and Mining, Professional Services, Real Estate and Equipment Rental Services, Retail, Technology Information and Media, Transportation Logistics Supply Chain and Storage, Utilities, Wholesale), nested up to six levels. All 56 distinct `Company.industry` values in the LinkedIn pilot DB match a label in it exactly. The picker is searchable and nested: pick a group (covers everything under it) or individual sub-industries. Values are stored as the industry label because that is what the DB stores. Two labels differ slightly between the Label column and the Hierarchy path in LinkedIn's own table (ids 3251 and 3133); the builder must pick one and test it against the DB.

**Years of experience:** LinkedIn's facet is `yearsOfExperienceRanges`, values `urn:li:yearsOfExperience:N`, range 1 to 12+ years, taking one URN as a lower bound or two as lower and upper bound. The card is a "from / to" pair of dropdowns (1 to 12+, with "12+" meaning no upper bound). Our old bands (1-3, 3-5, 5-8, 8+) are dropped.

**Degrees and fields of study:** LinkedIn's standardized degrees (`urn:li:degree:N`, each with a `rollup` to a broader category; for example Bachelor of Architecture 258, Bachelor of Applied Science 259 and Bachelor of Business Administration 450 all roll up to 200) and fields of study (`urn:li:fieldOfStudy:N` with `rollupIds`, a nested list). **LinkedIn does not publish these lists. They are only served by the Standardized Data API (`GET_ALL`), which needs an approved developer token.** We therefore cannot save them with this spec. A sync script (see "Left to the builder") pulls both lists into the repo snapshots once a token exists. The degree card shows the rollup categories; the field-of-study card is a searchable nested list. Until the snapshots exist the HR education card cannot be built.

**Geography:** two levels, the ones the LinkedIn DB stores for both people and company HQs.
- **Country**: ISO 3166-1 list. Required level.
- **City**: optional free-text chips under a selected country. LinkedIn has no static city list we can use.
- Region/state, geographic divisions and postal code arrive with Sales Navigator.
- Two pickers where the role needs both: person location and company HQ location. Always asked separately, no "same as" shortcut.

## Flow

### Step 1: About you (both roles)

All fields required:

| Field | Input |
|---|---|
| Role | dropdown: Sales / BDE, HR. Replaces the old free-text role. Compulsory. |
| Company name | text |
| Website | text |
| Industry | dropdown: LinkedIn industry list (top-level groups) |

Submitting step 1 creates the session and starts website enrichment in the background, so it runs while the user fills step 2.

### Step 2: Core profile (5 cards, role-specific)

All cards are taps and chips. Each card is required except where marked. Multi-select and range cards accept an explicit **Any**.

**Sales / BDE**

| # | Card | Slots | Input | Shown when |
|---|---|---|---|---|
| 1 | Who you sell to | `business_model` | single: B2B / B2C / Both | always |
| 2 | Target industries | `industries` | LinkedIn industry picker | B2B or Both |
| 3 | Target company size | `company_size` | multi: LinkedIn codes A to I | B2B or Both |
| 4 | Geography | `lead_geographies`, `company_hq_geographies` | two country(+city) pickers | always; HQ picker only for B2B or Both |
| 5 | Buyer roles | `buyer_seniority`, `buyer_departments`, `buyer_roles` | seniority chips, function chips, title chips (titles optional) | B2B or Both |

B2C shows only cards 1 and 4, with the geography card relabelled "Where do your customers live?" and one picker. B2C `customer_profile` is asked in the chat.

**HR**

| # | Card | Slots | Input | Shown when |
|---|---|---|---|---|
| 1 | Role you're hiring for | `target_functions`, `target_roles` | function chips (LinkedIn list), title chips | always |
| 2 | Experience level | `experience_level` | single: Fresher / Experienced / Both | always |
| 3a | Education | `education_level`, `field_of_study`, `graduation_year` | LinkedIn degree categories; LinkedIn field-of-study list; pass-out year range | Fresher: all three. Both: `education_level` only. |
| 3b | Background | `years_experience`, `current_industries`, `current_company_size`, `current_company_hq` | years of experience from / to (1 to 12+); LinkedIn industry picker; LinkedIn size codes; country(+city) picker (HQ optional) | Experienced or Both |
| 4 | Candidate location | `candidate_locations` | country(+city) picker | always |
| 5 | Must-have skills | `required_skills` | free-text skill chips, suggested from the roles in card 1 | always |

Years of experience is matched against years computed from `experience[].period`.

### Step 3: Chat

The existing interviewer, started after step 2 is submitted. Step 2 slots arrive already known. The opener acknowledges what the user gave and goes straight to the first unknown must-ask slot. Progress starts above zero.

Chat slots, asked only if still unknown:

- **Sales/BDE:** the registry was already trimmed to LinkedIn-matchable fields (commit `a25725c`: no pain points, outcomes, revenue, budget, technology or tier D). The chat asks what step 2 does not cover: `customer_profile` (B2C), `customer_segments`, `buying_triggers`, `disqualifiers`, the tier B seller-context slots (`how_we_work`, `lead_sources`, `value_proposition`), and industry-specific custom fields. Revenue is not in the registry; it returns with Sales Navigator.
- **HR:** nice-to-have skills, certifications, employment type, headcount, work mode, relocation, salary range, notice period, companies to source from, companies to avoid, seniority and reporting line, languages, hiring urgency, and `disqualifiers`. Final keys and tiers follow `domain/slots.py` conventions.

Values the chat extracts for a step 2 facet (industry, size, country, seniority, function) are resolved to the catalog. A value that cannot be resolved is kept as free text and flagged `unmapped`, and the ICP view shows that it will not become a filter.

## Architecture

### Slot registries per role

`domain/slots.py` becomes one registry per role, selected by `SessionState.track` (`"sales"` | `"hr"`). Helper functions take the track. Existing sessions load as `"sales"`.

`geographies` is replaced by `lead_geographies` and `company_hq_geographies`, both tier A, the HQ one with `skip_if` B2C. New Sales slots: `buyer_seniority`. New HR slots as in the tables above. References in `engine/planner.py` (`CUSTOMER_KEYS`), `icp/scoring.py` (`geographic_fit`), `icp/document.py`, the eval cases and the tests are updated. `SIZE_BANDS` is replaced by the LinkedIn codes. HR conditional slots use `skip_if` keyed on `experience_level`.

### Slot values carry option references

`SlotValue.values` stays `list[str]` (canonical labels). `SlotValue` gains an optional `refs: list[OptionRef]` with `{facet, id, level, parent}` for each value that resolved to a catalog option, and a list of unresolved values. Old data without `refs` loads unchanged.

### Legacy data

Saved session state is JSON, so no Alembic migration. A state load hook maps legacy `geographies` to `lead_geographies` (free text, unresolved until the user edits) and leaves `company_hq_geographies` unknown. Legacy `company_size` values that match a new bucket are resolved; others stay free text and unmapped.

### Form definition is data, served by the backend

A role's step 2 is a declarative card list (card id, slots, input type, facet, `show_if`, required). Options come from the `OptionCatalog`. The frontend renders any role's cards generically. The existing `GET /fallback-form` is the precedent.

### API changes

- `POST /sessions` (step 1): body gains `track`. The UI always sends it; the API defaults a missing `track` to `"sales"`. It creates the session, kicks off enrichment, and returns the session id with the step 2 form definition. It no longer returns the first chat question. The old free-text `role` stays accepted but unused.
- `GET /profile-form?track=...`: the card definitions.
- `GET /options/{facet}?q=...`: catalog options for a facet, with search, for the large lists (industry, country).
- `POST /sessions/{id}/profile` (step 2 submit): validates required cards, writes the slots as stated values with `refs`, then runs the interviewer opener and returns the existing `TurnResponse`. Same validation style as `POST /sessions/{id}/fallback`.
- Chat endpoints are unchanged.

### Seeding rule

Step 2 answers are `SlotSource.STATED` at confidence 0.9, `source_turn` 0, evidence "step 2 form". The model must not overwrite them with lower-confidence inference.

### Scoring (both roles)

`icp/scoring.py` keeps one engine: criteria that read slots, rules built from the slot values, weights summing to 100, and renormalisation over the criteria that apply and have data. Defaults are chosen by track.

HR criteria (defaults, editable like Sales):

| Criterion | Weight | Reads | Rule |
|---|---|---|---|
| Role fit | 25 | `target_functions`, `target_roles` | list |
| Experience fit | 20 | `years_experience` | range (`"3-8"`, `"3+"`) |
| Skills fit | 20 | `required_skills` | list |
| Education fit | 15 | `education_level`, `field_of_study`, `graduation_year` | list, list, range with a narrow tolerance (about 2 years) |
| Location fit | 10 | `candidate_locations` | list |
| Background fit | 10 | `current_industries`, `current_company_size` | list, range |

- A criterion with no rules (a branch that was skipped, or an Any answer) is not applicable, and the rest renormalise. A Fresher profile therefore scores on role, skills, education and location; an Experienced profile on role, experience, skills, location and background.
- `years_experience` and `graduation_year` join `RANGE_SLOTS`. Range tolerance becomes per slot (default 0.2; `graduation_year` about 0.001), because 20% of a year like 2024 would accept any year.
- HR disqualifier fields: `candidate_locations`, `current_industries`, `current_company_size`.
- `validate_model` checks slot names against the model's track, not the Sales registry.
- Sales criteria are unchanged except `geographic_fit`, which reads `lead_geographies` and `company_hq_geographies` (Task 2).

### Prompts, labels, output

- Interviewer, opener and extraction prompts are written per role in `prompts.py`. The extraction prompt lists the catalog options for the facet slots so values resolve to them. The HR set speaks of candidates, never customers or leads.
- The output document: Ideal Candidate Profile for HR (role and seniority, background, skills, location and work terms, compensation and timing, sourcing, exclusions) and the current ICP for Sales/BDE. Both gain a "LinkedIn filters" block listing each facet value with its id, and marking unmapped values. Same builder, review, edit, approve and verdict flow.

### Frontend

`App.tsx` stages: step 1, step 2, chat, then the existing building and review stages. A three-step progress indicator sits above all of them. Saved-stage restore reads `step` from the session view and falls back to step 1 for unknown stages.

### Testing and release gate

- Unit tests: both registries, `skip_if` branches, catalogs (every option has an id, `match` values are unique), legacy load, step 2 seeding, Any handling.
- **Catalog reconciliation test against the LinkedIn DB:** every distinct `Company.industry` and `Company.employeeRange` value in the pilot data must resolve to a catalog option. Failures are listed, not ignored.
- API tests for the three-step flow for each role.
- New HR eval cases (fresher, experienced, both, vague request); existing Sales cases updated for the geography split and new size bands.
- Prompts changed here must pass the existing eval gate before release. Earlier eval results do not count.

## Behaviour details

**Stage and in-flight sessions.** `SessionState` gains `step`: `"profile"` (step 2 not yet submitted) or `"chat"`. A session with no `step` loads as `"chat"` and `track="sales"`; it skips step 2 and continues, with new slots unknown so the chat asks them. A session whose step 2 was never submitted has `step="profile"`; resuming it lands on step 2. Chat endpoints return 409 for a session still in `"profile"`.

**Skip rules.** `business_model` already exists as a tier A single-choice slot. `skip_if` compares exact values, so an unset `business_model` or any value other than B2C means "not skipped".

**Any.** Stored as the sentinel `"Any"`, exclusive. Offered on multi-select and range cards only (industries, size, geography pickers, seniority, function, current industries, current size, candidate location). Not on `business_model`, `experience_level` or `education_level`. Counts as an answer in `real_values`; an Any slot builds no scoring rule, so its criterion drops out and the other weights renormalise; the document renders "Any"; it adds no filter in the LinkedIn block.

**Enrichment for HR.** The website describes the employer. For HR it feeds only the opener context and the company industry; it does not write Sales slots.

**HR must-ask and stop rules.** Tier A for HR chat: `employment_type`, `work_mode`, `salary_range`, `disqualifiers`. The rest are tier B. Stop rules follow the current engine (`enough_for_icp`: the interviewer's own call plus a floor). The HR floor is `target_roles` plus three more step 2 or chat facts, mirroring `CUSTOMER_KEYS` for Sales. Tiers are A, B, C only; there are no topics or tier D.

**Vocabulary.** Only user-visible strings change for HR. Internal route names (`/icp`), field names and identifiers stay.

**Review, edit, verdict for HR.** Same edit and approve flow, with an HR section map. The verdict question is relabelled for HR and kept.

**Step 2 validation.** Every card shown for the user's answers is required, except `current_company_hq` and the title chips in Sales card 5. Fresher requires `education_level`, `field_of_study`, `graduation_year`. Experienced requires `years_experience` (at least the lower bound), `current_industries`, `current_company_size`. Both requires `education_level` plus the Experienced set. Required means at least one value or Any.

## Left to the builder

- Exact HR chat slot keys, tiers and importance values.
- **Prerequisite, needs the user:** a LinkedIn developer app and token with Standardized Data access. With it, a sync script (`scripts/sync_linkedin_catalogs.py`) calls `GET /v2/degrees` and `GET /v2/fieldsOfStudy` with pagination (and can also refresh industries, seniorities, functions) and writes the repo snapshots. The same call can later fill `skills` and `titles` typeahead. Without a token the HR education card stays unbuilt.
- The saved snapshot format for the reference tables and how they are refreshed.
- Which of LinkedIn's `locations` entity levels (country group, country, state, region) the later geography catalog uses.
- How a lead's free-text `Lead.location` is matched to a country (the DB stores a line, not a code).
- Skill and field-of-study suggestions per role, and the degree-keyword match against `education[].degree`.
- Whether `track` is also a DB column for metrics.
- Whether the user can return to step 2 once chat has begun. Default: no; later edits go through the existing ICP edit flow.

## Calls made without the user (marked for review)

- Geography levels are Country then City, because those are the levels the LinkedIn DB stores. Region/state and divisions wait for Sales Navigator.
- Company size uses LinkedIn's nine codes, matched to the DB's stored strings (including `2-10` for code B).
- Buyer roles card becomes Seniority + Function + Title chips, because those are LinkedIn's three facets. Seniority and function are not stored in the DB, so they are kept for the Sales Navigator filter and cannot be matched yet.
- Titles and skills are free-text chips: LinkedIn has no static list for either.
- Years of experience uses LinkedIn's 1 to 12+ lower/upper range. Degrees and fields of study use LinkedIn's standardized lists, which exist only behind the API, so they depend on the sync prerequisite above.
- Step 1 industry is a dropdown from the LinkedIn industry list. You only named the Role dropdown explicitly.
- New `track` field instead of reusing `role`: the existing `role` is a job title used as `speaker_role`.
- Session created at the end of step 1 to hide enrichment latency.
- Step 2 answers seeded as STATED at confidence 0.9.
- Unresolvable chat values are kept as free text and flagged `unmapped`.
- `Any` stored as an exclusive sentinel.
- Session `step` field, 409 on chat while in step 2, old sessions skipping step 2.
- `track` defaults to Sales at the API.
- HR scoring weights and the HR disqualifier fields are my defaults. HR enrichment feeds only opener context and industry.
- HR tier A set: `employment_type`, `work_mode`, `salary_range`, `disqualifiers`.
- Only user-visible strings are renamed for HR.
- HR verdict question relabelled, not removed.
- Legacy `geographies` maps to `lead_geographies` only.
