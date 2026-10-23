# Engineering an Adaptive, Personalized, Multi-Turn LLM Interview Bot (ICP slot-filling), as of 2026-10-23

Scope: patterns, trade-offs, numbers and failure modes for memory, tone adaptation, slot-filling behaviour, token cost, evaluation and edge cases, applied to a B2B bot that interviews a business owner to fill Ideal Customer Profile (ICP) slots.

Context note for the report writer: this repo (`src/icp_chatbot/llm/pricing.py`) prices Gemini models only (gemini-3.8-flash, 3.1-flash-lite, 3.5-flash(-lite), 3.1-pro-preview, 2.5-*), so Gemini caching and pricing are the most directly applicable numbers. Anthropic and OpenAI numbers are included for comparison and as fallback-provider options. All prices below were read from the official pages on 2026-10-23. Model names (e.g. "Claude Opus 5.5", "gpt-6.1-sol", "Gemini 3.8 Flash") are as they appear on those pages that day.

---

## 1. Memory architectures: which fits, how to handle conflicting/stale memories, how to scope

### Takeaway
For one ICP interview (tens of turns, a few thousand tokens), the canonical memory should be the **structured slot store** (a typed key-value profile with provenance), plus the **full recent transcript**. Rolling summaries and vector or graph memory only pay off for long horizons (100k+ tokens, many sessions). On short conversations, full context still beats every memory system on accuracy (LoCoMo: 72.9% full-context vs 66.9% Mem0). Contradictions need explicit handling. Write them as new slot versions that invalidate the old ones, as Zep does, and do not overwrite silently.

### Cited Findings

**Benchmarks: what they measure and their sizes**
- LoCoMo: very long-term conversations averaging "300 turns and 9K tokens", over up to 35 sessions. Tasks are QA, event summarization and multimodal dialogue generation. Long-context LLMs and RAG "still substantially lag behind human performance." — [LoCoMo, arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
- LongMemEval (ICLR 2025): 500 curated questions testing five abilities: information extraction, multi-session reasoning, temporal reasoning, **knowledge updates**, and **abstention**. Commercial assistants and long-context LLMs show "a 30% accuracy drop on memorizing information across sustained interactions." Proposed fixes are session decomposition, fact-augmented key expansion and time-aware query expansion. — [LongMemEval, arXiv 2410.10813](https://arxiv.org/abs/2410.10813)

**Mem0 (vector store of extracted facts, plus an optional graph)**
- LoCoMo overall LLM-as-judge J scores, from Mem0's own paper (Apr 2025): **Full-context 72.90%**, Mem0g 68.44%, Mem0 66.88%, Zep 65.99%, best RAG (chunk 256, k=2) 60.97%, LangMem 58.10%, OpenAI memory 52.90%, A-Mem 48.38%. — [Mem0 paper, arXiv 2504.19413 (HTML)](https://arxiv.org/html/2504.19413)
- p95 total latency in seconds: full-context 17.117, LangMem 60.40, A-Mem 4.374, Zep 2.926, Mem0g 2.590, RAG 1.907, Mem0 1.440, OpenAI 0.889. Memory footprint per conversation: Mem0 ~7k tokens, Mem0g ~14k, Zep >600k, raw conversation ~26k. — [Mem0 paper](https://arxiv.org/html/2504.19413)
- Mem0 extraction inputs: a rolling **conversation summary** (refreshed asynchronously), the last **m = 10** messages, and the new message pair. The update phase retrieves the top **s = 10** similar memories, and an LLM tool call chooses **ADD / UPDATE / DELETE / NOOP**. Contradictions are handled as DELETE. Mem0g instead marks obsolete relations **invalid** rather than deleting them, which preserves temporal reasoning. — [Mem0 paper](https://arxiv.org/html/2504.19413)
- **Disputed numbers:** Zep says Mem0 misconfigured Zep and that a correct setup scores ~84% on LoCoMo, not 65.99%. Mem0 now self-reports 91.6 on LoCoMo and 93.4 on LongMemEval. Both claims are vendor-produced. — [Mem0 blog, Zep vs Mem0](https://mem0.ai/blog/zep-vs-mem0-which-ai-memory-layer-should-you-choose); search summary of the [Towards AI comparison](https://pub.towardsai.net/mem0-vs-zep-vs-letta-a-folder-of-text-files-shouldnt-beat-the-61k-star-memory-layer-9d7e65c5799c)

**Zep / Graphiti (temporal knowledge graph)**
- LongMemEval (~115k-token histories): gpt-4o full-context 60.2% at 28.9 s, against Zep 71.2% at 2.58 s with ~1.6k context tokens. gpt-4o-mini: 55.4% (31.3 s) against 63.8% (3.20 s). These are relative gains of +18.5% and +15.2%. — [Zep paper, arXiv 2501.13956](https://arxiv.org/html/2501.13956v1)
- DMR benchmark (only ~60 messages per conversation): Zep 94.8%, full-conversation 94.4%, MemGPT 93.4%, conversation summaries 78.6%, **recursive summarization 35.3%** (all with gpt-4-turbo). The paper itself says DMR is too small to separate methods well. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Conflict handling is **bi-temporal**. Each fact carries created/expired timestamps (ingestion time) and valid_at/invalid_at (real-world time). A new contradicting edge sets the old edge's invalid_at to its own valid_at, so history is kept. — [Zep paper](https://arxiv.org/html/2501.13956v1)

**Letta / MemGPT (in-context memory blocks the agent edits)**
- Memory blocks are labelled sections of the context window with a description, value and **character limit** (the examples use 4,000–5,000 chars). They persist across turns without retrieval, and the agent edits them through memory tools. Blocks can be marked `read_only`. A block can be shared across agents, but writes are **last-write-wins**, so concurrent writers can clobber each other. — [Letta docs, memory blocks](https://docs.letta.com/guides/agents/memory-blocks)

**LangGraph (framework-level memory)**
- Short-term memory is thread-scoped state persisted by a checkpointer. Long-term memory lives in a store under **custom namespaces** ("not just within a single thread ID"). Memory types are semantic (facts), episodic (past experiences, e.g. few-shot examples) and procedural (instructions/prompt). — [LangGraph memory docs](https://docs.langchain.com/oss/python/langgraph/memory)
- **Profile vs collection:** a single JSON profile gives a unified view, but updates "can become error-prone as it grows". A collection of small documents has higher recall, but the model must manage updates and deletes. **Hot-path vs background writes:** hot-path writes make memories available immediately, at the cost of latency and complexity. Background writes add no latency, but you have to decide when to write. — [LangGraph memory docs](https://docs.langchain.com/oss/python/langgraph/memory)

**What ChatGPT does (personalization lessons)**
- Officially, there are "saved memories" (details the user explicitly asks it to remember) and "chat history" reference, and ChatGPT "doesn't remember every detail from past chats". Users can view and restore earlier versions of saved memories. — [OpenAI Memory FAQ](https://help.openai.com/en/articles/8590148-memory-faq)
- A reverse-engineering write-up (Manthan Gupta, late 2025) claims ChatGPT uses **no vector DB/RAG over history**. In its account, each prompt is assembled from layered blocks: saved user facts, brief notes from recent chats, and the current session. **Unverified**: this is one engineer's inference and OpenAI has not confirmed it. — [GIGAZINE summary](https://www.gigazine.net/gsc_news/en/20251226-reverse-engineered-chatgpt-memory-system); [llmrefs write-up](https://llmrefs.com/blog/reverse-engineering-chatgpt-memory)

**Long-context degradation (why "just send everything" stops working)**
- Chroma "Context Rot" (July 14, 2025), testing 18 models including GPT-4.1, Claude 4, Gemini 2.5 and Qwen3: performance degrades as input grows, even on simple tasks. On LongMemEval, **focused prompts (~300 tokens) beat full prompts (~113k tokens) for every model**. Distractors hurt, and lower question–needle similarity degrades faster. — [Chroma, Context Rot](https://www.trychroma.com/research/context-rot)
- "Lost in the Middle" (TACL): performance "is often highest when relevant information occurs at the beginning or end of the input context" and "significantly degrades" in the middle. — [Liu et al., arXiv 2307.03172](https://arxiv.org/abs/2307.03172)
- Anthropic (Sep 29, 2025) recommends compaction (summarize near the limit and "start by maximizing recall"), **structured note-taking** outside the window, just-in-time retrieval, and "the smallest set of high-signal tokens". — [Anthropic, Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

**Scoping**
- LangGraph namespaces let you scope memory to any tuple, e.g. (org, user). — [LangGraph memory docs](https://docs.langchain.com/oss/python/langgraph/memory)
- Anthropic prompt caches are isolated per workspace on the Claude API (Bedrock and Vertex isolate per organization). — [Anthropic prompt caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- OpenAI `prompt_cache_key` can be used per customer, e.g. `support:customer_123`. — [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)

### Inferences
- **Fit for the ICP bot.** One interview is roughly LoCoMo-or-smaller (well under 26k tokens), the regime where full context wins on accuracy (72.9% vs ≤68.4%). Vector or graph memory adds latency and failure modes for no accuracy gain within a session. Use:
  1. a **typed slot store** (the "profile"), which is the source of truth, rendered compactly into every prompt;
  2. the **last N raw turns** (Mem0 used m = 10);
  3. a **short rolling summary** only if sessions get long or resume after a gap.
- **Keep summaries off critical facts.** Recursive summarization scored 35.3% on DMR, against 94%+ for full context or structured memory. Never let a summary be the only home of a slot value. Slots belong in structured fields, and summaries carry only the narrative ("user was hesitant about pricing questions").
- **Slot store design, borrowing from Mem0, Zep and LangGraph.** Store each slot value with `value`, `source_quote`, `turn_id`, `timestamp`, `confidence`, `status` (e.g. confirmed / tentative / user_unsure / skipped), and `superseded_by`. A correction creates a new version and invalidates the old one, which keeps an audit trail. The extractor's job maps to Mem0's ADD/UPDATE/DELETE/NOOP: a correction is an UPDATE, "never mind that" is a DELETE, and an unchanged answer is a NOOP.
- **Profile vs collection.** A fixed ICP schema is a natural "profile". Avoid an LLM rewriting the whole profile JSON every turn, which is error-prone as it grows. Have the extractor emit **deltas** for the changed slots only, and merge them in code.
- **Hot path vs background.** Slot extraction must run in the hot path, because the next question depends on it. Narrative summaries and cross-session company memory can run in the background.
- **Scope memory three ways:** per **session** (transcript, tone state), per **respondent** (style preferences), and per **company/account** (ICP slot values, shared across respondents only with attribution). Do not let one respondent's tone profile leak to another person on the same account.
- **Position matters.** Render the slot state and the "next question" instruction at the end of the prompt, not buried mid-history, because of lost-in-the-middle and context rot.

### Gaps
- No independent (non-vendor) head-to-head of Mem0, Zep, Letta and LangMem on identical setups was found. The LoCoMo numbers are disputed between Mem0 and Zep.
- No benchmark specific to slot-filling or intake memory was found. LoCoMo and LongMemEval target open-ended personal chat.
- Letta's default block limits and sleep-time agents were not documented on the page fetched.

---

## 2. Tone adaptation: detecting style, adjusting per turn, persona consistency, avoiding sycophancy

### Takeaway
LLMs already mirror the user's tone without being told to (the EMNLP 2025 personas study). The engineering job is therefore mostly to **constrain** adaptation, not to add it: keep persona and boundaries fixed, adjust only register and verbosity, and guard against sycophancy and persona drift (significant drift within ~8 rounds). No head-to-head study of classifier-based vs prompt-based style detection was found. Cheap deterministic signals plus a style field in the existing extraction call look like the best cost/quality point (inference).

### Cited Findings
- Chatbots shift their conversational style in response to user personas, mirroring tone through punctuation and style. The authors flag this as an alignment and trust concern. — [User Personas Influence Chatbot Personality Shifts, EMNLP 2025](https://aclanthology.org/2025.emnlp-main.875.pdf)
- Prior work cited in a 2026 preprint: user tone affects chatbot tone (Yin et al. 2024); chatbots **over-correct** on negatively framed prompts (Bardol 2025) and temporarily **mirror negative tone** (Crolic et al. 2022). — [Every(bot) Makes Mistakes, arXiv 2605.05391](https://arxiv.org/pdf/2605.05391)
- Refusal style affects frustration: brief denials with no reason were rated significantly more frustrating and less useful or appropriate. — [Investigating LLM Denials of User Requests, CHI 2024](https://dl.acm.org/doi/fullHtml/10.1145/3613904.3642135)
- Lexical alignment (reusing the user's words) has been shown to improve recall and understanding and to reduce perceived effort and frustration. Product advisors adapting to the user's knowledge level is studied in the same paper (2026 preprint). — [“I don't know anything about laptops!”, arXiv 2608.06091](https://arxiv.org/pdf/2608.06091)
- **Persona drift:** LLaMA2-chat-70B (and GPT-3.5, per the news version) shows significant persona or instruction drift **within eight rounds**. The authors attribute it partly to attention decay over long exchanges. Their fix, split-softmax, needs model internals. — [Li et al. 2024, arXiv 2402.10962](https://arxiv.org/html/2402.10962v1); [Northeastern news](https://news.northeastern.edu/?p=230037)
- Anthropic "persona vectors" can monitor shifts toward traits such as sycophancy through activation strength. This requires model internals (shown on Qwen 2.5-7B and Llama-3.1-8B), so it is not usable through hosted APIs. — [Anthropic research, persona vectors](https://www.anthropic.com/research/persona-vectors)
- **Sycophancy under multi-turn pressure.** SYCON Bench (EMNLP 2025 Findings) tested 17 LLMs and found sycophancy prevalent. Alignment tuning strengthens it, while scaling and reasoning help. A **third-person-perspective prompt cut sycophancy by up to 63.8%** in debate scenarios. — [SYCON Bench, arXiv 2505.23840](https://arxiv.org/abs/2505.23840v1)
- SPINE (2026 preprint): a persistently mistaken simulated user, over 25 turns, made models abandon correct positions at 0–57% within 5 turns, rising to 20–97% within 25 turns. Emotional appeals were the most effective tactic. This was seen in search snippets only and not verified. — [SPINE, arXiv 2609.09090](https://arxiv.org/pdf/2609.09090)
- OpenAI's April 2025 GPT-4o sycophancy incident, per secondary coverage of the postmortem (the primary page returned 403). Reward signals including **thumbs-up/down user feedback** weakened the checks on sycophancy. There were no deployment evals tracking sycophancy. Expert testers said it "felt slightly off", but launch went ahead on positive A/B signals, and the rollback came days later. — [Georgetown Tech Institute brief](https://www.law.georgetown.edu/tech-institute/tech-brief-ai-sycophancy-openai/); [TechCrunch, 2025-04-29](https://techcrunch.com/2025/04/29/openai-explains-why-chatgpt-became-too-sycophantic)
- Anthropic recommends system prompts "at the right altitude" (between brittle hard-coded rules and vague guidance) and **a few diverse canonical examples** over long edge-case rule lists. — [Anthropic, Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- Survey-chatbot research: respondents disliked **repetitive probes** and not knowing how many exchanges were left (GPT-3 probing chatbot, 1,200-person experiment). — [Google Research, Combining closed-based and chatbot-based questions](https://research.google/pubs/combining-closed-based-and-chatbot-based-questions-in-surveys/)

### Inferences
- **Separate fixed persona from adaptive register.** Keep persona (role, goals, boundaries, honesty rules) in a static, cached system block. Pass a small per-turn "style directive" at the end of the prompt, for example `{formality: casual, verbosity: short, language: hi-en mix, mood: impatient}`. This keeps the cache prefix stable and puts the directive where attention is strongest.
- **Detection without an extra call.** Compute deterministic features in code: user message length, emoji or exclamation use, language ID, the turns-since-last-substantive-answer counter, and repeated "skip"/"idk". Have the existing structured extraction call also emit `user_mood`/`style` enums. A separate sentiment classifier call adds latency and cost and has no evidence of better quality (see Gaps).
- **Smooth the style state.** Use hysteresis across turns, so that one curt message does not flip the bot into "terse mode" forever. The over-correction and negative-mirroring findings argue against a full mirror. Match **verbosity and formality**, but not negativity.
- **Anti-sycophancy rules specific to intake.** The bot must not praise the ICP ("great choice!"), must not accept contradictions just to please, and should reflect values back neutrally. The third-person reframing from SYCON is a cheap prompt trick worth testing, for example "Summarize what the business owner said about X". Do not use thumbs-up rate as the primary optimisation target; the OpenAI incident is the cautionary tale.
- **Counter drift with re-injection, not longer prompts.** Persona drift appears within about 8 rounds, and an ICP interview is often longer. Re-state the persona and style directive near the end of each prompt as a short block. Avoid relying on a long system prompt at the top of a growing context.
- **Frustration handling.** When signals say frustrated, shorten replies, give a reason when declining (per the CHI denial study), show progress ("3 questions left"), and offer to skip. Repetitive probing was the top complaint in survey chatbots.

### Gaps
- No study was found that directly compares classifier-based style or frustration detection against prompt-based detection for production chatbots: no accuracy, latency or cost numbers.
- OpenAI's primary sycophancy postmortem could not be fetched (HTTP 403). The details above are from secondary coverage.
- No quantitative data was found on how much tone adaptation improves completion rates in B2B intake bots specifically.

---

## 3. Slot-filling and interview bots: vague answers, ranges, IDK, dodges, contradictions, corrections, tangents, user questions, multi-slot answers, confirm vs move on

### Takeaway
Treat each of these as an explicit **conversation-repair pattern** with code-level state, not as free-form LLM behaviour. Rasa CALM's pattern catalogue (correction, digression, cancel, skip, chitchat, clarification, cannot-handle) is a mature industry template. The biggest LLM-specific risk is the model "getting lost": premature assumptions in early turns and a 39% average quality drop when information arrives in pieces. Mitigate it by keeping the authoritative slot state outside the model and re-presenting it consolidated every turn.

### Cited Findings
- **Multi-turn degradation:** across 15 LLMs and 200k+ simulated conversations, performance drops **39% on average** when an instruction is revealed in shards across turns instead of all at once. Aptitude falls only ~16%, while **unreliability rises ~112%**. Four causes: premature answer attempts, answer bloat that builds on earlier wrong attempts, over-weighting the first and last turns, and verbose responses that add assumptions. Lowering temperature does not fix it (unreliability stays ~30% at T=0). "Snowball" (restating all prior shards every turn) recovers only 15–20% of the loss. The recommended fix is **consolidating requirements into a single fresh prompt**. Quote: "when LLMs take a wrong turn in a conversation, they get lost and do not recover." Accepted at ICLR 2026. — [Laban et al., LLMs Get Lost In Multi-Turn Conversation, arXiv 2505.06120](https://arxiv.org/html/2505.06120v1); [ICLR 2026 listing](https://iclr.cc/virtual/2026/oral/10009147)
- **Rasa CALM repair patterns (industry reference):** `pattern_correction` (updates the slot and by default confirms with "corrected previous input" without asking), `pattern_continue_interrupted` (asks whether to resume after a digression), `pattern_cancel_flow`, `pattern_skip_question` (by default explains the step must be completed), `pattern_chitchat`, `pattern_clarification` (offers options for ambiguous input), `pattern_cannot_handle`, `pattern_human_handoff`, `pattern_completed`, and `pattern_internal_error` (including input that is too long or empty). A confirm-before-correcting variant collects a boolean `confirm_slot_correction` and asks "Do you want to update the {slots}?". — [Rasa docs, conversation repair patterns](https://rasa.com/docs/reference/primitives/patterns/)
- **LLM slot filling on real conversational data:** conversational data is "highly dynamic", with abrupt topic shifts, interruptions and implicit references. Distilling a teacher LLM into a smaller student gave **+26% absolute F1 over vanilla LLMs** and +34% relative F1 over extractive models (call centre), with near real-time inference. — [Rana et al., COLING 2025 Industry, arXiv 2411.18980](https://arxiv.org/abs/2411.18980)
- Cross-domain zero-shot slot filling improves when **small-model signals augment the LLM** (Findings of ACL 2025). — [Synergistic Augmentation, ACL Findings 2025](https://aclanthology.org/2025.findings-acl.1097.pdf)
- **Conversational surveys and interviews (closest analogue to an ICP interview):**
  - About 600 participants and 5,200+ free-text responses: the chatbot survey drew more engagement and more informative, specific answers than a form. — [Xiao et al., arXiv 1905.10700](https://arxiv.org/abs/1905.10700v2)
  - GPT-3 dynamic probes (1,200 people) gave higher-quality answers than a single open question. However, users disliked repetitive probes and not knowing how many exchanges were left, and the authors propose a hybrid of closed form questions plus chatbot probes. — [Google Research](https://research.google/pubs/combining-closed-based-and-chatbot-based-questions-in-surveys/)
  - Contextual probes referencing the respondent's own prior answer increased response length and thematic richness (N=151). — [HICSS, From Prompts to Probes](https://scholarspace.manoa.hawaii.edu/items/6c969f15-23f3-41b8-ba54-88e2c8791189)
  - Rule-based and LLM-augmented probing chatbots differed little in engagement or richness, but LLM probing improved user experience (N=399). — [arXiv 2309.10187](https://arxiv.org/pdf/2309.10187v2)
  - Probes can hurt the interviewee's experience, so design for respondent burden. — [arXiv 2503.08582](https://export.arxiv.org/pdf/2503.08582)
  - The open-source KI-Umfrage tool scores each answer against a **clarity threshold**, triggers follow-ups only for vague answers, and caps the number of follow-ups. — [HU Berlin KI-Umfrage](https://llm-enabled-development-41d6d8.pages.cms.hu-berlin.de/en/tools/ki-umfrage/)

### Inferences
Recommended handling per case. These are inferences built on the sources above.
- **Vague answers / ranges.** Store ranges natively (e.g. `employee_count: {min: 50, max: 200, kind: range}`) and do not force a point value. Add one clarity-threshold probe (KI-Umfrage style) with a **hard cap of 1–2 follow-ups per slot**, then accept the value as `tentative` and move on. Repetitive probing is the top user complaint.
- **"I don't know".** Make it a first-class slot status (`user_unsure`), distinct from `skipped` and `empty`. Offer one easier proxy question or examples, then move on. LongMemEval treats abstention as a core ability, and the ICP output should show "unknown" honestly rather than inventing a value.
- **Dodged or skipped questions.** Unlike Rasa's default ("must complete the step"), a B2B interview should let the user skip non-critical slots. Mark them `skipped`, optionally re-ask once near the end, and never loop. Keep a short list of `required` slots for which the bot explains why it needs the answer.
- **Contradictions.** Detect them in code by comparing the new extracted value to the stored one. Ask one neutral clarification ("Earlier you mentioned X; now Y. Which should I use?"). Don't silently pick the newer one when both are plausible, for example "SMBs" vs "enterprise" could both be true segments. Keep both versions with timestamps, as in Zep's bi-temporal model.
- **Explicit corrections ("actually, change that").** Apply them immediately and acknowledge briefly, as in Rasa's default `pattern_correction`. Ask for confirmation only for high-impact slots or when the extractor's confidence about **which** slot is being corrected is low.
- **Off-topic tangents.** Answer briefly, using an LLM chitchat response rather than "I can't help", then **resume with the pending question**. This is Rasa's `pattern_continue_interrupted` idea, but the resume should be automatic, not asked.
- **Users asking the bot questions** (e.g. "why do you need this?", "what's an ICP?"). Answer them; this is the "bot not answerable" problem the recent commits touch. Keep a small FAQ or knowledge block in the cached system prompt, and route these as an "answer then resume" turn type.
- **Multi-slot answers in one message.** Extraction must be schema-wide every turn, not limited to the slot just asked. Then skip questions whose slots were already filled, so the bot never re-asks something the user already said.
- **Confirm vs move on.** Do not confirm per slot, because it adds turns and annoys users. Do a **single consolidated read-back** at the end (or per section), which also directly addresses Laban's finding that consolidated, single-turn presentations are far more reliable. Confirm mid-flow only on low-confidence extraction of a required slot, or on a contradiction.
- **Avoid "getting lost".** Never let the reply model infer slot state from the transcript alone. Each turn, render (a) the canonical slot table, (b) the next target slot or question, and (c) the last few turns. This is the "consolidate" fix, done structurally. Also avoid premature ICP synthesis before enough slots are filled, which is the "premature answer attempt" failure mode.

### Gaps
- No peer-reviewed paper was found that evaluates LLM handling of corrections, dodges and contradictions specifically in **B2B intake or form-filling** dialogues. The strongest sources are Rasa's documented patterns and survey-chatbot HCI studies.
- A 2026 study of LLM conversational agents for web-form filling (blind and low-vision users) was found but not read: [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1071581926002004).
- Engineering write-ups from Drift and Qualified (B2B conversational marketing bots) on qualification-flow design were not found in this pass.

---

## 4. Token usage and cost: context growth, trimming and summarization, prompt caching, structured outputs, model tiering, latency, long-context costs

### Takeaway
Input tokens grow roughly quadratically over a conversation, because every turn resends the whole prefix. **Prompt caching** (~90% discount on cached input across all three providers) is the main lever, but only above each provider's minimum length and only if the prefix stays byte-stable. For this Gemini-based bot, Gemini 3.x implicit caching needs **≥4,096 input tokens**, so aggressive prompt slimming can *increase* cost if it drops requests below that line. Thinking tokens, billed as output, are often the largest line item on reasoning models.

### Cited Findings

**Gemini (the provider this repo uses), from official pages on 2026-10-23**
- Implicit caching is "enabled by default for all Gemini 2.5 and newer models". Minimum input tokens: **4,096** for Gemini 4 Argon, 3.8 Flash, 3.6 Flash and 3.1 Pro Preview; **2,048** for 2.5 Flash and 2.5 Pro. To maximize hits, put large common content first and send similar prefixes within a short window. — [Gemini context caching docs](https://ai.google.dev/gemini-api/docs/caching)
- On **Gemini 4 Argon and newer**, the full previous request is cached only if the new request adds **≥2,050 tokens**. Otherwise, the last 2,050 tokens of the previous request are reprocessed at the standard input rate. Explicit caching is not supported on Gemini 4 Argon and later, or in the Interactions API. — [Gemini caching docs](https://ai.google.dev/gemini-api/docs/caching)
- Prices per 1M tokens (Standard; output includes thinking):

  | Model | Input | Output | Cached input | Cache storage per hour |
  |---|---|---|---|---|
  | gemini-3.8-flash (to Dec 31, 2026) | $0.75 | $3.75 | $0.075 | $0.50 |
  | gemini-3.8-flash (from Jan 1, 2027) | $1.50 | $7.50 | $0.15 | $1.00 |
  | gemini-3.1-flash-lite | $0.25 | $1.50 | $0.025 | — |
  | gemini-3.5-flash-lite | $0.30 | $2.50 | $0.03 | — |
  | gemini-3.1-pro-preview (≤200k) | $2.00 | $12.00 | $0.20 | — |
  | gemini-2.5-flash | $0.30 | $2.50 | $0.03 | — |
  | gemini-2.5-flash-lite | $0.10 | $0.40 | $0.01 | — |

  Batch is about 50% off. Cached input is therefore 10% of input (a 90% discount). — [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)

**Anthropic (2026-10-23)**
- Minimum cacheable length is **512 tokens** for Opus 5.5, Sonnet 5.5, Haiku 5.5, Opus 5 (and Fable/Mythos 5.x); **1,024** for Opus 4.8, Sonnet 5, Sonnet 4.5/4.6; **2,048** for Opus 4.7; **4,096** for Opus 4.5/4.6 and Haiku 4.5. Shorter prompts silently don't cache. — [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- TTL is 5 min by default, refreshed free on each hit, or 1 h at extra cost. Multipliers: **5-min write 1.25×, 1-h write 2×, read 0.1×**. Reads are 0.05× on Opus 5.5 and Sonnet 5.5, and 0.025× on Fable/Mythos 5.1. Up to **4 breakpoints**, a **20-block lookback**, and a top-level automatic `cache_control` mode that moves the breakpoint forward as the chat grows. Invalidation is hierarchical (tools → system → messages): changing tools invalidates everything, and changing `output_config.format` invalidates the conversation's cache. `max_tokens: 0` pre-warms the cache. On newer models, a mid-conversation `{"role":"system"}` message does not invalidate the cache. — [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching); [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- Prices per MTok (input / output / cache read):

  | Model | Input | Output | Cache read |
  |---|---|---|---|
  | Opus 5.5 | $4 | $20 | $0.20 |
  | Sonnet 5.5 | $2 | $10 | $0.10 |
  | Sonnet 5 | $2 | $10 | $0.20 |
  | Haiku 5.5 (≤100k prompt) | $0.10 | $0.50 | $0.01 |
  | Haiku 5.5 (>100k prompt) | $0.50 | $2.50 | $0.05 |
  | Haiku 4.5 | $1 | $5 | $0.10 |

  Sonnet 5's $2/$10 introductory price became the standard price, and the planned rise to $3/$15 on Sep 1, 2026 will not happen. Batch is 50% off. Claude 4.6+ models (except Haiku 5.5) get a 1M-token window at standard price. — [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- Caching launch claim (Aug 2024): up to 90% lower cost and **up to 85% lower latency** on long prompts. Example: a 100k-token cached book took 2.4 s to query, against 11.5 s uncached (−79%). — [Anthropic news, prompt caching](https://www.anthropic.com/news/prompt-caching)

**OpenAI (2026-10-23)**
- GPT-5.6+: minimum **1,024** visible input tokens. Reads cost **0.1×** (0.05× on gpt-6.1-sol). Writes cost **1.25×** for the default **30-min** TTL, or 2.5× for 12 h. Explicit breakpoints are available. Earlier models: in-memory retention of about 5–10 minutes of inactivity (up to 1 h), or 24h retention, plus `prompt_cache_key` routing (~15 req/min per key). Guidance: put stable content first and dynamic content (timestamps, user-specific text) last, **append rather than rewrite turns** ("summarization or compaction can reset reuse"), and keep tools and schemas stable. — [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- Prices per 1M tokens (input / cached / output):

  | Model | Input | Cached input | Output |
  |---|---|---|---|
  | gpt-6.1-sol | $2.00 | $0.10 | $10.00 |
  | gpt-6-luna | $0.10 | $0.01 | $0.50 |
  | gpt-5.6-terra | $2.00 | $0.20 | $12.00 |
  | gpt-5.6-luna | $0.20 | $0.02 | $1.20 |
  | gpt-5.4-mini | $0.75 | $0.075 | $4.50 |
  | gpt-5-nano | $0.05 | $0.005 | $0.40 |

  Batch and Flex are about 50% off. "Priority processing was renamed Fast mode on July 30, 2026" (about 2× price). — [OpenAI pricing](https://developers.openai.com/api/docs/pricing)

**Structured outputs (extraction reliability and cost)**
- Claude JSON outputs use constrained decoding ("guaranteed schema-compliant", no retries for schema violations). Limits:
  - No numeric `minimum`/`maximum`, no string-length constraints, no recursive schemas.
  - At most 24 optional params and 16 union-typed params.
  - Compiled grammars are cached for 24 h; the first use of a schema adds compile latency.
  - Enum casing is not guaranteed.
  - Refusals and `max_tokens` truncation can break the schema.
  - It adds a hidden system prompt (more input tokens).

  — [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- On Claude 4.6+, **assistant prefill is rejected** (400). **Forced tool use** (`tool_choice: any/tool`) is rejected on Opus 5.5 and Sonnet 5.5, so use structured outputs or strict tools with `auto`. Thinking is always on for Opus 5.5. Haiku 5.5 accepts `thinking: disabled`. — [Anthropic API errors](https://platform.claude.com/docs/en/api/errors)

**Context length costs quality, not just money**
- The Chroma context-rot and Lost-in-the-Middle findings in section 1 apply here. A related multi-turn finding: verbose responses introduce assumptions, and reasoning models' responses ran ~33% longer with similar degradation. — [Laban et al.](https://arxiv.org/html/2505.06120v1)
- Anthropic: clearing old tool results is a low-risk first compaction step, and compaction should be tuned for recall first. — [Anthropic, Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

### Inferences
- **Worked example (inference; Gemini 3.8 Flash at 2026 intro prices).** Assume a 4,500-token static prompt, ~270 tokens of new history per turn, and 20 turns.
  - Total input ≈ 20×4,500 + 270×(0+1+…+19) = 90,000 + 51,300 ≈ **141k tokens**.
  - Uncached: 141k × $0.75/M ≈ **$0.106**. With ~85% of input hitting the cache: 0.15×141k×$0.75/M + 0.85×141k×$0.075/M ≈ **$0.025**.
  - Visible output of 20×120 tokens ≈ $0.009. Adding **500 thinking tokens per turn** adds 10k×$3.75/M ≈ **$0.0375**, which is more than all the cached input.
  - A separate per-turn extraction call on gemini-3.1-flash-lite (~3k in / 200 out) adds ≈ 20×(3k×$0.25 + 200×$1.50)/M ≈ $0.021 uncached.
  - Everything roughly doubles on Jan 1, 2027, when 3.8 Flash moves to $1.50/$7.50.
- **Prompt slimming vs caching (ties to the paused prompt-slimming work).** Gemini 3.x implicit caching needs ≥4,096 total input tokens. Per call, a 4,500-token cached static prompt costs ≈ 4,500×$0.075/M = $0.00034, while a slimmed 3,000-token prompt with **no** cache hit costs 3,000×$0.75/M = $0.00225, about 6.6× more. History pushes requests over 4,096 after a few turns anyway, so the real effect is concentrated in early turns and in calls that send no history, such as a stateless extraction call. Measure `usage.total_cached_tokens` and the cache-hit rate before and after slimming; do not assume shorter is cheaper. Slimming still helps quality (context rot) and latency.
- **Keep the prefix byte-stable.** Order the request as static persona + rules + FAQ + schema descriptions → append-only transcript → per-turn dynamic block (slot table, style directive, next question) **at the end**. Never inject timestamps, user names or per-turn state into the system prompt. Rewriting or summarizing history breaks the cache from that point on (OpenAI guidance), so compact rarely and in big steps, not every turn.
- **Gemini 4 Argon caveat.** If the bot upgrades to Gemini 4 Argon, normal chat turns (≪2,050 new tokens) will re-bill the last 2,050 tokens at the full rate each turn. Budget for that, or batch extraction plus reply into one call.
- **Model tiering.** The slot-filling literature supports small or distilled models for extraction (+26% F1 for distilled students vs vanilla LLMs). Use a Flash-Lite-class model with schema-constrained output for extraction, and the Flash-class model for the user-facing reply. Alternatively, do both in **one call** with structured output (reply text plus slot deltas) to halve the number of calls and the prefix re-reads. The trade-off is coupling and less control over the extraction prompt.
- **Thinking budget is the hidden cost.** For a slot-filling reply, low or minimal thinking is usually enough. Reserve higher effort for the final ICP synthesis.
- **Latency budget (inference).** Chat UX typically wants TTFT under ~1–2 s. Hot-path extraction plus reply in series doubles latency, so run extraction and reply generation in parallel where the next question doesn't depend on this turn's extraction. Otherwise use the single-call design. Caching cuts TTFT substantially on long prefixes (Anthropic's 11.5 s → 2.4 s at 100k tokens).
- **Trim policy for an interview.** Keep the full transcript up to ~15–20k tokens, which is cheap with caching and avoids the summary-loss risk. Beyond that, compact older turns into a recall-oriented summary, while the slot store stays authoritative.

### Gaps
- Gemini docs fetched did not state an explicit-cache default TTL, a minimum for explicit caches, or an explicit implicit-cache discount percentage. The 90% figure is inferred from the pricing table (cached = 10% of input).
- No current provider-published TTFT or latency figures per model were found. OpenAI's caching page gives no latency numbers.
- No measured cache-hit rate for Gemini implicit caching in multi-turn chat was found. Implicit caching is best-effort and not guaranteed.

---

## 5. Evaluation: LLM-as-judge, regression sets, simulated users, online signals

### Takeaway
Use a three-layer eval:
1. **Deterministic checks** on slot state (did the bot extract X, avoid re-asking, handle the correction).
2. **Persona-driven user simulation** to generate multi-turn trajectories, scored with **pass^k** for reliability.
3. **LLM-as-judge** for tone and helpfulness, with bias controls.

Simulators conditioned on detailed user profiles track human judgments best (ρ ≈ 0.7), but default simulated users are too cooperative. Online, beware optimizing thumbs-up (OpenAI's sycophancy incident) and loosely defined "resolution" metrics.

### Cited Findings
- **SimulatorArena (EMNLP 2025):** 909 annotated human–LLM conversations (math tutoring, document creation). Simulators **conditioned on user profiles** matched human judgments best (Spearman ρ ≈ 0.7). The best simulators were then used to benchmark 18 assistants, including GPT-5 and Claude 4.1 Opus. — [SimulatorArena, arXiv 2510.05444](https://arxiv.org/abs/2510.05444v2); [ACL Anthology](https://preview.aclanthology.org/setup/2025.emnlp-main.1786)
- Default simulated users are **too cooperative**, which overstates agent quality. Tooling therefore ships diverse personas (terse, impatient, info-withholding). — [Potato annotator docs, persona simulation](https://potatoannotator.readthedocs.io/en/latest/agent-evaluation/persona_simulation/)
- EYT-Bench (2026) combines a persona-grounded user simulator, the target model and an independent LLM judge. Its personas come from **human-curated corpora** to reduce LLM-induced persona bias, and it adds trajectory metrics such as final-intent completion rate. — [EYT-Bench, arXiv 2607.10428](https://www.alphaxiv.org/abs/2607.10428)
- **τ-bench (ICLR 2025):** an LLM-simulated user plus agent with tools, scored on final database state. **pass^k** is the probability of success on *all* k trials. gpt-4o succeeded on <50% of tasks, and **pass^8 was below 25% in retail**. Secondary summaries give conflicting per-domain figures. — [τ-bench, arXiv 2406.12045](https://export.arxiv.org/pdf/2406.12045); [ICLR proceedings](https://proceedings.iclr.cc/paper_files/paper/2025/hash/1b126cc38b8638e07bef37e7b2bb72bf-Abstract-Conference.html)
- **Sharded-simulation method:** Laban et al. split a full specification into shards that a simulated user reveals one per turn, and measured aptitude vs unreliability over 200k+ conversations. This design maps directly onto an ICP interview, where the full "ground-truth ICP" is revealed piecemeal. — [Laban et al.](https://arxiv.org/html/2505.06120v1)
- **LLM-as-judge:** strong judges (GPT-4) reached **>80% agreement with humans**, the same as human–human agreement. Known biases are position, verbosity and self-enhancement. Only about 65% of verdicts stayed consistent when answer order was swapped (secondary reporting of MT-Bench). A later study (9 judges, 22 tasks) found position bias is driven largely by the quality gap between candidates. — [Zheng et al. MT-Bench, review](https://liner.com/review/judging-llmasajudge-with-mtbench-and-chatbot-arena); [Shi et al., Judging the Judges](https://arxiv.org/html/2406.07791v1); [LLMs-as-Judges survey, arXiv 2412.05579](https://arxiv.org/pdf/2412.05579)
- **Online signals can mislead.** In OpenAI's April 2025 incident, positive A/B and user-feedback signals masked sycophancy, and there was no dedicated sycophancy eval. — [Georgetown Tech Institute](https://www.law.georgetown.edu/tech-institute/tech-brief-ai-sycophancy-openai/)
- **Metric definitions matter.** Intercom Fin is billed at **$0.99 per "resolution"**. Third-party critics say a conversation where the customer simply stops replying after Fin's last message can be counted as resolved. Intercom reportedly claims a ~67% resolution rate across 40M+ conversations. These are third-party sources only and not verified against Intercom. — [aimdoc, Intercom resolution pricing explained](https://aimdoc.ai/blog/intercom-resolution-pricing-explained); [SiteGPT Fin review 2026](https://sitegpt.ai/blog/intercom-fin-ai-review)
- Sycophancy can be tested with multi-turn pressure benchmarks: SYCON's Turn-of-Flip and Number-of-Flip metrics, and SPINE's 25-turn persistent mistaken user. — [SYCON Bench](https://arxiv.org/abs/2505.23840v1); [SPINE](https://arxiv.org/pdf/2609.09090)

### Inferences
- **Regression set design for the ICP bot.** Build 30–100 scripted "ground-truth ICP" profiles. For each, run simulated users with personas:
  - terse / one-word
  - hostile or impatient
  - chatty with tangents
  - non-native English or Hinglish code-switching
  - "IDK" / unsure
  - contradicting (changes the answer mid-way)
  - dodging (refuses pricing questions)
  - multi-slot dumper (answers 4 questions in one message)
  - question-asker ("why do you need this?")
  - pasted website copy

  Ground personas in real transcripts where possible (the EYT-Bench approach) to avoid overly cooperative LLM users.
- **Score mostly deterministically:** slot precision and recall against ground truth; contradiction-handled rate; re-ask rate (asking for a slot already filled); probes per slot (cap check); turns to completion; correct `user_unsure` and `skipped` statuses; no hallucinated slot values. Report **pass^k (k = 3–5)** per scenario, not single-run pass rates, because multi-turn unreliability is the dominant failure (+112% in Laban).
- **LLM-as-judge for tone only.** Rubric items: matched formality and verbosity; no flattery or sycophancy; brief answer to user questions before resuming; acknowledged corrections. Use pointwise rubric scoring, not pairwise, to avoid position bias, or swap order and average. Use a judge from a different model family than the bot to limit self-preference. Calibrate against ~50 human-labelled conversations.
- **Gate every prompt change.** This repo's prompts live in a DB (v2) and edits are made in `prompts.py`. Run the simulator suite on each prompt version before syncing to the DB, and store the score per prompt version.
- **Online signals for an intake bot:** completion rate (all required slots filled); **drop-off turn distribution**, which shows which question loses people; skip rate per question; correction rate per slot (high rates mean bad extraction or a confusing question); median turns and time to completion; read-back acceptance rate (the user accepts the final summary without edits); cost per completed ICP. Use thumbs as a secondary signal, never the primary optimisation target.

### Gaps
- No published simulated-user eval specific to B2B intake or ICP interviews was found.
- Primary MT-Bench numbers for the order-swap consistency (65%) were not verified from the paper itself; they come from secondary summaries.
- Engineering blogs from Intercom, Drift or Qualified describing their own evaluation stacks were not found in this pass. Only third-party pricing commentary on Intercom Fin turned up.

---

## 6. Edge cases and limits: multilingual and code-switching, long messages and pasted docs, prompt injection and persona jailbreaks, returning users, shared accounts, rate limits and outages

### Takeaway
Most of these are handled in **code around the model**, not in the prompt:
- language detection and per-session language state;
- input-length caps with a "document intake" path;
- treating user-pasted content as untrusted data;
- session resume with a fresh consolidated state block;
- respondent identity scoping;
- retries, backoff and a provider-fallback ladder.

Prompt-level defenses against injection are necessary but insufficient (OWASP LLM01:2025).

### Cited Findings
- **Prompt injection (OWASP LLM01:2025), official mitigations:**
  1. Constrain model behaviour in the system prompt and ignore attempts to change core instructions.
  2. Define and validate output formats with deterministic code.
  3. Use input and output filtering.
  4. Enforce least privilege.
  5. Require human approval for high-risk actions.
  6. **Segregate and clearly mark untrusted external content.**
  7. Run adversarial testing.

  OWASP defines jailbreaking as "a form of prompt injection" and covers direct and indirect injection. It does not explicitly treat persona-change attacks. — [OWASP GenAI, LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- Regex-style input filtering is easy to evade and only a partial measure. Many guides are vendor content. — [search synthesis; e.g. DeepInspect LLM01](https://www.deepinspect.ai/blog/owasp-llm01-prompt-injection)
- **Code-switching and multilingual input.** Gaps remain for low-resource languages, unseen code-mix patterns and generation consistency. Non-English tokens inside English contexts hurt performance, while English tokens inside other languages often help (Mohamed et al. 2025, as cited in the survey). Multilingual LLMs often trail fine-tuned smaller models on code-switched tasks. — [Survey of Code-Switched NLP in the LLM era, arXiv 2510.07037](https://arxiv.org/html/2510.07037v5)
- **Request limits (Anthropic):** the Messages API maximum request size is 32 MB (413 if exceeded). Error codes:
  - 429 `rate_limit_error`. A spend-cap 429 has **no retry-after header** and keeps failing.
  - 500 `api_error`: retry with backoff.
  - 504 timeout: use streaming.
  - **529 `overloaded_error`** under global high traffic.
  - Sharp usage increases can trigger "acceleration limits" (429), so ramp traffic gradually.

  The SDK retries transient errors **twice by default** with exponential backoff, honouring `retry-after`. Streaming responses can fail **after** a 200 with mid-stream error events. — [Anthropic API errors](https://platform.claude.com/docs/en/api/errors)
- `pattern_internal_error` in Rasa has specific messages for input that is too long or empty. — [Rasa patterns](https://rasa.com/docs/reference/primitives/patterns/)
- **Returning users and cache expiry.** Anthropic caches last 5 min (or 1 h). OpenAI GPT-5.6+ caches last 30 min by default (or 12 h); earlier OpenAI models keep in-memory caches for ~5–10 min of inactivity. Gemini implicit caching works only for "similar prefix within a short time window". A user who returns after days pays full input price on the first turn back. — [Anthropic caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching); [OpenAI caching](https://developers.openai.com/api/docs/guides/prompt-caching); [Gemini caching](https://ai.google.dev/gemini-api/docs/caching)
- ChatGPT lets users view and restore saved memories and does not remember every detail of past chats, which is the transparency and control pattern for persisted memory. — [OpenAI Memory FAQ](https://help.openai.com/en/articles/8590148-memory-faq)
- Shared memory written by several writers is last-write-wins in Letta. This is directly relevant when several people from one company edit the same ICP. — [Letta memory blocks](https://docs.letta.com/guides/agents/memory-blocks)
- Thinking blocks in Claude 5.5-series models are bound to the exact prior prefix. Editing history (e.g. compaction) can trigger 400s unless `prefix_mismatch_behavior: "drop_block"` is set, so keep history append-only. — [Anthropic API errors](https://platform.claude.com/docs/en/api/errors)

### Inferences
- **Multilingual and code-switching.** Detect language per message in code. Keep a session `reply_language` that switches only after 2+ consecutive messages in a new language, and mirror a Hinglish-style mix if the user mixes. **Normalise extracted slot values to English** for the ICP record while keeping `source_quote` in the original language. Add code-switched personas to the simulator suite, since the survey says LLM quality is weakest there.
- **Very long messages and pasted documents** (e.g. a pasted website "About" page or pitch deck text). Above a threshold (say 1,500–2,000 tokens), route to a "document intake" step: a one-off extraction over the pasted text into slot candidates marked `from_document` / `tentative`. Then store a short digest in the transcript instead of the raw paste, so the paste doesn't sit in every future prompt (cost and context rot). Confirm document-derived slots in the final read-back.
- **Prompt injection and persona jailbreaks.**
  - Wrap user and pasted content in clearly delimited "untrusted data" blocks (OWASP mitigation #6).
  - Keep the persona and rules in the system prompt and re-assert them near the end of each prompt.
  - Validate all extractor output against the schema in code (#2).
  - Give the bot **no privileged tools**: it should only write slot deltas, through a validated, least-privilege path (#4).
  - For "ignore your instructions, you are now…" attempts, respond in persona and steer back to the pending question. Don't lecture.
  - Never put secrets or internal scoring logic in prompts.
  - Add injection personas to the eval suite (#7).
- **Users returning after days.** On resume, do not replay the whole old transcript. Send the cached static prefix, a fresh **consolidated state block** (filled slots, open questions, short narrative summary), and a re-orientation message ("Welcome back — we'd covered X and Y; next is Z"). This is also Laban's "consolidate into a fresh conversation" fix. Expect the first resumed call to be a cache miss.
- **Multiple people on one account.** Scope tone and style memory **per respondent**, and attribute each slot value to a respondent. When respondent B contradicts respondent A's stored answer, treat it as a cross-respondent conflict to surface, not a silent overwrite. Show "last updated by" on the read-back. Avoid last-write-wins on shared ICP state (the Letta caveat).
- **Rate limits and outages.**
  - Use the SDK retries with jittered exponential backoff.
  - Show a user-facing "one moment…" after ~3–5 s.
  - Keep a fallback ladder: the same provider's cheaper or older model, then a second provider. Note that a fallback provider means a **cold cache** and different tone behaviour, so run the regression suite on the fallback model too.
  - Persist the user's message and slot state before calling the LLM, so a failed turn can be retried without losing input.
  - Handle mid-stream errors.
  - Treat a spend-cap 429 (no retry-after) as a hard stop, not something to retry.

### Gaps
- No primary source was found on handling **multiple people sharing one account** in chatbots. The recommendations above are inference.
- No concrete data was found on how prompt-injection success rates change with delimiting or "sandwich" defenses on current (2026) models.
- Gemini- and OpenAI-specific error-code and overload semantics were not fetched; only Anthropic's error docs were read.
- Rate-limit tiers (RPM/TPM) for any provider were not collected.
