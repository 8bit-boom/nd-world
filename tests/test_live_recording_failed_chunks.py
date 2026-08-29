"""Regression test for docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2
item 2.4: Live Recording (app/templates/sessions/detail.html) previously
permanently discarded a chunk after 3 failed upload attempts
(`_liveQueue.shift()` unconditionally), and the status line got stuck on
"Stopped — finishing the last chunk…" forever once nothing was left to
finish (the queue-drain path only ever rewrote status while `_liveRecording`
was still true). This is a JS-source assertion test — no browser
automation, matching this file's established convention for template-JS
regression coverage (see test_live_recording_wake_lock.py)."""
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


def test_failed_chunks_are_retained_not_discarded(client, seed):
    page = _get_page(client, seed)
    assert "_liveFailedChunks" in page
    # The old unconditional shift-and-forget behavior must be gone.
    assert "Lost a ~1min chunk after 3 failed upload attempts" not in page
    assert "_liveFailedChunks.push(file)" in page


def test_retry_button_requeues_failed_chunks(client, seed):
    page = _get_page(client, seed)
    assert "function liveRetryFailedChunks()" in page
    assert "_liveQueue.push(..." in page or "_liveQueue.push(...chunks)" in page
    assert "retryBtn.onclick = liveRetryFailedChunks" in page


def test_status_shows_retry_affordance_when_chunks_failed(client, seed):
    page = _get_page(client, seed)
    assert "chunk(s) failed to upload" in page


def test_status_settles_to_a_final_message_once_idle(client, seed):
    """The old bug: after Stop, status stayed "finishing the last chunk…"
    forever once the queue actually drained. liveRefreshStatus must be the
    one place status settles once nothing is uploading."""
    page = _get_page(client, seed)
    assert "function liveRefreshStatus()" in page
    assert "'Stopped — transcript saved.'" in page
    # Called at the end of the upload loop, not just under `if (_liveRecording)`.
    assert "liveRefreshStatus();" in page.split("async function liveProcessQueue", 1)[1][:2000]


def test_backlog_is_shown_while_still_recording(client, seed):
    page = _get_page(client, seed)
    assert "chunk(s) waiting for Whisper" in page
