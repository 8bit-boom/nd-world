"""GM Cockpit (Phase 3) — a modular workspace of draggable, resizable
"windows" where the GM keeps everything they'd otherwise open in browser
tabs: the live battle map, dice tray, party vitals, active quests, any
entity's page, scratch notes, AI Chat.

Everything is a panel. Panels are added/removed/resized/rearranged freely
(the Add-panel modal lists the types), and the whole arrangement — plus
named presets like "Combat" / "Exploration" — is saved per world
(World.cockpit_ws_json) through the sanitizing workspace routes below, so
it follows the GM across browsers and devices. Live panels (party vitals,
quests) re-fetch over the Phase-1 live-sync bus; window content is either
an embedded live page (?embed=1 chrome-less mode) or a client-side render
of an existing JSON endpoint — no re-implementations of features.

GM-only by default (no _is_player_safe/_is_assistant_safe entry): it's a
composition of GM-facing tools, and the nav entry (nav_menus.py, id
"cockpit") is gm_only to match.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import ai as _ai
from .. import retrieval as _retrieval
from ..database import get_db, SessionLocal
from ..deps import get_world_ctx
from ..models import Entity, Party, PlayerCharacter, Quest, Schematic
from ..party_refs import member_ids
from ..pc_stats import pc_maxima
from .parties import _member_vitals, _pc_levelup_ready
from ..templating import templates

router = APIRouter()

# The panel types the Add-panel modal offers. Everything else in a posted
# layout is dropped by the sanitizer.
PANEL_TYPES = {"map", "wmap", "dice", "party", "quests", "entity", "ecard",
               "notes", "ai", "ai_chat", "combat", "tables", "calendar",
               "gallery", "audio", "video", "imagestudio", "find", "timer"}

MAX_PANELS = 24          # per layout
MAX_PRESETS = 12         # named layouts per world
MAX_PANELS_BYTES = 128 * 1024  # whole workspace JSON cap

_ACCENT_RE = re.compile(r"^$|^#[0-9a-fA-F]{6}$")


def _clamp_int(v, lo, hi, default=0):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _sanitize_panels(raw_panels) -> list:
    """Round a posted panel list into the exact saved shape — unknown keys
    dropped, types allowlisted, geometry clamped, strings bounded. Raises
    ValueError on structural nonsense (not a list, too many panels)."""
    if not isinstance(raw_panels, list):
        raise ValueError("panels must be a list")
    if len(raw_panels) > MAX_PANELS:
        raise ValueError(f"too many panels (max {MAX_PANELS})")
    out = []
    for p in raw_panels[:MAX_PANELS]:
        if not isinstance(p, dict) or p.get("type") not in PANEL_TYPES:
            continue
        data = p.get("data")
        accent = str(p.get("accent") or "")
        out.append({
            "id": str(p.get("id") or "")[:40],
            "type": p["type"],
            "ref": str(p.get("ref") or "")[:120],
            "title": str(p.get("title") or "")[:120],
            "x": _clamp_int(p.get("x"), 0, 8000),
            "y": _clamp_int(p.get("y"), 0, 8000),
            "w": _clamp_int(p.get("w"), 160, 8000, default=420),
            "h": _clamp_int(p.get("h"), 120, 8000, default=300),
            "z": _clamp_int(p.get("z"), 0, 100000),
            "collapsed": bool(p.get("collapsed")),
            "accent": accent if _ACCENT_RE.match(accent) else "",
            "data": {"text": str((data or {}).get("text") or "")[:8000]}
            if isinstance(data, dict) else {},
        })
    return out


def _sanitize_workspace(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("workspace must be an object")
    presets_raw = raw.get("presets")
    if presets_raw is not None and not isinstance(presets_raw, dict):
        raise ValueError("presets must be an object")
    presets = {}
    for name, layout in list((presets_raw or {}).items())[:MAX_PRESETS]:
        name = str(name).strip()[:40]
        if not name:
            continue
        presets[name] = {"panels": _sanitize_panels((layout or {}).get("panels"))}
    return {
        "current": {"panels": _sanitize_panels((raw.get("current") or {}).get("panels"))},
        "presets": presets,
    }


def _load_workspace(db: Session, world) -> Optional[dict]:
    try:
        ws = json.loads(world.cockpit_ws_json) if world.cockpit_ws_json else None
    except ValueError:
        ws = None
    return ws if isinstance(ws, dict) else None


@router.get("/cockpit", response_class=HTMLResponse)
def cockpit(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)

    # Pickers for the Add-panel modal: floatable maps (non-HTML schematics
    # — HTML-type maps are external files with no embeddable view), the
    # world's file-based maps (the Leaflet viewer at /maps/{slug} — the
    # JSON-marker maps under <DB dir>/maps), and the world's parties.
    # Entities come from the existing /api/entities/picker (fetched on first
    # use of the modal, client-side filtered).
    maps = (
        db.query(Schematic)
        .filter(Schematic.world_id == world.id, Schematic.is_html.is_(False))
        .order_by(Schematic.name)
        .all()
    )
    parties = db.query(Party).filter(Party.world_id == world.id).order_by(Party.name).all()

    return templates.TemplateResponse("cockpit.html", {
        "request": request, "world": world, "worlds": worlds,
        "maps_json": [{"slug": s.slug, "name": s.name} for s in maps],
        "world_maps_json": _world_maps(world.id),
        "parties_json": [{"id": p.id, "name": p.name} for p in parties],
    })


def _maps_dir() -> Path:
    # Same derivation as main.py's _MAPS_DIR (routers can't import main —
    # circular): the maps dir sits beside the SQLite file.
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "maps"


def _world_maps(world_id: int) -> list:
    """The world's file-based (Leaflet) maps for the cockpit's world-map
    picker: [{slug, name, markers}]. Mirrors main.py's _iter_world_maps
    shape tolerantly — malformed/legacy files are skipped, never raised —
    kept local because importing main from a router would be circular."""
    out = []
    maps_dir = _maps_dir()
    if not maps_dir.exists():
        return out
    for jf in sorted(maps_dir.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("world_id", 1) != world_id:
            continue
        markers = data.get("markers")
        out.append({
            "slug": jf.stem,
            "name": str(data.get("name") or jf.stem)[:120],
            "markers": len(markers) if isinstance(markers, list) else 0,
        })
    return out


@router.get("/api/cockpit/workspace")
async def cockpit_get_workspace(request: Request, db: Session = Depends(get_db),
                                active_world: str = Cookie(None)):
    """The saved workspace for the active world (or null if the GM never
    arranged this world's cockpit)."""
    user = getattr(request.state, "user", None)
    if not user or not user.is_gm:
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    return {"workspace": _load_workspace(db, world)}


@router.post("/api/cockpit/workspace")
async def cockpit_save_workspace(request: Request, db: Session = Depends(get_db),
                                 active_world: str = Cookie(None)):
    """Save (and switch/replace) the workspace: current panels plus named
    presets. Sanitized and size-capped — this is GM-authored UI state, not
    free-form storage."""
    user = getattr(request.state, "user", None)
    if not user or not user.is_gm:
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    body = await request.json()
    try:
        ws = _sanitize_workspace(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    dumped = json.dumps(ws)
    if len(dumped) > MAX_PANELS_BYTES:
        raise HTTPException(400, "Workspace too large — trim notes or panels.")
    world.cockpit_ws_json = dumped
    db.commit()
    return {"ok": True}


# ── AI find / connections ────────────────────────────────────────────────────
# "Find using AI" (and an entity card's 🔗 Find-connections seed): thinking +
# RAG are HARDCODED ON for this surface — no toggles, per design. Runs as a
# background job (POST start + GET poll, the quest-sync pattern) because
# thinking + RAG regularly outlives Cloudflare Tunnel's ~100 s no-byte
# timeout; a restart just means re-clicking Find. GM-only via middleware
# (nothing under /api/cockpit/* is in _is_player_safe); the start route
# re-checks is_gm, same as the workspace routes above.
_COCKPIT_FIND_JOBS: dict = {}
_COCKPIT_FIND_SEQ: list = [0]


def _extract_json(text: str) -> dict:
    """Parse the model's reply as JSON, tolerating markdown fences (same
    lenience quests.py's own extractor applies)."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9]*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the model's reply")
    return json.loads(raw[start:end + 1])


@router.post("/api/cockpit/ai/find/start")
async def cockpit_ai_find_start(request: Request, db: Session = Depends(get_db),
                                active_world: str = Cookie(None)):
    """Start an AI find job for the active world: {query} for a free-text
    search, or {entity_id} to find what is connected to that entity (the
    entity card's 🔗 button). Thinking + RAG always on. GM-only."""
    user = getattr(request.state, "user", None)
    if not user or not user.is_gm:
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    body = await request.json()
    query = str(body.get("query") or "").strip()
    entity_id = body.get("entity_id")
    try:
        entity_id = int(entity_id) if entity_id else None
    except (TypeError, ValueError):
        entity_id = None
    if not query and not entity_id:
        raise HTTPException(400, "Type what to look for first — or seed me from an entity card.")
    if not _ai.effective_llm_api_key():
        raise HTTPException(400, "No AI backend configured — set UNSLOTH_API_KEY (Settings → System).")

    job_id = _COCKPIT_FIND_SEQ[0] + 1
    _COCKPIT_FIND_SEQ[0] = job_id
    _COCKPIT_FIND_JOBS[job_id] = {"status": "running", "started": time.time(),
                                  "results": None, "error": ""}
    done = [j for j, v in _COCKPIT_FIND_JOBS.items() if v["status"] != "running"]
    while len(done) > 12:
        _COCKPIT_FIND_JOBS.pop(done.pop(0), None)

    import asyncio as _asyncio
    _asyncio.get_running_loop().create_task(
        _cockpit_find_task(job_id, world.id, query, entity_id))
    return {"job_id": job_id, "status": "running"}


@router.get("/api/cockpit/ai/find/{job_id}")
async def cockpit_ai_find_poll(job_id: int, request: Request):
    """Poll a find job: running (with elapsed seconds), done (enriched
    results — hallucinated ids filtered against the real candidates), or
    error (with the reason). The start route is GM-only and its results
    carry hidden-entity names + GM-lore reasons — the poll re-checks GM
    rather than trusting the auth gate's allowlist to stay closed forever
    (audit 2026-09-30, routers finding 12)."""
    if not (getattr(getattr(request, "state", None), "user", None) or None) or not request.state.user.is_gm:
        raise HTTPException(403)
    job = _COCKPIT_FIND_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown find job")
    if job["status"] == "running":
        return {"status": "running", "elapsed": round(time.time() - job["started"])}
    if job["status"] == "error":
        return {"status": "error", "error": job["error"]}
    return {"status": "done", "results": job["results"]}


async def _cockpit_find_task(job_id: int, world_id: int, query: str,
                             entity_id: Optional[int]):
    """The find worker: RAG retrieves world excerpts (always on), the model
    ranks the candidate list against focus + excerpts with thinking on, and
    hallucinated/duplicate ids are dropped. The blanket except is the
    AI-Build lesson — an unhandled task exception would leave the job stuck
    at "running" forever."""
    db = SessionLocal()
    try:
        material = query
        exclude_ids = {0}
        if entity_id:
            ent = db.get(Entity, entity_id)
            if not ent or ent.world_id != world_id:
                raise ValueError("Focused entity not found in this world")
            exclude_ids.add(ent.id)
            material = (ent.name
                        + ((" — " + ent.summary) if ent.summary else "")
                        + "\n" + (ent.body or "")[:1500])
            if query:
                material = query + "\n\n" + material
        rag = ""
        try:
            rag, _n, _notes = _retrieval.smart_world_context(
                db, world_id, material[:1500], entity_limit=10, notes_limit=4)
            if rag:
                rag = rag[:4000]
        except Exception:
            rag = ""
        cands = (db.query(Entity)
                 .filter(Entity.world_id == world_id,
                         Entity.id.notin_(exclude_ids))
                 .order_by(Entity.name).limit(200).all())
        board = "\n".join(
            f"- id={e.id} [{e.kind or '?'}] {e.name}"
            + (f" — {(e.summary or '')[:100]}" if e.summary else "")
            for e in cands) or "(none)"
        system = (
            "You are a campaign co-GM's research assistant. From the CANDIDATE "
            "list and the RETRIEVED WORLD EXCERPTS, select the entities and "
            "notes most connected to the FOCUS and give each a one-line reason. "
            "Return STRICT JSON only:\n"
            '{"results": [{"id": int, "reason": str}]}\n'
            "Rules: only propose ids that appear verbatim in the candidate list; "
            "3-8 results, most relevant first; reasons are one short in-world "
            "sentence; return an empty list when nothing is genuinely connected. "
            "No comments, no markdown fences."
        )
        user_text = ("=== FOCUS ===\n" + material[:3000]
                     + "\n\n=== RETRIEVED WORLD EXCERPTS ===\n" + (rag or "(none)")
                     + "\n\n=== CANDIDATES ===\n" + board)
        raw = await _ai.generate_chat(
            [{"role": "user", "content": user_text}],
            system=system, model="", think=True,
            format={"type": "object",
                    "properties": {"results": {"type": "array"}},
                    "required": ["results"]},
        )
        parsed = _extract_json(raw)
        by_id = {e.id: e for e in cands}
        results = []
        seen = set()
        for item in (parsed.get("results") or [])[:8]:
            if not isinstance(item, dict):
                continue
            try:
                eid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            e = by_id.get(eid)
            if not e or eid in seen:
                continue  # hallucinated or duplicated id — drop
            seen.add(eid)
            results.append({"id": e.id, "name": e.name, "kind": e.kind or "",
                            "reason": str(item.get("reason") or "")[:300]})
        _COCKPIT_FIND_JOBS[job_id].update(status="done", results=results)
    except Exception as exc:
        _COCKPIT_FIND_JOBS[job_id].update(
            status="error", error=str(exc) or exc.__class__.__name__)
    finally:
        db.close()


# ── Player Cockpit ───────────────────────────────────────────────────────────
# The cockpit route adapts by role: a non-GM member gets the same window
# workspace rendered in player mode (cockpit.html hands cockpit.js a
# CK_PLAYER_MODE flag) — panels tailored to what players can already see,
# data from player-safe endpoints only. Player workspaces persist in
# localStorage: the workspace API routes stay GM-only, because World.
# cockpit_ws_json is ONE shared blob per world and a player saving would
# stomp the GM's layout.

def _viewer_pcs(db: Session, world, user, as_pc: int = 0) -> list:
    """The characters the player cockpit is FOR: the viewer's own — or, for a GM who opened it from a
    character's page (?pc=), that character's owner's characters (just that one if it has no owner), so
    the GM sees the dashboard the way that player does. `as_pc` is ignored for anyone but a GM, and for a
    character of another world."""
    if not user:
        return []
    owner_id = user.id
    if as_pc and user.is_gm:
        target = db.get(PlayerCharacter, as_pc)
        if target is not None and target.world_id == world.id:
            if not target.owner_user_id:
                return [target]
            owner_id = target.owner_user_id
    return (db.query(PlayerCharacter)
            .filter(PlayerCharacter.world_id == world.id, PlayerCharacter.owner_user_id == owner_id)
            .order_by(PlayerCharacter.name).all())


def _player_parties(db: Session, world, user, as_pc: int = 0) -> list:
    """Parties the player's own PCs belong to, with member vitals — the
    same strip the party detail page shows viewers with parties
    visibility. Deliberately NOT gated on the Parties section level: these
    are only parties one of the viewer's OWN characters belongs to (their
    teammates' HP, never hidden companions), which they already know — the
    section toggle governs browsing every party in the world. `as_pc`: see _viewer_pcs (GM only)."""
    my_pc_ids = [pc.id for pc in _viewer_pcs(db, world, user, as_pc)]
    if not my_pc_ids:
        return []
    out = []
    for p in db.query(Party).filter(Party.world_id == world.id).order_by(Party.name).all():
        ids = member_ids(p.member_pc_ids_json)
        if not [i for i in my_pc_ids if i in ids]:
            continue
        members = (db.query(PlayerCharacter)
                   .filter(PlayerCharacter.id.in_(ids)).all())
        out.append({"id": p.id, "name": p.name,
                    "members": _member_vitals(db, members)})
    return out


def _player_quests(db: Session, world) -> list:
    """Active top-level quests the world shows players (visible_to_players),
    party names attached — the player-mode counterpart of quests.py's GM
    board. Deliberately NOT gated on the Quests section level: the cockpit is
    its own player surface, and a GM who flags a quest visible_to_players has
    chosen to show it there even while the /quests browser stays closed (the
    default for players)."""
    quests = (db.query(Quest)
              .filter(Quest.world_id == world.id, Quest.status == "active",
                      Quest.parent_id.is_(None), Quest.visible_to_players.is_(True))
              .order_by(Quest.category, Quest.title)
              .limit(12)
              .all())
    party_names = {p.id: p.name for p in db.query(Party).filter(Party.world_id == world.id).all()}
    return [{"id": q.id, "title": q.title, "category": q.category or "main",
             "summary": q.summary or "", "party_id": q.assigned_party_id,
             "party": party_names.get(q.assigned_party_id)} for q in quests]


def _player_my_pcs(db: Session, world, user, as_pc: int = 0) -> list:
    """The viewer's own PlayerCharacters in this world, with live vitals —
    feeds the Player Cockpit's My Character panel. `as_pc`: see _viewer_pcs (GM only)."""
    pcs = _viewer_pcs(db, world, user, as_pc)
    # Same per-character vitals as the party strip: an N&D sheet reads its columns,
    # a custom system its vital track (no AC, no level-ups) — plus the XP total.
    out = []
    for pc, v in zip(sorted(pcs, key=lambda p: p.name or ""), _member_vitals(db, pcs, resource_limit=None)):
        out.append({**v, "xp": pc.xp})
    return out


def _player_cockpit(request: Request, db: Session, world, worlds, focus_pc: int = 0):
    """Render the cockpit shell in player mode: player-safe panel types
    (cockpit.js gates by CK_PLAYER_MODE), the player's parties (with
    vitals) as picker data, localStorage-only persistence. `focus_pc` (the
    character page's Cockpit tab / button passes ?pc=) is honoured only if it
    is one of the viewer's OWN characters in this world (or, for a GM, any
    character of it: the cockpit then shows that player's parties and
    characters) — the My Character panel leads with that character."""
    maps = (db.query(Schematic)
            .filter(Schematic.world_id == world.id, Schematic.is_html.is_(False))
            .order_by(Schematic.name)
            .all())
    user = getattr(request.state, "user", None)
    parties = _player_parties(db, world, user, focus_pc)
    my_pcs = [{"id": m["id"], "name": m["name"]} for m in _player_my_pcs(db, world, user, focus_pc)]
    focus = focus_pc if any(m["id"] == focus_pc for m in my_pcs) else None
    # A GM opening a character they don't own sees the cockpit through that player's eyes; the page keeps
    # that layout apart from the GM's own saved cockpit (CK_VIEW_AS).
    target = db.get(PlayerCharacter, focus) if focus else None
    view_as = bool(user and user.is_gm and target is not None and target.owner_user_id != user.id)
    return templates.TemplateResponse("cockpit.html", {
        "request": request, "world": world, "worlds": worlds,
        "maps_json": [{"slug": s.slug, "name": s.name} for s in maps],
        "world_maps_json": _world_maps(world.id),
        "parties_json": parties,
        "my_pcs_json": my_pcs,
        "focus_pc_id": focus,
        "view_as": view_as,
        # Closing the full-screen cockpit goes back to where it was opened from: the character's sheet, or
        # the Player Characters list when it was opened without one.
        "exit_href": f"/characters/{focus}" if focus else "/characters",
        "player_mode": True,
    })


@router.get("/player-cockpit", response_class=HTMLResponse)
def player_cockpit_page(request: Request, pc: str = "", db: Session = Depends(get_db),
                        active_world: str = Cookie(None)):
    """The Player Cockpit — a SEPARATE page from the GM cockpit, tailored to
    what the logged-in viewer can access (their parties' vitals, visible
    quests, entity pages, dice, media). Players use it during sessions; a
    GM can open it too to see exactly what their table sees. Renders the
    same window workspace in player mode (CK_PLAYER_MODE)."""
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    return _player_cockpit(request, db, world, worlds, int(pc) if pc.isdigit() else 0)


@router.get("/api/cockpit/player-board")
async def cockpit_player_board(request: Request, pc: str = "", db: Session = Depends(get_db),
                               active_world: str = Cookie(None)):
    """Live data for the player cockpit's party-vitals and quests panels:
    the player's own parties (member vitals included) and the world's
    player-visible active quests. Player-safe via the /cockpit +
    /api/cockpit/player-board allowlist entries; visibility is enforced
    here — hidden quests never leave the server."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    as_pc = int(pc) if pc.isdigit() else 0   # GM only: the board as that character's player sees it
    return {
        "parties": _player_parties(db, world, user, as_pc),
        "quests": _player_quests(db, world),
        "my_pcs": _player_my_pcs(db, world, user, as_pc),
    }