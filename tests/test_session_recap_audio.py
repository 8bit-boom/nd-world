"""Audio on a session recap (a folk song, a read-aloud recap): clips from the audio library are
attached to a session; the GM can attach an existing clip, upload one, or have Studio perform AI
lyrics; players hear only the clips marked visible to them, on the Session Log page."""
import io
import json

import pytest

from app.database import SessionLocal
from app.main import UPLOADS_DIR
from app.models import AudioClip, GameSession, World
from app.routers import session_audio as sa

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


def _gm(client, seed, world=None):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", (world or seed.world_a).slug)


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


def _clip(world, name="Ballad", visible=False, transcript=""):
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "audio").mkdir(exist_ok=True)
    fname = f"{name.lower().replace(' ', '-')}.mp3"
    (UPLOADS_DIR / "audio" / fname).write_bytes(b"ID3fake")
    db = SessionLocal()
    try:
        c = AudioClip(world_id=world.id, name=name, file_url=f"/uploads/audio/{fname}",
                      visible_to_players=visible, transcript=transcript)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def _attached(sid):
    db = SessionLocal()
    try:
        return json.loads(db.get(GameSession, sid).recap_audio_json or "[]")
    finally:
        db.close()


def test_attach_an_existing_clip_and_list_it(client, seed):
    _gm(client, seed)
    sid, cid = _session(seed), _clip(seed.world_a, "Ballad of the Ledger", transcript="Oh the ledger burned")
    r = client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": cid})
    assert r.status_code == 200, r.text
    assert _attached(sid) == [cid]
    d = client.get(f"/api/sessions/{sid}/recap-audio").json()
    assert [c["id"] for c in d["clips"]] == [cid] and d["clips"][0]["transcript"] == "Oh the ledger burned"
    assert d["clips"][0]["visible_to_players"] is False and d["library"] == [], "attached clips leave the picker"
    other = _clip(seed.world_a, "Drone")
    assert [c["id"] for c in client.get(f"/api/sessions/{sid}/recap-audio").json()["library"]] == [other]
    # attaching twice is a no-op, not a duplicate
    client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": cid})
    assert _attached(sid) == [cid]


def test_a_clip_can_be_published_to_players_when_attached(client, seed):
    _gm(client, seed)
    sid, cid = _session(seed), _clip(seed.world_a)
    client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": cid, "visible_to_players": True})
    assert client.get(f"/api/sessions/{sid}/recap-audio").json()["clips"][0]["visible_to_players"] is True
    r = client.post(f"/api/sessions/{sid}/recap-audio/{cid}/visibility", json={"visible_to_players": False})
    assert r.status_code == 200
    assert client.get(f"/api/sessions/{sid}/recap-audio").json()["clips"][0]["visible_to_players"] is False


def test_remove_detaches_but_keeps_the_clip_in_the_library(client, seed):
    _gm(client, seed)
    sid, cid = _session(seed), _clip(seed.world_a)
    client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": cid})
    assert client.post(f"/api/sessions/{sid}/recap-audio/{cid}/remove").status_code == 200
    assert _attached(sid) == []
    db = SessionLocal()
    try:
        assert db.get(AudioClip, cid) is not None
    finally:
        db.close()


def test_only_this_worlds_clips_and_sessions_can_be_used(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    foreign = _clip(seed.world_b, "Other world")
    assert client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": foreign}).status_code == 404
    assert client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": 999999}).status_code == 404
    other_session = _session(seed, world=seed.world_b)
    assert client.get(f"/api/sessions/{other_session}/recap-audio").status_code == 404
    assert client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": "abc"}).status_code == 400


def test_the_number_of_attached_clips_is_capped(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    ids = [_clip(seed.world_a, f"Clip {i}") for i in range(sa.MAX_RECAP_CLIPS + 1)]
    for cid in ids[:-1]:
        assert client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": cid}).status_code == 200
    r = client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": ids[-1]})
    assert r.status_code == 400 and str(sa.MAX_RECAP_CLIPS) in r.text


def test_upload_creates_a_library_clip_and_attaches_it(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    r = client.post(f"/api/sessions/{sid}/recap-audio/upload",
                    data={"name": "Folk song", "lyrics": "Verse one", "visible_to_players": "true"},
                    files={"file": ("song.mp3", io.BytesIO(b"ID3data"), "audio/mpeg")})
    assert r.status_code == 200, r.text
    cid = r.json()["clip"]["id"]
    assert _attached(sid) == [cid]
    db = SessionLocal()
    try:
        c = db.get(AudioClip, cid)
        assert c.name == "Folk song" and c.transcript == "Verse one" and c.visible_to_players is True
        assert c.world_id == seed.world_a.id and (UPLOADS_DIR / "audio" / c.file_url.rsplit("/", 1)[1]).is_file()
    finally:
        db.close()
    bad = client.post(f"/api/sessions/{sid}/recap-audio/upload", files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert bad.status_code == 400 and "Unsupported" in bad.text
    assert client.post(f"/api/sessions/{sid}/recap-audio/upload").status_code in (400, 422)


# ── performing AI lyrics with Studio TTS ─────────────────────────────────────

def test_a_song_is_performed_by_tts_and_attached(client, seed, monkeypatch):
    _gm(client, seed)
    sid = _session(seed)
    seen = {}

    async def fake_tts(text, model, voice="", response_format="mp3", speed=1.0, instructions="", language=""):
        seen.update(text=text, model=model, instructions=instructions, voice=voice)
        return b"ID3sung", "audio/mpeg"

    monkeypatch.setattr(sa._unsloth_extras, "tts", fake_tts)
    monkeypatch.setattr(sa._ai, "get_tts_model", lambda: "tts-model")
    lyrics = "## The Ledger Burns\n\n*Oh* the ledger **burned** bright,\n\n\n\nand the clerks ran into the night [chorus]"
    r = client.post(f"/api/sessions/{sid}/recap-song", json={"lyrics": lyrics, "name": "Ledger Song", "style": "sea shanty"})
    assert r.status_code == 200, r.text
    assert seen["model"] == "tts-model"
    assert "sea shanty" in seen["instructions"].lower() and "sing" in seen["instructions"].lower()
    assert "#" not in seen["text"] and "*" not in seen["text"] and "[chorus]" not in seen["text"], "markup is not read aloud"
    assert "ledger burned bright" in seen["text"] and "\n\n\n" not in seen["text"]
    cid = r.json()["clip"]["id"]
    assert _attached(sid) == [cid]
    db = SessionLocal()
    try:
        c = db.get(AudioClip, cid)
        assert c.name == "Ledger Song" and c.visible_to_players is False, "starts GM-only until reviewed"
        assert "Oh* the ledger" in c.transcript or "ledger **burned**" in c.transcript, "the lyrics stay with the clip"
    finally:
        db.close()


def test_song_errors_are_clean(client, seed, monkeypatch):
    _gm(client, seed)
    sid = _session(seed)
    assert client.post(f"/api/sessions/{sid}/recap-song", json={"lyrics": "  "}).status_code == 400
    assert client.post(f"/api/sessions/{sid}/recap-song", json={"lyrics": "x" * 20001}).status_code == 400

    async def missing(*a, **k):
        raise sa._unsloth_extras.StudioMissing("Unsloth Studio is not configured")

    monkeypatch.setattr(sa._unsloth_extras, "tts", missing)
    r = client.post(f"/api/sessions/{sid}/recap-song", json={"lyrics": "La la la"})
    assert r.status_code == 400 and "not configured" in r.text
    assert _attached(sid) == []


# ── who may do what, and what players see ────────────────────────────────────

def test_players_cannot_change_recap_audio(client, seed):
    sid, cid = _session(seed), _clip(seed.world_a, visible=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for method, path, kw in (("get", f"/api/sessions/{sid}/recap-audio", {}),
                             ("post", f"/api/sessions/{sid}/recap-audio", {"json": {"clip_id": cid}}),
                             ("post", f"/api/sessions/{sid}/recap-audio/{cid}/remove", {}),
                             ("post", f"/api/sessions/{sid}/recap-song", {"json": {"lyrics": "la"}})):
        assert getattr(client, method)(path, **kw).status_code == 403, path


def test_players_hear_only_visible_clips_on_the_session_log(client, seed):
    sid = _session(seed)
    shown = _clip(seed.world_a, "Public Ballad", visible=True, transcript="Verse one\nVerse two")
    hidden = _clip(seed.world_a, "Secret Dirge", visible=False)
    db = SessionLocal()
    try:
        db.get(GameSession, sid).recap_audio_json = json.dumps([shown, hidden, 424242])   # 424242 = since deleted
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/session-log/{sid}").text
    assert "Public Ballad" in html and "/uploads/audio/public-ballad.mp3" in html and 'id="recap-audio"' in html
    assert "Verse two" in html, "lyrics show under the player"
    assert "Secret Dirge" not in html and "secret-dirge" not in html
    # the session list marks sessions that have a song to hear
    assert "Song / audio to listen to" in client.get("/session-log").text
    # the GM sees every attached clip, hidden ones flagged
    client.cookies.clear()
    _gm(client, seed)
    gm_html = client.get(f"/session-log/{sid}").text
    assert "Secret Dirge" in gm_html and "Public Ballad" in gm_html


def test_a_session_with_only_hidden_audio_shows_players_nothing(client, seed):
    sid, cid = _session(seed), _clip(seed.world_a, "Hidden", visible=False)
    db = SessionLocal()
    try:
        db.get(GameSession, sid).recap_audio_json = json.dumps([cid])
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/session-log/{sid}").text
    assert 'id="recap-audio"' not in html and "Hidden" not in html
    assert "Song / audio to listen to" not in client.get("/session-log").text


def test_the_gm_session_page_has_the_recap_audio_panel(client, seed):
    _gm(client, seed)
    sid = _session(seed)
    html = client.get(f"/sessions/{sid}").text
    assert 'id="recap-audio-panel"' in html and f"/api/sessions/{sid}/recap-audio" in html
    assert "Perform it" in html, "the folk-tale result can be performed"
    new_page = client.get("/sessions/new").text
    assert 'id="recap-audio-panel"' not in new_page, "needs a saved session"


def _make_assistant(seed, player):
    from app.models import WorldMembership
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == player.id).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()


def test_an_assistant_needs_the_sessions_edit_level(client, seed):
    """The route allowlist admits every assistant; the handler enforces Settings → Navigation's tier."""
    _make_assistant(seed, seed.player_a)
    sid, cid = _session(seed), _clip(seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/sessions/{sid}/recap-audio", json={"clip_id": cid}).status_code == 200
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.section_access_json = json.dumps({"sessions": {"assistant": "read"}})
        db.commit()
    finally:
        db.close()
    assert client.post(f"/api/sessions/{sid}/recap-audio/{cid}/remove").status_code == 403
    assert client.get(f"/api/sessions/{sid}/recap-audio").status_code == 403


def test_the_style_picker_only_offers_styles_the_server_knows(client, seed):
    import re
    _gm(client, seed)
    html = client.get(f"/sessions/{_session(seed)}").text
    block = html.split('id="tale-style"')[1].split("</select>")[0]
    offered = re.findall(r'<option value="([^"]+)"', block)
    assert offered and set(offered) == set(sa.SONG_STYLES)
    for style in offered:
        assert style.split()[0] in sa.song_instructions(style).lower() or style in sa.song_instructions(style).lower()


def test_lyrics_for_speech_unit():
    out = sa.lyrics_for_speech("# Title\n\n**Bold** line [Chorus]\nNormal _line_ (repeat x2)\n\n\n\nLast")
    assert out == "Title\n\nBold line\nNormal line\n\nLast"
