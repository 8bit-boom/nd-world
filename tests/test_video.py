"""Tests for the /video library (app/routers/video.py, VideoClip in
app/models.py) — mirrors tests/test_audio.py almost line for line, since
video.py itself mirrors audio.py's album-tree/visibility/upload shape.
GET /video is player-safe (see /video and /video/albums/{id} in
_is_player_safe); upload/edit/delete/album-management stay GM-only,
enforced both by the middleware (no POST /video/* entry there) and by an
explicit check in each handler.
"""
import io
import shutil
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import VideoAlbum, VideoClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 500


def _mp4_file(name="clip.mp4"):
    return {"file": (name, io.BytesIO(_MP4_BYTES), "video/mp4")}


def _add_album(world_id, **kw):
    db = SessionLocal()
    try:
        a = VideoAlbum(world_id=world_id, name=kw.pop("name", "Album"), **kw)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id
    finally:
        db.close()


def _add_clip(world_id, **kw):
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


def test_video_upload_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload", data={"name": "Intro Cutscene", "description": "Opening crawl"},
                     files=_mp4_file(), follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        clips = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).all()
        assert len(clips) == 1
        assert clips[0].name == "Intro Cutscene"
        assert clips[0].description == "Opening crawl"
        assert clips[0].visible_to_players is False  # checkbox not sent in this request
        assert clips[0].file_url.startswith("/uploads/video/")
    finally:
        db.close()


def test_video_upload_defaults_name_to_filename(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/video/upload", files=_mp4_file("goblin_ambush.mp4"))
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip.name == "goblin_ambush"
    finally:
        db.close()


def test_video_upload_rejects_bad_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload",
                     files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert r.status_code == 400


def test_video_upload_forbidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload", files=_mp4_file())
    assert r.status_code == 403


def test_video_page_reachable_by_gm_and_player(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/video").status_code == 200
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/video").status_code == 200


def test_video_upload_form_shows_configured_size_limit(client, seed, monkeypatch):
    import app.routers.video as video_module

    monkeypatch.setattr(video_module, "_MAX_VIDEO_BYTES", 75 * 1024 * 1024)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/video")
    assert "Up to 75 MB" in r.text


def test_video_upload_rejects_file_over_configured_limit(client, seed, monkeypatch):
    import app.routers.video as video_module

    monkeypatch.setattr(video_module, "_MAX_VIDEO_BYTES", 100)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload", files=_mp4_file())
    assert r.status_code == 413
    db = SessionLocal()
    try:
        assert db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).count() == 0
    finally:
        db.close()


def test_video_upload_file_input_allows_multiple(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/video")
    assert '<input type="file" name="file" id="video-upload-file" accept="video/*" multiple required/>' in r.text


def test_video_bulk_upload_sequential_requests_create_separate_clips(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for fname in ("first_clip.mp4", "second_clip.mp4", "third_clip.mp4"):
        r = client.post(
            "/video/upload",
            data={"description": "Shared batch description", "visible_to_players": "1"},
            files=_mp4_file(fname),
            follow_redirects=False,
        )
        assert r.status_code == 303
    db = SessionLocal()
    try:
        clips = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).order_by(VideoClip.name).all()
        assert [c.name for c in clips] == ["first_clip", "second_clip", "third_clip"]
        assert all(c.description == "Shared batch description" for c in clips)
        assert all(c.visible_to_players for c in clips)
        assert len({c.file_url for c in clips}) == 3  # distinct stored files, no collision
    finally:
        db.close()


def test_video_player_only_sees_visible_clips(client, seed):
    _add_clip(seed.world_a.id, name="Visible Clip", visible_to_players=True)
    _add_clip(seed.world_a.id, name="GM Secret Clip", visible_to_players=False)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/video")
    assert "Visible Clip" in r.text
    assert "GM Secret Clip" in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/video")
    assert "Visible Clip" in r.text
    assert "GM Secret Clip" not in r.text


def test_video_edit_updates_fields(client, seed):
    cid = _add_clip(seed.world_a.id, name="Old Name", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/edit",
                     data={"name": "New Name", "description": "Updated", "visible_to_players": "1"},
                     follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        clip = db.get(VideoClip, cid)
        assert clip.name == "New Name"
        assert clip.description == "Updated"
        assert clip.visible_to_players is True
    finally:
        db.close()


def test_video_edit_forbidden_for_player(client, seed):
    cid = _add_clip(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/edit", data={"name": "Hacked"})
    assert r.status_code == 403


def test_video_delete_removes_row_and_file(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    video_dir = tmp_path / "video"
    video_dir.mkdir()
    f = video_dir / "clip123.mp4"
    f.write_bytes(b"fake video")
    cid = _add_clip(seed.world_a.id, file_url="/uploads/video/clip123.mp4")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not f.exists()
    db = SessionLocal()
    try:
        assert db.get(VideoClip, cid) is None
    finally:
        db.close()


def test_video_delete_also_removes_poster_file(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    video_dir = tmp_path / "video"
    video_dir.mkdir()
    f = video_dir / "clip123.mp4"
    f.write_bytes(b"fake video")
    poster = video_dir / "clip123.jpg"
    poster.write_bytes(b"fake jpg")
    cid = _add_clip(seed.world_a.id, file_url="/uploads/video/clip123.mp4", poster_url="/uploads/video/clip123.jpg")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/video/{cid}/delete")
    assert not f.exists()
    assert not poster.exists()


def test_video_delete_forbidden_for_player(client, seed):
    cid = _add_clip(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/delete")
    assert r.status_code == 403
    db = SessionLocal()
    try:
        assert db.get(VideoClip, cid) is not None
    finally:
        db.close()


def test_video_edit_cross_world_404s(client, seed):
    cid = _add_clip(seed.world_b.id, name="Other World Clip")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/edit", data={"name": "Hijacked"})
    assert r.status_code == 404


# ── Poster-frame generation (ffmpeg, best-effort) ───────────────────────────

def test_video_upload_sets_poster_when_generation_succeeds(client, seed, monkeypatch):
    import app.routers.video as video_module

    async def fake_poster(video_path, dest_dir):
        return f"/uploads/video/{video_path.stem}.jpg"
    monkeypatch.setattr(video_module, "_generate_poster", fake_poster)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/video/upload", files=_mp4_file())
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip.poster_url is not None
        assert clip.poster_url.startswith("/uploads/video/")
        assert clip.poster_url.endswith(".jpg")
    finally:
        db.close()


def test_video_upload_leaves_poster_null_when_generation_fails(client, seed, monkeypatch):
    import app.routers.video as video_module

    async def fake_poster(video_path, dest_dir):
        return None
    monkeypatch.setattr(video_module, "_generate_poster", fake_poster)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/video/upload", files=_mp4_file())
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip.poster_url is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_generate_poster_falls_back_gracefully_when_ffmpeg_missing(tmp_path, monkeypatch):
    """Same graceful-degradation contract as app.ai's _split_audio_into_chunks
    for missing ffmpeg — a poster is a bonus, never a required step."""
    import asyncio as _asyncio_mod
    import app.routers.video as video_module

    async def _raise(*a, **kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(_asyncio_mod, "create_subprocess_exec", _raise)
    video_path = tmp_path / "clip123.mp4"
    video_path.write_bytes(b"fake video")
    result = await video_module._generate_poster(video_path, tmp_path)
    assert result is None
    assert not (tmp_path / "clip123.jpg").exists()


# ── Space-saving AV1 conversion (ffmpeg, best-effort, opt-in per world) ─────

def test_video_settings_save_updates_world(client, seed):
    from app.models import World
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/settings", data={
        "video_convert_enabled": "1", "video_convert_max_height": "720", "video_convert_bitrate_kbps": "1500",
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        assert world.video_convert_enabled is True
        assert world.video_convert_max_height == 720
        assert world.video_convert_bitrate_kbps == 1500
    finally:
        db.close()


def test_video_settings_save_clears_optional_fields_when_blank(client, seed):
    from app.models import World
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.video_convert_enabled = True
        w.video_convert_max_height = 1080
        w.video_convert_bitrate_kbps = 3000
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/video/settings", data={})  # nothing checked/filled in
    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        assert world.video_convert_enabled is False
        assert world.video_convert_max_height is None
        assert world.video_convert_bitrate_kbps is None
    finally:
        db.close()


def test_video_settings_save_forbidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/settings", data={"video_convert_enabled": "1"})
    assert r.status_code == 403


def test_video_settings_panel_shown_on_index_not_inside_an_album(client, seed):
    aid = _add_album(seed.world_a.id, name="Cutscenes")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/video")
    assert "Space-Saving Conversion" in r.text
    r = client.get(f"/video/albums/{aid}")
    assert "Space-Saving Conversion" not in r.text


def test_video_upload_uses_converted_file_when_world_opts_in(client, seed, monkeypatch):
    import app.routers.video as video_module
    from app.models import World

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.video_convert_enabled = True
        w.video_convert_max_height = 720
        w.video_convert_bitrate_kbps = 1500
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_convert(src, dest_dir, max_height, bitrate_kbps):
        captured["max_height"] = max_height
        captured["bitrate_kbps"] = bitrate_kbps
        out = dest_dir / f"{src.stem}-av1.webm"
        out.write_bytes(b"fake av1 output")
        return out
    monkeypatch.setattr(video_module, "_convert_video", fake_convert)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/video/upload", files=_mp4_file())

    assert captured == {"max_height": 720, "bitrate_kbps": 1500}
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip.file_url.endswith("-av1.webm")
    finally:
        db.close()


def test_video_upload_deletes_original_after_successful_conversion(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    from app.models import World
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.video_convert_enabled = True
        db.commit()
    finally:
        db.close()

    seen_src = {}

    async def fake_convert(src, dest_dir, max_height, bitrate_kbps):
        seen_src["path"] = src
        assert src.is_file()  # original still exists at conversion time
        out = dest_dir / f"{src.stem}-av1.webm"
        out.write_bytes(b"fake av1 output")
        return out
    monkeypatch.setattr(video_module, "_convert_video", fake_convert)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/video/upload", files=_mp4_file())

    assert not seen_src["path"].exists()  # original deleted once conversion succeeded


def test_video_upload_keeps_original_when_conversion_disabled(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/video/upload", files=_mp4_file())
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert not clip.file_url.endswith("-av1.webm")
        assert clip.file_url.endswith(".mp4")
    finally:
        db.close()


def test_video_upload_keeps_original_when_conversion_fails(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    from app.models import World
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.video_convert_enabled = True
        db.commit()
    finally:
        db.close()

    async def fake_convert(src, dest_dir, max_height, bitrate_kbps):
        return None  # graceful failure — see _convert_video's contract
    monkeypatch.setattr(video_module, "_convert_video", fake_convert)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload", files=_mp4_file(), follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip.file_url.endswith(".mp4")
        stored = tmp_path / clip.file_url[len("/uploads/"):]
        assert stored.is_file()
    finally:
        db.close()


def test_chunked_upload_also_applies_conversion(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    from app.models import World
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(video_module, "_CHUNKS_ROOT", tmp_path / "video" / "_chunks")

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.video_convert_enabled = True
        db.commit()
    finally:
        db.close()

    async def fake_convert(src, dest_dir, max_height, bitrate_kbps):
        out = dest_dir / f"{src.stem}-av1.webm"
        out.write_bytes(b"fake av1 output")
        return out
    monkeypatch.setattr(video_module, "_convert_video", fake_convert)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "6" * 32
    import io
    r0 = client.post("/video/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files={"file": ("part", io.BytesIO(b"x" * 100), "application/octet-stream")})
    assert r0.status_code == 200
    r = client.post("/video/upload/complete", data={
        "upload_id": upload_id, "filename": "clip.mp4", "total_chunks": "1",
    })
    assert r.status_code == 200
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip.file_url.endswith("-av1.webm")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_convert_video_falls_back_gracefully_when_ffmpeg_missing(tmp_path, monkeypatch):
    import asyncio as _asyncio_mod
    import app.routers.video as video_module

    async def _raise(*a, **kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(_asyncio_mod, "create_subprocess_exec", _raise)
    src = tmp_path / "clip123.mp4"
    src.write_bytes(b"fake video")
    result = await video_module._convert_video(src, tmp_path, None, None)
    assert result is None
    assert not (tmp_path / "clip123-av1.webm").exists()
    assert src.is_file()  # untouched — the caller decides whether to delete it, and only on success


@pytest.mark.asyncio
async def test_convert_video_uses_default_bitrate_when_world_unset(tmp_path, monkeypatch):
    import app.routers.video as video_module

    captured_cmd = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        captured_cmd["args"] = args
        # Simulate ffmpeg actually writing the output file.
        out_path = Path(args[-1])
        out_path.write_bytes(b"fake av1 output")
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    src = tmp_path / "clip123.mp4"
    src.write_bytes(b"fake video")
    result = await video_module._convert_video(src, tmp_path, None, None)
    assert result == tmp_path / "clip123-av1.webm"
    assert f"{video_module._DEFAULT_VIDEO_BITRATE_KBPS}k" in captured_cmd["args"]
    assert "-vf" not in captured_cmd["args"]  # no resolution limit configured


@pytest.mark.asyncio
async def test_convert_video_applies_resolution_limit_and_custom_bitrate(tmp_path, monkeypatch):
    import app.routers.video as video_module

    captured_cmd = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        captured_cmd["args"] = args
        out_path = Path(args[-1])
        out_path.write_bytes(b"fake av1 output")
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    src = tmp_path / "clip123.mp4"
    src.write_bytes(b"fake video")
    result = await video_module._convert_video(src, tmp_path, 720, 800)
    assert result == tmp_path / "clip123-av1.webm"
    assert "800k" in captured_cmd["args"]
    vf_index = captured_cmd["args"].index("-vf")
    assert "720" in captured_cmd["args"][vf_index + 1]


@pytest.mark.asyncio
async def test_convert_video_returns_none_on_nonzero_exit(tmp_path, monkeypatch):
    import app.routers.video as video_module

    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"unknown encoder 'libsvtav1'"

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    src = tmp_path / "clip123.mp4"
    src.write_bytes(b"fake video")
    result = await video_module._convert_video(src, tmp_path, None, None)
    assert result is None
    assert not (tmp_path / "clip123-av1.webm").exists()


# ── Non-native containers (MKV/AVI/... accepted, remuxed/transcoded to MP4) ─

def test_video_upload_accepts_mkv_extension(client, seed, monkeypatch):
    import app.routers.video as video_module

    async def fake_remux(src):
        out = src.with_suffix(".mp4")
        out.write_bytes(b"fake remuxed mp4")
        return out
    monkeypatch.setattr(video_module, "_remux_or_transcode", fake_remux)

    async def fake_poster(video_path, dest_dir):
        return None
    monkeypatch.setattr(video_module, "_generate_poster", fake_poster)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload", files=_mp4_file("movie.mkv"), follow_redirects=False)
    assert r.status_code == 303  # accepted — not the 400 a truly unknown ext gets
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip is not None
        assert clip.file_url.endswith(".mp4")  # stored under the converted name
    finally:
        db.close()


def test_video_upload_complete_accepts_mkv_extension(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(video_module, "_CHUNKS_ROOT", tmp_path / "video" / "_chunks")

    async def fake_remux(src):
        out = src.with_suffix(".mp4")
        out.write_bytes(b"fake remuxed mp4")
        return out
    monkeypatch.setattr(video_module, "_remux_or_transcode", fake_remux)

    async def fake_poster(video_path, dest_dir):
        return None
    monkeypatch.setattr(video_module, "_generate_poster", fake_poster)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "7" * 32
    r0 = client.post("/video/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files={"file": ("part", io.BytesIO(b"x" * 100), "application/octet-stream")})
    assert r0.status_code == 200
    r = client.post("/video/upload/complete", data={
        "upload_id": upload_id, "filename": "movie.mkv", "total_chunks": "1",
    })
    assert r.status_code == 200  # accepted — not the 400 a truly unknown ext gets
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip is not None
        assert clip.file_url.endswith(".mp4")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_finish_stored_file_non_native_uses_remux_when_av1_off(seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    from app.models import World

    # In-memory flag only — `w` is passed straight to _finish_stored_file,
    # and committing would expire its attributes while it's detached after
    # close (DetachedInstanceError on the first attribute access).
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.video_convert_enabled = False
    finally:
        db.close()

    dest = tmp_path / "clip123.mkv"
    dest.write_bytes(b"fake mkv")

    async def fake_remux(src):
        out = src.with_suffix(".mp4")
        out.write_bytes(b"fake remuxed mp4")
        return out
    monkeypatch.setattr(video_module, "_remux_or_transcode", fake_remux)

    result = await video_module._finish_stored_file(dest, tmp_path, w)
    assert result.suffix == ".mp4"
    assert result.is_file()
    assert not dest.exists()  # original MKV replaced by the remuxed MP4


@pytest.mark.asyncio
async def test_finish_stored_file_non_native_prefers_av1_when_enabled(seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    from app.models import World

    # In-memory flag only — same detached-after-close reasoning as the
    # av1-off variant above.
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.video_convert_enabled = True
    finally:
        db.close()

    async def fake_convert(src, dest_dir, max_height, bitrate_kbps):
        out = dest_dir / f"{src.stem}-av1.webm"
        out.write_bytes(b"fake av1 output")
        return out
    monkeypatch.setattr(video_module, "_convert_video", fake_convert)

    remux_calls = []

    async def recording_remux(src):
        remux_calls.append(src)  # records any call so the test can assert there was none
        return None
    monkeypatch.setattr(video_module, "_remux_or_transcode", recording_remux)

    dest = tmp_path / "clip123.mkv"
    dest.write_bytes(b"fake mkv")
    result = await video_module._finish_stored_file(dest, tmp_path, w)
    assert remux_calls == []  # AV1 succeeded, so the remux/transcode ladder never ran
    assert result.name.endswith("-av1.webm")
    assert not dest.exists()


@pytest.mark.asyncio
async def test_finish_stored_file_non_native_falls_back_to_remux_when_av1_fails(seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    from app.models import World

    # In-memory flag only — same detached-after-close reasoning as the
    # av1-off variant above.
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.video_convert_enabled = True
    finally:
        db.close()

    async def fake_convert(src, dest_dir, max_height, bitrate_kbps):
        return None  # graceful AV1 failure — see _convert_video's contract
    monkeypatch.setattr(video_module, "_convert_video", fake_convert)

    async def fake_remux(src):
        out = src.with_suffix(".mp4")
        out.write_bytes(b"fake remuxed mp4")
        return out
    monkeypatch.setattr(video_module, "_remux_or_transcode", fake_remux)

    dest = tmp_path / "clip123.mkv"
    dest.write_bytes(b"fake mkv")
    result = await video_module._finish_stored_file(dest, tmp_path, w)
    assert result.suffix == ".mp4"
    assert not dest.exists()


@pytest.mark.asyncio
async def test_finish_stored_file_keeps_original_when_everything_fails(seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    from app.models import World

    # In-memory flag only — same detached-after-close reasoning as the
    # av1-off variant above.
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.video_convert_enabled = True
    finally:
        db.close()

    async def fake_convert(src, dest_dir, max_height, bitrate_kbps):
        return None
    monkeypatch.setattr(video_module, "_convert_video", fake_convert)

    async def fake_remux(src):
        return None
    monkeypatch.setattr(video_module, "_remux_or_transcode", fake_remux)

    dest = tmp_path / "clip123.mkv"
    dest.write_bytes(b"fake mkv")
    result = await video_module._finish_stored_file(dest, tmp_path, w)
    assert result == dest  # original kept — the clip is stored unconverted
    assert result.is_file()  # and untouched
    assert not (tmp_path / "clip123.mp4").exists()  # no half-written output left behind


def test_remux_command_builder():
    import app.routers.video as video_module

    src, out = Path("movie.mkv"), Path("movie.mp4")
    hevc_cmd = video_module._remux_command(src, out, hevc=True)
    assert hevc_cmd[hevc_cmd.index("-tag:v") + 1] == "hvc1"  # Apple's required HEVC tag
    assert hevc_cmd.count("-map") == 2  # video then audio, in that order
    assert hevc_cmd[hevc_cmd.index("-map") + 1] == "0:v:0"
    assert hevc_cmd[hevc_cmd.index("-map", hevc_cmd.index("-map") + 1) + 1] == "0:a:0"
    for flag, value in (("-c", "copy"), ("-movflags", "+faststart")):
        assert hevc_cmd[hevc_cmd.index(flag) + 1] == value
    plain_cmd = video_module._remux_command(src, out, hevc=False)
    assert "-tag:v" not in plain_cmd and "hvc1" not in plain_cmd  # no tag for non-HEVC sources
    assert plain_cmd[-1] == str(out)


@pytest.mark.skipif(shutil.which("ffmpeg") is None,
                    reason="ffmpeg absent — the mocked tests above already pin the ladder's contract")
def test_video_upload_mkv_gets_remuxed_to_mp4_by_real_ffmpeg(client, seed, tmp_path, monkeypatch):
    """Real-ffmpeg integration for the whole non-native path: build a tiny
    MKV with ffmpeg itself, upload it with the world's AV1 opt-in off (the
    default), and assert the stored clip is a real, non-empty .mp4 — the
    remux rung runs for real (H.264/AAC in MKV remuxes into MP4 losslessly)."""
    import subprocess

    import app.routers.video as video_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)

    built = subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=0.5:size=128x96:rate=10",
        "-f", "lavfi", "-i", "sine=duration=0.5", "-c:v", "libx264", "-c:a", "aac", str(tmp_path / "source.mkv"),
    ], capture_output=True)
    assert built.returncode == 0
    assert (tmp_path / "source.mkv").stat().st_size > 0

    async def fake_poster(video_path, dest_dir):
        return None  # poster is not this test's subject — skip its own ffmpeg run
    monkeypatch.setattr(video_module, "_generate_poster", fake_poster)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    # Upload the BUILT mkv's actual bytes — _mp4_file's _MP4_BYTES placeholder
    # content would just exercise the graceful keep-original fallback instead.
    r = client.post("/video/upload",
                    files={"file": ("cutscene.mkv", io.BytesIO((tmp_path / "source.mkv").read_bytes()), "video/x-matroska")},
                    follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        file_url = clip.file_url
    finally:
        db.close()
    assert file_url.endswith(".mp4")
    stored = tmp_path / "video" / file_url[len("/uploads/video/"):]
    assert stored.is_file()
    assert stored.stat().st_size > 0
    # And it is genuinely an MP4 container now, not a renamed MKV — ffprobe
    # reports mp4 among the mov-style format names for a real MP4 mux output.
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "csv=p=0", str(stored),
    ], capture_output=True)
    assert probe.returncode == 0
    assert "mp4" in probe.stdout.decode(errors="replace")


# ── Albums and sub-albums ───────────────────────────────────────────────────

def test_video_album_create_top_level(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/albums/new", data={"name": "Session 1 Clips"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        album = db.query(VideoAlbum).filter(VideoAlbum.world_id == seed.world_a.id).first()
        assert album.name == "Session 1 Clips"
        assert album.parent_id is None
    finally:
        db.close()


def test_video_album_create_sub_album(client, seed):
    parent_id = _add_album(seed.world_a.id, name="Parent")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/albums/new", data={"name": "Child", "parent_id": str(parent_id)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/video/albums/")
    db = SessionLocal()
    try:
        child = db.query(VideoAlbum).filter(VideoAlbum.name == "Child").first()
        assert child.parent_id == parent_id
    finally:
        db.close()


def test_video_album_create_forbidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/albums/new", data={"name": "Hacked"})
    assert r.status_code == 403


def test_video_album_detail_page_reachable_by_gm_and_player(client, seed):
    aid = _add_album(seed.world_a.id, name="Cutscenes")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/video/albums/{aid}").status_code == 200
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/video/albums/{aid}").status_code == 200


def test_video_album_breadcrumb_shows_full_chain(client, seed):
    root_id = _add_album(seed.world_a.id, name="Root")
    mid_id = _add_album(seed.world_a.id, name="Middle", parent_id=root_id)
    leaf_id = _add_album(seed.world_a.id, name="Leaf", parent_id=mid_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/video/albums/{leaf_id}")
    assert r.status_code == 200
    assert "Root" in r.text
    assert "Middle" in r.text
    assert "Leaf" in r.text


def test_video_album_shows_only_own_clips_and_sub_albums(client, seed):
    aid = _add_album(seed.world_a.id, name="Album A")
    sub_id = _add_album(seed.world_a.id, name="Sub Album", parent_id=aid)
    _add_clip(seed.world_a.id, name="In Album", album_id=aid, visible_to_players=True)
    _add_clip(seed.world_a.id, name="Top Level Clip", album_id=None, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/video/albums/{aid}")
    assert "In Album" in r.text
    assert "Top Level Clip" not in r.text
    assert "Sub Album" in r.text


def test_video_album_player_only_sees_visible_clips_inside(client, seed):
    aid = _add_album(seed.world_a.id, name="Album A")
    _add_clip(seed.world_a.id, name="Visible In Album", album_id=aid, visible_to_players=True)
    _add_clip(seed.world_a.id, name="Hidden In Album", album_id=aid, visible_to_players=False)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/video/albums/{aid}")
    assert "Visible In Album" in r.text
    assert "Hidden In Album" not in r.text


def test_video_album_rename(client, seed):
    aid = _add_album(seed.world_a.id, name="Old Name")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/albums/{aid}/rename", data={"name": "New Name"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(VideoAlbum, aid).name == "New Name"
    finally:
        db.close()


def test_video_album_delete_cascades_to_sub_albums_and_clips(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    video_dir = tmp_path / "video"
    video_dir.mkdir()
    f = video_dir / "nested.mp4"
    f.write_bytes(b"fake video")

    root_id = _add_album(seed.world_a.id, name="Root")
    child_id = _add_album(seed.world_a.id, name="Child", parent_id=root_id)
    clip_in_root_id = _add_clip(seed.world_a.id, name="Root Clip", album_id=root_id,
                                 file_url="/uploads/video/nested.mp4")
    clip_in_child_id = _add_clip(seed.world_a.id, name="Child Clip", album_id=child_id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/albums/{root_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/video"
    assert not f.exists()

    db = SessionLocal()
    try:
        assert db.get(VideoAlbum, root_id) is None
        assert db.get(VideoAlbum, child_id) is None
        assert db.get(VideoClip, clip_in_root_id) is None
        assert db.get(VideoClip, clip_in_child_id) is None
    finally:
        db.close()


def test_video_album_delete_redirects_to_parent(client, seed):
    root_id = _add_album(seed.world_a.id, name="Root")
    child_id = _add_album(seed.world_a.id, name="Child", parent_id=root_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/albums/{child_id}/delete", follow_redirects=False)
    assert r.headers["location"] == f"/video/albums/{root_id}"


def test_video_album_delete_forbidden_for_player(client, seed):
    aid = _add_album(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/albums/{aid}/delete")
    assert r.status_code == 403


def test_video_album_cross_world_404s(client, seed):
    aid = _add_album(seed.world_b.id, name="Other World Album")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/video/albums/{aid}")
    assert r.status_code == 404


def test_video_upload_into_album(client, seed):
    aid = _add_album(seed.world_a.id, name="Cutscenes")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload", data={"album_id": str(aid)}, files=_mp4_file(), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/video/albums/{aid}"
    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip.album_id == aid
    finally:
        db.close()


def test_video_edit_moves_clip_between_albums(client, seed):
    aid = _add_album(seed.world_a.id, name="Destination")
    cid = _add_clip(seed.world_a.id, name="Clip", album_id=None)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/video/{cid}/edit", data={"name": "Clip", "album_id": str(aid)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/video/albums/{aid}"
    db = SessionLocal()
    try:
        assert db.get(VideoClip, cid).album_id == aid
    finally:
        db.close()


# ── Nav visibility ───────────────────────────────────────────────────────────

def test_nav_shows_video_link_to_everyone(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/video"' in r.text
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/video"' in r.text
