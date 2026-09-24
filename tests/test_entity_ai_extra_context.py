"""Tests for app.retrieval.entity_extra_context and its wiring into the
entity detail page's ENTITY_HEADER (entities/detail.html, embedded once at
page load and reused for every turn of the "Ask AI about {entity}"/
Roleplay panel — epFetchEntityContext/EP_SYSTEM_ASK/EP_SYSTEM_ROLEPLAY).

Before this, that panel only ever saw kind/name/subtype/summary plus a
per-question body excerpt — an entity's stat-block custom fields, its
discrete EntityNote rows, and its linked/backlinked relations were
invisible to it entirely, even though they're often the actual answer to
a question ("what's this NPC's AC?", "who do they report to?") the body
text never mentions.
"""
import json

import pytest

from app.database import SessionLocal
from app.models import Entity, EntityNote, EntityTemplate

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entity(world_id, **kwargs):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kwargs.pop("kind", "character"), name=kwargs.pop("name", "Entity"), **kwargs)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _make_template(**kwargs):
    db = SessionLocal()
    try:
        tpl = EntityTemplate(name=kwargs.pop("name", "Stat Block"), slug=kwargs.pop("slug", "stat-block-test"), **kwargs)
        db.add(tpl)
        db.commit()
        db.refresh(tpl)
        return tpl.id
    finally:
        db.close()


def _add_note(entity_id, content, visible_to_players=True, content_is_html=False):
    db = SessionLocal()
    try:
        n = EntityNote(entity_id=entity_id, content=content, visible_to_players=visible_to_players, content_is_html=content_is_html)
        db.add(n)
        db.commit()
    finally:
        db.close()


def _link(source_id, target_id):
    db = SessionLocal()
    try:
        src = db.get(Entity, source_id)
        tgt = db.get(Entity, target_id)
        src.related.append(tgt)
        db.commit()
    finally:
        db.close()


# ── app.retrieval.entity_extra_context (unit) ───────────────────────────────

from app import retrieval as retrieval_module


def _rel(name):
    return type("Rel", (), {"name": name})()


def test_empty_inputs_return_empty_string():
    assert retrieval_module.entity_extra_context([], {}, [], [], []) == ""


def test_formats_simple_custom_fields():
    sections = [("Stats", [{"id": "hp", "label": "HP", "type": "number"}, {"id": "ac", "label": "AC", "type": "number"}])]
    fields = {"hp": 42, "ac": 15}
    ctx = retrieval_module.entity_extra_context(sections, fields, [], [], [])
    assert "Custom fields:" in ctx
    assert "- HP: 42" in ctx
    assert "- AC: 15" in ctx


def test_blank_custom_field_values_are_omitted():
    sections = [("Stats", [{"id": "hp", "label": "HP", "type": "number"}])]
    ctx = retrieval_module.entity_extra_context(sections, {"hp": ""}, [], [], [])
    assert ctx == ""


def test_formats_list_type_custom_fields():
    sections = [("Abilities", [{
        "id": "abilities", "label": "Abilities", "type": "list",
        "item_fields": [{"id": "name", "label": "Name"}, {"id": "dmg", "label": "Damage"}],
    }])]
    fields = {"abilities": [{"name": "Claw", "dmg": "1d6"}, {"name": "Bite", "dmg": "2d4"}]}
    ctx = retrieval_module.entity_extra_context(sections, fields, [], [], [])
    assert "- Abilities: Name: Claw; Damage: 1d6" in ctx
    assert "- Abilities: Name: Bite; Damage: 2d4" in ctx


def test_formats_notes():
    class _Note:
        content = "The party owes them a favor."
        content_is_html = False
        visible_to_players = True
    ctx = retrieval_module.entity_extra_context([], {}, [_Note()], [], [])
    assert "Notes:\n- The party owes them a favor." in ctx


def test_html_note_converted_to_markdown():
    class _Note:
        content = "<p>They <strong>always</strong> lie.</p>"
        content_is_html = True
        visible_to_players = True
    ctx = retrieval_module.entity_extra_context([], {}, [_Note()], [], [])
    assert "always" in ctx
    assert "<strong>" not in ctx


def test_formats_related_and_backlink_entities_deduped_and_sorted():
    ctx = retrieval_module.entity_extra_context([], {}, [], [_rel("Zed"), _rel("Alice")], [_rel("Alice"), _rel("Bob")])
    assert "Related entities: Alice, Bob, Zed" in ctx


def test_strip_gm_only_drops_hidden_notes_entirely():
    class _Note:
        content = "Secret plan."
        content_is_html = False
        visible_to_players = False
    ctx = retrieval_module.entity_extra_context([], {}, [_Note()], [], [], strip_gm_only=True)
    assert ctx == ""


def test_strip_gm_only_strips_gmonly_spans_from_field_values_and_notes():
    sections = [("Stats", [{"id": "weak", "label": "Weakness", "type": "text"}])]
    fields = {"weak": "Fire [gmonly]— exploit this to end the fight fast[/gmonly]"}

    class _Note:
        content = "Loyal [gmonly]but secretly a spy[/gmonly]."
        content_is_html = False
        visible_to_players = True
    ctx = retrieval_module.entity_extra_context(sections, fields, [_Note()], [], [], strip_gm_only=True)
    assert "exploit this to end the fight fast" not in ctx
    assert "secretly a spy" not in ctx
    assert "Fire" in ctx
    assert "Loyal" in ctx


# ── Wired into the entity detail page's ENTITY_HEADER (route level) ────────

def test_gm_page_embeds_custom_fields_notes_and_relations(client, seed):
    tpl_id = _make_template(fields_json=json.dumps([{"id": "hp", "label": "HP", "type": "number", "section": "Stats"}]))
    npc_id = _make_entity(seed.world_a.id, name="Old Man Harrow", template_id=tpl_id, custom_fields_json=json.dumps({"hp": 12}))
    ally_id = _make_entity(seed.world_a.id, name="Bob the Fence", kind="character")
    _link(npc_id, ally_id)
    _add_note(npc_id, "Hides a knife in his boot.", visible_to_players=True)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get(f"/entity/{npc_id}").text
    assert "HP: 12" in page
    assert "Hides a knife in his boot." in page
    assert "Bob the Fence" in page


def test_player_page_connections_section_omits_hidden_related_entity(client, seed):
    """A separate bug found while fixing this: the "// CONNECTIONS" section
    rendered entity.related — the raw, unfiltered relationship — so a
    hidden (visible_to_players=False) related entity's name/kind/subtype
    leaked to any player viewing an entity that links to it, the one
    entity list on this page that skipped the visibility filter every
    other one here (backlinks, entity_notes) already applies."""
    npc_id = _make_entity(seed.world_a.id, name="Old Man Harrow", visible_to_players=True)
    hidden_id = _make_entity(seed.world_a.id, name="Secret Cult Leader", kind="character", visible_to_players=False)
    _link(npc_id, hidden_id)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get(f"/entity/{npc_id}").text
    assert "Secret Cult Leader" not in page
    assert "No connections yet." in page


def test_gm_page_connections_section_still_shows_related_entities(client, seed):
    npc_id = _make_entity(seed.world_a.id, name="Old Man Harrow")
    ally_id = _make_entity(seed.world_a.id, name="Bob the Fence", kind="character")
    _link(npc_id, ally_id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get(f"/entity/{npc_id}").text
    assert "Bob the Fence" in page


def test_player_page_omits_gm_only_note_and_hidden_related_entity(client, seed):
    tpl_id = _make_template(fields_json=json.dumps([{"id": "hp", "label": "HP", "type": "number", "section": "Stats"}]))
    npc_id = _make_entity(
        seed.world_a.id, name="Old Man Harrow", template_id=tpl_id,
        custom_fields_json=json.dumps({"hp": 12}), visible_to_players=True,
    )
    hidden_ally_id = _make_entity(seed.world_a.id, name="Secret Cult Leader", kind="character", visible_to_players=False)
    _link(npc_id, hidden_ally_id)
    _add_note(npc_id, "Secretly working for the cult.", visible_to_players=False)
    _add_note(npc_id, "Runs the general store.", visible_to_players=True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get(f"/entity/{npc_id}").text
    assert "Runs the general store." in page
    assert "Secretly working for the cult." not in page
    assert "Secret Cult Leader" not in page
