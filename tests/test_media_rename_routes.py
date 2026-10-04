"""The AI media renamer's HTTP surface (app/routers/media_rename.py, templates/media_rename/index.html): who may use it,
the item lists, suggesting (with a mocked model), applying, undoing, and where the new names show up."""
import io
import json
import re

import pytest

from app import ai as ai_module
from app import ai_background
from app.database import SessionLocal
from app.models import AudioAlbum, AudioClip, Entity, ImageAlbum, MediaTitle, VideoClip, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _assistant(client, seed):
    db = SessionLocal()
    try:
        db.query(WorldMembership).filter(WorldMembership.user_id == seed.player_a.id).update({"role": "assistant"})
        db.commit()
    finally:
        db.close()
    _player(client, seed)


def _set_access(world_id, **levels):
    from app.deps import world_section_access
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        access = world_section_access(w)
        for sid, lv in levels.items():
            access[sid].update(lv)
        w.section_access_json = json.dumps(access)
        db.commit()
    finally:
        db.close()


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), "teal").save(buf, "PNG")
    return buf.getvalue()


def _write_upload(rel: str, data: bytes) -> str:
    from app.main import UPLOADS_DIR
    p = UPLOADS_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return "/uploads/" + rel


def _clips(seed, names, kind="audio", album_id=None):
    db = SessionLocal()
    try:
        cls = AudioClip if kind == "audio" else VideoClip
        rows = [cls(world_id=seed.world_a.id, name=n, file_url=f"/uploads/{kind}/a1b2c3d4e5f{i}-{re.sub('[^a-z0-9]+', '-', n.lower())}.bin", album_id=album_id)
                for i, n in enumerate(names)]
        db.add_all(rows)
        db.commit()
        return [r.id for r in rows]
    finally:
        db.close()


def _mock_names(monkeypatch, name="Proposed Name"):
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")

    async def chat(messages, system="", model="", options=None, think=False, format=None):
        ids = re.findall(r"^\[(\d+)\]", messages[-1]["content"], re.M)
        return json.dumps({"names": [{"id": i, "name": f"{name} {i}"} for i in ids]})
    monkeypatch.setattr(ai_module, "generate_chat", chat)


# ── access ──────────────────────────────────────────────────────────────────────────────────────

def test_a_player_cannot_reach_any_of_it(client, seed):
    _player(client, seed)
    for method, path in (("get", "/media-rename"), ("get", "/api/media-rename/items?kind=audio"), ("get", "/api/media-rename/history"),
                         ("post", "/api/media-rename/suggest"), ("post", "/api/media-rename/apply"), ("post", "/api/media-rename/undo")):
        assert getattr(client, method)(path, **({"json": {}} if method == "post" else {})).status_code == 403, path


def test_the_gm_and_an_assistant_can_open_the_page(client, seed):
    _gm(client, seed)
    page = client.get("/media-rename")
    assert page.status_code == 200 and "AI rename" in page.text
    _assistant(client, seed)
    assert client.get("/media-rename").status_code == 200
    assert client.get("/api/media-rename/items?kind=audio").status_code == 200


def test_an_assistant_dialled_down_on_a_section_cannot_touch_that_kind(client, seed):
    _set_access(seed.world_a.id, images={"assistant": "read"})
    _assistant(client, seed)
    assert client.get("/api/media-rename/items?kind=image").status_code == 403
    assert client.get("/api/media-rename/items?kind=audio").status_code == 200
    r = client.post("/api/media-rename/apply", json={"renames": [{"kind": "image", "url": "/uploads/x.png", "name": "x"}]})
    assert r.status_code == 403
    _set_access(seed.world_a.id, images={"assistant": "none"}, audio={"assistant": "read"}, video={"assistant": "read"})
    assert client.get("/media-rename").status_code == 403, "nothing left to rename"


def test_the_page_starts_on_the_requested_kind_and_album(client, seed):
    _gm(client, seed)
    boot = lambda q: json.loads(re.search(r'<script id="mr-start" type="application/json">(.*?)</script>', client.get(q).text, re.S).group(1))
    assert boot("/media-rename?kind=video&album=7")["start"] == {"kind": "video", "album": 7, "kinds": ["audio", "video", "image"]}
    assert boot("/media-rename?kind=bogus&album=x")["start"]["kind"] == "audio"


def test_the_page_says_when_no_ai_is_configured(client, seed, monkeypatch):
    _gm(client, seed)
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "")
    page = client.get("/media-rename").text
    assert "No AI backend is configured" in page and re.search(r'id="mr-go"[^>]*disabled', page)
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")
    page = client.get("/media-rename").text
    assert "No AI backend is configured" not in page and not re.search(r'id="mr-go"[^>]*disabled', page)


def test_the_libraries_link_to_the_renamer(client, seed):
    _gm(client, seed)
    album = SessionLocal()
    try:
        a = AudioAlbum(world_id=seed.world_a.id, name="Tavern")
        album.add(a)
        album.commit()
        album_id = a.id
    finally:
        album.close()
    assert "/media-rename?kind=audio" in client.get("/audio").text
    assert f"/media-rename?kind=audio&amp;album={album_id}" in client.get(f"/audio/albums/{album_id}").text
    assert "/media-rename?kind=video" in client.get("/video").text
    assert "/media-rename?kind=image" in client.get("/images").text


# ── the lists ───────────────────────────────────────────────────────────────────────────────────

def _items(client, query):
    r = client.get("/api/media-rename/items?" + query)
    assert r.status_code == 200, r.text
    return r.json()


def test_audio_list_flags_names_that_need_work_and_filters(client, seed):
    _gm(client, seed)
    db = SessionLocal()
    try:
        album = AudioAlbum(world_id=seed.world_a.id, name="Tavern")
        db.add(album)
        db.commit()
        album_id = album.id
    finally:
        db.close()
    _clips(seed, ["track_0043", "Ember Waltz", "Recording 12"], album_id=album_id)
    _clips(seed, ["Elsewhere"], kind="audio")
    db = SessionLocal()
    try:
        db.add(AudioClip(world_id=seed.world_b.id, name="OtherWorld", file_url="/uploads/audio/z.mp3"))
        db.commit()
    finally:
        db.close()
    d = _items(client, "kind=audio")
    names = {i["name"]: i for i in d["items"]}
    assert set(names) == {"track_0043", "Ember Waltz", "Recording 12", "Elsewhere"}, "only this world's clips"
    assert names["track_0043"]["generic"] and names["Recording 12"]["generic"] and not names["Ember Waltz"]["generic"]
    assert d["generic_count"] == 2 and d["albums"] == [{"id": album_id, "name": "Tavern"}]
    assert names["Ember Waltz"]["album"] == "Tavern" and names["Elsewhere"]["album"] == ""
    assert {i["name"] for i in _items(client, f"kind=audio&album={album_id}")["items"]} == {"track_0043", "Ember Waltz", "Recording 12"}
    assert [i["name"] for i in _items(client, "kind=audio&q=waltz")["items"]] == ["Ember Waltz"]
    assert len(_items(client, "kind=audio&album=abc")["items"]) == 4


def test_video_list_and_bad_kind(client, seed):
    _gm(client, seed)
    _clips(seed, ["VID_0001"], kind="video")
    d = _items(client, "kind=video")
    assert d["items"][0]["kind"] == "video" and d["items"][0]["generic"]
    assert client.get("/api/media-rename/items?kind=nonsense").status_code == 403 or client.get("/api/media-rename/items?kind=nonsense").status_code == 400


def test_image_list_shows_chosen_titles_and_usage(client, seed):
    _gm(client, seed)
    used = _write_upload("a1b2c3d4e5f6-kaelen.png", _png())
    loose = _write_upload("0123456789ab-img-2041.png", _png())
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Kaelen", image_url=used))
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Dumps", image_urls_json=json.dumps([loose])))
        db.add(MediaTitle(world_id=seed.world_a.id, url=loose, title="Rainy Rooftop"))
        db.commit()
        album_id = db.query(ImageAlbum).first().id
    finally:
        db.close()
    d = _items(client, "kind=image")
    by_url = {i["url"]: i for i in d["items"]}
    assert by_url[used]["name"] == "Kaelen" and by_url[used]["used_in"] == ["Kaelen"] and not by_url[used]["generic"]
    assert by_url[loose]["name"] == "Rainy Rooftop" and by_url[loose]["album"] == "Dumps"
    assert [i["url"] for i in _items(client, f"kind=image&album={album_id}")["items"]] == [loose]
    assert d["albums"] == [{"id": album_id, "name": "Dumps"}]


# ── suggest ─────────────────────────────────────────────────────────────────────────────────────

def test_suggest_needs_a_model_and_sane_input(client, seed, monkeypatch):
    _gm(client, seed)
    ids = _clips(seed, ["track_1"])
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "")
    assert client.post("/api/media-rename/suggest", json={"items": [{"kind": "audio", "id": ids[0]}]}).status_code == 400
    _mock_names(monkeypatch)
    assert client.post("/api/media-rename/suggest", json={"items": []}).status_code == 400
    assert client.post("/api/media-rename/suggest", json={"items": "x"}).status_code == 400
    assert client.post("/api/media-rename/suggest", data="nope", headers={"content-type": "application/json"}).status_code == 400
    too_many = [{"kind": "audio", "id": ids[0]}] * 13
    assert client.post("/api/media-rename/suggest", json={"items": too_many}).status_code == 400
    assert client.post("/api/media-rename/suggest", json={"items": [{"kind": "nonsense", "id": 1}]}).status_code == 403


def test_suggest_returns_proposals_and_changes_nothing(client, seed, monkeypatch):
    _gm(client, seed)
    _mock_names(monkeypatch)
    ids = _clips(seed, ["track_1", "track_2"])
    r = client.post("/api/media-rename/suggest", json={"items": [{"kind": "audio", "id": i} for i in ids], "preset": "short", "style": "no numbers"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [x["new"] for x in d["results"]] == ["Proposed Name 1", "Proposed Name 2"] and d["errors"] == []
    assert d["results"][0]["old"] == "track_1" and d["results"][0]["changed"] is True
    db = SessionLocal()
    try:
        assert {c.name for c in db.query(AudioClip).filter(AudioClip.world_id == seed.world_a.id)} == {"track_1", "track_2"}
        assert db.query(MediaTitle).count() == 0
    finally:
        db.close()


def test_suggest_is_a_background_ai_task():
    assert ai_background.TASK_PATHS.match("/api/media-rename/suggest")
    assert ai_background.label_for("/api/media-rename/suggest") == "Media names"
    assert not ai_background.TASK_PATHS.match("/api/media-rename/apply")


# ── apply, history, undo — and the names show up ─────────────────────────────────────────────────────

def test_apply_renames_and_the_new_names_show_in_the_libraries(client, seed):
    _gm(client, seed)
    audio_id, = _clips(seed, ["track_1"])
    video_id, = _clips(seed, ["VID_1"], kind="video")
    url = _write_upload("a1b2c3d4e5f6-x.png", _png())
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Dumps", image_urls_json=json.dumps([url])))
        db.commit()
    finally:
        db.close()
    r = client.post("/api/media-rename/apply", json={"renames": [
        {"kind": "audio", "id": audio_id, "name": "Ember Waltz"}, {"kind": "video", "id": video_id, "name": "Opening Cutscene"},
        {"kind": "image", "url": url, "name": "Harbour at Dusk"}]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"] == 3 and d["skipped"] == []
    assert "Ember Waltz" in client.get("/audio").text
    assert "Opening Cutscene" in client.get("/video").text
    album_id = SessionLocal().query(ImageAlbum).first().id
    assert "Harbour at Dusk" in client.get(f"/images/albums/{album_id}").text
    hist = client.get("/api/media-rename/history").json()["batches"]
    assert hist[0]["batch_id"] == d["batch_id"] and hist[0]["count"] == 3 and hist[0]["undone"] is False
    undone = client.post("/api/media-rename/undo", json={"batch_id": d["batch_id"]})
    assert undone.status_code == 200 and undone.json()["restored"] == 3
    assert "Ember Waltz" not in client.get("/audio").text and "Harbour at Dusk" not in client.get(f"/images/albums/{album_id}").text
    assert client.get("/api/media-rename/history").json()["batches"][0]["undone"] is True


def test_apply_input_checks(client, seed):
    _gm(client, seed)
    assert client.post("/api/media-rename/apply", json={}).status_code == 400
    assert client.post("/api/media-rename/apply", json={"renames": []}).status_code == 400
    assert client.post("/api/media-rename/apply", json={"renames": "x"}).status_code == 400
    ids = _clips(seed, ["a"])
    too_many = [{"kind": "audio", "id": ids[0], "name": "x"}] * 501
    assert client.post("/api/media-rename/apply", json={"renames": too_many}).status_code == 400
    assert client.post("/api/media-rename/apply", json={"renames": ["junk"]}).status_code == 403
    assert client.post("/api/media-rename/undo", json={}).status_code == 400
    assert client.post("/api/media-rename/undo", json={"batch_id": "nope"}).json()["restored"] == 0


def test_a_renamed_image_title_is_dropped_when_the_image_is_deleted(client, seed):
    _gm(client, seed)
    url = _write_upload("a1b2c3d4e5f6-gone.png", _png())
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Dumps", image_urls_json=json.dumps([url])))
        db.commit()
    finally:
        db.close()
    assert client.post("/api/media-rename/apply", json={"renames": [{"kind": "image", "url": url, "name": "Temporary"}]}).json()["applied"] == 1
    assert client.post("/images/delete", json={"url": url}).status_code == 200
    db = SessionLocal()
    try:
        assert db.query(MediaTitle).count() == 0
    finally:
        db.close()


def test_the_entity_picker_uses_chosen_titles(client, seed):
    _gm(client, seed)
    url = _write_upload("a1b2c3d4e5f6-pick.png", _png())
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Dumps", image_urls_json=json.dumps([url])))
        db.commit()
        from app.gallery import all_world_image_urls
        before = {e["url"]: e["name"] for e in all_world_image_urls(db, db.get(World, seed.world_a.id))}
        assert before[url] == "pick.png"
    finally:
        db.close()
    client.post("/api/media-rename/apply", json={"renames": [{"kind": "image", "url": url, "name": "Chosen Name"}]})
    db = SessionLocal()
    try:
        after = {e["url"]: e["name"] for e in all_world_image_urls(db, db.get(World, seed.world_a.id))}
    finally:
        db.close()
    assert after[url] == "Chosen Name"
