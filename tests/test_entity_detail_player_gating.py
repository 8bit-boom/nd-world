"""Tests for entity-detail UI gating (app/templates/entities/detail.html):
Edit/Delete were rendered for every viewer regardless of GM status (POSTing
Delete just got the raw 403 page), the Download link showed even when
World.players_can_download_entities was off, and the Ask AI panel rendered
(with every send failing 403) even when World.players_can_ask_ai was off.
The underlying routes were already correctly gated — this only fixes what
the page itself shows a player."""
from app.database import SessionLocal
from app.models import Entity, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _add_entity(world_id, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kw.pop("kind", "character"), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_player_does_not_see_edit_or_delete(client, seed):
    eid = _add_entity(seed.world_a.id, name="Some NPC", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert f'/entity/{eid}/edit' not in r.text
    assert f'/entity/{eid}/delete' not in r.text


def test_gm_still_sees_edit_and_delete(client, seed):
    eid = _add_entity(seed.world_a.id, name="Some NPC", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert f'/entity/{eid}/edit' in r.text
    assert f'/entity/{eid}/delete' in r.text


def test_player_does_not_see_download_link_when_toggle_off(client, seed):
    eid = _add_entity(seed.world_a.id, name="Some NPC", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert f'/entity/{eid}/download.md' not in r.text


def test_player_sees_download_link_once_gm_enables_it(client, seed):
    eid = _add_entity(seed.world_a.id, name="Some NPC", visible_to_players=True)
    _set_world(seed.world_a.id, players_can_download_entities=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert f'/entity/{eid}/download.md' in r.text


def test_player_does_not_see_ask_ai_panel_when_toggle_off(client, seed):
    eid = _add_entity(seed.world_a.id, name="Some NPC", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert 'id="ep-panel"' not in r.text


def test_player_sees_ask_ai_panel_once_gm_enables_it(client, seed):
    eid = _add_entity(seed.world_a.id, name="Some NPC", visible_to_players=True)
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert 'id="ep-panel"' in r.text


def test_gm_always_sees_ask_ai_panel(client, seed):
    eid = _add_entity(seed.world_a.id, name="Some NPC", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert 'id="ep-panel"' in r.text


def test_ask_ai_panel_has_thinking_checkbox_wired_into_send(client, seed):
    """The Ask AI panel's Thinking checkbox (default unchecked) must both
    render and be read by epSend() into the /api/ai/stream POST body —
    otherwise the checkbox would be decorative."""
    eid = _add_entity(seed.world_a.id, name="Some NPC", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert '<input type="checkbox" id="ep-think-checkbox">' in r.text  # unchecked by default
    assert "think: document.getElementById('ep-think-checkbox').checked" in r.text
