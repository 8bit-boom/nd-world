"""Regression test for plan item QoL 3.6: keep the screen awake during a
Live Session Recording (app/templates/sessions/detail.html). Already
implemented — liveAcquireWakeLock()/liveReleaseWakeLock() wrap the
navigator.wakeLock API (best-effort; no-ops on unsupported browsers), wired
into toggleLiveRecording()'s start/stop, re-acquired on visibilitychange
(the browser releases a wake lock whenever the tab is hidden), and paired
with a beforeunload warning so a GM can't accidentally navigate away
mid-recording. This test just locks in that it stays wired — no new code."""
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


def test_live_recording_acquires_and_releases_a_wake_lock(client, seed):
    sid = _make_session(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    assert "async function liveAcquireWakeLock()" in r.text
    assert "navigator.wakeLock.request('screen')" in r.text
    assert "function liveReleaseWakeLock()" in r.text
    # Acquired on start, released on stop.
    assert "liveAcquireWakeLock();" in r.text
    assert "liveReleaseWakeLock();" in r.text
    # Re-acquired when the tab regains visibility — the browser silently
    # releases any wake lock the moment a tab is hidden.
    assert "visibilitychange" in r.text
    assert "liveAcquireWakeLock()" in r.text.split("visibilitychange", 1)[1][:300]
    # Warn before an accidental navigation away mid-recording.
    assert "beforeunload" in r.text
