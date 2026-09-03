"""Template wiring for the Live Session Recording panel's GM-configurable
chunk length and opt-in raw-audio archive (app/templates/sessions/
detail.html): the chunk-length select, the "Save raw audio" checkbox, both
controls' localStorage persistence, the per-upload archive tagging in the
upload queue, and the saved-audio status/download line. This is a JS-source
assertion test — no browser automation, matching this area's established
convention for template-JS regression coverage
(test_live_recording_failed_chunks.py, test_live_recording_wake_lock.py)."""
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


def test_chunk_length_selector_offers_all_durations(client, seed):
    page = _get_page(client, seed)
    sel = page.split('id="live-chunk-seconds"', 1)[1][:1500]
    for value, label in (("60", "1 min"), ("120", "2 min"), ("180", "3 min"),
                         ("300", "5 min"), ("600", "10 min"), ("900", "15 min")):
        assert f'value="{value}"' in sel
        assert label in sel


def test_chunk_length_persists_to_local_storage(client, seed):
    page = _get_page(client, seed)
    # Both directions: restored on load, saved on change.
    assert "localStorage.getItem('liveChunkSeconds')" in page
    assert "localStorage.setItem('liveChunkSeconds'" in page


def test_chunk_length_is_no_longer_a_hardcoded_const(client, seed):
    """The old `const LIVE_CHUNK_SECONDS = 60` must be gone, and the segment
    timer must read the CURRENT configured value — liveChunkSeconds() is
    called inside the setTimeout arming, i.e. once per segment, so a change
    mid-recording applies from the next segment."""
    page = _get_page(client, seed)
    assert "const LIVE_CHUNK_SECONDS = 60" not in page
    timer = page.split("_liveChunkTimer = setTimeout", 1)[1][:300]
    assert "liveChunkSeconds() * 1000" in timer


def test_save_raw_audio_checkbox_persists_and_defaults_checked(client, seed):
    page = _get_page(client, seed)
    assert '<input type="checkbox" id="live-save-audio" checked' in page
    assert "localStorage.getItem('liveSaveAudio')" in page
    assert "localStorage.setItem('liveSaveAudio'" in page


def test_uploads_carry_the_archive_tags(client, seed):
    """Each segment upload posts save_audio (current checkbox state),
    recording_id (per-recording 32-hex id from chunked-upload.js's shared
    generator), and segment_index — the fields the server's archive path
    keys on."""
    page = _get_page(client, seed)
    upload = page.split("async function liveProcessQueue", 1)[1][:2500]
    assert "fd.append('save_audio'" in upload
    assert "fd.append('recording_id'" in upload
    assert "fd.append('segment_index'" in upload
    # The id generator is available on this page (chunked-upload.js is
    # script-included before the inline block) and used at recording start.
    assert 'src="/static/js/chunked-upload.js"' in page
    assert "_liveRecordingId = ndChunkUploadRandomId();" in page
    assert "_liveSegmentIndex = 0;" in page


def test_raw_audio_status_line_is_wired(client, seed):
    page = _get_page(client, seed)
    assert 'id="live-audio-status"' in page
    assert "async function liveRefreshAudioStatus()" in page
    assert "/live-audio/download" in page
    assert "Raw audio: " in page
    # Refreshed when a recording stops …
    stop = page.split("Stopped — finishing the last chunk…", 1)[1][:300]
    assert "liveRefreshAudioStatus();" in stop
    # … on page load (after the renderPrep/renderLoot init) …
    tail = page.split("renderLoot();", 1)[1]
    assert "liveRefreshAudioStatus();" in tail
    # … and after the post-stop upload flush, but NOT from drains while
    # still recording (that would turn the one-shot refresh into a poll).
    drain = page.split("if (!_liveRecording) liveRefreshAudioStatus();", 1)[0][-800:]
    assert "_liveQueueBusy = false;" in drain
