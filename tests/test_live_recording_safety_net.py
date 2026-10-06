"""Live session recording: the safety net added after a GM reported "it recorded only about 3/4" - the END of the session
was missing. The capture chain itself was verified lossless (a fake microphone for 200 s gave ~99% of the wall-clock
time); what lost the end was everything around it: "Stopped - transcript saved." was printed the instant Stop was pressed
while the last chunk was not even cut and a backlog could still be waiting in the tab, unsent chunks lived only in memory,
a mic that vanished was given up on after 3 tries (6 seconds) with a grey status line, a computer that slept recorded
nothing and said nothing, and a muted microphone "recorded" silence.

JS-source assertions against the rendered session page, like the other live-recording tests (the decisions themselves are
pure functions tested under Node in tests/test_live_health.py; the whole flow was also driven in a real browser with a
fake microphone)."""
from app.database import SessionLocal
from app.models import GameSession

from .conftest import GM_PASSWORD, login


def _get_page(client, seed):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        sid = gs.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    return r.text


def _body(page, marker, size):
    return page.split(marker, 1)[1][:size]


# ── the modules and the markup ───────────────────────────────────────────────────────────────────────

def test_the_page_loads_the_health_and_store_modules_before_its_own_script(client, seed):
    page = _get_page(client, seed)
    for name in ("live-health.js", "live-store.js"):
        assert f'src="/static/js/{name}?v=' in page
        assert page.index(f"/static/js/{name}") < page.index("function toggleLiveRecording")
        assert client.get(f"/static/js/{name}").status_code == 200


def test_the_panel_has_the_alert_banner_the_recovery_banner_and_the_mic_meter(client, seed):
    page = _get_page(client, seed)
    for marker in ('id="live-alert"', 'role="alert"', 'id="live-recover"', 'id="live-level"', 'id="live-mic-warning"'):
        assert marker in page
    # the old unconditional message is gone: "saved" now comes from the counters, once it is true
    assert "Stopped — transcript saved." not in page


# ── "saved" is only said when it is true ─────────────────────────────────────────────────────────────

def test_stop_marks_the_last_chunk_as_not_cut_yet_and_it_is_cleared_once_queued(client, seed):
    page = _get_page(client, seed)
    stop = _body(page, "async function toggleLiveRecording()", 1800)
    assert stop.index("_liveFinalizing = true;") < stop.index("_liveCurrentRecorder.stop();")
    segment = _body(page, "function liveStartSegment()", 5200)
    # cleared only AFTER the final chunk is in the queue, never before
    assert segment.index("_liveQueue.push(segFile);") < segment.index("_liveFinalizing = false;")
    assert "!_liveRecording" in segment.split("_liveQueue.push(segFile);", 1)[1][:200]


def test_a_recorder_that_never_finishes_the_last_chunk_raises_an_alert_instead_of_hanging(client, seed):
    page = _get_page(client, seed)
    stop = _body(page, "async function toggleLiveRecording()", 1800)
    assert "The last chunk could not be finished" in stop


def test_the_done_message_is_only_announced_when_everything_is_transcribed(client, seed):
    page = _get_page(client, seed)
    fn = _body(page, "function liveAnnounceIfDone()", 1400)
    for guard in ("_liveRecording", "_liveFinalizing", "_liveQueue.length", "_liveQueueBusy", "_liveFailedChunks.length"):
        assert guard in fn.split("_liveDoneAnnounced", 1)[0]
    assert "NOT recorded" in fn      # stretches the computer slept through / the mic was gone are named, not hidden
    # the queue's own `finally` is where it is called from
    assert "liveAnnounceIfDone();" in _body(page, "async function liveProcessQueue()", 9000)


def test_a_failed_upload_raises_a_loud_alert_not_just_a_status_line(client, seed):
    page = _get_page(client, seed)
    queue = _body(page, "async function liveProcessQueue()", 9000)
    parked = queue.split("_liveFailedChunks.push(file);", 1)[1][:400]
    assert "liveRaiseAlert(" in parked and "kept in this browser" in parked


def test_a_held_status_message_is_put_back_instead_of_going_stale(client, seed):
    """'Can't reach the server - retrying in 20 s' must stay on the line while true, even if Stop or the last chunk's
    arrival wrote over it; and a success / a drained queue releases it."""
    page = _get_page(client, seed)
    refresh = _body(page, "function liveRefreshStatus(tick)", 700)
    assert "_liveSayText" in refresh and "Date.now() < _liveSayUntil" in refresh
    queue = _body(page, "async function liveProcessQueue()", 9000)
    assert queue.count("_liveSayUntil = 0;") >= 2


# ── nothing exists only in memory ────────────────────────────────────────────────────────────────────

def test_every_chunk_is_copied_to_the_browser_store_before_it_is_queued_and_removed_once_the_server_has_it(client, seed):
    page = _get_page(client, seed)
    segment = _body(page, "function liveStartSegment()", 5200)
    assert segment.index("_liveStore.put({") < segment.index("_liveQueue.push(segFile);")
    assert "blob: blob" in segment
    queue = _body(page, "async function liveProcessQueue()", 9000)
    uploaded = queue.split("if (uploaded) {", 1)[1][:700]
    assert "_liveStore.remove(file.ndStoreKey)" in uploaded


def test_leftover_chunks_are_offered_on_the_next_visit_and_keep_their_original_slot(client, seed):
    page = _get_page(client, seed)
    fn = _body(page, "async function liveRecoverUnsent()", 2800)
    assert "_liveStore.list(SESSION_ID)" in fn
    assert "never reached the server" in fn and "Send them now" in fn and "Discard" in fn
    # the server's idempotency key is (recording_id, segment_index): resending a chunk it already has replaces, never duplicates
    assert "f.ndRecordingId = r.recordingId; f.ndSegmentIndex = r.index;" in fn
    # chunks this very page already has in hand are not offered back
    assert "inHand" in fn
    assert "\nif (_liveStore) liveRecoverUnsent();" in page


def test_closing_the_tab_is_warned_about_until_the_last_chunk_is_cut_too(client, seed):
    page = _get_page(client, seed)
    unload = _body(page, "window.addEventListener('beforeunload'", 900)
    assert "_liveFinalizing" in unload


# ── the microphone ───────────────────────────────────────────────────────────────────────────────────

def test_a_lost_microphone_raises_an_alert_at_once_and_keeps_trying_for_about_half_an_hour(client, seed):
    page = _get_page(client, seed)
    fn = _body(page, "async function liveHandleMicFailure(err)", 5200)
    assert "liveRaiseAlert('⚠ The microphone stopped" in fn
    assert "ndLiveHealth.reconnectDelayMs(_liveRecoverAttempts)" in fn
    assert "const LIVE_RECOVER_BUDGET = ndLiveHealth.RECONNECT_ATTEMPTS;" in page
    assert "_liveGaps.push(" in fn and "that stretch was not recorded" in fn
    # giving up is loud, resets the button, and says what happened to what was captured before
    assert "Recording STOPPED" in fn and "still being transcribed" in fn
    # Stop pressed during a long pause is answered at once (the wait is sliced), not after the pause
    assert "waited += 500" in fn and "_liveRecording; waited" in fn


def test_a_muted_microphone_and_a_silent_one_are_both_flagged(client, seed):
    page = _get_page(client, seed)
    watch = _body(page, "function liveWatchMicTracks()", 1400)
    assert "t.onmute" in watch and "t.onunmute" in watch
    meter = _body(page, "function liveStartMeter(stream)", 2600)
    assert "createSilenceMonitor(" in meter and "justBecameSilent" in meter and "No sound from the microphone" in meter
    # started with the recording and again after a reconnect (a new stream), stopped with the mic
    assert "liveStartMeter(_liveMicStream);" in _body(page, "async function toggleLiveRecording()", 6000)
    assert "liveStartMeter(_liveMicStream);" in _body(page, "async function liveHandleMicFailure(err)", 5200)
    assert "liveStopMeter();" in _body(page, "function liveStopMicStream()", 120)


def test_a_sleeping_computer_is_noticed_and_a_dead_recorder_is_restarted(client, seed):
    page = _get_page(client, seed)
    beat = _body(page, "function liveBeat()", 1300)
    assert "ndLiveHealth.detectPause(" in beat and "ndLiveHealth.pauseMessage(" in beat and "_liveGaps.push(" in beat
    assert "_liveRecorderIdle >= 4" in beat and "liveStartSegment();" in beat
    assert "setInterval(liveBeat, 1000)" in page


def test_start_clears_the_previous_alert_and_asks_for_notification_permission_inside_the_click(client, seed):
    page = _get_page(client, seed)
    start = _body(page, "async function toggleLiveRecording()", 6000).split("_liveStarting = true;", 1)[1]
    assert "Notification.requestPermission()" in start[:700]
    assert "liveClearAlert();" in start
    assert "_liveGaps = [];" in start


def test_the_recorded_clock_includes_the_chunk_being_recorded(client, seed):
    """Otherwise a one-minute chunk length shows 'Recording... 0:00' for the whole first minute."""
    page = _get_page(client, seed)
    state = _body(page, "function liveHealthState()", 900)
    assert "_liveSegStartedAt" in state and "_liveRecovering" in state


# ── no internet: recording carries on, chunks wait in the browser, uploads resume by themselves ──────

def test_a_connection_that_is_down_is_never_a_deadline_and_never_parks_a_chunk(client, seed):
    """The capture is local (MediaRecorder), so recording never needed the network; what used to end was the upload:
    after ~9 minutes of failed fetches a chunk was parked behind a manual Retry button."""
    page = _get_page(client, seed)
    queue = _body(page, "async function liveProcessQueue()", 12000)
    assert "const offlineErr =" in queue
    # an unreachable server is dealt with BEFORE the capped transient-wait counter is ever consulted
    assert queue.index("if (offlineErr) {") < queue.index("transientWaits++ < MAX_TRANSIENT_WAITS")
    offline = queue.split("if (offlineErr) {", 1)[1][:900]
    assert "_liveOffline = true;" in offline and "await liveWaitOrOnline(" in offline and "continue;" in offline
    assert "_liveFailedChunks.push" not in offline


def test_the_wait_ends_the_moment_the_browser_is_back_online(client, seed):
    page = _get_page(client, seed)
    fn = _body(page, "function liveWaitOrOnline(ms)", 700)
    assert "addEventListener('online'" in fn and "removeEventListener('online'" in fn and "setTimeout(" in fn
    # going offline aborts an upload that would otherwise hang until a TCP timeout, so it is retried cleanly on 'online'
    assert "addEventListener('offline'" in page and "_liveUploadAbort" in page
    queue = _body(page, "async function liveProcessQueue()", 12000)
    assert "signal: _liveUploadAbort.signal" in queue and "e.name === 'AbortError'" in queue


def test_a_successful_upload_clears_the_offline_state(client, seed):
    page = _get_page(client, seed)
    queue = _body(page, "async function liveProcessQueue()", 12000)
    uploaded = queue.split("if (uploaded) {", 1)[1][:900]
    assert "_liveOffline = false;" in uploaded and "_liveOfflineSince = 0;" in uploaded


def test_the_counters_know_when_the_browser_is_offline_even_between_chunks(client, seed):
    page = _get_page(client, seed)
    state = _body(page, "function liveHealthState()", 1200)
    assert "offline: _liveOffline || navigator.onLine === false" in state


def test_a_long_outage_raises_a_reminder_that_the_audio_only_lives_in_this_browser(client, seed):
    page = _get_page(client, seed)
    beat = _body(page, "function liveBeat()", 2400)
    assert "_liveOfflineAlerted" in beat and "only in this browser" in beat
