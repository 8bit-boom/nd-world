"""Hybrid RAG + knowledge-graph pipeline, the one HTTP surface it needs:
triggering a vault rebuild (see app.vault_sync.sync_vault). Everything
else — World.obsidian_vault_path itself, and the query-time vector_search/
graph_context that consult what this produces — is plumbed through the
existing world-settings form and app.retrieval respectively; this router
exists only because "parse the vault right now" is an action, not a
setting, and every other action-triggering route in this app already
follows the same one-router-per-feature layout (see AGENTS.md)."""
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from typing import Optional

from .. import vault_sync as _vault_sync
from ..database import get_db
from ..deps import get_world_ctx

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
