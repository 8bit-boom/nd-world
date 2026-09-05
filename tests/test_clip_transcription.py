"""Tests for AI transcript + subtitle generation on Audio/Video library
clips — POST /audio/{id}/transcribe and /video/{id}/transcribe (app/
routers/audio.py, app/routers/video.py), backed by app.ai.
transcribe_audio_with_subtitles and its verbose-json/VTT helpers. Whisper
itself is never actually called here (see tests/test_whisper.py for that
kind of coverage) — these monkeypatch transcribe_audio_with_subtitles the
same way tests/test_audio_jobs.py monkeypatches transcribe_audio.
"""
import app.ai as ai_module
from app.database import SessionLocal
from app.models import AudioClip, VideoClip, World

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


# ── Segment/VTT helpers (app.ai) ─────────────────────────────────────────────

def test_format_vtt_timestamp():
    assert ai_module._format_vtt_timestamp(0) == "00:00:00.000"
    assert ai_module._format_vtt_timestamp(65.25) == "00:01:05.250"
    assert ai_module._format_vtt_timestamp(3661.001) == "01:01:01.001"
    assert ai_module._format_vtt_timestamp(-5) == "00:00:00.000"


def test_segments_to_vtt_basic():
    segments = [
        {"start": 0.0, "end": 1.5, "text": "Hello there."},
        {"start": 1.5, "end": 3.0, "text": "General Kenobi."},
    ]
    vtt = ai_module._segments_to_vtt(segments)
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.500" in vtt
    assert "Hello there." in vtt
    assert "General Kenobi." in vtt


def test_segments_to_vtt_skips_blank_segments():
    segments = [{"start": 0.0, "end": 1.0, "text": "   "}, {"start": 1.0, "end": 2.0, "text": "Real line"}]
    vtt = ai_module._segments_to_vtt(segments)
    assert vtt.count("-->") == 1
    assert "Real line" in vtt


def test_collapse_repeated_segments_collapses_long_run():
    segments = [{"start": float(i), "end": float(i + 1), "text": "loop"} for i in range(6)]
    out = ai_module._collapse_repeated_segments(segments, min_repeat=4)
    assert len(out) == 1
    assert out[0]["start"] == 0.0
    assert out[0]["end"] == 6.0
    assert out[0]["text"] == "loop"


def test_collapse_repeated_segments_leaves_short_run_alone():
    segments = [{"start": 0.0, "end": 1.0, "text": "hi"}, {"start": 1.0, "end": 2.0, "text": "hi"}]
    out = ai_module._collapse_repeated_segments(segments, min_repeat=4)
    assert len(out) == 2


# ── Audio: POST /audio/{id}/transcribe ───────────────────────────────────────

def test_audio_transcribe_gm_success(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    url = _write_clip_file(monkeypatch, audio_module, tmp_path, "audio", "clip1.mp3")
    cid = _add_audio_clip(seed.world_a.id, file_url=url)

    async def fake(path, glossary="", language="", denoise=False):
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

    async def fake(path, glossary="", language="", denoise=False):
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


def test_audio_transcribe_whisper_error_returns_400(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    url = _write_clip_file(monkeypatch, audio_module, tmp_path, "audio", "clip1.mp3")
    cid = _add_audio_clip(seed.world_a.id, file_url=url)

    async def fake(path, glossary="", language="", denoise=False):
        raise ai_module.WhisperError("could not reach whisper")
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", fake)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/transcribe")
    assert r.status_code == 400
    assert "could not reach whisper" in r.text.lower()


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


def test_audio_transcribe_uses_world_whisper_settings(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    url = _write_clip_file(monkeypatch, audio_module, tmp_path, "audio", "clip1.mp3")
    cid = _add_audio_clip(seed.world_a.id, file_url=url)

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.whisper_language = "ru"
        w.whisper_denoise = True
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake(path, glossary="", language="", denoise=False):
        captured["language"] = language
        captured["denoise"] = denoise
        return "Привет.", ""
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", fake)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/transcribe")
    assert r.status_code == 200
    assert captured["language"] == "ru"
    assert captured["denoise"] is True


# ── Video: POST /video/{id}/transcribe ───────────────────────────────────────

def test_video_transcribe_gm_success(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    url = _write_clip_file(monkeypatch, video_module, tmp_path, "video", "clip1.mp4")
    cid = _add_video_clip(seed.world_a.id, file_url=url)

    async def fake(path, glossary="", language="", denoise=False):
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

    async def fake(path, glossary="", language="", denoise=False):
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
