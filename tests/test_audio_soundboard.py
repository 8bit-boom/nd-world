"""Tests for plan item QoL 3.5: the Audio Library as a soundboard — a loop
toggle and a pop-out mini-player per clip (app/templates/audio_library.html).
Entirely client-side (audioToggleLoop/audioPopOut/audioMiniPlayerReturn) —
no new backend route, so these confirm the markup/JS are wired correctly;
the actual pop-out/loop behavior (does playback survive the DOM move?) is
covered by a live browser check (see session notes)."""
from app.database import SessionLocal
from app.models import AudioClip

from .conftest import GM_PASSWORD, login


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


def test_clip_row_has_loop_and_popout_controls(client, seed):
    cid = _add_clip(seed.world_a.id, name="Tavern Ambiance")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/audio")
    assert r.status_code == 200
    assert f'id="audio-el-{cid}"' in r.text
    assert f'id="audio-slot-{cid}"' in r.text
    assert f"onclick=\"audioToggleLoop({cid}, this)\"" in r.text
    assert f'onclick="audioPopOut({cid}, this.dataset.clipName)"' in r.text
    assert 'data-clip-name="Tavern Ambiance"' in r.text
    assert 'id="audio-mini-player"' in r.text
    assert "function audioToggleLoop(" in r.text
    assert "function audioPopOut(" in r.text
    assert "function audioMiniPlayerReturn(" in r.text


def test_clip_name_with_quotes_does_not_break_the_onclick_attribute(client, seed):
    # Regression: an earlier version embedded the name via {{ c.name|tojson }}
    # directly inside the onclick="..." attribute — tojson's raw JSON double
    # quotes terminate an HTML double-quoted attribute early, breaking the
    # handler for any clip name containing a double quote. Fixed by carrying
    # the name through a normal (HTML-escaped) data-clip-name attribute
    # instead and reading it back via this.dataset in JS.
    cid = _add_clip(seed.world_a.id, name='Tavern "Ambiance" & Stuff')
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert r.status_code == 200
    assert f'onclick="audioPopOut({cid}, this.dataset.clipName)"' in r.text
    assert 'data-clip-name="Tavern &#34;Ambiance&#34; &amp; Stuff"' in r.text
    # The malformed pre-fix form must not reappear.
    assert 'onclick="audioPopOut(' + str(cid) + ', "Tavern' not in r.text


def test_mini_player_hidden_by_default(client, seed):
    _add_clip(seed.world_a.id, name="Rain")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/audio")
    assert r.status_code == 200
    idx = r.text.index('id="audio-mini-player"')
    tag_end = r.text.index(">", idx)
    assert "display:none" in r.text[idx:tag_end]
