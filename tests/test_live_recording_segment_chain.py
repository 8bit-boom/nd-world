"""Regression tests for docs/LIVE_RECORDING_AUDIT.md items 1, 2, 4, 5, 6, 9: a
live recording that silently stopped after its first chunk while the UI kept
claiming "Recording…", with no error and no failed-chunk retry prompt.

Root cause (confirmed by an Opus audit — see docs/LIVE_RECORDING_AUDIT.md item
1 for the full writeup): each recording segment shares one long-lived
getUserMedia stream across MediaRecorder instances. If that stream goes
inactive between segments (an OS-level mic reclaim, a Bluetooth reconnect, a
backgrounded tab's capture pipeline being torn down), the *next* segment's
`mediaRecorder.start()` throws synchronously inside `ndMicRecorder.start()` (an
`async` function) — which becomes a rejected promise. `liveStartSegment()`'s
`.then(started => ...)` had no `.catch()`, so the rejection went nowhere: the
chunk timer was never re-armed, `_liveRecording` was never cleared, and the
button/status text kept lying that recording was still in progress.

JS-source assertion test — no browser automation, matching this repo's
established convention for template-JS regression coverage (see
test_live_recording_persistent_mic.py, whose _get_page/_make_session this
file reuses verbatim)."""
from .test_live_recording_persistent_mic import _get_page  # noqa: F401 (reused verbatim)


def test_mic_recorder_construction_and_start_are_guarded(client, seed):
    """base.html's ndMicRecorder.start(): with a reused (existingStream)
    stream, the getUserMedia try/catch is skipped entirely — the constructor
    and .start() call that actually run for every live-recording segment must
    have their own guard, or a synchronous throw there becomes an unhandled
    promise rejection."""
    page = _get_page(client, seed)
    body = page.split("mediaRecorder = new MediaRecorder(stream);", 1)[1][:1200]
    assert "mediaRecorder.start();" in body
    assert "} catch (e) {" in body
    assert "mediaRecorder = null;" in body
    # The block leading up to the constructor must actually be inside a try.
    preamble = page.split("mediaRecorder = new MediaRecorder(stream);", 1)[0][-400:]
    assert "try {" in preamble
    # The catch must actually report the failure and signal "did not start" —
    # not just swallow it.
    catch_body = body.split("} catch (e) {", 1)[1][:200]
    assert "onError(e)" in catch_body
    assert "return false;" in catch_body


def test_mic_recorder_wires_an_onerror_handler(client, seed):
    """A stream that goes inactive mid-recording (not just at a segment
    boundary) fires MediaRecorder's own `error` event — without a handler
    that failure was invisible until the next scheduled stop()."""
    page = _get_page(client, seed)
    body = page.split("mediaRecorder = new MediaRecorder(stream);", 1)[1][:1200]
    assert "mediaRecorder.onerror = function (e) {" in body
    assert "onError(" in body.split("mediaRecorder.onerror = function (e) {", 1)[1][:200]


def test_live_start_segment_catches_a_rejected_start_promise(client, seed):
    """The actual bug: liveStartSegment's .then(...) had no .catch(), so a
    rejected start() promise (see the ndMicRecorder tests above) vanished —
    the chunk timer was never re-armed and _liveRecording was never cleared."""
    page = _get_page(client, seed)
    segment_body = page.split("function liveStartSegment()", 1)[1][:2600]
    assert ".catch(err => liveHandleMicFailure(err))" in segment_body
    # The non-throwing failure path (started === false) must also route
    # through the recorder's own onError rather than duplicating error UI —
    # liveStartSegment's onError callback IS liveHandleMicFailure.
    assert "(err) => liveHandleMicFailure(err)" in segment_body


def test_mic_failure_handler_recovers_with_a_bounded_ladder(client, seed):
    """liveHandleMicFailure restores the self-healing that the shared
    _liveMicStream removed (see DYNAMIC_THINKING_AND_PIPELINE_PLAN.md item
    3.1) — but ONLY on the failure path, and only up to a bounded number of
    attempts, with a sticky, visible error once it gives up."""
    page = _get_page(client, seed)
    assert "async function liveHandleMicFailure(err)" in page
    assert "const LIVE_MAX_RECOVER_ATTEMPTS = 3;" in page
    handler_body = page.split("async function liveHandleMicFailure(err)", 1)[1][:1800]
    assert "_liveRecoverAttempts >= LIVE_MAX_RECOVER_ATTEMPTS" in handler_body
    assert "navigator.mediaDevices.getUserMedia(" in handler_body
    assert "_liveLastError" in handler_body
    # Giving up must reset the button, not leave it claiming "Stop Recording"
    # forever.
    assert "'🔴 Start Recording'" in handler_body


def test_mic_track_ended_is_watched_mid_segment(client, seed):
    """Without this, a mic dying mid-segment is only noticed at the NEXT
    chunk boundary — up to 15 minutes away at this panel's longest chunk
    length setting."""
    page = _get_page(client, seed)
    assert "function liveWatchMicTracks()" in page
    watch_body = page.split("function liveWatchMicTracks()", 1)[1][:500]
    assert "getAudioTracks()" in watch_body
    assert "t.onended = ()" in watch_body
    assert "liveHandleMicFailure(" in watch_body
    # And it must actually be armed when a recording starts.
    start_body = page.split("async function toggleLiveRecording()", 1)[1]
    assert "liveWatchMicTracks();" in start_body


def test_sticky_mic_error_is_surfaced_by_status_refresh(client, seed):
    """liveRefreshStatus is the single source of truth for the status line —
    a sticky post-give-up error must render there, not just get set once and
    risk being silently overwritten by the next refresh."""
    page = _get_page(client, seed)
    status_body = page.split("function liveRefreshStatus()", 1)[1][:900]
    assert "_liveLastError" in status_body


def test_stop_checks_is_recording_before_treating_the_chain_as_alive(client, seed):
    """docs/LIVE_RECORDING_AUDIT.md item 2: after a failed segment start, the
    recorder object is truthy but not actually recording — calling .stop() on
    it silently no-ops (onStop never fires), which used to leave the mic
    hot forever and the status stuck on 'finishing the last chunk…'."""
    page = _get_page(client, seed)
    stop_body = page.split("async function toggleLiveRecording()", 1)[1][:900]
    assert "_liveCurrentRecorder && _liveCurrentRecorder.isRecording()" in stop_body
    assert "liveStopMicStream();" in stop_body


def test_start_has_a_reentrancy_guard_against_double_click(client, seed):
    """docs/LIVE_RECORDING_AUDIT.md item 5: toggleLiveRecording awaits
    getUserMedia before _liveRecording is set — a second click in that
    window used to start a second independent recording chain sharing (and
    colliding with) the first's _liveMicStream/_liveRecordingId."""
    page = _get_page(client, seed)
    assert "let _liveStarting = false;" in page
    start_body = page.split("async function toggleLiveRecording()", 1)[1]
    assert "if (_liveStarting) return;" in start_body
    assert "_liveStarting = true;" in start_body


def test_queue_busy_flag_is_cleared_in_a_finally(client, seed):
    """docs/LIVE_RECORDING_AUDIT.md item 4: an unexpected throw inside
    liveProcessQueue's drain loop (outside the per-chunk upload try) used to
    leave _liveQueueBusy stuck true forever, silently wedging every future
    chunk behind this function's own early-return guard."""
    page = _get_page(client, seed)
    queue_body = page.split("async function liveProcessQueue()", 1)[1][:3300]
    assert "} finally {" in queue_body
    finally_body = queue_body.split("} finally {", 1)[1][:500]
    assert "_liveQueueBusy = false;" in finally_body


def test_transcript_display_is_rendered_outside_the_upload_retry_try(client, seed):
    """A rendering bug inside the old try block would be caught by the same
    catch as an upload failure, triggering a needless re-upload of a chunk
    the server already saved — the exact duplicate-append trigger item 3
    guards against server-side."""
    page = _get_page(client, seed)
    queue_body = page.split("async function liveProcessQueue()", 1)[1][:3300]
    assert "if (uploaded) {" in queue_body
    render_guard = queue_body.split("if (uploaded) {", 1)[1][:200]
    assert "liveSetTranscriptDisplay(transcriptText)" in render_guard


def test_fire_and_forget_queue_calls_have_a_catch(client, seed):
    page = _get_page(client, seed)
    assert page.count("liveProcessQueue().catch(e => console.error('live upload queue', e));") == 2


def test_beforeunload_also_guards_a_pending_upload_queue(client, seed):
    """docs/LIVE_RECORDING_AUDIT.md item 6: Stop clears _liveRecording
    immediately even though the final segment (or a failed chunk awaiting
    Retry) can still be unsaved for a while afterward — closing the tab in
    that window used to lose it with no warning at all."""
    page = _get_page(client, seed)
    assert "_liveRecording || _liveQueue.length || _liveFailedChunks.length || _liveQueueBusy" in page


def test_one_shot_mic_buttons_also_catch_a_rejected_start(client, seed):
    """docs/LIVE_RECORDING_AUDIT.md item 9: the two one-shot 🎤 buttons share
    ndMicRecorder's now-guarded start(), but their own .then() chains still
    need a .catch() for consistency (they never pass existingStream, so their
    exposure was always narrower, but the shape should match)."""
    page = _get_page(client, seed)
    assert "rec.start().then(function (started) {" in page
    attach_body = page.split("rec.start().then(function (started) {", 1)[1][:400]
    assert "}).catch(function (err) {" in attach_body

    assert "_micRecorder.start().then(started => {" in page
    session_mic_body = page.split("_micRecorder.start().then(started => {", 1)[1][:400]
    assert "}).catch(err => {" in session_mic_body
