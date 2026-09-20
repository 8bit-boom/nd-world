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
from .rendering import strip_gm_only as _strip_gm_only

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


# A short, generic English stopword list — NOT a length cutoff. Every
# keyword-scoring/tokenizing helper below used to filter with a blunt
# `len(w) > 3`, which was really trying to drop noise words like "what"/
# "does"/"have"/"were" but, as a side effect, also silently dropped every
# genuine short RPG term a query could contain ("axe", "orc", "elf", "bow",
# "gun", "imp", "hex", "war", and abbreviations like "hp"/"ac"/"xp") —
# exactly the kind of query a player in a hurry actually types. A player
# asking "does this axe have the brutal trait" got every one of those
# content words thrown away except "brutal", so the answer's odds of
# actually finding the relevant weapon entry depended on one lucky keyword.
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "what", "does", "have", "has",
    "had", "will", "would", "should", "could", "with", "this", "that",
    "from", "there", "their", "about", "which", "when", "where", "how",
    "why", "who", "whom", "can", "did", "do", "is", "am", "be", "been",
    "being", "not", "but", "than", "then", "them", "they", "you", "your",
    "yours", "my", "mine", "me", "i", "we", "us", "our", "ours", "he",
    "she", "his", "her", "hers", "its", "in", "on", "at", "to", "of", "a",
    "an", "as", "by", "or", "if", "so", "no", "yes", "all", "any", "some",
    "each", "few", "more", "most", "other", "such", "only", "own", "same",
    "just", "into", "over", "under", "again", "once", "here", "also",
    "tell", "give", "show", "please", "know", "want", "like", "get",
})


def _query_words(query: str) -> list:
    """Tokenizes `query` for keyword search/scoring: lowercased, split on
    non-word runs, dropping single characters and this module's own
    stopword list (see _STOPWORDS above) — used identically everywhere
    this file needs "the meaningful words in this query", so a fix or
    tuning here applies consistently to entity search, Rules search,
    excerpt-picking, and priority-entity search alike instead of drifting
    across four separately-maintained copies."""
    return [w for w in re.split(r'\W+', query.lower()) if len(w) >= 2 and w not in _STOPWORDS]


# A heading/name match is a far stronger "this is the right section" signal
# than a body match: a section literally TITLED "Weapon Traits" is almost
# certainly the answer to "tell me about weapon traits" even if its body
# (e.g. a table) never repeats those words again, whereas a long prose
# section that happens to mention "weapon" several times in an unrelated
# context is not. Plain substring-count scoring treated the two the same,
# so a verbose section repeating a query word 4-5 times in passing could
# outrank the one section whose own heading names exactly what was asked —
# confirmed to reproduce the reported "AI can't find the Weapon Traits
# table even though there's a whole section titled that" failure.
_HEADING_MATCH_WEIGHT = 8


def _keyword_score(heading: str, body: str, words: list) -> int:
    """Scores a (heading, body) section/entity against tokenized query
    `words` — whole-word matches only (a regex \\b...\\b, not a raw
    substring count, so "art" in a query can't silently score a hit against
    "Cartographer"), tolerating a simple trailing-s plural mismatch either
    direction ("trait" query vs. "Traits" heading, or vice versa) since RPG
    terminology constantly shifts singular/plural between a question and a
    table's own column header. Heading matches count for
    _HEADING_MATCH_WEIGHT points each; body matches count for 1.

    Does NOT cap `body`'s length itself — a caller scoring a whole,
    potentially very large document as a single candidate (priority_
    entities_context, deciding whether a flagged reference document is
    relevant at all) needs the WHOLE body considered, or a match sitting
    past whatever cap was chosen would make an actually-relevant document
    score as a non-match and get silently excluded — exactly backwards for
    a mechanism whose entire purpose is surfacing content buried deep in a
    large document. A caller scoring individual, already-bounded SECTIONS
    of a document (rules_context, best_matching_excerpt) is the one that
    needs a length cap (see _MAX_SCORED_CHARS) — because there, an
    unbounded section is itself the bug — so those callers cap `body`
    before passing it in, rather than this shared function capping it for
    everyone."""
    heading_l = heading.lower()
    body_l = body.lower()
    score = 0
    for w in words:
        stem = w[:-1] if w.endswith('s') and len(w) > 3 else w
        pattern = re.compile(r'\b' + re.escape(stem) + r's?\b')
        score += len(pattern.findall(heading_l)) * _HEADING_MATCH_WEIGHT
        score += len(pattern.findall(body_l))
    return score


_MD_HEADING_RE = re.compile(r'^(#{1,6})[ \t]+(.+?)[ \t]*$', re.MULTILINE)

# Dual purpose, both defending against the same failure mode: raw
# keyword-count scoring scales with how much text is being counted, so an
# unusually large section — whether from _rules_sections merging many
# nested children into one parent (see its own docstring), or simply a
# single huge leaf section with no children at all (e.g. a document's own
# Table of Contents, which densely repeats every chapter's own title as a
# link) — can outscore a properly-scoped candidate purely by containing
# more words, not by being more relevant. Used both to cap how much
# nested-child text _rules_sections merges into a parent before it falls
# back to that parent's own direct text, and by rules_context/
# best_matching_excerpt to cap the body they pass into _keyword_score when
# scoring one already-split SECTION at a time (see that function's own
# docstring for why it does NOT apply this cap itself).
_MAX_SCORED_CHARS = 4000

# Independent of EXCERPT_CHARS/EXCERPT_TOTAL_BUDGET above (those are
# per-entity-body budgets) — Rules is one document searched as a whole, so
# it gets its own, deliberately small budget: a couple of matching
# sections is plenty to answer "how much does X cost", and a GM's full
# rules document can be very long.
RULES_EXCERPT_CHARS = 1200
RULES_EXCERPT_TOTAL_BUDGET = 2000
RULES_SECTION_LIMIT = 2


def _rules_sections(markdown: str) -> list:
    """Splits raw rules markdown into (heading, body) pairs at ANY markdown
    heading (H1-H6), hierarchy-aware: a heading's own section extends up to
    the next heading at the SAME OR SHALLOWER level, so a deeper heading
    nested underneath it stays part of that section's own body instead of
    ending it. A real GM rules document routinely structures a big
    reference chapter as an H3 "chapter" whose actual content lives in
    H4/H5 subheadings below it — e.g. an H3 "Traits of the Hunt" chapter
    containing H4 "Weapon Traits" containing H5 "Weapon — Hunt Mode" with
    the actual named trait table. Splitting on H1-H3 only used to make
    every deeper heading invisible to this function, so the WHOLE chapter
    (subheadings and all) collapsed into one giant section under its H3
    title; any excerpt taker slicing a prefix of that section only ever
    reached its opening paragraph, never the actual named entries several
    subsections down. Every heading still gets its own (heading, body)
    entry regardless of depth — a query matching the broad chapter title
    and a query matching one specific nested subheading are both scored as
    candidates, and whichever one actually matches the query wins; a
    parent's own section body naturally includes its children's text too
    (nesting, not exclusion), so the parent remains a valid, if coarser,
    candidate in its own right. Text before the first heading (if any) is
    kept under a synthetic "Introduction" heading rather than silently
    dropped; a document with no headings at all comes back as one single
    "Rules" section."""
    if not markdown:
        return []
    # A GM's rules_md can arrive with Windows line endings (pasted from a
    # Word doc, edited on Windows, etc.) — \r isn't matched by [ \t]*$
    # before MULTILINE's $ anchor, so an un-normalized "## Weapon Traits\r\n"
    # captured the heading as "Weapon Traits\r": a trailing \r baked into
    # the section's own name. That heading no longer string-matched anything
    # (the "\r" is invisible but present), and worse, `\r` further corrupts
    # every downstream keyword-count/exact-match check against that heading.
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_MD_HEADING_RE.finditer(markdown))
    if not matches:
        return [("Rules", markdown.strip())] if markdown.strip() else []
    sections = []
    if matches[0].start() > 0:
        intro = markdown[:matches[0].start()].strip()
        if intro:
            sections.append(("Introduction", intro))
    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.end()
        rest = matches[i + 1:]
        own_end = rest[0].start() if rest else len(markdown)  # up to the very next heading of ANY level
        full_end = len(markdown)  # up to the next heading at this level or shallower
        for later in rest:
            if len(later.group(1)) <= level:
                full_end = later.start()
                break
        # Merging every nested child into its parent's body (full_end) is
        # what lets a section like "WEAPON TRAITS" pull in its own H5
        # sub-tables — but applied without limit, the same merge makes a
        # heading near the TOP of the document (the file's own H1 title, a
        # "Part" divider) swallow enormous stretches of unrelated text,
        # including this document's own Table of Contents (which densely
        # repeats every chapter's own title). A raw keyword-count score
        # scales with how much text is being counted, so that one
        # accidentally-enormous "section" would then outscore every
        # properly-scoped candidate purely by containing more words — not
        # by being more relevant. Above this cap, fall back to the
        # heading's own direct text only (own_end) — usually just its own
        # intro paragraph — rather than the runaway merged version; its
        # nested children still get their own, individually-capped entries
        # from their own loop iterations either way.
        end = full_end if (full_end - start) <= _MAX_SCORED_CHARS else own_end
        body = markdown[start:end].strip()
        if body:
            sections.append((heading, body))
    return sections


def rules_context(world, query: str, limit: int = RULES_SECTION_LIMIT) -> str:
    """Keyword-relevance search over `world`'s Rules text (the GM's own
    rules_md, or the bundled core rules) — same word-tokenization
    find_relevant_entities uses (_query_words: stopwords filtered out,
    not just short words), scored by raw occurrence count across each
    section's heading+body, highest first.

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
    words = _query_words(query)
    if not words:
        return ""
    markdown = world_rules_markdown(world)
    if not markdown:
        return ""
    scored = []
    for heading, body in _rules_sections(markdown):
        score = _keyword_score(heading, body[:_MAX_SCORED_CHARS], words)
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
    kind with most of them then discarded.

    A real bug this fixes: visibility filtering happens AFTER this raw SQL
    query already applied its own LIMIT — unlike find_relevant_entities_
    ilike below, whose _visibility_filter is part of the SAME SQLAlchemy
    query LIMIT gets applied to. For a real non-GM `user`, the top-`limit`
    FTS matches by rank can be mostly or entirely hidden from them, which
    used to silently shrink the returned list below `limit` even when
    plenty of OTHER, lower-ranked-but-still-visible matches existed to
    fill it — a player could get zero or few results for a query a GM
    asking the identical thing would get plenty for. Over-fetching a
    multiple of `limit` before filtering (only when a real non-GM `user`
    is given — the GM/no-user path is already exactly right and pays no
    extra cost) gives filtering enough candidates, still in the same rank
    order, to actually fill the requested count when enough visible
    matches exist at all."""
    fts_query = " OR ".join(f'"{w.replace(chr(34), chr(34)*2)}"*' for w in words)
    needs_filtering = user is not None and not user.is_gm
    # max(..., 20): even the smallest `limit` a real caller passes still
    # needs a real cushion — plenty of exact-rank ties are realistic (many
    # short entities matching the same one or two words), and fetching only
    # limit*5 of those (e.g. 5, for a limit=1 lookup) would still starve out
    # a visible match sitting just past the tied group ahead of it.
    fetch_limit = min(max(limit * 5, 20), 250) if needs_filtering else limit
    sql = (
        "SELECT entities.id FROM entity_fts "
        "JOIN entities ON entities.id = entity_fts.rowid "
        "WHERE entity_fts MATCH :q AND entities.world_id = :wid "
    )
    params = {"q": fts_query, "wid": world_id, "lim": fetch_limit}
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
    return [by_id[i] for i in ids if i in by_id][:limit]


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
    words = _query_words(query)
    if not words:
        q = _visibility_filter(db.query(Entity).filter(Entity.world_id == world_id), user)
        return q.order_by(Entity.kind, Entity.name).limit(limit).all()
    try:
        return find_relevant_entities_fts(db, world_id, words, limit, user=user)
    except Exception:
        return find_relevant_entities_ilike(db, world_id, words, limit, user=user)


def best_matching_excerpt(body: str, query: str, chars_budget: int) -> str:
    """The chunk of `body` most relevant to `query`, for a body long enough
    that a plain prefix slice risks missing the actually-relevant part
    entirely — the same problem rules_context already solves for World
    Rules text, applied here to any single large entity/note body. A GM's
    whole "Player Guide" consolidated into one note is exactly this case:
    the section actually answering "what weapon traits exist" can sit
    thousands of characters past the document's own front matter/revision
    history, which is all body[:chars_budget] would ever surface — the
    entity gets correctly RETRIEVED (its body mentions "weapon" plenty),
    but the excerpt the model actually sees never reaches the relevant
    table, so it truthfully (and unhelpfully) reports the info isn't there.

    Splits on the same H1-H3 markdown headings _rules_sections uses, scores
    each section by keyword overlap with `query` (_query_words: stopwords
    filtered out, same tokenization as find_relevant_entities/rules_context),
    and returns the single highest-scoring section's own text — not several
    sections stitched together, since this fills one entity's own excerpt slot, not
    a dedicated multi-section block the way rules_context's return value
    is. Falls back to a plain prefix slice when the body has no headings to
    split on, already fits the budget uncut, or nothing in it scores
    against the query — so a caller still gets SOME content rather than
    none when the question shares no keywords with any heading/body."""
    body = body.strip()
    if not body or len(body) <= chars_budget:
        return body
    words = _query_words(query)
    if not words:
        return body[:chars_budget]
    sections = _rules_sections(body)
    if len(sections) <= 1:
        return body[:chars_budget]
    scored = []
    for heading, section_body in sections:
        score = _keyword_score(heading, section_body[:_MAX_SCORED_CHARS], words)
        if score:
            scored.append((score, heading, section_body))
    if not scored:
        return body[:chars_budget]
    scored.sort(key=lambda t: t[0], reverse=True)
    _score, heading, section_body = scored[0]
    excerpt = section_body[:chars_budget]
    return excerpt if heading == "Introduction" else f"[{heading}] {excerpt}"


def format_context_from_entities(
    entities: list, excerpt_count: int = EXCERPT_COUNT,
    excerpt_chars: int = EXCERPT_CHARS, excerpt_total_budget: int = EXCERPT_TOTAL_BUDGET,
    strip_gm_only: bool = False, query: str = "",
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
    the old summary-only behavior.

    query, when given, picks each excerpt via best_matching_excerpt
    instead of a blind body[:excerpt_chars] prefix slice — the section of a
    long, heading-structured body actually relevant to the question, not
    just whatever happens to come first. Pass "" (the default) to keep the
    old prefix-slice behavior for a caller with no real query of its own
    (e.g. app.audio_jobs' pinned-entity path has one; a caller building
    context for something other than a single user question may not).

    strip_gm_only=True removes every [gmonly]...[/gmonly] block (tag and
    contents alike — see app.rendering.strip_gm_only) from each entity's
    summary/body before it's used here. Callers pass this whenever the
    context being built could reach a non-GM (a player's AI Chat question,
    Chronicler's answer) — entities themselves are already visibility-
    filtered by the caller's _visibility_filter pass, but that only decides
    whether a WHOLE entity is included; this is what keeps a GM secret
    embedded inside an otherwise player-visible entity from leaking into
    that included entity's own text."""
    lines = []
    excerpt_total = 0
    for i, e in enumerate(entities):
        summary = _strip_gm_only(e.summary) if strip_gm_only else e.summary
        line = f"- [{e.kind}] {e.name}"
        if e.subtype:
            line += f" ({e.subtype})"
        if summary:
            line += f": {summary}"
        lines.append(line)
        if i < excerpt_count and e.body and excerpt_total < excerpt_total_budget:
            body = _strip_gm_only(e.body) if strip_gm_only else e.body
            remaining = excerpt_total_budget - excerpt_total
            budget = min(excerpt_chars, remaining)
            excerpt = best_matching_excerpt(body, query, budget) if query else body.strip()[:budget]
            if excerpt:
                lines.append(f"  {excerpt}")
                excerpt_total += len(excerpt)
    return "\n".join(lines)


# Independent of entity_limit/notes_limit the same way Rules text is (see
# smart_world_context's own docstring on rules_context) — a GM-flagged
# Entity.rag_priority row is always searched regardless of those limits,
# and never counts toward them or appears in the returned non_notes/notes
# lists, matching Rules' own "only in the free-text context, not the
# RAG-transparency panel's pin/list UI" treatment.
PRIORITY_ENTITY_LIMIT = 3
PRIORITY_EXCERPT_CHARS = 1200
PRIORITY_EXCERPT_TOTAL_BUDGET = 2400


def priority_entities_context(
    db: Session, world_id: int, query: str, user=None,
    limit: int = PRIORITY_ENTITY_LIMIT, strip_gm_only: bool = False, exclude_ids: Optional[set] = None,
) -> str:
    """Same "always searched, only actually included when it scores
    against the query, never counted toward the ordinary entity/notes RAG
    limits" treatment rules_context gives World Rules text — extended to
    any Entity the GM has opted in via Entity.rag_priority (the entity
    edit form's "High priority for AI" checkbox). Exists for a case Rules
    alone can't cover: a big reference document the GM keeps as an
    Entity/note rather than in Rules — a consolidated "Player Guide" note,
    say — whose own relevant section keyword retrieval might otherwise
    never surface, either because ordinary top-N entity_limit retrieval
    ranks other, more literally-matching entities above it and it falls
    out of the window entirely, or because ITS OWN excerpt would
    (format_context_from_entities' plain prefix slice, when not given a
    query) never reach the part that actually answers the question —
    best_matching_excerpt (used here the same way format_context_from_
    entities uses it when given a query) is what solves that second half.

    exclude_ids skips any entity the caller already included via the
    ordinary retrieval path, so a priority entity that ALSO happens to
    rank in the normal top-N results doesn't appear twice in one prompt.
    Returns "" when there are no priority entities in this world, none
    score against the query's keywords, or limit<=0 — same "only when it
    actually matched" posture as every other retrieval path here, so an
    unrelated question doesn't pad every single prompt with a GM's
    reference document regardless of relevance."""
    if limit <= 0:
        return ""
    words = _query_words(query)
    if not words:
        return ""
    q = _visibility_filter(
        db.query(Entity).filter(Entity.world_id == world_id, Entity.rag_priority.is_(True)), user,
    )
    if exclude_ids:
        q = q.filter(~Entity.id.in_(exclude_ids))
    entities = q.all()
    if not entities:
        return ""
    scored = []
    for e in entities:
        summary = _strip_gm_only(e.summary) if strip_gm_only else (e.summary or "")
        body = _strip_gm_only(e.body) if strip_gm_only else (e.body or "")
        score = _keyword_score(e.name, f"{summary}\n{body}", words)
        if score:
            scored.append((score, e, summary, body))
    if not scored:
        return ""
    scored.sort(key=lambda t: t[0], reverse=True)
    lines = []
    budget = PRIORITY_EXCERPT_TOTAL_BUDGET
    for _score, e, summary, body in scored[:limit]:
        if budget <= 0:
            break
        header = f"- [{e.kind}] {e.name}"
        if summary:
            header += f": {summary}"
        lines.append(header)
        if body:
            excerpt = best_matching_excerpt(body, query, min(PRIORITY_EXCERPT_CHARS, budget))
            if excerpt:
                lines.append(f"  {excerpt}")
                budget -= len(excerpt)
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
      only, for the RAG-transparency panel's pin/list UI),
    - a priority_entities_context() block for any Entity.rag_priority row
      that scores against the query — same independence from entity_limit/
      notes_limit and the same exclusion from non_notes/notes as Rules
      above (see that function's own docstring for why this exists
      alongside Rules rather than being redundant with it).

    `user=None` (the default) is deliberately unfiltered — the posture the
    original world-context-smart route established for its GM + assistant
    callers, which the auth gate already restricts to those tiers. Passing
    a real, non-GM `user` applies _visibility_filter to EVERY query this
    function runs (the initial search, the non-note top-up, and the notes
    top-up alike) — see app.routers.ai's player-facing RAG endpoint, which
    is the one caller that ever passes a real player `user` through here.
    The same real-non-GM `user` also makes format_context_from_entities
    strip any [gmonly]...[/gmonly] block out of each included entity's own
    text — visibility filtering alone only decides whether a whole entity
    is in scope, not whether a secret embedded inside an otherwise
    player-visible entity's body should be.
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
    strip_secrets = bool(user) and not user.is_gm
    context = format_context_from_entities(non_notes + notes, strip_gm_only=strip_secrets, query=query)
    world = db.get(World, world_id)
    rules = rules_context(world, query, limit=rules_limit)
    priority = priority_entities_context(
        db, world_id, query, user=user, strip_gm_only=strip_secrets,
        exclude_ids={e.id for e in non_notes} | {e.id for e in notes},
    )
    extra = "\n\n".join(part for part in (rules, priority) if part)
    if extra:
        context = f"{context}\n\n{extra}" if context else extra
    return context, non_notes, notes
