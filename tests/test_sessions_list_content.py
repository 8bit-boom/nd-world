"""Sessions list cards used to carry no content beyond title/date/XP,
making it impossible to tell sessions apart without opening each one — see
app/templates/sessions/list.html. Now shows a recap excerpt and
has-transcript/has-recap badges."""
import json

from app.database import SessionLocal
from app.models import GameSession, World
from app.deps import world_section_access

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_session(world_id, **kwargs):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world_id, title="Session 1", session_num=1, **kwargs)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def test_session_with_recap_shows_excerpt_and_recap_badge(client, seed):
    _make_session(seed.world_a.id, summary="**The party** raided the bazaar and found a hidden vault.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/sessions")
    assert "The party raided the bazaar" in r.text
    assert "📓 recap" in r.text
    assert "📝 transcript" not in r.text


def test_session_with_live_transcript_shows_transcript_badge(client, seed):
    _make_session(seed.world_a.id, live_transcript="raw whisper output here")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/sessions")
    assert "📝 transcript" in r.text


def test_session_with_neither_shows_no_badges(client, seed):
    _make_session(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/sessions")
    assert "📓 recap" not in r.text
    assert "📝 transcript" not in r.text


def test_player_download_strips_gm_only_blocks(client, seed):
    """B13: a player granted sessions-read can download summary/transcript,
    but the download must strip [gmonly] blocks exactly like the rendered
    page — the file is just another way the same text reaches them."""
    from app.models import World
    from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        current = world_section_access(w)
        current["sessions"]["player"] = "read"
        w.section_access_json = json.dumps(current)
        gs = GameSession(
            world_id=seed.world_a.id, title="Secrets", session_num=9,
            summary="Public recap. [gmonly]hidden-summary-QORAX[/gmonly]",
            live_transcript="Public line. [gmonly]hidden-transcript-VEXIL[/gmonly]",
        )
        db.add(gs)
        db.commit()
        db.refresh(gs)
        sid = gs.id
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{sid}/summary.md")
    assert r.status_code == 200
    assert "QORAX" not in r.text and "Public recap." in r.text
    r = client.get(f"/sessions/{sid}/transcript.md")
    assert r.status_code == 200
    assert "VEXIL" not in r.text and "Public line." in r.text
    # GM downloads get the full text
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/sessions/{sid}/summary.md")
    assert "QORAX" in r.text
