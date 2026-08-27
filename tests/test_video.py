"""Tests for the /video library (app/routers/video.py, VideoClip in
app/models.py) — mirrors tests/test_audio.py almost line for line, since
video.py itself mirrors audio.py's album-tree/visibility/upload shape.
GET /video is player-safe (see /video and /video/albums/{id} in
_is_player_safe); upload/edit/delete/album-management stay GM-only,
enforced both by the middleware (no POST /video/* entry there) and by an
explicit check in each handler.
"""
import io

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
