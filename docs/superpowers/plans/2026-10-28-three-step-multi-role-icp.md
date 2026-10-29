# Three-step, multi-role, LinkedIn-aligned profile builder: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the ICP chat into three steps (about you, a 5-card form, the chat), add an HR "Ideal Candidate Profile" role beside Sales/BDE, and make every option a LinkedIn option so the result maps to LinkedIn filters.

**Architecture:** Same interview engine for both roles. A role ("track") selects a slot registry, a step 2 form definition, prompts and output labels. Option lists come from versioned LinkedIn snapshots behind a small `OptionCatalog`. Step 2 answers are written as stated slots, then the existing interviewer opens step 3.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy (session state is JSON), pytest (`.venv/bin/python -m pytest`), ruff, React + TypeScript + Vite (`cd frontend && npm run build`).

**Spec:** `docs/superpowers/specs/2026-10-28-three-step-multi-role-icp-design.md` (data files in `docs/superpowers/specs/data/`).

## Global Constraints

- Baseline before starting: `.venv/bin/python -m pytest -q` shows 425 passed. Every task ends with the full suite green and `.venv/bin/ruff check src tests` clean (line length 100).
- Tiers are A, B, C only. No topics, no tier D (removed in commit `a25725c`).
- Company size options are exactly LinkedIn's nine: A Self-employed, B 1-10 (DB `2-10`), C 11-50, D 51-200, E 201-500, F 501-1000 (DB `501-1000`), G 1001-5000, H 5001-10,000 (DB `5001-10000`), I 10,001+ (DB `10001+`).
- Seniority options (10): Unpaid, Training, Entry-level, Senior, Manager, Director, Vice President (VP), Chief X Officer (CxO), Partner, Owner. Function options: the 35 job function codes in the spec.
- Industry options: the 434 active nodes in `docs/superpowers/specs/data/linkedin-industries-v2.json`; stored value is the label.
- Geography has two levels only: Country (ISO 3166-1, required) then City (free-text chips under a country).
- Step 2 values are `SlotSource.STATED`, confidence 0.9, `source_turn` 0, evidence "step 2 form". **Any** is the exclusive value `"Any"`, only on multi-select and range cards.
- Session state: `track` (`"sales"` | `"hr"`, default `"sales"`), `step` (`"profile"` | `"chat"`, default `"chat"` for old sessions). Chat endpoints return 409 while `step == "profile"`.
- HR users never see "ICP", "lead" or "customer" in UI, chat prompts or the document. Route names and identifiers keep their current names.
- HR scoring uses the same engine with these default criteria and weights (sum 100): Role fit 25 (`target_functions`, `target_roles`), Experience fit 20 (`years_experience`), Skills fit 20 (`required_skills`), Education fit 15 (`education_level`, `field_of_study`, `graduation_year`), Location fit 10 (`candidate_locations`), Background fit 10 (`current_industries`, `current_company_size`). A slot answered **Any** builds no rule.
- `track` missing from `POST /sessions` means `"sales"`.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Out of scope: calling any LinkedIn or Sales Navigator API at runtime; region/state/postal geography; revenue.

## Review Focus

- Refresh or close the tab at step 2, reopen: lands on step 2 with answers kept (Task 6, Task 10).
- B2C Sales: cards 2, 3, 5 and the HQ picker are skipped and not required (Task 5).
- "Any" combined with a specific pick, or sent to a single-select card: rejected or normalised, never stored as a mixed list (Task 5).
- HR "Both": education level asked, field of study and graduation year not; experienced set required (Task 5).
- A pre-release session containing `geographies` loads, resumes in chat, and is asked the HQ geography (Task 2).
- Free-text chips (titles, skills, cities) containing a long string or prompt-injection text: length-capped, count-capped, and quoted as data in prompts (Task 5, Task 7).

## File Structure

- Create `src/icp_chatbot/linkedin/__init__.py`, `catalog.py`, `data/*.json`: option snapshots and the catalog.
- Create `src/icp_chatbot/engine/profile_form.py`: step 2 card definitions, validation, seeding.
- Create `scripts/sync_linkedin_catalogs.py`: pulls degrees and fields of study from the Standardized Data API.
- Modify `src/icp_chatbot/domain/slots.py` (registries per track), `domain/state.py` (track, step, refs, legacy load), `engine/planner.py`, `engine/extraction.py`, `engine/interview.py`, `engine/questions.py`, `engine/fallback.py`, `icp/document.py`, `icp/builder.py`, `icp/scoring.py`, `prompts.py`, `versions.py`, `api/app.py`, `api/schemas.py`, `eval/cases.py`, `eval_cases/*.json`.
- Modify `frontend/src/App.tsx`, `api.ts`, `types.ts`; create `frontend/src/components/StepOne.tsx`, `StepTwo.tsx`, `StepIndicator.tsx`.
- Tests: new `tests/test_linkedin_catalog.py`, `test_profile_form.py`, `test_three_step_api.py`, `test_hr_track.py`; existing tests updated where slots are renamed.

---

### Task 1: LinkedIn option catalog

**Files:**
- Create: `src/icp_chatbot/linkedin/{__init__,catalog}.py`, `src/icp_chatbot/linkedin/data/{industries,company_sizes,seniorities,functions,countries}.json`, `tests/data/linkedin_db_vocab.json`
- Test: `tests/test_linkedin_catalog.py`

**Interfaces:**
- Produces: `Option(facet: str, id: str, label: str, level: int = 1, parent: str | None = None, match: tuple[str, ...] = ())` (frozen dataclass); `FACETS = ("industry", "company_size", "seniority", "function", "country", "degree", "field_of_study")`; `SLOT_FACETS: dict[str, str]` mapping slot key to facet (`industries`/`current_industries`→industry, `company_size`/`current_company_size`→company_size, `buyer_seniority`/`target_seniority`→seniority, `buyer_departments`/`target_functions`→function, `lead_geographies`/`company_hq_geographies`/`candidate_locations`/`current_company_hq`→country, `education_level`→degree, `field_of_study`→field_of_study); `class OptionCatalog` with `has(facet) -> bool`, `options(facet) -> list[Option]`, `search(facet, q, limit=50) -> list[Option]`, `resolve(facet, text) -> Option | None` (case-insensitive match on label, id, or any `match` value); `default_catalog() -> OptionCatalog` (cached).

- [ ] **Step 1: Build the DB vocabulary fixture.** Run the `pg_restore -a -t Company` extraction from the spec research against `Projects/LinkedIn/linkedin-pilot-20261026-1708.dump` and write only the distinct `industry`, `employeeRange` and `hqCountry` values to `tests/data/linkedin_db_vocab.json` as `{"industry": [...], "employeeRange": [...], "hqCountry": [...]}`. No lead, profile or personal data.
- [ ] **Step 2: Write failing tests** in `tests/test_linkedin_catalog.py`: `test_industry_snapshot_has_434_unique_ids`; `test_every_db_industry_resolves` (each fixture industry → `resolve("industry", v)` not None); `test_every_db_size_range_resolves_to_a_company_size` (fixture ranges `2-10`, `11-50`, … `10001+` resolve; `resolve("company_size", "1-10").match` contains `"2-10"`); `test_company_sizes_are_the_nine_linkedin_codes`; `test_seniority_has_10_and_function_35`; `test_every_db_country_code_resolves` (`IN`, `US`, `GB` …); `test_search_is_case_insensitive_and_limited`; `test_missing_degree_catalog_reports_has_false`.
- [ ] **Step 3: Run** `.venv/bin/python -m pytest tests/test_linkedin_catalog.py -v`. Expected: FAIL (module missing).
- [ ] **Step 4: Implement the catalog.** Copy the industry JSON from the spec data folder (keep `id`, `label`, `hierarchy`, `level`, `parent`). Write `company_sizes.json` with the nine rows and `match` per the Global Constraints table, `seniorities.json` and `functions.json` from the spec, and `countries.json` as the ISO 3166-1 alpha-2 list (id = code, label = English short name). `degree` and `field_of_study` have no snapshot yet; `has()` returns False for them.
- [ ] **Step 5: Run the tests.** Expected: PASS. Full suite green.
- [ ] **Step 6: Commit** `feat: LinkedIn option catalog with industry, size, seniority, function, country snapshots`.

### Task 2: Slot registries per track and the Sales geography split

**Files:**
- Modify: `domain/slots.py`, `domain/state.py`, `engine/planner.py:34`, `engine/extraction.py`, `engine/fallback.py`, `icp/document.py:161`, `icp/builder.py`, `icp/scoring.py:26`, `eval/cases.py`, `eval_cases/*.json`, existing tests that mention `geographies`/`SIZE_BANDS`
- Test: `tests/test_slots_and_state.py`

**Interfaces:**
- Produces: `Track = Literal["sales", "hr"]`; `registry(track: Track) -> tuple[SlotDef, ...]`; `slot_map(track: Track) -> dict[str, SlotDef]`; `SessionState.track: Track = "sales"`, `SessionState.step: Literal["profile", "chat"] = "chat"`. `SLOT_LIST`/`SLOTS` remain the Sales registry. New Sales slots: `lead_geographies` (tier A, importance 0.9), `company_hq_geographies` (tier A, 0.9, `skip_if=_B2C`), `buyer_seniority` (tier A, 0.8, `skip_if=_B2C`). `geographies` and `SIZE_BANDS` removed; `company_size` choices become the nine LinkedIn labels.

- [ ] **Step 1: Write failing tests:** `test_sales_registry_has_split_geography_and_no_geographies`; `test_company_hq_skipped_for_b2c` (via `state.is_applicable`); `test_legacy_state_with_geographies_loads_into_lead_geographies` (`SessionState.model_validate({"slots": {"geographies": {...}}})` → `lead_geographies` set, `company_hq_geographies` absent, `track == "sales"`, `step == "chat"`); `test_state_without_track_defaults_to_sales`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the registries and a `model_validator(mode="before")` on `SessionState` for the legacy mapping; `slot_defs`, `slot_def`, `is_applicable` read `registry(self.track)`.
- [ ] **Step 4: Migrate dependants** so the whole suite is green: `CUSTOMER_KEYS` uses `lead_geographies`; `geographic_fit` reads `("lead_geographies", "company_hq_geographies")` keeping weight 25; `render_summary` and the builder use the new keys; `eval/cases.py` and `eval_cases/*.json` rename the slot; `RANGE_SLOTS` still `{"company_size"}`: confirm the range parser handles `10001+` and `5001-10000`, adding cases to `tests/test_scoring.py` if not.
- [ ] **Step 5: Run the full suite.** Expected: all pass (count increases by the new tests).
- [ ] **Step 6: Commit** `refactor: per-track slot registries, split Sales geography, LinkedIn size bands`.

### Task 3: HR registry and floor

**Files:**
- Modify: `domain/slots.py`, `engine/planner.py`
- Test: `tests/test_hr_track.py`

**Interfaces:**
- Consumes: Task 2 `registry`, `Track`.
- Produces: `HR_SLOT_LIST` and `HR_SECTION_ORDER = ("role_and_seniority", "background", "skills", "location_and_terms", "compensation_and_timing", "sourcing", "exclusions")`; `section_order(track) -> tuple[str, ...]`; `customer_keys(track) -> tuple[str, ...]` (Sales: existing; HR: `("target_roles", "target_functions", "required_skills", "candidate_locations", "education_level", "years_experience")`); `icp_floor_met` and `enough_for_icp` use `customer_keys(state.track)` and for HR require `target_roles` told.
- HR slots (key, tier): `target_functions` A, `target_roles` A, `experience_level` A (single choice: Fresher, Experienced, Both), `education_level` A (skip if Experienced), `field_of_study` A and `graduation_year` A (skip if Experienced or Both), `years_experience` A, `current_industries` A, `current_company_size` A (all three skip if Fresher), `current_company_hq` B (skip if Fresher), `candidate_locations` A, `required_skills` A, `employment_type` A, `work_mode` A, `salary_range` A, `disqualifiers` A, and tier B: `nice_to_have_skills`, `certifications`, `headcount`, `relocation`, `notice_period`, `source_companies`, `avoid_companies`, `target_seniority`, `languages`, `urgency`.

- [ ] **Step 1: Write failing tests:** `test_hr_registry_keys_are_unique_and_have_sections`; `test_fresher_skips_experience_slots`; `test_both_skips_field_of_study_and_graduation_year_but_keeps_education_level`; `test_experienced_skips_all_education_slots`; `test_hr_floor_needs_role_and_three_facts`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the HR registry and track-aware helpers.
- [ ] **Step 4: Run the full suite.** Expected: pass.
- [ ] **Step 5: Commit** `feat: HR slot registry and floor`.

### Task 3b: HR scoring

**Files:**
- Modify: `icp/scoring.py` (`RANGE_SLOTS` line 19, `CRITERIA_DEFS` line 22, `build_rule`, `default_scoring_model`, `DISQUALIFIER_FIELDS`, `validate_model`), `icp/builder.py:191,202`, `icp/review.py` (calls to `validate_model`, `refresh_model`)
- Test: `tests/test_scoring.py`, `tests/test_hr_track.py`

**Interfaces:**
- Consumes: Task 3 `slot_map(track)`; `ICPDocument.track` from Task 8 is not needed yet, so `default_scoring_model` and `validate_model` take the track as an argument and the builder passes `state.track`.
- Produces: `CRITERIA_BY_TRACK: dict[Track, tuple[...]]` (Sales unchanged except `geographic_fit` reading `("lead_geographies", "company_hq_geographies")`; HR as in the Global Constraints); `RANGE_TOLERANCE: dict[str, float]` with `graduation_year: 0.001` (default 0.2); `RANGE_SLOTS` gains `years_experience`, `current_company_size`, `graduation_year`; `default_scoring_model(slot_values, disqualifiers=None, track="sales")`; `validate_model(model, track="sales") -> list[str]`; `DISQUALIFIER_FIELDS` gains `candidate_locations`, `current_industries`, `current_company_size`. `build_rule` returns `None` for `["Any"]` so the criterion becomes not applicable; `MatchRule.tolerance` is set from `RANGE_TOLERANCE`.

- [ ] **Step 1: Write failing tests:** `test_hr_default_weights_sum_to_100_and_use_the_six_criteria`; `test_fresher_profile_scores_without_experience_and_background` (those criteria `applicable` False, score is renormalised over the rest); `test_years_experience_range_parses_3_dash_8_and_3_plus` (candidate `["6"]` → 1.0, `["1"]` → 0.0, `["3+"]` rule has `max None`); `test_graduation_year_far_outside_range_scores_zero` (rule 2024-2026, candidate 2015 → 0.0, 2027 → 0.5); `test_any_answer_builds_no_rule` (Sales `industries=["Any"]` and HR `candidate_locations=["Any"]`); `test_hr_candidate_scores_skills_by_loose_match` (rule `["Python"]`, candidate `["Python, Django"]` → 1.0); `test_validate_model_uses_the_tracks_registry` (an HR model with `required_skills` is valid for `hr`, reports an unknown slot for `sales`); `test_hr_disqualifier_on_candidate_location`; `test_required_hr_criterion_failing_makes_candidate_unqualified`; `test_builder_emits_hr_scoring_model_for_hr_state` (in `tests/test_builder.py` style).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the track-aware criteria, per-slot tolerance and `Any` handling; `review.py` passes `document.track` once Task 8 adds it (until then it defaults to `"sales"`, and Task 8 makes the HR path real).
- [ ] **Step 4: Run the full suite.** Expected: pass.
- [ ] **Step 5: Commit** `feat: HR scoring criteria on the shared scoring engine`.

### Task 4: Option references and "Any"

**Files:**
- Modify: `domain/state.py` (SlotValue, `real_values`), `engine/extraction.py`
- Test: `tests/test_option_refs.py`

**Interfaces:**
- Consumes: Task 1 `default_catalog`, `SLOT_FACETS`.
- Produces: `OptionRef(facet: str, id: str, level: int = 1, parent: str | None = None)`; `SlotValue.refs: list[OptionRef] = []` and `SlotValue.unmapped: list[str] = []`; `ANY = "Any"`; `resolve_slot_values(slot_key: str, values: list[str], catalog=None) -> tuple[list[str], list[OptionRef], list[str]]` returning canonical labels, refs, unmapped. `real_values` treats `ANY` as a real answer.

- [ ] **Step 1: Write failing tests:** `test_resolve_industry_label_gives_ref_with_id`; `test_resolve_size_range_string_uses_match` (`"11-50"` → id `C`); `test_unresolvable_value_is_kept_as_unmapped`; `test_slot_without_facet_resolves_nothing`; `test_any_is_a_real_answer`; `test_old_slot_value_without_refs_loads`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the resolver and call it when extraction applies a stated update for a slot in `SLOT_FACETS`, filling `refs` and `unmapped`.
- [ ] **Step 4: Full suite green. Commit** `feat: slot values carry LinkedIn option refs; Any counts as an answer`.

### Task 5: Step 2 form definition, validation, seeding

**Files:**
- Create: `src/icp_chatbot/engine/profile_form.py`
- Test: `tests/test_profile_form.py`

**Interfaces:**
- Consumes: Tasks 1 to 4.
- Produces: `Card(id: str, title: str, slots: tuple[str, ...], input: Literal["single", "multi", "chips", "range", "geo"], facet: str | None, required: bool = True, show_if: tuple[tuple[str, str], ...] = (), any_allowed: bool = False, requires_catalog: tuple[str, ...] = ())`; `profile_cards(track: Track) -> list[Card]`; `visible_cards(track, answers: dict[str, list[str]]) -> list[Card]`; `validate_profile(track, answers) -> list[str]` returning missing or invalid slot keys; `apply_profile(state: SessionState, answers, turn_no: int = 0) -> list[str]` writing `SlotValue`s per the Global Constraints. Limits: at most 10 values per slot, each at most 80 characters, trimmed; `Any` must be the only value.
- Cards follow the spec tables exactly. Cards with `requires_catalog` (HR education: `("degree", "field_of_study")`) are not visible, and their slots not required, while `default_catalog().has(...)` is False.

- [ ] **Step 1: Write failing tests:** `test_sales_b2b_cards_in_order`; `test_b2c_shows_only_model_and_geography_and_hq_hidden`; `test_missing_required_card_is_reported`; `test_any_cannot_mix_with_other_values`; `test_any_rejected_on_business_model`; `test_hr_both_requires_education_level_and_experienced_set`; `test_hr_fresher_requires_graduation_year_when_catalog_present` (monkeypatch catalog `has` True); `test_education_card_hidden_without_catalog`; `test_values_longer_than_80_chars_or_more_than_10_are_rejected`; `test_apply_profile_writes_stated_0_9_with_refs`; `test_title_chips_optional_for_sales`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** `profile_form.py`; `years_experience` is stored as a single string, `"3-8"` (lower and upper bound) or `"3+"` (no upper bound, `"12+"` for the top of LinkedIn's range), with a helper `parse_years(value: str) -> tuple[int, int | None]`.
- [ ] **Step 4: Full suite green. Commit** `feat: step 2 form definition, validation and seeding`.

### Task 6: Three-step engine and API

**Files:**
- Modify: `engine/interview.py:257-312` (split `start`), `api/app.py:128-135`, `api/schemas.py`, `domain/signup.py`
- Test: `tests/test_three_step_api.py` (follow the `World` fixture in `tests/test_api.py`)

**Interfaces:**
- Consumes: Task 5.
- Produces: `StartRequest.track: Literal["sales", "hr"] = "sales"`; `InterviewEngine.create(tenant_id, signup) -> str` (session id; ensures tenant, creates the session with `step="profile"`, `track`, starts enrichment); `InterviewEngine.begin_chat(tenant_id, session_id, answers) -> TurnResult` (validates, `apply_profile`, generates the opener, sets `step="chat"`); `POST /sessions` → 201 `{session_id, step: "profile", track, cards}`; `GET /profile-form?track=` → cards; `GET /options/{facet}?q=&limit=` → `[{id,label,level,parent}]`; `POST /sessions/{id}/profile` → `TurnResponse`; `SessionView.step`, `SessionView.track`, and `SessionView.cards` + `SessionView.answers` while `step == "profile"`. 409 `{"detail": "profile_incomplete"}` from answers/detail/revise/finish/icp endpoints while `step == "profile"`. 422 `{"missing_required": [...]}` from `/profile`.

- [ ] **Step 1: Write failing tests:** `test_step1_returns_cards_for_track`; `test_missing_track_defaults_to_sales`; `test_profile_submit_seeds_slots_and_returns_first_question`; `test_chat_endpoint_409_before_profile`; `test_profile_missing_required_is_422`; `test_get_session_in_profile_step_returns_cards_and_saved_answers`; `test_options_endpoint_search_industry`; `test_options_unknown_facet_404`; `test_old_session_without_step_still_answers` (state saved without `step`).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** `begin_chat` reuses the opener generation from `start`; keep a thin `start` wrapper only if existing tests use it, otherwise update them to the new two-call flow. Save partial step 2 answers on `GET`-resume by storing `profile_answers: dict[str, list[str]]` on state when `/profile` fails validation.
- [ ] **Step 4: Update existing API and interview tests to the two-call flow; full suite green. Commit** `feat: three-step session flow and options endpoint`.

### Task 7: Per-track prompts and catalog-aware extraction

**Files:**
- Modify: `prompts.py`, `versions.py`, `engine/questions.py`, `engine/interview.py`, `engine/extraction.py`
- Test: `tests/test_hr_track.py` (prompts), `tests/test_extraction.py`

**Interfaces:**
- Consumes: Tasks 3, 4.
- Produces: `prompts_for(track: Track) -> dict[str, str]` (keys as in `DEFAULT_PROMPTS`); `render_slot_catalog(track)` lists the slots of that track and, for facet slots, the allowed option labels (industry list abbreviated to top-level groups plus a note to use exact LinkedIn labels); `generate_opener` takes the seeded step 2 summary and the track. HR prompts use "candidate", "role you are hiring for", "the profile"; none contain "ICP", "lead" or "customer". Free-text step 2 values are included in prompts inside a quoted data block with the existing injection guard wording.

- [ ] **Step 1: Write failing tests:** `test_hr_prompts_contain_no_sales_vocabulary` (regex over every HR prompt string for `\bICP\b|\blead|\bcustomer`); `test_slot_catalog_lists_only_track_slots`; `test_extraction_resolves_chat_industry_to_catalog_label`; `test_opener_mentions_known_step2_facts_and_asks_first_unknown`; `test_step2_chip_with_injection_text_is_quoted_as_data` (assert the rendered prompt wraps it in the data block).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** per-track prompt selection from `track` in state (config remains the DB `ConfigVersionData` version; the prompts overlay stays code-defined per the project memory note).
- [ ] **Step 4: Full suite green. Commit** `feat: per-track prompts and catalog-aware extraction`.

### Task 8: Documents: Ideal Candidate Profile and LinkedIn filters block

**Files:**
- Modify: `icp/document.py`, `icp/builder.py`, `icp/review.py`
- Test: `tests/test_document.py`, `tests/test_hr_track.py`

**Interfaces:**
- Consumes: Tasks 3, 4.
- Produces: `ICPDocument.track: Track = "sales"`; `ICPDocument.linkedin_filters: list[FilterEntry]` where `FilterEntry(facet: str, slot: str, ids: list[str], labels: list[str], unmapped: list[str], any: bool)`; `render_summary(entries, defs, track)` writes the HR summary as "Ideal candidate: ..." with the sections in `HR_SECTION_ORDER`; `build_linkedin_filters(state) -> list[FilterEntry]` skips `Any` slots (no filter) and lists unmapped values.

- [ ] **Step 1: Write failing tests:** `test_linkedin_filters_block_lists_ids_for_industries_and_size`; `test_any_adds_no_filter`; `test_unmapped_values_are_flagged`; `test_hr_document_sections_follow_hr_order`; `test_hr_summary_has_no_sales_words`; `test_review_edit_of_field_works_for_hr_slot`; `test_sales_document_unchanged_apart_from_filters_block`; `test_hr_document_carries_scoring_model_and_approve_validates_it` (approval calls `validate_model(document.scoring_model, document.track)`).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** The HR verdict text is a frontend label (Task 10); the verdict endpoint is unchanged.
- [ ] **Step 4: Full suite green. Commit** `feat: Ideal Candidate Profile document and LinkedIn filters block`.

### Task 9: Catalog sync script and education card gating

**Files:**
- Create: `scripts/sync_linkedin_catalogs.py`, `tests/test_sync_linkedin_catalogs.py`
- Modify: `src/icp_chatbot/linkedin/catalog.py` (load `degrees.json`, `fields_of_study.json` when present)

**Interfaces:**
- Produces: `fetch_all(path: str, token: str, client: httpx.Client) -> list[dict]` paging `GET https://api.linkedin.com/v2/degrees` and `/v2/fieldsOfStudy` with `start`/`count` until `paging` is exhausted, sending `Authorization: Bearer <token>`; `to_options(kind, rows) -> list[Option]` (degree: id, English name, `parent` = rollup id, aliases in `match`; field of study: id, name, `parent` = first rollup id); CLI `python scripts/sync_linkedin_catalogs.py --token-env LINKEDIN_TOKEN` writes the two JSON files into `src/icp_chatbot/linkedin/data/`. The token is read from an environment variable only, never an argument or file.

- [ ] **Step 1: Write failing tests** with `respx`: `test_fetch_all_follows_paging`; `test_degree_rows_become_options_with_rollup_parent_and_aliases`; `test_missing_token_exits_with_message`; `test_catalog_has_degree_after_files_present` (tmp data dir).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the script and catalog loading. Document the one-line run command in `README.md`.
- [ ] **Step 4: Full suite green. Commit** `feat: LinkedIn catalog sync script for degrees and fields of study`.

### Task 10: Frontend three-step flow

**Files:**
- Create: `frontend/src/components/StepOne.tsx`, `StepTwo.tsx`, `StepIndicator.tsx`
- Modify: `frontend/src/App.tsx` (Stage type line 18, `saved` restore line 98, `intro` state line 112), `api.ts`, `types.ts`, `components/IcpView.tsx`

**Interfaces:**
- Consumes: Task 6 endpoints. `types.ts` adds `Track`, `Card`, `ProfileOption`, `Step1Response`, `StartRequest.track`; `api.ts` adds `getOptions(facet, q)`, `submitProfile(id, answers)`, and `startSession` returns `Step1Response`.
- Stage becomes `"step1" | "step2" | "interview" | "fallback" | "building" | "done"`; unknown saved stages restore to `"step1"`; a restored session whose view says `step === "profile"` restores to `"step2"` with saved answers.
- `StepOne`: Role dropdown (Sales / BDE, HR) required, company name, website, industry dropdown from `/options/industry` top-level groups; submit disabled until all are set. `StepTwo`: renders any card by `input` type, with an **Any** chip that clears other picks, and a geography picker (country search + optional city chips). `StepIndicator`: three labelled steps above all stages. `IcpView` and button copy use "Ideal Candidate Profile", "candidate" for `track === "hr"`, and show the LinkedIn filters block with unmapped values marked.

- [ ] **Step 1: Implement the components and wiring** per the interfaces above.
- [ ] **Step 2: Verify the build:** `cd frontend && npm run build`. Expected: `tsc --noEmit` clean and a Vite build output.
- [ ] **Step 3: Verify in a browser** with the run skill and the Playwright MCP: Sales B2B path, Sales B2C path (cards skipped), HR Fresher, HR Experienced, refresh at step 2 resumes, Any clears other picks, submit disabled when a required card is empty. Record what was seen.
- [ ] **Step 4: Commit** `feat: three-step frontend with role selection`.

### Task 11: Evals, docs and gate

**Files:**
- Create: `eval_cases/hr_fresher.json`, `hr_experienced.json`, `hr_both.json`, `hr_vague.json`
- Modify: `eval_cases/*.json` (add step 2 answers where the case format needs them), `README.md`, `docs/DEPLOY.md` if the API contract is described there
- Test: `tests/test_eval.py`

- [ ] **Step 1: Write failing tests:** `test_hr_cases_load_and_use_hr_slots` and `test_required_tags_still_covered`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Add the HR cases** following the existing case schema (`eval/cases.py`); update the README flow section.
- [ ] **Step 4: Run the full suite, then the eval runner** the way `README.md` describes. Report the result as-is. Prompts must pass the existing gate before release; a failing gate blocks the release, not the merge of this branch.
- [ ] **Step 5: Commit** `test: HR eval cases and docs for the three-step flow`.

## Self-review notes

- Spec coverage: three steps (6, 10), roles (3, 3b, 5, 7, 8, 10), HR scoring (3b, 8), LinkedIn options (1, 4, 5), geography split (2, 5), Any (4, 5), legacy and in-flight sessions (2, 6), HR vocabulary (7, 8, 10), HR education card gating and sync (5, 9), catalog reconciliation test (1), eval gate (11).
- Not built until access exists: HR degree and field-of-study lists (Task 9 supplies the script; Task 5 hides the card until the snapshots exist).

### Task 12a: Sales step 2 reduced to industries, geography, buyer roles (added 2026-10-29)

**Files:**
- Modify: `src/icp_chatbot/engine/profile_form.py` (Sales cards), `tests/test_profile_form.py`, `tests/test_three_step_api.py` (the `World.start` helper's Sales profile), `eval_cases/*.json` whose `profile_answers` mention `business_model` or `company_size`, `README.md` flow text

**Decisions (user, 2026-10-29):** the Sales form has three cards only: `target_industries` (`industries`), `geography` (`lead_geographies`, `company_hq_geographies`), `buyer_roles` (`buyer_seniority`, `buyer_departments`, `buyer_roles` optional). The "who you sell to" and "target company size" cards are removed. HR order is unchanged (role you are hiring for first).

- [ ] **Step 1: Write failing tests:** `test_sales_cards_are_industries_geography_buyer_roles`; `test_business_model_is_seeded_b2b_default_not_asked` (after `apply_profile` for Sales: `business_model == ["B2B"]`, source DEFAULT, confidence 0.5, evidence "step 2 form default"); `test_stated_business_model_is_not_overwritten_by_the_default`; `test_company_size_is_not_required_by_the_sales_form`; `test_all_sales_fields_visible_without_a_business_model_answer` (the controlling-slot-answered rule must not hide industries or buyer roles for a Sales form: seed the default before computing visibility); `test_profile_form_endpoint_returns_three_sales_cards`; `test_b2b_default_session_still_reaches_the_floor_with_company_size_asked_in_chat`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Remove the two cards; in `apply_profile` and `validate_profile` treat an unanswered `business_model` as `B2B` for Sales (seed a DEFAULT value, confidence 0.5, evidence "step 2 form default", only when the slot has no value); keep `business_model` and `company_size` in the registry (tier A, asked in chat). The frontend needs no change (cards are backend-driven); confirm in the browser.
- [ ] **Step 4: Update dependants:** the `World.start` Sales profile in tests, eval cases (`b2c_retail_form` loses the form-level B2C answer: its B2C-ness must come from the chat, so reduce it to a chat-only case or drop it), README flow text; run the full suite.
- [ ] **Step 5: Commit** `feat: Sales step 2 asks only industries, geography and buyer roles`.

### Task 12b: LinkedIn filter and profile-path metadata on every slot, plus the new slots and option lists (added 2026-10-29)

**Decision (user):** every slot carries two attributes: `linkedin_filter` (the LinkedIn search filter name from the TL's lists, or None) and `profile_path` (where the value is read from the LinkedIn DB profile or company data, or None); a third flag `derived` marks values guessed from other data (seniority and function from the title). Source lists: Sales Navigator lead and account filters for Sales; Recruiter Lite filters for HR (ruling R19, provisional: no Lite filter for seniority and company HQ, so those two are scoring-only).

**Files:**
- Modify: `src/icp_chatbot/domain/slots.py` (`SlotDef` gains `linkedin_filter: str | None = None`, `profile_path: str | None = None`, `derived: bool = False`; fill them for EVERY slot in both registries; add the new slots below), `src/icp_chatbot/linkedin/catalog.py` (new facets), new data files under `src/icp_chatbot/linkedin/data/`, `tests/test_slots_and_state.py`, `tests/test_linkedin_catalog.py`
- Not touched here: `engine/profile_form.py`, `icp/scoring.py`, `icp/document.py`, eval cases, frontend (other tasks).

**Slot metadata (pin these):**

| Slot | linkedin_filter | profile_path |
|---|---|---|
| `industries` | Industry | `company.industry` |
| `company_size` | Company headcount | `company.employeeRange` |
| `company_type` (promote C to B, choices from the LinkedIn company types seen in the DB: Privately Held, Public Company, Partnership, Sole Proprietorship, Self-Employed, Educational Institution, Nonprofit; ruling R20 provisional) | Company type | `company.companyType` |
| `lead_geographies` | Geography | `profile.country`, `profile.city` |
| `company_hq_geographies` | Company headquarters location | `company.hqCountry`, `company.hqCity` |
| `buyer_seniority` | Seniority level | `profile.title` (derived=True) |
| `buyer_departments` | Function | `profile.title` (derived=True) |
| `buyer_roles` | Current job title | `profile.title`, `profile.currentPositions[].title` |
| `buying_triggers` (choices become Sales Navigator options: Changed jobs, Posted on LinkedIn, Mentioned in news, Job opportunities, Recent activities (funding, leadership changes), Buyer intent; open to free text) | Recent updates / Spotlight | None |
| `years_in_operation` | None | `company.foundedYear` |
| NEW `revenue_range` (tier B, Sales, filter-only, range-parsed) | Annual revenue | None |
| NEW `technologies_used` (tier B, Sales, chips) | Technologies used | None |
| `target_roles` (HR) | Job titles | `profile.title`, `profile.experience[].title` |
| `target_functions` | Job functions | `profile.title` (derived=True) |
| `years_experience` | Years of experience | `profile.experience[].period` |
| `education_level` | Degrees | `profile.education[].degree` |
| `field_of_study` | Fields of study | `profile.education[].field` |
| `graduation_year` | Year of graduation | `profile.education[].period` |
| NEW `target_schools` (HR tier B, chips) | Schools | `profile.education[].school` |
| `required_skills` | Skills | `profile.skills[]` |
| `nice_to_have_skills` | Keywords | `profile.skills[]` |
| `certifications` | Keywords | `profile.certifications[].name` |
| `languages` | Spoken languages | None (not in the DB) |
| `candidate_locations` | Locations | `profile.country`, `profile.city` |
| `current_industries` | Industries | `profile.currentPositions[].industry` |
| `current_company_size` | Company size | `profile.currentPositions[].employeeRange` |
| `current_company_hq` | None (no Lite filter, R19) | `company.hqCountry`, `company.hqCity` |
| `source_companies` | Current companies / Past companies | `profile.experience[].companyName` |
| `avoid_companies` | Current companies / Past companies (excluded) | `profile.experience[].companyName` |
| `target_seniority` | None (not in Lite, R19) | `profile.title` (derived=True) |
| `employment_type` (choices: Full-time, Part-time, Contract, Internship, Temporary, Volunteer, Other) | Employment type | None |
| `work_mode` (choices: On-site, Hybrid, Remote; rename "Onsite") | Workplace type | None |
| `salary_range`, `notice_period`, `relocation`, `urgency`, `headcount` | None | None (notes) |
| `disqualifiers`, `product`, `business_model`, `how_we_work`, `lead_sources`, `value_proposition`, `online_presence`, `customer_profile`, `customer_segments`, `experience_level` | None | None |

- [ ] **Step 1: Write failing tests:** `test_every_slot_declares_filter_and_profile_path_explicitly` (each slot in both registries has the metadata set deliberately: a module-level table `SLOT_LINKEDIN` keyed by slot key must cover every slot key of both registries, no missing and no extra); `test_filter_names_come_from_the_tl_lists` (every non-None `linkedin_filter` is in the allowed set for its track, listed in a constant); `test_derived_slots_are_marked` (buyer_seniority, buyer_departments, target_functions, target_seniority); `test_new_slots_exist_with_tiers` (revenue_range, technologies_used, target_schools tier B; company_type tier B); `test_work_mode_and_employment_type_choices_match_linkedin`; `test_buying_triggers_choices_are_sales_navigator_options`; `test_no_hr_slot_text_contains_sales_words`; `test_fallback_form_and_floor_unchanged`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Keep the metadata in one table `SLOT_LINKEDIN` in `domain/slots.py` and attach it when building the registries (single source of truth); update `SLOT_FACETS` only where a catalog exists; values LinkedIn does not publish (company type list beyond the DB values, revenue ranges) are marked provisional in a comment and left open to free text.
- [ ] **Step 4: Run the full suite.** Expected: pass. Update existing tests that pin slot counts or tier sets.
- [ ] **Step 5: Commit** `feat: LinkedIn filter and profile-path metadata on every slot`.

### Task 12c: profile mapper and scoring criteria for every slot with a profile path (added 2026-10-29; starts after 12b is committed)

**Files:** create `src/icp_chatbot/linkedin/profile_map.py`, modify `src/icp_chatbot/icp/scoring.py`, `src/icp_chatbot/icp/document.py` (filters block uses `SlotDef.linkedin_filter`), tests.

- [ ] **Step 1:** `slots_from_profile(profile: dict, company: dict | None, track) -> dict[str, list[str]]` reads the LinkedIn DB shapes (`ProfileDetails`: title, currentPositions[], experience[], education[], skills[], certifications[], city, country; `Company`: industry, employeeRange, companyType, hqCountry, hqCity, foundedYear, specialities) using each slot's `profile_path`; `years_experience` computed from `experience[].period`; derived slots tagged; tests with fixture profiles.
- [ ] **Step 2:** extend the scoring criteria per track (HR: add certifications_fit, school_fit, employer_fit with avoid companies as disqualifier, nice-to-have skills as the half-credit secondary list; Sales: company_type_fit, company_age_fit; derived criteria weight at most 5 each), keep weights summing to 100, renormalisation unchanged; tests including a fairness test (no protected attribute is read).
- [ ] **Step 3:** run the full suite and commit `feat: profile mapper and scoring criteria for LinkedIn profile fields`.
