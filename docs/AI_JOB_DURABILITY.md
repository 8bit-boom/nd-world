# AI Job Durability Audit

Audit date: 2026-09-07 · branch `fix/ai-job-durability`

The question this audit answers: **for every AI operation nd-world can
perform, what happens when the browser tab closes, the reverse proxy
times out, or the server restarts mid-work?** "Durable" means all three
survive: the work runs as a background task independent of any HTTP
connection, every state transition is persisted to a DB row, and a
restart resumes the work (from a checkpoint where the op is chunked, or
by re-running from its saved inputs where it isn't) instead of losing it.

---

## 1. The durability machinery (shared by all three job engines)

`app/job_shutdown.py` — three phases on every shutdown (Docker restart,
Watchtower update, `docker compose up -d --build`):

- **Phase A**: `request_stop()` flips a process-wide flag; chunk loops in
  `app/ai.py` check it at each chunk boundary and exit cleanly with the
  checkpoint already saved (up to `ND_JOB_STOP_GRACE_SECONDS`, default 5s).
- **Phase B**: `drain()` cancels whatever is still running and waits a
  bounded settle window for each task's synchronous `CancelledError`
  handler to persist `status="interrupted"` (distinguishable from a
  GM-initiated cancel via `stopping()`).
- **Phase C**: `mark_stragglers_interrupted()` sweeps any row still
  mid-flight in the DB after drain — the same shape as
  `sweep_interrupted_jobs()` on boot, which covers an unclean death
  (SIGKILL/OOM/power loss) where no handler ran at all.

**Boot**: `sweep_interrupted_jobs()` (normalize unclean-death rows) →
`resume_interrupted_jobs()` (auto-resume every `interrupted` job up to
`MAX_AUTO_RESUMES=3`, past that a deliberate error so a
crash-looping input can't retry forever). Wired in `app/main.py`'s
lifespan for **all three engines** (audio, image, chat).

Common invariants, verified per engine: every job is a DB row created
before the task starts; tasks are strongly referenced (`_running_tasks`
+ done-callback) so asyncio can't GC them mid-run; Ollama calls run under
`ollama_job_semaphore`, Whisper under `whisper_job_semaphore`, imagegen
under `imagegen_job_semaphore`; `generate_chat` failure sentinels become
`status="error"` rows, never cached "done" results; per-job settings
(model/think/RAG/limits/params) live on the row so a resume re-runs the
identical operation.

## 2. Engine-by-engine verdict

| Engine | Row / task model | Checkpoint | Boot behavior | Verdict |
|---|---|---|---|---|
| `audio_jobs` — `session_recap` (uploaded audio) | AudioJob | Whisper chunk checkpoints + transcript map-reduce part checkpoints (`checkpoint_json`, mirrored partial transcript) | resume from checkpoint; audio file preserved on interrupt and swept only when unreferenced (`sweep_orphaned_job_audio`) | ✅ durable |
| `audio_jobs` — `session_recap` (live transcript via `create_text_recap_job`) | AudioJob, input = `GameSession.live_transcript` re-read at run time | transcript chunks checkpoint mid-summarize | same | ✅ durable |
| `audio_jobs` — `condense`, `facts_parse`, `session_log_recap`, `attachment`, `ai_assist`, `world_summary` | AudioJob (text input in `transcript`, params in row columns / `assist_params_json`) | single-call ops (no chunks to checkpoint); facts/assist results in `result_json` | single-phase resume (re-run from row); **gap fixed, see §4** | ✅ durable |
| `image_jobs` — imagegen | ImageJob, full params in `params_json` | n/a (one call) | restart-from-params on boot; the Image Gen UI's Generate button and the chat page's Illustrate action both run through this engine (the old blocking button 524'd — fixed 2026-09-07) | ✅ durable |
| `chat_jobs` — non-streaming chat completion | ChatJob, `messages_json`/`system`/`options_json` | n/a | restart-from-saved-request on boot | ✅ durable |
| Live recording | **client-driven** chunk loop: browser POSTs each chunk; every completed chunk's text is committed to `live_transcript` immediately, raw-audio segments archived per chunk when enabled | per-chunk, in the DB | a restart loses at most the one in-flight chunk (~30–60 s), which the recorder's failed-chunk/retry UI re-uploads | ✅ durable by design (client-coupled) |

## 3. Everything else: interactive by design

These hold an HTTP request open on purpose. They are **not** durability
bugs — each is short, bounded, and produces nothing that must outlive the
request — but on a Cloudflare-tunnel deployment each can 524 if the
backend is unusually slow (cold model, CPU-only box). Listed so the
boundary is a recorded decision, not an accident:

- `/api/ai/chat` + `/api/ai/stream` (SSE — bytes flow continuously, so
  the tunnel doesn't kill them; heartbeat comments included), entity
  Ask-AI panel, King-in-Yellow play streaming.
- `/api/ai/assist` — the shared ✨ panel, input-capped at 60k chars; the
  job variant (`/api/ai/assist-job`) exists precisely for bigger content,
  and the rules editor uses it.
- `/api/ai/generate/entity-smart` (entity form's one-click drafter),
  `/api/ai/entity-from-text` (chat → draft entity): single short calls.
- Sessions page: **Expand notes into recap** and **Generate prep
  checklist** — small-input, single-call, recap-family guards applied.
- Chronicler `/api/chronicler/ask` (+ MCP `ask_chronicler`):
  visibility-filtered, output-capped, 30 s answer cache, LLM cooldown.
- Attachment **direct** upload (`/api/ai/attachments/upload`): inline
  Whisper for a dropped file; the 🔒 background-voice-memo path is the
  durable alternative for anything recording-length, and the Whisper
  **test tab** is a test surface by definition.
- Sessions page **Process recording (direct)** — the non-🔒 path of the
  audio panel: a deliberate "process now, small file" choice whose 🔒
  sibling is the durable one.
- The blocking sync routes kept as **documented API only** (no UI
  caller): `condense-recap`, `summarize-from-audio`, `summarize-from-facts`,
  `summarize-live-transcript`, `facts/parse`, `imagegen/generate`.
- GGUF model import (`/api/ai/ollama/upload/...`): the push-to-Ollama
  phase runs as a detached task with an **in-memory** progress dict — a
  restart loses the progress indicator (and can leave an orphaned blob);
  accepted as model management, not a content job.

## 4. Gaps found by this audit → fixed

1. **Boot auto-resume mislabeled `ai_assist`/`world_summary` jobs.**
   `audio_jobs.resume_interrupted_jobs` only exempted
   `session_log_recap` from the "no transcript and no audio → 'please
   re-upload'" check — but a content-less assist op (table_entries) and
   a world summary legitimately have neither (params/state live
   elsewhere). An interrupted job of either purpose was turned into an
   error at boot instead of resuming, disagreeing with
   `start_resume_job`'s manual-resume path (which already had the
   `single_phase` carve-out). Fixed: the boot check now exempts all
   three single-phase purposes, plus a regression test that interrupts
   both and asserts boot resume completes them.
2. **Sessions page "✨ Summarize transcript" was the last UI flow
   blocking on a minutes-long route.** Same failure the Image Gen tab
   had: a multi-hour live transcript against a slow local model 524s at
   the tunnel with all work lost. Both that button and its 🔒 sibling now
   start the durable job (checkpointing, RAG, retry ladder, tab-close
   proof); the blocking route stays as documented API.

## 5. Recommendations (not acted on)

- The direct "Process recording" audio path could follow imagegen/live-
  transcript and default to a job, keeping the blocking route API-only —
  deliberately deferred: the direct path doubles as the small-file
  "process now" UX and its 🔒 alternative is already one click away.
- `expand-notes` and `prep/generate` could become jobs on very slow
  setups; their inputs are small enough that interactive is currently
  the right trade.

---

Related: [AI_EVERYWHERE_AUDIT.md](AI_EVERYWHERE_AUDIT.md) ·
[API_REFERENCE.md](API_REFERENCE.md) §Background Jobs ·
`app/job_shutdown.py` (the three-phase shutdown design, in code).
