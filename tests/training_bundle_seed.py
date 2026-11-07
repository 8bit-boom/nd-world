"""A world with something in every table the Training Bundle reads; shared by the Python test and the gateway parity tests."""
import json

from app.database import SessionLocal
from app.models import (
    AiInstruction, Entity, EntityNote, EntityRelation, EntityTemplate, Fact, GameSession, PlayerCharacter, Quest,
    RandomTable, SheetTemplate, World, entity_links,
)


def seed_rich_world(world_id: int, rules_md: str = "# House rules\n\nRoll d6 pools. :::gm\nsecret\n:::\n") -> dict:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        w.rules_md = rules_md
        tpl = EntityTemplate(world_id=world_id, name="Hunter", slug=f"hunter-{world_id}", kind="character",
                             fields_json=json.dumps([{"id": "rank", "label": "Rank", "type": "text"}]))
        db.add(tpl)
        db.flush()
        anders = Entity(world_id=world_id, kind="character", subtype="NPC", name="Anders de Vallière", aliases=" Anders, the Hunter ,",
                        folder="Heroes", tags="hunter, fire", summary="A Widow-Bound Hunter.",
                        body="# Anders\nHunts beasts. [gmonly]Fears fire.[/gmonly] Also “quotes” and emoji 🔥.",
                        template_id=tpl.id, custom_fields_json=json.dumps({"rank": "Captain", "level": 3, "tags": ["a", "b"]}),
                        roleplay_personality="gruff, terse", visible_to_players=False, rag_priority=True,
                        group_ids_json="[1, 2]")
        dock = Entity(world_id=world_id, kind="location", subtype="", name="Dockside", body="Smugglers meet here.",
                      custom_fields_json="not json", group_ids_json="{}")
        other = Entity(world_id=world_id, kind="note", name="Rumours", body=None, summary="Only a summary", tags=None)
        db.add_all([anders, dock, other])
        db.flush()
        db.add(EntityNote(entity_id=anders.id, content="Secret note ✓", visible_to_players=False))
        db.add(EntityRelation(world_id=world_id, source_id=anders.id, target_id=dock.id, relation="located_in"))
        db.add(EntityRelation(world_id=world_id, source_id=dock.id, target_id=anders.id, relation=None))
        db.execute(entity_links.insert().values(source_id=other.id, target_id=anders.id))
        gs = GameSession(world_id=world_id, title="Arrival", session_num=1, session_date="2026-10-01", summary="The party arrived.",
                         npcs_json=json.dumps([{"entity_id": anders.id, "name": "Anders"}]), loot_json="oops")
        db.add(gs)
        db.flush()
        db.add(Fact(world_id=world_id, game_session_id=gs.id, content="The duke leads the cult.", visible_to_players=False, tags="duke"))
        db.add(Quest(world_id=world_id, title="Find the duke", status="active", category="main", summary="s", body="b",
                     linked_entities_json=json.dumps([{"entity_id": anders.id, "role": "ally"}])))
        db.add(RandomTable(world_id=world_id, name="Rumours", slug=f"rumours-{world_id}", category="general",
                           entries_json=json.dumps([{"label": "A ship vanished", "weight": 2}])))
        db.add(AiInstruction(world_id=world_id, title="Voice", content="Answer like a noir narrator.", enabled=True))
        db.add(AiInstruction(world_id=world_id, title="Off", content="disabled", enabled=False))
        db.add(PlayerCharacter(world_id=world_id, name="Mira", race="Elf", char_class="Ranger", level=4, backstory="Orphan.",
                               notes="Likes bows", equipment_json=json.dumps(["bow"]), feats_json="[]"))
        db.add(SheetTemplate(world_id=world_id, name="Hunt", slug=f"hunt-{world_id}", rules_md="Sheet rules here"))
        db.add(SheetTemplate(world_id=world_id, name="Empty", slug=f"empty-{world_id}", rules_md="  "))
        db.commit()
        return {"anders": anders.id, "dock": dock.id}
    finally:
        db.close()
