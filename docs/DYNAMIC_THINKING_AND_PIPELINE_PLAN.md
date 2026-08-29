# Dynamic Thinking Budget & Session/Audio/AI Pipeline Plan

Follow-up to `docs/ENHANCEMENT_PLAN.md` (fully executed) and
`docs/RECAP_THINKING_BUDGET_PLAN.md` (fully executed). **Nothing in this plan
re-proposes anything already shipped by those two** — the fixed
`_THINKING_HEADROOM_TOKENS` widening, the two-attempt think=False auto-retry,
`is_thinking_starved_sentinel`, the stream_chat empty-stream diagnostic, the
AI-Chat lore-injection reorder, the Chronicler fact cap/context sizing, the
glossary char budget, the wake lock, and the summarize-transcript retry route
all exist and are referenced below as the baseline being built on.

Trigger: despite that prior work, a production Session Recap background job
(model `gemma4:26b`) failed again after 11m54s with the thinking-starved
sentinel — *"it produced 8036 character(s) of hidden 'thinking' output but no
final answer"*. Part 1 diagnoses why the fixed-headroom design still fails and
replaces it with a progressive, effectively-unbounded budget ladder. Part 2
is a fresh audit of the whole session/audio/AI pipeline.

Each item names concrete files/functions/line references (line numbers as of
this writing) and a rough size (Small/Medium/Large). Status legend as waves
are executed: ✅ done · 🚧 in progress · ⬜ not started.

---

## 0. Findings summary — confirm/refute per investigated angle

Recorded up front so the implementer doesn't have to re-derive any of it.

**Why today's job still starved — the fallback rung is the WEAKEST rung.**
`app/audio_jobs.py:_run_job` runs `attempt_think_values = [True, False]`
(`:722`). Attempt 1 (think=True) gets the widened cap
(`configured num_predict + 4096`, `app/ai.py:933-953`). But when it starves,
attempt 2 runs with think=False, and `_thinking_num_predict_override(False)`
returns `{}` (`app/ai.py:948-949`) — the GM's configured `num_predict`
applies **un-widened**. A model that ignores think=False (the documented
reason the sentinel exists at all — `app/ai.py:455-462`) therefore retries
into a *smaller* budget than the attempt that just failed. The observed
number fits this exactly: 8036 chars ≈ 3.9 chars/token × a 2048-token
unwidened cap — i.e. the error the GM saw most plausibly came from the
**fallback** attempt hitting the raw configured cap (the implementer can
confirm from logs: `generate_chat` logs `thinking_chars=` per empty response,
`app/ai.py:469-473`, once per attempt). Either way the structural conclusion
holds: any fixed headroom is a guess, and the recovery path must end at a
rung with genuinely more room, not less. → Items 1.1/1.2.

**num_predict is not the only starvation mechanism — num_ctx is too.**
With `num_predict` unset, generation is bounded by remaining context. The
chunked summarize path reserves headroom out of `num_ctx`
(`_transcript_chunk_char_budget`, `app/ai.py:1037-1065`) but that arithmetic
uses the GM's *configured* num_ctx or an assumed 4096 — if the model's real
default context is smaller, or the system-prompt estimate is off (next
finding), generation room silently shrinks. Both mechanisms produce the
identical sentinel, so the expanded rung must widen **both** knobs, and must
send `num_ctx` explicitly (removing the "we can't know the real default"
uncertainty for that one call). → Item 1.1.

**Real bug: the system prompt's token estimate ignores dense scripts.**
`_transcript_chunk_char_budget` (`app/ai.py:1062`) computes
`system_tokens = len(system) // _CHARS_PER_TOKEN_ESTIMATE` — a fixed 4
chars/token — while the *transcript* estimate correctly drops to 2 for
non-ASCII text (`_chars_per_token_estimate`, `:1021-1034`). The system prompt
contains `extra_instructions` and RAG `world_context`, which for exactly the
Russian-language worlds this app documents supporting (`:1192-1201`) can be
mostly Cyrillic — the estimate then undercounts system tokens by ~2×,
over-allocating input and squeezing generation room. → Item 1.5.

**`num_predict = -1`: considered and rejected for the expanded rung.**
Ollama treats -1 as "generate until natural stop or context exhausted" and
-2 as "fill remaining context". Since generation is context-bounded, -1 can
never truly run forever — but its *practical* ceiling depends on how the
running Ollama version behaves at context exhaustion (older versions
context-shift and keep generating; newer ones stop with
done_reason="length"), which is exactly the kind of version-dependent
behavior this codebase avoids relying on. A **large explicit cap** gives the
same headroom with a deterministic stop, so a job can never "hang with no
signal" longer than the cap allows — the UX concern that matters on a
homelab where cost doesn't. → Item 1.1 uses explicit numbers, never -1/-2.

**Interactive Ask AI with Thinking on has NO widening at all.** The entity
detail panel's checkbox (`app/templates/entities/detail.html:218-221`) sends
`ChatBody.think` (`app/routers/ai.py:187`) into `ai_stream` (`:800-833`),
which passes `_clamp_options(body.options)` straight to `stream_chat` — no
`_thinking_num_predict_override` anywhere on this path. A GM-configured
num_predict therefore applies un-widened to every thinking Ask AI reply; the
only defense is the (shipped) empty-stream sentinel. → Item 1.4.

**Auto-retrying a streaming SSE reply: rejected.** Tokens already sent to
the client can't be unsent; a mid-stream retry would need client-side
protocol changes for a rare case the widening in 1.4 mostly eliminates. The
sentinel text itself already tells the user what to do. No retry ladder for
`stream_chat`.

**The `think_fallback` job-card note never shipped.** `_job_to_dict` exposes
it (`app/routers/audio_jobs.py:66`) but `background_jobs.html` never renders
it — RECAP plan item 1.2's "dim note on done cards" was dropped somewhere.
Folded into item 1.3's UI work.

**Refuted / already-covered (do not build):**
- Orphaned chunk-upload staging: `uploads.sweep_stale_chunk_sessions`
  (`app/uploads.py:123-140`) sweeps abandoned sessions on the next upload;
  job audio has `sweep_orphaned_job_audio` (`app/audio_jobs.py:1101`). No gap.
- Fit-context "should it trigger automatically" — it already does: the
  non-forced path of `condense_call_options` returns a widened num_ctx
  whenever the computed need exceeds both the configured and assumed
  baseline (`app/ai.py:1129-1132`). The only remaining gap is the *upper*
  bound (item 3.3).
- Job concurrency/fairness: the two semaphores (`app/ai.py:31-34`) are held
  for whole logical calls, FIFO, and shared by audio+chat jobs; interactive
  routes bypass them deliberately (a GM actively waiting must not queue
  behind a batch job). Sound as designed.
- Chat/image jobs restarting (not resuming) after a restart is inherent —
  one opaque remote call, nothing to checkpoint. Correct as-is.

---

## Part 1: Dynamic thinking budget — the progressive ladder

**Chosen design** (one design, not a menu): keep attempt 1 exactly as it is
today (fast, honors the GM's configured cap approximately, works for the
overwhelming majority of runs), and replace the single think=False fallback
with a **ladder** that only climbs on the specific thinking-starved sentinel:

| rung | think | budget | resumes prior checkpoint? |
|------|-------|--------|---------------------------|
| 1 | job's own | normal (configured + `_THINKING_HEADROOM_TOKENS`) | yes (existing behavior) |
| 2 | job's own | **expanded** (explicit large num_predict + explicit enlarged num_ctx) | yes — same think ⇒ same chunking ⇒ checkpoint still valid |
| 3 (only for think=True jobs) | False | **expanded** | no (think flip changes chunking) |

Rung 3 keeps the expanded budget precisely because the observed failure mode
is a model that ignores think=False — giving the last-resort rung the
*smallest* budget (today's behavior) is the bug. A job started with Thinking
off gets rungs 1–2 only (its think value never flips against the GM's
explicit choice). Latency cost is paid only after an attempt that produced
nothing usable, so the tradeoff is one-sided; worst case is bounded at 3
passes and every rung's generation is bounded by an explicit num_predict.

**1.1 The "expanded" budget primitives in `app/ai.py`. (Medium; everything else builds on this)**

- New constant next to `_THINKING_HEADROOM_TOKENS` (`app/ai.py:930`):

  ```python
  _THINKING_EXPANDED_HEADROOM_TOKENS = max(
      _THINKING_HEADROOM_TOKENS,
      int(os.getenv("THINKING_EXPANDED_HEADROOM_TOKENS", "12288")),
  )
  ```

  12288 ≈ 3× the normal headroom ≈ 24k–48k chars of thinking depending on
  script — 3–6× the worst observed failure. Env-tunable, same idiom as
  `THINKING_HEADROOM_TOKENS`; **no Settings field** (same reasoning RECAP
  plan 1.5 recorded: this is an escape hatch, not a knob to tune per run).
  Comment must note the KV-cache cost: the expanded rung's num_ctx bump adds
  roughly 8k tokens of KV (≈1.5–2.5 GB at f16 on a ~26B model) — Ollama
  offloads to RAM if VRAM is short, so the rung runs slower rather than
  failing; that is the accepted price of a recovery rung.

- New public helper (public — `app/audio_jobs.py` and `app/routers/ai.py`
  both call it; same promotion reasoning as `merge_glossary`):

  ```python
  def expanded_thinking_options() -> dict:
      """Options for a retry after is_thinking_starved_sentinel: a large
      EXPLICIT num_predict (never -1 — its behavior at context exhaustion
      is Ollama-version-dependent; an explicit cap gives the same headroom
      with a guaranteed stop) plus a num_ctx with matching room, sent
      explicitly so the 'we can't know the model's real default context'
      uncertainty is gone for this one call."""
      opts = effective_ollama_options()
      base_ctx = opts.get("num_ctx") or _DEFAULT_ASSUMED_CTX_TOKENS
      configured = opts.get("num_predict") or 0
      return {
          "num_predict": max(configured, 0) + _THINKING_EXPANDED_HEADROOM_TOKENS,
          "num_ctx": base_ctx + (_THINKING_EXPANDED_HEADROOM_TOKENS - _THINKING_HEADROOM_TOKENS),
      }
  ```

  The num_ctx delta is `EXPANDED − NORMAL` (not the full expanded value) so
  that, on the chunked summarize path, chunk sizing stays byte-identical to
  rung 1 (see below) while total generation room grows to
  `~_CHUNK_RESERVED_TOKENS + EXPANDED`.

- `summarize_transcript(..., expanded_thinking: bool = False)`
  (`app/ai.py:1217`): when set, `predict_override =
  expanded_thinking_options()` replaces the `_thinking_num_predict_override`
  result at `:1299`. **`chunk_chars` stays computed exactly as today**
  (`:1294`, normal headroom against the configured/assumed base ctx) — this
  is deliberate and must be documented in the docstring: identical
  `chunk_chars` + identical `chunk_total` means a checkpoint written by rung
  1 validates and resumes under rung 2 (`:1323-1324`), so parts already
  summarized before the starved part are never redone.

- `condense_call_options(..., expanded: bool = False)` (`app/ai.py:1068`):
  when set, use `_THINKING_EXPANDED_HEADROOM_TOKENS` as `thinking_headroom`
  unconditionally (bypassing the `think and (max_tokens or configured)` gate
  at `:1119-1123` — the expanded rung exists *because* something starved) and
  always return the computed `{"num_ctx": ...}` (like `force_fit`). Update
  its docstring's gate paragraph.

- `condense_recap(..., expanded_thinking: bool = False)` (`app/ai.py:821`):
  replace `:891`'s unconditional `opts.update(_thinking_num_predict_override(think))`
  with:

  ```python
  if expanded_thinking:
      opts["num_predict"] = expanded_thinking_options()["num_predict"]
  else:
      opts.update(_thinking_num_predict_override(think))
  ```

  (num_ctx comes in via `options` from `condense_call_options(expanded=True)`;
  the current code would otherwise clobber a caller-passed num_predict.)

Tests (`tests/test_transcript_chunking.py` / `tests/test_ollama_options.py`
conventions — `_FakeChatClient`, monkeypatched `effective_ollama_options`):
(a) `expanded_thinking_options()` with `{"num_predict": 512, "num_ctx": 8192}`
configured → `num_predict == 512 + ai._THINKING_EXPANDED_HEADROOM_TOKENS`,
`num_ctx == 8192 + (EXPANDED - NORMAL)`; (b) with nothing configured →
`num_predict == EXPANDED`, `num_ctx == 4096 + (EXPANDED - NORMAL)`;
(c) `summarize_transcript(expanded_thinking=True)` sends those options on
every chunk call AND produces the same `chunk_chars` (assert the fake saw
identically-sized user contents as a non-expanded run); (d)
`condense_recap(think=True, expanded_thinking=True, max_tokens=150)` → the
fake saw the expanded num_predict, never 150; (e) `condense_call_options(
expanded=True)` returns a num_ctx even for a short input.

**1.2 The ladder in `_run_job`. (Medium)**
`app/audio_jobs.py`. Replace `attempt_think_values` (`:722`) with:

```python
# (attempt_think, attempt_expanded) rungs — see docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md
attempt_plans = ([(True, False), (True, True), (False, True)]
                 if think else [(False, False), (False, True)])
```

(A think=False job gets an expanded second rung too — the "model ignores
think=False and starves anyway" case is real and cheap to cover; its think
value never flips.)

Both branches: advance to the next rung **only** when
`is_thinking_starved_sentinel(recap)`; any other failure breaks out into the
existing error handling unchanged; `JobInterrupted`/`CancelledError`
propagate as today. All attempts stay inside the existing
`ollama_job_semaphore` spans (`:753`, `:785`).

*session_recap branch* (`:773-801`): make `_checkpoint` (`:614`) also record
its state into a local `latest_checkpoint = {"state": None}` mutable, seeded
from `summarize_resume`. Per rung:

```python
resume_for_attempt = latest_checkpoint["state"] if attempt_think == think else None
```

— i.e. rungs that keep the job's original think value (1 and 2, and both
rungs of a think=False job) may resume the freshest checkpoint (identical
chunking, see 1.1); the think-flipped rung 3 starts fresh, preserving the
documented past-bug guard at `:773-784` (rewrite that comment to describe
the new rule: *resumable iff the attempt's think matches the think the
checkpoints were written under; `summarize_transcript`'s own
chunk_chars/chunk_total validation remains the backstop either way*). Pass
`expanded_thinking=attempt_expanded` through to `summarize_transcript`.

*condense branch* (`:738-766`): recompute per rung
`options = condense_call_options(..., think=attempt_think,
force_fit=fit_context, expanded=attempt_expanded)` and pass
`expanded_thinking=attempt_expanded` to `condense_recap`. Reuse the
already-computed `world_context` (no RAG re-query), as today.

*Persistence/surface*: new nullable column
`AudioJob.expanded_thinking = Column(Boolean, default=False)` in
`app/models.py` (next to `think_fallback`, `:882`; "audio_jobs" is already
in `_heal_table_from_model`'s healed list so existing installs migrate
automatically). Set it via `_set(expanded_thinking=True)` when entering any
expanded rung; keep setting `_set(think=False, think_fallback=True)` when
entering rung 3, as today. Log one warning per climb naming the rung:
`"session_recap job %s: thinking starved (think=%s expanded=%s) — climbing to (think=%s expanded=%s)"`.
Add `"expanded_thinking": bool(job.expanded_thinking)` to
`app/routers/audio_jobs.py:_job_to_dict` (`:37-77`).

Tests (`tests/test_audio_jobs.py` conventions — autouse `_fake_ai`,
`_await_terminal`): fake `summarize_transcript` that returns the real-shaped
starved sentinel unless its `expanded_thinking` kwarg is truthy →
job ends `done`, exactly 2 calls, `expanded_thinking` True, `think` still
True, `think_fallback` False; fake starved-unless-think-False → 3 calls,
`done`, `think` False, both flags True; fake always-starved → 3 calls,
`error`; think=False job + always-starved fake → 2 calls, `error`, think
never flipped; `"[AI error: …]"` on rung 1 → 1 call, no climb; checkpoint
reuse: fake that records the `resume` kwarg → rung 2 received the state rung
1 checkpointed, rung 3 received None; the condense-branch mirror of the
first two cases asserting `condense_call_options` was called with
`expanded=True` on the climb.

**1.3 Surface the ladder to the GM. (Small)**
`app/templates/background_jobs.html`: on done audio cards render dim
`bg-job-note-text` lines — when `job.expanded_thinking`: *"Thinking needed a
larger token budget — this run automatically retried with an expanded limit
(slower, no action needed)."*; when `job.think_fallback`: *"Thinking ran out
of output budget even with an expanded limit — this recap was written with
Thinking off."* (this second note was specified by the previous plan's 1.2
but never actually rendered — see section 0). The existing error-path
affordances ("🧠✕ Retry without Thinking", pre-unchecked checkbox,
`thinking_starved`) all still apply unchanged for the now-much-rarer case
where even rung 3 starves. Template-source test in the
`tests/test_live_recording_wake_lock.py` JS-source style.

**1.4 Widen the interactive thinking path. (Small)**
`app/routers/ai.py:ai_stream` (`:800-833`): after
`options = _clamp_options(body.options)` add

```python
if body.think and "num_predict" not in options:
    options = {**options, **_ai._thinking_num_predict_override(True)}
```

— a preset/caller that explicitly set num_predict keeps its exact value (the
allowlist at `:196-205` already admits it); otherwise a thinking Ask AI
request gets the same `configured + _THINKING_HEADROOM_TOKENS` widening the
recap family has. No SSE retry ladder (section 0). No new UI: the empty-
stream sentinel already renders as the reply and names the remedy; with the
widening in place the GM's practical fix ("uncheck 🧠 and resend") is one
click away in the existing panel. Tests: `tests/test_ai_stream.py` — POST
`/api/ai/stream` with `think: true` and a monkeypatched `stream_chat`
capturing `options` → widened num_predict present; with
`options: {"num_predict": 200}` → exactly 200; with `think: false` → absent.

**1.5 Fix the dense-script system-token estimate. (Small)**
`app/ai.py:1062`: `system_tokens = (len(system) // _chars_per_token_estimate(system)) if system else 0`.
Side effect to note in the commit: chunk_chars changes for jobs whose system
prompt is >30% non-ASCII, so an in-flight checkpoint from before the deploy
gets discarded by `summarize_transcript`'s own validation (logged, safe —
the guard exists for exactly this). Test: in
`tests/test_transcript_chunking.py`, a mostly-Cyrillic `system` yields a
smaller budget than the same-length ASCII system.

**1.6 Copy updates. (Small; text-only)**
- `app/templates/settings.html:230`: extend the existing help line: *"…if
  even that runs out, the run automatically retries with a much larger
  expanded limit (THINKING_EXPANDED_HEADROOM_TOKENS, default 12288)."*
- `app/templates/sessions/detail.html:57` (Thinking tooltip): append *"if
  thinking runs out of room, background runs retry automatically with a
  bigger budget."*
- `.env.example` (if it lists THINKING_HEADROOM_TOKENS) gains the new var.
No tests needed beyond optional template-content assertions.

---

## Part 2: Pipeline audit

### Verdict per area (honest scope)

1. **Sessions** — structurally sound; two real parity gaps (2.1, 2.2) and a
   missing background path for long live transcripts (3.2).
2. **Live Recording** — the design (1-min segments, sequential upload queue,
   3-attempt retry, wake lock, beforeunload guard) is good; the mic stream
   teardown/reacquire per segment (3.1) and permanent chunk loss after 3
   failed uploads (2.4) are the real reliability issues.
3. **Background tasks** — the engines themselves (checkpoints, resume,
   drain, sweeps, semaphores, `_forget_task` races) are solid; every finding
   is UI-side (2.5, 2.6, 4.1).
4. **Audio → Whisper** — solid (denoise fallback, chunk salvage, language
   forcing, repetition collapse all shipped); one enhancement with real
   value (2.7) plus documented-only limitations (segment-boundary word
   splits; within-job chunk parallelism deliberately not built — whisper.cpp
   serves one inference at a time and the cross-job semaphore is the
   protection that matters).
5. **Condense** — essentially finished by the prior plans + Part 1; the one
   remaining hole is the missing *upper* bound on auto-sized contexts (3.3).
6. **Summarize from Facts** — caching/invalidation shipped; the one gap is
   unbounded prompt size with no context sizing (2.2).
7. **Expand notes** — works; it's the one recap-family member that ignores
   both the "recap" model surface and World.recap_instructions (2.1).
8. **Thinking exposure** — deliberately inconsistent in a sensible way
   (Sessions default-on for quality, Ask AI default-off for latency; World
   Chat/Chronicler/JSON-schema calls have none by design — schema calls
   *must* stay think=False for parseable output). No missing toggles worth
   adding; Part 1 items 1.3/1.6 close the sentinel-confusion gap. **No
   further work.**
9. **RAG** — retrieval + formatting are good; three real findings: the
   non-English top-up exists only on the job path (2.3), pinned-entity
   bodies are uncapped in chat (3.5), and interactive chat has no num_ctx
   accounting at all (3.4). Unbounded chat history remains out of scope
   (trimming loses memory — prior plan's refutation stands) but 4.2 gives
   the GM visibility instead.

### Wave 2 — quick safe wins (no schema changes, no Part 1 dependency)

**2.1 Bring Expand-notes into the recap family. (Small)**
`app/routers/sessions.py:api_expand_recap_notes` (`:564-573`) passes neither
a model nor the world's standing instructions — every sibling route does
both. Change `app/ai.py:expand_recap_notes` (`:766`) to accept
`extra_instructions: str = ""` and apply `_with_instructions` to
`_EXPAND_NOTES_SYSTEM`; change the route to resolve the world from the
cookie (add `db`/`active_world` params, same signature as
`api_condense_recap`) and call
`expand_recap_notes(notes, model=_recap_model(""), extra_instructions=_recap_instructions_for_world(world), think=…)`.
A GM whose standing instruction is "write in Spanish" currently gets an
English expansion — this fixes that. Tests: extend
`tests/test_recap_instructions.py` / `tests/test_recap_model_surface.py`
with the expand-notes case (monkeypatched `generate_chat` capturing
system/model).

**2.2 Context-size the facts and notes recaps. (Small)**
`summarize_session_from_facts` (`app/ai.py:795-809`) and
`expand_recap_notes` pass no num_ctx — a fact-heavy session (the player
session-log route at `app/routers/sessions.py:1069` sends *every* fact,
uncapped) or a huge notes paste silently truncates at the configured/default
context, the exact garbage-output failure `condense_call_options`'s
docstring documents. Add a tiny private helper in `app/ai.py` next to
`context_sized_options`:

```python
def _ctx_override_if_needed(text: str, reserve_tokens: int) -> dict:
    needed = context_sized_options(text, reserve_tokens=reserve_tokens)["num_ctx"]
    baseline = effective_ollama_options().get("num_ctx") or _DEFAULT_ASSUMED_CTX_TOKENS
    return {"num_ctx": needed} if needed > baseline else {}
```

and in both functions merge it into the options they already build, with
`reserve_tokens = _CONTEXT_FIT_RESERVED_TOKENS + (_THINKING_HEADROOM_TOKENS if think else 0)`
and `text = system + user-content`. (Refactoring `condense_call_options`
onto this helper is optional and not required.) Tests: seed enough fact
lines that the assembled prompt exceeds 4096 assumed tokens → captured
options carry a scaled num_ctx; a short list carries none.

**2.3 Port the non-English entity top-up to chat RAG. (Small–Medium)**
`_build_rag_context` tops up non-note entities when keyword search finds
nothing — built for transcripts in a different language than entity names
(`app/audio_jobs.py:518-524` and its docstring). The chat-side
`/api/ai/world-context-smart` (`app/main.py:2449-2484`) has the notes
guarantee but **not** the entity top-up, so a Russian-language chat question
against English-named entities still gets a context with notes but no
characters/places — the same hole, one surface over. Add the identical
top-up block after `:2461` (query non-note entities excluding `seen_ids`,
ordered kind/name, limited to `body.limit - len(non_notes)`), or — better,
Medium — extract the shared "search + top-up + guaranteed notes" sequence
into `app/retrieval.py` (e.g. `retrieve_with_topup(db, world_id, query,
entity_limit, notes_limit, user=None, exclude_ids=frozenset())`) and call it
from both `ai_world_context_smart` and `_build_rag_context` so the two can't
drift again. Tests: `tests/test_ai_chat_lore_kv_cache.py` /
`tests/test_retrieval*`-style — a world with English entities + a Cyrillic
query → response `entities` non-empty.

**2.4 Live recording: stop losing failed chunks, unstick the status line. (Small)**
`app/templates/sessions/detail.html`:
- `liveProcessQueue` (`:724-752`) drops a chunk permanently after 3 failed
  uploads (`_liveQueue.shift()` regardless). Keep a `_liveFailedChunks`
  array instead; render *"⚠ N chunk(s) failed to upload — Retry"* in
  `#live-record-status` with a retry handler that re-queues them (they're
  `File` blobs; order-append at the transcript's end is acceptable and
  should be said in the label — better a late paragraph than silence).
- After Stop, the status stays *"Stopped — finishing the last chunk…"*
  forever (`:803-804`; the queue-drain path only rewrites status when
  `_liveRecording`). At the end of `liveProcessQueue`, when not recording
  and the queue is empty, set *"Stopped — transcript saved."*.
- While recording with a backlog, show it: *"Recording… (N chunks waiting
  for Whisper)"* — one-line change in the same function; a
  slower-than-realtime Whisper backend is currently invisible until chunks
  start timing out.
JS-source tests in the `tests/test_live_recording_wake_lock.py` style.

**2.5 Background Jobs page: stop wiping form state every 3 seconds. (Small–Medium)**
`bgRefresh` (`app/templates/background_jobs.html:478-503`) re-renders the
whole list (`list.innerHTML = ''`) on every 3s poll while anything is in
progress — which destroys whatever a GM has typed into a retry row's
"Extra instructions" input, resets the model select and Thinking checkbox,
and resets scroll position inside expanded `.bg-job-result` boxes. Fix
without restructuring: keep a `_bgFormState` map (`bgKey(job)` →
`{model, instructions, think}`) updated via `input`/`change` listeners on
the three controls, re-applied after each render (falling back to the
job-derived defaults at `:243/:251/:264` when absent); capture/restore
`.scrollTop` of expanded result boxes the same way. Template-source test
asserting the state map is wired.

**2.6 Inline jobs panel: give interrupted jobs an affordance. (Small)**
`static/js/audio-jobs.js`: "interrupted" is in neither `IN_PROGRESS` nor
`FINISHED` (`:27-28`), so on the Sessions page an interrupted condense/recap
job renders as a frozen "⏸ Interrupted" row with no button and no further
polling. Add a "▶ Resume" button for `status === "interrupted"` posting to
the existing `/api/audio-jobs/{id}/resume` (route already exists,
`app/routers/audio_jobs.py:222`), and include "interrupted" in the FINISHED
set for the delete-button purpose. Test in
`tests/test_background_jobs_unified.py`'s JS-source style.

**2.7 Whisper glossary: put this session's featured names first. (Small)**
The glossary's entity names are the first 50 alphabetically by kind/name
(`entity_glossary_terms`, `app/audio_jobs.py:266-284`) — on a big world the
NPCs actually spoken aloud this session can lose the ~600-char budget to
alphabetically-earlier strangers. When a job has a `game_session_id`, feed
the GM's "Entities Featured" picks (already read by
`_session_featured_picks`, `:398`) to the front of the entity-term list:
give `_glossary_for_world` an optional `game_session_id` param; inside,
fetch pinned entity names + pinned PC names first, then extend with
`entity_glossary_terms` minus duplicates, then `merge_glossary` as today
(GM-typed text still first and never trimmed). Callers:
`_run_job` (`:653`) passes the job's `game_session_id`;
`app/routers/sessions.py:_glossary_for_world` (`:76-82`) grows a
pass-through param used by the live-transcript append route (`:914` — the
session is known there). Pure-function tests on the new ordering.

### Wave 3 — reliability & budget-accounting (some UI/JS surgery; 3.2 depends on Part 1)

**3.1 One mic stream for the whole live recording. (Medium)**
`ndMicRecorder` (`app/templates/base.html:329-368`) calls `getUserMedia`
fresh on every `start()` and stops all tracks in `onstop` — and the live
loop creates a new recorder per ~60s segment
(`liveStartSegment`, `sessions/detail.html:754-774`). Cost: an audible gap
of getUserMedia latency every minute, a flickering mic indicator, and — on
browsers that re-prompt or fail re-acquisition after backgrounding (mobile
Safari in particular) — a recording that silently dies between segments.
Fix: let `ndMicRecorder(onStop, onError, existingStream)` accept an optional
already-acquired `MediaStream`; when given, skip `getUserMedia` and **do
not** stop the tracks in `onstop` (ownership stays with the caller). In
`sessions/detail.html`: `toggleLiveRecording` acquires the stream once on
Start (with the same audio constraints), passes it to every
`liveStartSegment` recorder, and stops the tracks only on Stop/mic-error.
Segments stay separate MediaRecorder instances producing complete standalone
containers — required, since each chunk is transcribed independently
(`recorder.start(timeslice)` interior chunks lack container headers; do not
switch to it). The one-shot 🎤 Record button keeps the current
self-contained behavior (no third argument). JS-source tests.

**3.2 Summarize the live transcript as a background job. (Medium; wants Part 1 landed first)**
`summarizeLiveTranscript` → `POST /api/sessions/{id}/ai/summarize-live-transcript`
(`app/routers/sessions.py:936-956`) runs a full `summarize_transcript`
inline in one request — for a multi-hour live transcript this is exactly the
reverse-proxy-timeout trap that motivated background jobs, and it gets
neither checkpoints, nor RAG, nor Part 1's retry ladder. Add
`app/audio_jobs.py:create_text_recap_job(world_id, text, *, model="",
think=True, extra_instructions="", game_session_id=None,
created_by_user_id=None, use_rag=False, rag_entity_limit=None,
rag_notes_limit=None)` — a sibling of `create_condense_job` (`:184-235`)
that seeds `purpose="session_recap"`, `transcript=text`, `audio_path=""`,
`delete_after=False`, `filename="Live Transcript"`; `_run_job`'s existing
"already has a transcript" carve-out (`:650`) skips straight to summarize,
so **no engine changes**. New route
`POST /api/sessions/{session_id}/ai/summarize-live-transcript-job` reading
the session's `live_transcript` server-side (400 when empty) plus the same
think/RAG/model fields the audio-job create route takes (`:780-811`); wire a
second button "🔒 Summarize in Background" next to the existing one in the
Live Recording panel, reusing `sessionAudioJobs` for polling/"Use this"
(purpose is already in `_SESSION_JOB_PURPOSES`). Keep the blocking route
for short transcripts. Route tests mirroring `tests/test_session_recap_ai.py`.

**3.3 An upper bound for every auto-sized context. (Small–Medium)**
`context_sized_options` (`app/ai.py:956-982`) and therefore
`condense_call_options`/Chronicler have **no ceiling** — a pathological
paste (say a 2 MB transcript dropped into the Summary field and Condensed)
computes a six-figure num_ctx that Ollama will try to allocate KV for, and
the `_OPTION_ALLOWLIST` num_ctx has no upper bound either
(`app/routers/ai.py:198`). Add
`MAX_AUTO_NUM_CTX = max(8192, int(os.getenv("MAX_AUTO_NUM_CTX", "32768")))`
in `app/ai.py`; clamp `context_sized_options`' return and
`expanded_thinking_options`' num_ctx to it. Because clamping alone would
reintroduce the silent-truncation garbage failure for condense, make the
condense entry points *refuse* instead of truncate: in
`api_condense_recap`/`api_condense_job_create`
(`app/routers/sessions.py:576-633`) estimate input tokens via
`_chars_per_token_estimate` and 400 with *"This text is too long to condense
in one call (≈N tokens > the M-token ceiling) — use Summarize, which splits
long input into parts"* when it exceeds `MAX_AUTO_NUM_CTX` minus reserve.
Chunked `summarize_transcript` is unaffected (its budgets already bound each
call). Tests: clamp unit tests + a 400 route test.

**3.4 Interactive chat gets context accounting. (Medium)**
`/api/ai/stream` (`app/routers/ai.py:800-833`) sends system + lore pair +
full history + attachments with **no num_ctx sizing at all** — the one AI
surface left where assembled input silently overflows the configured/default
context (Ollama truncates; with attachments and lore up front post-2.1's
reorder, what gets cut is model-visible history). In `ai_stream`, when the
caller didn't set num_ctx in `body.options`, estimate total chars of
`system + all message contents` (images excluded — they don't share the
text budget meaningfully), and merge
`_ctx_override_if_needed(total_text, _CONTEXT_FIT_RESERVED_TOKENS + headroom-if-think)`
(item 2.2's helper) clamped by `MAX_AUTO_NUM_CTX` (3.3). Same one-call
override semantics as everywhere else — the instance default is untouched.
Apply identically in `api_chat_job_create` (`:1923`) so backgrounded chat
matches. Tests in `tests/test_ai_stream.py`/`tests/test_chat_jobs.py`:
long-history request → captured options carry scaled num_ctx; explicit
caller num_ctx wins.

**3.5 Cap pinned-entity bodies in chat RAG. (Small)**
`_pinnedEntitiesContext` (`static/js/ai-chat-core.js:904-915`) injects each
pinned entity's **full body** into every send — three long write-ups can
dwarf the retrieved context and (post 3.4) inflate num_ctx every turn. Slice
each body to ~4000 chars with a visible `…[truncated — open the entity for
the rest]` suffix, and cap the combined pinned block at ~12000 chars
(drop-with-notice beyond it, keeping insertion order). Constants at the top
of the file next to the existing context helpers. JS-source test in
`tests/test_ai_chat_split.py` style.

### Wave 4 — polish (lowest risk, do whenever)

**4.1 Background Jobs page pagination honesty. (Small)**
`/api/audio-jobs` paginates (`app/routers/audio_jobs.py:100-102`) but the
page fetches only page 1 and renders it as if it were everything
(`bgFetchJobs('audio', '/api/audio-jobs', true)`, `background_jobs.html:487`).
Either render a "Showing the latest N audio jobs — older ones exist" line
when `total_pages > 1`, or add a simple "Load more" appending page 2+.
Chat/image lists are capped at 20 server-side — same one-line notice.

**4.2 Chat context-usage indicator. (Small–Medium)**
With history unbounded by design, give the GM *visibility* instead of
trimming: in `ai_chat.html`'s status area, after each send show
`≈N tokens sent` (reuse `ndTokenLabel`-style estimation over the assembled
messages — `buildChatMessagesWithContext` already has them in hand) and
color it warning-red when it exceeds the configured num_ctx fetched once
from a small `GET /api/ai/context-info` (returns
`effective_ollama_options().get("num_ctx")` / assumed default). Pairs with
3.4: the GM sees *why* a long chat got slow or forgetful.

**4.3 Micro-fixes & doc pass. (Small)**
- `app/routers/sessions.py:1034`: `_session_log_recap_cache` annotation says
  `dict[tuple, dict]` but stores `(monotonic, dict)` tuples — correct to
  `dict[tuple, tuple[float, dict]]`.
- `app/routers/sessions.py:604-633` (`api_condense_job_create`)'s docstring
  and the blocking `api_condense_recap` (`:576`): note the blocking route is
  API-compat only (no UI caller since Condense became a job) — keep, don't
  extend it with RAG.
- Record this plan's "preserve these" guidance next to the code it protects:
  a comment on `attempt_plans` pointing here; a comment in
  `liveStartSegment` stating why segments are separate MediaRecorders
  (3.1's timeslice rejection).

---

## Suggested execution order

Each wave is independently shippable by an implementer with no memory of
this research — every item carries its own file/function references and test
plan. Run the full suite between waves (baseline at time of writing:
1903 passed, 2 skipped).

**Wave 1 — Part 1, the reason this plan exists: ⬜**
1.1 (primitives) → 1.2 (ladder + `AudioJob.expanded_thinking`) → 1.3 (job-card
notes incl. the missing think_fallback note) → 1.4 (interactive widening) →
1.5 (system-token estimate) → 1.6 (copy). 1.1 and 1.2 are one logical change
and should land together; the rest can follow in the same wave as separate
commits.

**Wave 2 — quick safe wins, no schema/UI restructuring: ⬜**
2.1 (expand-notes parity) · 2.2 (facts/notes context sizing) · 2.3
(chat-RAG top-up parity) · 2.4 (live-recording failed-chunk retention +
status) · 2.5 (background-jobs form-state preservation) · 2.6 (inline-panel
resume) · 2.7 (glossary featured-names priority).

**Wave 3 — reliability & budget accounting: ⬜**
3.1 (persistent mic stream) · 3.2 (live-transcript background job — after
Wave 1, so those jobs inherit the ladder) · 3.3 (MAX_AUTO_NUM_CTX ceiling —
before 3.4, which uses it) · 3.4 (interactive chat context sizing) · 3.5
(pinned-body caps).

**Wave 4 — polish: ⬜**
4.1 (jobs pagination notice) · 4.2 (context-usage indicator) · 4.3
(micro-fixes/doc pass).
