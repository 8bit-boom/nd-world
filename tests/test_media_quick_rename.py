"""Quick rename of images, videos and audio clips from their libraries (static/js/media-rename.js -> /api/media-rename/apply)."""
from app.database import SessionLocal
from app.models import AudioClip, ImageAlbum, MediaTitle, VideoClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _make(seed):
    db = SessionLocal()
    try:
        a = AudioClip(world_id=seed.world_a.id, name="track 07", file_url="/uploads/audio/a.mp3", visible_to_players=True)
        v = VideoClip(world_id=seed.world_a.id, name="clip 1", file_url="/uploads/video/v.mp4", visible_to_players=True)
        alb = ImageAlbum(world_id=seed.world_a.id, name="Maps", image_urls_json='["/uploads/gallery/x.png"]')
        db.add_all([a, v, alb]); db.commit()
        return a.id, v.id, alb.id
    finally:
        db.close()


def test_libraries_offer_a_rename_button_and_label_hooks_to_editors_only(client, seed):
    aid, vid, alb = _make(seed)
    _gm(client, seed)
    audio = client.get("/audio").text
    assert f'data-nd-rename data-kind="audio" data-id="{aid}"' in audio and f'data-rename-label="audio:{aid}"' in audio
    video = client.get("/video").text
    assert f'data-nd-rename data-kind="video" data-id="{vid}"' in video and f'data-rename-label="video:{vid}"' in video
    album = client.get(f"/images/albums/{alb}").text
    assert 'data-nd-rename data-kind="image" data-url="/uploads/gallery/x.png"' in album and 'data-rename-label="image:/uploads/gallery/x.png"' in album
    assert "media-rename.js" in audio
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert "data-nd-rename" not in client.get("/audio").text


def test_renaming_clips_and_images_is_saved_and_undoable(client, seed):
    aid, vid, alb = _make(seed)
    _gm(client, seed)
    url = "/uploads/gallery/x.png"
    r = client.post("/api/media-rename/apply", json={"renames": [
        {"kind": "audio", "id": aid, "name": "Tavern Crowd Murmur"},
        {"kind": "video", "id": vid, "name": "Harbour Fly-over"},
        {"kind": "image", "url": url, "name": "Harbour Map"}]})
    assert r.status_code == 200 and r.json()["applied"] == 3, r.text
    batch = r.json()["batch_id"]
    db = SessionLocal()
    try:
        assert db.get(AudioClip, aid).name == "Tavern Crowd Murmur" and db.get(VideoClip, vid).name == "Harbour Fly-over"
        assert db.query(MediaTitle).filter_by(url=url).first().title == "Harbour Map"
    finally:
        db.close()
    assert "Harbour Map" in client.get(f"/images/albums/{alb}").text
    # an empty clip name is refused; an empty image name puts back the automatic one
    assert client.post("/api/media-rename/apply", json={"renames": [{"kind": "audio", "id": aid, "name": " "}]}).json()["applied"] == 0
    assert client.post("/api/media-rename/apply", json={"renames": [{"kind": "image", "url": url, "name": ""}]}).json()["applied"] == 1
    # undo the first batch
    client.post("/api/media-rename/undo", json={"batch_id": batch})
    db = SessionLocal()
    try:
        assert db.get(AudioClip, aid).name == "track 07" and db.get(VideoClip, vid).name == "clip 1"
    finally:
        db.close()


def test_a_player_cannot_rename(client, seed):
    aid, _vid, _alb = _make(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/media-rename/apply", json={"renames": [{"kind": "audio", "id": aid, "name": "Hacked"}]})
    assert r.status_code in (401, 403)
    db = SessionLocal()
    try:
        assert db.get(AudioClip, aid).name == "track 07"
    finally:
        db.close()


def test_all_images_page_tiles_have_rename_buttons(client, seed):
    from app.models import Entity
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Portrait Holder", image_url="/uploads/gallery/p.png")); db.commit()
    finally:
        db.close()
    _gm(client, seed)
    html = client.get("/images").text
    assert 'data-nd-rename data-kind="image" data-url="/uploads/gallery/p.png"' in html
    assert 'data-rename-label="image:/uploads/gallery/p.png"' in html and "NDGalleryCanRename = true" in html
