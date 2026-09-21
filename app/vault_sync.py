"""Hybrid RAG + knowledge-graph pipeline, ingestion half: parses a GM's
Obsidian vault (World.obsidian_vault_path — see that column's own comment
in app/models.py) into the two DERIVED indexes app.retrieval's
vector_search/graph_context read at query time: VaultChunk (embedded,
heading-scoped note excerpts) and EntityRelation (typed graph edges
resolved against this world's existing Entity names).

Deliberately NOT a replacement for the web-edited Entity table — a vault
note that doesn't match any existing Entity by name still contributes its
own VaultChunk rows (so its prose is still semantically searchable), but
never creates a new node: entity_relations edges only ever connect two
ALREADY-EXISTING Entities. Auto-creating entities from unmatched vault
notes is a real possible follow-up, deliberately deferred — it touches
visibility rules and kind assignment this first pass doesn't need to.

A full sync always REBUILDS a world's vault-derived rows from scratch
(deletes then reinserts) rather than diffing note-by-note — simpler, and
correct by construction: whatever the vault currently says IS the
complete truth for these two indexes, so there is no stale-edge case to
reason about. Safe to re-run any time; nothing here is ever hand-edited
afterward."""
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from . import ai as _ai
from .models import Entity, EntityRelation, VaultChunk
from .rendering import derive_name_variants
from .retrieval import _rules_sections

_log = logging.getLogger("nd.vault_sync")

_FRONTMATTER_RE = re.compile(r'\A---\s*\n(.*?)\n---\s*\n?', re.DOTALL)
_WIKILINK_RE = re.compile(r'\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]')
_FRONTMATTER_LIST_ITEM_RE = re.compile(r'^\s*-\s*(.+?)\s*$')
_H1_RE = re.compile(r'^#\s+(.+?)\s*$', re.MULTILINE)


def extract_wikilinks(text: str) -> list[str]:
    """Every distinct [[Name]] / [[Name|Display text]] / [[Name#Heading]]
    target in `text`, in first-seen order, trimmed of the alias/heading
    suffix Obsidian allows after a | or #."""
    seen = []
    for m in _WIKILINK_RE.finditer(text):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _strip_value_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse_frontmatter(md: str) -> tuple[dict, str]:
    """Splits `md` into (frontmatter, body). Not a general YAML parser —
    Obsidian frontmatter in practice is flat key:value pairs, inline
    lists (`tags: [a, b]`), and simple block lists (`tags:\\n  - a\\n  -
    b`); that is exactly what this covers, hand-rolled rather than
    pulling in a YAML dependency for a format this constrained. Anything
    genuinely exotic (nested maps, multi-line scalars) is left as a
    best-effort single string rather than raising — a vault sync
    shouldn't hard-fail a whole note over one unusual field.

    Every value comes back as either a `str` or a `list[str]`. No
    frontmatter block at all (or a malformed one, missing its closing
    `---`) yields ({}, md) unchanged."""
    m = _FRONTMATTER_RE.match(md)
    if not m:
        return {}, md
    raw, body = m.group(1), md[m.end():]
    fields: dict = {}
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if not key:
            continue
        if not rest:
            # Possible block list on the following indented "- item" lines.
            items = []
            while i < len(lines) and _FRONTMATTER_LIST_ITEM_RE.match(lines[i] or ""):
                items.append(_strip_value_quotes(_FRONTMATTER_LIST_ITEM_RE.match(lines[i]).group(1)))
                i += 1
            fields[key] = items
        elif rest.startswith("[") and rest.endswith("]") and not (rest.startswith("[[") and rest.endswith("]]")):
            # A YAML flow list (`tags: [npc, dockside]`) — but NOT a bare
            # [[wikilink]] value, which also happens to start/end with a
            # bracket and must stay a single scalar string so
            # extract_wikilinks can still find it.
            inner = rest[1:-1].strip()
            fields[key] = [_strip_value_quotes(v) for v in inner.split(",") if v.strip()] if inner else []
        else:
            fields[key] = _strip_value_quotes(rest)
    return fields, body


def _iter_vault_notes(vault_root: Path):
    for path in sorted(vault_root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            _log.warning("vault_sync: could not read %s", path, exc_info=True)
            continue
        yield path.relative_to(vault_root).as_posix(), text


def _entity_name_map(db: Session, world_id: int) -> dict[str, int]:
    """{lowercased name/alias/structural-variant: entity id} for this
    world's own Entity rows — how a vault wikilink/frontmatter relation
    target gets resolved to an existing node. No visibility filtering:
    unlike app.main._autolink_name_map (which feeds a specific viewer's
    rendered page), this only ever runs as a GM-triggered, server-side
    sync over the world's own full entity set."""
    names: dict[str, int] = {}
    rows = db.query(Entity.id, Entity.name, Entity.aliases).filter(Entity.world_id == world_id).all()
    for entity_id, name, aliases in rows:
        if name:
            names.setdefault(name.lower(), entity_id)
        for alias in (aliases or "").split(","):
            alias = alias.strip()
            if alias:
                names.setdefault(alias.lower(), entity_id)
    for entity_id, name, _aliases in rows:
        for variant in derive_name_variants(name or ""):
            names.setdefault(variant.lower(), entity_id)
    return names


async def sync_vault(db: Session, world) -> dict:
    """Rebuilds `world`'s VaultChunk/EntityRelation rows from its
    configured obsidian_vault_path. Returns a plain summary dict —
    {"notes", "chunks", "edges", "unmatched_notes", "vault_path"} — for a
    GM-facing confirmation (see POST /api/knowledge/sync). Raises
    ValueError if no vault path is configured or the path doesn't exist,
    so the route can turn that into a clean 400 instead of a stack trace."""
    vault_path = (getattr(world, "obsidian_vault_path", None) or "").strip()
    if not vault_path:
        raise ValueError("This world has no Obsidian vault path configured.")
    vault_root = Path(vault_path)
    if not vault_root.is_dir():
        raise ValueError(f"Vault path does not exist or is not a directory: {vault_path}")

    name_map = _entity_name_map(db, world.id)
    notes = list(_iter_vault_notes(vault_root))

    chunk_rows: list[VaultChunk] = []
    edge_rows: list[EntityRelation] = []
    unmatched_notes: list[str] = []

    for rel_path, raw in notes:
        frontmatter, body = parse_frontmatter(raw)
        h1_match = _H1_RE.search(body)
        stem_title = Path(rel_path).stem
        # A note's real "name" for matching against an existing Entity can
        # come from three places, in priority order: an explicit
        # frontmatter title (a GM saying so directly), the note's own H1
        # heading (Obsidian's own convention — the filename is often just
        # a slug, e.g. "bob.md" titled "# Bob the Fence" inside), or
        # finally the filename stem itself. Tried in that order rather
        # than only the first that exists, since a note titled in its H1
        # only might still fail to match on frontmatter alone.
        title_candidates = [
            str(frontmatter["title"]) if frontmatter.get("title") else None,
            h1_match.group(1) if h1_match else None,
            stem_title,
        ]
        display_title = next((t for t in title_candidates if t), stem_title)
        source_entity_id = next(
            (name_map[t.lower()] for t in title_candidates if t and t.lower() in name_map), None,
        )
        if source_entity_id is None:
            unmatched_notes.append(rel_path)

        if source_entity_id is not None:
            # Any frontmatter field whose value is (or contains) a
            # [[wikilink]] is treated as a graph edge, labeled by the
            # field's own key ("located_in", "member_of", ...) — no fixed
            # relation vocabulary to maintain here, since the vault is the
            # source of truth for whatever labels a GM actually uses. A
            # plain-string value with no brackets (title, kind, tags, a
            # status field) is just metadata, never a relation — requiring
            # the explicit [[ ]] signal avoids guessing that some
            # unrelated string field coincidentally matches an entity name.
            for key, value in frontmatter.items():
                if key in ("title", "tags", "kind", "aliases"):
                    continue
                values = value if isinstance(value, list) else [value]
                for v in values:
                    for link_name in extract_wikilinks(v):
                        target_id = name_map.get(link_name.lower())
                        if target_id is not None and target_id != source_entity_id:
                            edge_rows.append(EntityRelation(
                                world_id=world.id, source_id=source_entity_id,
                                target_id=target_id, relation=key, source_path=rel_path,
                            ))
            for link_name in extract_wikilinks(body):
                target_id = name_map.get(link_name.lower())
                if target_id is not None and target_id != source_entity_id:
                    edge_rows.append(EntityRelation(
                        world_id=world.id, source_id=source_entity_id,
                        target_id=target_id, relation="mentions", source_path=rel_path,
                    ))

        for heading, text in _rules_sections(body):
            text = text.strip()
            if not text:
                continue
            display_heading = display_title if heading in ("Introduction", "Rules") else heading
            embedding = None
            try:
                vec = await _ai.embed_text(text)
                embedding = _ai.pack_embedding(vec)
            except Exception:
                _log.warning("vault_sync: embedding failed for %s / %s", rel_path, heading, exc_info=True)
            chunk_rows.append(VaultChunk(
                world_id=world.id, source_path=rel_path, heading=display_heading,
                text=text, embedding=embedding,
            ))

    db.query(VaultChunk).filter(VaultChunk.world_id == world.id).delete(synchronize_session=False)
    db.query(EntityRelation).filter(
        EntityRelation.world_id == world.id, EntityRelation.source_path.isnot(None),
    ).delete(synchronize_session=False)
    db.add_all(chunk_rows)
    db.add_all(edge_rows)
    db.commit()

    return {
        "vault_path": vault_path,
        "notes": len(notes),
        "chunks": len(chunk_rows),
        "edges": len(edge_rows),
        "unmatched_notes": unmatched_notes,
    }
