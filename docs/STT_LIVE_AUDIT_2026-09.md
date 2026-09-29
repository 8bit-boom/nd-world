# STT & Live Recording — audit and fixes (2026-09-30)

Read-only audit of the full speech-to-text pipeline (both backends: the
whisper.cpp sidecar and Unsloth Studio's `/v1/audio/transcriptions`) and the
live session-recording flow, followed by fixes for everything fixable in
code. Companion to `docs/LIVE_RECORDING_AUDIT.md` (whose 14 items were all
re-verified landed at audit time). Line numbers refer to the fixing commits;
grep for the quoted symbol when in doubt.

**Deployment context that shaped the findings:** the live deployment runs
CPU-only Unsloth Studio behind a Cloudflare tunnel (world.neonanddragons.com,
~100 s response ceiling) with the whisper.cpp sidecar removed.

## Findings → status

### 1. Live chunks through the unsloth backend cannot fit inside one HTTP request behind a proxy — **mitigated (server + client)**

The live panel transcribes each chunk synchronously and returns the text in
the response — correct for a local whisper.cpp, but CPU-only Studio runs at
~3–4× realtime, so even a 1-minute chunk outlasts the tunnel's ~100 s
ceiling. The proxy gives up on the response while the server keeps working;
the client's 3-attempt retry ladder re-POSTs; the segment-level idempotency
check (which only sees COMMITTED state) lets every retry start a concurrent
duplicate transcription — ~3× the compute per chunk, duplicate jobs stacked
in Studio's queue, chunks wrongly shown as failed.

Fixes (both halves needed):

- **Server:** `api_live_transcript_append` now keeps an in-flight marker per
  `(session_id, recording_id:segment_index)` — a retry arriving while the
  first attempt is still transcribing gets an immediate 409 ("Still
  transcribing this segment…") instead of a second transcription. The marker
  is popped in a `finally` and expires after `LIVE_STT_INFLIGHT_TTL_SECONDS`
  (45 min default) so a hard crash can't block its segment forever. The
  transcription also now holds `app.ai.whisper_job_semaphore` — the same
  serialization background jobs already respect — so a live chunk doesn't
  stack against a running session-recap job in the backend's queue.
- **Client:** `liveProcessQueue` treats that 409 as "keep waiting", not a
  failure — it doesn't consume the 3-attempt ladder, polling every 15 s up
  to ~10 minutes per chunk (status line says the server is still working),
  after which the chunk parks in the failed list for manual Retry (which
  then hits the committed idempotency fast path and returns instantly).

**Still true and unfixable in code:** end-to-end latency of the live
transcript on a CPU-only Studio behind a proxy is minutes, not seconds.
Until a GPU lands, the mitigations above make it correct (no duplicate work,
no lost chunks, eventual delivery) rather than fast. Recording over the LAN
(around the tunnel) or reinstating a whisper.cpp sidecar restores realtime.

### 2. AI-chat audio attachments could outlive the proxy ceiling — **fixed**

`_finish_attachment_upload` already degraded a `WhisperError` to "attach
without transcript", but a slow backend doesn't raise — it just keeps the
request hanging past the proxy's ceiling, and the 524 reads as a failed
upload (the attachment is re-sent and re-transcribed on retry). The
in-request transcription is now bounded by `AI_ATTACH_TRANSCRIBE_TIMEOUT_SECONDS`
(90 s default); on timeout the attachment is still stored and returned,
just without its transcript (logged server-side).

### 3. One mic death could trigger two concurrent recovery ladders — **fixed**

`liveHandleMicFailure` was reachable twice for a single failure:
`track.onended` AND the dead segment's chained `liveStartSegment()` failing
on the inactive stream. Two concurrent ladders could each acquire a
`getUserMedia` stream (one orphaned — mic indicator hot until tab close),
each call `liveStartSegment` (two chains racing the shared
`_liveCurrentRecorder`/`_liveSegmentIndex`/`_liveChunkTimer`), and
double-increment the attempt counter toward a spurious "Mic lost".

- `liveHandleMicFailure` is now single-flight (`_liveRecovering` mutex), and
  its backoff re-entry is a loop rather than recursion so the mutex doesn't
  strand the retry.
- `liveStartSegment` pins the stream generation it was built on (`segStream`)
  and refuses to run while recording with no stream — a superseded
  generation's `onStop` can neither chain a second loop onto the recovered
  stream nor release the recovered stream as if it were the last segment.

### 4. A silent segment's idempotency key was never recorded — **fixed**

A chunk that transcribed to "" with "Save raw audio" off skipped the commit
entirely (`if chunk_text or saved_rel:`), so its segment key never landed in
`live_transcript_segments_json` and a retried silent chunk re-burned a full
Whisper pass for text that was always going to be empty. The commit now also
fires for a newly-transcribed silent segment, and the key append is gated on
"not already appended" (which also stops a benign duplicate key entry on
post-commit retries with the archive on).

### 5. The world's Whisper knobs silently no-op on the unsloth backend — **disclosed**

`whisper_glossary` / `whisper_language` / `whisper_denoise` are read and
passed on every transcription path, but `_transcribe_one_file_unsloth` drops
all three (Studio's own Voice settings own those; only documented in a
docstring before). The Settings → STT backend selector now shows a note,
when Unsloth Studio is selected, that those world settings don't apply.

### 6. Smaller observations — **documented, deliberately unchanged**

- Checkpoint identity is `(chunk_total, chunk_seconds, audio_size)` — a
  byte-size collision between different audio would splice mismatched text.
  Theoretical only: resume is scoped to a job's own uploaded file.
- The unsloth path forwards original extensions including `.oga/.opus/.aac`,
  a wider set than OpenAI-dialect servers commonly advertise. A rejection
  surfaces verbatim via the 409/400 path; unverified against live Studio.
- `_liveQueue`/`_liveFailedChunks` are unbounded in memory (~100–200 MB for
  an 8-hour backlog); the status line surfaces the backlog count, which is
  the honest UI for a backend that's genuinely behind.

## What was verified solid (no action)

Checkpoint/resume with phase precedence and partial-transcript salvage;
append-route idempotency including the early-skip-before-Whisper ordering;
the pool-hold fixes from the prior audit's Wave 5; the atomic `.part` concat
with mtime-based cache invalidation; the repetition-loop mitigation stack
(beam/entropy overrides + chunking + collapse, each with a cited reason);
the unsloth chunk-planning math and transcode ladder; 15 STT/live test
files; and the Settings ▷ Test STT health-check as a pre-session probe of
finding 1's precondition.
