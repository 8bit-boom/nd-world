"""Keepsakes: something shown or sent to the players, kept on a character under the player's own name and a short description
(or kept for them by the GM). The server resolves WHAT is kept from its own copy - a player can only keep what they could already see."""
import io

import pytest

from app.database import SessionLocal
from app.models import AudioClip, CharacterKeepsake, GameSession, PlayerCharacter, PrivateNote, WorldHandout
from app.main import UPLOADS_DIR

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


def _pc(seed, owner=None):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=(owner or seed.player_a).id, name="Aria")
        db.add(pc)
        db.commit()
        return pc.id
    finally:
        db.close()


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        return obj.id
    finally:
        db.close()


def _as_player(client, seed, user=None):
    client.cookies.clear()
    login(client, (user or seed.player_a).email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _as_gm(client, seed):
    client.cookies.clear()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_a_player_keeps_a_shown_picture_under_their_own_name_and_can_rename_and_delete_it(client, seed):
    pc = _pc(seed)
    hid = _add(WorldHandout(world_id=seed.world_a.id, url="/uploads/gallery/map.png", label="map.png"))
    _as_player(client, seed)
    listing = client.get(f"/api/characters/{pc}/hub/keepsakes").json()
    assert [h["id"] for h in listing["saveable"]["handouts"]] == [hid] and listing["keepsakes"] == []
    r = client.post(f"/api/characters/{pc}/hub/keepsakes", json={"name": "  The   hidden vault ", "description": "Found behind the altar",
                                                                    "source": {"type": "handout", "id": hid}})
    assert r.status_code == 200, r.text
    k = r.json()
    assert (k["kind"], k["url"], k["name"], k["description"], k["by"]) == ("image", "/uploads/gallery/map.png", "The hidden vault", "Found behind the altar", "player")
    assert client.post(f"/api/characters/{pc}/hub/keepsakes/{k['id']}", json={"name": "Vault map", "description": ""}).json()["name"] == "Vault map"
    assert client.post(f"/api/characters/{pc}/hub/keepsakes/{k['id']}", json={"name": " "}).status_code == 400
    assert client.post(f"/api/characters/{pc}/hub/keepsakes/{k['id']}/delete").status_code == 200
    assert client.get(f"/api/characters/{pc}/hub/keepsakes").json()["keepsakes"] == []


def test_an_empty_name_falls_back_to_the_original_one(client, seed):
    pc = _pc(seed)
    hid = _add(WorldHandout(world_id=seed.world_a.id, url="/uploads/gallery/a.png", label="Old letter"))
    _as_player(client, seed)
    k = client.post(f"/api/characters/{pc}/hub/keepsakes", json={"name": "", "source": {"type": "handout", "id": hid}}).json()
    assert k["name"] == "Old letter"


def test_a_player_keeps_a_note_from_the_gm_as_text_but_not_gm_only_parts(client, seed):
    pc = _pc(seed)
    nid = _add(PrivateNote(world_id=seed.world_a.id, player_user_id=seed.player_a.id, author_id=seed.gm.id, title="Riddle",
                           content="The door opens at dawn.[gmonly]It is trapped.[/gmonly]"))
    _as_player(client, seed)
    saveable = client.get(f"/api/characters/{pc}/hub/keepsakes").json()["saveable"]["notes"]
    assert [n["id"] for n in saveable] == [nid] and "trapped" not in saveable[0]["text"]
    k = client.post(f"/api/characters/{pc}/hub/keepsakes", json={"name": "Door riddle", "source": {"type": "note", "id": nid}}).json()
    assert k["kind"] == "text" and "dawn" in k["text"] and "trapped" not in k["text"]


def test_session_media_can_be_kept_only_when_players_may_see_it(client, seed):
    pc = _pc(seed)
    (UPLOADS_DIR / "audio").mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "audio" / "song.mp3").write_bytes(b"ID3x")
    heard = _add(AudioClip(world_id=seed.world_a.id, name="Shared song", file_url="/uploads/audio/song.mp3", visible_to_players=True))
    secret = _add(AudioClip(world_id=seed.world_a.id, name="Secret song", file_url="/uploads/audio/song.mp3", visible_to_players=False))
    sid = _add(GameSession(world_id=seed.world_a.id, title="Night", session_num=1, recap_audio_json=f"[{heard}, {secret}]"))
    _as_player(client, seed)
    items = client.get(f"/api/characters/{pc}/hub/keepsakes").json()["saveable"]["session"]
    assert [i["name"] for i in items] == ["Shared song"]
    ok = client.post(f"/api/characters/{pc}/hub/keepsakes", json={"source": {"type": "session", "session_id": sid, "kind": "audio", "ref": str(heard)}})
    assert ok.status_code == 200 and ok.json()["kind"] == "audio" and ok.json()["name"] == "Shared song"
    no = client.post(f"/api/characters/{pc}/hub/keepsakes", json={"source": {"type": "session", "session_id": sid, "kind": "audio", "ref": str(secret)}})
    assert no.status_code == 404


def test_players_cannot_add_raw_content_or_touch_someone_elses_character(client, seed):
    pc = _pc(seed)
    _as_player(client, seed)
    raw = client.post(f"/api/characters/{pc}/hub/keepsakes", json={"name": "x", "source": {"type": "raw", "kind": "text", "text": "hi"}})
    assert raw.status_code == 403
    _as_player(client, seed, seed.player_b)
    assert client.get(f"/api/characters/{pc}/hub/keepsakes").status_code == 404
    assert client.post(f"/api/characters/{pc}/hub/keepsakes", json={"source": {"type": "handout", "id": 1}}).status_code == 404


def test_the_gm_can_keep_a_text_or_a_file_for_a_player_and_it_shows_to_the_player(client, seed):
    pc = _pc(seed)
    _as_gm(client, seed)
    t = client.post(f"/api/characters/{pc}/hub/keepsakes", json={"name": "Prophecy", "description": "Read at the temple",
                                                                    "source": {"type": "raw", "kind": "text", "text": "When the moon bleeds..."}})
    assert t.status_code == 200 and t.json()["by"] == "gm" and t.json()["kind"] == "text"
    f = client.post(f"/api/characters/{pc}/hub/keepsakes", json={"name": "Portrait", "source": {"type": "raw", "kind": "image", "url": "/uploads/gallery/p.png"}})
    assert f.status_code == 200 and f.json()["kind"] == "image"
    for bad in ("http://evil.example/x.png", "/etc/passwd", "/uploads/../secret.db"):
        assert client.post(f"/api/characters/{pc}/hub/keepsakes", json={"name": "x", "source": {"type": "raw", "kind": "image", "url": bad}}).status_code == 400
    _as_player(client, seed)
    names = [k["name"] for k in client.get(f"/api/characters/{pc}/hub/keepsakes").json()["keepsakes"]]
    assert set(names) == {"Prophecy", "Portrait"}


def test_keepsakes_are_capped_and_go_when_the_character_is_deleted(client, seed):
    from app.routers.character_hub import KEEPSAKE_MAX, delete_character_journal
    pc = _pc(seed)
    db = SessionLocal()
    try:
        for i in range(KEEPSAKE_MAX):
            db.add(CharacterKeepsake(world_id=seed.world_a.id, character_id=pc, kind="text", text="x", name=f"n{i}"))
        db.commit()
    finally:
        db.close()
    _as_gm(client, seed)
    full = client.post(f"/api/characters/{pc}/hub/keepsakes", json={"name": "one more", "source": {"type": "raw", "kind": "text", "text": "x"}})
    assert full.status_code == 400
    db = SessionLocal()
    try:
        delete_character_journal(db, pc)
        db.commit()
        assert db.query(CharacterKeepsake).filter(CharacterKeepsake.character_id == pc).count() == 0
    finally:
        db.close()


def test_the_hub_has_a_kept_tab_that_fits_a_phone(client, seed):
    pc = _pc(seed)
    _as_player(client, seed)
    page = client.get(f"/characters/{pc}").text
    assert 'data-tab="keepsakes"' in page and 'data-panel="keepsakes"' in page and "loadKeepsakes" in page
    assert "font-size:16px" in page and "@media (max-width:720px) { .pch-modal" in page      # no zoom on focus; a sheet on a narrow screen


# ── a player's OWN files, with per-world limits ─────────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 600


def _upload(client, pc, fname, data=PNG, **form):
    return client.post(f"/api/characters/{pc}/hub/keepsakes/upload", data=form, files={"file": (fname, io.BytesIO(data), "application/octet-stream")})


def _set_world_limits(seed, **kw):
    from app.models import World
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _reset_limits(client, seed):
    _set_world_limits(seed, personal_images_max=None, personal_audio_max=None, personal_video_max=None)
    yield
    _set_world_limits(seed, personal_images_max=None, personal_audio_max=None, personal_video_max=None)


def test_the_defaults_are_30_pictures_10_audio_and_5_videos():
    from app import personal_files as pf

    class W:
        personal_images_max = personal_audio_max = personal_video_max = None
    assert pf.limits(W) == {"image": 30, "audio": 10, "video": 5}
    W.personal_audio_max = 0
    assert pf.limit_for(W, "audio") == 0 and pf.clamp("abc") is None and pf.clamp("9999") == 500 and pf.clamp("-3") == 0


def test_a_player_uploads_their_own_file_with_a_name_and_description_and_it_is_kept_separate(client, seed):
    pc = _pc(seed)
    _as_player(client, seed)
    r = _upload(client, pc, "selfie.png", name="Me at the gate", description="Taken before the heist")
    assert r.status_code == 200, r.text
    k = r.json()
    assert (k["kind"], k["name"], k["own"], k["by"]) == ("image", "Me at the gate", True, "player") and k["url"].startswith("/uploads/")
    listing = client.get(f"/api/characters/{pc}/hub/keepsakes").json()
    assert listing["own_limits"]["image"] == {"used": 1, "max": 30} and listing["own_limits"]["audio"] == {"used": 0, "max": 10} and listing["is_owner"] is True
    assert [x["own"] for x in listing["keepsakes"]] == [True]
    # the description can be changed or added afterwards
    assert client.post(f"/api/characters/{pc}/hub/keepsakes/{k['id']}", json={"description": "New text"}).json()["description"] == "New text"


def test_the_world_limit_per_kind_is_enforced_before_the_file_is_written_and_zero_turns_a_kind_off(client, seed):
    pc = _pc(seed)
    _set_world_limits(seed, personal_images_max=2, personal_video_max=0)
    _as_player(client, seed)
    assert _upload(client, pc, "a.png").status_code == 200 and _upload(client, pc, "b.png").status_code == 200
    third = _upload(client, pc, "c.png")
    assert third.status_code == 400 and "2" in third.json()["detail"]
    off = _upload(client, pc, "clip.mp4", data=b"\x00\x00\x00\x18ftypmp42")
    assert off.status_code == 400 and "turned off" in off.json()["detail"]
    assert _upload(client, pc, "notes.exe").status_code == 400                  # not a picture / audio / video
    # the table's own keepsakes do not count against the personal limit
    hid = _add(WorldHandout(world_id=seed.world_a.id, url="/uploads/gallery/z.png", label="z"))
    assert client.post(f"/api/characters/{pc}/hub/keepsakes", json={"source": {"type": "handout", "id": hid}}).status_code == 200


def test_deleting_a_personal_file_removes_it_from_disk_but_a_table_keepsake_never_deletes_its_file(client, seed):
    from app.routers.gallery import _resolve_upload_path
    pc = _pc(seed)
    _as_player(client, seed)
    mine = _upload(client, pc, "mine.png").json()
    assert _resolve_upload_path(mine["url"]) is not None
    assert client.post(f"/api/characters/{pc}/hub/keepsakes/{mine['id']}/delete").status_code == 200
    assert _resolve_upload_path(mine["url"]) is None


def test_only_the_owner_uploads_and_the_gm_cannot_do_it_for_them(client, seed):
    pc = _pc(seed)
    _as_gm(client, seed)
    assert _upload(client, pc, "x.png").status_code == 404                       # not the owner: the same 404 as a stranger
    _as_player(client, seed, seed.player_b)
    assert _upload(client, pc, "x.png").status_code == 404


def test_the_gm_sets_the_limits_in_edit_world_and_blank_means_the_default(client, seed):
    from app.models import World
    _as_gm(client, seed)
    form = {"name": "World A", "personal_images_max": "12", "personal_audio_max": "0", "personal_video_max": ""}
    assert client.post(f"/worlds/{seed.world_a.id}/edit", data=form, follow_redirects=False).status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert (w.personal_images_max, w.personal_audio_max, w.personal_video_max) == (12, 0, None)
    finally:
        db.close()
    page = client.get(f"/worlds/{seed.world_a.id}/edit").text
    assert 'name="personal_images_max"' in page and 'value="12"' in page


def test_a_kept_picture_blocks_the_gallery_from_deleting_it(client, seed):
    from app.gallery import discover_world_images
    from app.models import World
    pc = _pc(seed)
    _add(CharacterKeepsake(world_id=seed.world_a.id, character_id=pc, kind="image", url="/uploads/gallery/kept.png", name="k"))
    _add(CharacterKeepsake(world_id=seed.world_a.id, character_id=pc, kind="image", url="/uploads/own.png", name="o", own=True))
    db = SessionLocal()
    try:
        urls = {e["url"]: e for e in discover_world_images(db, db.get(World, seed.world_a.id))}
    finally:
        db.close()
    assert "/uploads/gallery/kept.png" in urls and "(kept)" in urls["/uploads/gallery/kept.png"]["uses"][0]["label"]
    assert "/uploads/own.png" not in urls            # a player's own upload is not part of the world's gallery
