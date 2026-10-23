# ICP bot: current state (for the ChatGPT personalization comparison)

Snapshot: 2026-10-23, branch `staging`, working tree including the uncommitted edits (slots.py, extraction.py, interview.py, questions.py, prompts.py, two test files). Paths are relative to the repo root. `spec` = `../docs/superpowers/specs/2026-10-15-icp-chatbot-design.md` (outside the repo). "DB v2" = the live row in Postgres `config_versions`, read with a read-only query on 2026-10-23; it has no file:line.

## 0. Known context, checked

| Claim from memory | What the code and DB show |
|---|---|
| App reads prompts from DB v2, not prompts.py | **True.** Sessions load the live config row (`src/icp_chatbot/store/repo.py:379-394`, `src/icp_chatbot/engine/interview.py:218-226`) and stay on that version (`src/icp_chatbot/store/models.py:47`, `interview.py:301`). prompts.py is only used when no live row exists (`src/icp_chatbot/versions.py:60-66`). DB: v1 retired, v2 live (author "dev", created 2026-10-15). |
| DB v2 also fixes the models | **True, and they differ from .env.** DB v2 uses `gemini-3.1-flash-lite` for extraction, seed, opener **and question**, and `gemini-2.5-flash` (thinking_budget 2048) for final_icp. `.env`/settings say question/opener = `gemini-3.8-flash`, final = `gemini-2.5-pro` (`src/icp_chatbot/config.py:21-23`). Users get the DB models. |
| Slim prompts.py is not synced to the DB | **True.** None of the 5 DB prompts match prompts.py. Word counts DB vs file: extraction 1209 vs 603, question 842 vs 322, opener 242 vs 144, final 217 vs 153, seed 78 vs 63. The closest git version of the DB text is commit 7bdbb0c (2026-10-22, before slimming): question 0.98 similar, final_icp identical. Recent rules were also patched into the DB by hand (for example DB extraction already has `flags.corrects`), so the DB is a separate fork, not an old copy. |
| Slim prompts are "unevaluated" | **Out of date.** The slimming was committed in 2a2f688 (2026-10-22 20:43, 2460 to about 1465 words). On 2026-10-23 at 11:09-11:20, a scratch script in an earlier session (`/tmp/claude-1000/.../2c81d705-.../scratchpad/eval_prompts.py`) compared DB v2 prompts with prompts.py, both on DB v2 models. Full 14 cases: mean F1 0.548 vs 0.551, cost/case $0.0168 vs $0.0113, verdict **FAIL** (mean questions 4.79 to 5.0; hostile/injection F1 0.8 to 0.4; empty_website 0.5 to 0.0; one forbidden value in both runs). A 4-case subset went 0.547 to 0.789. prompts.py has changed again since (63ab6e7, 0ea4b6a, uncommitted edits), so the current file has not been evaluated. |
| Recent commits are about tone | **Mostly.** b7cbbdb adds the "ask for a rough range" path (`src/icp_chatbot/engine/questions.py:586-594`, `interview.py:421-448`). 0ea4b6a adds the declined note, the personalized re-ask, the vague-revision clarify and the "enough for an ICP" check (`questions.py:243-256, 576-583`; `interview.py:66-70, 395-404`; `src/icp_chatbot/engine/planner.py:136-152`). **86f2651 "bot not answerable" is about service failures, not out-of-scope questions:** it adds the plain "AI service is not responding" notices, retries and cut-off handling (`interview.py:72-79`, `src/icp_chatbot/llm/adapter.py:18-20, 41-44, 138-142`). |

## 1. Purpose, user, output

- Purpose: when a company signs up for LeadNest, a chatbot interviews it and produces that company's Ideal Customer Profile (spec:9; `README.md:3-4`). ICP = ideal customer profile.
- End user: a person at a company signing up to LeadNest (founder, sales and so on), talking about their own customers. Signup fields: company_name, website, email, role, industry, company_size, source (`src/icp_chatbot/domain/signup.py:15-22`). Tenant identity comes from an `X-Tenant-Id` header set by LeadNest's gateway; there is no auth in this service (`src/icp_chatbot/api/app.py:1-5, 109-114`; `README.md:32`).
- Output: a versioned `ICPDocument` (`src/icp_chatbot/icp/document.py:36-58`). It has fields grouped by section, each with values, confidence, source (stated/confirmed/inferred), evidence, last_verified and required/preferred; a scoring model of 7 weighted criteria plus disqualifier rules (`src/icp_chatbot/icp/scoring.py:7-8, 22-30`) that LeadNest's matcher will use; a generated summary (`document.py:123-172`); completeness; and open questions. The company reviews, edits and approves it (`src/icp_chatbot/icp/review.py:1-3`; `app.py:285-313`). The builder may reword values but cannot add fields (`src/icp_chatbot/icp/builder.py:3-5, 125-139`).

## 2. Conversation flow

**Type:** a code-driven state machine with LLM wording. Code decides what to ask next and when to stop, with no model involved (`planner.py:1-3`). The LLM extracts slot values from each answer and writes each message.

- **Slots:** about 50 standard slots in tiers A (must ask), B (if relevant), C (inferred, never asked) and D (detail mode only) (`src/icp_chatbot/domain/slots.py:9-14, 111-536`). The extraction model also adds 4 to 7 industry-specific fields; the 3 most important must be filled before the interview can stop (`slots.py:72-101`; `prompts.py:39-47`). Skip rules: B2C skips company slots and B2B skips customer_profile (`slots.py:108-109`; `src/icp_chatbot/domain/state.py:176-184`).
- **Start** (`interview.py:234-281`): budget guard, new session pinned to the live config, a random `order_seed`, and the best-customer question placed at question 2 to 5 (`interview.py:240-242`). Enrichment reads the company website and fills a seed; inferred values are capped at 0.5 confidence (`src/icp_chatbot/enrichment/service.py:77-94`). Then an LLM opener (`questions.py:457-501`).
- **Each answer** (`interview.py:295-549`): check input; one extraction call (`313`); merge the values (`src/icp_chatbot/engine/extraction.py:160-228`); then the first branch that matches, in this order:
  1. Extraction failed: show the same question again. After 2 failures in a row, offer the fallback form (`342-353`).
  2. User asked something: answer it and ask the waiting question again (`361-380`). If they dodge the same question twice, it is set aside (`336-360`).
  3. Off-topic: fixed redirect text plus the same question. A 2nd off-topic reply, counted across the whole session and never reset, ends the chat in the fallback form (`59, 71, 381-392`).
  4. `wants_to_finish`: complete (`393-394`).
  5. A vague "needs changes" note on the ICP: one fixed clarify question (`395-404`).
  6. User won't name a client: privacy follow-up, once (`405-420`).
  7. Refusal or "not sure" on size, revenue, budget or deal size: ask for a rough range, once (`421-448`).
  8. Other refusal or "not sure": set the slot aside and never ask it again (`449-454`, `141-147`).
  9. Bare "no": one personalized re-ask (`455-475`).
  10. Contradiction: ask which answer is right. Skipped when the user says it is a correction (uncommitted) (`326-331, 476-489`).
  11. Otherwise: choose the next question (`490-501`).
- **Next question** (`interview.py:551-625`): a follow-up on something the user raised, at most 3 in a row (`MAX_THREAD_QUESTIONS` `65`), then a bridge back. Otherwise one follow-up to the opener or best-customer question, then the best-customer question when due, then a probe on a vague or partial slot (up to 6 per slot, stops after 2 probes with no gain; `60-63, 627-648`), and finally a planner slot.
- **Planner score** = tier weight x importance x (1 - confidence) x per-session jitter (0.6 to 1.4). Industry fields get x1.3, a slot just asked gets x0.4, disqualifiers are damped before question 5, and slots wait until B2B/B2C is known (`planner.py:18-35, 77-83, 86-133`).
- **Stop:** quick mode stops at 20 questions (`config.py:30-32`), or when tier A is at confidence 0.7 or higher and at least 3 tier B topics have been asked (`planner.py:12, 16, 87-91`). The spec targeted 8 to 10 questions with a hard cap of 10 (spec:17). Detail mode adds 5 more, total cap 25; the API supports it but the UI has no button (`interview.py:676-704`; `planner.py:93-99`).
- **End:** an LLM closing line written for the user (uncommitted: now on every completion) (`interview.py:503-516`; `questions.py:760-800`). The UI then calls POST /icp: one final_icp call, falling back to values as recorded if it fails (`builder.py:95-169`). The ICP is refused if under 30% of core slots are known (`app.py:221-229`; `planner.py:136-152`). "Needs improvement" reopens the session and treats the note as an answer (`interview.py:706-721`).
- **Fallback form:** static form of the tier A slots plus 6 tier B slots (`src/icp_chatbot/engine/fallback.py:1-2, 24-34`).

## 3. LLM usage

- **Provider:** Google Gemini via `google-genai==2.31.0` (`requirements.txt`). Two backends, Developer API key or Vertex; `.env` uses vertex with location=global (`src/icp_chatbot/llm/factory.py:13-51`; `.env.example:9`). There is one adapter, behind an `LLM` protocol (`src/icp_chatbot/llm/types.py:76-86`).
- **Steps** (`versions.py:13`): extraction, seed, opener, question, final_icp. All per-turn writer variants (question, probe, follow-up, re-ask, reply, clarify, thread, best-customer, closing) share the `question` step config and system prompt (for example `questions.py:330-339`).

**Settings** (code defaults `versions.py:60-99`; DB v2 has the same values except the models):

| Step | Model (DB v2, live) | Temp | max_output_tokens | Thinking |
|---|---|---|---|---|
| extraction | 3.1-flash-lite | 0.0 | 2048 | minimal |
| seed | 3.1-flash-lite | 0.0 | 2048 | minimal |
| opener | 3.1-flash-lite | 0.7 | 2048 | low |
| question | 3.1-flash-lite | 0.5 | 2048 | low |
| final_icp | 2.5-flash | 0.1 | 8192 | budget 2048 |

- **Calls per answer:** usually 2 (extraction, then one writer). Up to 3 when the thread writer fails and another writer runs (`questions.py:401-406` then `interview.py:556-567`), or when a closing line is added (`interview.py:503-516`). Session start: 0 or 1 seed call (only with a website or work-email domain; `service.py:107-109, 154-162`) plus the opener. ICP build: 1 call, cached until a new answer arrives (`app.py:232-238`). The calls run one after another.
- **Prompt assembly:** the system instruction is the step's prompt; extraction also gets the slot catalog appended (`versions.py:32-37`; `slots.py:550-563`). The request content is **one user string per call, not a list of chat messages** (`adapter.py:106-108`).
  - Extraction input: industry context, all recorded slots as JSON, the rolling summary, the last question, the target slot, and the answer between `<<< >>>` (`extraction.py:111-123`). It does **not** get the last turns.
  - Writer input: industry context, target slot, rolling summary, the last 2 question/answer pairs, then situation hints and a style line (`questions.py:308-329`).
- **Structured output:** JSON with a pydantic `response_schema` (`adapter.py:64-73`), parsed from `response.parsed` or the raw text (`adapter.py:75-86`). Invalid output is retried; a reply cut off at max tokens is retried with double the budget, up to 8192 (`adapter.py:138-143`). Schemas: ExtractionResult with updates, domain, flags, thread and summary (`extraction.py:59-99`); QuestionOutput with question, reply and options (`questions.py:50-53`); SeedProfile (`service.py:41-48`); ICPSynthesis (`builder.py:37-52`).
- **Rules the code enforces on output:** unknown slot keys are dropped (`extraction.py:170-173`); a weaker value never replaces a stronger one (`199-200`); pain points and outcomes given off-target are capped at 0.6 (`24-27, 181-184`); question at most 400 chars, reply at most 300 (`questions.py:39, 47, 206-214`); options are cleaned, 2 to 8, no "Other" (`65-75`); fixed choices override model options; open-ended slots get no options (uncommitted; `questions.py:352-357, 903`; `slots.py:59, 138`).

## 4. Tone and personality

- **Persona as live (DB v2 question prompt):** "a warm person ... not a form or a survey". Rules: at most 25 words, plain words, no idioms; an acknowledgement of at most 8 words in the user's own words, never a bare "Got it"; no praise, judging or guessing; exactly one question with no "A or B"; vary phrasing; and **rule 6: if the user did not answer, say "No problem."**. Rule 11: answer a user's question in at most 2 sentences; say "I don't have that information" about pricing, features or data handling; never name the model; politely decline anything unrelated.
- **prompts.py version (not live):** "warm, curious person" (`prompts.py:57`). Shape: reflect their words, then one question, at most 25 words, and never tell them how long to answer (`prompts.py:61-63`). No praise, idioms or invented detail (`64-65`). Answer user questions "from general knowledge", say when unsure, no figures or promises, never name the model (`72-75`). Vary wording and follow the style line (`79`).
- **Opener:** greet them as a guest; if the website was read, confirm what it says ("From your website ... Is that right, and what would you add?"); otherwise open with a random angle (`prompts.py:82-95`; `questions.py:411-432, 484`). The DB v2 opener prompt contains a broken fragment line ("the angle, so no two openers sound alike."), left over from an edit made directly in the DB.
- **Variety comes from randomness, not from the user:**
  - 6 question styles chosen by a seeded random pick per session and question, for example "casually, as a colleague would over coffee" or "very short, let the options carry the detail" (`questions.py:434-454`);
  - 20 opener angles (`411-432`); 4 best-customer variants (`87-92, 443-448`);
  - per-session topic order (`planner.py:29-35`);
  - role-based fixed opener as fallback: founder vs sales (`questions.py:105-113`).
- **Adapting to the user (all rule-based, keyed on extraction flags and regexes):**
  - Language: reply in the user's language (`prompts.py:58`). Fixed texts and UI strings are English only.
  - Industry: the extraction model writes an industry brief, focus items and custom fields, and questions use that industry's words (`state.py:95-118`; `prompts.py:39-47, 69-70`).
  - Negative or evasive users: flags `refuses` and `unsure` plus regexes (`state.py:32-54`; `prompts.py:32-37`). They are never pushed more than once (`state.declines`, `state.py:154-156`); a declined slot goes into `skipped` and is never asked again (`planner.py:72, 105`). Code hints shape the message:
    - DECLINED_NOTE: a friendly line that they passed, varied, no stock phrase (`questions.py:243-256`);
    - DECLINE_REASK_HINT: one re-ask that mentions their details and why it helps, at most 45 words (`576-583`);
    - RANGE_HINT: offer a rough range (`586-594`);
    - PRIVACY_HINT: they don't want to name clients (`504-512`);
    - CLARIFY_HINT: point out a contradiction "kindly and without blame" (`704-708`).
  - Questions to the bot: answered, including questions the model missed, caught by a regex (`extraction.py:39-46, 126-138`; `interview.py:333-335`). Capped at 6 per session; after that they are handled like any other reply (`interview.py:64, 173-175`).
  - Topics the user raises: up to 3 curious follow-ups, then a bridge back (`questions.py:224-241`; `interview.py:150-163`).
  - Corrections ("sorry, I meant B2B") are accepted without asking which is right (uncommitted; `extraction.py:49-56`; `prompts.py:29-31`).
  - Off-topic: one fixed redirect, "Let's get back to describing your ideal customer.", then the form (`interview.py:71, 381-392`).
  - Closing line written for the user, banning the stock "Thanks, that is everything I need." (uncommitted; `questions.py:271-282`).
- **Not adapted:** formality, verbosity, expertise level, mood or pace. Length is fixed at 25 words or less for everyone. There is no user-set tone or custom instructions.
- **Contradictions inside the tone design:**
  - DB rule 6 and FOLLOW_UP_HINT (`questions.py:177-183`) require "No problem.", while other hints forbid it (`questions.py:195, 202, 230, 291, 707, 807`).
  - Fallback texts are the stock phrases the LLM hints ban: DECLINE_PREFIX "No worries, and thanks for being open with me..." (`interview.py:80-84`) vs DECLINE_REASK_HINT (`questions.py:581-582`). The frontend falls back to "Thanks, that is everything I need." (`frontend/src/App.tsx:118`).
- **Service-failure notices** use plain words ("not about what you wrote") (`interview.py:72-79`) and show as red error bubbles (`App.tsx:110`).

## 5. Memory and state

- **Per-session working memory** is a `SessionState` JSON in `sessions.state` (`state.py:121-158`; `models.py:42-61`). It holds:
  - slots, each with values, confidence, source, source turn and evidence;
  - the website seed, kept separately;
  - industry context;
  - a **rolling summary of at most 600 chars, rewritten by the extraction model on every turn** (`extraction.py:30, 226-227`; `prompts.py:38`);
  - **the last 2 turns only** (`state.py:127, 191-192`);
  - counters: asked, probes, stalls, skipped, declines, off-topic, user questions, thread;
  - the pending question.
  Saves use optimistic locking on row_version (`repo.py:154-195`).
- **Transcript:** every Q/A is stored append-only in `turns`, with a full state snapshot per turn (`models.py:64-82, 136-141`). It is **never read back into a prompt**: `repo.list_turns` (`repo.py:238`) has no caller in `src/`.
- **Context window:** no token counting or trimming logic. Inputs are bounded by design: last 2 turns, a 600-char summary, answers at most 2000 chars (`config.py:35`), website text at most 15,000 chars (`service.py:33-35`). The slot JSON grows with every filled slot and is never trimmed.
- **Across sessions: nothing carries over.** Each start creates a blank `SessionState()` (`interview.py:239`). `tenants` holds only id, name, `segment` and `sharing_consent`, and neither of the last two is read anywhere (`models.py:32-39`). Earlier ICPs or answers from the same tenant are never loaded. The spec's other memory tiers were never built: episodic memory (`knowledge_items` + pgvector "exemplar cards" from past approved sessions) and statistical memory (per-segment hints) (spec:265-285; no match in `src/`).
- **Other stored data:** append-only `icp_versions` (bot vs company author), `feedback_events`, `enrichment_runs`, and `config_versions` with draft/approved/live/retired status (`models.py:85-133`).
- **Migrations:** a single Alembic revision, `0001`, which just runs `metadata.create_all` (`alembic/versions/0001_initial_schema.py:1-25`). The DB is at 0001.
- **Lifecycle:** sessions idle for 48h are marked abandoned by a CLI meant to run hourly (`src/icp_chatbot/maintenance.py:11-36`; `src/icp_chatbot/cli/abandon.py`). The local DB shows 73 active, 15 completed, 1 fallback and 0 abandoned, so the job is not scheduled locally.
- **Frontend memory:** messages live only in React state (`App.tsx:32`). A reload loses the chat display. `GET /sessions/{id}` exists (`app.py:158-181`) but the UI never calls it (`frontend/src/api.ts:33-46`).

## 6. Tokens and cost

- **Tracking:** usage comes from `usage_metadata`: input, output, thought and cached tokens (`types.py:20-53`). Cost uses a price table with a date-effective price for 3.8-flash (`src/icp_chatbot/llm/pricing.py:17-66`). It is summed per turn and per session (`repo.py:174-177`; `models.py:53-56, 78-81`), and logged per call with step, model, token counts, cost and latency, never text (`adapter.py:132-136`).
- **Budget limits:** per-tenant daily caps of 200 sessions and 2M tokens, global caps of 2000 sessions and 20M tokens (`config.py:43-47`). They are **checked only when a session starts** (`src/icp_chatbot/limits.py:22-46`). A rate limit of 60 requests/min per tenant is kept in process memory (`limits.py:49-65`; `app.py:112-113`).
- **Caching:** relies on Gemini's implicit prefix caching only: static system prompt first, dynamic content in the user message (`prompts.py:1-7`). Cached tokens are priced (`pricing.py:57-66`). There are no explicit cache objects.
- **max_tokens** per step (table in section 3), auto-raised when a reply is cut off (`adapter.py:138-142`).
- **Retries:** the adapter makes up to 4 attempts on 429/5xx (backoff 1, 2, 4 s) and on invalid output; a timeout is retried once (`adapter.py:18-20, 53, 100-143`). The SDK's own retry (`attempts=3`, `factory.py:15-23`) sits under it, so retries are nested. Timeout is 20 s by default, 30 s in `.env`. Worst case for one step is about 60 s.
- **Observed cost:** $0.011 to $0.017 per eval case in the 2026-10-23 runs, but those cases are short (about 5 questions). The spec budget is about $0.14 per interview (spec:19).

## 7. Privacy and safety

- **Auth:** none in this service; it trusts `X-Tenant-Id` and must not be internet-facing (`README.md:32`; `app.py:1-5`). The tenant id is regex-checked (`app.py:50`). The frontend hardcodes tenant "demo" (`api.ts:3`). Every repo call checks tenant ownership (`repo.py:1-2, 126-131`).
- **PII:**
  - The extraction model sets `flags.contains_pii` (`prompts.py:33`; `extraction.py:70`), but **no code reads it**.
  - Answers are stored raw in `turns.answer_text`, and again in each `state_after` snapshot (`models.py:73-75`).
  - The signup email is used only to find a website and is not stored (`service.py:67-74`).
  - Prompts tell the bot never to ask for personal data (`prompts.py:79`).
  - The spec's anonymization step (spec 11.2) is not built.
- **Logging:** app logs carry no user text (`adapter.py:132-136`; `interview.py:320`). Vertex calls carry labels with session_id, turn and config_version (`interview.py:228-230`; `factory.py:58`; `adapter.py:72`).
- **Retention:** no TTL and no purge job. `Repo.delete_tenant` (`repo.py:414-432`) is not exposed by any API route or CLI. The endpoint is global, with no data residency (`.env.example:9`; spec 11.6).
- **Prompt injection:**
  - Every prompt says its input is data, not instructions (`prompts.py:10, 58-59, 84, 98, 108`).
  - Answers are wrapped in `<<< >>>` (`extraction.py:122`), and so is website text (`service.py:159`).
  - Output is schema-validated and slot keys are enforced in code.
  - Website seed values are capped at 0.5 confidence (`state.py:17`; `service.py:88-93`).
  - The model has no tools.
  - Two eval cases cover it: `eval_cases/injection_in_answer.json` and `hidden_instructions_website.json`. In the 2026-10-23 run, the slim prompts dropped hostile/injection F1 from 0.8 to 0.4.
- **Input validation:** answers must be non-empty and at most 2000 chars (`interview.py:285-293`; `frontend/src/components/Composer.tsx:3`). Signup fields have max lengths (`src/icp_chatbot/api/schemas.py:11-18`). There is no moderation or abuse classifier.
- **Website fetcher:** public IPs only, every redirect re-checked, robots.txt respected, size and type limits (`src/icp_chatbot/enrichment/fetcher.py:1-8, 66-73`). DNS rebinding remains an open risk, as the file notes.

## 8. Evaluation

- **Eval set:** 14 handwritten JSON cases in `eval_cases/`, each with tags, a signup, optional website text, **fixed scripted answers** (2 to 6), and expected/forbidden slot values. Launch needs at least 30 cases plus a required tag set (`src/icp_chatbot/eval/cases.py:11-21`); `launch_check` currently prints "NOT READY: only 14 cases".
- **Replay:** runs through the real engine on in-memory SQLite in a fixed order (`src/icp_chatbot/eval/replay.py:76-127`). Score is precision/recall/F1 on expected values plus forbidden-value violations (`replay.py:59-73`). The summary is compared with a baseline at a 2% margin (`src/icp_chatbot/eval/report.py:9-84`).
- **What the eval measures:** field accuracy, completion, number of questions, cost and scoring validity. **Nothing about tone, friendliness, question quality, repetition or user experience.** The scripted answers don't respond to what the bot asks, so the back-and-forth itself isn't really tested.
- **Eval/prod mismatch:** `cli.replay` passes no config (`src/icp_chatbot/cli/replay.py:24-25`). The in-memory DB is therefore empty, and the engine builds `default_config` from prompts.py and the .env models, 3.8-flash and 2.5-pro (`interview.py:218-226`; `versions.py:60-99`). **The standard eval tests prompts and models users never get.** Only the ad-hoc scratch script tested with DB v2 settings.
- **Unit tests:** 26 files, **429 passed** on 2026-10-23. They use a scripted `FakeLLM` (`src/icp_chatbot/llm/fake.py:21-60`), so they check control flow and that the right hints are sent, not the wording. Tone-path examples: bare no re-asked once (`tests/test_interview.py:323`), firm refusal (`690`), user question answered (`515`), range (`1067`), stated correction (`1085`), closing line (`934`), open-ended slot gets no options (`tests/test_questions.py:303`).
- **Production metrics:** completion, mean questions, cost, p50/p95 latency, edit rate on approved ICPs, drop-off by slot, and field/weight/requirement edits (`src/icp_chatbot/metrics/queries.py:24-112`; `src/icp_chatbot/icp/diff.py:22-25`). There is no per-message signal. The spec's twice-monthly prompt-optimizer loop (spec 9.3) is not built.

## 9. Frontend

- **Stack and flow:** React 18 + Vite + TypeScript, a single chat page with stages home, signup, interview, fallback, building and done (`App.tsx:9`). Plain JSON fetch through the Vite dev proxy `/api` (`api.ts:14-31`; `frontend/vite.config.ts`).
- **No streaming:** the user sees a "Thinking..." placeholder until the whole turn finishes (`App.tsx:248`). The spec wanted median time to first streamed token under 4 s (spec:20).
- **Suggested answers:** shown as chips, either multi-select with Confirm or single-pick that sends at once. Picks and typed text are joined with ", " into one answer (`App.tsx:141-156, 225-246`).
- **Signup card:** asks only company name, website, industry and role (`frontend/src/components/SignupCard.tsx:4-9`). It never asks for email, size or source, so the UI cannot find a website from a work email.
- **ICP view:** summary, sections and "Still unclear". "Looks good" calls approve plus verdict(true) (`App.tsx:177-183`). "Needs improvement" goes to the revise path (`App.tsx:185-190`; `frontend/src/components/IcpView.tsx:55`). There is no field-edit UI (`editIcp` in `api.ts:41` is unused), no detail-mode button, and verdict(false) is never sent.

## 10. Gaps vs a ChatGPT-style adaptive assistant (observations only)

1. **No cross-session or user memory.** Every interview starts blank. There is no tenant profile, no reuse of earlier answers or ICPs, and no memory the user can view or edit. The spec's episodic and statistical tiers were never built.
2. **No tone adaptation to the user.** Style variety is random per session and question. Formality, verbosity, expertise and mood are not detected, everyone gets the same 25-word limit, and there are no user-set preferences or custom instructions.
3. **Very short context.** Writers see only the last 2 turns, a 600-char summary and the slot JSON. Extraction sees no turns at all. Each call is a single assembled string, not a chat history, so references further back depend on the summary.
4. **The planner drives, not the user.** The bot answers user questions in at most 2 sentences and at most 6 times. Off-topic tolerance is 2 for the whole session, and the counter never resets. After that the chat becomes a static form.
5. **No streaming, and 2 to 3 LLM calls run one after another per turn.** With nested retries, a turn can stall for a long time.
6. **Thin feedback capture.** There is no per-message thumbs or rating, and the UI can't send a negative verdict. Revision text is stored but nothing learns from it: no optimizer loop and no retrieval of good past sessions.
7. **Prompt/config drift across 4 sources:** prompts.py (slim, current), DB v2 (long and hand-patched, with different models), code hints in questions.py (live after a restart), and .env models (used only by the eval). The eval and production don't match, and the tone rules contradict each other ("No problem." required in DB rule 6 and FOLLOW_UP_HINT, banned elsewhere).
8. **Fallback texts break the tone goals:** DECLINE_PREFIX, the frontend's fixed closing line, the fixed off-topic redirect, and English-only fallbacks, notices and UI for non-English users.
9. **The eval ignores conversation quality.** There is no tone or UX rubric, no LLM-as-judge, no simulated reactive user, and 14 static cases where 30 are required.
10. **Privacy controls are partial.** `contains_pii` is never used, there is no redaction or anonymization, no retention policy, no deletion endpoint, and raw answers are duplicated in every turn snapshot.
11. **No abuse or moderation layer.** "Hostile" is tested only as prompt injection.
12. **Limits are basic.** Budget caps apply only at session start, and the rate limiter lives in process memory, so it breaks with more than one worker.
13. **Little transparency during the chat.** The user doesn't see what the bot has recorded until the final ICP. There is no way to correct the bot's understanding along the way beyond answering.
