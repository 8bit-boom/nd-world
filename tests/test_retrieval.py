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
from app.models import Entity, EntityNote, User, World, entity_player_access
from app.retrieval import (
    _keyword_score,
    _query_words,
    _rules_sections,
    _table_aware_truncate,
    best_matching_excerpt,
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


def test_query_words_drops_rule_and_rules_as_navigational_noise():
    """A phrase like "...from Rules" steers AI Chat toward the World Rules
    document rather than entities/notes — that's an instruction about
    WHERE to search, not a content word, and once actually scoring
    sections inside the Rules document itself it's a near-useless
    discriminator (almost every real rulebook has several unrelated
    "___ Rule"-titled sections)."""
    words = _query_words("give me a list of weapons from the Rules")
    assert "rule" not in words and "rules" not in words
    assert "weapons" in words


# ── _keyword_score coverage-based scoring ───────────────────────────────────

def test_keyword_score_coverage_beats_repetition_of_one_word():
    """A real bug found against an actual ~870KB rules document: a pure
    editorial "Source Map" table (listing which source file each chapter
    was split from) scored competitively with real content purely by
    repeating "rules"/"ordinary" many times as substrings of unrelated
    filenames/chapter titles — raw per-occurrence counting can't tell
    "matches many DIFFERENT query words" from "repeats one word a lot",
    and the second one is much weaker evidence of real relevance."""
    words = _query_words("list of ordinary weapons")
    repeats_one_word = _keyword_score(
        "Source Map", "rules rules rules rules rules ordinary", words,
    )
    matches_two_words = _keyword_score(
        "Weapon Notes", "This covers ordinary weapons in one place.", words,
    )
    assert matches_two_words > repeats_one_word


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


# ── EntityNote inclusion — previously invisible to every RAG surface ────────
#
# EntityNote (a GM's discrete, independently hide/reveal-able notes
# attached to an entity, separate from Entity.body/summary) was never read
# by format_context_from_entities at all, regardless of relevance — not a
# markdown-formatting gap, the content just never reached the model.

def test_notes_omitted_when_no_db_given_old_behavior_unchanged(client, seed):
    eid = _make_entity(seed.world_a.id, name="Bob")
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        db.add(EntityNote(entity_id=eid, content="A secret note.", visible_to_players=True))
        db.commit()
        context = format_context_from_entities([e])  # no db= passed
        assert "A secret note." not in context
    finally:
        db.close()


def test_gm_sees_both_hidden_and_visible_notes(client, seed):
    eid = _make_entity(seed.world_a.id, name="Bob")
    db = SessionLocal()
    try:
        db.add_all([
            EntityNote(entity_id=eid, content="Secretly works for the Thieves Guild.", visible_to_players=False),
            EntityNote(entity_id=eid, content="Has a scar on his left hand.", visible_to_players=True),
        ])
        db.commit()
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], db=db)
        assert "[note] Secretly works for the Thieves Guild." in context
        assert "[note] Has a scar on his left hand." in context
    finally:
        db.close()


def test_player_only_sees_visible_to_players_notes(client, seed):
    eid = _make_entity(seed.world_a.id, name="Bob")
    db = SessionLocal()
    try:
        db.add_all([
            EntityNote(entity_id=eid, content="Secretly works for the Thieves Guild.", visible_to_players=False),
            EntityNote(entity_id=eid, content="Has a scar on his left hand.", visible_to_players=True),
        ])
        db.commit()
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], db=db, strip_gm_only=True)
        assert "Thieves Guild" not in context
        assert "[note] Has a scar on his left hand." in context
    finally:
        db.close()


def test_gmonly_span_inside_a_visible_note_is_still_stripped_for_players(client, seed):
    eid = _make_entity(seed.world_a.id, name="Bob")
    db = SessionLocal()
    try:
        db.add(EntityNote(
            entity_id=eid,
            content="Public info. [gmonly]Secret weakness: silver.[/gmonly] More public info.",
            visible_to_players=True,
        ))
        db.commit()
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], db=db, strip_gm_only=True)
        assert "silver" not in context
        assert "Public info." in context
        assert "More public info." in context
    finally:
        db.close()


def test_html_note_converted_to_markdown(client, seed):
    eid = _make_entity(seed.world_a.id, name="Bob")
    db = SessionLocal()
    try:
        db.add(EntityNote(
            entity_id=eid, content="<p>Imported <b>HTML</b> note content.</p>",
            visible_to_players=True, content_is_html=True,
        ))
        db.commit()
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], db=db)
        assert "<p>" not in context
        assert "**HTML**" in context
    finally:
        db.close()


def test_ineligible_entity_notes_are_excluded(client, seed):
    """Notes are gated by the same `eligible` check a body excerpt uses —
    an entity that isn't in excerpt_ids (e.g. an alphabetical top-up with
    no real relevance) gets no notes either."""
    eid = _make_entity(seed.world_a.id, name="Bob")
    db = SessionLocal()
    try:
        db.add(EntityNote(entity_id=eid, content="Irrelevant top-up note.", visible_to_players=True))
        db.commit()
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], db=db, excerpt_ids=set())
        assert "Irrelevant top-up note." not in context
    finally:
        db.close()


def test_notes_total_budget_caps_across_notes(client, seed):
    eid = _make_entity(seed.world_a.id, name="Bob")
    db = SessionLocal()
    try:
        db.add_all([EntityNote(entity_id=eid, content="Z" * 100, visible_to_players=True) for _ in range(3)])
        db.commit()
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], db=db)
        from app.retrieval import ENTITY_NOTES_TOTAL_BUDGET
        assert context.count("Z") <= ENTITY_NOTES_TOTAL_BUDGET
    finally:
        db.close()


def test_notes_fetched_in_one_bulk_query_not_per_entity(client, seed):
    """Regression guard against an N+1 query: notes for every entity in
    the list are fetched in a single EntityNote.entity_id.in_(...) query,
    not one query per entity."""
    ids = [_make_entity(seed.world_a.id, name=f"Entity {i}") for i in range(3)]
    db = SessionLocal()
    try:
        for eid in ids:
            db.add(EntityNote(entity_id=eid, content=f"Note for {eid}", visible_to_players=True))
        db.commit()
        entities = [db.get(Entity, i) for i in ids]
        from sqlalchemy import event
        query_count = 0

        def _count_queries(*_a, **_kw):
            nonlocal query_count
            query_count += 1

        event.listen(db.get_bind(), "before_cursor_execute", _count_queries)
        try:
            format_context_from_entities(entities, db=db)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", _count_queries)
        # One SELECT for the notes bulk-fetch; generous upper bound (not
        # an exact count) since this isn't testing unrelated query counts.
        assert query_count <= 3
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


def test_world_context_smart_excerpts_the_actual_match_not_alphabetical_topup_filler(client, seed):
    """A real bug: format_context_from_entities excerpts the first
    EXCERPT_COUNT (5) entities in LIST ORDER, but that order is
    non_notes (matched non-notes, THEN the arbitrary alphabetical top-up)
    followed by notes (matched notes, then recent-notes top-up) — always,
    regardless of which one actually matched the query. Here the query
    only matches a NOTE; with entity_limit >= 5 the ordinary (non-note)
    entity search finds nothing, so the ENTIRE non-note top-up (5 unrelated
    decoys, scoring zero against the query) fills every excerpt slot ahead
    of the note in list order — the note that actually answers the
    question never reaches an excerpt at all, while the 5 decoys each get
    a blind prefix-slice "excerpt" of irrelevant filler text instead."""
    for i in range(5):
        _make_entity(
            seed.world_a.id, name=f"Decoy {i}", kind="character",
            body="This is unrelated front-matter text that should never be quoted. " * 5,
        )
    _make_entity(
        seed.world_a.id, name="Zylo's Secret", kind="note",
        body="Zylo the blacksmith knows the secret of the Undermarket vault.",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={"query": "Zylo blacksmith", "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    context = r.json()["context"]
    assert "Undermarket" in context
    assert "unrelated front-matter" not in context


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


# A real reported bug, reproduced against the GM's actual ~870KB rules
# document: asking for "23. Ordinary Weapons table" never surfaced the
# actual weapon price list. "23. Ordinary Weapons" collapses (its full
# children exceed _MAX_SCORED_CHARS — see _rules_sections_full's own
# docstring) to just this short intro, yet its heading STILL matches every
# query word literally, so it (and another unrelated section that merely
# repeats "ordinary"/"weapons" in prose) fill rules_context's top-`limit`
# slots ahead of "Melee Weapons" — the section nested INSIDE "23. Ordinary
# Weapons" that actually has the data table the query is asking for.
_ORDINARY_WEAPONS_RULES_MD = (
    "# Book\n\n"
    "## 23. Ordinary Weapons\n\n"
    "Ordinary weapons are mundane arms anyone might carry.\n\n"
    "### Melee Weapons\n\n"
    "| Weapon | Cost |\n|---|---|\n| Kitchen knife | 2 Crows |\n| Hand axe | 5 Crows |\n\n"
    "### Filler Subsection\n\n" + ("Padding text to exceed the merge cap. " * 200)
    + "\n\n## Ordinary Armor\n\nOrdinary armor is cheap and easy to find, unlike ordinary weapons which are pricier.\n\n"
    + "## Unrelated Chapter\n\nNothing to do with any of this."
)


def test_rules_context_surfaces_a_table_buried_under_a_collapsed_ancestor(client, seed):
    _set_world_rules(seed.world_a.id, _ORDINARY_WEAPONS_RULES_MD)
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        ctx = rules_context(w, "23 ordinary weapons")
        assert "Kitchen knife" in ctx
        assert "Hand axe" in ctx
    finally:
        db.close()


def test_rules_context_guarantee_does_not_fire_for_a_normal_top_pick():
    """The guarantee only kicks in when the #1 pick is either a collapsed
    ancestor (see _rules_sections_full) or got its own excerpt truncated
    by the budget — an ordinary, short, non-collapsed, non-truncated top
    pick behaves exactly as before, with no extra sections pulled in."""
    from app.retrieval import _rules_sections_full, _select_relevant_sections, _query_words

    md = (
        "## Armor\n\nChainmail armor costs 120 crowns.\n\n"
        "## Weapons\n\n| Weapon | Cost |\n|---|---|\n| Sword | 15 Crows |\n"
    )
    words = _query_words("armor")
    picks = _select_relevant_sections(_rules_sections_full(md), words, 2000, 2)
    headings = [h for h, _ in picks]
    assert headings == ["Armor"]


def test_select_relevant_sections_guarantees_a_table_truncated_out_of_a_non_collapsed_top_pick():
    """A second, distinct way a table can get lost: the top pick's own
    merged body (children small enough to NOT trigger _rules_sections'
    collapse fallback) can still be bigger than the excerpt budget, so the
    shown excerpt is truncated well before reaching a table nested inside
    it — the "collapsed ancestor" framing was never really the point,
    "the reader can't see the whole table" is, and that can happen to a
    perfectly ordinary, non-collapsed section too."""
    from app.retrieval import _rules_sections_full, _select_relevant_sections, _query_words

    intro = "Ordinary weapons are common. " * 12  # long enough to fill a small budget on its own
    md = (
        "## 23. Ordinary Weapons\n\n" + intro + "\n\n"
        "### Melee Weapons\n\n| Weapon | Cost |\n|---|---|\n"
        "| Kitchen knife | 2 Crows |\n| Hand axe | 5 Crows |\n"
    )
    full = _rules_sections_full(md)
    assert not any(h == "23. Ordinary Weapons" and (fe - s) > 4000 for h, _b, _l, s, fe in full), (
        "fixture must NOT collapse — this test is specifically about the non-collapsed case"
    )
    words = _query_words("23 ordinary weapons")
    picks = _select_relevant_sections(full, words, 200, 2, per_section_cap=200)
    headings = [h for h, _ in picks]
    assert "Melee Weapons" in headings
    melee_excerpt = dict(picks)["Melee Weapons"]
    assert "Kitchen knife" in melee_excerpt
    assert "Hand axe" in melee_excerpt


def test_clip_section_never_severs_a_table_row_mid_line():
    from app.retrieval import _clip_section

    table = (
        "| Weapon | Cost |\n|---|---|\n"
        "| Kitchen knife | 2 Crows |\n"
        "| Hand axe | 5 Crows |\n"
        "| Straight sword | 12 Crows |\n"
    )
    # Cut in the middle of the "Hand axe" row.
    clipped = _clip_section(table, len("| Weapon | Cost |\n|---|---|\n| Kitchen knife | 2 Crows |\n| Hand ax"))
    assert "| Hand ax" not in clipped  # no dangling row fragment
    for line in clipped.splitlines():
        if line.startswith("|"):
            assert line.endswith("|"), f"truncated mid-row: {line!r}"


def test_clip_section_marks_truncated_output():
    from app.retrieval import _clip_section

    assert _clip_section("short", 100) == "short"  # no marker when nothing was cut
    clipped = _clip_section("a" * 500, 100)
    assert "truncated" in clipped
    assert len(clipped) <= 100 + len("\n…[truncated — more entries follow in the full Rules text]")


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


def test_smart_world_context_leads_with_rules_not_matched_entities(client, seed):
    """Rules (and, by the same logic, GM-flagged priority content) is the
    more authoritative source — the official rules text, or content a GM
    specifically flagged as important — and should read FIRST in the
    assembled context, ahead of ordinary matched entities, not as an
    afterthought tacked onto the end."""
    _set_world_rules(seed.world_a.id, "## Armor Prices\n\nChainmail armor costs 120 crowns.")
    _make_entity(
        seed.world_a.id, name="Chainmail Merchant", kind="character",
        body="Sells chainmail armor and other goods.",
    )
    db = SessionLocal()
    try:
        context, _non_notes, _notes = smart_world_context(db, seed.world_a.id, "chainmail armor cost")
        assert context.index("120 crowns") < context.index("Chainmail Merchant")
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


def test_smart_world_context_strips_gm_only_rules_directive_for_players(client, seed):
    """A real leak: rules_context read world.rules_md completely raw, with
    no GM-only filtering at all — app.rules_render documents the :::gm
    directive as a security invariant ("never rendered ... a player could
    read") for the rendered Rules PAGE, but this RAG path bypassed it
    entirely and would happily quote a :::gm secret straight back to a
    player who asked the right question. Non-secret content in the SAME
    document must still reach the player (this isn't just "hide all
    rules" — the previous test already pins that half)."""
    _set_world_rules(
        seed.world_a.id,
        "## Armor Prices\n\nChainmail armor costs 120 crowns.\n\n"
        ":::gm\nThe secret weakness of the BBEG is silver.\n:::\n",
    )
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        context_player, _, _ = smart_world_context(
            db, seed.world_a.id, "chainmail armor cost secret BBEG", user=player,
        )
        assert "120 crowns" in context_player
        assert "silver" not in context_player
        assert "BBEG" not in context_player

        gm = db.get(User, seed.gm.id)
        context_gm, _, _ = smart_world_context(
            db, seed.world_a.id, "chainmail armor cost secret BBEG", user=gm,
        )
        assert "120 crowns" in context_gm
        assert "silver" in context_gm
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


# ── Table-aware excerpts: "list ordinary weapons"-style questions ───────────
#
# A rules section whose body IS a markdown table used to reach the model as
# body[:1200] — sliced mid-row ("| Pe"), most rows discarded, so a listing
# question got an unusable fragment and the model (per the grounding clause)
    # correctly reported it could only see part of the list.

def _weapons_rules_md(row_count=30):
    rows = "\n".join(f"| Weapon {i} | {i}d6 | {i * 10} cr |" for i in range(1, row_count + 1))
    return (
        "# Core Rules\n\n"
        "## Attributes\n\nStrength, Dexterity.\n\n"
        "## Ordinary Weapons\n\nOrdinary weapons available to all characters:\n\n"
        "| Name | Damage | Cost |\n|------|--------|------|\n" + rows + "\n\n"
        "## Armor\n\nChainmail costs 500 cr.\n"
    )


def test_rules_context_listing_query_returns_complete_table(client, seed):
    """Every row of the weapons table reaches the context — the question
    'list ordinary weapons' is only answerable with the whole list."""
    _set_world_rules(seed.world_a.id, _weapons_rules_md())
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        ctx = rules_context(w, "list ordinary weapons")
        for i in range(1, 31):
            assert f"Weapon {i} |" in ctx
        # No dangling partial row: every emitted table line is complete.
        for line in ctx.splitlines():
            if line.strip().startswith("|"):
                assert line.rstrip().endswith("|"), f"partial table row leaked: {line!r}"
    finally:
        db.close()


def test_rules_context_skips_duplicate_parent_section(client, seed):
    """The hierarchy-aware merge makes the H1 parent's body CONTAIN the
    matching child's table, so both used to be excerpted — the duplicate
    burned a slot and the shared budget re-sending rows already present.
    The parent must now be skipped and its slot left for different
    content (or simply unused)."""
    _set_world_rules(seed.world_a.id, _weapons_rules_md())
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        ctx = rules_context(w, "list ordinary weapons")
        # Each table row appears exactly once — the H1 re-quote is gone.
        assert ctx.count("Weapon 1 |") == 1
        assert "- [Rules] Core Rules:" not in ctx
    finally:
        db.close()


def test_table_aware_truncate_prose_unchanged():
    """Outside tables the behavior is byte-identical to body[:limit]."""
    body = "just some prose with no table at all " * 50
    assert _table_aware_truncate(body, 100) == body[:100]


def test_table_aware_truncate_extends_whole_table_within_hard_budget():
    """A table that starts inside the preferred limit but ends before the
    hard budget is included IN FULL, header to last row — one table is one
    answer for a listing question."""
    table = "| A | B |\n|---|---|\n" + "\n".join(f"| row{i} | x |" for i in range(40))
    body = "intro paragraph\n\n" + table + "\n\noutro paragraph"
    # Hard budget ends exactly at the table's last row: truncation is forced
    # (the whole body doesn't fit), yet the whole table does.
    budget = len("intro paragraph\n\n" + table)
    out = _table_aware_truncate(body, 40, hard_budget=budget)
    assert out.endswith("| row39 | x |")
    assert "outro" not in out


def test_table_aware_truncate_never_cuts_row_when_table_exceeds_budget():
    """A table that cannot fit the hard budget is cut at a row boundary:
    whole rows only, however many fit, never a sliced fragment."""
    table = "| H1 | H2 |\n|---|---|\n" + "\n".join(f"| row{i} | data |" for i in range(200))
    out = _table_aware_truncate(table, 500, hard_budget=500)
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines[-1].endswith("|")
    assert all(l.startswith("|") for l in lines)
    assert len(out) <= 500
    for l in lines:  # every emitted line is a verbatim source line
        assert l in table


def test_best_matching_excerpt_keeps_table_rows_intact(client, seed):
    """Entity-bodies (catalog notes etc.) get the same guarantee via
    best_matching_excerpt, not just World Rules text."""
    rows = "\n".join(f"| Catalog Weapon {i} | {i}d6 |" for i in range(1, 26))
    body = (
        "# Player Guide\n\nRevision history and front matter filler.\n\n"
        "## Equipment Catalog\n\nAll ordinary weapons in the game:\n\n"
        "| Name | Damage |\n|------|--------|\n" + rows + "\n\n"
        "## Spells\n\nFireball does 8d6.\n"
    )
    excerpt = best_matching_excerpt(body, "list the catalog weapons", 1200)
    for i in range(1, 26):
        assert f"Catalog Weapon {i}" in excerpt
    for line in excerpt.splitlines():
        if line.strip().startswith("|"):
            assert line.rstrip().endswith("|"), f"partial table row leaked: {line!r}"
