"""MCP server exposing this world's Facts/Chronicler/Quest data to an MCP
client (e.g. a phone's Claude app), so a GM can log facts and ask the
Chronicler from a normal chat conversation, not just the web UI.

Authenticated by a bearer token (ApiToken, see app/auth.py), not the session
cookie — see app/main.py's auth_gate middleware, which resolves the token to
a User and sets request.state.user for /mcp exactly like it does from the
session cookie for every other route. Tools read that same request.state.user
via ctx.request_context.request (the raw Starlette request for this call,
threaded through by the streamable-http transport), so every tool enforces
the identical GM/player boundary as the web UI — a player's token can never
do more than the player already could there.

Mounted stateless (stateless_http=True) — each request is already fully
scoped by the bearer token plus explicit world_id/fact_id arguments, so no
server-side MCP session needs to persist between calls.
"""
from datetime import datetime
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import ai as _ai_module
from . import auth
from . import retrieval as _retrieval
from .database import SessionLocal
from .models import Entity, Fact, GameSession, Quest, World
from .routers.chronicler import build_chronicler_system_prompt, visible_facts

mcp = FastMCP(
    name="nd-world",
    # The MCP SDK's own DNS-rebinding Host-header check is redundant with
    # (and, unconfigured, stricter than) this app's existing TrustedHostMiddleware
    # / ND_ALLOWED_HOSTS — self-hosted the same way, behind whatever hostname
    # the GM deploys under, so the outer middleware already covers this.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions="Tools for a Neon & Dragons GM toolkit world: log session facts, "
    "search entities, list quests, and ask the Chronicler about campaign history. "
    "GM-only tools (create/update/delete facts) reject a player's token.",
    stateless_http=True,
)


def _current_user(ctx: Context):
    request = ctx.request_context.request
    user = getattr(request.state, "user", None) if request else None
    if not user:
        raise PermissionError("No authenticated user for this token")
    return user


def _load_world(db, world_id: int, user) -> World:
    world = db.get(World, world_id)
    if not world or not auth.user_can_access_world(db, user, world):
        raise PermissionError(f"World {world_id} not found or not accessible to this token")
    return world


def _require_gm(user):
    if not user.is_gm:
        raise PermissionError("This action requires a GM token")


def _bump_recap_content_touch(world) -> None:
    """Advance the world's durable recap-staleness watermark (World.
    recap_content_touch — see its docstring in app/models.py). The MCP fact
    tools call this on deletion, the one fact mutation that leaves no Fact
    row behind for the session-log recap freshness rule to timestamp
    (creates/updates are covered by Fact.created_at/updated_at, which the
    freshness rule reads directly). Without it, a phone-logged fact removal
    would leave every cached player recap mentioning the removed fact
    looking fresh forever — the exact class of MCP-side staleness the
    durable rule exists to close."""
    if world is None:
        return
    world.recap_content_touch = datetime.utcnow()


@mcp.tool()
def list_worlds(ctx: Context) -> list[dict]:
    """List the worlds this token's user can access (all worlds for a GM,
    only worlds they're a member of for a player)."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        ids = auth.accessible_world_ids(db, user)
        q = db.query(World)
        if ids is not None:
            if not ids:
                return []
            q = q.filter(World.id.in_(ids))
        return [{"id": w.id, "name": w.name, "slug": w.slug} for w in q.order_by(World.name).all()]
    finally:
        db.close()


@mcp.tool()
def create_fact(
    ctx: Context, world_id: int, content: str, visible_to_players: bool = True,
    game_session_id: Optional[int] = None,
) -> dict:
    """Log a new fact about what happened in play (GM-only). Set
    visible_to_players=False for GM-only secrets the party hasn't
    discovered yet."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        world = _load_world(db, world_id, user)
        content = content.strip()
        if not content:
            raise ValueError("content must not be empty")
        f = Fact(world_id=world.id, game_session_id=game_session_id, content=content,
                 visible_to_players=visible_to_players, author_id=user.id,
                 # Belt-and-braces alongside the column default — the
                 # session-log recap freshness rule reads this timestamp to
                 # invalidate cached recaps (see app/routers/sessions.py).
                 updated_at=datetime.utcnow())
        db.add(f)
        db.commit()
        db.refresh(f)
        return {"id": f.id, "content": f.content, "visible_to_players": f.visible_to_players}
    finally:
        db.close()


@mcp.tool()
def list_facts(ctx: Context, world_id: int, game_session_id: Optional[int] = None) -> list[dict]:
    """List facts for a world, filtered to what this token's user may see —
    a player token never receives a GM-only (visible_to_players=False) fact."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        world = _load_world(db, world_id, user)
        facts = visible_facts(db, world.id, user)
        if game_session_id is not None:
            facts = [f for f in facts if f.game_session_id == game_session_id]
        return [
            {"id": f.id, "content": f.content, "visible_to_players": f.visible_to_players,
             "game_session_id": f.game_session_id}
            for f in facts
        ]
    finally:
        db.close()


@mcp.tool()
def update_fact(
    ctx: Context, fact_id: int, content: Optional[str] = None,
    visible_to_players: Optional[bool] = None,
) -> dict:
    """Edit an existing fact's content and/or visibility (GM-only)."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        fact = db.get(Fact, fact_id)
        if not fact:
            raise ValueError(f"Fact {fact_id} not found")
        _load_world(db, fact.world_id, user)
        if content is not None and content.strip():
            fact.content = content.strip()
        if visible_to_players is not None:
            fact.visible_to_players = visible_to_players
        fact.updated_at = datetime.utcnow()
        db.commit()
        return {"id": fact.id, "content": fact.content, "visible_to_players": fact.visible_to_players}
    finally:
        db.close()


@mcp.tool()
def delete_fact(ctx: Context, fact_id: int) -> dict:
    """Delete a fact (GM-only)."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        fact = db.get(Fact, fact_id)
        if not fact:
            raise ValueError(f"Fact {fact_id} not found")
        world = _load_world(db, fact.world_id, user)
        db.delete(fact)
        # No Fact row survives a delete to carry a timestamp, so the world's
        # recap watermark records it instead — see _bump_recap_content_touch.
        _bump_recap_content_touch(world)
        db.commit()
        return {"deleted": fact_id}
    finally:
        db.close()


@mcp.tool()
def search_entities(ctx: Context, world_id: int, query: str, kind: Optional[str] = None) -> list[dict]:
    """Keyword search over this world's entities (characters, locations,
    items, etc.), filtered to what this token's user may see. Optionally
    narrow to one kind (e.g. "character", "location")."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        world = _load_world(db, world_id, user)
        entities = _retrieval.find_relevant_entities(db, world.id, query, limit=25, user=user)
        if kind:
            entities = [e for e in entities if e.kind == kind]
        return [
            {"id": e.id, "kind": e.kind, "subtype": e.subtype, "name": e.name, "summary": e.summary}
            for e in entities
        ]
    finally:
        db.close()


@mcp.tool()
def list_quests(ctx: Context, world_id: int, status: Optional[str] = None) -> list[dict]:
    """List quests in a world, filtered to what this token's user may see
    (a player token never sees a GM-only quest)."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        world = _load_world(db, world_id, user)
        q = db.query(Quest).filter(Quest.world_id == world.id)
        if not user.is_gm:
            q = q.filter(Quest.visible_to_players.isnot(False))
        if status:
            q = q.filter(Quest.status == status)
        return [
            {"id": qq.id, "title": qq.title, "status": qq.status, "category": qq.category,
             "summary": qq.summary}
            for qq in q.order_by(Quest.title).all()
        ]
    finally:
        db.close()


@mcp.tool()
async def ask_chronicler(ctx: Context, world_id: int, question: str) -> str:
    """Ask the Chronicler a question about campaign history, using only
    facts/entities this token's user may see — the same filtered chat as
    the web UI's /chronicler page."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        world = _load_world(db, world_id, user)
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")
        system = build_chronicler_system_prompt(db, world.id, question, user)
        return await _ai_module.generate_chat([{"role": "user", "content": question}], system=system)
    finally:
        db.close()
