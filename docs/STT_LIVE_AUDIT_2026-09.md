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

## Re-audit (same day, after the fixes) — F1/F2 fixed, F3/F4 accepted

A verification pass over the fixing commit re-read every changed hunk and
traced its races. Two real defects were found in the fixes themselves and
are fixed; two trade-offs were confirmed acceptable and are recorded here.

### F1. A TTL-expired twin could still append its text twice — **fixed**

The in-flight guard's marker expires after 45 min. A request that aged
past that while still queued on `whisper_job_semaphore` (e.g. behind a
multi-hour background transcription) admits a twin; the twin's
`already_appended` snapshot was read at ITS request start — before the
first request committed — so its commit block appended the same
re-transcribed text a second time. (The unconditional text append itself
predates the audit; the guard made it near-unreachable, and this was the
last reachable residue.) The commit block now re-checks the segment key
against the freshly re-fetched row (`duplicate_now`) and skips both the
text and the key append when the first twin already committed. The wasted
re-transcription remains — that's the TTL's documented cost — but
duplicated transcript text is now impossible on this path.
`test_stale_snapshot_twin_cannot_append_its_text_twice` pins it.

### F2. The STT Test button/note visibility didn't sync after prefs load — **fixed**

`loadPrefs` sets the backend `<select>` programmatically, which doesn't
fire `change` — so on page load with a stored backend of "unsloth", the
▷ Test STT button (pre-existing) and the whisper-knobs note (new) stayed
hidden until the GM manually touched the select. `loadPrefs` now calls
`syncSttTestVisibility()` after applying the stored value.

### F3. Each patient in-flight poll re-uploads the segment's audio — **accepted**

The 409 path is a full re-POST (multipart file included), so ~10 minutes
of patient polling can re-send the chunk ~40 times (~a few MB each on a
typical Opus segment). Functionally harmless — the guard answers before
reading the body — but not free on a metered upstream. A cheap
`GET .../live-transcript/status` poll endpoint would remove the traffic;
not worth the surface area until someone records over a metered link.

### F4. Live chunks may queue arbitrarily long behind a background job — **accepted**

With `WHISPER_JOB_CONCURRENCY=1`, a live chunk arriving while a full
multi-hour recording job transcribes waits on the semaphore for as long
as that job runs. The system degrades correctly (the chunk's in-flight
marker makes the client poll patiently, then park it for Retry once the
job drains), and the backend genuinely can't serve both at once — but a
GM who starts a recap job mid-session should expect the live transcript
to lag until it finishes. Deliberate politeness, not an accident;
raise `WHISPER_JOB_CONCURRENCY` on hardware that can actually parallelize.

Also re-verified in this pass: the check-and-set of the in-flight marker
contains no `await` (atomic on the event loop — two truly simultaneous
requests cannot both pass it); the semaphore has no nested acquisition
path; `asyncio.wait_for` cancellation on the attach route runs only
synchronous `finally` cleanup; and the client's restructured retry loop
increments its attempt counter only on real failures.
