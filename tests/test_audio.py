"""Tests for the /audio library (app/routers/audio.py, AudioClip in
app/models.py). Unlike /images (GM-only end to end), GET /audio is
player-safe — a player sees a read-only list filtered to
visible_to_players=True, exactly like an Entity's own default-visible
convention. Upload/edit/delete stay GM-only, enforced both by the
middleware (no POST /audio/* entry in _is_player_safe) and by an explicit
check in each handler.
"""
import io

from app.database import SessionLocal
from app.models import AudioAlbum, AudioClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 500


def _mp3_file(name="clip.mp3"):
    return {"file": (name, io.BytesIO(_MP3_BYTES), "audio/mpeg")}


def _add_album(world_id, **kw):
    db = SessionLocal()
    try:
        a = AudioAlbum(world_id=world_id, name=kw.pop("name", "Album"), **kw)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id
    finally:
        db.close()


def _add_clip(world_id, **kw):
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


def test_audio_upload_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/audio/upload", data={"name": "Tavern Ambiance", "description": "Loud and cheerful"},
                     files=_mp3_file(), follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        clips = db.query(AudioClip).filter(AudioClip.world_id == seed.world_a.id).all()
        assert len(clips) == 1
        assert clips[0].name == "Tavern Ambiance"
        assert clips[0].description == "Loud and cheerful"
        assert clips[0].visible_to_players is False  # checkbox not sent in this request
        assert clips[0].file_url.startswith("/uploads/audio/")
    finally:
        db.close()


def test_audio_upload_defaults_name_to_filename(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/audio/upload", files=_mp3_file("goblin_growl.mp3"))
    db = SessionLocal()
    try:
        clip = db.query(AudioClip).filter(AudioClip.world_id == seed.world_a.id).first()
        assert clip.name == "goblin_growl"
    finally:
        db.close()


def test_audio_upload_rejects_bad_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/audio/upload",
                     files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert r.status_code == 400


def test_audio_upload_forbidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/audio/upload", files=_mp3_file())
    assert r.status_code == 403


def test_audio_page_reachable_by_gm_and_player(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/audio").status_code == 200
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/audio").status_code == 200


def test_audio_upload_form_shows_configured_size_limit(client, seed, monkeypatch):
    import app.routers.audio as audio_module

    monkeypatch.setattr(audio_module, "_MAX_AUDIO_BYTES", 75 * 1024 * 1024)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert "Up to 75 MB" in r.text


def test_audio_upload_rejects_file_over_configured_limit(client, seed, monkeypatch):
    import app.routers.audio as audio_module

    monkeypatch.setattr(audio_module, "_MAX_AUDIO_BYTES", 100)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/audio/upload", files=_mp3_file())
    assert r.status_code == 413
    assert "60 MB" not in r.text  # old hardcoded limit shouldn't leak into the message
    db = SessionLocal()
    try:
        assert db.query(AudioClip).filter(AudioClip.world_id == seed.world_a.id).count() == 0
    finally:
        db.close()


def test_audio_upload_file_input_allows_multiple(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert '<input type="file" name="file" id="audio-upload-file" accept="audio/*" multiple required/>' in r.text


def test_audio_bulk_upload_sequential_requests_create_separate_clips(client, seed):
    # The browser-side bulk upload (static/js/audio_library.html's
    # audioUploadBulk) has no server component of its own — it just POSTs to
    # /audio/upload once per selected file, in order, reusing the shared
    # description/visibility for each. This exercises that same sequence
    # server-side to confirm repeated single-file uploads land as distinct
    # clips rather than overwriting/colliding with each other.
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for fname in ("first_track.mp3", "second_track.mp3", "third_track.mp3"):
        r = client.post(
            "/audio/upload",
            data={"description": "Shared batch description", "visible_to_players": "1"},
            files=_mp3_file(fname),
            follow_redirects=False,
        )
        assert r.status_code == 303
    db = SessionLocal()
    try:
        clips = db.query(AudioClip).filter(AudioClip.world_id == seed.world_a.id).order_by(AudioClip.name).all()
        assert [c.name for c in clips] == ["first_track", "second_track", "third_track"]
        assert all(c.description == "Shared batch description" for c in clips)
        assert all(c.visible_to_players for c in clips)
        assert len({c.file_url for c in clips}) == 3  # distinct stored files, no collision
    finally:
        db.close()


def test_audio_player_only_sees_visible_clips(client, seed):
    _add_clip(seed.world_a.id, name="Visible Track", visible_to_players=True)
    _add_clip(seed.world_a.id, name="GM Secret Track", visible_to_players=False)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert "Visible Track" in r.text
    assert "GM Secret Track" in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert "Visible Track" in r.text
    assert "GM Secret Track" not in r.text


def test_audio_edit_updates_fields(client, seed):
    cid = _add_clip(seed.world_a.id, name="Old Name", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/edit",
                     data={"name": "New Name", "description": "Updated", "visible_to_players": "1"},
                     follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        clip = db.get(AudioClip, cid)
        assert clip.name == "New Name"
        assert clip.description == "Updated"
        assert clip.visible_to_players is True
    finally:
        db.close()


def test_audio_edit_forbidden_for_player(client, seed):
    cid = _add_clip(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/edit", data={"name": "Hacked"})
    assert r.status_code == 403


def test_audio_delete_removes_row_and_file(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    monkeypatch.setattr(audio_module, "_UPLOADS_DIR", tmp_path)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    f = audio_dir / "clip123.mp3"
    f.write_bytes(b"fake audio")
    cid = _add_clip(seed.world_a.id, file_url="/uploads/audio/clip123.mp3")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not f.exists()
    db = SessionLocal()
    try:
        assert db.get(AudioClip, cid) is None
    finally:
        db.close()


def test_audio_delete_forbidden_for_player(client, seed):
    cid = _add_clip(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/delete")
    assert r.status_code == 403
    db = SessionLocal()
    try:
        assert db.get(AudioClip, cid) is not None
    finally:
        db.close()


def test_audio_edit_cross_world_404s(client, seed):
    cid = _add_clip(seed.world_b.id, name="Other World Clip")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/edit", data={"name": "Hijacked"})
    assert r.status_code == 404


# ── Albums and sub-albums ───────────────────────────────────────────────────

def test_album_create_top_level(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/audio/albums/new", data={"name": "Session 1 Ambiance"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        album = db.query(AudioAlbum).filter(AudioAlbum.world_id == seed.world_a.id).first()
        assert album.name == "Session 1 Ambiance"
        assert album.parent_id is None
    finally:
        db.close()


def test_album_create_sub_album(client, seed):
    parent_id = _add_album(seed.world_a.id, name="Parent")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/audio/albums/new", data={"name": "Child", "parent_id": str(parent_id)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/audio/albums/")
    db = SessionLocal()
    try:
        child = db.query(AudioAlbum).filter(AudioAlbum.name == "Child").first()
        assert child.parent_id == parent_id
    finally:
        db.close()


def test_album_create_forbidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/audio/albums/new", data={"name": "Hacked"})
    assert r.status_code == 403


def test_album_detail_page_reachable_by_gm_and_player(client, seed):
    aid = _add_album(seed.world_a.id, name="Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/audio/albums/{aid}").status_code == 200
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/audio/albums/{aid}").status_code == 200


def test_album_breadcrumb_shows_full_chain(client, seed):
    root_id = _add_album(seed.world_a.id, name="Root")
    mid_id = _add_album(seed.world_a.id, name="Middle", parent_id=root_id)
    leaf_id = _add_album(seed.world_a.id, name="Leaf", parent_id=mid_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/audio/albums/{leaf_id}")
    assert r.status_code == 200
    assert "Root" in r.text
    assert "Middle" in r.text
    assert "Leaf" in r.text


def test_album_shows_only_own_clips_and_sub_albums(client, seed):
    aid = _add_album(seed.world_a.id, name="Album A")
    sub_id = _add_album(seed.world_a.id, name="Sub Album", parent_id=aid)
    _add_clip(seed.world_a.id, name="In Album", album_id=aid, visible_to_players=True)
    _add_clip(seed.world_a.id, name="Top Level Clip", album_id=None, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/audio/albums/{aid}")
    assert "In Album" in r.text
    assert "Top Level Clip" not in r.text
    assert "Sub Album" in r.text


def test_album_player_only_sees_visible_clips_inside(client, seed):
    aid = _add_album(seed.world_a.id, name="Album A")
    _add_clip(seed.world_a.id, name="Visible In Album", album_id=aid, visible_to_players=True)
    _add_clip(seed.world_a.id, name="Hidden In Album", album_id=aid, visible_to_players=False)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/audio/albums/{aid}")
    assert "Visible In Album" in r.text
    assert "Hidden In Album" not in r.text


def test_album_rename(client, seed):
    aid = _add_album(seed.world_a.id, name="Old Name")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/albums/{aid}/rename", data={"name": "New Name"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(AudioAlbum, aid).name == "New Name"
    finally:
        db.close()


def test_album_delete_cascades_to_sub_albums_and_clips(client, seed, tmp_path, monkeypatch):
    import app.routers.audio as audio_module
    monkeypatch.setattr(audio_module, "_UPLOADS_DIR", tmp_path)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    f = audio_dir / "nested.mp3"
    f.write_bytes(b"fake audio")

    root_id = _add_album(seed.world_a.id, name="Root")
    child_id = _add_album(seed.world_a.id, name="Child", parent_id=root_id)
    clip_in_root_id = _add_clip(seed.world_a.id, name="Root Clip", album_id=root_id,
                                 file_url="/uploads/audio/nested.mp3")
    clip_in_child_id = _add_clip(seed.world_a.id, name="Child Clip", album_id=child_id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/albums/{root_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/audio"
    assert not f.exists()

    db = SessionLocal()
    try:
        assert db.get(AudioAlbum, root_id) is None
        assert db.get(AudioAlbum, child_id) is None
        assert db.get(AudioClip, clip_in_root_id) is None
        assert db.get(AudioClip, clip_in_child_id) is None
    finally:
        db.close()


def test_album_delete_redirects_to_parent(client, seed):
    root_id = _add_album(seed.world_a.id, name="Root")
    child_id = _add_album(seed.world_a.id, name="Child", parent_id=root_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/albums/{child_id}/delete", follow_redirects=False)
    assert r.headers["location"] == f"/audio/albums/{root_id}"


def test_album_delete_forbidden_for_player(client, seed):
    aid = _add_album(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/albums/{aid}/delete")
    assert r.status_code == 403


def test_album_cross_world_404s(client, seed):
    aid = _add_album(seed.world_b.id, name="Other World Album")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/audio/albums/{aid}")
    assert r.status_code == 404


def test_audio_upload_into_album(client, seed):
    aid = _add_album(seed.world_a.id, name="Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/audio/upload", data={"album_id": str(aid)}, files=_mp3_file(), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/audio/albums/{aid}"
    db = SessionLocal()
    try:
        clip = db.query(AudioClip).filter(AudioClip.world_id == seed.world_a.id).first()
        assert clip.album_id == aid
    finally:
        db.close()


def test_audio_edit_moves_clip_between_albums(client, seed):
    aid = _add_album(seed.world_a.id, name="Destination")
    cid = _add_clip(seed.world_a.id, name="Clip", album_id=None)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/edit", data={"name": "Clip", "album_id": str(aid)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/audio/albums/{aid}"
    db = SessionLocal()
    try:
        assert db.get(AudioClip, cid).album_id == aid
    finally:
        db.close()
