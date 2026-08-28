"""Sessions list cards used to carry no content beyond title/date/XP,
making it impossible to tell sessions apart without opening each one — see
app/templates/sessions/list.html. Now shows a recap excerpt and
has-transcript/has-recap badges."""
from app.database import SessionLocal
from app.models import GameSession

from .conftest import GM_PASSWORD, login


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
