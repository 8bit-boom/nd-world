"""An entity page's Media tab: the entity's pictures (kept on it, the portrait, the ones written in its body and notes) and the audio and
video clips that belong to it, shown on the server to every viewer with only what that viewer may see - and, for whoever may edit the entity,
every way of adding to the tab or changing it (app/routers/entity_media.py, static/js/entity-media.js)."""
import io
import json
import shutil
import subprocess

import pytest

from app import entity_media as em
from app.database import SessionLocal
from app.main import UPLOADS_DIR
from app.models import AudioClip, Entity, EntityNote, ImageAlbum, VideoClip, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 600
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _assistant(client, seed):
    """Player A as a GM-Assistant of world A."""
    db = SessionLocal()
    try:
        db.query(WorldMembership).filter(WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id).update({"role": "assistant"})
        db.commit()
    finally:
        db.close()
    _player(client, seed)


def _entity(seed, world=None, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=(world or seed.world_a).id, kind=kw.pop("kind", "character"), name=kw.pop("name", "Mira Voss"),
                   visible_to_players=True, **kw)
        db.add(e)
        db.commit()
        return e.id
    finally:
        db.close()


def _keep(eid, *items):
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        em.store_pictures(e, [{"uid": f"u{i}", "url": u, "caption": c, "visible": v} for i, (u, c, v) in enumerate(items)])
        db.commit()
    finally:
        db.close()


def _audio_clip(world, name="Ballad", visible=False, entity_id=None):
    (UPLOADS_DIR / "audio").mkdir(parents=True, exist_ok=True)
    fname = f"{name.lower().replace(' ', '-')}.mp3"
    (UPLOADS_DIR / "audio" / fname).write_bytes(b"ID3fake")
    db = SessionLocal()
    try:
        c = AudioClip(world_id=world.id, name=name, file_url=f"/uploads/audio/{fname}", visible_to_players=visible, entity_id=entity_id)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def _video_clip(world, name="Cutscene", visible=False, entity_id=None):
    (UPLOADS_DIR / "video").mkdir(parents=True, exist_ok=True)
    fname = f"{name.lower().replace(' ', '-')}.mp4"
    (UPLOADS_DIR / "video" / fname).write_bytes(b"\x00\x00\x00\x18ftypmp42")
    db = SessionLocal()
    try:
        c = VideoClip(world_id=world.id, name=name, file_url=f"/uploads/video/{fname}", visible_to_players=visible, entity_id=entity_id)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def _gallery_image(world, name="market-map.png"):
    fname = name
    (UPLOADS_DIR / "gallery").mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "gallery" / fname).write_bytes(PNG)
    db = SessionLocal()
    try:
        a = ImageAlbum(world_id=world.id, name="Album", image_urls_json=json.dumps([f"/uploads/gallery/{name}"]))
        db.add(a)
        db.commit()
    finally:
        db.close()
    return f"/uploads/gallery/{name}"


def _upload(client, eid, fname, data=PNG, ctype="application/octet-stream", **form):
    return client.post(f"/api/entity/{eid}/media/upload", data=form, files={"file": (fname, io.BytesIO(data), ctype)})


def _state(client, eid):
    r = client.get(f"/api/entity/{eid}/media")
    assert r.status_code == 200, r.text
    return r.json()


def _page(client, eid):
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200, r.text
    return r.text


def _media(html):
    """The page's Media tab on its own."""
    i = html.index('<section class="entity-media"')
    return html[i:html.index("</section>", i)]


# ── the stored list ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_stored_picture_list_is_cleaned_on_read():
    class E:
        media_images_json = json.dumps([
            {"uid": "a", "url": "/uploads/gallery/a.png", "caption": "A", "visible": True},
            "not a dict", {"url": "javascript:alert(1)"}, {"url": "/uploads/gallery/b.png"},  # junk, and one without a uid
            {"url": "/uploads/gallery/a.png", "caption": "again"},                           # a repeat
        ] + [{"uid": f"n{i}", "url": f"/uploads/gallery/{i}.png"} for i in range(em.MAX_PICTURES + 5)])
    got = em.pictures(E)
    assert [p["url"] for p in got[:3]] == ["/uploads/gallery/a.png", "/uploads/gallery/b.png", "/uploads/gallery/0.png"]
    assert len(got) == em.MAX_PICTURES and got[1]["uid"] and got[1]["visible"] is False
    assert em.pictures(type("J", (), {"media_images_json": "{not json"})()) == []
    assert em.pictures(type("N", (), {"media_images_json": None})()) == []


def test_a_new_column_is_healed_into_an_old_database(tmp_path):
    """entities predate the generic healing: its ADD COLUMN is hand-typed in _migrate, so this is the proof it is there - a database made
    without the column gets it, with its default, and keeps its rows."""
    from sqlalchemy import create_engine, text
    from app import database
    real = database.engine
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    try:
        database.Base.metadata.create_all(bind=eng)
        with eng.begin() as c:
            c.execute(text("ALTER TABLE entities DROP COLUMN media_images_json"))
            c.execute(text("INSERT INTO entities (id, world_id, kind, name) VALUES (1, 1, 'item', 'Old')"))
        database.engine = eng
        database._migrate()
        with eng.connect() as c:
            assert c.execute(text("SELECT name, media_images_json FROM entities")).fetchall() == [("Old", "[]")]
    finally:
        database.engine = real
        eng.dispose()


# ── the page ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_gm_always_gets_the_tab_and_a_player_only_when_there_is_something_to_show(client, seed):
    eid = _entity(seed)
    _gm(client, seed)
    tab = _media(_page(client, eid))
    assert 'data-ep="media"' in tab and 'data-em-manage="1"' in tab and 'id="em-file"' in tab and 'id="em-pick"' in tab
    assert "No pictures yet." in tab and "No audio yet." in tab and "No video yet." in tab
    _player(client, seed)
    assert 'data-ep="media"' not in _page(client, eid)


def test_the_tab_lists_the_portrait_the_kept_pictures_and_the_ones_written_in_the_text_each_once(client, seed):
    eid = _entity(seed, image_url="/uploads/gallery/mira.png", body="![Mira](/uploads/gallery/mira.png)\n![Shop](/uploads/gallery/shop.png)")
    _keep(eid, ("/uploads/gallery/mira.png", "Mira", True), ("/uploads/gallery/secret.png", "", False))
    db = SessionLocal()
    db.add(EntityNote(entity_id=eid, content="A note ![Plan](/uploads/gallery/plan.png)", visible_to_players=True))
    db.add(EntityNote(entity_id=eid, content="[gmonly]![Vault](/uploads/gallery/vault.png)[/gmonly]", visible_to_players=True))
    db.commit()
    db.close()
    _gm(client, seed)
    tab = _media(_page(client, eid))
    assert tab.count('class="em-item em-pic') == 5
    assert tab.index("mira.png") < tab.index("shop.png") < tab.index("plan.png") < tab.index("vault.png")   # kept, body, notes - the portrait is the kept one
    assert 'src="/uploads/gallery/mira.png' in tab and 'data-full="/uploads/gallery/mira.png"' in tab and "GM only" in tab
    assert tab.count('data-em-act="remove"') == 2 and tab.count('data-em-act="visibility"') == 2            # only what is kept can be taken off
    assert "In the text" in tab and "In a note" in tab


def test_a_player_sees_only_what_players_may_see_and_never_a_picture_hidden_in_gm_only_text(client, seed):
    eid = _entity(seed, body="[gmonly]![Vault](/uploads/gallery/vault.png)[/gmonly]\n![Shop](/uploads/gallery/shop.png)")
    _keep(eid, ("/uploads/gallery/open.png", "Open", True), ("/uploads/gallery/secret.png", "Secret", False))
    _audio_clip(seed.world_a, "Open song", visible=True, entity_id=eid)
    _audio_clip(seed.world_a, "Hidden song", visible=False, entity_id=eid)
    _video_clip(seed.world_a, "Film", visible=False, entity_id=eid)
    _player(client, seed)
    tab = _media(_page(client, eid))
    assert "vault.png" not in tab and "secret.png" not in tab and "Hidden song" not in tab and "Film" not in tab
    assert "shop.png" in tab and "open.png" in tab and "Open song" in tab
    assert 'data-em-manage' not in tab and "data-em-act" not in tab and "GM only" not in tab
    assert client.get(f"/api/entity/{eid}/media").status_code == 403


def test_the_gm_preview_as_a_player_shows_the_players_tab(client, seed):
    eid = _entity(seed)
    _keep(eid, ("/uploads/gallery/secret.png", "", False))
    _gm(client, seed)
    html = client.get(f"/entity/{eid}?as=player").text
    assert "secret.png" not in html and 'data-ep="media"' not in html       # nothing a player may see: no tab at all


def test_clips_of_another_world_do_not_turn_up_on_the_tab(client, seed):
    eid = _entity(seed)
    _audio_clip(seed.world_b, "Elsewhere", visible=True, entity_id=eid)
    _gm(client, seed)
    assert "Elsewhere" not in _page(client, eid)


def test_an_assistant_gets_the_tools_but_sees_what_players_see(client, seed):
    eid = _entity(seed)
    _keep(eid, ("/uploads/gallery/secret.png", "", False))
    _audio_clip(seed.world_a, "Hidden song", visible=False, entity_id=eid)
    _assistant(client, seed)
    tab = _media(_page(client, eid))
    assert 'data-em-manage="1"' in tab and "secret.png" not in tab and "Hidden song" not in tab
    state = _state(client, eid)
    assert state["pictures"] == [] and state["audio"] == [] and all(c["visible_to_players"] for c in state["library"]["audio"])


# ── keeping pictures ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_picture_of_the_gallery_is_kept_once_hidden_by_default(client, seed):
    eid = _entity(seed)
    url = _gallery_image(seed.world_a)
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/media/attach", json={"kind": "image", "url": url})
    assert r.status_code == 200, r.text
    p = r.json()["pictures"]
    assert len(p) == 1 and p[0]["url"] == url and p[0]["visible_to_players"] is False and p[0]["caption"] == "market-map.png" and p[0]["uid"]
    assert len(client.post(f"/api/entity/{eid}/media/attach", json={"kind": "image", "url": url, "visible_to_players": True}).json()["pictures"]) == 1
    db = SessionLocal()
    try:
        assert db.get(Entity, eid).media_images_json.startswith('[{"uid":')
    finally:
        db.close()


def test_only_pictures_of_this_world_can_be_kept(client, seed):
    eid = _entity(seed)
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/media/attach", json={"kind": "image", "url": "/uploads/gallery/not-here.png"})
    assert r.status_code == 404 and r.json()["detail"] == "That picture is not one of this world's images"
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "spell", "url": "x"}).status_code == 400
    assert client.post(f"/api/entity/{eid}/media/attach", content=b"{not json").status_code == 400       # an unreadable body is an empty one


def test_a_picture_can_be_shown_named_and_taken_off_and_the_file_stays(client, seed):
    eid = _entity(seed)
    url = _gallery_image(seed.world_a)
    _gm(client, seed)
    uid = client.post(f"/api/entity/{eid}/media/attach", json={"kind": "image", "url": url}).json()["pictures"][0]["uid"]
    assert client.post(f"/api/entity/{eid}/media/image/{uid}/visibility", json={"visible_to_players": True}).json()["pictures"][0]["visible_to_players"] is True
    assert client.post(f"/api/entity/{eid}/media/image/{uid}/visibility", json={"visible_to_players": "yes"}).status_code == 400
    assert client.post(f"/api/entity/{eid}/media/image/{uid}/rename", json={"name": "  The   market  "}).json()["pictures"][0]["caption"] == "The market"
    assert client.post(f"/api/entity/{eid}/media/image/{uid}/rename", json={"name": ""}).json()["pictures"][0]["caption"] == ""
    assert client.post(f"/api/entity/{eid}/media/image/nope/rename", json={"name": "x"}).status_code == 404
    assert client.post(f"/api/entity/{eid}/media/image/{uid}/remove").json()["pictures"] == []
    assert (UPLOADS_DIR / "gallery" / "market-map.png").exists()
    assert client.post(f"/api/entity/{eid}/media/image/{uid}/remove").status_code == 404
    assert client.post(f"/api/entity/{eid}/media/spell/x/remove").status_code == 404


def test_an_entity_keeps_at_most_a_hundred_pictures_and_a_refused_upload_leaves_no_file(client, seed):
    eid = _entity(seed)
    _keep(eid, *[(f"/uploads/gallery/{i}.png", "", True) for i in range(em.MAX_PICTURES)])
    url = _gallery_image(seed.world_a, "one-more.png")
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/media/attach", json={"kind": "image", "url": url})
    assert r.status_code == 400 and "maximum of 100 pictures" in r.json()["detail"]
    before = sorted(p.name for p in (UPLOADS_DIR / "gallery").iterdir())
    assert _upload(client, eid, "dropped.png").status_code == 400
    assert sorted(p.name for p in (UPLOADS_DIR / "gallery").iterdir()) == before


def test_kept_pictures_count_as_used_in_the_gallery(client, seed):
    eid = _entity(seed, name="Mira Voss")
    url = _gallery_image(seed.world_a)
    _keep(eid, (url, "", False))
    _gm(client, seed)
    assert client.post("/images/delete", json={"url": url}).status_code == 400
    html = client.get("/images").text
    assert "Mira Voss (media)" in html


# ── uploading ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_dropped_picture_is_kept_on_the_entity(client, seed):
    eid = _entity(seed)
    _gm(client, seed)
    r = _upload(client, eid, "neon-alley.png")
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "image" and r.json()["item"]["visible_to_players"] is False and r.json()["item"]["caption"] == "neon-alley"
    assert r.json()["item"]["url"].startswith("/uploads/gallery/") and r.json()["max_pictures"] == em.MAX_PICTURES
    assert _state(client, eid)["pictures"][0]["url"] == r.json()["item"]["url"]
    assert _upload(client, eid, "named.png", name="The alley", visible_to_players="1").json()["item"]["caption"] == "The alley"
    tab = _media(_page(client, eid))
    assert 'data-em-key="pic:' in tab and "The alley" in tab


def test_a_dropped_audio_clip_belongs_to_the_entity_and_a_video_too(client, seed):
    eid = _entity(seed)
    _gm(client, seed)
    r = _upload(client, eid, "creepy-drone.mp3", data=b"ID3fake", name="Creepy drone")
    assert r.status_code == 200 and r.json()["kind"] == "audio", r.text
    db = SessionLocal()
    try:
        c = db.get(AudioClip, r.json()["item"]["id"])
        assert (c.entity_id, c.name, c.description, c.visible_to_players) == (eid, "Creepy drone", "Media for Mira Voss", False)
    finally:
        db.close()
    assert _upload(client, eid, "cut.mp4", data=b"\x00\x00\x00\x18ftypmp42").json()["kind"] == "video"
    tab = _media(_page(client, eid))
    assert "🎵 Creepy drone" in tab and "GM only" in tab and "🎬 cut" in tab
    # the picker does not offer what is already here
    state = _state(client, eid)
    assert state["library"]["audio"] == [] and state["library"]["video"] == []


def test_an_unsupported_file_is_refused_and_a_webm_goes_by_its_type(client, seed):
    eid = _entity(seed)
    _gm(client, seed)
    r = _upload(client, eid, "notes.txt")
    assert r.status_code == 400 and "Unsupported file type" in r.json()["detail"]
    # .webm fits two kinds: the browser's own type decides (the tab's script sends it as the "kind" field), else it is a video
    assert _upload(client, eid, "voice.webm", ctype="audio/webm", kind="audio").json()["kind"] == "audio"
    assert _upload(client, eid, "clip.webm").json()["kind"] == "video"


def test_a_file_sent_in_parts_is_assembled_and_kept(client, seed):
    eid = _entity(seed)
    _gm(client, seed)
    upload_id = "a" * 32
    for i, part in enumerate([PNG[:300], PNG[300:]]):
        r = client.post(f"/api/entity/{eid}/media/upload/chunk", data={"upload_id": upload_id, "chunk_index": str(i)}, files={"file": (f"{i}.part", io.BytesIO(part), "application/octet-stream")})
        assert r.status_code == 200 and r.json() == {"ok": True}
    r = client.post(f"/api/entity/{eid}/media/upload/complete", data={"upload_id": upload_id, "filename": "two.png", "total_chunks": "2"})
    assert r.status_code == 200 and r.json()["item"]["caption"] == "two", r.text
    assert not (UPLOADS_DIR / "entity_media" / "_chunks" / upload_id).exists()


# ── clips from the libraries ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_clip_is_attached_toggleable_detached_and_can_be_named_by_its_address(client, seed):
    eid = _entity(seed)
    cid = _audio_clip(seed.world_a, "Ballad")
    vid = _video_clip(seed.world_a, "Cutscene")
    _gm(client, seed)
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "audio", "id": True}).status_code == 400
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "audio", "id": 9999}).status_code == 404
    r = client.post(f"/api/entity/{eid}/media/attach", json={"kind": "audio", "url": "/uploads/audio/ballad.mp3", "visible_to_players": True})
    assert [c["id"] for c in r.json()["audio"]] == [cid] and r.json()["audio"][0]["visible_to_players"] is True
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "audio", "url": "/uploads/audio/none.mp3"}).status_code == 404
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "video", "id": vid}).json()["video"][0]["poster_url"] == ""
    assert client.post(f"/api/entity/{eid}/media/audio/{cid}/rename", json={"name": "  Mira's   theme "}).json()["audio"][0]["name"] == "Mira's theme"
    assert client.post(f"/api/entity/{eid}/media/audio/{cid}/rename", json={"name": " "}).status_code == 400
    assert client.post(f"/api/entity/{eid}/media/audio/{cid}/visibility", json={"visible_to_players": False}).json()["audio"][0]["visible_to_players"] is False
    # a clip that is not on this entity is nobody's to change here
    other = _audio_clip(seed.world_a, "Other")
    assert client.post(f"/api/entity/{eid}/media/audio/{other}/visibility", json={"visible_to_players": True}).status_code == 404
    assert client.post(f"/api/entity/{eid}/media/audio/x/visibility", json={"visible_to_players": True}).status_code == 404
    assert client.post(f"/api/entity/{eid}/media/video/{vid}/remove").json()["video"] == []
    assert client.post(f"/api/entity/{eid}/media/audio/{cid}/remove").json()["audio"] == []
    db = SessionLocal()
    try:
        assert db.get(AudioClip, cid).entity_id is None and (UPLOADS_DIR / "audio" / "ballad.mp3").exists()
    finally:
        db.close()
    assert "Ballad" not in _media(_page(client, eid))


def test_a_clip_of_another_world_cannot_be_attached(client, seed):
    eid = _entity(seed)
    cid = _audio_clip(seed.world_b, "Elsewhere", visible=True)
    _gm(client, seed)
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "audio", "id": cid}).status_code == 404


def test_hiding_the_song_that_is_playing_for_the_table_stops_it(client, seed):
    eid = _entity(seed)
    cid = _audio_clip(seed.world_a, "Ballad", visible=True, entity_id=eid)
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).now_playing_url = "/uploads/audio/ballad.mp3"
        db.commit()
    finally:
        db.close()
    _gm(client, seed)
    client.post(f"/api/entity/{eid}/media/audio/{cid}/visibility", json={"visible_to_players": False})
    db = SessionLocal()
    try:
        assert db.get(World, seed.world_a.id).now_playing_url is None
    finally:
        db.close()


# ── who may ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_players_cannot_touch_the_tab(client, seed):
    eid = _entity(seed)
    _player(client, seed)
    assert _upload(client, eid, "x.png").status_code == 403
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "image", "url": "/uploads/gallery/x.png"}).status_code == 403
    assert client.post(f"/api/entity/{eid}/media/image/x/remove").status_code == 403


def test_an_entity_of_another_world_is_a_404(client, seed):
    eid = _entity(seed, seed.world_b)
    _gm(client, seed)
    assert client.get(f"/api/entity/{eid}/media").status_code == 404
    assert _upload(client, eid, "x.png").status_code == 404


def test_an_assistant_may_keep_pictures_but_needs_the_audio_sections_edit_level_for_clips(client, seed):
    eid = _entity(seed)
    cid = _audio_clip(seed.world_a, "Ballad", visible=True)
    url = _gallery_image(seed.world_a)
    _assistant(client, seed)
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "image", "url": url}).status_code == 200
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "audio", "id": cid}).status_code == 200
    from app.database import get_app_settings
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.section_access_json = json.dumps({"audio": {"assistant": "read"}})
        db.commit()
    finally:
        db.close()
    assert client.post(f"/api/entity/{eid}/media/audio/{cid}/remove").status_code == 403
    assert client.post(f"/api/entity/{eid}/media/attach", json={"kind": "audio", "id": cid, "visible_to_players": False}).status_code == 403


def test_deleting_an_entity_sets_its_clips_loose(client, seed):
    eid, eid2 = _entity(seed), _entity(seed, name="Second")
    c1, c2 = _audio_clip(seed.world_a, "One", entity_id=eid), _audio_clip(seed.world_a, "Two", entity_id=eid2)
    v = _video_clip(seed.world_a, "Film", entity_id=eid)
    _gm(client, seed)
    assert client.post(f"/entity/{eid}/delete", follow_redirects=False).status_code in (302, 303)
    assert client.post("/kind/character/bulk-delete", data={"entity_ids": [str(eid2)]}, follow_redirects=False).status_code in (302, 303)
    db = SessionLocal()
    try:
        assert [db.get(AudioClip, i).entity_id for i in (c1, c2)] == [None, None] and db.get(VideoClip, v).entity_id is None
    finally:
        db.close()


# ── the page's script ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_page_loads_the_tabs_script_and_the_media_block_is_tagged_for_the_layout(client, seed):
    eid = _entity(seed)
    _gm(client, seed)
    html = _page(client, eid)
    assert "/static/js/entity-media.js" in html and "/static/js/chunked-upload.js" in html


@needs_node
def test_pure_decisions_of_the_tabs_script():
    out = subprocess.run([NODE, "-e", """
      const c = require('./static/js/entity-media.js');
      const p = (live, fresh) => JSON.stringify(c.plan(live, fresh));
      console.log(JSON.stringify({
        image: c.kindOf({name: 'A.PNG', type: ''}), webmAudio: c.kindOf({name: 'v.webm', type: 'audio/webm'}), webmVideo: c.kindOf({name: 'v.WEBM', type: 'video/webm'}),
        webmPlain: c.kindOf({name: 'v.webm'}), other: c.kindOf({name: 'x.txt', type: 'text/plain'}), none: c.kindOf({name: 'noext'}),
        plan: p(['a', 'b', 'c'], ['a', 'x', 'c']), plan2: p([], ['a', 'b']), plan3: p(['a'], []), same: p(['a'], ['a'])
      }));"""], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert (got["image"], got["webmAudio"], got["webmVideo"], got["webmPlain"], got["other"], got["none"]) == ("image", "audio", "video", "video", "", "")
    assert json.loads(got["plan"]) == {"remove": ["b"], "insert": [{"key": "x", "before": "c"}]}     # a player that stays keeps running
    assert json.loads(got["plan2"]) == {"remove": [], "insert": [{"key": "a", "before": None}, {"key": "b", "before": None}]}   # appended, in order
    assert json.loads(got["plan3"]) == {"remove": ["a"], "insert": []} and json.loads(got["same"]) == {"remove": [], "insert": []}
