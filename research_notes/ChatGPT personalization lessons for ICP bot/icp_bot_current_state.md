# ICP bot: current state (for the ChatGPT personalization comparison)

Snapshot: 2026-10-23 (updated after the model-led interviewer), branch `staging`, HEAD `27c6033`. Paths are relative to the repo root. `spec` = `../docs/superpowers/specs/2026-10-15-icp-chatbot-design.md` (outside the repo). "DB v2" = the live row in Postgres `config_versions`, read with a read-only query on 2026-10-23. Line numbers are re-checked for `interview.py`, `questions.py`, `planner.py`, `agent.py`, `repo.py` and `versions.py`; for `state.py`, `prompts.py` and `extraction.py` (edited since the first version of this note) references use names, and older line numbers for untouched files are unchanged.

## 0. What changed since the first version of this note

| Change | Where |
|---|---|
| **Prompts now come from `prompts.py`, not the DB.** Every config load overlays `DEFAULT_PROMPTS` on the stored row. The DB row still decides the version a session is pinned to, the models and the step settings. The DB prompt text (the long, hand-patched copy) is ignored. | `src/icp_chatbot/versions.py:29` (`with_code_prompts`), `src/icp_chatbot/store/repo.py:408, 415` |
| **A second, model-led engine exists.** `AgentEngine` makes one `interviewer` call per turn and lets the model decide what to ask, when to finish and whether there is enough to build an ICP. Code keeps validation, caps and a floor. Selected by `INTERVIEW_ENGINE`; code default is `classic`, `.env` and `.env.example` set `agent`. | `src/icp_chatbot/engine/agent.py`; `src/icp_chatbot/config.py:23-24`; `src/icp_chatbot/api/app.py:78` |
| **The edit form is gone.** The ICP card is read-only (summary, sections, "Still unclear") with "Looks good" and "Needs improvement". "Needs improvement" reopens the session through `POST /sessions/{id}/revise`. | `frontend/src/components/IcpView.tsx`; `interview.py:728` (`revise`); `app.py:147` |
| **Not-enough-information gate.** `POST /icp` returns 422 with the missing fields instead of building a thin ICP. | `app.py:221-229`; `planner.py:136-164` |
| New classic-engine behaviours: privacy follow-up, range ask, personalized re-ask, dynamic "you skipped this" note, stated corrections, model-written closing line, open-ended fields get no options. | sections 2 and 4 |

Checked claims from earlier notes:

| Claim | Status now |
|---|---|
| App reads prompts from DB v2 | **No longer true** (see above). Prompt text is `prompts.py`. Models and step settings are still the DB row's: `gemini-3.1-flash-lite` for extraction, seed, opener and question, and `gemini-2.5-flash` (thinking budget 2048) for final_icp. `.env` says 3.8-flash and 2.5-pro (`config.py:21-23`), which only apply when there is no DB row or in the eval. |
| Slim prompts are unevaluated | **Still true for the current text.** The earlier eval (2026-10-23 11:09-11:20, scratch script, DB v2 models) of the slimmed prompts against the old DB prompts gave mean F1 0.548 vs 0.551, cost/case $0.0168 vs $0.0113, verdict **FAIL** (hostile/injection F1 0.8 to 0.4, empty_website 0.5 to 0.0). `prompts.py` has changed since, and these slimmed prompts are now what runs, so the current text and the new `INTERVIEWER_PROMPT` have not been evaluated. |
| "bot not answerable" commit is about out-of-scope questions | **No**, 86f2651 is about service failures (plain "AI service is not responding" notices, retries, cut-off handling): `interview.py:72-79`, `src/icp_chatbot/llm/adapter.py:18-20, 41-44, 138-142`. |

## 1. Purpose, user, output

- Purpose: when a company signs up for LeadNest, a chatbot interviews it and produces that company's Ideal Customer Profile (spec:9; `README.md:3-4`).
- End user: a person at a company signing up to LeadNest (founder, sales and so on), talking about their own customers. Signup fields: company_name, website, email, role, industry, company_size, source (`src/icp_chatbot/domain/signup.py:15-22`). Tenant identity comes from an `X-Tenant-Id` header set by LeadNest's gateway; there is no auth in this service (`app.py:1-5`; `README.md:32`).
- Output: a versioned `ICPDocument` (`src/icp_chatbot/icp/document.py:36-58`): fields grouped by section, each with values, confidence, source (stated/confirmed/inferred), evidence, last_verified and required/preferred; a scoring model of 7 weighted criteria plus disqualifier rules (`src/icp_chatbot/icp/scoring.py:7-8, 22-30`); a generated summary; completeness; and open questions. The builder may reword values but cannot add fields (`src/icp_chatbot/icp/builder.py:3-5, 125-139`).
- **Review step now:** the user sees the summary and fields as plain text and answers "fine or needs improvement". "Looks good" calls approve plus `verdict(correct=true)`. "Needs improvement" asks "what should be changed or added?" and sends the reply to `revise`, which reopens the session, logs a `revision_requested` feedback event and treats the note as an answer; the ICP is rebuilt afterwards. `icp/review.py` and `PATCH /icp` still exist but nothing in the UI calls them.

## 2. Conversation flow

There are two engines behind the same API. `AgentEngine` subclasses `InterviewEngine`, so start (enrichment plus opener), finish, detail mode and revise are shared.

### 2a. Model-led engine (`INTERVIEW_ENGINE=agent`, current local setting)

`src/icp_chatbot/engine/agent.py`. **Type:** the model leads the conversation; code is a guardrail.

- **One call per answer** (`AgentEngine.answer`, `agent.py:153`). Step name `interviewer`. Input (`build_message`): mode and questions asked, industry context, recorded slots as JSON, core fields still unknown, the rolling summary, **the last 12 turns** (`HISTORY_LIMIT`, `agent.py:42`), the last question asked and the user's reply between `<<< >>>`.
- **Output** (`AgentTurn`): field updates, an optional domain update, a summary, `say`, `action` (`ask` or `finish`), `target_slot`, `options`, `enough`, `missing`, `user_wants_to_stop`, `off_topic`.
- **What the model decides:** what to ask and how to word it, how to react to refusals, "what do you mean?", corrections and dodging, whether the profile is ready (`enough`), and when to finish. Contradictions are settled in conversation: updates are applied with `corrects=True`, so the classic "which is right?" branch is bypassed.
- **What code still decides** (`_problem`, `agent.py:106`; `_turn`, `agent.py:131`):
  - `say` must be non-empty and at most 700 characters;
  - the model cannot finish unless `enough` is true or the user wants to stop;
  - `enough` is rejected unless the basics are known: product at confidence 0.5 or more plus at least 2 of the customer fields (`planner.py:148-157`), checked on a deep copy of the state with the updates applied;
  - a rejected reply goes back to the model **once** with the reason, then counts as a failed step (`MAX_ATTEMPTS = 2`, `agent.py:44`);
  - question cap: quick mode 20 (`config.py:30`), detail mode `min(total cap, started + 5)`; if the model still wants to ask, the interview ends with a model-written closing (`generate_closing`);
  - 2 consecutive model failures offer the fallback form; a second off-topic reply (counted over the whole session) does too;
  - options come from the slot where it has fixed choices, are dropped for open-ended slots, and are cleaned otherwise (`clean_options`).
- **Finish:** `enough` true gives the model's own closing line. If the user stops early without enough, `agent_enough=False` and `agent_missing` are stored and **no closing line is shown**, so the page can say what is missing.
- **Build gate:** `enough_for_icp` uses the interviewer's `agent_enough` (and the floor) when present, otherwise the old 30% rule (`planner.py:160-164`). The 422 detail lists `agent_missing` or the missing core slot labels (`app.py:221-229`).
- **Not used in this engine:** the planner, the thread/probe/follow-up writers, the decline, privacy, range, clarify and revision branches, the question styles and the per-session topic order. `generate_closing` is the only classic writer it calls.
- **Verified:** 9 unit tests (`tests/test_agent.py`), and a nine-reply scripted run against live Gemini on 2026-10-23: the schema was accepted, fields were recorded, refusals and a user question got natural replies, and the model finished with a written closing. During that run Gemini returned 429 errors and one 30 s timeout. The run also showed the model asking one unanswered question (pain points) three times; the scripted replies did not answer it, so this is not a clean signal either way.

### 2b. Classic engine (`INTERVIEW_ENGINE=classic`, code default, still used by the eval)

**Type:** a code-driven state machine with LLM wording. Code decides what to ask next and when to stop, with no model involved (`planner.py:1-3`). The LLM extracts slot values from each answer and writes each message.

- **Slots:** about 50 standard slots in tiers A (must ask), B (if relevant), C (inferred, never asked) and D (detail mode only) (`src/icp_chatbot/domain/slots.py:9-14, 111-536`). The extraction model also adds 4 to 7 industry-specific fields. Skip rules: B2C skips company slots and B2B skips customer_profile. A slot can be flagged `open_ended` (currently `how_we_work`): it never gets suggested options (`slots.py:59`).
- **Start** (`interview.py:254`): budget guard, new session pinned to the live config, a random `order_seed`, best-customer question at question 2 to 5. Enrichment reads the company website and fills a seed (inferred values capped at 0.5; `src/icp_chatbot/enrichment/service.py:77-94`). Then an LLM opener (`questions.py:457`). The opener prompt asks for a one-sentence description of the company from the website, then "is that right?".
- **Each answer** (`interview.py:317`): check input; one extraction call; merge the values; then the first branch that matches, in this order:
  1. Extraction failed: show the same question again; after 2 failures in a row, the fallback form.
  2. User asked something and dodged the same question twice: answer it and set the question aside (`376`), with a "you passed on that" note from the model; a first ask is answered and the waiting question repeated.
  3. Off-topic: fixed redirect text plus the same question; a 2nd off-topic reply in the session ends in the fallback form (`403`).
  4. `wants_to_finish`: complete (`415`).
  5. Vague "needs changes" note after the ICP (question kind `revision`): one fixed clarify question, `REVISION_CLARIFY`.
  6. Won't name a client (best-customer question): privacy follow-up asking for the kind of client, once (`430`).
  7. Refusal or "not sure" on company size, revenue, budget or deal size: ask for a rough range, once (`446`; `RANGE_SLOTS` `questions.py:586`).
  8. Other refusal or "not sure": set the slot aside, never ask again, with a model-written "you skipped this" note (`declined_note`, `questions.py:252`).
  9. Bare "no": one model-written re-ask (`generate_reask`, `questions.py:597`; `477`), with the fixed `DECLINE_PREFIX` text only as a failure fallback.
  10. Contradiction: ask which answer is right (`498`). Skipped when the user says it is a correction: the flag `corrects` or plain wording such as "sorry", "I meant", "my mistake", "typo" (`corrected`, `348`; `is_correction` in `extraction.py`). "Actually" and "instead" no longer count, because they can add a second value.
  11. Otherwise: choose the next question (`_next`, `573`).
- **Next question:** a follow-up on something the user raised (at most 3 in a row), then a bridge back; otherwise a follow-up to the opener or best-customer question, then the best-customer question when due, then a probe on a vague or partial slot (up to 6 per slot, stops after 2 with no gain), and finally a planner slot (`planner.py:86`).
- **Planner score** = tier weight x importance x (1 - confidence) x per-session jitter (0.6 to 1.4). Industry fields x1.3, a slot just asked x0.4, disqualifiers damped before question 5, slots wait until B2B/B2C is known.
- **Stop:** quick mode stops at 20 questions, or when tier A is at confidence 0.7 or more and at least 3 tier B topics were asked (`planner.py:12, 16`). The spec targeted 8 to 10 questions with a hard cap of 10 (spec:17). Detail mode adds 5 more, total cap 25 (`interview.py:698`); the API supports it but the UI has no button.
- **End:** an LLM-written closing line on every completion: a plain ending, a "go ahead with the current profile, you can change it later" version after a revision that added nothing, or an answer to a last user question (`interview.py:527`; `questions.py:760`). The UI then calls POST /icp (`builder.py:95-169`); refused if under 30% of core slots are known (`planner.py:136-145`).
- **Fallback form:** static form of the tier A slots plus 6 tier B slots (`src/icp_chatbot/engine/fallback.py:1-2, 24-34`).

## 3. LLM usage

- **Provider:** Google Gemini via `google-genai==2.31.0`. Two backends, Developer API key or Vertex; `.env` uses vertex with location=global (`src/icp_chatbot/llm/factory.py:13-51`). One adapter behind an `LLM` protocol (`src/icp_chatbot/llm/types.py:76-86`).
- **Steps** (`versions.py:13`): extraction, seed, opener, question, final_icp, plus **`interviewer`** for the model-led engine. `interviewer` has no row of its own in DB v2: it **borrows the `question` step's settings** and overrides temperature (0.6), max tokens (at least 3072) and, if set, the model from `MODEL_INTERVIEWER` (`agent.py:96-103`). **With `MODEL_INTERVIEWER` empty it runs on the DB question model, `gemini-3.1-flash-lite`, not on `.env` `MODEL_QUESTION`** (the comment in `config.py:23` says the opposite and is wrong for sessions that load a DB row). Its system prompt is `INTERVIEWER_PROMPT` plus the slot catalog (`versions.py:37`).

**Settings** (code defaults `versions.py`; DB v2 has the same values except the models):

| Step | Model (DB v2, live) | Temp | max_output_tokens | Thinking |
|---|---|---|---|---|
| extraction | 3.1-flash-lite | 0.0 | 2048 | minimal |
| seed | 3.1-flash-lite | 0.0 | 2048 | minimal |
| opener | 3.1-flash-lite | 0.7 | 2048 | low |
| question | 3.1-flash-lite | 0.5 | 2048 | low |
| interviewer (agent) | question model unless `MODEL_INTERVIEWER` | 0.6 | at least 3072 | low (from question) |
| final_icp | 2.5-flash | 0.1 | 8192 | budget 2048 |

- **Calls per answer:** agent engine: 1 (`interviewer`), 2 if its first reply is rejected, plus a closing call only when the question cap forces an end. Classic engine: usually 2 (extraction, then one writer), up to 3 with a closing line. Session start: 0 or 1 seed call plus the opener. ICP build: 1 call, cached until a new answer arrives (`app.py:232-238`). Calls run one after another.
- **Prompt assembly:** system instruction is the step's prompt (extraction and interviewer also get the slot catalog appended; `slots.py:550-563`). The request content is **one user string per call, not a list of chat messages** (`adapter.py:106-108`). The agent engine's string carries the transcript as `You:`/`User:` lines; the classic extraction call gets no turns at all, and classic writers get only the last 2.
- **Structured output:** JSON with a pydantic `response_schema` (`adapter.py:64-73`); invalid output is retried; a reply cut off at max tokens is retried with double the budget up to 8192 (`adapter.py:138-143`). Schemas: `ExtractionResult`, `QuestionOutput`, `AgentTurn`, `SeedProfile`, `ICPSynthesis`.
- **Rules the code enforces on output:** unknown slot keys are dropped; a weaker value never replaces a stronger one (so a correction must come in at 0.9); pain points and outcomes given off-target are capped at 0.6; question at most 400 chars (agent `say` at most 700), reply at most 300; options cleaned to 2 to 8 with no "Other"; fixed choices override model options; open-ended slots get none.

## 4. Tone and personality

- **Persona (live now, from `prompts.py`):** the classic writers use `QUESTION_PROMPT`: a warm, curious person, not a form; at most 25 words; a reflection of something specific they said, then one direct question; never tell them how long to answer; no praise, judging or idioms; "you"/"your company"; answer user questions in at most two sentences from general knowledge and say plainly when unsure. The agent engine uses `INTERVIEWER_PROMPT` (10 rules, in `prompts.py`): answer before asking, react to something specific then ask one question of at most 30 words, never repeat or re-ask what was clear, start with what they do and who they sell to, an example client once around question 2 to 4, respond to refusals in own words and ask once in an easier way (kind of client, rough range) then drop it with a light "you skipped this", options only for pick-lists, a closing written for them, and a rule for revisions.
- **No more "No problem." contradiction in prompt text:** the long DB prompts (which required "No problem.") are no longer used. Some code hints in `questions.py` still mention it as something to avoid. Fixed fallback strings remain (see below).
- **Opener:** greet as a guest; if the website was read, describe the company in one sentence from it and ask "is that right?"; otherwise open with one of 20 random angles (`questions.py:411-432, 457`). Enrichment timeout is 40 s (`ENRICHMENT_TIMEOUT_SECONDS`); before that it was 5 s, which made scraping of slow sites fail silently.
- **Variety:** the classic engine uses randomness (6 question styles, 20 opener angles, 4 best-customer variants, a per-session topic order); the agent engine relies on the model plus a "vary your wording" rule, and has no topic-order shuffle.
- **Adapting to the user:**
  - Language: reply in the user's language (both prompts). Fixed texts and UI strings are English only.
  - Industry: the extraction (or interviewer) model writes an industry brief, focus items and custom fields; questions use that industry's words.
  - Refusals and evasion: classic uses flags plus code hints (`DECLINED_NOTE`, `DECLINE_REASK_HINT`, `RANGE_HINT`, `PRIVACY_HINT`, `CLARIFY_HINT`); agent leaves it to prompt rule 5 and 6.
  - Questions to the bot: answered in both engines; the classic engine caps them at 6 per session.
  - Corrections: classic accepts them without asking (narrow wording list); agent decides itself.
  - Off-topic: classic uses one fixed redirect then the form; agent lets the model redirect, and a second off-topic flag still ends in the form.
- **Not adapted:** formality, verbosity, expertise level, mood or pace; there is no user-set tone or custom instructions.
- **Remaining fixed strings that break the tone goal:** `DECLINE_PREFIX` (now only a failure fallback), `REDIRECT_PREFIX`, `REVISION_CLARIFY`, `PRIVACY_FALLBACK`, `RANGE_FALLBACK`, the service-failure notices (`interview.py:72-79`, shown as red error bubbles). The frontend no longer shows a fixed "Thanks, that is everything I need." line; when a closing line is missing, only the "Building your ICP" note appears.

## 5. Memory and state

- **Per-session working memory** is a `SessionState` JSON in `sessions.state` (`src/icp_chatbot/domain/state.py`; `models.py:42-61`): slots with values, confidence, source, evidence; the website seed; industry context; a rolling summary of at most 600 chars rewritten each turn (by extraction in the classic engine, by the interviewer in the agent engine); `last_turns` (last 2); **`history`** (last 12 turns, written only by the agent engine); counters (asked, probes, stalls, skipped, declines, off-topic, user questions, thread); the pending question; **`agent_enough` and `agent_missing`** (the interviewer's last judgement). Saves use optimistic locking on row_version (`repo.py:154-195`).
- **Transcript:** every Q/A is stored append-only in `turns` with a state snapshot (`models.py:64-82`). It is still **never read back into a prompt**; `repo.list_turns` has no caller in `src/`. The agent engine gets its transcript from `state.history` instead.
- **Context window:** no token counting or trimming; inputs are bounded by design (12 turns in the agent engine, 2 in the classic writers, a 600-char summary, answers at most 2000 chars, website text at most 15,000 chars). The slot JSON grows with every filled slot.
- **Across sessions: nothing carries over.** Each start creates a blank `SessionState()`. `tenants.segment` and `sharing_consent` exist and are read nowhere. The spec's episodic and statistical memory tiers were never built.
- **Other stored data:** append-only `icp_versions` (bot vs company author), `feedback_events`, `enrichment_runs`, `config_versions`. Feedback events now include `approve`, `icp_verdict` (only `correct=true` is sent by the UI), `revision_requested` (the user's note) and `detail_mode_start`. The field-, weight- and requirement-edit events are no longer produced by the UI. `enrichment_runs` stores only `ok/partial/failed/blocked/skipped`, not the block reason.
- **Migrations:** a single Alembic revision `0001` that runs `metadata.create_all`. New `SessionState` fields are JSON, so no migration was needed.
- **Lifecycle:** sessions idle for 48h are marked abandoned by a CLI meant to run hourly (`src/icp_chatbot/maintenance.py:11-36`); the job is not scheduled locally.
- **Frontend memory:** messages live only in React state; a reload loses the chat display. `GET /sessions/{id}` exists but the UI never calls it.

## 6. Tokens and cost

- **Tracking:** usage from `usage_metadata` (input, output, thought, cached); a price table with a date-effective price for 3.8-flash (`src/icp_chatbot/llm/pricing.py:17-66`); summed per turn and per session and logged per call without text (`adapter.py:132-136`). The agent engine records its calls under the `interviewer` model key. A new model needs a price entry or the default is used.
- **Budget limits:** per-tenant daily caps of 200 sessions and 2M tokens, global caps of 2000 sessions and 20M tokens (`config.py:43-47`), **checked only when a session starts** (`src/icp_chatbot/limits.py:22-46`). Rate limit 60 requests/min per tenant, in process memory.
- **Caching:** relies on Gemini implicit prefix caching only (static system prompt first). `INTERVIEWER_PROMPT` plus the catalog is a long static prefix, which helps.
- **Retries:** up to 4 attempts on 429/5xx (backoff 1, 2, 4 s) and invalid output; a timeout is retried once (`adapter.py:18-20, 53, 100-143`). The SDK's own retry sits under it, so retries are nested. Timeout is 20 s by default, 30 s in `.env`. **In the 2026-10-23 live agent run Gemini returned 429 errors and one 30 s timeout on the interviewer step.** The agent step is a larger call than the classic writers, so it is more exposed.
- **Observed cost:** $0.011 to $0.017 per eval case for the classic engine on short cases (about 5 questions). The agent engine's cost has not been measured; it sends a growing transcript each turn. The spec budget is about $0.14 per interview.

## 7. Privacy and safety

- **Auth:** none; trusts `X-Tenant-Id` and must not be internet-facing. The frontend hardcodes tenant "demo". Every repo call checks tenant ownership.
- **PII:** the extraction model sets `flags.contains_pii` but **no code reads it**; the `AgentTurn` schema has no PII flag at all. Answers are stored raw in `turns.answer_text` and in every `state_after` snapshot; the agent's `history` duplicates recent answers in `sessions.state`. The signup email is used only to find a website. Prompts tell the bot never to ask for personal data. The spec's anonymization step is not built.
- **Logging:** app logs carry no user text. Vertex calls carry labels with session_id, turn and config_version.
- **Retention:** no TTL and no purge job; `Repo.delete_tenant` is not exposed by any route or CLI.
- **Prompt injection:** every prompt says its input is data; answers and website text are wrapped in `<<< >>>`; output is schema-validated and slot keys enforced in code; seed values capped at 0.5; the model has no tools. **New exposure in the agent engine:** the model now writes the user-visible message directly and also decides when to finish, with only length, the "enough" rule and the floor as checks. The two injection eval cases exist, but the slimmed classic prompts already scored lower on them (F1 0.8 to 0.4) and the agent engine has not been run against them.
- **Input validation:** answers non-empty and at most 2000 chars; signup fields have max lengths. No moderation or abuse classifier.
- **Website fetcher:** public IPs only, redirects re-checked, robots.txt respected, size and type limits; DNS rebinding remains an open risk. It identifies itself as `LeadNestICPBot/1.0`. On 2026-10-23 three sites rejected that identity: cricbuzz.com (its `robots.txt` disallows all bots except Googlebot), swiggy.com (403 from anti-bot protection) and zomato.com (connection reset, which shows up as a read timeout). Getting past that would need a browser user agent, which was not done.

## 8. Evaluation

- **Eval set:** 14 handwritten JSON cases in `eval_cases/`, each with a signup, optional website text, **fixed scripted answers** (2 to 6) and expected/forbidden slot values. Launch needs at least 30 cases; `launch_check` prints "NOT READY: only 14 cases".
- **Replay** (`src/icp_chatbot/eval/replay.py:76-127`) runs the **classic `InterviewEngine`** (`replay.py:87`) on in-memory SQLite. It never uses `AgentEngine`, so **the engine that is now configured locally is not covered by the eval harness.** Scoring is precision/recall/F1 on expected values plus forbidden-value violations; the summary is compared with a baseline at a 2% margin.
- **Eval/prod mismatch:** `cli.replay` passes no config, so the in-memory DB builds `default_config` (prompts.py plus the `.env` models 3.8-flash and 2.5-pro). Prompt text now matches production (both are `prompts.py`), but **the models still differ** from the DB row's 3.1-flash-lite and 2.5-flash that users get.
- **What the eval measures:** field accuracy, completion, number of questions, cost, scoring validity. **Nothing about tone, friendliness, question quality, repetition or user experience**, and the scripted answers don't react to what the bot asks.
- **Unit tests:** 28 test files, **441 passed** on 2026-10-23, including `tests/test_agent.py` (9 tests: model-written question and options, open-ended slots, rejected early finish retried, enough-without-basics rejected, finish when basics known, stop-early marked not enough, cap, two failures to fallback, transcript shown to the model). They use a scripted `FakeLLM`, so they check control flow and the hints sent, not wording quality.
- **Production metrics:** completion, mean questions, cost, p50/p95 latency, edit rate on approved ICPs, drop-off by slot, field/weight/requirement edits (`src/icp_chatbot/metrics/queries.py:24-112`). With the edit form gone, the edit-rate metrics will stop moving; `revision_requested` and `icp_verdict` are the new signals, and nothing aggregates them yet. The spec's prompt-optimizer loop is not built.

## 9. Frontend

- **Stack and flow:** React 18 + Vite + TypeScript, one chat page with stages home, signup, interview, fallback, building and done (`frontend/src/App.tsx`). Plain JSON fetch through the Vite dev proxy `/api`.
- **No streaming:** the user sees "Thinking..." until the whole turn finishes. The spec wanted median time to first streamed token under 4 s.
- **Suggested answers:** chips, multi-select with Confirm or single-pick that sends at once; picks and typed text are joined with ", " into one answer.
- **Signup card:** asks company name, website, industry and role only (`SignupCard.tsx:4-9`); it never asks for email, size or source.
- **ICP view** (`IcpView.tsx`): read-only summary, sections and "Still unclear", then "Looks good" and "Needs improvement" (no edit controls, weights, completeness bars or exclusion list). "Needs improvement" asks "what should be changed or added?" and sends the next message to `/revise` (`App.tsx`: `improve`, `revising`). If the build is refused, the chat says "You have not provided enough information to build an ICP yet. Still missing: ..." and reopens the composer on the revise path. `verdict(false)` is never sent, so a "needs improvement" click is logged only as `revision_requested`. There is no detail-mode button.

## 10. Gaps vs a ChatGPT-style adaptive assistant (observations only)

1. **No cross-session or user memory.** Every interview starts blank. No tenant profile, no reuse of earlier answers or ICPs, no memory the user can view or edit.
2. **No tone adaptation to the user.** Style variety is random (classic) or left to the model (agent). Formality, verbosity, expertise and mood are not detected and there are no user-set preferences.
3. **Context.** The agent engine now sees 12 turns plus the summary, which closes most of the old "last 2 turns" gap for that engine. Classic writers still see 2, and extraction sees no turns. Each call is still one assembled string, not a chat history.
4. **Who drives.** Resolved for the agent engine: the model decides what to ask and when to stop, with code vetoes. The classic engine is still planner-driven. The agent engine's quality is untested at scale: it has had one scripted live run and no eval.
5. **No streaming, and slow or rate-limited turns.** One interviewer call per turn is faster than extraction plus writer, but the call is larger. With nested retries a turn can still stall. The live run hit 429s.
6. **Model strength.** The agent engine, as configured through DB v2, runs on a small flash-lite model unless `MODEL_INTERVIEWER` is set; a stronger model was part of the intent and is not yet in place.
7. **Thin feedback capture and no learning.** There is no per-message rating, no `verdict(false)`, and nothing learns from revision notes. A report or prompt-suggestion step over `revision_requested`, `icp_verdict` and refusals was proposed and not built.
8. **Config drift is smaller:** prompts have one source (`prompts.py`). Models and step settings still live in DB v2 and differ from `.env`; the eval ignores the DB row; `interviewer` borrows `question` settings.
9. **The eval ignores conversation quality and the agent engine.** No tone rubric, no LLM judge, no simulated reactive user, 14 static cases where 30 are required, and no harness run for `AgentEngine`.
10. **Privacy controls are partial.** `contains_pii` is unused (and absent from the agent schema), no redaction, retention policy or deletion endpoint, and raw answers are duplicated across turn snapshots and `history`.
11. **No abuse or moderation layer.** "Hostile" is tested only as prompt injection, and not against the agent engine.
12. **Limits are basic.** Budget caps apply only at session start; the rate limiter lives in process memory; there is no per-session cost cap for the agent engine beyond the question cap.
13. **Little transparency during the chat.** The user doesn't see what has been recorded until the final ICP.
14. **Website scraping is fragile.** Sites with anti-bot protection or restrictive `robots.txt` (the three tested) cannot be read; the failure reason is not stored, and the chat does not tell the user the site could not be read.
