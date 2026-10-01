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
from . import live as _live
from . import retrieval as _retrieval
from . import rendering as _rendering
from . import rules_render
from .constants import KINDS
from .database import SessionLocal
from .deps import load_custom_kinds, world_can_view_section
from .models import (
    Entity, EntityRelation, Fact, GameSession, Party, PlayerCharacter, Quest, RandomTable, World, entity_player_access,
)
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
    "Player characters and parties are system-aware (Neon & Dragons, Hunt in the Moonlight, "
    "Asterion, GM-made sheets): list_characters/get_character/get_party report each one's own "
    "vital, resources and conditions, and adjust_character_resource, set_character_conditions, "
    "award_character_xp and rest_party change them by that system's rules. "
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


def _require_view_section(ctx: Context, world: World, section_id: str) -> None:
    """The section-permission matrix gate for MCP read tools — the same
    deps.world_can_view_section rule the matching web route enforces, so a
    player/assistant token can't read through the API what the web UI 403s
    (this module's instructions promise "a player's token only ever sees
    what the player could see in the web UI" — that includes Settings →
    Navigation's per-section None/Read/Edit levels). The MCP request's
    state carries .user from the auth wrapper; is_assistant is never set
    on it, so an assistant token conservatively reads at the player tier
    via _role_for_request's getattr default — a GM bypasses via the
    is_gm check inside world_section_level itself."""
    request = ctx.request_context.request
    if not world_can_view_section(request, world, section_id):
        raise PermissionError(
            f"This token's role has no access to the '{section_id}' section of this world"
        )


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
        _require_view_section(ctx, world, "facts")
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
        # Same rule the /kind/{kind} list pages enforce for a non-GM: an
        # explicit kind the caller's role can't view is an error, and a
        # cross-kind search drops every row whose own kind section is shut.
        if kind:
            _require_view_section(ctx, world, f"kind_{kind}")
        entities = _retrieval.find_relevant_entities(db, world.id, query, limit=25, user=user)
        if kind:
            entities = [e for e in entities if e.kind == kind]
        if not user.is_gm:
            entities = [e for e in entities if world_can_view_section(
                ctx.request_context.request, world, f"kind_{e.kind}")]
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
        _require_view_section(ctx, world, "quests")
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
        world = _load_world(db, e.world_id, user)
        # Same kind-section gate the web detail page's _entity_view_gate
        # enforces — a player with kind_{kind} = none must not read the
        # entity through MCP either.
        _require_view_section(ctx, world, f"kind_{e.kind}")
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
        # Entity.id is a plain INTEGER PRIMARY KEY (no AUTOINCREMENT), so
        # SQLite can reuse this id for the next entity created — leaving a
        # stale EntityRelation row behind would silently reattach a GM's
        # confirmed graph edge to an unrelated future entity.
        db.query(EntityRelation).filter(
            (EntityRelation.source_id == entity_id) | (EntityRelation.target_id == entity_id)
        ).delete()
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
        _require_view_section(ctx, world, "rules")
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
        _require_view_section(ctx, world, "sessions")
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
        world = _load_world(db, gs.world_id, user)
        _require_view_section(ctx, world, "sessions")
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


# ── Characters & parties (system-aware) ──────────────────────────────────────
# A player character is a native Neon & Dragons sheet OR a character on a custom
# system (Hunt in the Moonlight, Asterion, a GM's own template) whose numbers live in
# template fields. These tools speak both: each character reports its OWN system's vital,
# resources and conditions, and a sheet is rendered as the same markdown the web exports
# and the AI reviewer use. Access mirrors the web UI: a GM sees everything; a player token
# sees its own characters (plus other owned ones when the world lets players see the
# party), and nothing at all when the Characters / Parties section is closed to players.

def _pc_state(db, pc) -> dict:
    """{system, native, vital, resources, conditions} for one character, via the same code the
    party page and roster use."""
    from .pc_stats import pc_maxima
    from .routers.parties import _member_vitals
    from .sheet_systems import system_label
    v = _member_vitals(db, [pc], resource_limit=None)[0]
    native = v["native"]
    resources = [{"label": t["label"], "current": t["current"], "max": t["max"]}
                 for t in v["resources"] if t["id"] != v["hp_id"]]
    if native:
        m = pc_maxima(pc)
        resources = [{"label": "Shock", "current": pc.shock_current or 0, "max": m["shock"]},
                     {"label": "PP", "current": pc.pp_current or 0, "max": m["pp"]},
                     {"label": "MP", "current": pc.mp_current or 0, "max": m["mp"]}]
    return {
        "system": system_label(pc.sheet_template if pc.sheet_template_id else None, native),
        "native": native,
        "vital": ({"label": v["hp_label"], "current": v["hp"], "max": v["max_hp"], "down": v["down"]}
                  if v["max_hp"] else None),
        "resources": resources,
        "conditions": list(v["conditions"]),
    }


def _character_for(ctx: Context, db, character_id: int, *, manage: bool = False):
    """(user, pc, world) the token may read (or, with manage=True, change); PermissionError otherwise."""
    from .routers.characters import _can_manage_character, _can_view_character
    user = _current_user(ctx)
    pc = db.get(PlayerCharacter, character_id)
    if not pc:
        raise ValueError(f"Character {character_id} not found")
    world = _load_world(db, pc.world_id, user)
    _require_view_section(ctx, world, "characters")
    allowed = _can_manage_character(user, pc) if manage else _can_view_character(db, user, pc, world)
    if not allowed:
        raise PermissionError(f"Character {character_id} not found or not accessible to this token")
    return user, pc, world


@mcp.tool()
def list_characters(ctx: Context, world_id: int) -> list[dict]:
    """The world's player characters, each with its system and live state (vital such as
    HP / Health / Flesh, other resources, conditions). A GM token sees all of them; a player
    token sees its own (and other owned characters when the world lets players see the party)."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        world = _load_world(db, world_id, user)
        _require_view_section(ctx, world, "characters")
        q = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world.id)
        if not user.is_gm:
            q = q.filter(PlayerCharacter.owner_user_id.isnot(None) if world.players_see_party
                         else PlayerCharacter.owner_user_id == user.id)
        out = []
        for pc in q.order_by(PlayerCharacter.name).all():
            out.append({"id": pc.id, "name": pc.name, "player_name": pc.player_name or "",
                        "mine": pc.owner_user_id == user.id, **_pc_state(db, pc)})
        return out
    finally:
        db.close()


@mcp.tool()
def get_character(ctx: Context, character_id: int) -> dict:
    """One character in full: its system, live state and the whole sheet as markdown (every
    section of its own system — resources as saved, conditions, abilities, gear, backstory).
    `[gmonly]` blocks are removed for a player token."""
    from .routers.characters import _pc_to_markdown
    db = SessionLocal()
    try:
        user, pc, world = _character_for(ctx, db, character_id)
        sheet = _pc_to_markdown(pc, db)
        if not user.is_gm:
            sheet = _rendering.strip_gm_only(sheet)
        return {"id": pc.id, "name": pc.name, "world_id": world.id, **_pc_state(db, pc), "sheet": sheet}
    finally:
        db.close()


@mcp.tool()
def adjust_character_resource(ctx: Context, character_id: int, resource: str,
                              delta: Optional[int] = None, value: Optional[int] = None) -> dict:
    """Change one resource of a character (GM or the character's owner): pass `delta` (e.g. -2) or
    `value`, exactly one. `resource` is hp / shock / pp / mp for a native Neon & Dragons sheet, or a
    track's name or id for a custom system (Health, Stamina, Hunger, Flesh, Ichor, ...). The result is
    clamped to 0..max. Returns the character's state afterwards."""
    from .pc_stats import pc_maxima
    from .sheet_systems import adjust_track, parse_custom_fields
    if (delta is None) == (value is None):
        raise ValueError("Pass exactly one of delta or value")
    db = SessionLocal()
    try:
        user, pc, world = _character_for(ctx, db, character_id, manage=True)
        m = pc_maxima(pc)
        if m["native"]:
            key = (resource or "").strip().lower()
            cols = {"hp": ("current_hp", m["hp"]), "shock": ("shock_current", m["shock"]),
                    "pp": ("pp_current", m["pp"]), "mp": ("mp_current", m["mp"])}
            if key not in cols:
                raise ValueError("A native sheet's resources are: hp, shock, pp, mp")
            col, top = cols[key]
            cur = getattr(pc, col) or 0
            new = int(value) if value is not None else cur + int(delta)
            setattr(pc, col, max(0, min(top, new)) if top > 0 else max(0, new))
        else:
            if pc.sheet_template is None:
                raise ValueError("This character has no sheet template to take resources from")
            cf, _track = adjust_track(pc.sheet_template, parse_custom_fields(pc.custom_fields_json), resource,
                                      delta=delta, value=value)
            pc.custom_fields_json = _json.dumps(cf)
        db.commit()
        _live.touch(world.id)
        return {"id": pc.id, "name": pc.name, **_pc_state(db, pc)}
    finally:
        db.close()


@mcp.tool()
def set_character_conditions(ctx: Context, character_id: int, conditions: list[str]) -> dict:
    """Replace a character's active conditions (GM or the owner), e.g. ["Stunned", "Burning"]. Pass []
    to clear. Free text is accepted; the sheet offers each system's own list as quick chips."""
    from .pc_stats import clean_conditions
    db = SessionLocal()
    try:
        user, pc, world = _character_for(ctx, db, character_id, manage=True)
        pc.conditions_json = _json.dumps(clean_conditions(conditions))
        db.commit()
        _live.touch(world.id)
        return {"id": pc.id, "name": pc.name, **_pc_state(db, pc)}
    finally:
        db.close()


@mcp.tool()
def award_character_xp(ctx: Context, character_id: int, amount: int) -> dict:
    """Award (or, negative, take back) XP to one character — GM only. A native sheet gains XP; a custom
    system's own XP / Glory fields grow (Hunt in the Moonlight: Current + Lifetime XP; Asterion: Glory)."""
    from .sheet_systems import apply_xp_award, parse_custom_fields, system_meta
    db = SessionLocal()
    try:
        user, pc, world = _character_for(ctx, db, character_id, manage=True)
        _require_gm(user)
        amount = int(amount)
        if abs(amount) > 100000:
            raise ValueError("amount out of range")
        tpl = pc.sheet_template if pc.sheet_template_id else None
        if tpl is not None and tpl.sheet_mode == "custom":
            if not system_meta(tpl)["xp"]:
                raise ValueError(f"{tpl.name} has no XP field to award")
            pc.custom_fields_json = _json.dumps(apply_xp_award(tpl, parse_custom_fields(pc.custom_fields_json), amount))
        else:
            pc.xp = max(0, (pc.xp or 0) + amount)
        db.commit()
        _live.touch(world.id)
        return {"id": pc.id, "name": pc.name, "awarded": amount}
    finally:
        db.close()


def _party_for(ctx: Context, db, party_id: int, *, gm_only: bool = False):
    """(user, party, world, members) the token may read (gm_only: a GM token, checked first)."""
    user = _current_user(ctx)
    if gm_only:
        _require_gm(user)
    party = db.get(Party, party_id)
    if not party:
        raise ValueError(f"Party {party_id} not found")
    world = _load_world(db, party.world_id, user)
    _require_view_section(ctx, world, "parties")
    ids = [i for i in _json.loads(party.member_pc_ids_json or "[]") if isinstance(i, int)]
    members = (db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(ids), PlayerCharacter.world_id == world.id)
               .order_by(PlayerCharacter.name).all() if ids else [])
    if not user.is_gm and not world.players_see_party and not any(m.owner_user_id == user.id for m in members):
        raise PermissionError(f"Party {party_id} not found or not accessible to this token")
    return user, party, world, members


@mcp.tool()
def list_parties(ctx: Context, world_id: int) -> list[dict]:
    """The world's parties with member names. A player token sees only parties it is in (unless the
    world lets players see every party)."""
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        world = _load_world(db, world_id, user)
        _require_view_section(ctx, world, "parties")
        mine = {pc.id for pc in db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world.id,
                                                                  PlayerCharacter.owner_user_id == user.id).all()}
        out = []
        for party in db.query(Party).filter(Party.world_id == world.id).order_by(Party.name).all():
            ids = [i for i in _json.loads(party.member_pc_ids_json or "[]") if isinstance(i, int)]
            if not (user.is_gm or world.players_see_party or mine.intersection(ids)):
                continue
            names = [n for (n,) in db.query(PlayerCharacter.name).filter(PlayerCharacter.id.in_(ids)).order_by(PlayerCharacter.name).all()] if ids else []
            out.append({"id": party.id, "name": party.name, "members": names})
        return out
    finally:
        db.close()


@mcp.tool()
def get_party(ctx: Context, party_id: int) -> dict:
    """One party: each member with its system and live state, plus goals and shared loot."""
    db = SessionLocal()
    try:
        user, party, world, members = _party_for(ctx, db, party_id)
        from .party_refs import load_loot
        goals = party.goals or ""
        if not user.is_gm:
            goals = _rendering.strip_gm_only(goals)
        return {
            "id": party.id, "name": party.name, "goals": goals,
            "members": [{"id": pc.id, "name": pc.name, **_pc_state(db, pc)} for pc in members],
            "loot": [{"name": i["name"], "qty": i["qty"], "notes": i["notes"],
                      "claimed_by": [m.name for m in members if m.id in i["claimed_by"]]} for i in load_loot(party)],
        }
    finally:
        db.close()


@mcp.tool()
def rest_party(ctx: Context, party_id: int, kind: str = "long") -> dict:
    """Apply a Rest to every party member, each by THEIR system's rules (GM only): N&D restores half
    PP/MP and all Shock; Hunt in the Moonlight restores Stamina and clears Strain; Asterion's short rest
    restores the Spark Shield (+2 Ichor), its long rest all Flesh and Ichor. `kind` is "short" or "long"."""
    if kind not in ("short", "long"):
        raise ValueError('kind must be "short" or "long"')
    from .routers.parties import apply_party_rest
    db = SessionLocal()
    try:
        user, party, world, _members = _party_for(ctx, db, party_id, gm_only=True)
        applied, _snapshot = apply_party_rest(db, party, kind)
        db.commit()
        _live.touch(world.id)
        return {"party": party.name, "kind": kind, "rested": [a["name"] for a in applied]}
    finally:
        db.close()


# ── Character-sheet templates (GM only) ──────────────────────────────────────
# A custom game system is a sheet template: fields + system hooks (HP track, XP, name/player binds,
# conditions, Rest, pages, roster comparison) + a rules digest. These tools run an AI client's
# proposal through app.template_draft (the same validator the in-app "Draft with AI" uses), so what
# gets stored always loads and works; anything dropped or renamed comes back as `warnings`.

def _template_for(ctx: Context, db, template_id: int):
    """(user, template) a GM token may read or change; PermissionError otherwise."""
    from .models import SheetTemplate
    user = _current_user(ctx)
    _require_gm(user)
    tpl = db.get(SheetTemplate, template_id)
    if not tpl:
        raise ValueError(f"Sheet template {template_id} not found")
    if tpl.world_id is not None:
        _load_world(db, tpl.world_id, user)
    return user, tpl


def _template_summary(tpl) -> dict:
    from .sheet_systems import system_meta, system_spec, template_fields
    spec = system_spec(tpl)
    return {"id": tpl.id, "name": tpl.name, "description": tpl.description or "", "builtin": bool(tpl.is_builtin),
            "sheet_mode": tpl.sheet_mode, "world_id": tpl.world_id, "fields": len(template_fields(tpl)),
            "integration": {"hp": system_meta(tpl)["hp"], "xp": system_meta(tpl)["xp"],
                            "binds": system_meta(tpl)["binds"], "conditions": len(spec.get("conditions") or []),
                            "rest": sorted((spec.get("rest") or {}).keys()), "pages": len(spec.get("pages") or []),
                            "roster": len(spec.get("roster") or [])}}


@mcp.tool()
def list_sheet_templates(ctx: Context, world_id: int) -> list[dict]:
    """The character-sheet templates usable in a world (built-in systems, then the GM's own): id, name,
    sheet_mode ("custom" = a whole game system, "nd" = extra fields on the Neon & Dragons sheet), field
    count and which integrations it has (HP track, XP, name/player binds, conditions, Rest, pages, roster).
    GM token only."""
    from .routers.characters import _templates_for_world
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        world = _load_world(db, world_id, user)
        return [_template_summary(t) for t in _templates_for_world(db, world.id)]
    finally:
        db.close()


@mcp.tool()
def get_sheet_template(ctx: Context, template_id: int) -> dict:
    """One sheet template in full: its fields (id, label, type, section, default_value, options /
    item_fields, and the vital / xp / binds flags), its own system hooks, the effective system hooks
    (a built-in system's table overlaid with its own), and its rules text. GM token only."""
    from .sheet_systems import system_rules_markdown, system_spec, system_spec_public, template_fields
    db = SessionLocal()
    try:
        _user, tpl = _template_for(ctx, db, template_id)
        return {**_template_summary(tpl), "field_list": template_fields(tpl),
                "system": system_spec_public(tpl), "effective_system": _json.loads(_json.dumps(system_spec(tpl))),
                "rules_md": tpl.rules_md or "", "rules": system_rules_markdown(tpl)}
    finally:
        db.close()


def _unique_template_slug(db, name: str) -> str:
    from .models import SheetTemplate
    base = (name.lower().replace(" ", "-"))[:50] or "template"
    slug, n = base, 1
    while db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first():
        slug, n = f"{base}-{n}", n + 1
    return slug


@mcp.tool()
def create_sheet_template(ctx: Context, world_id: int, name: str, fields: list[dict], description: str = "",
                          system: Optional[dict] = None, rules_md: str = "") -> dict:
    """Create a whole custom game system as a character-sheet template in a world (GM token only).

    `fields`: [{id, label, type, section, default_value, ...}] — type is text | textarea | number | select
    (with `options`) | resource (default_value "cur/max") | list (with `item_fields`: [{id,label,type}]).
    Flags: `vital: "hp"` on ONE resource (the 'still standing' track party HP bars and Combat use),
    `xp: true` on number fields that hold experience, `binds: "name" | "player_name"` on a text field
    that is the character's name / the player's name.
    `system` (all optional): {conditions: [names], rest: {short: [ops], long: [ops]} with ops
    ["full"|"empty", resource id], ["add", resource id, n], ["set", field id, value]; pages: [{label, icon,
    sections: [section names]}]; roster: [[title, [field ids to compare across the party]]]}.
    `rules_md`: a markdown digest of the system's rules — the AI character creator and sheet reviews follow it.
    Ids are made safe and unique, impossible flags/references are dropped; the result lists every
    `warnings` change. Returns the template summary."""
    from .models import SheetTemplate
    from .template_draft import clean_template_draft
    db = SessionLocal()
    try:
        user = _current_user(ctx)
        _require_gm(user)
        world = _load_world(db, world_id, user)
        draft, warnings = clean_template_draft({"name": name, "description": description, "fields": fields,
                                                "system": system or {}, "rules_md": rules_md})
        if draft is None:
            raise ValueError("No usable fields: " + "; ".join(warnings[:5]))
        tpl = SheetTemplate(world_id=world.id, name=draft["name"], slug=_unique_template_slug(db, draft["name"]),
                            description=draft["description"], is_builtin=False, sheet_mode="custom",
                            fields_json=_json.dumps(draft["fields"]), system_json=_json.dumps(draft["system"]),
                            rules_md=draft["rules_md"])
        db.add(tpl)
        db.commit()
        db.refresh(tpl)
        return {**_template_summary(tpl), "warnings": warnings}
    finally:
        db.close()


@mcp.tool()
def update_sheet_template(ctx: Context, template_id: int, name: Optional[str] = None,
                          description: Optional[str] = None, fields: Optional[list[dict]] = None,
                          system: Optional[dict] = None, rules_md: Optional[str] = None) -> dict:
    """Change one of the GM's own sheet templates (GM token only; built-in systems are read-only here —
    copy one with create_sheet_template). Pass only what changes; `fields` / `system` / `rules_md` replace
    the stored value wholesale and have the same format as create_sheet_template. Changing only `system`
    is checked against the template's existing fields (their ids are never renamed). Returns the summary
    plus `warnings`."""
    from .sheet_systems import clean_system_spec, system_spec_public, template_fields
    from .template_draft import clean_template_draft
    db = SessionLocal()
    try:
        _user, tpl = _template_for(ctx, db, template_id)
        if tpl.is_builtin:
            raise PermissionError("Built-in templates can't be changed over MCP — copy it with create_sheet_template")
        warnings = []
        new_name = " ".join((name if name is not None else tpl.name).split())
        new_desc = description if description is not None else (tpl.description or "")
        new_rules = (rules_md if rules_md is not None else (tpl.rules_md or ""))
        if fields is not None:
            draft, warnings = clean_template_draft({
                "name": new_name, "description": new_desc, "fields": fields,
                "system": system if system is not None else system_spec_public(tpl), "rules_md": new_rules})
            if draft is None:
                raise ValueError("No usable fields: " + "; ".join(warnings[:5]))
            tpl.fields_json = _json.dumps(draft["fields"])
            tpl.system_json = _json.dumps(draft["system"])
            tpl.name, tpl.description, tpl.rules_md = draft["name"], draft["description"], draft["rules_md"]
        else:
            if not new_name:
                raise ValueError("name can't be empty")
            spec = system if system is not None else system_spec_public(tpl)
            cleaned, warnings = clean_system_spec(spec, template_fields(tpl))
            tpl.system_json = _json.dumps(cleaned)
            tpl.name, tpl.description = new_name[:80], new_desc.replace("<", "").replace(">", "").strip()[:300]
            tpl.rules_md = new_rules.replace("\x00", "").strip()[:20000]
        tpl.updated_at = datetime.utcnow()
        db.commit()
        return {**_template_summary(tpl), "warnings": warnings}
    finally:
        db.close()
