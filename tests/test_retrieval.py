"""Unit tests for app/retrieval.py — the shared entity-retrieval module
extracted from app.main (plan item AI 1.2), covering the two things that
didn't already have direct test coverage at this layer: the `user`
visibility filter (new — previously only chronicler.py's now-removed
duplicate had this, and only via ILIKE) and the AI 1.1 body-excerpt
behavior in format_context_from_entities. Route-level FTS/ranking/sync
coverage lives in tests/test_rag_fts5.py; this file is about retrieval.py
itself.

Also covers plan item AI 1.11: the "guaranteed recent notes" top-up in
both RAG consumers (app.main.ai_world_context_smart and app.audio_jobs.
_build_rag_context) ordered by Entity.name instead of Entity.updated_at.
desc() — "recent" meaning most-recently-edited, not alphabetically-first.
"""
from datetime import datetime, timedelta

from app import audio_jobs
from app.database import SessionLocal
from app.models import Entity, User, World, entity_player_access
from app.retrieval import (
    _query_words,
    _rules_sections,
    find_relevant_entities,
    format_context_from_entities,
    rules_context,
    smart_world_context,
    world_rules_markdown,
)

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


def _share_with(entity_id, user_id):
    db = SessionLocal()
    try:
        db.execute(entity_player_access.insert().values(entity_id=entity_id, user_id=user_id))
        db.commit()
    finally:
        db.close()


# ── _query_words tokenizer ──────────────────────────────────────────────────

def test_query_words_keeps_short_rpg_terms():
    """The old len(word) > 3 filter silently dropped every one of these
    real, meaningful short RPG terms — a player asking about any of them
    got that word thrown away before search/scoring ever saw it."""
    for term in ("axe", "orc", "elf", "bow", "gun", "imp", "hex", "war", "ac", "hp"):
        assert term in _query_words(f"tell me about the {term}"), term


def test_query_words_drops_stopwords_regardless_of_length():
    words = _query_words("tell me about weapon traits")
    assert words == ["weapon", "traits"]
    assert "tell" not in words and "about" not in words


def test_query_words_drops_common_question_words():
    words = _query_words("what does the brutal trait do")
    assert "what" not in words
    assert "does" not in words
    assert "brutal" in words and "trait" in words


# ── user= visibility filter ─────────────────────────────────────────────────

def test_user_none_is_unfiltered(client, seed):
    """No `user` given (background jobs, already-GM-gated routes) — every
    existing call site's prior behavior, preserved exactly."""
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    db = SessionLocal()
    try:
        results = find_relevant_entities(db, seed.world_a.id, "Secret Vault", limit=10, user=None)
        assert hidden_id in {e.id for e in results}
    finally:
        db.close()


def test_gm_user_sees_hidden_entities(client, seed):
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    db = SessionLocal()
    try:
        gm = db.get(User, seed.gm.id)
        results = find_relevant_entities(db, seed.world_a.id, "Secret Vault", limit=10, user=gm)
        assert hidden_id in {e.id for e in results}
    finally:
        db.close()


def test_player_user_excludes_hidden_entities(client, seed):
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    visible_id = _make_entity(seed.world_a.id, name="Public Vault", visible_to_players=True)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        results = find_relevant_entities(db, seed.world_a.id, "Vault", limit=10, user=player)
        ids = {e.id for e in results}
        assert hidden_id not in ids
        assert visible_id in ids
    finally:
        db.close()


def test_player_user_sees_hidden_entity_specifically_shared_with_them(client, seed):
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    _share_with(hidden_id, seed.player_a.id)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        results = find_relevant_entities(db, seed.world_a.id, "Secret Vault", limit=10, user=player)
        assert hidden_id in {e.id for e in results}
    finally:
        db.close()


def test_player_visibility_filter_also_applies_to_the_no_query_words_fallback(client, seed):
    """A query with no words >3 chars (e.g. "hi") skips keyword search
    entirely and falls back to "every entity in the world" — that fallback
    must still respect visibility, not bypass it."""
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        results = find_relevant_entities(db, seed.world_a.id, "hi", limit=50, user=player)
        assert hidden_id not in {e.id for e in results}
    finally:
        db.close()


# ── AI 1.1 — format_context_from_entities body excerpts ────────────────────

def test_body_excerpt_appended_for_first_entities(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Old Man Harrow", summary="A hermit.",
        body="He speaks constantly of a hidden vault called the Undermarket, sealed beneath the old cistern.",
    )
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e])
        assert "- [character] Old Man Harrow: A hermit." in context
        assert "Undermarket" in context
    finally:
        db.close()


def test_excerpt_count_zero_is_summary_only_like_before(client, seed):
    eid = _make_entity(seed.world_a.id, name="Old Man Harrow", body="Contains the word Undermarket.")
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], excerpt_count=0)
        assert "Undermarket" not in context
        assert "- [character] Old Man Harrow" in context
    finally:
        db.close()


def test_only_first_excerpt_count_entities_get_a_body_excerpt(client, seed):
    ids = [
        _make_entity(seed.world_a.id, name=f"Entity {i}", body=f"UniqueBodyMarker{i}")
        for i in range(3)
    ]
    db = SessionLocal()
    try:
        entities = [db.get(Entity, i) for i in ids]
        context = format_context_from_entities(entities, excerpt_count=1)
        assert "UniqueBodyMarker0" in context
        assert "UniqueBodyMarker1" not in context
        assert "UniqueBodyMarker2" not in context
    finally:
        db.close()


def test_excerpt_truncated_to_per_entity_char_cap(client, seed):
    eid = _make_entity(seed.world_a.id, name="Long Entity", body="X" * 5000)
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], excerpt_count=1, excerpt_chars=100, excerpt_total_budget=8000)
        assert context.count("X") == 100
    finally:
        db.close()


def test_excerpt_total_budget_caps_across_multiple_entities(client, seed):
    ids = [_make_entity(seed.world_a.id, name=f"Entity {i}", body="Y" * 100) for i in range(3)]
    db = SessionLocal()
    try:
        entities = [db.get(Entity, i) for i in ids]
        context = format_context_from_entities(
            entities, excerpt_count=3, excerpt_chars=100, excerpt_total_budget=150,
        )
        assert context.count("Y") == 150
    finally:
        db.close()


def test_entity_with_no_body_gets_no_excerpt_line(client, seed):
    eid = _make_entity(seed.world_a.id, name="Bodyless Entity", summary="Just a summary.")
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e])
        assert context == "- [character] Bodyless Entity: Just a summary."
    finally:
        db.close()


# ── End-to-end: excerpts actually reach the RAG-consuming routes ───────────

def test_world_context_smart_includes_body_excerpt(client, seed):
    _make_entity(
        seed.world_a.id, name="Old Man Harrow", summary="A hermit.",
        body="He speaks constantly of a hidden vault called the Undermarket.",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={"query": "Harrow", "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    assert "Undermarket" in r.json()["context"]


def test_world_context_smart_tops_up_entities_for_a_foreign_language_query(client, seed):
    """Mirrors app.audio_jobs._build_rag_context's own non-English top-up
    (see test_build_rag_context_tops_up_entities_for_a_foreign_language_
    query in test_audio_jobs.py) — the same gap existed one surface over on
    AI Chat's RAG (docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2, item
    2.3): a Russian-language chat question against English-named entities
    had no literal keyword overlap for find_relevant_entities to match, so
    it silently came back with no characters/places at all."""
    _make_entity(
        seed.world_a.id, name="Gareth Ashfall", kind="character", summary="A blacksmith.",
        body="Gareth Ashfall runs the forge near the eastern gate.",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    russian_query = "Партия встретила Гарета возле восточных ворот"
    r = client.post("/api/ai/world-context-smart", json={"query": russian_query, "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    data = r.json()
    assert "Gareth Ashfall" in data["context"]
    assert any(e["name"] == "Gareth Ashfall" for e in data["entities"])


def test_world_context_smart_no_topup_leak_when_search_already_fills_the_limit(client, seed):
    _make_entity(seed.world_a.id, name="Gareth Ashfall", kind="character", body="A blacksmith.")
    _make_entity(seed.world_a.id, name="Completely Unrelated Entity", kind="location", body="Nothing to do with Gareth.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={
        "query": "Gareth Ashfall the blacksmith", "limit": 1, "notes_limit": 0,
    })
    assert r.status_code == 200
    data = r.json()
    assert "Gareth Ashfall" in data["context"]
    assert "Completely Unrelated Entity" not in data["context"]


# ── AI 1.11 — guaranteed-recent-notes top-up ordered by recency ────────────

def _make_note_with_updated_at(world_id, name, updated_at):
    db = SessionLocal()
    try:
        n = Entity(world_id=world_id, kind="note", name=name, body=f"Body of {name}.")
        db.add(n)
        db.commit()
        db.refresh(n)
        n.updated_at = updated_at
        db.commit()
        return n.id
    finally:
        db.close()


def test_world_context_smart_notes_topup_prefers_most_recently_updated(client, seed):
    """Alphabetically "Ancient Note" would win; recency-wise "Zebra Note"
    (updated far more recently) should be the one guaranteed by notes_limit
    when the keyword search itself doesn't match either."""
    now = datetime.utcnow()
    _make_note_with_updated_at(seed.world_a.id, "Ancient Note", now - timedelta(days=30))
    recent_id = _make_note_with_updated_at(seed.world_a.id, "Zebra Note", now)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={
        "query": "unrelated-query-xyz", "limit": 10, "notes_limit": 1,
    })
    assert r.status_code == 200
    note_ids = [e["id"] for e in r.json()["entities"] if e["kind"] == "note"]
    assert note_ids == [recent_id]


def test_build_rag_context_notes_topup_prefers_most_recently_updated(client, seed):
    now = datetime.utcnow()
    _make_note_with_updated_at(seed.world_a.id, "Ancient Note", now - timedelta(days=30))
    _make_note_with_updated_at(seed.world_a.id, "Zebra Note", now)

    context = audio_jobs._build_rag_context(
        seed.world_a.id, "unrelated-query-xyz", entity_limit=0, notes_limit=1,
    )
    assert "Zebra Note" in context
    assert "Ancient Note" not in context


# ── Rules RAG: world_rules_markdown / _rules_sections / rules_context ───────

def _set_world_rules(world_id, rules_md):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        w.rules_md = rules_md
        db.commit()
    finally:
        db.close()


def test_world_rules_markdown_uses_world_own_rules(client, seed):
    _set_world_rules(seed.world_a.id, "# My Homebrew\n\nCustom content here.")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert world_rules_markdown(w) == "# My Homebrew\n\nCustom content here."
    finally:
        db.close()


def test_world_rules_markdown_falls_back_to_bundled_core_rules(client, seed):
    """Blank/unset rules_md (the default) falls back to the bundled
    core_rules.md, same as app.main._world_rules_markdown always has."""
    _set_world_rules(seed.world_a.id, "")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        md = world_rules_markdown(w)
        assert md  # the bundled file is non-empty
        assert "My Homebrew" not in md
    finally:
        db.close()


def test_rules_sections_splits_on_headings():
    md = "# Title\n\nIntro text.\n\n## Armor\n\nChainmail costs 120 crowns.\n\n## Weapons\n\nA sword costs 15 crowns."
    sections = _rules_sections(md)
    headings = [h for h, _body in sections]
    assert "Title" in headings
    assert "Armor" in headings
    assert "Weapons" in headings
    armor_body = dict(sections)["Armor"]
    assert "120 crowns" in armor_body


def test_rules_sections_keeps_text_before_first_heading_as_introduction():
    md = "Some preamble with no heading yet.\n\n# Real Heading\n\nBody."
    sections = _rules_sections(md)
    assert sections[0] == ("Introduction", "Some preamble with no heading yet.")


def test_rules_sections_no_headings_returns_one_section():
    sections = _rules_sections("Just plain text, no markdown headings at all.")
    assert sections == [("Rules", "Just plain text, no markdown headings at all.")]


def test_rules_sections_empty_input():
    assert _rules_sections("") == []


def test_rules_sections_strips_windows_line_endings_from_headings():
    """A GM's rules_md pasted from Windows (or edited on Windows) can carry
    \\r\\n line endings — the heading regex's [ \\t]*$ doesn't consume \\r
    before MULTILINE's $ anchor, so an un-normalized heading captured a
    literal trailing \\r baked into the section's own name, silently
    breaking any exact heading comparison downstream."""
    md = "## Weapon Traits\r\n\r\nBrutal: no mechanical bonus.\r\n"
    sections = _rules_sections(md)
    assert sections == [("Weapon Traits", "Brutal: no mechanical bonus.")]


def test_rules_sections_recognizes_headings_deeper_than_h3():
    """A real reported bug: a GM's actual rules document nested its real
    content under H4/H5 headings inside a broader H3 "chapter" (e.g. H3
    "Traits of the Hunt" containing H4 "Weapon Traits" containing H5
    "Weapon — Hunt Mode" with the actual named trait table) — the old
    H1-H3-only regex made every deeper heading invisible, collapsing the
    whole chapter into one undifferentiated blob."""
    md = "### Chapter\n\nIntro.\n\n#### Weapon Traits\n\nBrutal: no bonus.\n\n##### Sub Detail\n\nExtra nuance."
    sections = _rules_sections(md)
    headings = [h for h, _ in sections]
    assert "Weapon Traits" in headings
    assert "Sub Detail" in headings


def test_rules_sections_nests_deeper_headings_into_their_parent():
    """A heading's own section extends until the next heading at the SAME
    OR SHALLOWER level — a deeper nested heading stays part of the
    parent's body instead of ending it, so a parent like "Weapon Traits"
    naturally includes all of its own H5 sub-tables rather than just its
    opening sentence."""
    md = (
        "#### Weapon Traits\n\nIntro sentence.\n\n"
        "##### Weapon — Hunt Mode\n\nHoned Edge: +1d10.\n\n"
        "##### Weapon — Pursuit Mode\n\nReaching Form: +1 range.\n\n"
        "#### Attire Traits\n\nUnrelated attire content."
    )
    sections = _rules_sections(md)
    sd = dict(sections)
    assert "Honed Edge" in sd["Weapon Traits"]
    assert "Reaching Form" in sd["Weapon Traits"]
    assert "Unrelated attire content" not in sd["Weapon Traits"]
    # The nested headings still get their own, individually-scoped entries too.
    assert "Honed Edge" in sd["Weapon — Hunt Mode"]
    assert "Reaching Form" not in sd["Weapon — Hunt Mode"]


def test_rules_sections_falls_back_to_own_text_when_merge_would_be_too_large():
    """A parent heading whose nested children would merge into a body
    bigger than _MAX_SCORED_CHARS falls back to just its own direct text
    (usually a short intro) instead of an enormous merged blob — otherwise
    a heading near the top of a large document (the file's own title, a
    "Part" divider) would swallow huge unrelated stretches of the
    document, and a query happening to share any word with that ocean of
    text would always win purely by sheer size."""
    from app.retrieval import _MAX_SCORED_CHARS
    big_child_body = "Filler word. " * (_MAX_SCORED_CHARS // len("Filler word. ") + 10)
    md = f"# Parent\n\nShort intro.\n\n## Child\n\n{big_child_body}"
    sections = _rules_sections(md)
    sd = dict(sections)
    assert sd["Parent"] == "Short intro."
    assert "Filler word" in sd["Child"]


def test_rules_context_finds_matching_section(client, seed):
    _set_world_rules(seed.world_a.id, "## Armor Prices\n\nChainmail armor costs 120 crowns and grants +3 AC.")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        ctx = rules_context(w, "how much does chainmail armor cost")
        assert "Armor Prices" in ctx
        assert "120 crowns" in ctx
    finally:
        db.close()


def test_rules_context_empty_when_nothing_matches(client, seed):
    _set_world_rules(seed.world_a.id, "## Combat\n\nRoll a d20 to attack.")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert rules_context(w, "completely unrelated wizzlefrobnicator query") == ""
    finally:
        db.close()


def test_rules_context_respects_limit(client, seed):
    _set_world_rules(seed.world_a.id, "## Armor\n\nArmor armor armor costs money.\n\n## Weapons\n\nA weapon, armor-adjacent, costs less.")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        ctx = rules_context(w, "armor", limit=1)
        # Only the higher-scoring section (more "armor" occurrences) should appear.
        assert "Armor" in ctx
        assert "Weapons" not in ctx
    finally:
        db.close()


def test_rules_context_zero_limit_disabled(client, seed):
    _set_world_rules(seed.world_a.id, "## Armor\n\nChainmail costs 120 crowns.")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert rules_context(w, "chainmail armor", limit=0) == ""
    finally:
        db.close()


# ── smart_world_context now also folds in Rules ─────────────────────────────

def test_smart_world_context_includes_rules_when_relevant(client, seed):
    """The original motivating bug: a price that only ever lived in Rules
    prose (never as an Entity) is now actually findable."""
    _set_world_rules(seed.world_a.id, "## Armor Prices\n\nChainmail armor costs 120 crowns.")
    db = SessionLocal()
    try:
        context, non_notes, notes = smart_world_context(db, seed.world_a.id, "chainmail armor cost")
        assert "120 crowns" in context
        # Rules text isn't an Entity — it must never show up in these lists,
        # which the RAG-transparency panel renders as pinnable chips.
        assert non_notes == []
        assert notes == []
    finally:
        db.close()


def test_smart_world_context_rules_runs_unfiltered_for_players_too(client, seed):
    """Rules has no per-row visibility to filter — it's already visible to
    every world member regardless of role, same as GET /rules itself — so
    a non-GM `user` must still get the Rules match."""
    _set_world_rules(seed.world_a.id, "## Armor Prices\n\nChainmail armor costs 120 crowns.")
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        context, _non_notes, _notes = smart_world_context(db, seed.world_a.id, "chainmail armor cost", user=player)
        assert "120 crowns" in context
    finally:
        db.close()


# ── End-to-end: the player-facing route now surfaces Rules content ─────────

def test_world_context_player_route_surfaces_rules_content(client, seed):
    _set_world_rules(seed.world_a.id, "## Armor Prices\n\nChainmail armor costs 120 crowns.")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.players_can_use_ai_chat = True
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "how much does chainmail armor cost"})
    assert r.status_code == 200
    assert "120 crowns" in r.json()["context"]
