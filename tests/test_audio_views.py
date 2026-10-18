"""Audio layouts: the registry's audio section, the filters / grouping the engine gained for it, the playlist order and pad hotkeys
(static/js/audio-views.js under Node), and the page wiring."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import AudioClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def _run(file, script):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/{file}');" + script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_audio_section_offers_rows_compact_playlist_and_a_soundboard():
    sp = view_prefs.spec("audio")
    assert [m["id"] for m in sp["modes"]] == ["rows", "compact", "playlist", "pads"] and sp["default"] == "rows"
    assert sp["size_modes"] == ["pads"]                                     # only the soundboard has a size
    assert [g["id"] for g in sp["groups"]] == ["visibility"] and [f["id"] for f in sp["filters"]] == ["hidden"]
    assert view_prefs.clean("audio", {"mode": "grid"})["mode"] == "rows"      # a picture layout is not an audio one
    assert view_prefs.spec("images")["size_modes"] == ["grid", "masonry", "filmstrip"]


@needs_node
def test_engine_groups_by_visibility_and_filters_hidden_clips():
    metas = json.dumps([{"name": "a", "vis": ""}, {"name": "b", "vis": "hidden"}, {"name": "c", "vis": ""}])
    spec = json.dumps(view_prefs.spec("audio"))

    def plan(state):
        return _run("nd-views.js", f"console.log(JSON.stringify(c.plan({metas}, c.cleanState({json.dumps(state)}, {spec}))))")
    grouped = plan({"group": "visibility"})["entries"]
    assert [e.get("head") or e["item"] for e in grouped] == ["Visible to players", 0, 2, "Hidden from players", 1]
    only = plan({"filters": ["hidden"]})
    assert only["hidden"] == [0, 2] and [e["item"] for e in only["entries"]] == [1]


@needs_node
def test_playlist_order_and_pad_hotkeys():
    nxt = lambda *a: _run("audio-views.js", f"console.log(JSON.stringify(c.nextIndex(...{json.dumps(list(a))})))")
    assert nxt(0, 3, {}) == 1 and nxt(2, 3, {}) == -1 and nxt(2, 3, {"repeat": True}) == 0       # in order; the end; repeat all
    assert nxt(0, 0, {}) == -1 and nxt(0, 1, {"shuffle": True}) == -1 and nxt(0, 1, {"shuffle": True, "repeat": True}) == 0
    for current in range(4):                                                                       # shuffle never repeats the same clip
        for r in (0, 0.2, 0.5, 0.99):
            got = _run("audio-views.js", f"console.log(JSON.stringify(c.nextIndex({current}, 4, {{shuffle: true}}, () => {r})))")
            assert got != current and 0 <= got < 4
    assert _run("audio-views.js", "console.log(JSON.stringify([c.nextIndex(1, 4, {shuffle: true}, () => 0), c.nextIndex(1, 4, {shuffle: true}, () => 0.99)]))") == [0, 3]
    assert _run("audio-views.js", "console.log(JSON.stringify([c.prevIndex(0, 3), c.prevIndex(2, 3), c.prevIndex(0, 0)]))") == [2, 1, -1]
    assert _run("audio-views.js", "console.log(JSON.stringify(['1', '9', '0', 'a', '5'].map(k => c.padForKey(k, 6))))") == [0, -1, -1, -1, 4]


def _login(client, seed, player=False):
    client.cookies.clear()
    login(client, (seed.player_a if player else seed.gm).email, PLAYER_PASSWORD if player else GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_the_audio_page_has_the_switcher_data_and_scripts_for_gm_and_players(client, seed):
    db = SessionLocal()
    try:
        db.add(AudioClip(world_id=seed.world_a.id, name="Tavern", file_url="/uploads/audio/t.mp3", visible_to_players=True))
        db.add(AudioClip(world_id=seed.world_a.id, name="Secret", file_url="/uploads/audio/s.mp3", visible_to_players=False))
        db.commit()
    finally:
        db.close()
    _login(client, seed)
    page = client.get("/audio").text
    assert 'data-for="audio-clips"' in page and 'class="audio-clip-list nd-collection"' in page
    assert page.count('class="audio-clip-row nd-item"') == 2 and 'data-vis="hidden"' in page
    assert "/static/js/nd-views.js" in page and "/static/js/audio-views.js" in page
    _login(client, seed, player=True)
    seen = client.get("/audio").text
    assert seen.count('class="audio-clip-row nd-item"') == 1 and "Secret" not in seen and 'data-for="audio-clips"' in seen   # players: their clips, with the switcher


def test_the_video_section_and_page(client, seed):
    from app.models import VideoClip
    sp = view_prefs.spec("video")
    assert [m["id"] for m in sp["modes"]] == ["rows", "compact", "posters"] and sp["size_modes"] == ["posters"]
    db = SessionLocal()
    try:
        db.add(VideoClip(world_id=seed.world_a.id, name="Cutscene", file_url="/uploads/video/c.mp4", visible_to_players=True))
        db.add(VideoClip(world_id=seed.world_a.id, name="Spoiler", file_url="/uploads/video/s.mp4", visible_to_players=False))
        db.commit()
    finally:
        db.close()
    _login(client, seed)
    page = client.get("/video").text
    assert 'data-for="video-clips"' in page and page.count('class="video-clip-row nd-item"') == 2 and 'data-vis="hidden"' in page
    assert "/static/js/nd-views.js" in page
    _login(client, seed, player=True)
    seen = client.get("/video").text
    assert seen.count('class="video-clip-row nd-item"') == 1 and "Spoiler" not in seen
