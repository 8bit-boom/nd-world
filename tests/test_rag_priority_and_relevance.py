"""Tests for two related RAG fixes, both driven by the same reported bug:
asking AI Chat (and the entity detail page's "Ask AI about {entity}" panel)
about weapon traits found nothing, even though a "Player Guide" note in the
world had a whole table for it — the note IS retrieved (it mentions
"weapon" plenty), but every excerpt taken from it was a blind prefix slice
(format_context_from_entities' body[:excerpt_chars], or the entity panel's
old fixed body[:3000]) that only ever reached the document's own front
matter, never the actually-relevant section buried deep inside a large,
heading-structured document.

1. best_matching_excerpt (app/retrieval.py) — splits a large body on
   markdown headings and returns the section that actually scores against
   the query's keywords, instead of a blind prefix. Wired into
   format_context_from_entities via its new `query` param, and into the
   entity detail page's "Ask AI" panel via a new per-question endpoint
   (POST /api/ai/entity-context) replacing that panel's old fixed-prefix
   context.

2. Entity.rag_priority (a new "⭐ High priority for AI" checkbox on the
   entity edit form) + app.retrieval.priority_entities_context — lets a GM
   flag a big reference-document entity (exactly the "Player Guide" case)
   so it's always searched independently of the entity_limit/notes_limit
   sliders, the same "always eligible, only actually included when
   relevant" treatment World Rules text already gets from rules_context —
   for the case where ordinary top-N keyword retrieval might not even
   include the entity among its results at all.
"""
from app.database import SessionLocal
from app.models import Entity, User, World
from app.retrieval import (
    best_matching_excerpt,
    format_context_from_entities,
    priority_entities_context,
    smart_world_context,
)

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_WEAPON_TRAITS_SECTION = (
    "## Weapon Traits (choose at most one beneficial)\n\n"
    "Brutal: The GM may describe an extra visceral or intimidating consequence on a hit; no mechanical bonus.\n"
    "Reach: Can strike a target at Close range instead of Melee only.\n"
)

_PLAYER_GUIDE_BODY = (
    "# Players Guide\n\n"
    "## Revision History\n\n" + ("Consolidated from many source documents. " * 80) + "\n\n"
    "## General Introduction\n\n" + ("This is a gothic science-fantasy TTRPG about hunters. " * 80) + "\n\n"
    + _WEAPON_TRAITS_SECTION
    + "\n\n## Body Modifications\n\n" + ("Unrelated filler about body modifications. " * 80)
)


def _make_entity(world_id, **kwargs):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kwargs.pop("kind", "note"), name=kwargs.pop("name", "Entity"), **kwargs)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


# ── best_matching_excerpt ───────────────────────────────────────────────────

def test_short_body_returned_as_is():
    assert best_matching_excerpt("Short body.", "anything", 1000) == "Short body."


def test_no_headings_falls_back_to_prefix_slice():
    body = "X" * 5000
    assert best_matching_excerpt(body, "weapon traits", 100) == "X" * 100


def test_no_query_words_falls_back_to_prefix_slice():
    assert best_matching_excerpt(_PLAYER_GUIDE_BODY, "hi", 50) == _PLAYER_GUIDE_BODY.strip()[:50]


def test_picks_the_actually_relevant_section_not_the_front_matter():
    excerpt = best_matching_excerpt(_PLAYER_GUIDE_BODY, "what weapon traits exist", 500)
    assert "Brutal" in excerpt
    assert "no mechanical bonus" in excerpt
    assert "Consolidated from many source documents" not in excerpt


def test_no_section_scores_falls_back_to_prefix_slice():
    excerpt = best_matching_excerpt(_PLAYER_GUIDE_BODY, "zzzznonexistentwordzzzz", 50)
    assert excerpt == _PLAYER_GUIDE_BODY.strip()[:50]


# A "tell me about X" / "is there a Y for X" style question, phrased the way
# a player actually types it — plus a decoy section that repeats "weapon"
# several times in unrelated prose, but never says "trait(s)" at all. The
# real reported bug: a blunt len(word) > 3 filter let noise words like
# "tell"/"about" survive into keyword-count scoring (diluting/skewing it),
# AND plain substring-count scoring let the decoy's repeated "weapon"s
# outscore the one section actually titled "Weapon Traits" even though that
# heading match is the far stronger signal.
_MODES_DECOY_SECTION = (
    "## The Three Modes\n\n"
    "Every Tool of the Hunt (which includes Hunter Weapons) is defined by how a "
    "weapon functions across three weapon modes: Hunt Mode, Pursuit Mode, and "
    "Execution Mode. Building a weapon means picking a mode for each weapon.\n\n"
)

_GUIDE_WITH_MODES_DECOY = (
    "# Players Guide\n\n" + _MODES_DECOY_SECTION + _WEAPON_TRAITS_SECTION
)


def test_picks_titled_section_over_a_decoy_that_merely_repeats_the_word():
    excerpt = best_matching_excerpt(_GUIDE_WITH_MODES_DECOY, "tell me about weapon traits", 200)
    assert excerpt.startswith("[Weapon Traits")
    assert "Brutal" in excerpt
    assert "The Three Modes" not in excerpt


def test_picks_titled_section_for_is_there_a_trait_phrasing():
    """Matches the exact reported phrasing: "is there a 'Brutal' trait for
    weapons?" — singular "trait" in the query must still line up with the
    plural "Weapon Traits" heading."""
    excerpt = best_matching_excerpt(_GUIDE_WITH_MODES_DECOY, "is there a Brutal trait for weapons?", 200)
    assert excerpt.startswith("[Weapon Traits")
    assert "Brutal" in excerpt
    assert "The Three Modes" not in excerpt


# ── best_matching_excerpt: multiple sibling sections, not just the #1 pick ──

_MELEE_WEAPONS_SECTION = (
    "## Melee Weapons\n\n"
    "| Weapon | Trait | Cost |\n|---|---|---|\n"
    "| Kitchen knife | Concealable | 2 Crows |\n"
    "| Hand axe | Brutal | 5 Crows |\n"
)
_THROWN_WEAPONS_SECTION = (
    "## Thrown Weapons\n\n"
    "| Weapon | Trait | Cost |\n|---|---|---|\n"
    "| Throwing knife | Concealable | 3 Crows |\n"
)
_ORDINARY_WEAPONS_GUIDE = (
    "# Players Guide\n\n"
    "## 23. Ordinary Weapons\n\n"
    "Ordinary weapons are mundane arms anyone might carry: blades, firearms, clubs.\n\n"
    + _MELEE_WEAPONS_SECTION + _THROWN_WEAPONS_SECTION
    # Long enough that the whole document exceeds chars_budget below —
    # otherwise best_matching_excerpt's own "already fits uncut" fast path
    # returns the raw document verbatim and never exercises section
    # scoring/selection at all.
    + "\n\n## Unrelated Chapter\n\n" + ("Nothing to do with any of this. " * 200)
)


def test_best_matching_excerpt_returns_multiple_comparable_sections():
    """A "list all X" question can span several SIBLING headings (separate
    Melee/Thrown Weapons tables here) rather than one combined list —
    returning only the single top-scoring section gave a partial answer
    even once ranking correctly favored the right neighborhood of the
    document. Both real weapon tables should appear; the unrelated filler
    chapter must not, regardless of leftover budget."""
    excerpt = best_matching_excerpt(_ORDINARY_WEAPONS_GUIDE, "list of ordinary weapons", 2000)
    assert "Kitchen knife" in excerpt
    assert "Throwing knife" in excerpt
    assert "Unrelated Chapter" not in excerpt
    assert "Nothing to do with any of this" not in excerpt


def test_best_matching_excerpt_does_not_pad_with_a_barely_relevant_section():
    """A section whose only overlap with the query is one common word
    appearing once must not get swept in as a "second section" just
    because leftover budget happens to exist — it has to be a genuinely
    competitive match relative to the #1 pick (_SECTION_RELEVANCE_RATIO),
    not merely technically nonzero."""
    guide = (
        "# Guide\n\n"
        "## Weapon Traits\n\nBrutal: no mechanical bonus.\n\n"
        "## Unrelated Lore\n\n"
        + ("A merchant once sold a fine weapon here, long ago. " * 40)
    )
    excerpt = best_matching_excerpt(guide, "tell me about weapon traits", 2000)
    assert "Brutal" in excerpt
    assert "Unrelated Lore" not in excerpt
    assert "merchant" not in excerpt


# ── format_context_from_entities(query=...) ─────────────────────────────────

def test_format_context_uses_relevant_excerpt_when_query_given(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY)
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], query="what weapon traits exist")
        assert "Brutal" in context
        assert "no mechanical bonus" in context
    finally:
        db.close()


def test_format_context_without_query_keeps_old_prefix_behavior(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY)
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e])
        assert "Brutal" not in context
        assert "Consolidated from many source documents" in context
    finally:
        db.close()


# ── Entity.rag_priority + priority_entities_context ─────────────────────────

def test_priority_entities_context_empty_when_none_flagged(client, seed):
    _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY)
    db = SessionLocal()
    try:
        assert priority_entities_context(db, seed.world_a.id, "weapon traits") == ""
    finally:
        db.close()


def test_priority_entities_context_surfaces_flagged_entity(client, seed):
    _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY, rag_priority=True)
    db = SessionLocal()
    try:
        context = priority_entities_context(db, seed.world_a.id, "what weapon traits exist")
        assert "Players Guide" in context
        assert "Brutal" in context
    finally:
        db.close()


def test_priority_entities_context_empty_when_nothing_scores(client, seed):
    _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY, rag_priority=True)
    db = SessionLocal()
    try:
        assert priority_entities_context(db, seed.world_a.id, "zzzznonexistentzzzz") == ""
    finally:
        db.close()


def test_priority_entities_context_excludes_ids_already_included(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY, rag_priority=True)
    db = SessionLocal()
    try:
        context = priority_entities_context(db, seed.world_a.id, "weapon traits", exclude_ids={eid})
        assert context == ""
    finally:
        db.close()


def test_priority_entities_context_respects_visibility_for_a_player(client, seed):
    _make_entity(
        seed.world_a.id, name="Secret Players Guide", body=_PLAYER_GUIDE_BODY,
        rag_priority=True, visible_to_players=False,
    )
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        context = priority_entities_context(db, seed.world_a.id, "weapon traits", user=player)
        assert context == ""
        gm = db.get(User, seed.gm.id)
        context_gm = priority_entities_context(db, seed.world_a.id, "weapon traits", user=gm)
        assert "Brutal" in context_gm
    finally:
        db.close()


def test_priority_entities_context_strips_gm_only_when_asked(client, seed):
    body = _PLAYER_GUIDE_BODY + "\n\n[gmonly]The real trigger is the third lever.[/gmonly]"
    _make_entity(seed.world_a.id, name="Players Guide", body=body, rag_priority=True, visible_to_players=True)
    db = SessionLocal()
    try:
        context = priority_entities_context(db, seed.world_a.id, "weapon traits", strip_gm_only=True)
        assert "third lever" not in context
    finally:
        db.close()


# ── smart_world_context end-to-end: the reported bug, fixed ────────────────

def test_smart_world_context_finds_weapon_traits_via_priority_even_with_entity_rag_off(client, seed):
    """Demonstrates the "doesn't count toward entity_limit" half concretely:
    entity_limit=0 means the ordinary retrieval path includes ZERO
    entities of its own (ordinary keyword ranking never even gets a
    chance to crowd this one out or rank it low), yet the rag_priority
    flag still surfaces its actually-relevant section."""
    _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY, rag_priority=True)
    db = SessionLocal()
    try:
        context, non_notes, notes = smart_world_context(
            db, seed.world_a.id, "what weapon traits exist, like Brutal", entity_limit=0, notes_limit=0,
        )
        assert "Brutal" in context
        assert "no mechanical bonus" in context
        # Priority content never pollutes the entity-only transparency lists.
        assert non_notes == []
        assert notes == []
    finally:
        db.close()


def test_smart_world_context_without_priority_flag_misses_it_when_entity_rag_off(client, seed):
    """Baseline confirming the flag is what does the work: the identical
    setup, minus rag_priority, finds nothing with entity RAG off — guards
    against this test suite accidentally passing for an unrelated reason
    if the priority wiring regresses."""
    _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY)
    db = SessionLocal()
    try:
        context, _non_notes, _notes = smart_world_context(
            db, seed.world_a.id, "what weapon traits exist, like Brutal", entity_limit=0, notes_limit=0,
        )
        assert "no mechanical bonus" not in context
    finally:
        db.close()


# ── Entity edit form: the checkbox ──────────────────────────────────────────

def test_entity_form_ships_rag_priority_checkbox(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body="x")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}/edit")
    assert r.status_code == 200
    assert 'name="rag_priority"' in r.text
    assert "High priority for AI" in r.text


def test_entity_create_persists_rag_priority(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/new", data={
        "kind": "note", "name": "Players Guide", "rag_priority": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        e = db.query(Entity).filter(Entity.name == "Players Guide").first()
        assert e.rag_priority is True
    finally:
        db.close()


def test_entity_edit_updates_rag_priority(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body="x", rag_priority=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/entity/{eid}/edit", data={
        "kind": "note", "name": "Players Guide", "rag_priority": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        assert e.rag_priority is True
    finally:
        db.close()
    # Omitting the checkbox (unchecked in a real form submit) clears it.
    r2 = client.post(f"/entity/{eid}/edit", data={
        "kind": "note", "name": "Players Guide",
    }, follow_redirects=False)
    assert r2.status_code == 303
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        assert e.rag_priority is False
    finally:
        db.close()


# ── POST /api/ai/entity-context ─────────────────────────────────────────────

def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def test_entity_context_route_returns_relevant_excerpt(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/entity-context", json={"entity_id": eid, "query": "what weapon traits exist"})
    assert r.status_code == 200
    assert "Brutal" in r.json()["context"]


def test_entity_context_route_blank_query_returns_empty(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/entity-context", json={"entity_id": eid, "query": ""})
    assert r.status_code == 200
    assert r.json() == {"context": ""}


def test_entity_context_route_strips_gm_only_for_player(client, seed):
    body = _PLAYER_GUIDE_BODY + "\n\n[gmonly]He is secretly a cult spy.[/gmonly]"
    eid = _make_entity(seed.world_a.id, name="Players Guide", body=body, visible_to_players=True)
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/entity-context", json={"entity_id": eid, "query": "cult spy secret"})
    assert r.status_code == 200
    assert "cult spy" not in r.json()["context"]


def test_entity_context_route_404_for_hidden_entity_as_player(client, seed):
    eid = _make_entity(seed.world_a.id, name="Secret Guide", body=_PLAYER_GUIDE_BODY, visible_to_players=False)
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/entity-context", json={"entity_id": eid, "query": "weapon traits"})
    assert r.status_code == 404


def test_entity_context_route_gm_only_when_players_cant_ask_ai(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body=_PLAYER_GUIDE_BODY, visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/entity-context", json={"entity_id": eid, "query": "weapon traits"})
    assert r.status_code == 403


def test_entity_context_route_unknown_entity_404(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/entity-context", json={"entity_id": 999999, "query": "weapon traits"})
    assert r.status_code == 404


# ── Entity detail page: Ask AI think-checkbox default + wiring ─────────────

def test_entity_detail_ask_ai_think_checkbox_checked_by_default(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body="x")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert 'id="ep-think-checkbox" checked' in r.text


def test_entity_detail_page_wires_per_question_context_fetch(client, seed):
    eid = _make_entity(seed.world_a.id, name="Players Guide", body="x")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get(f"/entity/{eid}").text
    assert "async function epFetchEntityContext(query)" in page
    assert "/api/ai/entity-context" in page
    assert "const bodyExcerpt = await epFetchEntityContext(text);" in page
