"""Session media: pictures, videos and several audio clips on a session (up to a limit that Settings can change,
10 by default), dropped onto the session page or picked from the libraries, and shown to the players on the Session
Log - only the items the GM ticked "players can see".

Audio already had its own panel (tests/test_session_recap_audio.py); this generalises it: the same session, the same
Session Log, one shared limit over audio + video + images."""
import io
import json

import pytest

from app import session_media as sm
from app.database import SessionLocal, get_app_settings
from app.main import UPLOADS_DIR
from app.models import AudioClip, GameSession, ImageAlbum, VideoClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 600


def _gm(client, seed, world=None):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", (world or seed.world_a).slug)


def _player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _session(seed, world=None, **kw):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=(world or seed.world_a).id, title=kw.pop("title", "The Night Market"),
                         session_num=kw.pop("num", 3), summary="They burned the ledger.", **kw)
        db.add(gs)
        db.commit()
        return gs.id
    finally:
        db.close()


def _set_limit(value):
    db = SessionLocal()
    try:
        get_app_settings(db).session_media_max = value
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _default_limit(client, seed):
    _set_limit(None)
    yield
    _set_limit(None)


def _upload(client, sid, name, data=PNG, ctype="application/octet-stream", **form):
    return client.post(f"/api/sessions/{sid}/media/upload", data=form, files={"file": (name, io.BytesIO(data), ctype)})


def _panel(client, sid):
    r = client.get(f"/api/sessions/{sid}/media")
    assert r.status_code == 200, r.text
    return r.json()


def _video_clip(world, name="Cutscene", visible=False):
    (UPLOADS_DIR / "video").mkdir(parents=True, exist_ok=True)
    fname = f"{name.lower().replace(' ', '-')}.mp4"
    (UPLOADS_DIR / "video" / fname).write_bytes(b"\x00\x00\x00\x18ftypmp42")
    db = SessionLocal()
    try:
        c = VideoClip(world_id=world.id, name=name, file_url=f"/uploads/video/{fname}", visible_to_players=visible)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def _audio_clip(world, name="Ballad", visible=False):
    (UPLOADS_DIR / "audio").mkdir(parents=True, exist_ok=True)
    fname = f"{name.lower().replace(' ', '-')}.mp3"
    (UPLOADS_DIR / "audio" / fname).write_bytes(b"ID3fake")
    db = SessionLocal()
    try:
        c = AudioClip(world_id=world.id, name=name, file_url=f"/uploads/audio/{fname}", visible_to_players=visible)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def _gallery_image(world, name="map-of-the-market.png"):
    (UPLOADS_DIR / "gallery").mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "gallery" / name).write_bytes(PNG)
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=world.id, name="Maps", image_urls_json=json.dumps([f"/uploads/gallery/{name}"])))
        db.commit()
    finally:
        db.close()
    return f"/uploads/gallery/{name}"


# ── the limit ────────────────────────────────────────────────────────────────────────────────────────

def test_the_default_limit_is_ten_and_the_setting_changes_it(client, seed):
    db = SessionLocal()
    try:
        assert sm.limit(db) == 10 == sm.DEFAULT_MAX
        for raw, expected in ((3, 3), (1, 1), (50, 50), (500, 50), (0, 10), (-4, 10), (None, 10)):
            get_app_settings(db).session_media_max = raw
            db.commit()
            assert sm.limit(db) == expected, raw
    finally:
        db.close()


def test_the_panel_reports_the_limit_and_how_much_is_used(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    d = _panel(client, sid)
    assert d["max"] == 10 and d["used"] == 0 and d["images"] == [] and d["videos"] == [] and d["clips"] == []
    _set_limit(4)
    assert _panel(client, sid)["max"] == 4


def test_the_limit_counts_audio_video_and_images_together(client, seed):
    _gm(client, seed)
    _set_limit(3)
    sid = _session(seed)
    assert _upload(client, sid, "one.png").status_code == 200
    assert _upload(client, sid, "song.mp3", b"ID3data").status_code == 200
    vid = _video_clip(seed.world_a)
    assert client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "video", "id": vid}).status_code == 200
    assert _panel(client, sid)["used"] == 3
    # one more of ANY kind is refused, with the limit in the message and nothing left behind
    before = sorted(p.name for p in (UPLOADS_DIR / "gallery").glob("*")) if (UPLOADS_DIR / "gallery").exists() else []
    r = _upload(client, sid, "two.png")
    assert r.status_code == 400 and "3" in r.text
    assert (sorted(p.name for p in (UPLOADS_DIR / "gallery").glob("*")) if (UPLOADS_DIR / "gallery").exists() else []) == before
    r = client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": _audio_clip(seed.world_a)})
    assert r.status_code == 400 and "3" in r.text, "the older audio route obeys the same limit"
    # removing one makes room again
    first = _panel(client, sid)["images"][0]
    assert client.post(f"/api/sessions/{sid}/media/image/{first['uid']}/remove").status_code == 200
    assert _upload(client, sid, "two.png").status_code == 200


def test_lowering_the_limit_below_what_a_session_holds_blocks_adding_but_keeps_what_is_there(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    for n in range(3):
        assert _upload(client, sid, f"p{n}.png").status_code == 200
    _set_limit(2)
    d = _panel(client, sid)
    assert d["used"] == 3 and len(d["images"]) == 3
    assert _upload(client, sid, "more.png").status_code == 400


# ── pictures ─────────────────────────────────────────────────────────────────────────────────────────

def test_dropping_pictures_stores_them_hidden_from_players_by_default(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    r = _upload(client, sid, "Market Square.png", PNG, "image/png")
    assert r.status_code == 200, r.text
    img = r.json()["images"][0]
    assert img["url"].startswith("/uploads/gallery/") and img["visible_to_players"] is False
    assert img["caption"] == "Market Square" and img["uid"]
    assert (UPLOADS_DIR / "gallery" / img["url"].rsplit("/", 1)[1]).is_file()
    r = _upload(client, sid, "shown.png", PNG, "image/png", visible_to_players="true")
    assert r.json()["images"][1]["visible_to_players"] is True
    assert r.json()["used"] == 2


def test_an_unsupported_file_is_refused(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    r = _upload(client, sid, "virus.exe", b"MZ")
    assert r.status_code == 400 and "Unsupported" in r.text
    assert client.post(f"/api/sessions/{sid}/media/upload").status_code in (400, 422)
    assert _panel(client, sid)["used"] == 0


def test_a_picture_from_the_gallery_can_be_attached_once(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    url = _gallery_image(seed.world_a)
    r = client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "image", "url": url})
    assert r.status_code == 200, r.text
    assert [i["url"] for i in r.json()["images"]] == [url] and r.json()["images"][0]["own"] is False
    again = client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "image", "url": url})
    assert again.status_code == 200 and again.json()["used"] == 1, "attaching twice is a no-op"


def test_only_pictures_of_this_world_can_be_attached(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    (UPLOADS_DIR / "gallery").mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "gallery" / "stray.png").write_bytes(PNG)
    for url in ("/uploads/gallery/stray.png", "/uploads/../../etc/passwd", "https://example.com/x.png", "/static/style.css", ""):
        r = client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "image", "url": url})
        assert r.status_code in (400, 404), url
    assert _panel(client, sid)["used"] == 0


def test_picture_visibility_and_removal(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    uid = _upload(client, sid, "a.png").json()["images"][0]["uid"]
    r = client.post(f"/api/sessions/{sid}/media/image/{uid}/visibility", json={"visible_to_players": True})
    assert r.status_code == 200 and r.json()["images"][0]["visible_to_players"] is True
    assert client.post(f"/api/sessions/{sid}/media/image/{uid}/visibility", json={"visible_to_players": "yes"}).status_code == 400
    assert client.post(f"/api/sessions/{sid}/media/image/nope/visibility", json={"visible_to_players": True}).status_code == 404
    path = UPLOADS_DIR / "gallery" / _panel(client, sid)["images"][0]["url"].rsplit("/", 1)[1]
    assert path.is_file()
    assert client.post(f"/api/sessions/{sid}/media/image/{uid}/remove").json()["images"] == []
    assert not path.exists(), "a picture uploaded for the session goes with it"


def test_removing_a_gallery_picture_never_deletes_the_gallery_file(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    url = _gallery_image(seed.world_a, "keep-me.png")
    client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "image", "url": url})
    uid = _panel(client, sid)["images"][0]["uid"]
    client.post(f"/api/sessions/{sid}/media/image/{uid}/remove")
    assert (UPLOADS_DIR / "gallery" / "keep-me.png").is_file()


# ── videos ───────────────────────────────────────────────────────────────────────────────────────────

def test_dropping_a_video_adds_it_to_the_library_and_the_session(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    r = _upload(client, sid, "Intro Cutscene.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100, "video/mp4")
    assert r.status_code == 200, r.text
    v = r.json()["videos"][0]
    assert v["name"] == "Intro Cutscene" and v["file_url"].startswith("/uploads/video/") and v["visible_to_players"] is False
    db = SessionLocal()
    try:
        clip = db.get(VideoClip, v["id"])
        assert clip.world_id == seed.world_a.id and (UPLOADS_DIR / "video" / clip.file_url.rsplit("/", 1)[1]).is_file()
    finally:
        db.close()


def test_the_library_lists_carry_what_a_preview_needs(client, seed):
    """The picker plays an audio clip / shows a video's poster BEFORE it is added, so the library entries name their files."""
    _gm(client, seed)
    sid = _session(seed)
    vid, aud = _video_clip(seed.world_a, "Cutscene"), _audio_clip(seed.world_a, "Ballad")
    d = _panel(client, sid)
    v = next(x for x in d["video_library"] if x["id"] == vid)
    a = next(x for x in d["library"] if x["id"] == aud)
    assert v["file_url"].startswith("/uploads/video/") and "poster_url" in v and v["visible_to_players"] is False
    assert a["file_url"].startswith("/uploads/audio/") and a["name"] == "Ballad"
    # picking several at once is just several attaches, and the room limit still applies
    _set_limit(2)
    try:
        assert client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "video", "id": vid}).status_code == 200
        assert client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "audio", "id": aud}).status_code == 200
        extra = _video_clip(seed.world_a, "One too many")
        assert client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "video", "id": extra}).status_code >= 400
    finally:
        _set_limit(10)


def test_attach_toggle_and_detach_a_library_video(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    vid, other = _video_clip(seed.world_a, "Cutscene"), _video_clip(seed.world_a, "Trailer")
    d = client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "video", "id": vid}).json()
    assert [v["id"] for v in d["videos"]] == [vid]
    assert [v["id"] for v in d["video_library"]] == [other], "attached videos leave the picker"
    d = client.post(f"/api/sessions/{sid}/media/video/{vid}/visibility", json={"visible_to_players": True}).json()
    assert d["videos"][0]["visible_to_players"] is True
    d = client.post(f"/api/sessions/{sid}/media/video/{vid}/remove").json()
    assert d["videos"] == []
    db = SessionLocal()
    try:
        assert db.get(VideoClip, vid) is not None, "the video stays in the library"
    finally:
        db.close()


def test_a_video_of_another_world_cannot_be_attached(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    foreign = _video_clip(seed.world_b, "Foreign")
    assert client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "video", "id": foreign}).status_code == 404


# ── audio through the same door ──────────────────────────────────────────────────────────────────────

def test_several_audio_clips_can_be_dropped_and_each_is_attached(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    for n in range(4):
        r = _upload(client, sid, f"song{n}.mp3", b"ID3data", "audio/mpeg")
        assert r.status_code == 200, r.text
    d = _panel(client, sid)
    assert len(d["clips"]) == 4 and d["used"] == 4
    db = SessionLocal()
    try:
        assert json.loads(db.get(GameSession, sid).recap_audio_json) == [c["id"] for c in d["clips"]]
    finally:
        db.close()


def test_audio_visibility_and_removal_work_through_the_media_routes_too(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    cid = _upload(client, sid, "s.mp3", b"ID3").json()["clips"][0]["id"]
    d = client.post(f"/api/sessions/{sid}/media/audio/{cid}/visibility", json={"visible_to_players": True}).json()
    assert d["clips"][0]["visible_to_players"] is True
    assert client.post(f"/api/sessions/{sid}/media/audio/{cid}/remove").json()["clips"] == []


def test_the_old_audio_panel_routes_still_return_everything(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    _upload(client, sid, "a.png")
    d = client.get(f"/api/sessions/{sid}/recap-audio").json()
    assert len(d["images"]) == 1 and d["max"] == 10 and d["clips"] == []


# ── chunked upload (a big video through a proxy with a body cap) ─────────────────────────────────────

def test_a_file_sent_in_parts_is_assembled_and_added(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    upload_id = "ab" * 16
    half = len(PNG) // 2
    for i, part in enumerate((PNG[:half], PNG[half:])):
        r = client.post(f"/api/sessions/{sid}/media/upload/chunk", data={"upload_id": upload_id, "chunk_index": i},
                        files={"file": ("part", io.BytesIO(part), "application/octet-stream")})
        assert r.status_code == 200, r.text
    r = client.post(f"/api/sessions/{sid}/media/upload/complete",
                    data={"upload_id": upload_id, "filename": "big map.png", "total_chunks": 2, "visible_to_players": "true"})
    assert r.status_code == 200, r.text
    img = r.json()["images"][0]
    assert img["caption"] == "big map" and img["visible_to_players"] is True
    assert (UPLOADS_DIR / "gallery" / img["url"].rsplit("/", 1)[1]).read_bytes()[:8] == PNG[:8]


def test_a_chunked_upload_over_the_limit_leaves_no_file_behind(client, seed):
    _gm(client, seed)
    _set_limit(1)
    sid = _session(seed)
    assert _upload(client, sid, "first.png").status_code == 200
    upload_id = "cd" * 16
    client.post(f"/api/sessions/{sid}/media/upload/chunk", data={"upload_id": upload_id, "chunk_index": 0},
                files={"file": ("part", io.BytesIO(PNG), "application/octet-stream")})
    before = sorted(p.name for p in (UPLOADS_DIR / "gallery").glob("*"))
    r = client.post(f"/api/sessions/{sid}/media/upload/complete",
                    data={"upload_id": upload_id, "filename": "second.png", "total_chunks": 1})
    assert r.status_code == 400
    assert sorted(p.name for p in (UPLOADS_DIR / "gallery").glob("*")) == before


# ── who may do what ──────────────────────────────────────────────────────────────────────────────────

def test_players_cannot_change_or_list_session_media(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    _player(client, seed)
    assert client.get(f"/api/sessions/{sid}/media").status_code == 403
    assert _upload(client, sid, "x.png").status_code == 403
    assert client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "video", "id": 1}).status_code == 403
    assert client.post(f"/api/sessions/{sid}/media/image/x/remove").status_code == 403


def test_another_worlds_session_is_a_404(client, seed):
    _gm(client, seed)
    sid = _session(seed, seed.world_b)
    assert client.get(f"/api/sessions/{sid}/media").status_code == 404


# ── what players see ─────────────────────────────────────────────────────────────────────────────────

def test_players_see_only_the_ticked_media_on_the_session_log(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    shown = _upload(client, sid, "Visible Map.png", visible_to_players="true").json()["images"][0]
    hidden = _upload(client, sid, "Secret Map.png").json()["images"][1]
    vid_shown, vid_hidden = _video_clip(seed.world_a, "Public Cutscene", True), _video_clip(seed.world_a, "Secret Cutscene", False)
    for v in (vid_shown, vid_hidden):
        client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "video", "id": v})
    _player(client, seed)
    page = client.get(f"/session-log/{sid}").text
    assert shown["url"] in page and "Visible Map" in page
    assert hidden["url"] not in page and "Secret Map" not in page
    assert "/uploads/video/public-cutscene.mp4" in page and "Public Cutscene" in page
    assert "secret-cutscene" not in page and "Secret Cutscene" not in page
    assert 'id="session-media"' in page and "<video" in page


def test_the_gm_sees_everything_on_the_session_log_with_hidden_items_flagged(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    hidden = _upload(client, sid, "Secret Map.png").json()["images"][0]
    page = client.get(f"/session-log/{sid}").text
    assert hidden["url"] in page and "GM only" in page


def test_a_session_with_no_visible_media_shows_players_no_media_section(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    _upload(client, sid, "Secret.png")
    _player(client, seed)
    assert 'id="session-media"' not in client.get(f"/session-log/{sid}").text


def test_the_session_list_marks_sessions_with_media_players_can_see(client, seed):
    _gm(client, seed)
    with_pic, without = _session(seed, title="With a picture", num=1), _session(seed, title="Nothing shared", num=2)
    _upload(client, with_pic, "shown.png", visible_to_players="true")
    _upload(client, without, "hidden.png")
    _player(client, seed)
    page = client.get("/session-log").text
    cards = page.split('<a href="')[1:]
    card = lambda title: next(c for c in cards if title in c)
    assert "🖼" in card("With a picture") and "🖼" not in card("Nothing shared")


# ── how the rest of the app sees session pictures ────────────────────────────────────────────────────

def test_session_pictures_count_as_used_in_the_gallery_and_cannot_be_deleted_from_under_the_session(client, seed):
    _gm(client, seed)
    sid = _session(seed, title="Heist Night")
    url = _upload(client, sid, "Plan.png").json()["images"][0]["url"]
    from app.gallery import discover_world_images
    db = SessionLocal()
    try:
        entry = {e["url"]: e for e in discover_world_images(db, seed.world_a)}[url]
        assert any("Heist Night" in u["label"] for u in entry["uses"]) and entry["uses"][0]["href"] == f"/sessions/{sid}"
    finally:
        db.close()
    r = client.post("/images/delete", json={"url": url})
    assert r.status_code == 400 and "Heist Night" in r.text


def test_deleting_a_session_deletes_the_pictures_uploaded_for_it_but_not_gallery_ones(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    own = _upload(client, sid, "own.png").json()["images"][0]["url"]
    shared = _gallery_image(seed.world_a, "shared-art.png")
    client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "image", "url": shared})
    assert client.post(f"/sessions/{sid}/delete", follow_redirects=False).status_code == 303
    assert not (UPLOADS_DIR / "gallery" / own.rsplit("/", 1)[1]).exists()
    assert (UPLOADS_DIR / "gallery" / "shared-art.png").is_file()


# ── the page and the setting ─────────────────────────────────────────────────────────────────────────

def test_the_gm_session_page_has_the_drop_zone_and_the_pickers(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    page = client.get(f"/sessions/{sid}").text
    for marker in ('id="session-media-panel"', 'id="sm-drop"', 'id="sm-file"', 'multiple', 'id="sm-choose"', 'id="smp"', 'smpOpen', 'addEventListener(\'paste\'',
                   f"/api/sessions/{sid}/media", "SM_BASE + '/upload'", "smDropFiles", "Session media", "addEventListener('drop'"):
        assert marker in page, marker
    assert "ndGalleryPickerOpen" in page and 'id="gallery-picker-overlay"' in page


def test_the_limit_can_be_changed_in_settings(client, seed):
    _gm(client, seed)
    page = client.get("/settings?tab=system").text
    assert 'name="session_media_max"' in page
    body = {"session_media_max": "25"}
    r = client.post("/settings/system", data=body, follow_redirects=False)
    assert r.status_code in (200, 303), r.text[:200]
    db = SessionLocal()
    try:
        assert get_app_settings(db).session_media_max == 25
    finally:
        db.close()
    r = client.post("/settings/system", data={"session_media_max": "0"}, follow_redirects=False)
    assert r.status_code == 400
    r = client.post("/settings/system", data={"session_media_max": ""}, follow_redirects=False)
    assert r.status_code in (200, 303)
    db = SessionLocal()
    try:
        assert get_app_settings(db).session_media_max is None
    finally:
        db.close()


def test_a_clip_can_be_attached_by_the_address_copied_from_the_library(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    aud, vid = _audio_clip(seed.world_a, "Ballad"), _video_clip(seed.world_a, "Cutscene")
    db = SessionLocal()
    try:
        a_url, v_url = db.get(AudioClip, aud).file_url, db.get(VideoClip, vid).file_url
    finally:
        db.close()
    d = client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "audio", "url": a_url}).json()
    assert [c["id"] for c in d["clips"]] == [aud]
    d = client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "video", "url": v_url}).json()
    assert [v["id"] for v in d["videos"]] == [vid]
    assert client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "audio", "url": "/uploads/audio/nope.mp3"}).status_code == 404
    assert client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "audio", "url": v_url}).status_code == 404     # a video's address is not an audio clip
    other = _session(seed, world=seed.world_b) if hasattr(seed, "world_b") else None
    if other:                                                                                                       # another world's clip is refused
        foreign = _audio_clip(seed.world_b, "Foreign")
        db = SessionLocal()
        try:
            f_url = db.get(AudioClip, foreign).file_url
        finally:
            db.close()
        assert client.post(f"/api/sessions/{sid}/media/attach", json={"kind": "audio", "url": f_url}).status_code == 404
