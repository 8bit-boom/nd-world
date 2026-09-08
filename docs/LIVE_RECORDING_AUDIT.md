# Live Session Recording — focused audit

Read-only audit of the "🔴 Live Session Recording" panel, triggered by a real
report: a GM recorded a multi-hour session, the UI showed "Recording…" the whole
time, and **only chunk 1 was saved**. No error, no failed-chunk retry prompt.

Line numbers are as of commit `aef3b79`. If drift is suspected, grep for the
quoted function name instead.

**Surfaces covered:** `app/templates/sessions/detail.html:244-280` (panel markup)
and `:1076-1414` (panel JS); `app/templates/base.html:337-408` (`ndMicRecorder`,
shared with the one-shot 🎤 buttons); `app/routers/sessions.py:1051-1268`
(append/archive/download routes).

| Wave | Theme | Items |
|------|-------|-------|
| 1 | The reported data loss — capture chain dies silently | 1–2 |
| 2 | Other silent stalls and transcript corruption | 3–6 |
| 3 | Failure visibility and known-limitation disclosure | 7–10 |
| 4 | Test coverage | 11 |

**Out of scope (deliberate, pre-existing non-goals — do not "fix"):**
recovering a recording after the tab crashes or is closed. The panel already
states this contract at `detail.html:253` ("Leave this tab open and awake for the
duration; the recording stops if the tab is closed") and the `beforeunload`
guard at `:1412-1414` is the intended mitigation. The opt-in raw-audio archive
plus `GET /api/sessions/{id}/live-audio/download` is the intended recovery path
for a bad or truncated transcript (see `models.py:939-951`). Item 6 below is the
one genuine *gap* inside that existing contract, not a new goal.

---

## Wave 1 — The reported data loss

### 1. A failed segment start silently kills the recording while the UI keeps saying "Recording…"  ← **this is the GM's bug; fix this first**

**What's broken.** After chunk 1, capture stops permanently. The button still
reads "⏹ Stop Recording", the status still reads "Recording…", no error appears,
and the failed-chunk retry UI never triggers. Everything after chunk 1 is lost.

**Root cause — a three-link chain, all three links required:**

1. `app/templates/base.html:363-388` — `ndMicRecorder`'s `start()` guards
   `getUserMedia` in a `try/catch` (`:369-376`), but that branch is **skipped
   entirely when `existingStream` is passed**, which is the case for every live
   segment (`detail.html:1274` passes `_liveMicStream`). The two lines that
   actually run for a live segment are unguarded:
   - `:379` `mediaRecorder = new MediaRecorder(stream);`
   - `:386` `mediaRecorder.start();`

   `start()` is the specified thrower: per the MediaStream Recording spec it
   raises `InvalidStateError` when the `MediaStream` being recorded is inactive
   (i.e. all its tracks have ended). Because `start()` is declared `async`
   (`:363`), a synchronous throw inside it does not propagate to the caller as
   an exception — it becomes a **rejected promise**.

2. `app/templates/sessions/detail.html:1276-1285` — `_liveCurrentRecorder.start().then(...)`
   has **no `.catch()`**. On rejection the callback never runs, so
   `_liveChunkTimer` is never re-armed, `_liveRecording` is never cleared, and
   the button/status text are never updated. The state machine simply stops with
   every flag still saying "recording".

3. `app/templates/sessions/detail.html:1277` — even the non-throwing failure path
   lies. `if (!started) { _liveRecording = false; return; }` clears the flag but
   leaves the button on "⏹ Stop Recording", the status on "Recording…", and
   `_liveMicStream` un-released.

**Why the stream dies in the first place** is browser/OS/hardware specific and
may never be fully knowable from code — plausible triggers include an OS-level
mic reclaim, a Bluetooth headset reconnect, or (most likely for a long
table-side recording) mobile Safari suspending the page's capture pipeline when
backgrounded, which ends the audio track. **This does not need to be identified
to fix the bug.** Note the ordering that makes it invisible: per spec, when a
stream goes inactive mid-recording the UA fires a final `dataavailable` and then
`stop` — so chunk *N*'s partial blob uploads *successfully*, `onStop` chains to
segment *N+1* (`:1262-1263`), and only *that* start throws. The last thing the GM
sees is a successful upload.

**Aggravating factor.** `DYNAMIC_THINKING_AND_PIPELINE_PLAN.md:501-518` introduced
the shared `_liveMicStream` specifically to stop per-segment `getUserMedia` from
"silently killing the recording between segments." That was a real fix, but
per-segment `getUserMedia` was also the feature's only **self-healing**
mechanism. With one long-lived stream there is now no code path anywhere that
re-acquires the mic. A dead stream is permanent.

**The fix.** Three parts. All three are needed — 1a alone only makes it visible,
1b/1c make it survivable.

**(1a)** `app/templates/base.html`, `ndMicRecorder.start()` — guard construction
and start, and wire the error event.

**(1b)** `app/templates/sessions/detail.html`, `liveStartSegment()` — add a
`.catch()` routing failure into a new recovery handler.

**(1c)** New `liveHandleMicFailure()` / `liveWatchMicTracks()` — restores the
self-healing that the persistent-mic-stream change removed, on the failure path
only: drop the dead stream, re-acquire via `getUserMedia` with a bounded
(3-attempt) backoff ladder, then resume segment chaining. Give up after 3
attempts with a sticky, visible error and a button reset to "🔴 Start Recording".
Watch `track.onended` on the stream so a mid-segment mic death is caught within
seconds, not at the next chunk boundary (which could be up to 15 minutes away).

**Do not touch `_liveSegmentIndex`.** It is incremented in `onStop` at capture
completion, so a segment that never started never consumes an index — recovery
leaves no gap in the archive.

**How to verify.**
- *Automated (JS-source, repo convention — see item 11):* assert `base.html`'s
  `start()` body wraps `new MediaRecorder(stream)` in `try`/`catch` that calls
  `onError` and returns `false`; assert `mediaRecorder.onerror` is wired; assert
  `liveStartSegment`'s body contains `.catch(` and `liveHandleMicFailure`; assert
  `t.onended` / `getAudioTracks()` appear; assert a bounded attempt counter.
- *Manual (the real proof — 2 minutes):* start a recording with chunk length
  60s, wait for chunk 1 to upload, then in DevTools run
  `_liveMicStream.getAudioTracks()[0].stop()`. **Before the fix:** the panel keeps
  saying "Recording…" and no further transcript ever appears. **After the fix:**
  the status flips to "⚠ Mic dropped — reconnecting (1/3)…", the browser
  re-prompts or silently re-grants, and chunk 2 appears in the transcript within
  a chunk length. Then repeat with the mic permission revoked in site settings to
  confirm the ladder gives up after 3 tries with a visible, *sticky* error and a
  button that returns to "🔴 Start Recording".

---

### 2. After the chain dies, Stop leaves the mic hot and the status permanently wrong

**What's broken.** A consequence of item 1, worth fixing independently because it
is what the GM actually observed after pressing Stop.

`toggleLiveRecording()`'s Stop branch calls `_liveCurrentRecorder.stop()`
whenever `_liveCurrentRecorder` is truthy. After a failed start the object *is*
truthy, but its inner `mediaRecorder` is either `null` (constructor threw) or
`inactive` (`start()` threw) — so `stop()` no-ops, `onStop` never fires, and the
`else` branch that would have called `liveStopMicStream()` is never reached.
Results:

- `_liveMicStream`'s tracks are never stopped — **the browser's mic indicator
  stays lit until the page is navigated away from.** A privacy problem, not just
  cosmetic.
- Status is left on "Stopped — finishing the last chunk…" forever, since
  `liveRefreshStatus()` is only reachable from the queue drain, which never
  happens.

**The fix.** In the Stop branch, don't infer liveness from the object's
existence — ask it via the existing `isRecording()` helper. Stop only when
genuinely capturing; otherwise release the stream and settle the status
immediately.

**How to verify.** Manual: reproduce item 1's dead-chain state, press Stop, and
confirm (a) the browser mic indicator goes out immediately and (b) the status
reads "Stopped — transcript saved." (or the sticky mic error from item 1c),
never "finishing the last chunk…".

---

## Wave 2 — Other silent stalls and transcript corruption

### 3. A retried chunk appends its text to the transcript twice

**What's broken.** `api_live_transcript_append` is idempotent for the **raw
audio file** (overwrites the same path, dedups the JSON list) but not for the
**transcript** — `gs.live_transcript = ... + chunk_text` appends
unconditionally. The client's 3-attempt retry ladder re-POSTs the identical
segment on any `fetch` rejection or non-OK response; if the server transcribed
and committed but the *response* was lost (a reverse-proxy gateway timeout, a
reset connection), the retry duplicates that chunk's text.

**Not exotic on this deployment:** the panel offers 15-minute chunks, and a
single request holding a connection open for several minutes of self-hosted
Whisper transcription is a prime candidate for a proxy timeout. The symptom
(duplicated passages) is indistinguishable from the Whisper
boundary-duplication artifact the chunk-length control already exists to
reduce, so it would be misattributed if left as-is.

**The fix.** Make the transcript append idempotent on
`(recording_id, segment_index)` — both fields are already sent on every upload,
unconditionally, by the client:

1. Add `GameSession.live_transcript_segments_json` (JSON array of
   `"<recording_id>:<segment_index>"` keys already folded into the transcript).
   No migration code needed — `game_sessions` is already in
   `database.py`'s generic heal-table list.
2. In `api_live_transcript_append`, before transcribing: if the key is already
   recorded, early-return the current transcript without re-invoking Whisper
   (early-return *before* transcription, not after — a retried 15-minute chunk
   should not burn GPU minutes re-transcribing audio whose text is already
   saved).
3. Append the key in the same commit that appends the text.
4. Leave the legacy path (blank `recording_id` / `segment_index == -1`, an
   older client) falling through to today's unconditional append.
5. `api_live_transcript_clear` must reset the new column alongside
   `live_transcript`.

**How to verify.** A pytest route test: POST the same audio file twice with
identical `recording_id`/`segment_index` and assert the transcript contains the
chunk's text exactly once and Whisper is not re-invoked on the duplicate; a
different `segment_index` with the same `recording_id` still appends.

---

### 4. One unexpected throw wedges the upload queue permanently

**What's broken.** `liveProcessQueue` sets `_liveQueueBusy = true` and only
clears it after the loop — nothing guarantees the flag is cleared if something
inside the loop throws outside the per-chunk `try`. A stuck `true` means every
future call bails immediately: capture continues, blobs pile into `_liveQueue`,
nothing is ever uploaded again, and the status line never updates — the same
"looks fine, saves nothing" failure mode as item 1, from a different direction.

A second, subtler bug in the same function: `liveSetTranscriptDisplay(...)` is
called *inside* the upload's try block, after the fetch already succeeded. If
rendering throws, the catch treats a **successful upload** as a failure and
re-uploads the chunk up to 3 times — which is exactly item 3's duplicate-append
trigger.

**The fix.** Wrap the queue-drain loop body in `try { ... } finally { ... }` so
`_liveQueueBusy` can never stick and the status always settles. Move the
transcript-display update out of the upload's try block into its own
try/catch, so a rendering failure can never be mistaken for an upload failure.
Add `.catch()` to the two fire-and-forget `liveProcessQueue()` call sites.

**How to verify.** JS-source assertions on the `finally` and the moved display
call; behavioural coverage via the optional Node harness in item 11.

---

### 5. Double-click on Start races: two streams, two chains, one hot orphan

**What's broken.** `toggleLiveRecording` awaits `getUserMedia` before setting
`_liveRecording = true`, and the Start button is never disabled during that
window. Two clicks before the mic permission resolves both take the Start
branch: the first stream leaks (mic stays hot, nothing ever releases it), the
second chain's `clearTimeout` cancels the first chain's pending stop (so the
first recorder never uploads its audio at all), and both chains mint
colliding `recording_id`/`segment_index` values into the same archive folder.

**The fix.** Add a re-entrancy guard (set before the `await`, cleared in a
`finally`) and disable the Start button across the await.

**How to verify.** Manual: double-click Start rapidly, confirm exactly one mic
indicator and one monotonic segment sequence on disk.

---

### 6. `beforeunload` doesn't warn while uploads are still pending after Stop

**What's broken.** The close-tab warning only checks `_liveRecording`, which
Stop clears immediately — while the final segment may still be uploading, and
failed chunks may still be sitting in memory waiting for Retry. A GM who
presses Stop and closes the tab loses them with **no warning at all**,
contradicting the panel's own promise that "nothing is lost beyond the current
chunk."

**The fix.** Broaden the `beforeunload` condition to also cover a non-empty
upload queue, non-empty failed-chunks list, or an in-flight queue drain.

**How to verify.** Manual: stop Whisper, record two chunks, press Stop, attempt
to close the tab — the browser must prompt.

---

## Wave 3 — Failure visibility and disclosure

### 7. The failed-chunk retry path is healthy, and could not have fired for this bug

The existing `_liveFailedChunks` / Retry-button mechanism works correctly on its
own terms, but it is structurally incapable of reporting a *capture-side*
failure (item 1) — that failure happens before any blob exists, so the upload
queue and retry list both stay empty and are never consulted. A well-built
safety net for uploads, none at all for capture — item 1c is what closes that
gap. Worth a bound on how many failed chunks are retained in memory (they hold
full audio blobs), but low priority for a single-GM deployment.

### 8. Background-tab throttling causes chunk-length drift, not this bug

A backgrounded tab can have its `setTimeout` chain throttled, stretching a
chunk's actual length well past the configured value — a real effect, but it
produces *longer chunks*, not a silent stop, so it doesn't reproduce "only
chunk 1 saved" by itself. It is a plausible *trigger* for the stream death
behind item 1 on mobile, where the OS may tear down the capture pipeline
entirely rather than merely slow timers. Once item 1 lands, a background-caused
stream death becomes a visible reconnect instead of silence. Also worth one
added sentence in the panel's own copy: keep the tab in the foreground.

### 9. Same unguarded-promise shape in both one-shot mic buttons

The AI Chat mic-attach button and the session audio-recap mic button share
`ndMicRecorder`'s `start()` via the identical `.then(started => ...)` pattern
with no `.catch()`. Fix 1a already covers their exposure (neither passes an
`existingStream`, so a `getUserMedia` failure was already caught before; after
1a, a constructor/`start()` throw also routes to their existing `onError`
handlers). Add `.catch()` to both anyway for consistency — low priority, since
a one-shot button stuck on "🎤 Record" is far milder than a multi-hour
recording dying silently.

### 10. Panel copy overstates the guarantee

"Nothing is lost beyond the current chunk if your browser or connection drops"
is broken by items 1, 4, and 6 as they stand. Re-check this line once Wave 1
and 2 land — it should be accurate again at that point.

---

## Wave 4 — Test coverage

### 11. No coverage of segment chaining — only of the upload/retry queue

All four existing live-recording test files
(`test_live_recording_audio_ui.py`, `test_live_recording_failed_chunks.py`,
`test_live_recording_persistent_mic.py`, `test_live_recording_wake_lock.py`)
are JS-source assertion tests (fetch the rendered page, assert on substrings) —
this repo's established convention for template JS. None of them exercises
what happens when a segment fails to start, which is why item 1 was invisible
to the suite.

**Plan:**
1. `tests/test_live_recording_segment_chain.py` (JS-source) — pin the shape of
   the Wave 1/2 fixes: try/catch around `MediaRecorder` construction and
   `start()`, `onerror` wiring, `.catch(` on the segment-start promise, the
   recovery handler and its bounded attempt counter, `track.onended` wiring,
   the `finally`-protected queue-busy flag, the Start re-entrancy guard, the
   broadened `beforeunload` condition.
2. `tests/test_live_recording_append_idempotency.py` (real pytest route tests,
   item 3) — the one finding that is fully server-side and therefore properly
   testable end to end.

Inherently manual-only (documented as a checklist next to the recovery
handler, not as tests): real `getUserMedia` permission re-prompt behavior,
actual mobile browser backgrounding, Bluetooth headset reconnects, and the
mic-indicator lifecycle. Item 1's DevTools repro
(`_liveMicStream.getAudioTracks()[0].stop()`) is the closest thing to a
deterministic manual reproduction.

---

## Landing order

1. Item 1 — the GM's data loss.
2. Item 2 — same code path, completes the story.
3. Items 3, 4 — transcript integrity (4 is a partial trigger for 3).
4. Items 5, 6 — races and the close-warning gap.
5. Items 9, 10 — consistency and disclosure.
6. Item 11 — tests, landing alongside item 1 rather than after.
