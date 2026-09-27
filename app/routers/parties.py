import json

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx, paginate, world_can_edit_section, world_can_view_section, world_row_visible
from .. import live
from ..models import CalendarEvent, CombatSession, Entity, GameSession, Party, PlayerCharacter, Quest, SheetTemplate, World
from .characters import _levelup_ready as _pc_levelup_ready  # cross-router import, per AGENTS.md
from ..templating import templates
from .combat import entity_to_combatant, pc_to_combatant, _COMBATANT_KINDS

router = APIRouter()


def _can_manage_parties(request: Request, world) -> bool:
    """True for a GM, or an assistant with parties:edit — the tier allowed
    to create/delete parties or manage their structure (name, membership).
    A plain player's own "edit" grant never reaches this far (see
    _party_edit_level's own docstring) — parties aren't something a player
    conjures from nothing, only something they can already be a member
    of."""
    if not world_can_edit_section(request, world, "parties"):
        return False
    user = getattr(request.state, "user", None)
    return bool(user and (user.is_gm or getattr(request.state, "is_assistant", False)))


def _party_edit_level(request: Request, db: Session, world, party: Party) -> str:
    """"none" | "member" | "full" — Parties has no owner column the way
    Quest/CalendarEvent/RandomTable do (a party is a GM-organized grouping,
    not something a player conjures from nothing), so "edit" for a plain
    player means something narrower and different: editing notes/loot on a
    party they're already IN (one of their own PlayerCharacters is a
    member), never creating/deleting parties or changing membership/name.
    "full" (GM, or an assistant with parties:edit) may do anything to ANY
    party, same as world_can_edit_row's rule for the other three sections.
    "member" only ever applies to notes/loot (see party_edit/party_loot)."""
    if _can_manage_parties(request, world):
        return "full"
    if not world_can_edit_section(request, world, "parties"):
        return "none"
    user = getattr(request.state, "user", None)
    if not user:
        return "none"
    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    if not pc_ids:
        return "none"
    owner_ids = {
        row[0] for row in db.query(PlayerCharacter.owner_user_id)
        .filter(PlayerCharacter.id.in_(pc_ids)).all()
    }
    return "member" if user.id in owner_ids else "none"


def _member_vitals(db: Session, member_pcs: list) -> list:
    """The live member-vitals strip, shared by the party detail page, the
    /api/parties/{id}/vitals JSON (live-sync refetches) and the GM Cockpit.
    HP/temp/AC read straight off the PC rows, so they're current the moment
    anyone's sheet changes. Conditions stay on the sheet (freeform JSON).
    System-aware: for members on a custom sheet (Asterion, HITM, ...),
    surface the template's resource tracks (current/max) from the PC's own
    custom fields — Health/Stamina/Hunger for Hunters, Spark Shield/Flesh/
    Ichor for gods, whatever the system defines. Pure-N&D members keep the
    HP/AC strip."""
    member_vitals = []
    _tpl_resource_cache = {}
    for pc in sorted(member_pcs, key=lambda p: p.name or ""):
        try:
            conds = json.loads(pc.conditions_json or "[]")
        except ValueError:
            conds = []
        resources = []
        tpl_id = getattr(pc, "sheet_template_id", None)
        if tpl_id:
            if tpl_id not in _tpl_resource_cache:
                tpl = db.get(SheetTemplate, tpl_id)
                try:
                    fields = json.loads(tpl.fields_json or "[]") if tpl else []
                except ValueError:
                    fields = []
                _tpl_resource_cache[tpl_id] = [
                    f for f in fields if f.get("type") == "resource"
                ]
            try:
                cf = json.loads(pc.custom_fields_json or "{}")
            except ValueError:
                cf = {}
            for f in _tpl_resource_cache[tpl_id][:4]:
                cur = cf.get(f"{f['id']}_current")
                mx = cf.get(f"{f['id']}_max")
                if cur is None and mx is None:
                    continue
                resources.append({
                    "label": f.get("label", f["id"]),
                    "current": cur if cur is not None else 0,
                    "max": mx if mx is not None else 0,
                })
        member_vitals.append({
            "id": pc.id, "name": pc.name,
            "hp": pc.current_hp, "max_hp": pc.max_hp, "temp_hp": pc.temp_hp,
            "ac": pc.armor_class, "level": pc.level,
            "down": (pc.max_hp or 0) > 0 and (pc.current_hp or 0) <= 0,
            "levelup": _pc_levelup_ready(pc),
            "conditions": [c for c in conds if isinstance(c, str)][:4],
            "resources": resources,
        })
    return member_vitals


@router.get("/parties", response_class=HTMLResponse)
def parties_list(request: Request, page: int = 1, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_view_section(request, world, "parties"):
        raise HTTPException(403)
    base_q = db.query(Party).filter(Party.world_id == world.id).order_by(Party.name)
    parties, page, total_pages = paginate(base_q, page)
    member_counts = {
        p.id: len(json.loads(p.member_pc_ids_json or "[]")) + len(json.loads(p.member_entity_ids_json or "[]"))
        for p in parties
    }
    return templates.TemplateResponse("parties/list.html", {
        "request": request, "world": world, "worlds": worlds,
        "parties": parties, "member_counts": member_counts,
        "page": page, "total_pages": total_pages,
        "can_create": _can_manage_parties(request, world),
    })


@router.post("/parties/new")
def party_create(request: Request, name: str = Form("New Party"), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world or not _can_manage_parties(request, world):
        raise HTTPException(403)
    p = Party(world_id=world.id, name=name.strip() or "New Party")
    db.add(p)
    db.commit()
    db.refresh(p)
    live.touch(world.id)
    return RedirectResponse(f"/parties/{p.id}", status_code=303)


@router.get("/parties/{party_id}", response_class=HTMLResponse)
def party_detail(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party or not world_row_visible(request, db, party.world_id, "parties"):
        raise HTTPException(404)
    party_world = world if (world and world.id == party.world_id) else db.get(World, party.world_id)
    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    entity_ids = json.loads(party.member_entity_ids_json or "[]")
    member_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all() if pc_ids else []
    user = getattr(request.state, "user", None)
    my_pc_ids = (
        [pc.id for pc in member_pcs if user and not user.is_gm and pc.owner_user_id == user.id]
        if user else []
    )
    member_entities = db.query(Entity).filter(Entity.id.in_(entity_ids)).all() if entity_ids else []
    all_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == party.world_id).order_by(PlayerCharacter.name).all()
    all_entities = db.query(Entity).filter(
        Entity.world_id == party.world_id, Entity.kind.in_(_COMBATANT_KINDS)
    ).order_by(Entity.name).all()
    assigned_quests = db.query(Quest).filter(Quest.assigned_party_id == party.id).all()
    loot = json.loads(party.loot_json or "[]")

    # Live member vitals — the GM's at-a-glance strip (see _member_vitals;
    # shared with the JSON refetch route and the GM Cockpit).
    member_vitals = _member_vitals(db, member_pcs)

    # Party history: every session, combat, and calendar event tied to this
    # party, newest first — the "where have we been" view.
    history_sessions = (
        db.query(GameSession).filter(GameSession.party_id == party.id)
        .order_by(GameSession.session_num.desc()).all()
    )
    history_combats = (
        db.query(CombatSession).filter(CombatSession.party_id == party.id)
        .order_by(CombatSession.created_at.desc()).all()
    )
    history_events = (
        db.query(CalendarEvent).filter(CalendarEvent.party_id == party.id)
        .order_by(CalendarEvent.day.desc()).all()
    )
    return templates.TemplateResponse("parties/detail.html", {
        "request": request, "world": world, "worlds": worlds, "party": party,
        "member_pcs": member_pcs, "member_entities": member_entities,
        "all_pcs": all_pcs, "all_entities": all_entities,
        "assigned_quests": assigned_quests, "loot": loot,
        "pc_ids": pc_ids, "entity_ids": entity_ids,
        "edit_level": _party_edit_level(request, db, party_world, party),
        "member_names": {p.id: p.name for p in member_pcs},
        "my_pc_ids": my_pc_ids,
        "member_vitals": member_vitals,
        "xp_ledger": list(reversed(json.loads(party.xp_json or "[]"))),
        "history_sessions": history_sessions,
        "history_combats": history_combats,
        "history_events": history_events,
    })


@router.get("/api/parties/{party_id}/vitals")
def party_vitals(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Member-vitals as JSON — what the live-sync bus tells open party pages
    and GM Cockpit panels to re-fetch when anything about a member changes.
    Same visibility rule as the party detail page itself."""
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party or not world_row_visible(request, db, party.world_id, "parties"):
        raise HTTPException(404)
    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    member_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all() if pc_ids else []
    return {
        "party_id": party.id,
        "name": party.name,
        "members": _member_vitals(db, member_pcs),
    }


@router.post("/parties/{party_id}/edit")
async def party_edit(party_id: int, request: Request, db: Session = Depends(get_db)):
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    world = db.get(World, party.world_id)
    level = _party_edit_level(request, db, world, party)
    if level == "none":
        raise HTTPException(403)
    form = await request.form()
    party.notes = str(form.get("notes", "")).strip()
    if level == "full":
        # Name and membership are structural — only a GM/edit-level
        # assistant may change them; a member-level player may only touch
        # notes (see _party_edit_level's own docstring).
        party.name = str(form.get("name", party.name)).strip() or party.name
        pc_ids = [int(v) for v in form.getlist("member_pc_ids")]
        entity_ids = [int(v) for v in form.getlist("member_entity_ids")]
        party.member_pc_ids_json = json.dumps(pc_ids)
        party.member_entity_ids_json = json.dumps(entity_ids)
    db.commit()
    live.touch(party.world_id)
    return RedirectResponse(f"/parties/{party_id}", status_code=303)


@router.post("/parties/{party_id}/delete")
def party_delete(party_id: int, request: Request, db: Session = Depends(get_db)):
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    world = db.get(World, party.world_id)
    if _party_edit_level(request, db, world, party) != "full":
        raise HTTPException(403)
    db.query(Quest).filter(Quest.assigned_party_id == party_id).update({"assigned_party_id": None})
    db.delete(party)
    db.commit()
    live.touch(party.world_id)
    return RedirectResponse("/parties", status_code=303)


@router.post("/api/parties/{party_id}/loot")
async def party_loot(party_id: int, request: Request, db: Session = Depends(get_db)):
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    world = db.get(World, party.world_id)
    if _party_edit_level(request, db, world, party) == "none":
        raise HTTPException(403)
    body = await request.json()
    action = body.get("action")
    loot = json.loads(party.loot_json or "[]")
    # Normalize pre-claim items so index-based actions never hit a missing key.
    for item in loot:
        item.setdefault("claimed_by", [])

    def _member_pc_ids_of(user) -> set:
        if not user:
            return set()
        member_ids = json.loads(party.member_pc_ids_json or "[]")
        if not member_ids:
            return set()
        return {row[0] for row in db.query(PlayerCharacter.id).filter(
            PlayerCharacter.id.in_(member_ids),
            PlayerCharacter.owner_user_id == user.id).all()}

    user = getattr(request.state, "user", None)
    level = _party_edit_level(request, db, world, party)

    if action == "add":
        loot.append({"name": body.get("name", "Item"), "qty": int(body.get("qty", 1) or 1),
                     "notes": body.get("notes", ""), "claimed_by": []})
    elif action == "remove":
        idx = int(body.get("index", -1))
        if 0 <= idx < len(loot):
            loot.pop(idx)
    elif action in ("claim", "unclaim"):
        # A member-level player claims/unclaims FOR THEIR OWN PC only; a
        # full-level editor (GM/assistant) may claim for any member.
        idx = int(body.get("index", -1))
        pc_id = int(body.get("pc_id", 0))
        member_ids = json.loads(party.member_pc_ids_json or "[]")
        if not (0 <= idx < len(loot)) or pc_id not in member_ids:
            raise HTTPException(400, "Invalid loot index or PC")
        if level != "full":
            own = _member_pc_ids_of(user)
            if pc_id not in own:
                raise HTTPException(403, "You can only claim loot for your own character.")
        claimed = loot[idx].setdefault("claimed_by", [])
        if action == "claim" and pc_id not in claimed:
            claimed.append(pc_id)
        elif action == "unclaim" and pc_id in claimed:
            claimed.remove(pc_id)
    party.loot_json = json.dumps(loot)
    db.commit()
    live.touch(party.world_id)
    return {"loot": loot}


@router.post("/api/parties/{party_id}/location")
async def party_set_location(party_id: int, request: Request, db: Session = Depends(get_db)):
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    world = db.get(World, party.world_id)
    if _party_edit_level(request, db, world, party) != "full":
        raise HTTPException(403)
    body = await request.json()
    kind = body.get("kind")
    if kind not in ("map", "schematic"):
        party.location_json = "{}"
    else:
        slug = str(body.get("slug", "")).strip()
        if not slug:
            raise HTTPException(400, "Missing slug")
        try:
            if kind == "map":
                party.location_json = json.dumps({
                    "kind": "map", "slug": slug,
                    "lat": float(body.get("lat")), "lng": float(body.get("lng")),
                })
            else:
                party.location_json = json.dumps({
                    "kind": "schematic", "slug": slug,
                    "x": float(body.get("x")), "y": float(body.get("y")),
                })
        except (TypeError, ValueError):
            raise HTTPException(400, "Missing or invalid coordinates")
    db.commit()
    live.touch(party.world_id)
    return {"location": json.loads(party.location_json)}


@router.post("/api/parties/{party_id}/launch-combat")
def party_launch_combat(party_id: int, request: Request, db: Session = Depends(get_db)):
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    world = db.get(World, party.world_id)
    if _party_edit_level(request, db, world, party) != "full":
        raise HTTPException(403)
    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    entity_ids = json.loads(party.member_entity_ids_json or "[]")
    combatants = []
    for pc in db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all() if pc_ids else []:
        combatants.append(pc_to_combatant(pc))
    for ent in db.query(Entity).filter(Entity.id.in_(entity_ids)).all() if entity_ids else []:
        combatants.append(entity_to_combatant(ent))
    cs = CombatSession(world_id=party.world_id, name=f"{party.name} Encounter",
                       party_id=party.id, combatants_json=json.dumps(combatants))
    db.add(cs)
    db.commit()
    db.refresh(cs)
    live.touch(party.world_id)
    return {"redirect": f"/combat/{cs.id}"}
