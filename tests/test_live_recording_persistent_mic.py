"""Regression test for docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2
item 3.1: ndMicRecorder (app/templates/base.html) previously called
getUserMedia fresh on every start() and stopped all tracks in onstop, and
the Live Recording loop (sessions/detail.html) created a brand new recorder
per ~60s segment — re-acquiring the mic every minute costs an audible
getUserMedia gap, flickers the browser's mic indicator, and can silently
kill the recording between segments on browsers that re-prompt or fail
re-acquisition after backgrounding (mobile Safari in particular). JS-source
assertion test — no browser automation, matching this repo's established
convention for template-JS regression coverage. Uses the session detail
page (which extends base.html, so ndMicRecorder's own definition is on the
same rendered page) for every assertion."""
from app.database import SessionLocal
from app.models import GameSession

from .conftest import GM_PASSWORD, login


def _make_session(world_id, title="Session 1"):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world_id, title=title, session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _get_page(client, seed):
    sid = _make_session(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    return r.text


def test_ndmicrecorder_accepts_and_reuses_an_existing_stream(client, seed):
    page = _get_page(client, seed)
    assert "function ndMicRecorder(onStop, onError, existingStream)" in page
    assert "var stream = existingStream || null;" in page
    assert "var ownsStream = !existingStream;" in page
    body = page.split("function ndMicRecorder(onStop, onError, existingStream)", 1)[1][:1200]
    assert "if (!stream) {" in body
    assert "await navigator.mediaDevices.getUserMedia(" in body


def test_ndmicrecorder_does_not_stop_tracks_it_does_not_own(client, seed):
    page = _get_page(client, seed)
    body = page.split("function ndMicRecorder(onStop, onError, existingStream)", 1)[1][:1500]
    assert "if (ownsStream) stream.getTracks().forEach" in body


def test_live_recording_acquires_the_stream_once_on_start(client, seed):
    page = _get_page(client, seed)
    assert "let _liveMicStream = null;" in page
    assert "function liveStopMicStream()" in page
    start_body = page.split("async function toggleLiveRecording()", 1)[1]
    assert "_liveMicStream = await navigator.mediaDevices.getUserMedia(" in start_body


def test_live_recording_passes_the_shared_stream_into_every_segment(client, seed):
    page = _get_page(client, seed)
    segment_body = page.split("function liveStartSegment()", 1)[1][:1200]
    assert "_liveMicStream," in segment_body  # passed as ndMicRecorder's 3rd arg


def test_live_recording_only_releases_the_mic_after_the_final_segment(client, seed):
    """The stream must stay alive through the in-flight segment's own
    stop()/onstop flush — released from liveStartSegment's onStop callback
    once there's no next segment to start, not synchronously when Stop is
    clicked."""
    page = _get_page(client, seed)
    segment_body = page.split("function liveStartSegment()", 1)[1][:1200]
    assert "liveStopMicStream();  // that was the final segment" in segment_body


def test_one_shot_mic_button_keeps_self_contained_behavior(client, seed):
    """The one-shot 🎤 Record button (used elsewhere, e.g. Ask AI panels)
    must keep calling ndMicRecorder with no existingStream argument — it
    still owns and releases its own stream per use."""
    page = _get_page(client, seed)
    assert "ndMicRecorder(onStop, onError, existingStream)" in page
