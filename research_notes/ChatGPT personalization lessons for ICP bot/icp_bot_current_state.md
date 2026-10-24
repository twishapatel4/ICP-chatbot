# ICP bot: current state (for the ChatGPT personalization comparison)

Snapshot: 2026-10-24 (the classic engine has been removed; the model-led interviewer is the only engine), branch `staging`, on top of `27c6033` with uncommitted changes. Paths are relative to the repo root. `spec` = `../docs/superpowers/specs/2026-10-15-icp-chatbot-design.md` (outside the repo). "DB v2" = the live row in Postgres `config_versions`, read with a read-only query on 2026-10-23. References to the engine files use function names (all of `interview.py`, `questions.py`, `planner.py`, `extraction.py`, `prompts.py` and `versions.py` changed on 2026-10-24); line numbers for untouched files are unchanged.

## 0. What changed since the first version of this note

| Change | Where |
|---|---|
| **Prompts come from `prompts.py`, not the DB.** Every config load overlays `DEFAULT_PROMPTS` on the stored row. The DB row still decides the version a session is pinned to, the models and the step settings; its stored prompt text is ignored. | `src/icp_chatbot/versions.py` (`with_code_prompts`), `src/icp_chatbot/store/repo.py` (`live_config`, `get_config`) |
| **The classic planner-driven engine was removed (2026-10-24).** `InterviewEngine` is now model-led: one `interviewer` call per turn decides what to say, whether the profile is ready and what it learned; code validates and limits. Deleted: the planner (`plan_next`, scoring, jitter), the extraction call and prompt, the thread, probe, follow-up, re-ask, reply, clarify, best-customer and question writers, the decline/privacy/range/clarify branches, the per-session topic order, and the `INTERVIEW_ENGINE` and `VARY_QUESTION_ORDER` settings. | `src/icp_chatbot/engine/interview.py` (the merged engine; `agent.py` no longer exists) |
| **The edit form is gone.** The ICP card is read-only with "Looks good" and "Needs improvement"; "Needs improvement" reopens the session through `POST /sessions/{id}/revise`. | `frontend/src/components/IcpView.tsx`; `InterviewEngine.revise`; `app.py` |
| **Not-enough-information gate.** `POST /icp` returns 422 with the missing fields instead of building a thin ICP. | `app.py`; `planner.enough_for_icp` |
| Detail mode now also runs through the interviewer (no new reply, a "go deeper" note). | `InterviewEngine.request_detail` |

Checked claims from earlier notes:

| Claim | Status now |
|---|---|
| App reads prompts from DB v2 | **No longer true.** Prompt text is `prompts.py`. Models and step settings are still the DB row's: `gemini-3.1-flash-lite` for extraction (now unused), seed, opener and question, and `gemini-2.5-flash` (thinking budget 2048) for final_icp. The DB row has no `interviewer` step, so the engine falls back to its `question` settings. `.env` says 3.8-flash and 2.5-pro (`config.py:21-23`), which apply only when there is no DB row or in the eval. |
| Slim prompts are unevaluated | **Still true, and they are now what runs.** The earlier eval (2026-10-23, scratch script, DB v2 models) of the slimmed prompts against the old DB prompts gave mean F1 0.548 vs 0.551, verdict **FAIL** (hostile/injection F1 0.8 to 0.4, empty_website 0.5 to 0.0). That eval covered the removed extraction/question prompts. The new `INTERVIEWER_PROMPT` has never been evaluated. |
| "bot not answerable" commit is about out-of-scope questions | **No**, 86f2651 is about service failures (plain "AI service is not responding" notices, retries, cut-off handling). |

## 1. Purpose, user, output

- Purpose: when a company signs up for LeadNest, a chatbot interviews it and produces that company's Ideal Customer Profile (spec:9; `README.md:3-4`).
- End user: a person at a company signing up to LeadNest (founder, sales and so on), talking about their own customers. Signup fields: company_name, website, email, role, industry, company_size, source (`src/icp_chatbot/domain/signup.py:15-22`). Tenant identity comes from an `X-Tenant-Id` header set by LeadNest's gateway; there is no auth in this service (`app.py:1-5`; `README.md:32`).
- Output: a versioned `ICPDocument` (`src/icp_chatbot/icp/document.py:36-58`): fields grouped by section, each with values, confidence, source (stated/confirmed/inferred), evidence, last_verified and required/preferred; a scoring model of 7 weighted criteria plus disqualifier rules (`src/icp_chatbot/icp/scoring.py:7-8, 22-30`); a generated summary; completeness; and open questions. The builder may reword values but cannot add fields (`src/icp_chatbot/icp/builder.py:3-5, 125-139`).
- **Review step now:** the user sees the summary and fields as plain text and answers "fine or needs improvement". "Looks good" calls approve plus `verdict(correct=true)`. "Needs improvement" asks "what should be changed or added?" and sends the reply to `revise`, which reopens the session, logs a `revision_requested` feedback event and treats the note as an answer; the ICP is rebuilt afterwards. `icp/review.py` and `PATCH /icp` still exist but nothing in the UI calls them.

## 2. Conversation flow

**Type:** the model leads the conversation; code is a guardrail. `src/icp_chatbot/engine/interview.py`, class `InterviewEngine`.

- **Start** (`start`): budget guard, new session pinned to the live config, enrichment (an earlier read of the same website is reused from the cache; otherwise the site is fetched and a seed filled, inferred values capped at 0.5; `src/icp_chatbot/enrichment/service.py`), then an LLM opener (`questions.generate_opener`). The opener prompt asks for a one-sentence description of the company from the website, then "is that right?". Enrichment timeout is 40 s.
- **One call per answer** (`answer` → `_run`, step name `interviewer`). Input (`build_message`): mode and questions asked, industry context, recorded slots as JSON, core fields still unknown, the rolling summary, **the last 12 turns** (`HISTORY_LIMIT`), the last question asked and the user's reply between `<<< >>>`.
- **Output** (`AgentTurn`): field updates, an optional domain update, a summary, `say`, `action` (`ask` or `finish`), `target_slot`, `options`, `enough`, `missing`, `user_wants_to_stop`, `off_topic`.
- **What the model decides:** what to ask and how to word it, how to react to refusals, "what do you mean?", corrections, ranges and dodging, whether the profile is ready (`enough`), and when to finish. Contradictions are settled in conversation: updates are applied with `corrects=True`, so there is no code "which is right?" branch.
- **What code still decides** (`_problem`, `_turn`, `_run`):
  - `say` must be non-empty and at most 700 characters;
  - the model cannot finish unless `enough` is true or the user wants to stop;
  - `enough` is rejected unless the basics are known: product at confidence 0.5 or more plus at least 2 of the customer fields (`planner.icp_floor_met`), checked on a deep copy of the state with the updates applied;
  - a rejected reply goes back to the model **once** with the reason, then counts as a failed step (`MAX_ATTEMPTS = 2`);
  - question cap: quick mode 20, detail mode `min(total cap 25, started + 5)`; if the model still wants to ask at the cap, the interview ends with a model-written closing (`generate_closing`);
  - 2 consecutive model failures offer the fallback form; a second off-topic reply (counted over the whole session) does too;
  - options come from the slot where it has fixed choices, are dropped for `open_ended` slots (`how_we_work`) and are cleaned otherwise.
- **Finish:** `enough` true gives the model's own closing line. If the user stops early without enough, `agent_enough=False` and `agent_missing` are stored and **no closing line is shown**, so the page can say what is missing.
- **Build gate:** `enough_for_icp` uses the interviewer's `agent_enough` (and the floor); sessions without it (the fallback form, "finish" clicked directly) use a 30% rule on core fields. The 422 detail lists `agent_missing` or the missing core slot labels.
- **Detail mode** (`request_detail`): reopens a completed session, sets `mode="detail"` and asks the interviewer for the next detail question with no new reply; if the interviewer fails, nothing is saved and the call is refused. Total cap 25, at most 5 more questions. The API supports it; the UI has no button.
- **Revision** (`revise`): reopens a completed session, logs `revision_requested`, and treats the user's note as the reply to "What should be changed or added in this profile?"; the interviewer records it, asks which part if the note is unusable, or finishes. Prompt rule 11 covers this.
- **Fallback form:** static form of the tier A slots plus 6 tier B slots (`src/icp_chatbot/engine/fallback.py`).
- **Slots:** about 50 standard slots in tiers A (core), B, C and D (`src/icp_chatbot/domain/slots.py`), plus 4 to 7 industry fields the model adds. Tiers now matter only for what counts as "core" and for the catalog the model sees; nothing schedules them.
- **Verified:** unit tests with a scripted fake model, and live Gemini runs on 2026-10-23 and 2026-10-24: the schema was accepted, fields recorded, refusals and a user question got natural replies, options were offered for a list question, and the model finished with a written closing. A normal interviewer call took about 7 to 8 s (2,900 input and about 600 output tokens). In both live runs several calls timed out at 30 s or returned 429 errors; two failures in a row ended one run in the fallback form.

## 3. LLM usage

- **Provider:** Google Gemini via `google-genai==2.31.0`. Two backends, Developer API key or Vertex; `.env` uses vertex with location=global (`src/icp_chatbot/llm/factory.py:13-51`). One adapter behind an `LLM` protocol (`src/icp_chatbot/llm/types.py:76-86`).
- **Steps** (`versions.py`): seed, opener, question, **interviewer**, final_icp. The old `extraction` step and prompt are gone. `interviewer` is the conversation step; `question` now only writes the closing line (`generate_closing`). The DB v2 config predates `interviewer`, so the engine falls back to its `question` settings and overrides temperature (0.6) and max tokens (at least 3072); `MODEL_INTERVIEWER` overrides the model either way. **With it empty, live sessions run on the DB question model, `gemini-3.1-flash-lite`, not on `.env` `MODEL_QUESTION`.**

**Settings** (code defaults in `versions.py`; DB v2 has the same values except the models, and no interviewer row):

| Step | Model (DB v2, live) | Temp | max_output_tokens | Thinking |
|---|---|---|---|---|
| seed | 3.1-flash-lite | 0.0 | 2048 | minimal |
| opener | 3.1-flash-lite | 0.7 | 2048 | low |
| question (closing only) | 3.1-flash-lite | 0.5 | 2048 | low |
| interviewer | question model unless `MODEL_INTERVIEWER` | 0.6 | at least 3072 | low (3.8-flash rejects "minimal") |
| final_icp | 2.5-flash | 0.1 | 8192 | budget 2048 |

- **Calls per answer:** 1 (`interviewer`), 2 if its first reply is rejected, plus a closing call only when the question cap forces an end. Session start: 0 or 1 seed call plus the opener. ICP build: 1 call, cached until a new answer arrives. Calls run one after another.
- **Prompt assembly:** the system instruction is the step's prompt; the interviewer also gets the slot catalog appended. The request content is **one user string per call, not a list of chat messages**; the transcript is `You:`/`User:` lines.
- **Structured output:** JSON with a pydantic `response_schema`; invalid output is retried; a reply cut off at max tokens is retried with double the budget up to 8192. Schemas: `AgentTurn`, `QuestionOutput`, `SeedProfile`, `ICPSynthesis`.
- **Rules the code enforces on output:** unknown slot keys are dropped; a weaker value never replaces a stronger one (so a correction must come in at 0.9); pain points and outcomes given off-target are capped at 0.6; `say` at most 700 chars; options cleaned to 2 to 8 with no "Other"; fixed choices override model options; open-ended slots get none.

## 4. Tone and personality

- **Persona (live now):** `INTERVIEWER_PROMPT` in `prompts.py`, 11 rules: answer before asking; react to something specific in a few words, then ask one direct question of at most 30 words in plain words; never ask what they already told you or repeat a question; start with what they do and who they sell to, ask for an example client once around question 2 to 4; respond to a refusal in your own words, ask once in an easier way (kind of client instead of a name, a rough range instead of an exact figure), then drop it and say lightly that you noticed they passed on it; options only for pick-lists, none for open questions or processes; a closing written for them; a detail-mode rule; and a rule for revisions. `QUESTION_PROMPT` is now a short prompt for the closing line only.
- **Opener:** greet as a guest; if the website was read, describe the company in one sentence from it and ask "is that right?"; otherwise open with one of 20 random angles. A failed model call falls back to a fixed opener variant by role.
- **Variety:** left to the model plus a "vary your wording" rule; there is no style or topic-order randomness any more.
- **Adapting to the user:** language (reply in the user's language); industry (the interviewer writes an industry brief, focus items and custom fields); refusals, evasion, questions to the bot, corrections and off-topic handling are all prompt-driven now, with code limits only on off-topic replies (the second ends in the form).
- **Not adapted:** formality, verbosity, expertise level, mood or pace; there is no user-set tone or custom instructions.
- **Fixed strings that remain:** the service-failure notices (`RETRY_NOTICE`, `DEGRADED_NOTICE`, shown as red error bubbles) and the fixed opener variants. The frontend no longer shows a fixed closing line; when none is written, only the "Building your ICP" note appears.

## 5. Memory and state

- **Per-session working memory** is a `SessionState` JSON in `sessions.state` (`src/icp_chatbot/domain/state.py`; `models.py:42-61`): slots with values, confidence, source, evidence; the website seed; industry context; a rolling summary of at most 600 chars rewritten each turn (by the interviewer); `last_turns` (last 2, no longer read by a prompt); **`history`** (last 12 turns, what the interviewer sees); counters (asked, off-topic, failures; the probe, stall, skipped, declines, thread and order fields are leftovers from the removed engine and are no longer written); the pending question; **`agent_enough` and `agent_missing`** (the interviewer's last judgement). Saves use optimistic locking on row_version (`repo.py:154-195`).
- **Transcript:** every Q/A is stored append-only in `turns` with a state snapshot (`models.py:64-82`). It is still **never read back into a prompt**; `repo.list_turns` has no caller in `src/`. The interviewer gets its transcript from `state.history` instead.
- **Context window:** no token counting or trimming; inputs are bounded by design (the interviewer sees 12 turns, a 600-char summary, answers at most 2000 chars, website text at most 15,000 chars). The slot JSON grows with every filled slot.
- **Across sessions: nothing carries over.** Each start creates a blank `SessionState()`. `tenants.segment` and `sharing_consent` exist and are read nowhere. The spec's episodic and statistical memory tiers were never built.
- **Other stored data:** append-only `icp_versions` (bot vs company author), `feedback_events`, `enrichment_runs`, `config_versions`. Feedback events now include `approve`, `icp_verdict` (only `correct=true` is sent by the UI), `revision_requested` (the user's note) and `detail_mode_start`. The field-, weight- and requirement-edit events are no longer produced by the UI. `enrichment_runs` stores only `ok/partial/failed/blocked/skipped`, not the block reason.
- **Migrations:** a single Alembic revision `0001` that runs `metadata.create_all`. New `SessionState` fields are JSON, so no migration was needed.
- **Lifecycle:** sessions idle for 48h are marked abandoned by a CLI meant to run hourly (`src/icp_chatbot/maintenance.py:11-36`); the job is not scheduled locally.
- **Frontend memory:** messages live only in React state; a reload loses the chat display. `GET /sessions/{id}` exists but the UI never calls it.

## 6. Tokens and cost

- **Tracking:** usage from `usage_metadata` (input, output, thought, cached); a price table with a date-effective price for 3.8-flash (`src/icp_chatbot/llm/pricing.py:17-66`); summed per turn and per session and logged per call without text (`adapter.py:132-136`). Calls are recorded under the `interviewer` model key. A new model needs a price entry or the default is used.
- **Budget limits:** per-tenant daily caps of 200 sessions and 2M tokens, global caps of 2000 sessions and 20M tokens (`config.py:43-47`), **checked only when a session starts** (`src/icp_chatbot/limits.py:22-46`). Rate limit 60 requests/min per tenant, in process memory.
- **Caching:** relies on Gemini implicit prefix caching only (static system prompt first). `INTERVIEWER_PROMPT` plus the catalog is a long static prefix, which helps.
- **Retries:** up to 4 attempts on 429/5xx (backoff 1, 2, 4 s) and invalid output; a timeout is retried once (`adapter.py:18-20, 53, 100-143`). The SDK's own retry sits under it, so retries are nested. Timeout is 20 s by default, 30 s in `.env`. **In the live runs of 2026-10-23 and 2026-10-24 several interviewer calls hit the 30 s timeout or returned 429 errors**, although a normal call takes about 7 to 8 s. Two failures in a row end the chat in the form.
- **Observed cost:** no measurement of the current engine. A call is about 2,900 input and 600 output tokens at the start and grows with the transcript (the earlier $0.011 to $0.017 per eval case was for the removed engine). The spec budget is about $0.14 per interview.

## 7. Privacy and safety

- **Auth:** none; trusts `X-Tenant-Id` and must not be internet-facing. The frontend hardcodes tenant "demo". Every repo call checks tenant ownership.
- **PII:** the extraction model sets `flags.contains_pii` but **no code reads it**; the `AgentTurn` schema has no PII flag. Answers are stored raw in `turns.answer_text` and in every `state_after` snapshot; the interviewer's `history` duplicates recent answers in `sessions.state`. The signup email is used only to find a website. Prompts tell the bot never to ask for personal data. The spec's anonymization step is not built.
- **Logging:** app logs carry no user text. Vertex calls carry labels with session_id, turn and config_version.
- **Retention:** no TTL and no purge job; `Repo.delete_tenant` is not exposed by any route or CLI.
- **Prompt injection:** every prompt says its input is data; answers and website text are wrapped in `<<< >>>`; output is schema-validated and slot keys enforced in code; seed values capped at 0.5; the model has no tools. **Exposure:** the model now writes the user-visible message directly and also decides when to finish, with only length, the "enough" rule and the floor as checks. The two injection eval cases exist, but the earlier slimmed prompts scored lower on them (F1 0.8 to 0.4) and the current engine has not been run against them.
- **Input validation:** answers non-empty and at most 2000 chars; signup fields have max lengths. No moderation or abuse classifier.
- **Website fetcher:** public IPs only, redirects re-checked, robots.txt respected, size and type limits; DNS rebinding remains an open risk. It identifies itself as `LeadNestICPBot/1.0`. On 2026-10-23 three sites rejected that identity: cricbuzz.com (its `robots.txt` disallows all bots except Googlebot), swiggy.com (403 from anti-bot protection) and zomato.com (connection reset, which shows up as a read timeout). Getting past that would need a browser user agent, which was not done.

## 8. Evaluation

- **Eval set:** 14 handwritten JSON cases in `eval_cases/`, each with a signup, optional website text, **fixed scripted answers** (2 to 6), optional detail answers, and expected/forbidden slot values. Launch needs at least 30 cases; `launch_check` prints "NOT READY: only 14 cases".
- **Replay** (`src/icp_chatbot/eval/replay.py`) runs the real `InterviewEngine` on in-memory SQLite, so the harness now covers the engine users get. It has not been run against live models since the engine changed, and the scripted answers still do not react to what the bot asks. Scoring is precision/recall/F1 on expected values plus forbidden-value violations; the summary is compared with a baseline at a 2% margin.
- **Eval/prod mismatch:** `cli.replay` passes no config, so the in-memory DB builds `default_config`. Prompt text now matches production (both are `prompts.py`), but **the models still differ** from the DB row's 3.1-flash-lite and 2.5-flash that users get (the replay uses `.env` models, and has an `interviewer` step the DB row lacks).
- **What the eval measures:** field accuracy, completion, number of questions, cost, scoring validity. **Nothing about tone, friendliness, question quality, repetition or user experience.**
- **Unit tests:** **342 passed** on 2026-10-24, using a scripted `FakeLLM`; they check control flow and the hints sent, not wording quality. `tests/test_interview.py` covers: model-written question and options, open-ended slots, an early finish sent back, enough-without-basics rejected, finishing with the basics, stop-early marked not enough, the question cap, two failures to the form, a success resetting the count, off-topic twice to the form, the transcript shown to the model, a correction applied silently, concurrent answers, config pinning, finish, detail mode (open, five questions, failure leaves the session completed, refused) and revision.
- **Production metrics:** completion, mean questions, cost, p50/p95 latency, edit rate on approved ICPs, drop-off by slot, field/weight/requirement edits (`src/icp_chatbot/metrics/queries.py`). With the edit form gone, the edit-rate metrics will stop moving; `revision_requested` and `icp_verdict` are the new signals and nothing aggregates them yet. The spec's prompt-optimizer loop is not built.

## 9. Frontend

- **Stack and flow:** React 18 + Vite + TypeScript, one chat page with stages home, signup, interview, fallback, building and done (`frontend/src/App.tsx`). Plain JSON fetch through the Vite dev proxy `/api`.
- **No streaming:** the user sees "Thinking..." until the whole turn finishes. The spec wanted median time to first streamed token under 4 s.
- **Suggested answers:** chips, multi-select with Confirm or single-pick that sends at once; picks and typed text are joined with ", " into one answer.
- **Signup card:** asks company name, website, industry and role only (`SignupCard.tsx:4-9`); it never asks for email, size or source.
- **ICP view** (`IcpView.tsx`): read-only summary, sections and "Still unclear", then "Looks good" and "Needs improvement" (no edit controls, weights, completeness bars or exclusion list). "Needs improvement" asks "what should be changed or added?" and sends the next message to `/revise` (`App.tsx`: `improve`, `revising`). If the build is refused, the chat says "You have not provided enough information to build an ICP yet. Still missing: ..." and reopens the composer on the revise path. `verdict(false)` is never sent, so a "needs improvement" click is logged only as `revision_requested`. There is no detail-mode button.

## 10. Gaps vs a ChatGPT-style adaptive assistant (observations only)

1. **No cross-session or user memory.** Every interview starts blank. No tenant profile, no reuse of earlier answers or ICPs, no memory the user can view or edit.
2. **No tone adaptation to the user.** Style variety is left to the model. Formality, verbosity, expertise and mood are not detected and there are no user-set preferences.
3. **Context.** The interviewer sees 12 turns plus the summary. Each call is still one assembled string, not a chat history.
4. **Who drives.** The model decides what to ask and when to stop, with code vetoes. Its quality is untested at scale: two scripted live runs and no live eval.
5. **No streaming, and slow or rate-limited turns.** A normal call takes about 7 to 8 s, but both live runs hit 30 s timeouts or 429s, and two failures in a row drop the user to the form.
6. **Model strength.** Through DB v2 the interviewer runs on a small flash-lite model unless `MODEL_INTERVIEWER` is set; a stronger model was part of the intent and is not yet in place.
7. **Thin feedback capture and no learning.** There is no per-message rating, no `verdict(false)`, and nothing learns from revision notes. A report or prompt-suggestion step over `revision_requested`, `icp_verdict` and refusals was proposed and not built.
8. **Config drift is smaller:** prompts have one source (`prompts.py`). Models and step settings still live in DB v2 and differ from `.env`; the eval ignores the DB row; `interviewer` borrows `question` settings.
9. **The eval ignores conversation quality.** No tone rubric, no LLM judge, no simulated reactive user, 14 static cases where 30 are required, and no live run since the engine changed.
10. **Privacy controls are partial.** `contains_pii` is unused (and absent from the agent schema), no redaction, retention policy or deletion endpoint, and raw answers are duplicated across turn snapshots and `history`.
11. **No abuse or moderation layer.** "Hostile" is tested only as prompt injection, and not against the current engine.
12. **Limits are basic.** Budget caps apply only at session start; the rate limiter lives in process memory; there is no per-session cost cap beyond the question cap.
13. **Little transparency during the chat.** The user doesn't see what has been recorded until the final ICP.
14. **Website scraping is fragile.** Sites with anti-bot protection or restrictive `robots.txt` (the three tested) cannot be read; the failure reason is not stored, and the chat does not tell the user the site could not be read.
