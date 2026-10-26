"""Auto-tag mass mode + Player Cockpit (server side). Exact-match asserts."""
import re

# ── 1. ai.py — auto-tag scope:"all" (mass-tag the whole world) ───────────────
p = 'app/routers/ai.py'
src = open(p, encoding='utf-8').read()

def rep(old, new, tag):
    global src
    if new in src:
        print("SKIP", tag)
        return
    assert src.count(old) == 1, tag + " count=" + str(src.count(old))
    src = src.replace(old, new, 1)
    print("OK  ", tag)

rep('''    body = await request.json()
    ids = body.get("entity_ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "Select at least one entity first")
    try:
        ids = [int(i) for i in ids][:500]
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid entity ids")
    overwrite = bool(body.get("overwrite"))
    think = bool(body.get("think", True))

    query = db.query(Entity).filter(Entity.world_id == world.id, Entity.id.in_(ids))
    if not overwrite:
        query = query.filter((Entity.tags.is_(None)) | (Entity.tags == ""))
    ents = query.order_by(Entity.id).all()
    if not ents:
        raise HTTPException(400, "Nothing to tag — the selected entities already have tags.")

    job_id = _AUTO_TAG_SEQ[0] + 1
    _AUTO_TAG_SEQ[0] = job_id
    _AUTO_TAG_JOBS[job_id] = {"status": "running", "started": _time.time(),
                              "processed": 0, "total": len(ents), "tagged": 0,
                              "error": ""}
    done = [j for j, v in _AUTO_TAG_JOBS.items() if v["status"] != "running"]
    while len(done) > 12:
        _AUTO_TAG_JOBS.pop(done.pop(0), None)

    import asyncio as _asyncio
    _asyncio.get_running_loop().create_task(_auto_tag_task(
        job_id, world.id, [e.id for e in ents], overwrite, think))
    return {"job_id": job_id, "total": len(ents), "status": "running"}''',
'''    body = await request.json()
    overwrite = bool(body.get("overwrite"))
    think = bool(body.get("think", True))
    scope = str(body.get("scope") or "selection")
    if scope not in ("selection", "all"):
        raise HTTPException(400, "scope must be 'selection' or 'all'")

    if scope == "all":
        # Mass mode: every entity in the world (notes are entities — kind
        # "note"), untagged-only unless overwrite. Batched downstream, so
        # hundreds of entities just take proportionally longer.
        query = db.query(Entity).filter(Entity.world_id == world.id)
        if not overwrite:
            query = query.filter((Entity.tags.is_(None)) | (Entity.tags == ""))
        ents = query.order_by(Entity.id).all()
    else:
        ids = body.get("entity_ids")
        if not isinstance(ids, list) or not ids:
            raise HTTPException(400, "Select at least one entity first")
        try:
            ids = [int(i) for i in ids][:500]
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid entity ids")
        query = db.query(Entity).filter(Entity.world_id == world.id, Entity.id.in_(ids))
        if not overwrite:
            query = query.filter((Entity.tags.is_(None)) | (Entity.tags == ""))
        ents = query.order_by(Entity.id).all()
    if not ents:
        raise HTTPException(
            400, "Nothing to tag — the selected entities already have tags."
            if scope == "selection" else
            "Nothing to tag — every entity in this world already has tags (enable Replace to re-tag).")

    job_id = _AUTO_TAG_SEQ[0] + 1
    _AUTO_TAG_SEQ[0] = job_id
    _AUTO_TAG_JOBS[job_id] = {"status": "running", "started": _time.time(),
                              "processed": 0, "total": len(ents), "tagged": 0,
                              "error": ""}
    done = [j for j, v in _AUTO_TAG_JOBS.items() if v["status"] != "running"]
    while len(done) > 12:
        _AUTO_TAG_JOBS.pop(done.pop(0), None)

    import asyncio as _asyncio
    _asyncio.get_running_loop().create_task(_auto_tag_task(
        job_id, world.id, [e.id for e in ents], overwrite, think))
    return {"job_id": job_id, "total": len(ents), "status": "running"}''',
    "ai.py: auto-tag scope all")

# ── 2. cockpit.py — player cockpit: adaptive route + player board ────────────
p = 'app/routers/cockpit.py'
src = open(p, encoding='utf-8').read()

rep('''from ..database import get_db
from ..deps import get_world_ctx
from ..models import Party, Schematic''',
'''from ..database import get_db
from ..deps import get_world_ctx
from ..models import Party, PlayerCharacter, Quest, Schematic
from .parties import _member_vitals''',
    "cockpit imports")

rep('''@router.get("/cockpit", response_class=HTMLResponse)
def cockpit(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
''',
'''@router.get("/cockpit", response_class=HTMLResponse)
def cockpit(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        return _player_cockpit(request, db, world, worlds)
''',
    "cockpit route adapts by role")

player_board = '''

# ── Player Cockpit ───────────────────────────────────────────────────────────
# The GM cockpit route adapts: a non-GM member gets the same window
# workspace rendered in player mode (cockpit.html hands cockpit.js a
# CK_PLAYER_MODE flag) — panels tailored to what players can already see,
# data from player-safe endpoints only. Player workspaces persist in
# localStorage (the server workspace routes stay GM-only: World.
# cockpit_ws_json is ONE shared blob per world, and a player writing it
# would stomp the GM's layout).

def _player_parties(db: Session, world, user) -> list:
    """Parties the player's own PCs belong to, with member vitals — the
    same strip the party detail page shows viewers with parties
    visibility."""
    my_pc_ids = [pc.id for pc in db.query(PlayerCharacter)
                 .filter(PlayerCharacter.world_id == world.id,
                         PlayerCharacter.owner_user_id == user.id).all()]
    if not my_pc_ids:
        return []
    out = []
    for p in db.query(Party).filter(Party.world_id == world.id).order_by(Party.name).all():
        try:
            member_ids = json.loads(p.member_pc_ids_json or "[]")
        except ValueError:
            continue
        hits = [i for i in my_pc_ids if i in member_ids]
        if not hits:
            continue
        members = (db.query(PlayerCharacter)
                   .filter(PlayerCharacter.id.in_(member_ids)).all())
        out.append({"id": p.id, "name": p.name,
                    "members": _member_vitals(db, members)})
    return out


def _player_quests(db: Session, world) -> list:
    """Active top-level quests the world shows players (visible_to_players),
    party names attached — the player-mode counterpart of quests.py's GM
    board."""
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


def _player_cockpit(request: Request, db: Session, world, worlds):
    """Render the cockpit shell in player mode: player-safe panel types,
    the player's parties (with vitals) and visible quests as picker data,
    localStorage-only persistence."""
    maps = (db.query(Schematic)
            .filter(Schematic.world_id == world.id, Schematic.is_html.is_(False))
            .order_by(Schematic.name)
            .all())
    parties = _player_parties(db, world, getattr(request.state, "user", None))
    return templates.TemplateResponse("cockpit.html", {
        "request": request, "world": world, "worlds": worlds,
        "maps_json": [{"slug": s.slug, "name": s.name} for s in maps],
        "world_maps_json": _world_maps(world.id),
        "parties_json": parties,
        "player_mode": True,
    })


@router.get("/api/cockpit/player-board")
async def cockpit_player_board(request: Request, db: Session = Depends(get_db),
                               active_world: str = Cookie(None)):
    """Live data for the player cockpit's party-vitals and quests panels:
    the player's parties (member vitals included) and the world's
    player-visible active quests. Player-safe (the /api/cockpit/player-*
    allowlist entries); visibility enforced here — hidden quests never
    leave the server."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    return {
        "parties": _player_parties(db, world, user),
        "quests": _player_quests(db, world),
    }'''
rep('''    return templates.TemplateResponse("cockpit.html", {
        "request": request, "world": world, "worlds": worlds,
        "maps_json": [{"slug": s.slug, "name": s.name} for s in maps],
        "world_maps_json": _world_maps(world.id),
        "parties_json": [{"id": p.id, "name": p.name} for p in parties],
    })''',
'''    return templates.TemplateResponse("cockpit.html", {
        "request": request, "world": world, "worlds": worlds,
        "maps_json": [{"slug": s.slug, "name": s.name} for s in maps],
        "world_maps_json": _world_maps(world.id),
        "parties_json": [{"id": p.id, "name": p.name} for p in parties],
    })
''' + player_board,
    "cockpit: player board route")
open(p, 'w', encoding='utf-8', newline='\n').write(src)

# ── 3. main.py — player-safe allowlist entries ───────────────────────────────
p = 'app/main.py'
src = open(p, encoding='utf-8').read()
rep('''    if path == "/api/live":
        # Live-sync change counter (app/live.py). Payload is a bare integer''',
'''    if path == "/cockpit":
        # The cockpit route adapts by role: GMs get the full workspace,
        # players get a player-mode shell whose data comes from player-safe
        # endpoints only (visibility enforced per panel). Saving workspaces
        # stays GM-only (the workspace routes are NOT allowlisted here).
        return True
    if path == "/api/cockpit/player-board":
        # Player cockpit's live data: the caller's own parties (vitals) and
        # player-visible quests. Handler enforces visibility per row.
        return True
    if path == "/api/live":
        # Live-sync change counter (app/live.py). Payload is a bare integer''',
    "main: player cockpit allowlist")
open(p, 'w', encoding='utf-8', newline='\n').write(src)

# ── 4. cockpit.html — player mode flag ──────────────────────────────────────
p = 'app/templates/cockpit.html'
src = open(p, encoding='utf-8').read()
rep("""const CK_WORLD = {{ world.slug | tojson }};""",
    """const CK_WORLD = {{ world.slug | tojson }};
const CK_PLAYER_MODE = {{ 'true' if player_mode else 'false' }};""",
    "html: CK_PLAYER_MODE")
open(p, 'w', encoding='utf-8', newline='\n').write(src)
print("PART A DONE")
