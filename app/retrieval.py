"""Shared entity-retrieval helpers for every RAG-flavored feature in this
app: AI Chat's "Smart Context" panel and entity-generation assist
(app.main), the Chronicler's Q&A (app.routers.chronicler), and session
Summarize/Condense RAG (app.audio_jobs._build_rag_context). Previously
these lived in app.main as private helpers, with chronicler.py keeping its
own second, ILIKE-only, unfiltered copy — routers can't import from main.py
(main.py imports every router, so the reverse would be circular) and
audio_jobs.py could only reach them via a deferred `from . import main`
inside a function body for the same reason. Pulled out to this leaf module,
which imports nothing from main/audio_jobs/any router, so everyone can
import it directly and normally instead.
"""
import re
from pathlib import Path
from typing import Optional

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from .models import Entity, World, entity_player_access

_CORE_RULES_PATH = Path(__file__).parent / "core_rules.md"


def world_rules_markdown(world) -> str:
    """This world's own rules if the GM has set any, else the bundled N&D
    core rules — the same fallback app.main._world_rules_markdown uses for
    every human-facing Rules page/download (that function delegates here
    instead of keeping a second copy, now that a RAG caller in this leaf
    module needs it too). Unlike that version, this one does NOT strip
    legacy doc-export `<a name=...>` anchors — irrelevant noise for a
    plain-text RAG excerpt that's never rendered as HTML."""
    if world and (getattr(world, "rules_md", None) or "").strip():
        return world.rules_md
    return _CORE_RULES_PATH.read_text(encoding="utf-8", errors="ignore") if _CORE_RULES_PATH.exists() else ""


_MD_HEADING_RE = re.compile(r'^(#{1,3})[ \t]+(.+?)[ \t]*$', re.MULTILINE)

# Independent of EXCERPT_CHARS/EXCERPT_TOTAL_BUDGET above (those are
# per-entity-body budgets) — Rules is one document searched as a whole, so
# it gets its own, deliberately small budget: a couple of matching
# sections is plenty to answer "how much does X cost", and a GM's full
# rules document can be very long.
RULES_EXCERPT_CHARS = 1200
RULES_EXCERPT_TOTAL_BUDGET = 2000
RULES_SECTION_LIMIT = 2


def _rules_sections(markdown: str) -> list:
    """Splits raw rules markdown into (heading, body) pairs at H1/H2/H3
    boundaries. Rules' own convention (see app.main._rules_toc) reserves a
    bare H1 for the page's own title and never uses one in the body, but a
    GM's own uploaded rules_md isn't guaranteed to follow that, so this
    splits on all three rather than assuming. Text before the first
    heading (if any) is kept under a synthetic "Introduction" heading
    rather than silently dropped; a document with no headings at all comes
    back as one single "Rules" section."""
    if not markdown:
        return []
    matches = list(_MD_HEADING_RE.finditer(markdown))
    if not matches:
        return [("Rules", markdown.strip())] if markdown.strip() else []
    sections = []
    if matches[0].start() > 0:
        intro = markdown[:matches[0].start()].strip()
        if intro:
            sections.append(("Introduction", intro))
    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        if body:
            sections.append((heading, body))
    return sections


def rules_context(world, query: str, limit: int = RULES_SECTION_LIMIT) -> str:
    """Keyword-relevance search over `world`'s Rules text (the GM's own
    rules_md, or the bundled core rules) — same word-tokenization
    find_relevant_entities uses (words > 3 chars), scored by raw
    occurrence count across each section's heading+body, highest first.

    This closes a real gap: neither this module's own retrieval nor
    Chronicler's (app.routers.chronicler.build_chronicler_system_prompt)
    ever read World.rules_md at all — a player asking "how much does
    armor cost" got no answer whenever that price only ever lived in Rules
    prose, never in an Entity. Returns "" when nothing scores above zero,
    so an unrelated question doesn't pad every prompt with a generic rules
    dump — same "only when it actually matched" posture entity retrieval
    already has."""
    if limit <= 0:
        return ""
    words = [w for w in re.split(r'\W+', query.lower()) if len(w) > 3]
    if not words:
        return ""
    markdown = world_rules_markdown(world)
    if not markdown:
        return ""
    scored = []
    for heading, body in _rules_sections(markdown):
        haystack = f"{heading}\n{body}".lower()
        score = sum(haystack.count(w) for w in words)
        if score:
            scored.append((score, heading, body))
    if not scored:
        return ""
    scored.sort(key=lambda t: t[0], reverse=True)
    lines = []
    budget = RULES_EXCERPT_TOTAL_BUDGET
    for _score, heading, body in scored[:limit]:
        if budget <= 0:
            break
        excerpt = body[:min(RULES_EXCERPT_CHARS, budget)]
        lines.append(f"- [Rules] {heading}: {excerpt}")
        budget -= len(excerpt)
    return "\n".join(lines)

# AI 1.1 — RAG retrieval could always *find* an entity by its body text
# (FTS5 indexes name/summary/body/tags), but the model never actually saw
# that body: format_context_from_entities emitted only a one-line
# "[kind] name (subtype): summary" per entity, capped at whatever fits in
# Entity.summary. For the first EXCERPT_COUNT entities in retrieval-ranked
# order, an excerpt of the actual body now gets appended under the
# one-liner, capped per-entity and in total so a long session's worth of
# retrieved lore can't balloon the system prompt unboundedly. Fixed,
# conservative defaults rather than new user-facing controls in the three
# different RAG settings panels (AI Chat, Condense/Summarize, Chronicler
# has none at all) — the existing entity/notes *count* limits already let
# a GM dial back prompt size if needed; this only changes how much of each
# already-retrieved entity gets included.
EXCERPT_COUNT = 5
EXCERPT_CHARS = 1200
EXCERPT_TOTAL_BUDGET = 8000


def _visibility_filter(q, user):
    """Restrict `q` (a query over Entity) to what `user` may see. `user`
    is None for every caller that doesn't have one to give — a background
    job (audio_jobs.py) or an already-GM-only route (app.main's AI Chat/
    entity-gen endpoints) — which leaves the query unfiltered, identical to
    this module's pre-extraction behavior for those call sites. A real,
    logged-in, non-GM caller only sees visible_to_players entities plus
    anything specifically shared with them — the same rule entity list/
    detail pages themselves already enforce, applied here so the LLM
    reading this context never sees what its own player couldn't."""
    if user is None or user.is_gm:
        return q
    shared = q.session.query(entity_player_access.c.entity_id).filter(
        entity_player_access.c.user_id == user.id
    )
    return q.filter(or_(Entity.visible_to_players.isnot(False), Entity.id.in_(shared)))


def find_relevant_entities_fts(
    db: Session, world_id: int, words: list, limit: int, user=None, kind: Optional[str] = None,
) -> list:
    """FTS5 prefix search over Entity(name, summary, body, tags) — unlike
    the _ilike fallback below, this also matches an entity's full body
    text, and ranks results by SQLite's own bm25-based relevance (`rank`)
    instead of "whatever order the table happens to be in". Raises on any
    failure (FTS5 unavailable, entity_fts missing on an old/degraded DB) —
    the caller falls back to find_relevant_entities_ilike in that case.
    `kind`, when given, is applied in the SQL itself (not as a Python
    post-filter) so a kind-filtered caller's `limit` still returns up to
    that many matches of the right kind, not up to `limit` matches of any
    kind with most of them then discarded."""
    fts_query = " OR ".join(f'"{w.replace(chr(34), chr(34)*2)}"*' for w in words)
    sql = (
        "SELECT entities.id FROM entity_fts "
        "JOIN entities ON entities.id = entity_fts.rowid "
        "WHERE entity_fts MATCH :q AND entities.world_id = :wid "
    )
    params = {"q": fts_query, "wid": world_id, "lim": limit}
    if kind:
        sql += "AND entities.kind = :kind "
        params["kind"] = kind
    sql += "ORDER BY rank LIMIT :lim"
    rows = db.execute(text(sql), params).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return []
    q = _visibility_filter(db.query(Entity).filter(Entity.id.in_(ids)), user)
    by_id = {e.id: e for e in q.all()}
    return [by_id[i] for i in ids if i in by_id]


def find_relevant_entities_ilike(
    db: Session, world_id: int, words: list, limit: int, user=None, kind: Optional[str] = None,
) -> list:
    filters = [
        or_(
            Entity.name.ilike(f'%{w}%'),
            Entity.summary.ilike(f'%{w}%'),
            Entity.tags.ilike(f'%{w}%'),
        )
        for w in words
    ]
    q = _visibility_filter(
        db.query(Entity).filter(Entity.world_id == world_id, or_(*filters)), user,
    )
    if kind:
        q = q.filter(Entity.kind == kind)
    return q.order_by(Entity.kind, Entity.name).limit(limit).all()


def find_relevant_entities(db: Session, world_id: int, query: str, limit: int = 25, user=None) -> list:
    words = [w for w in re.split(r'\W+', query.lower()) if len(w) > 3]
    if not words:
        q = _visibility_filter(db.query(Entity).filter(Entity.world_id == world_id), user)
        return q.order_by(Entity.kind, Entity.name).limit(limit).all()
    try:
        return find_relevant_entities_fts(db, world_id, words, limit, user=user)
    except Exception:
        return find_relevant_entities_ilike(db, world_id, words, limit, user=user)


def format_context_from_entities(
    entities: list, excerpt_count: int = EXCERPT_COUNT,
    excerpt_chars: int = EXCERPT_CHARS, excerpt_total_budget: int = EXCERPT_TOTAL_BUDGET,
) -> str:
    """One line per entity ("- [kind] name (subtype): summary"), plus — for
    the first `excerpt_count` entities in the given order (retrieval-ranked
    callers should pass their most-relevant-first) — an indented excerpt of
    Entity.body underneath, so the model actually sees the lore text that
    got the entity retrieved in the first place rather than only its short
    summary. Each excerpt is capped at `excerpt_chars`; the running total
    across all excerpts stops growing past `excerpt_total_budget` (a
    handful of very long bodies can't blow the prompt out even if each
    individually fits under the per-entity cap). Pass excerpt_count=0 for
    the old summary-only behavior."""
    lines = []
    excerpt_total = 0
    for i, e in enumerate(entities):
        line = f"- [{e.kind}] {e.name}"
        if e.subtype:
            line += f" ({e.subtype})"
        if e.summary:
            line += f": {e.summary}"
        lines.append(line)
        if i < excerpt_count and e.body and excerpt_total < excerpt_total_budget:
            remaining = excerpt_total_budget - excerpt_total
            excerpt = e.body.strip()[:min(excerpt_chars, remaining)]
            if excerpt:
                lines.append(f"  {excerpt}")
                excerpt_total += len(excerpt)
    return "\n".join(lines)


def smart_world_context(
    db: Session, world_id: int, query: str,
    entity_limit: int = 25, notes_limit: int = 5, user=None, rules_limit: int = RULES_SECTION_LIMIT,
) -> tuple:
    """The interactive-RAG half of main.py's /api/ai/world-context-smart
    (AI Chat's Smart Context panel), factored to this leaf module so the
    AI-assist routes (app/routers/ai.py — which, like every router, can't
    import from main.py) get the identical retrieval behavior instead of a
    drifting second copy. Returns (context, non_notes, notes):

    - find_relevant_entities over the query (FTS5 with ILIKE fallback),
    - a non-note top-up ordered kind/name when keyword search underfills
      the entity limit (a query in a different language/script than the
      World's entity names has no literal overlap for the keyword search
      to match — the same gap app.audio_jobs._build_rag_context's own
      top-up closes on the job path),
    - a guaranteed-most-recent-notes block (ordered updated_at desc) up
      to notes_limit beyond whatever the search itself surfaced,
    - a rules_context() excerpt (see that function) appended at the end
      when the query matches something in the World's Rules text — Rules
      is a single free-text document, not a list of Entity rows, so unlike
      the two above it never counts toward entity_limit/notes_limit or
      appears in the returned non_notes/notes lists (which stay Entity-
      only, for the RAG-transparency panel's pin/list UI).

    `user=None` (the default) is deliberately unfiltered — the posture the
    original world-context-smart route established for its GM + assistant
    callers, which the auth gate already restricts to those tiers. Passing
    a real, non-GM `user` applies _visibility_filter to EVERY query this
    function runs (the initial search, the non-note top-up, and the notes
    top-up alike) — see app.routers.ai's player-facing RAG endpoint, which
    is the one caller that ever passes a real player `user` through here.
    Rules text has no per-row visibility to filter — a world's Rules page
    is already visible to every member of that world regardless of role,
    same as GET /rules itself, so rules_context runs unconditionally."""
    entities = find_relevant_entities(db, world_id, query, limit=max(entity_limit, 0), user=user)
    notes = [e for e in entities if e.kind == "note"]
    non_notes = [e for e in entities if e.kind != "note"]
    if entity_limit > 0 and len(non_notes) < entity_limit:
        seen_ids = {e.id for e in entities}
        topup_q = db.query(Entity).filter(Entity.world_id == world_id, Entity.kind != "note")
        if seen_ids:
            topup_q = topup_q.filter(~Entity.id.in_(seen_ids))
        topup_q = _visibility_filter(topup_q, user)
        topup = topup_q.order_by(Entity.kind, Entity.name).limit(entity_limit - len(non_notes)).all()
        non_notes = non_notes + topup
    if notes_limit > 0:
        notes_q = _visibility_filter(
            db.query(Entity).filter(Entity.world_id == world_id, Entity.kind == "note"), user,
        )
        note_entities = notes_q.order_by(Entity.updated_at.desc()).limit(notes_limit).all()
        seen_ids = {e.id for e in entities}
        extra_notes = [e for e in note_entities if e.id not in seen_ids]
        notes = notes + extra_notes
    context = format_context_from_entities(non_notes + notes)
    world = db.get(World, world_id)
    rules = rules_context(world, query, limit=rules_limit)
    if rules:
        context = f"{context}\n\n{rules}" if context else rules
    return context, non_notes, notes
