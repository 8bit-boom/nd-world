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
from app.models import AudioClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 500


def _mp3_file(name="clip.mp3"):
    return {"file": (name, io.BytesIO(_MP3_BYTES), "audio/mpeg")}


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
