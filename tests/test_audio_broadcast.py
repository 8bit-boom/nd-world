"""Tests for the "Play for players" audio broadcast feature — a GM/assistant
can push a visible AudioClip to every player's screen to auto-play in a
persistent floating widget that survives page navigation (base.html), the
same "GM pushes media to every player" mechanism as the image Spotlight
(see test_gallery.py's spotlight tests), just for audio. See
app/routers/audio.py's play-for-players/now-playing-stop routes and
app/main.py's GET /api/spotlight, extended to also report the audio
broadcast."""
from app.database import SessionLocal
from app.models import AudioClip, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


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


def _world(world_id):
    db = SessionLocal()
    try:
        return db.get(World, world_id)
    finally:
        db.close()


def _make_assistant(seed, player):
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == player.id
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()


def test_gm_play_for_players_sets_world_fields_and_bumps_version(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(f"/audio/{cid}/play-for-players")
    assert r.status_code == 200
    w = _world(seed.world_a.id)
    assert w.now_playing_url == "/uploads/audio/x.mp3"
    assert w.now_playing_label == "Tavern Ambiance"
    assert w.now_playing_loop is False
    assert w.now_playing_version == 1


def test_play_for_players_honors_loop_flag(client, seed):
    cid = _add_clip(seed.world_a.id, name="Loopy")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/play-for-players", data={"loop": "1"})
    assert r.status_code == 200
    assert _world(seed.world_a.id).now_playing_loop is True


def test_play_for_players_rejects_hidden_clip(client, seed):
    cid = _add_clip(seed.world_a.id, name="Secret", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/play-for-players")
    assert r.status_code == 400
    assert _world(seed.world_a.id).now_playing_url is None


def test_now_playing_stop_clears_and_bumps_version(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/audio/{cid}/play-for-players")
    version_after_send = _world(seed.world_a.id).now_playing_version

    r = client.post("/audio/now-playing/stop")
    assert r.status_code == 200
    w = _world(seed.world_a.id)
    assert w.now_playing_url is None
    assert w.now_playing_label is None
    assert w.now_playing_loop is False
    assert w.now_playing_version == version_after_send + 1


def test_deleting_the_broadcasting_clip_clears_now_playing(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/audio/{cid}/play-for-players")
    r = client.post(f"/audio/{cid}/delete")
    assert r.status_code in (200, 303)
    assert _world(seed.world_a.id).now_playing_url is None


def test_play_for_players_and_stop_are_gm_or_assistant_only(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/play-for-players")
    assert r.status_code == 403
    r = client.post("/audio/now-playing/stop")
    assert r.status_code == 403


def test_assistant_can_play_for_players(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    _make_assistant(seed, seed.player_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/audio/{cid}/play-for-players")
    assert r.status_code == 200


def test_api_spotlight_reports_audio_broadcast(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/audio/{cid}/play-for-players", data={"loop": "1"})

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/spotlight")
    assert r.status_code == 200
    data = r.json()
    assert data["audio_url"] == "/uploads/audio/x.mp3"
    assert data["audio_label"] == "Tavern Ambiance"
    assert data["audio_loop"] is True
    assert data["audio_version"] >= 1


def test_now_playing_is_world_scoped(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/audio/{cid}/play-for-players")

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/spotlight")
    assert r.json()["audio_url"] is None


def test_gm_only_now_playing_banner_shown_when_active(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/audio/{cid}/play-for-players")

    r = client.get("/audio")
    assert "Playing for players" in r.text
    assert "ndNowPlayingStop()" in r.text


def test_now_playing_banner_hidden_from_players_and_when_inactive(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/audio")
    assert "Playing for players" not in r.text  # nothing active yet

    client.post(f"/audio/{cid}/play-for-players")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert "Playing for players" not in r.text  # GM-only, even while active


def test_visible_clip_row_has_play_for_players_button(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert r.status_code == 200
    assert f"onclick=\"audioPlayForPlayers({cid}, this.dataset.clipName)\"" in r.text


def test_hidden_clip_row_has_no_play_for_players_button(client, seed):
    cid = _add_clip(seed.world_a.id, name="Secret", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert r.status_code == 200
    assert f"audioPlayForPlayers({cid}," not in r.text


def test_player_never_sees_play_for_players_button(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert r.status_code == 200
    assert f"audioPlayForPlayers({cid}," not in r.text


def test_now_playing_widget_present_on_every_page_for_gm_and_player(client, seed):
    """The floating widget lives in base.html (not audio_library.html), so
    it must render on an arbitrary other page too — that's the whole point
    of this feature (survives navigation, unlike the page-local pop-out)."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'id="nd-now-playing"' in r.text
    assert "function ndNowPlayingShow(" in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'id="nd-now-playing"' in r.text
