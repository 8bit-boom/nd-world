"""Tests for AI transcript generation on Audio/Video library clips — POST
/audio/{id}/transcribe and /video/{id}/transcribe (app/routers/audio.py,
app/routers/video.py), backed by app.ai.transcribe_audio_with_subtitles.
Unsloth Studio itself is never called here (see tests/test_stt_pipeline.py for
the pipeline) — these monkeypatch transcribe_audio_with_subtitles the same way
tests/test_audio_jobs.py monkeypatches transcribe_audio. Studio returns plain text
without timestamps, so the second element is normally "" and an existing subtitle
track is left alone.
"""
import app.ai as ai_module
from app.database import SessionLocal
from app.models import AudioClip, VideoClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _add_audio_clip(world_id, **kw):
    db = SessionLocal()
    try:
        c = AudioClip(world_id=world_id, name=kw.pop("name", "Clip"),
                      file_url=kw.pop("file_url", "/uploads/audio/x.mp3"), **kw)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id
    finally:
        db.close()


def _add_video_clip(world_id, **kw):
    db = SessionLocal()
    try:
        c = VideoClip(world_id=world_id, name=kw.pop("name", "Clip"),
                      file_url=kw.pop("file_url", "/uploads/video/x.mp4"), **kw)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id
    finally:
        db.close()


def _write_clip_file(monkeypatch, module, tmp_path, subdir, filename, content=b"fake media"):
    monkeypatch.setattr(module, "_UPLOADS_DIR", tmp_path)
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_bytes(content)
    return f"/uploads/{subdir}/{filename}"


def test_audio_transcribe_gm_success(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    url = _write_clip_file(monkeypatch, audio_module, tmp_path, "audio", "clip1.mp3")
    cid = _add_audio_clip(seed.world_a.id, file_url=url)

    async def fake(path):
        return "Hello world.", "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello world.\n"
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", fake)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/transcribe")
    assert r.status_code == 200

    db = SessionLocal()
    try:
        clip = db.get(AudioClip, cid)
        assert clip.transcript == "Hello world."
        assert "Hello world." in clip.subtitles_vtt
    finally:
        db.close()


def test_audio_transcribe_forbidden_for_player(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    url = _write_clip_file(monkeypatch, audio_module, tmp_path, "audio", "clip1.mp3")
    cid = _add_audio_clip(seed.world_a.id, file_url=url, visible_to_players=True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/transcribe")
    assert r.status_code == 403


def test_audio_transcribe_no_speech_leaves_clip_unchanged(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    url = _write_clip_file(monkeypatch, audio_module, tmp_path, "audio", "clip1.mp3")
    cid = _add_audio_clip(seed.world_a.id, file_url=url)

    async def fake(path):
        return "", ""
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", fake)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/transcribe")
    assert r.status_code == 400
    db = SessionLocal()
    try:
        assert db.get(AudioClip, cid).transcript == ""
    finally:
        db.close()


def test_audio_transcribe_stt_error_returns_400(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    url = _write_clip_file(monkeypatch, audio_module, tmp_path, "audio", "clip1.mp3")
    cid = _add_audio_clip(seed.world_a.id, file_url=url)

    async def fake(path):
        raise ai_module.SttError("could not reach studio")
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", fake)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/transcribe")
    assert r.status_code == 400
    assert "could not reach studio" in r.text.lower()


def test_audio_transcribe_missing_file_404(client, seed, monkeypatch):
    cid = _add_audio_clip(seed.world_a.id, file_url="/uploads/audio/does-not-exist.mp3")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/transcribe")
    assert r.status_code == 404


def test_audio_transcribe_other_world_404(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    url = _write_clip_file(monkeypatch, audio_module, tmp_path, "audio", "clip1.mp3")
    cid = _add_audio_clip(seed.world_b.id, file_url=url)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/transcribe")
    assert r.status_code == 404


def test_audio_transcribe_without_a_track_keeps_the_existing_subtitles(client, seed, tmp_path, monkeypatch):
    """Studio returns text only. Re-running must replace the transcript but not wipe a subtitle track the clip
    already had (it used to be overwritten with an empty string)."""
    import app.routers.audio as audio_module
    url = _write_clip_file(monkeypatch, audio_module, tmp_path, "audio", "clip1.mp3")
    cid = _add_audio_clip(seed.world_a.id, file_url=url, transcript="old", subtitles_vtt="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nold\n")

    async def fake(path):
        return "New words.", ""
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", fake)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/audio/{cid}/transcribe").status_code == 200
    db = SessionLocal()
    try:
        clip = db.get(AudioClip, cid)
        assert clip.transcript == "New words." and "old" in clip.subtitles_vtt
    finally:
        db.close()


# ── Video: POST /video/{id}/transcribe ───────────────────────────────────────

def test_video_transcribe_gm_success(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    url = _write_clip_file(monkeypatch, video_module, tmp_path, "video", "clip1.mp4")
    cid = _add_video_clip(seed.world_a.id, file_url=url)

    async def fake(path):
        return "A cutscene line.", "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nA cutscene line.\n"
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", fake)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/transcribe")
    assert r.status_code == 200

    db = SessionLocal()
    try:
        clip = db.get(VideoClip, cid)
        assert clip.transcript == "A cutscene line."
        assert "A cutscene line." in clip.subtitles_vtt
    finally:
        db.close()


def test_video_transcribe_forbidden_for_player(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    url = _write_clip_file(monkeypatch, video_module, tmp_path, "video", "clip1.mp4")
    cid = _add_video_clip(seed.world_a.id, file_url=url, visible_to_players=True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/transcribe")
    assert r.status_code == 403


def test_video_transcribe_no_speech_leaves_clip_unchanged(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    url = _write_clip_file(monkeypatch, video_module, tmp_path, "video", "clip1.mp4")
    cid = _add_video_clip(seed.world_a.id, file_url=url)

    async def fake(path):
        return "", ""
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", fake)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/transcribe")
    assert r.status_code == 400
    db = SessionLocal()
    try:
        assert db.get(VideoClip, cid).transcript == ""
    finally:
        db.close()


def test_video_transcribe_missing_file_404(client, seed):
    cid = _add_video_clip(seed.world_a.id, file_url="/uploads/video/does-not-exist.mp4")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/transcribe")
    assert r.status_code == 404


def test_video_transcribe_other_world_404(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    url = _write_clip_file(monkeypatch, video_module, tmp_path, "video", "clip1.mp4")
    cid = _add_video_clip(seed.world_b.id, file_url=url)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/transcribe")
    assert r.status_code == 404


# ── Templates render the saved transcript/subtitles ──────────────────────────

def test_audio_library_renders_transcript_and_subtitle_track(client, seed):
    cid = _add_audio_clip(
        seed.world_a.id, file_url="/uploads/audio/x.mp3",
        transcript="A spoken line.", subtitles_vtt="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nA spoken line.\n",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert r.status_code == 200
    assert "A spoken line." in r.text
    assert "data:text/vtt" in r.text
    assert f'id="audio-transcript-{cid}"' in r.text


def test_video_library_renders_transcript_and_subtitle_track(client, seed):
    cid = _add_video_clip(
        seed.world_a.id, file_url="/uploads/video/x.mp4",
        transcript="A cutscene line.", subtitles_vtt="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nA cutscene line.\n",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/video")
    assert r.status_code == 200
    assert "A cutscene line." in r.text
    assert "data:text/vtt" in r.text
    assert 'kind="subtitles"' in r.text
    assert f'id="video-transcript-{cid}"' in r.text


def test_audio_library_hides_transcribe_button_from_player(client, seed):
    _add_audio_clip(seed.world_a.id, file_url="/uploads/audio/x.mp3", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert r.status_code == 200
    # The JS function body always contains this string literal regardless
    # of viewer — check for the rendered <button id="..."> element itself,
    # gated by {% if can_edit(request) %}, not the bare substring.
    assert 'id="audio-transcribe-btn-' not in r.text
