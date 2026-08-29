"""Tests for the PC→NPC conversion feature (plan item, section 6's named
gap: "no 'retire PC → NPC entity' conversion"). POST /characters/{id}/
retire-to-npc converts a PlayerCharacter into a lore Entity (kind=
"character") and deletes the PlayerCharacter row — for a PC that died or
retired but whose write-up the GM wants to keep as an NPC. GM-only: entity
creation is a GM-only capability everywhere else in this app, and turning
a PC into GM-controlled lore content is a campaign-level call, not
something the owning player can do unilaterally (unlike Delete, which
both GM and the owning player can do)."""
from app.database import SessionLocal
from app.models import Entity, PlayerCharacter

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_pc(world_id, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(
            world_id=world_id, name=kw.pop("name", "Elowen Vex"),
            char_class=kw.pop("char_class", "Netrunner"), race=kw.pop("race", "Human"),
            level=kw.pop("level", 5), player_name=kw.pop("player_name", ""),
            backstory=kw.pop("backstory", ""), portrait_url=kw.pop("portrait_url", ""),
            **kw,
        )
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


def test_gm_can_retire_a_pc_to_an_npc_entity(client, seed):
    pc = _make_pc(
        seed.world_a.id, player_name="Alex", backstory="Grew up in the undercity.",
        portrait_url="/uploads/elowen.png",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(f"/characters/{pc.id}/retire-to-npc", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        assert db.query(PlayerCharacter).filter(PlayerCharacter.id == pc.id).first() is None
        entity = db.query(Entity).filter(Entity.world_id == seed.world_a.id, Entity.name == "Elowen Vex").first()
        assert entity is not None
        assert entity.kind == "character"
        assert entity.image_url == "/uploads/elowen.png"
        assert entity.visible_to_players is True
        assert "Netrunner" in entity.body
        assert "Human" in entity.body
        assert "Level 5" in entity.body
        assert "Grew up in the undercity." in entity.body
        assert "Alex" in (entity.summary or "")
    finally:
        db.close()

    assert r.headers["location"].startswith("/entity/")


def test_owning_player_cannot_retire_their_own_pc(client, seed):
    pc = _make_pc(seed.world_a.id, owner_user_id=seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(f"/characters/{pc.id}/retire-to-npc")
    assert r.status_code == 403

    db = SessionLocal()
    try:
        assert db.query(PlayerCharacter).filter(PlayerCharacter.id == pc.id).first() is not None
    finally:
        db.close()


def test_retire_nonexistent_pc_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/characters/999999/retire-to-npc")
    assert r.status_code == 404


def test_retire_button_shown_only_to_gm_on_sheet_page(client, seed):
    pc = _make_pc(seed.world_a.id, owner_user_id=seed.player_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r_gm = client.get(f"/characters/{pc.id}")
    assert r_gm.status_code == 200
    assert f'action="/characters/{pc.id}/retire-to-npc"' in r_gm.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r_player = client.get(f"/characters/{pc.id}")
    assert r_player.status_code == 200
    assert "retire-to-npc" not in r_player.text
