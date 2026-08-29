# Recap Thinking-Budget & Token-Usage Plan

Follow-up plan for the production Session Recap failure: a "session_recap" background job
(model `gemma4:26b`) failed after 365 minutes with generate_chat's own diagnostic —
*"empty response … it produced 7781 character(s) of hidden "thinking" output but no final
answer"* (`app/ai.py:447-453`). Commit f57db01 (already on main; test suite at that point:
1865 passed, 2 skipped) added `_thinking_num_predict_override()` and the per-chunk thinking
headroom in `_transcript_chunk_char_budget()`, wired into `expand_recap_notes`,
`summarize_session_from_facts`, and both `summarize_transcript` call sites. **Nothing in
this plan re-proposes any of that** — this is what's LEFT: the remaining budget gaps
(section 1), GM recovery from this exact failure (section 1), and a separately requested
token-usage optimization pass over every Ollama/Whisper caller (section 2).

Each item names concrete files/functions and a rough size (Small/Medium/Large). See
"Suggested execution order" at the end for the implementation sequence; sections are
grouped by topic, not priority.

Status legend as waves are executed: ✅ done · 🚧 in progress · ⬜ not started.

---

## 0. Findings summary — confirm/refute per investigated angle

Recorded up front so the implementer doesn't have to re-derive any of it:

- **`condense_recap` num_predict gap: CONFIRMED real.** `app/ai.py:806-808` — only
  `if max_tokens and not think` ever touches `num_predict`; with `think=True` the GM's
  instance-wide "Max output tokens" (`effective_ollama_options()["num_predict"]`, merged
  into every call by `_chat_kwargs`, `app/ai.py:133-140`) applies **un-widened**. Every
  other member of the recap family already passes `_thinking_num_predict_override(think)`
  (`app/ai.py:709-712`, `735-738`, `1191`). Its own docstring (`app/ai.py:777-789`)
  describes the exact risk but only removes the *max_tokens* cap, not the global one.
  → Item 1.1.
- **Headroom calibration: 4096 is adequate; don't raise the default.** The failed job's
  7781 thinking chars ≈ 1,945 tokens at the 4-chars/token English estimate
  (`app/ai.py:916`) or ≈ 3,890 at the 2-chars/token dense-script estimate
  (`app/ai.py:930-934`) — both under 4096. The production failure predates f57db01; the
  shipped headroom would have absorbed the observed thinking length. Raising the default
  instead over-chunks: under the unconfigured `_DEFAULT_ASSUMED_CTX_TOKENS = 4096` the
  chunk budget already sits on its 500-token floor (`app/ai.py:960-965`,
  `tests/test_transcript_chunking.py:92-103`), and at num_ctx=8192 a doubled headroom
  would collapse per-chunk input from ~2,900 tokens to that same floor — ~6× more AI
  calls per transcript. → Item 1.5 (env-tunable escape hatch, no default change, no new
  Settings field).
- **AI Chat / Ask AI: NO Thinking toggle, so no new exposure — with one residual hole.**
  `app/routers/ai.py` contains zero `think` occurrences; `ChatBody`
  (`app/routers/ai.py:168-180`) has no think field; `stream_chat` (`app/ai.py:476-491`)
  doesn't even accept one — every interactive surface runs `_chat_kwargs`'s think=False.
  BUT: a model that *ignores* think=False (the documented reason generate_chat's
  diagnostic exists at all, `app/ai.py:432-439`) produces a **completely silent** blank
  reply on the streaming path — stream_chat yields only non-empty `content` tokens and
  has no empty-stream diagnostic. → Item 1.4.
- **Auto-retry with think=False: safe and worth defaulting on.** The job has already
  failed and produced nothing, so a non-thinking fallback is strictly better; it fits the
  codebase's automation posture (`resume_interrupted_jobs`, `app/audio_jobs.py:968-979`:
  "a routine `git pull` … shouldn't require a human"). Hook: `_run_job`'s two
  `_looks_like_failure` sites (`app/audio_jobs.py:702-703` condense, `719-721`
  session_recap). → Item 1.2.
- **One-click "Retry without Thinking": straightforward.** The Retry-summary panel
  (`app/templates/background_jobs.html:228-283`) already has everything needed —
  `bgResummarize(jobId, model, instructions, think, btn)` at `:410` posts
  `POST /api/audio-jobs/{id}/resummarize` with `think=false`
  (`app/routers/audio_jobs.py:174-203`). Only detection + a button are missing.
  → Item 1.3.
- **done_reason / diagnosability: mostly REFUTED as a gap.** `done_reason` and
  `eval_count` are already logged on every empty response (`app/ai.py:443-446`) and
  `done_reason` is in the non-thinking sentinel (`app/ai.py:454-455`); the thinking
  sentinel deliberately omits it and a test pins that
  (`tests/test_ollama_options.py:404` asserts `"done_reason=length" not in result`) — do
  NOT add it there. The one real gap: the log line records `had_thinking` as a bool, not
  the thinking length, which is exactly what a GM would need to calibrate headroom across
  several failed jobs. Folded into item 1.5. Settings has no hint about the
  thinking/num_predict interaction (`app/templates/settings.html:227-230`) → item 1.6.
- **Other think=True call sites are already protected.** The player session-log recap
  (`app/routers/sessions.py:1027`) calls `summarize_session_from_facts` without `think`
  (→ defaults True, no player-facing toggle) but that function is f57db01-wired; the
  Chronicler (`app/routers/chronicler.py:99`), MCP (`app/mcp_server.py:227`), chat jobs
  (`app/chat_jobs.py:120`), and lore_extras streaming (`app/routers/lore_extras.py:281`)
  are all think=False callers. No further num_predict wiring needed anywhere.

---

## 1. Thinking-budget gaps and recovery

**1.1 Widen a GM-configured num_predict inside `condense_recap` when think=True. (Small; the core remaining gap)**
`app/ai.py:750-812`. After `opts = dict(options) if options else {}` (`:806`) and the
existing `if max_tokens and not think: opts["num_predict"] = max_tokens` (`:807-808`),
add `opts.update(_thinking_num_predict_override(think))` — the helper already returns
`{}` for think=False or an unconfigured/unlimited cap (`app/ai.py:848-853`), so one
unconditional update after the existing branch is the whole change. No key collision:
the only `options` callers ever pass is `condense_call_options(...)`, which returns only
`num_ctx` (`app/ai.py:1020-1024`). Existing semantics preserved: think=False+max_tokens
keeps the hard cap (`tests/test_ollama_options.py:138-144`, `164-168` unchanged);
`test_condense_recap_max_tokens_is_prompt_only_when_thinking` (`:148-158`) still passes
because the autouse `_reset_ollama_overrides` fixture leaves the instance options empty,
so the override contributes `{}` there.

Two required companion edits:
- `condense_call_options`'s num_ctx headroom gate (`app/ai.py:1015`:
  `think and max_tokens`) is justified by "condense_recap never touches num_predict
  when max_tokens isn't set" (`:998-1000`) — no longer true after this fix. Extend the
  gate to `think and (max_tokens or effective_ollama_options().get("num_predict"))` so a
  thinking condense bounded only by the (now widened) global cap also gets matching
  num_ctx room, and rewrite that docstring paragraph plus `condense_recap`'s own
  (`:777-789`) to describe the new behavior.
- Tests (in `tests/test_ollama_options.py`, `_FakeChatClient` + monkeypatched
  `effective_ollama_options` style, matching `tests/test_transcript_chunking.py:185-213`):
  (a) think=True + `{"num_predict": 512}` configured → `calls[0]["options"]["num_predict"]
  == 512 + ai_module._THINKING_HEADROOM_TOKENS`; (b) same but with `max_tokens=150` →
  still the widened value, never 150; (c) think=False + max_tokens=150 + configured 512 →
  exactly 150 (hard cap wins, unchanged); (d) `condense_call_options` widens num_ctx for
  think=True with a configured num_predict and no max_tokens (mirror of
  `test_condense_call_options_widens_further_for_thinking_plus_max_tokens`, `:272-281`).

Tradeoff: a thinking Condense can now generate up to 4096 tokens past the GM's configured
cap — the identical, already-accepted tradeoff f57db01 made for the other three recap
functions.

**1.2 Auto-retry a thinking-starved job once with think=False. (Medium)**
Two parts:

*Detection helper* — `app/ai.py`, next to `is_failure_sentinel` (`:464-473`):
`is_thinking_starved_sentinel(result: str) -> bool`, true iff
`result.startswith("[empty response")` and `'hidden "thinking"' in result` — the stable
phrase the sentinel emits (`app/ai.py:448-453`; `tests/test_ollama_options.py:393-404`
pins "hidden"+"thinking" presence). Keeping the string knowledge in `app/ai.py` means the
job engine and the UI item (1.3) never duplicate it. Note it must NOT match the
whitespace-only-part sentinel (`app/ai.py:1238`) or the `done_reason` variant (`:454-455`)
— neither contains `hidden "thinking"`.

*Engine hook* — `app/audio_jobs.py:_run_job`, both branches. Replace each single
summarize/condense call with a two-attempt loop, entirely inside the existing
`async with _ai_module.ollama_job_semaphore` block (`:696`, `:712` — the semaphore's whole
point is that one job's calls run back-to-back, `app/ai.py:20-30`):

- attempts = `[True, False]` when the job's `think` is True, `[False]` otherwise — so a
  job the GM explicitly ran with Thinking off never retries, and the fallback attempt can
  never itself loop (a second starvation, i.e. a model ignoring think=False, fails
  normally).
- After a first attempt where `think` was True and
  `_ai_module.is_thinking_starved_sentinel(recap)`: log a warning, `_set(think=False,
  think_fallback=True)`, and run the second attempt with `think=False` and **no resume
  checkpoint** (a think flip changes `chunk_chars` via the headroom, so
  `summarize_transcript` would discard the checkpoint anyway — `app/ai.py:1205-1215`).
  For the condense branch, recompute `options` via
  `condense_call_options(..., think=False, ...)` for the retry so num_ctx matches the
  non-thinking call. Reuse the already-computed `world_context` string — no RAG re-query.
- Any other failure sentinel, or a starved result on a think=False attempt, exits the
  loop into the existing error handling unchanged. `JobInterrupted`/`CancelledError`
  propagate out of the loop exactly as today.

*Persistence/surface*: new nullable column `AudioJob.think_fallback = Column(Boolean, default=False)`
in `app/models.py` (next to `think`, `:873`) — heals automatically on existing installs
via `_heal_table_from_model` ("audio_jobs" is already in the healed list,
`app/database.py:861-867`). Setting `job.think = False` also makes the Retry-summary
checkbox reflect what actually produced the recap
(`app/templates/background_jobs.html:259` reads `job.think !== false`). Add
`"think_fallback"` to `app/routers/audio_jobs.py:_job_to_dict` (`:36-61`) and render a
dim `bg-job-note-text` line on done cards when set — e.g. *"Thinking ran out of output
budget on the first pass — this recap was written with Thinking off."*

Default vs opt-in: **default on, no new setting.** A failed job produced nothing, so the
quality tradeoff is one-sided; cost is bounded at one extra pass (worst case ~2× wall
clock for the summarize phase — still far better than the observed 365-minute job ending
with nothing and waiting for a human).

Tests (`tests/test_audio_jobs.py` conventions — autouse `_fake_ai` fixture at `:73-87`,
`_await_terminal` at `:99-110`):
- fake `summarize_transcript` that returns the real-shaped starved sentinel when its
  `think` kwarg is truthy and a real recap when False → job ends `done`, recap set,
  `job.think is False`, `job.think_fallback is True`, exactly 2 calls;
- job created with `think=False` + always-starved fake → `error`, 1 call;
- `"[AI error: …]"` with think=True → `error`, 1 call (no retry on non-starved failures);
- the same done/error pair for `purpose="condense"` via `fake_condense`;
- unit tests for `is_thinking_starved_sentinel` in `tests/test_transcript_chunking.py`
  (true for the full production-shaped message, false for `"[AI error: …]"`, the
  done_reason variant, the whitespace-part sentinel, and `""`).

**1.3 One-click "Retry without Thinking" on the Background Jobs page. (Small)**
Server: add `"thinking_starved": _ai_module.is_thinking_starved_sentinel(job.error or "")`
to `_job_to_dict` in `app/routers/audio_jobs.py:36-61` (and optionally to
`app/routers/sessions.py:_job_to_dict`, `:734-746`, for the Sessions page's inline
panel). Client string-matching would duplicate sentinel knowledge — don't.

UI: `app/templates/background_jobs.html`, inside the existing
`job.purpose === 'session_recap' && job.transcript` retry block (`:228-283`): when
`job.status === 'error' && job.thinking_starved`, (a) render a visually distinct button
("🧠✕ Retry without Thinking") whose onclick is exactly
`bgResummarize(job.id, job.model || '', job.extra_instructions || '', false, btn)`
(`:410-435` — no new endpoint; the resummarize route already accepts `think=false`,
`app/routers/audio_jobs.py:174-203`), and (b) pre-uncheck the existing `thinkCheckbox`
(`:257-259`) so the manual path defaults sensibly too.

Known limitation to state in code comments: a failed `purpose="condense"` job has no
retry control on this page at all (`start_resummarize_job` rejects non-session_recap,
`app/audio_jobs.py:829-830`) — item 1.2 covers condense at the engine level; extending
resummarize to condense is deliberately out of scope here.

Value after 1.2 ships: covers rows that failed *before* 1.2 exists, and a GM who
re-enables Thinking on a retry and hits it again. Tests: route-level assertion that
`thinking_starved` is True for a job whose `error` is the real sentinel and False
otherwise (`tests/test_audio_jobs.py` or `tests/test_background_jobs_unified.py`);
template-source assertion that background_jobs.html wires the button (same JS-source
style as `tests/test_live_recording_wake_lock.py`).

**1.4 Give `stream_chat` the empty-content diagnostic `generate_chat` already has. (Small)**
`app/ai.py:476-491`. Track `yielded_any` and accumulate
`len(getattr(chunk.message, "thinking", None) or "")` across the stream; after a clean
loop that yielded nothing, yield one sentinel using the same wording family: the
thinking variant when thinking chars were seen, else the generic
"[empty response from {m} … check the Ollama server logs]" one. The SSE route
(`app/routers/ai.py:793-819`) and `app/routers/lore_extras.py:281` pass tokens through
verbatim, so the sentinel renders as the reply text — the same convention the
non-streaming path already has, replacing today's completely blank reply when a model
ignores think=False. Tests: `tests/test_ai_stream.py` / `tests/test_ollama_options.py`
style — a fake client whose stream yields chunks with `content=""` and `thinking="…"` →
the collected tokens equal exactly one sentinel; a normal stream is unchanged
(`test_stream_chat_passes_options`, `tests/test_ollama_options.py:407-414`, keeps
passing).

**1.5 Headroom: keep 4096, make it env-tunable, log the thinking length. (Small)**
Per the section-0 analysis: do not change the default, do not add a Settings field, do
not build adaptive per-model heuristics (there's no observed-thinking-length bookkeeping
to base one on, and the bookkeeping isn't worth building for this). Changes:
- `app/ai.py:830`: `_THINKING_HEADROOM_TOKENS = max(0, int(os.getenv("THINKING_HEADROOM_TOKENS", "4096")))`
  — same env-tunable idiom as `WHISPER_JOB_CONCURRENCY` (`app/ai.py:31`); extend the
  constant's comment with the calibration reasoning above (bigger = smaller chunks =
  more calls, floor behavior under the assumed 4096 default ctx).
- `app/ai.py:443-446`: include `len(thinking)` in the existing `_log.warning` (it
  currently logs only `had_thinking=bool`) so repeated starvation across jobs is
  calibratable from logs. Do NOT touch the sentinel text (see section 0 —
  `tests/test_ollama_options.py:404` pins its shape).
- Existing tests all reference `ai_module._THINKING_HEADROOM_TOKENS` rather than a
  literal (`tests/test_transcript_chunking.py:100-102, 188, 206`), so they survive
  unchanged; no new env-parsing test needed beyond, at most, asserting the constant is a
  non-negative int.

**1.6 Settings/UI hints for the thinking ↔ num_predict interaction. (Small; text-only)**
- `app/templates/settings.html:227-230`: add a dim help line under "Max output tokens":
  *"Recap/Condense actions run with Thinking on temporarily widen this cap (by 4096
  tokens by default) so hidden reasoning can't starve the visible answer."*
- `app/templates/sessions/detail.html:86-90`: the Condense "Max tokens" tooltip still
  says "Hard cap … (Ollama's num_predict)" — since f57db01 that's only true with
  Thinking off; append "(enforced only when 🧠 Thinking is off; with Thinking on it's a
  soft target)". The Thinking checkbox tooltip (`:56-59`) already warns about
  tight-context models — leave it.
- Tests optional (template-content assertions à la `tests/test_accessibility_sweep.py`);
  fine to skip for pure help text.

---

## 2. Token-usage optimization (Ollama + Whisper)

Requested as a distinct pass: reduce wasted/redundant tokens sent to or generated by the
models, beyond the thinking-headroom work above. Findings first, then items.

Confirmed-good patterns that must be **preserved** (guidance, no action): (a)
`summarize_transcript`'s chunked path already maximizes Ollama KV-prefix reuse — the part
system prompt is computed once and sent **identically** for every chunk
(`app/ai.py:1185-1201`), and `ollama_job_semaphore` holds the entire loop
(`app/audio_jobs.py:712`) so chunks run back-to-back with nothing interleaving to evict
the cached prefix; do not add per-part variation ("part 3 of 7") to that system prompt.
(b) No retry path ever re-transcribes: `start_resummarize_job` requires and reuses
`job.transcript` (`app/audio_jobs.py:831-832`), `_run_job` skips transcription whenever a
transcript exists (`:610`), and mid-transcription failures save chunk-level progress
(`WhisperError.partial_transcript`, `app/audio_jobs.py:628-637`; checkpoints,
`app/ai.py` transcribe contract) — item 1.2's auto-retry inherits all of this. (c)
Per-chunk repetition of recap_instructions/world_context is deliberate and already
budgeted (`_transcript_chunk_char_budget` counts the system prompt, `app/ai.py:962`;
each part summary is final as written, `tests/test_transcript_chunking.py:376-393`). (d)
Attachment audio is transcribed once at upload and reused as `att.text` thereafter
(`app/routers/ai.py:277-283`). (e) `_collapse_repeated_transcript_lines`
(`app/ai.py:1429-1456`) already strips degenerate Whisper repetition before any Ollama
call sees the transcript.

**2.1 AI Chat injects the fresh RAG block at the FRONT of every send — defeating Ollama's prefix cache for the whole conversation. (Medium; biggest interactive win)**
Confirmed: `static/js/ai-chat-core.js:939-976` (`buildChatMessagesWithContext`, shared by
the live send AND the background chat-job send per its own comment at `:931-938`) fetches
fresh `/api/ai/world-context-smart` for the newest message every send and assembles
`[{user: "Relevant world lore:\n\n"+ctx}, {assistant:"Got it."}, ...history]`
(`:967-972`), with pinned entities' full bodies prepended into ctx (`:904-915`,
`:962-965`). Because the lore block differs turn to turn, the token stream diverges right
after the system prompt, so Ollama must re-prefill the ENTIRE history on every turn —
prompt-eval work grows quadratically over a conversation, for zero informational gain.

Change: inject the transient lore pair immediately **before the final user turn** instead
(`[...base.slice(0,-1), loreUser, gotIt, base[base.length-1]]`), or fold ctx into the
last user message's content. System + all prior turns become a byte-stable prefix; the KV
cache then covers the whole conversation and only lore + the new turn get prefilled. Same
tokens in-window; large prefill saving; arguably better grounding (recency). The lore
block is transient (never pushed into `history`), so nothing persisted changes.
Tradeoff: prompt order changes model outputs slightly. Tests: JS-source assertions
(`tests/test_ai_chat_split.py` / wake-lock style) that lore injection follows history;
server-side chat-job tests are unaffected (assembly is client-side).

Refuted adjacent ideas: attachment text (12,000-char cap,
`app/routers/ai.py:49,272-278`) and base64 images re-sent per turn are prefix-STABLE, so
after this change the KV cache absorbs their recompute — eliding them from old turns
would change model-visible history for marginal window savings; skip. `history` is
unbounded (no slicing anywhere in `static/js/`) — real context pressure on very long
chats, but trimming loses conversational memory; out of scope, noted only.

**2.2 Chronicler sends up to 200 facts on every question, with no context sizing. (Small–Medium)**
Confirmed: `app/routers/chronicler.py:37-44` (`visible_facts`, `.limit(200)`) puts every
recent fact — thousands of tokens on a mature campaign — into the system prompt of every
ask (`:56-65`), plus 15 entities with body excerpts (up to 8,000 excerpt chars,
`app/retrieval.py:34-36,120-149`), and `generate_chat` is called with no `options`
(`:99`) — no `context_sized_options` anywhere in the file, so under the common ~4096
default context this can silently truncate (the exact garbage-output failure
`condense_call_options`'s docstring documents, `app/ai.py:984-989`).

Change: (a) mechanical and safe — pass
`options=_ai_module.context_sized_options(system + question)` at
`app/routers/chronicler.py:99`; (b) trim volume — reduce the always-include fact cap
(e.g. a `_CHRONICLER_FACT_LIMIT = 60` constant replacing the inline 200) and/or add a
cheap keyword filter over fact content mirroring `find_relevant_entities`' word matching,
keeping newest-first order. (b) trades "the Chronicler has seen everything recorded" for
tokens — implement (a) unconditionally, (b) as a constant with the tradeoff documented in
`visible_facts`' docstring. Tests: `tests/test_chronicler.py` — monkeypatch
`ai_module.generate_chat` capturing `options`, seed many facts, assert num_ctx scales
with the built prompt; assert the prompt contains at most the capped number of fact lines.

**2.3 Player session-log recap: 20-second TTL forces near-constant regeneration although exact invalidation already exists. (Small; large compute/token save)**
Confirmed: `app/routers/sessions.py:983` (`_SESSION_LOG_RECAP_CACHE_TTL = 20.0`) — but
`app/routers/facts.py:52,69,80,130` already clears the cache on every fact
create/edit/delete, and facts are the recap's only content input
(`app/routers/sessions.py:1019-1027`). So a browsed session-log page re-runs a full
`summarize_session_from_facts` — with think defaulting to True (`app/ai.py:724`; the
call at `sessions.py:1027` passes no `think`), i.e. paying thinking tokens too — as
often as every 20s per `(session_id, is_gm)` for identical input.

Change: raise the TTL to 600–3600s; residual staleness is bounded to non-fact inputs
(World.recap_instructions edits, the "recap" surface default model) — close the biggest
by also calling `clear_session_log_recap_cache()` from
`POST /api/ai/recap-instructions` (`app/routers/ai.py:1152`). Optionally pass
`think=False` at `sessions.py:1027` for a further fixed saving; recommendation: keep
think=True (quality on a player-facing page) and let the cache do the work — record the
option and tradeoff in a comment. Tests: extend `tests/test_ai_answer_caching.py`
(this cache's existing home) with the invalidation-on-instructions-save case.

**2.4 Blocking summarize-from-audio: a failed summary currently costs a full Whisper re-run to retry. (Medium)**
Background jobs are clean (see the preserved-patterns list). The BLOCKING routes are not:
`POST /api/sessions/ai/summarize-from-audio` (`app/routers/sessions.py:636-666`) and
`…/complete` (`:684-718`) transcribe to a temp file, summarize, return
`{transcript, recap}`, and delete the audio. When the summarize step returns a failure
sentinel, the client treats any non-empty `recap` as the draft (`aiRunRecap`,
`app/templates/sessions/detail.html:297-321` — the sentinel is even offered as applyable
text), and the only retry is re-uploading and re-transcribing the entire recording —
hours of Whisper compute to redo a step that already succeeded.

Change: (a) new GM-only route `POST /api/sessions/ai/summarize-transcript` taking
`{transcript, extra_instructions, think}` and calling
`summarize_transcript(transcript, model=_recap_model(""), extra_instructions=_combine_recap_instructions(_recap_instructions_for_world(world), extra), think=…)`
— mirror `api_summarize_live_transcript`'s shape (`app/routers/sessions.py:905-925`);
(b) have the two blocking routes include `recap_failed: _ai_module.is_failure_sentinel(recap)`
in their JSON (server-side detection — no JS string matching); (c) client-side, keep the
returned transcript (already displayed via `_setRecapTranscript`,
`sessions/detail.html:268-273`) and when `recap_failed` show "Retry summary from this
transcript" wired to the new route instead of offering the sentinel as a draft.
Tradeoff: one new route + client wiring; also fixes the sentinel-as-draft paper cut.
Tests: route tests with monkeypatched `ai_module.summarize_transcript`
(`tests/test_recap_instructions.py` / `tests/test_session_recap_ai.py` conventions).

**2.5 Whisper glossary: cap by characters, not just name count. (Small)**
Partially refuted, one real fix: re-sending the glossary as `initial_prompt` for every
audio chunk is REQUIRED for the bias to persist (`carry_initial_prompt`,
`app/ai.py:1648-1665`, documented at `:1716-1721`) and is negligible next to audio
compute — not waste. The real issue: whisper.cpp's prompt window is a fixed small token
budget (~224 tokens), and `_merge_glossary` (`app/audio_jobs.py:276-290`) bounds the
entity-name count (`GLOSSARY_ENTITY_LIMIT = 50`, `:251`) but not total length — a long
GM-typed glossary plus 50 names overflows it and whisper silently truncates the tail
(the entity names; GM terms are correctly ordered first). Change: add a char budget to
`_merge_glossary` (e.g. stop appending entity terms past ~600 total chars, whole-term
granularity, GM text never trimmed), log how many entity names were dropped, and surface
"N included / M dropped" via the existing `GET /api/ai/whisper/glossary` route (see
`entity_glossary_terms`' docstring, `app/audio_jobs.py:254-261`). Tests: pure-function
tests on `_merge_glossary`.

**2.6 Bound the output of the fixed micro-calls. (Small)**
Confirmed uncapped: `benchmark_model` (`app/ai.py:325-342` — a "two-sentence" prompt) and
`GET /api/ai/test-chat` (`app/routers/ai.py:1972-1981` — "Say only the word OK.") pass no
per-call options; a chatty model can generate paragraphs on someone's benchmark/health
check. Add `options={"num_predict": 128}` to the benchmark's `.chat()` call (also makes
cross-model timings comparable) and `options={"num_predict": 16}` to test-chat's
`generate_chat` call. REFUTED for the JSON-schema calls (`parse_facts_from_recap`,
`parse_entity_from_text`, `generate_session_prep`): their output length is genuinely
input-dependent and a cap risks truncated JSON that fails parsing — worse than a few
padded tokens. Tests: `tests/test_benchmark.py` — assert the fake client saw
`options["num_predict"]`.

---

## Suggested execution order

Each wave is independently shippable by an implementer with no memory of this research —
every item above carries its own file/function references and test plan.

**Wave 1 — the core gap + cheap high-value wins: ✅ done**
1.1 (condense num_predict widening) · 1.5 (env-tunable headroom + thinking-length log) ·
2.3 (session-log cache TTL) · 2.6 (micro-call caps)

**Wave 2 — recovery from this exact failure: ✅ done**
1.2 (auto-retry with think=False, incl. `is_thinking_starved_sentinel` +
`AudioJob.think_fallback`) · 1.3 (one-click Retry without Thinking — depends on 1.2's
helper) · 1.6 (Settings/tooltip text)

**Wave 3 — interactive surfaces: ✅ done**
1.4 (stream_chat diagnostic) · 2.1 (AI Chat lore-injection reorder) · 2.2 (Chronicler
context sizing + fact cap)

**Wave 4 — Whisper-side and blocking-route recovery: ✅ done**
2.4 (summarize-from-transcript retry route) · 2.5 (glossary char budget, ended up renaming
`_merge_glossary` to public `merge_glossary` since GET /whisper/glossary now calls it too) ·
comment pass recording section 2's "preserve these" guidance next to the relevant code
(`summarize_transcript`'s part-system computation, `merge_glossary`).

All four waves shipped. Full suite: 1903 passed, 2 skipped (was 1865 passed, 2 skipped
before this plan; 1890 before this final wave).
