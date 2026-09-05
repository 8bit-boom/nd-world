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
import json as _json
import random as _random

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import ai as _ai_module
from . import auth
from . import retrieval as _retrieval
from . import rules_render
from .constants import KINDS
from .database import SessionLocal
from .deps import load_custom_kinds
from .models import Entity, Fact, GameSession, Quest, RandomTable, World, entity_player_access
from .routers.chronicler import build_chronicler_system_prompt, visible_facts

mcp = FastMCP(
    name="nd-world",
    # The MCP SDK's own DNS-rebinding Host-header check is redundant with
    # (and, unconfigured, stricter than) this app's existing TrustedHostMiddleware
    # / ND_ALLOWED_HOSTS — self-hosted the same way, behind whatever hostname
    # the GM deploys under, so the outer middleware already covers this.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions="Tools for a Neon & Dragons GM toolkit world: log session facts, "
    "search/read/create/edit/delete entities (NPCs, locations, organizations, items, "
    "notes, ...), read the world rules, list sessions and their recaps, list and roll "
    "random tables, manage quests, and ask the Chronicler about campaign history. "
    "Write tools (and GM-secret reads) require a GM token; a player's token only ever "
    "sees what the player could see in the web UI.",
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


# ── Entities (full CRUD — an AI agent can audit, draft, and edit world
# content the same way the web UI's forms do) ─────────────────────────────


def _world_entity_kinds(db, world: World) -> list[str]:
    """The kinds valid for entity creation in this world: the app's fixed
    KINDS plus the world's own custom kinds (deps.load_custom_kinds — the
    same parser the entity form's kind picker uses)."""
    kinds = list(KINDS)
    for k in load_custom_kinds(world):
        if k.get("id") and k["id"] not in kinds:
            kinds.append(k["id"])
    return kinds


def _entity_visible_to(db, entity: Entity, user) -> bool:
    """The web UI's _filter_visible_entities boundary, expressed for one
    row: GM sees everything; anyone else needs visible_to_players plus an
    optional per-player grant (entity_player_access — same join the UI's
    'specific players' visibility mode uses)."""
    if user.is_gm:
        return True
    if not entity.visible_to_players:
        grant = db.query(entity_player_access).filter_by(
            entity_id=entity.id, user_id=user.id,
        ).first()
        return grant is not None
    return True


@mcp.tool()
def get_entity(ctx: Context, entity_id: int) -> dict:
    """Read one entity in full (name, kind, summary, full Markdown body,
    tags, folder, visibility) — a player's token only gets entities they
    could open in the web UI."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        e = db.get(Entity, entity_id)
        if not e:
            raise ValueError(f"Entity {entity_id} not found")
        _load_world(db, e.world_id, user)
        if not _entity_visible_to(db, e, user):
            raise PermissionError(f"Entity {entity_id} is not visible to this token")
        return {
            "id": e.id, "kind": e.kind, "subtype": e.subtype, "name": e.name,
            "summary": e.summary, "body": e.body, "tags": e.tags, "folder": e.folder,
            "visible_to_players": e.visible_to_players, "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        }
    finally:
        db.close()


@mcp.tool()
def create_entity(
    ctx: Context, world_id: int, kind: str, name: str, summary: str = "",
    body: str = "", subtype: str = "", tags: str = "", folder: str = "",
    visible_to_players: bool = True,
) -> dict:
    """Create a world entity (GM-only) — an NPC, location, organization,
    creature, item, event, note, race, profession, feat, or one of the
    world's custom kinds. `body` is Markdown; `tags` is a comma-separated
    string. Returns the created row."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        world = _load_world(db, world_id, user)
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        valid = _world_entity_kinds(db, world)
        if kind not in valid:
            raise ValueError(f"kind must be one of: {', '.join(valid)}")
        e = Entity(
            world_id=world.id, kind=kind, name=name,
            subtype=subtype.strip(), summary=summary.strip(), body=body,
            tags=tags.strip(), folder=folder.strip(),
            visible_to_players=visible_to_players,
        )
        db.add(e)
        db.commit()
        db.refresh(e)
        return {"id": e.id, "kind": e.kind, "name": e.name}
    finally:
        db.close()


@mcp.tool()
def create_note(ctx: Context, world_id: int, name: str, body: str, summary: str = "",
                visible_to_players: bool = True) -> dict:
    """Create a lore note (GM-only) — an entity of kind 'note': a document
    of world lore, research, or reference material in Markdown."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        world = _load_world(db, world_id, user)
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        e = Entity(
            world_id=world.id, kind="note", name=name,
            summary=(summary or body[:120]).strip(), body=body,
            visible_to_players=visible_to_players,
        )
        db.add(e)
        db.commit()
        db.refresh(e)
        return {"id": e.id, "name": e.name}
    finally:
        db.close()


@mcp.tool()
def update_entity(
    ctx: Context, entity_id: int, name: Optional[str] = None,
    summary: Optional[str] = None, body: Optional[str] = None,
    subtype: Optional[str] = None, tags: Optional[str] = None,
    folder: Optional[str] = None, visible_to_players: Optional[bool] = None,
) -> dict:
    """Edit an existing entity's fields (GM-only) — only the fields you
    pass change. Editing `body` replaces the whole Markdown body."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        e = db.get(Entity, entity_id)
        if not e:
            raise ValueError(f"Entity {entity_id} not found")
        _load_world(db, e.world_id, user)
        if name is not None and name.strip():
            e.name = name.strip()
        if summary is not None:
            e.summary = summary.strip()
        if body is not None:
            e.body = body
        if subtype is not None:
            e.subtype = subtype.strip()
        if tags is not None:
            e.tags = tags.strip()
        if folder is not None:
            e.folder = folder.strip()
        if visible_to_players is not None:
            e.visible_to_players = visible_to_players
        db.commit()
        return {"id": e.id, "name": e.name, "updated": True}
    finally:
        db.close()


@mcp.tool()
def delete_entity(ctx: Context, entity_id: int) -> dict:
    """Delete an entity (GM-only)."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        e = db.get(Entity, entity_id)
        if not e:
            raise ValueError(f"Entity {entity_id} not found")
        _load_world(db, e.world_id, user)
        db.delete(e)
        db.commit()
        return {"deleted": entity_id}
    finally:
        db.close()


# ── Rules / sessions / tables / quests ─────────────────────────────────────


def _player_rules_md(md: str) -> str:
    """Rules markdown with GM-only :::gm blocks removed — the text-level
    twin of rules_render's server-side removal for non-GM viewers (see
    rules_render._render_callout's gm branch). Callout text stays: notes/
    tips/warnings are player-visible on the rules page too."""
    try:
        skeleton, blocks = rules_render.extract_blocks(md)
    except Exception:
        return md
    gm_sentinels = {b.get("sentinel") for b in blocks if b.get("type") == "gm"}
    lines = [l for l in skeleton.splitlines() if l.strip() not in gm_sentinels]
    return "\n".join(lines)


@mcp.tool()
def get_rules(ctx: Context, world_id: int) -> dict:
    """Read the world's rules document (Markdown). A GM token gets the full
    source including :::gm secrets; a player token gets the same document
    with GM-only blocks removed, matching the /rules page."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        world = _load_world(db, world_id, user)
        md = world.rules_md or ""
        if not md.strip():
            return {"rules_md": "", "note": "This world has no custom rules — the app's bundled core rules apply."}
        if not user.is_gm:
            md = _player_rules_md(md)
        return {"rules_md": md, "length": len(md)}
    finally:
        db.close()


@mcp.tool()
def list_sessions(ctx: Context, world_id: int) -> list[dict]:
    """List play sessions (newest first): number, title, date, and — for a
    GM token — whether a player-facing recap is published."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        world = _load_world(db, world_id, user)
        q = db.query(GameSession).filter(GameSession.world_id == world.id)
        out = []
        for gs in q.order_by(GameSession.session_num.desc()).all():
            row = {
                "id": gs.id, "session_num": gs.session_num, "title": gs.title,
                "session_date": gs.session_date,
            }
            if user.is_gm:
                row["recap_published"] = bool(
                    gs.player_summary_published and (gs.player_summary or "").strip()
                )
            out.append(row)
        return out
    finally:
        db.close()


@mcp.tool()
def get_session(ctx: Context, session_id: int) -> dict:
    """Read one session. A GM token gets the GM summary/recap; a player
    token gets only the PUBLISHED player summary (or a note that none is
    published yet) — the same publish-model boundary as the Session Log
    page."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        gs = db.get(GameSession, session_id)
        if not gs:
            raise ValueError(f"Session {session_id} not found")
        _load_world(db, gs.world_id, user)
        base = {"id": gs.id, "session_num": gs.session_num, "title": gs.title, "session_date": gs.session_date}
        if user.is_gm:
            base["summary"] = gs.summary or ""
            base["player_summary_published"] = bool(gs.player_summary_published)
            if gs.player_summary_published:
                base["player_summary"] = gs.player_summary or ""
            return base
        published = bool(gs.player_summary_published and (gs.player_summary or "").strip())
        base["player_summary"] = gs.player_summary if published else ""
        base["note"] = "" if published else "No recap has been published for this session yet."
        return base
    finally:
        db.close()


@mcp.tool()
def list_tables(ctx: Context, world_id: int) -> list[dict]:
    """List random tables available to this world (its own plus the
    built-in library), with entry counts (GM-only)."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        world = _load_world(db, world_id, user)
        tables = (
            db.query(RandomTable)
            .filter((RandomTable.world_id.is_(None)) | (RandomTable.world_id == world.id))
            .order_by(RandomTable.name)
            .all()
        )
        import json as _json
        return [
            {"id": t.id, "name": t.name, "category": t.category, "description": t.description,
             "entry_count": len(_json.loads(t.entries_json or "[]"))}
            for t in tables
        ]
    finally:
        db.close()


@mcp.tool()
def roll_table(ctx: Context, table_id: int, times: int = 1) -> dict:
    """Roll a random table (GM-only) — weighted, same mechanics as the web
    UI's Roll button. `times` (1-10) rolls several at once."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        table = db.get(RandomTable, table_id)
        if not table:
            raise ValueError(f"Table {table_id} not found")
        if table.world_id is not None:
            # A world-owned table — verify the token reaches that world
            # (trivially true for the GM tokens that get this far, but the
            # check keeps the world-scoping explicit and future-proof).
            _load_world(db, table.world_id, user)
        times = max(1, min(10, int(times)))
        entries = _json.loads(table.entries_json or "[]")
        if not entries:
            raise ValueError("This table has no entries")
        weights = [max(0, int(e.get("weight", 1) or 1)) for e in entries]
        if sum(weights) == 0:
            weights = [1] * len(entries)
        picks = _random.choices(entries, weights=weights, k=times)
        return {
            "table": table.name,
            "results": [p.get("label", "") for p in picks],
        }
    finally:
        db.close()


@mcp.tool()
def create_quest(
    ctx: Context, world_id: int, title: str, summary: str = "", body: str = "",
    status: str = "active", category: str = "main", visible_to_players: bool = True,
) -> dict:
    """Create a quest (GM-only) — a plot thread with status (active/
    complete/failed/secret — freeform), category (main/side/personal), and
    optional Markdown body."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        world = _load_world(db, world_id, user)
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        q = Quest(
            world_id=world.id, title=title, summary=summary.strip(), body=body,
            status=status.strip() or "active", category=category.strip() or "main",
            visible_to_players=visible_to_players,
        )
        db.add(q)
        db.commit()
        db.refresh(q)
        return {"id": q.id, "title": q.title, "status": q.status}
    finally:
        db.close()


@mcp.tool()
def update_quest(
    ctx: Context, quest_id: int, title: Optional[str] = None,
    summary: Optional[str] = None, body: Optional[str] = None,
    status: Optional[str] = None, category: Optional[str] = None,
    visible_to_players: Optional[bool] = None,
) -> dict:
    """Edit an existing quest's fields (GM-only) — only the fields you pass
    change."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        q = db.get(Quest, quest_id)
        if not q:
            raise ValueError(f"Quest {quest_id} not found")
        _load_world(db, q.world_id, user)
        if title is not None and title.strip():
            q.title = title.strip()
        if summary is not None:
            q.summary = summary.strip()
        if body is not None:
            q.body = body
        if status is not None and status.strip():
            q.status = status.strip()
        if category is not None and category.strip():
            q.category = category.strip()
        if visible_to_players is not None:
            q.visible_to_players = visible_to_players
        db.commit()
        return {"id": q.id, "title": q.title, "status": q.status, "updated": True}
    finally:
        db.close()
