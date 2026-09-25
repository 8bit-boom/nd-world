"""Hybrid RAG + knowledge-graph pipeline, the HTTP surface it needs:
triggering a vault rebuild (see app.vault_sync.sync_vault) and the
AI-assisted relation-suggestion pass (see app.vault_sync.
suggest_relations_for_vault). Everything else — World.obsidian_vault_path
itself, and the query-time vector_search/graph_context that consult what
these produce — is plumbed through the existing world-settings form and
app.retrieval respectively; this router exists only because these are
actions, not settings, and every other action-triggering route in this app
already follows the same one-router-per-feature layout (see AGENTS.md)."""
import json as _json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from typing import Optional

from .. import vault_sync as _vault_sync
from ..database import get_db
from ..deps import get_world_ctx
from ..models import Entity, EntityRelation

router = APIRouter()


@router.post("/api/knowledge/sync")
async def api_knowledge_sync(
    request: Request, db=Depends(get_db), active_world: Optional[str] = Cookie(None),
):
    """GM-only (like every route not in app.main._is_player_safe/
    _is_assistant_safe) — rebuilds the active world's VaultChunk/
    EntityRelation rows from its configured obsidian_vault_path. Returns
    sync_vault's own summary dict straight through; a missing/unset/
    unreadable vault path comes back as a 400 with a plain message
    rather than a stack trace, since "I haven't set one up yet" or "I
    typo'd the path" are the expected first things a GM hits here, not
    exceptional server errors."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    try:
        return await _vault_sync.sync_vault(db, world)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/knowledge/suggest-relations")
async def api_knowledge_suggest_relations(
    request: Request, db=Depends(get_db), active_world: Optional[str] = Cookie(None),
):
    """GM-only — runs an AI extraction pass over the active world's vault
    notes looking for entity relationships their prose states but the
    [[wikilink]]/frontmatter pass sync_vault already does doesn't capture
    (see app.vault_sync.suggest_relations_for_vault's own docstring for the
    matching/anchoring rules). Returned for GM review — a
    {"suggestions": [...]} list, each item {source_id, source_name,
    target_id, target_name, relation, source_path} — and, like
    /api/facts/parse, writes nothing to the database; POST
    /api/knowledge/relations/bulk is the only thing that does that, once a
    GM has picked which ones to keep. Optional body {"model": "..."} picks
    a non-default model for the extraction, same convention every other AI
    route in this app uses."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    raw = await request.body()
    try:
        model = str((_json.loads(raw).get("model", "") if raw else "")).strip()
    except ValueError:
        model = ""  # a non-JSON body picks the default model, not a 500
    try:
        suggestions = await _vault_sync.suggest_relations_for_vault(db, world, model=model)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"suggestions": suggestions}


@router.post("/api/knowledge/relations/bulk")
async def api_knowledge_relations_bulk(
    request: Request, db=Depends(get_db), active_world: Optional[str] = Cookie(None),
):
    """GM-only — the relation-suggestion review panel's Confirm & Save
    action (mirrors /api/facts/bulk). Body {"relations": [{"source_id",
    "target_id", "relation", ...}, ...]} — the GM-edited/trimmed subset of
    what /api/knowledge/suggest-relations returned. Both ids are verified
    to belong to an Entity in the ACTIVE world before insert (the client's
    list is otherwise trusted, same as facts bulk-save, but ids specifically
    must not be trustable to link entities across worlds). Duplicates
    (an identical source/target/relation already confirmed, or repeated
    within this same payload — both compared case-insensitively on the
    relation label) are skipped, not re-inserted. Response is
    {"created", "skipped_duplicates"}.

    Inserted with source_path=None regardless of what the suggestion's own
    source_path was: once a GM confirms a suggested edge it's a GM-asserted
    fact, same trust tier as the hand-curated entity_links table, so the
    NEXT vault resync (which wipes every EntityRelation row WITH a
    source_path — see sync_vault) must not delete it. The tradeoff is losing
    "which vault note this came from" after confirmation, same as
    entity_links already has no provenance either."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    items = body.get("relations")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, '"relations" must be a non-empty list')

    existing = {
        (r.source_id, r.target_id, r.relation.strip().lower())
        for r in db.query(EntityRelation.source_id, EntityRelation.target_id, EntityRelation.relation)
                    .filter(EntityRelation.world_id == world.id)
    }
    world_entity_ids = {
        eid for (eid,) in db.query(Entity.id).filter(Entity.world_id == world.id).all()
    }
    created = 0
    skipped_duplicates = 0
    seen: set[tuple] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            source_id = int(item.get("source_id"))
            target_id = int(item.get("target_id"))
        except (TypeError, ValueError):
            continue
        relation = str(item.get("relation") or "").strip()
        if not relation or source_id == target_id:
            continue
        if source_id not in world_entity_ids or target_id not in world_entity_ids:
            continue
        key = (source_id, target_id, relation.lower())
        if key in existing or key in seen:
            skipped_duplicates += 1
            continue
        seen.add(key)
        db.add(EntityRelation(
            world_id=world.id, source_id=source_id, target_id=target_id,
            relation=relation, source_path=None,
        ))
        created += 1
    db.commit()
    return {"created": created, "skipped_duplicates": skipped_duplicates}
