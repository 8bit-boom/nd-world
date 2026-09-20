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
import logging
import re
from pathlib import Path
from typing import Optional

from sqlalchemy import or_, text
from sqlalchemy.orm import Session, defer

from .models import Entity, World, entity_player_access
from .rendering import strip_gm_only as _strip_gm_only
from .rules_render import strip_gm_directives as _strip_gm_directives

_log = logging.getLogger("nd.retrieval")

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
    # "rule(s)" specifically: a player phrase like "...from Rules" (steering
    # AI Chat toward the World Rules document, as opposed to entities/notes)
    # is a NAVIGATIONAL instruction, not a content keyword — but scored
    # as one, it's a near-useless discriminator once already inside the
    # Rules document itself: virtually any real rulebook has several
    # section titles built around the word "Rule" ("Core Rule", "The Rule
    # of Forms", "Armor Rule", ...) that have nothing to do with what's
    # actually being asked about, so it dilutes coverage-based scoring
    # toward whichever unrelated "Rule"-titled section happens to also
    # share one other word with the query, confirmed against a real rules
    # document (a query ending "...from Rules" pulled in several
    # completely unrelated "___ Rule"-titled sections above the section
    # that actually answered the question).
    "rule", "rules",
})


# Not a real-world limit for anything an actual player types — it exists
# to bound worst-case cost for callers that build a query from arbitrary
# document text rather than a chat message (app.routers.ai's interactive
# assist endpoints feed name+summary+body+instruction, up to tens of
# thousands of characters, straight into this function). Every downstream
# keyword-scoring pass is O(words) per candidate section/entity, so an
# uncapped word list turns one RAG call into tens of seconds of blocking
# CPU on a large Rules document — measured ~70ms to score a ~1.4MB rules
# doc against a 3-word query, which scales linearly with word count.
_MAX_QUERY_WORDS = 32


def _query_words(query: str) -> list:
    """Tokenizes `query` for keyword search/scoring: lowercased, split on
    non-word runs, dropping single characters and this module's own
    stopword list (see _STOPWORDS above) — used identically everywhere
    this file needs "the meaningful words in this query", so a fix or
    tuning here applies consistently to entity search, Rules search,
    excerpt-picking, and priority-entity search alike instead of drifting
    across four separately-maintained copies. De-duplicates (a repeated
    word used to double-count in _keyword_score for no reason) and caps at
    _MAX_QUERY_WORDS, both preserving first-occurrence order — a real
    chat question never comes close to the cap, so this only bites the
    document-sized queries described above."""
    seen: set = set()
    out: list = []
    for w in re.split(r'\W+', query.lower()):
        if len(w) >= 2 and w not in _STOPWORDS and w not in seen:
            seen.add(w)
            out.append(w)
            if len(out) >= _MAX_QUERY_WORDS:
                break
    return out


def _stem(word: str) -> str:
    """A crude, deliberately conservative singular/plural stem: strips a
    single trailing "s" for a word long enough that it's unlikely to BE
    the stem itself (len > 3, so "gas"/"was" aren't mangled). RPG
    terminology constantly shifts singular/plural between a player's
    question and a table's own column header ("trait" vs "Traits"), and
    unlike _STOPWORDS this needs to run in both directions — deliberately
    NOT extended to the "-es" plural pattern (class/box/church -> -es):
    that would require distinguishing a true sibilant plural from a word
    that already ends in a silent "e" before the "s" (headache+s vs
    church+es both end in "ches"), and getting it wrong risks mangling
    exactly the short RPG terms _STOPWORDS was built to protect (e.g.
    "axe"/"axes" — axe already ends in "xe", so an "-es" strip would wrongly
    yield "ax"). Shared by _keyword_score (whole-word scoring) and
    find_relevant_entities_fts (FTS5 query construction) so retrieval and
    scoring can never disagree about which words are "the same" word."""
    return word[:-1] if word.endswith('s') and len(word) > 3 else word


# A heading/name match is a far stronger "this is the right section" signal
# than a body match: a section literally TITLED "Weapon Traits" is almost
# certainly the answer to "tell me about weapon traits" even if its body
# (e.g. a table) never repeats those words again, whereas a long prose
# section that happens to mention "weapon" several times in an unrelated
# context is not.
#
# Scoring by DISTINCT-WORD COVERAGE (how many different query words
# matched at all), not raw occurrence count, for the same reason: raw
# counting lets a section that happens to repeat ONE word many times
# outscore a section that actually matches MULTIPLE different query
# words — confirmed against a real ~870KB rules document, where a
# "Source Map" chapter-history table (pure editorial meta-content, listing
# which source file each chapter came from) scored competitively with the
# real "Ordinary Weapons" chapter purely because its own table densely
# repeated "rules" and "ordinary" as substrings of unrelated filenames/
# chapter titles (16 and 5 raw hits) — zero of which were an actual
# heading match, and none of which reflect real relevance. Raw hit counts
# still contribute a small, CAPPED tiebreak (_BODY_HIT_TIEBREAK_CAP) so
# two candidates with identical coverage don't score as a dead tie, but a
# capped tiebreak can't let sheer repetition manufacture a win the way an
# uncapped sum could.
_HEADING_COVERAGE_WEIGHT = 40
_BODY_COVERAGE_WEIGHT = 8
_BODY_HIT_TIEBREAK_CAP = 3


def _keyword_score(heading: str, body: str, words: list) -> int:
    """Scores a (heading, body) section/entity against tokenized query
    `words` — whole-word matches only (a regex \\b...\\b, not a raw
    substring count, so "art" in a query can't silently score a hit against
    "Cartographer"), tolerating a simple trailing-s plural mismatch either
    direction ("trait" query vs. "Traits" heading, or vice versa) since RPG
    terminology constantly shifts singular/plural between a question and a
    table's own column header. Each DISTINCT query word matched in the
    heading counts for _HEADING_COVERAGE_WEIGHT; each distinct word matched
    anywhere in the body counts for _BODY_COVERAGE_WEIGHT — see the
    constants' own comment for why coverage, not a raw per-occurrence sum.

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
    head_coverage = 0
    body_coverage = 0
    body_hits = 0
    for w in words:
        pattern = re.compile(r'\b' + re.escape(_stem(w)) + r's?\b')
        if pattern.search(heading_l):
            head_coverage += 1
        b = len(pattern.findall(body_l))
        if b:
            body_coverage += 1
            body_hits += b
    return (
        head_coverage * _HEADING_COVERAGE_WEIGHT
        + body_coverage * _BODY_COVERAGE_WEIGHT
        + min(body_hits, _BODY_HIT_TIEBREAK_CAP)
    )


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

# best_matching_excerpt's own gate for how weak a match can be and still
# earn a slot alongside the #1 pick — see that function's own comment.
_SECTION_RELEVANCE_RATIO = 0.4

# Independent of EXCERPT_CHARS/EXCERPT_TOTAL_BUDGET above (those are
# per-entity-body budgets) — Rules is one document searched as a whole, so
# it gets its own, deliberately small budget: a couple of matching
# sections is plenty to answer "how much does X cost", and a GM's full
# rules document can be very long.
RULES_EXCERPT_CHARS = 1200
RULES_EXCERPT_TOTAL_BUDGET = 2000
RULES_SECTION_LIMIT = 2


def _rules_sections_full(markdown: str) -> list:
    """Same split _rules_sections does, but each entry also carries the
    heading's nesting `level` and its `start`/`full_end` positions in the
    (line-ending-normalized) markdown — the span a section would have
    covered before the merge-cap fallback below collapsed it to just its
    own direct text. _rules_sections itself only exposes the (heading,
    body) pairs most callers need; this richer form exists for the
    "guaranteed table slot" logic in best_matching_excerpt/rules_context,
    which needs to recognize that a COLLAPSED section's real content lives
    in headings physically nested inside that original span, not
    duplicate this same heading-matching scan a second time to find them."""
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
        return [("Rules", markdown.strip(), 0, 0, len(markdown))] if markdown.strip() else []
    sections = []
    if matches[0].start() > 0:
        intro = markdown[:matches[0].start()].strip()
        if intro:
            sections.append(("Introduction", intro, 0, 0, matches[0].start()))
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
            sections.append((heading, body, level, start, full_end))
    return sections


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
    return [(heading, body) for heading, body, _level, _start, _full_end in _rules_sections_full(markdown)]


# A markdown table's separator row — e.g. "|---|---|---|" or
# "| :--- | ---: |" — requiring at least two columns (so a plain "---"
# horizontal-rule divider, which has no pipe at all, never matches). Used
# by best_matching_excerpt/rules_context's "guaranteed table slot" (see
# _select_relevant_sections) to recognize a section that's an actual DATA
# table (a weapon/price list, a stat block) as opposed to prose that
# happens to mention a table exists, or a short "collapsed" ancestor
# section (see _rules_sections_full) whose own heading matches the query
# well but whose body — by construction — never contains the deeper
# content its heading promises.
_MD_TABLE_SEPARATOR_RE = re.compile(
    r'^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$',
    re.MULTILINE,
)


def _has_markdown_table(text: str) -> bool:
    return bool(_MD_TABLE_SEPARATOR_RE.search(text))


def rules_context(world, query: str, limit: int = RULES_SECTION_LIMIT, is_gm: bool = True) -> str:
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
    already has.

    is_gm=False (pass smart_world_context's own strip_secrets-derived
    flag) strips every :::gm directive block from the Rules text before
    it's searched/excerpted at all — a real leak this used to have: the
    GM-only-content invariant app.rules_render documents ("The :::gm block
    ... is never rendered ... a player could read") was enforced for the
    rendered Rules PAGE but not for this RAG path, which read world.
    rules_md completely raw and would happily quote a :::gm secret
    straight back to a player who asked the right question. [gmonly] tags
    (the Entity-body syntax) are stripped too, belt-and-braces, in case a
    GM pastes that syntax into rules_md instead of :::gm — this function
    doesn't know which one a given GM actually used."""
    if limit <= 0:
        return ""
    words = _query_words(query)
    if not words:
        return ""
    markdown = world_rules_markdown(world)
    if not markdown:
        return ""
    if not is_gm:
        markdown = _strip_gm_directives(markdown)
        markdown = _strip_gm_only(markdown)
    full_sections = _rules_sections_full(markdown)
    picks = _select_relevant_sections(
        full_sections, words, RULES_EXCERPT_TOTAL_BUDGET, limit,
        per_section_cap=RULES_EXCERPT_CHARS,
    )
    if not picks:
        return ""
    lines = [f"- [Rules] {heading}: {excerpt}" for heading, excerpt in picks]
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


def _fts_term(word: str) -> str:
    """One OR-able FTS5 MATCH term for `word`, plural-tolerant in both
    directions. FTS5's `"word"*` is a PREFIX match, so the singular-query-
    matches-plural-text direction already worked ("trait"* matches
    "traits") — but the reverse direction matched NOTHING: a player typing
    the plural, the more natural phrasing for "what weapons have traits"
    or "are there other armors", got zero FTS results whenever the actual
    text used the singular ("weapon"). Verified directly against SQLite's
    FTS5: `'"weapons"*'` against a document containing only "weapon"
    returns no rows. Uses the same _stem() rule _keyword_score does, so
    retrieval and scoring can never disagree about which words are meant
    to match the same thing."""
    esc = word.replace('"', '""')
    stem = _stem(word)
    if stem == word:
        return f'"{esc}"*'
    return f'("{esc}"* OR "{stem.replace(chr(34), chr(34) * 2)}"*)'


def find_relevant_entities_fts(
    db: Session, world_id: int, words: list, limit: int, user=None, kind: Optional[str] = None,
) -> list:
    """FTS5 prefix search over Entity(name, summary, body, tags, aliases)
    — unlike the _ilike fallback below (which does check aliases too, just
    not body), this also matches an entity's full body text, and ranks
    results by a bm25-based relevance weighted per column (see the
    ORDER BY below) instead of "whatever order the table happens to be
    in". Raises on any failure (FTS5 unavailable, entity_fts missing on an
    old/degraded DB) — the caller falls back to find_relevant_entities_ilike
    in that case.
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
    fts_query = " OR ".join(_fts_term(w) for w in words)
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
    # Plain `rank` is bm25 with every column weighted equally (1.0), so a
    # long body that happens to repeat a query word several times in
    # passing can outrank the entity literally NAMED after it — the same
    # bug class _HEADING_COVERAGE_WEIGHT already fixes for Rules sections,
    # still unfixed one layer up at the entity level. Verified directly:
    # an entity named "Weapon Traits" (whose body doesn't repeat the word)
    # lost to one named "Ashfall Rifle" whose body says "weapon" 8 times,
    # under plain `rank`; explicit column weights below reverse that.
    # Weights are pinned to entity_fts's actual column order from its
    # CREATE VIRTUAL TABLE statement (app/database.py) — name, summary,
    # body, tags, aliases — do NOT reorder these without also checking
    # that. An alias match ("Vosk" for "Hunter Edmund Vosk, the
    # Greyfather") is nearly as strong an identity signal as the name
    # itself, so it's weighted close to `name` rather than left at bm25's
    # silent 1.0 default for an unweighted column (which SQLite applies
    # with no error if fewer weights are given than columns exist —
    # verified directly; it does NOT raise on a stale weight count, it
    # just silently under-weights whatever column was left out).
    sql += "ORDER BY bm25(entity_fts, 10.0, 2.0, 1.0, 5.0, 8.0) LIMIT :lim"
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
            Entity.aliases.ilike(f'%{w}%'),
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
        # Silent before: an FTS5 failure (unavailable, entity_fts missing
        # on an old/degraded DB) degraded retrieval to the ILIKE fallback
        # — which doesn't search entity body text at all, exactly where
        # Rules/Player-Guide-style content lives — with no log line
        # anywhere. If FTS ever breaks on a real install, results get much
        # worse and nothing says so.
        _log.warning("FTS5 retrieval failed, falling back to ILIKE", exc_info=True)
    try:
        return find_relevant_entities_ilike(db, world_id, words, limit, user=user)
    except Exception:
        # Belt-and-braces: _query_words already caps word count (see
        # _MAX_QUERY_WORDS) specifically so this OR'd-ILIKE query can't hit
        # SQLite's "Expression tree is too large" error, but an uncaught
        # exception here would otherwise surface as an unhandled 500 on
        # whatever request triggered it rather than just an empty result.
        _log.warning("ILIKE retrieval fallback also failed", exc_info=True)
        return []


# Additional, small number of sections best_matching_excerpt/rules_context
# will ALWAYS reserve room for, on top of max_sections/limit, when the
# #1-ranked pick turned out to be a "collapsed" ancestor section (see
# _rules_sections_full): its own heading matched the query well, but
# _MAX_SCORED_CHARS' merge cap means its actual body is just a short
# intro, with the real content — the tables its heading promises — living
# in headings nested underneath it. _keyword_score has no way to know a
# heading like "23. Ordinary Weapons" no longer "contains" what it
# structurally implies once collapsed — it still legitimately matches
# every query word literally present in that heading — so without this, a
# genuinely relevant DATA TABLE nested under it (a "Melee Weapons" price
# list, say) could lose the ratio-gate/max_sections race to its own
# collapsed parent, or to an unrelated section elsewhere in a large
# document that merely happens to share one more keyword. This is
# scoped tight on purpose: only sections that are actual descendants of
# that specific collapsed pick (not "any table anywhere with a decent
# score") and that contain a real markdown table qualify — the concrete,
# reported failure mode was a reader asking for "the X table" and getting
# prose instead, even after literally naming the containing chapter.
# Additive, not carved out of chars_budget's normal share: a query that
# never triggers this (the common case — most top picks aren't collapsed,
# or their own body already covers the table) behaves byte-for-byte as
# before.
_MAX_GUARANTEED_TABLE_SECTIONS = 3
_GUARANTEED_TABLE_CHARS = 500


def _select_relevant_sections(
    full_sections: list, words: list, chars_budget: int, max_sections: int,
    per_section_cap: Optional[int] = None,
) -> list:
    """Shared by best_matching_excerpt and rules_context. Scores every
    (heading, body, level, start, full_end) entry from _rules_sections_full
    against tokenized `words` and returns up to `max_sections` (heading,
    excerpt) pairs: the highest-scoring sections whose score is
    competitive with the #1 pick (_SECTION_RELEVANCE_RATIO), deduped
    bidirectionally so a hierarchy-merged parent doesn't repeat a child's
    text verbatim — see best_matching_excerpt's own docstring for more on
    both of those — PLUS, if the #1 pick was itself a collapsed ancestor,
    any of ITS OWN descendant sections that carry a genuine markdown table
    and weren't already swept in above (see _MAX_GUARANTEED_TABLE_SECTIONS'
    own comment). Returns [] when nothing scores.

    `per_section_cap`, when given, additionally limits any single normal
    pick's own share of `chars_budget` (rules_context's RULES_EXCERPT_CHARS
    — a caller with no such per-item cap of its own, like
    best_matching_excerpt, leaves this None and a pick may use as much of
    the remaining budget as it needs)."""
    scored = []
    for heading, sect_body, _level, start, full_end in full_sections:
        score = _keyword_score(heading, sect_body[:_MAX_SCORED_CHARS], words)
        if score:
            scored.append((score, heading, sect_body, start, full_end))
    if not scored:
        return []
    scored.sort(key=lambda t: t[0], reverse=True)
    top_score = scored[0][0]
    top_start, top_full_end = scored[0][3], scored[0][4]
    top_collapsed = (top_full_end - top_start) > _MAX_SCORED_CHARS

    picks = []
    included_bodies = []
    remaining = chars_budget
    for score, heading, sect_body, _start, _full_end in scored:
        if len(picks) >= max_sections or remaining <= 0:
            break
        if picks and score < top_score * _SECTION_RELEVANCE_RATIO:
            break
        if any(sect_body in inc or inc in sect_body for inc in included_bodies):
            continue
        cap = min(remaining, per_section_cap) if per_section_cap else remaining
        piece = sect_body[:cap]
        picks.append((heading, piece))
        included_bodies.append(sect_body)
        remaining -= len(piece)

    if top_collapsed:
        guaranteed = 0
        for score, heading, sect_body, start, _full_end in scored:
            if guaranteed >= _MAX_GUARANTEED_TABLE_SECTIONS:
                break
            if not (top_start < start < top_full_end):
                continue
            if not _has_markdown_table(sect_body):
                continue
            if any(sect_body in inc or inc in sect_body for inc in included_bodies):
                continue
            piece = sect_body[:_GUARANTEED_TABLE_CHARS]
            picks.append((heading, piece))
            included_bodies.append(sect_body)
            guaranteed += 1

    return picks


def best_matching_excerpt(body: str, query: str, chars_budget: int, max_sections: int = 3) -> str:
    """The chunk(s) of `body` most relevant to `query`, for a body long
    enough that a plain prefix slice risks missing the actually-relevant
    part entirely — the same problem rules_context already solves for
    World Rules text, applied here to any single large entity/note body. A
    GM's whole "Player Guide" consolidated into one note is exactly this
    case: the section actually answering "what weapon traits exist" can
    sit thousands of characters past the document's own front matter/
    revision history, which is all body[:chars_budget] would ever surface
    — the entity gets correctly RETRIEVED (its body mentions "weapon"
    plenty), but the excerpt the model actually sees never reaches the
    relevant table, so it truthfully (and unhelpfully) reports the info
    isn't there.

    Splits on the same markdown headings _rules_sections uses, scores each
    section by keyword overlap with `query` (_query_words/_keyword_score —
    see those for the tokenization and coverage-based scoring), and
    returns up to `max_sections` of the highest-scoring sections' own text,
    joined together, instead of only ever the single best match — a real
    gap for a "list all X" question spanning several SIBLING headings
    (e.g. a document with separate "Melee Weapons"/"Thrown Weapons"/
    "Ammunition" tables rather than one combined list): returning only
    section #1 gave a partial answer even once ranking correctly favored
    the right neighborhood of the document. A section already fully
    contained in one already picked (the common parent/child overlap case
    — a parent section under _rules_sections' own merge cap includes all
    of its children's text verbatim) is skipped rather than duplicating
    the same content twice. Falls back to a plain prefix slice when the
    body has no headings to split on, already fits the budget uncut, or
    nothing in it scores against the query — so a caller still gets SOME
    content rather than none when the question shares no keywords with any
    heading/body."""
    body = body.strip()
    if not body or len(body) <= chars_budget:
        return body
    words = _query_words(query)
    if not words:
        return body[:chars_budget]
    full_sections = _rules_sections_full(body)
    if len(full_sections) <= 1:
        return body[:chars_budget]
    picks = _select_relevant_sections(full_sections, words, chars_budget, max_sections)
    if not picks:
        return body[:chars_budget]
    parts = [piece if heading == "Introduction" else f"[{heading}] {piece}" for heading, piece in picks]
    return "\n\n".join(parts)


def format_context_from_entities(
    entities: list, excerpt_count: int = EXCERPT_COUNT,
    excerpt_chars: int = EXCERPT_CHARS, excerpt_total_budget: int = EXCERPT_TOTAL_BUDGET,
    strip_gm_only: bool = False, query: str = "", excerpt_ids: Optional[set] = None,
) -> str:
    """One line per entity ("- [kind] name (subtype): summary"), plus — for
    the first `excerpt_count` ELIGIBLE entities in the given order
    (retrieval-ranked callers should pass their most-relevant-first) — an
    indented excerpt of Entity.body underneath, so the model actually sees
    the lore text that got the entity retrieved in the first place rather
    than only its short summary. Each excerpt is capped at `excerpt_chars`;
    the running total across all excerpts stops growing past
    `excerpt_total_budget` (a handful of very long bodies can't blow the
    prompt out even if each individually fits under the per-entity cap).
    Pass excerpt_count=0 for the old summary-only behavior.

    excerpt_ids, when given, restricts which entities are "eligible" for an
    excerpt at all — entities not in the set only ever get their one-line
    summary, regardless of position. Without it every entity in `entities`
    is eligible and only ITS OWN ORDER decides who gets the first
    `excerpt_count` slots — a real bug when the caller's list isn't purely
    rank order (smart_world_context's list is
    [matched] + [arbitrary alphabetical top-up] + [more matched] + [recent
    notes], and with entity_limit>=5 the alphabetical top-up alone fills
    every excerpt slot before a single MATCHED entity is ever reached,
    while the top-up itself — having scored zero against the query — falls
    through best_matching_excerpt straight to a blind prefix slice, so the
    prompt fills up on unrelated front matter instead of the content that
    actually answers the question). Pass the set of entity ids that
    actually matched the search to make ONLY those eligible, independent of
    where they land in the final display order.

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
    excerpted = 0
    for e in entities:
        summary = _strip_gm_only(e.summary) if strip_gm_only else e.summary
        line = f"- [{e.kind}] {e.name}"
        if e.subtype:
            line += f" ({e.subtype})"
        if summary:
            line += f": {summary}"
        lines.append(line)
        eligible = excerpt_ids is None or e.id in excerpt_ids
        if eligible and excerpted < excerpt_count and e.body and excerpt_total < excerpt_total_budget:
            body = _strip_gm_only(e.body) if strip_gm_only else e.body
            remaining = excerpt_total_budget - excerpt_total
            budget = min(excerpt_chars, remaining)
            excerpt = best_matching_excerpt(body, query, budget) if query else body.strip()[:budget]
            if excerpt:
                lines.append(f"  {excerpt}")
                excerpt_total += len(excerpt)
            excerpted += 1
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
    - a rules_context() excerpt (see that function), LED WITH rather than
      appended, when the query matches something in the World's Rules
      text — the official rules text is the more authoritative source, so
      it reads first, ahead of ordinary entities/notes, not as an
      afterthought tacked on at the end. Rules is a single free-text
      document, not a list of Entity rows, so unlike the two above it
      never counts toward entity_limit/notes_limit or appears in the
      returned non_notes/notes lists (which stay Entity-only, for the
      RAG-transparency panel's pin/list UI),
    - a priority_entities_context() block for any Entity.rag_priority row
      that scores against the query — same independence from entity_limit/
      notes_limit, the same lead-with-not-append-to ordering, and the same
      exclusion from non_notes/notes as Rules above (see that function's
      own docstring for why this exists alongside Rules rather than being
      redundant with it).

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
    Rules text has no per-ROW visibility to filter — a world's Rules page
    is already visible to every member of that world regardless of role,
    same as GET /rules itself, so rules_context runs unconditionally
    regardless of `user` — but it DOES still have GM-only CONTENT within
    that one document (:::gm directive blocks, or a stray [gmonly] tag),
    which is why rules_context is passed is_gm=not strip_secrets: the same
    real-non-GM `user` that strips secrets out of entity text above must
    also strip them out of the one Rules document every member can see."""
    entities = find_relevant_entities(db, world_id, query, limit=max(entity_limit, 0), user=user)
    notes = [e for e in entities if e.kind == "note"]
    non_notes = [e for e in entities if e.kind != "note"]
    if entity_limit > 0 and len(non_notes) < entity_limit:
        seen_ids = {e.id for e in entities}
        topup_q = db.query(Entity).filter(Entity.world_id == world_id, Entity.kind != "note")
        if seen_ids:
            topup_q = topup_q.filter(~Entity.id.in_(seen_ids))
        topup_q = _visibility_filter(topup_q, user)
        # A top-up entity only ever contributes its one-line name/summary
        # (it exists purely as a name-reference list — see this function's
        # own docstring — and is now excluded from excerpt eligibility by
        # the excerpt_ids fix below), so its (potentially large) `body`
        # never needs loading at all. A GM's own entity-limit slider goes
        # up to 250, so at max this was loading 250 full entity bodies
        # from SQLite on every single message just to print 250 one-line
        # summaries.
        topup_q = topup_q.options(defer(Entity.body))
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
    # format_context_from_entities only excerpts its first EXCERPT_COUNT
    # entities IN LIST ORDER — but non_notes/notes above is
    # [matched] + [arbitrary alphabetical top-up] + [more matched] + [most-
    # recent notes], so with entity_limit>=EXCERPT_COUNT (5) the top-up
    # alone can fill every excerpt slot before a single entity that
    # actually MATCHED the query is ever reached. Worse, the top-up scores
    # zero against the query (it exists only as a name-reference list for
    # cross-language recognition — see this function's own docstring),
    # so its "excerpt" is a blind prefix slice of unrelated front matter
    # burning the shared excerpt_total_budget for nothing. Reordering the
    # DISPLAY list to put every matched entity first (independent of
    # note/non-note kind) and passing excerpt_ids=matched_ids makes only
    # the entities that actually matched eligible for an excerpt at all —
    # non_notes/notes themselves are left untouched since the RAG-
    # transparency panel's pin/list UI depends on their existing order.
    matched_ids = {e.id for e in entities}
    display_order = (
        [e for e in non_notes if e.id in matched_ids]
        + [e for e in notes if e.id in matched_ids]
        + [e for e in non_notes if e.id not in matched_ids]
        + [e for e in notes if e.id not in matched_ids]
    )
    context = format_context_from_entities(
        display_order, strip_gm_only=strip_secrets, query=query, excerpt_ids=matched_ids,
    )
    world = db.get(World, world_id)
    rules = rules_context(world, query, limit=rules_limit, is_gm=not strip_secrets)
    priority = priority_entities_context(
        db, world_id, query, user=user, strip_gm_only=strip_secrets,
        exclude_ids={e.id for e in non_notes} | {e.id for e in notes},
    )
    # Rules and GM-flagged priority content lead the assembled context,
    # ahead of ordinary matched/topped-up entities — both are the more
    # authoritative sources (the official rules text itself, and content
    # the GM specifically flagged as important) and should read as
    # "here's the ground truth" before "here's some possibly-related
    # lore", not as an afterthought tacked on at the end.
    extra = "\n\n".join(part for part in (rules, priority) if part)
    if extra:
        context = f"{extra}\n\n{context}" if context else extra
    return context, non_notes, notes
