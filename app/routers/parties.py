import json

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth
from ..database import get_db
from ..deps import (
    filter_visible_entities, get_world_ctx, paginate, world_can_edit_section,
    world_can_view_section, world_row_visible,
)
from .. import live
from ..pc_stats import pc_maxima
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


def _party_for_write(request: Request, db: Session, party_id: int, active_world) -> tuple:
    """(party, its world) for a WRITE route, else 404. A GM may act on a party
    in any world; everyone else only on a party in the world they are
    currently ACTIVE in and belong to. _party_edit_level reads the caller's
    role from request.state (computed for the ACTIVE world), so without this
    an assistant of world A held "full" edit over any party id in world B
    whose matrix allowed assistant edits."""
    party = db.get(Party, party_id)
    if not party:
        raise HTTPException(404)
    world = db.get(World, party.world_id)
    if not _viewer_is_gm(request):
        user = getattr(request.state, "user", None)
        current, _ = get_world_ctx(request, db, active_world)
        if not (world and current and current.id == world.id
                and auth.user_can_access_world(db, user, world)):
            raise HTTPException(404)
    return party, world


def _viewer_is_gm(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.is_gm)


def _visible_entity_ids(db: Session, request: Request, ids: list, world_id: int) -> set:
    """Of `ids`, the Entity ids in `world_id` this viewer may know exist: a
    GM sees every (existing) one, everyone else — players AND assistants,
    who see what players see — only visible_to_players entities plus any
    hidden one shared with them specifically (deps.filter_visible_entities,
    the one filter every player-facing entity list goes through). Party
    companions are GM-curated and often secret NPCs, so every party view
    resolves them through this rather than a bare Entity query."""
    ids = [i for i in ids if isinstance(i, int)]
    if not ids:
        return set()
    q = filter_visible_entities(
        db.query(Entity.id).filter(Entity.id.in_(ids), Entity.world_id == world_id), request)
    return {row[0] for row in q.all()}


def visible_member_count(db: Session, request: Request, party: Party) -> int:
    """Member count as THIS viewer may know it — hidden companions don't
    count for non-GMs (a count that includes a secret NPC is itself a
    spoiler on the party list and map pins)."""
    n_pcs = len(json.loads(party.member_pc_ids_json or "[]"))
    ent_ids = json.loads(party.member_entity_ids_json or "[]")
    if _viewer_is_gm(request):
        return n_pcs + len(ent_ids)
    return n_pcs + len(_visible_entity_ids(db, request, ent_ids, party.world_id))


def _party_quests(db: Session, request: Request, party: Party, world) -> list:
    """Quests assigned to this party, as this viewer may see them: a GM sees
    all; everyone else only visible_to_players quests, and none at all if
    the world has closed the Quests section to their role (the same two
    rules the /quests page applies)."""
    q = db.query(Quest).filter(Quest.assigned_party_id == party.id)
    if _viewer_is_gm(request):
        return q.all()
    if not world_can_view_section(request, world, "quests"):
        return []
    return q.filter(Quest.visible_to_players.isnot(False)).all()


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
        hp_max = pc_maxima(pc)["hp"]  # 0 stored = auto-derived, same rule as the sheet
        member_vitals.append({
            "id": pc.id, "name": pc.name,
            "hp": pc.current_hp, "max_hp": hp_max, "temp_hp": pc.temp_hp,
            "ac": pc.armor_class, "level": pc.level,
            "down": hp_max > 0 and (pc.current_hp or 0) <= 0,
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
    member_counts = {p.id: visible_member_count(db, request, p) for p in parties}
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
    # Companions resolve through the viewer's visibility (see
    # _visible_entity_ids): a hidden NPC the GM added is never named to a
    # player/assistant — not in the member list, the editor's pick list, the
    # quick-add datalist, nor (below) the ids handed to the page.
    visible_ids = _visible_entity_ids(db, request, entity_ids, party.world_id)
    entity_ids = [i for i in entity_ids if i in visible_ids] if not _viewer_is_gm(request) else entity_ids
    member_entities = db.query(Entity).filter(Entity.id.in_(visible_ids)).all() if visible_ids else []
    all_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == party.world_id).order_by(PlayerCharacter.name).all()
    all_entities = filter_visible_entities(db.query(Entity).filter(
        Entity.world_id == party.world_id, Entity.kind.in_(_COMBATANT_KINDS)
    ), request).order_by(Entity.name).all()
    assigned_quests = _party_quests(db, request, party, party_world)
    loot = json.loads(party.loot_json or "[]")

    # Live member vitals — the GM's at-a-glance strip (see _member_vitals;
    # shared with the JSON refetch route and the GM Cockpit).
    member_vitals = _member_vitals(db, member_pcs)

    # At-a-glance party state (2026-09-30 improvements): level-up digest,
    # one-line condition summary, unclaimed-loot count.
    from .characters import _levelup_ready
    levelup_names = [pc.name for pc in member_pcs if _levelup_ready(pc)]
    cond_counts = {}
    downs = 0
    for v in member_vitals:
        if v.get("down"):
            downs += 1
        for c in (v.get("conditions") or []):
            cond_counts[str(c).strip().lower()] = cond_counts.get(str(c).strip().lower(), 0) + 1
    condition_summary = ", ".join(
        f"{n}×{c}" if c > 1 else n for n, c in sorted(cond_counts.items(), key=lambda kv: -kv[1]))
    if downs:
        condition_summary = (condition_summary + ", " if condition_summary else "") + f"{downs} DOWN"
    unclaimed_loot = sum(1 for item in loot if not (item.get("claimed_by") or []))

    # Party history: every session, combat, and calendar event tied to this
    # party, newest first — the "where have we been" view. Each list is gated
    # by the viewer's access to THAT section: a world that closes Sessions /
    # Combat / Calendar to players must not have their titles leak through
    # the party page (the rows themselves carry no per-row visibility flag).
    can_sessions = world_can_view_section(request, party_world, "sessions")
    history_sessions = (
        db.query(GameSession).filter(GameSession.party_id == party.id)
        .order_by(GameSession.session_num.desc()).all()
    ) if can_sessions else []
    history_combats = (
        db.query(CombatSession).filter(CombatSession.party_id == party.id)
        .order_by(CombatSession.created_at.desc()).all()
    ) if world_can_view_section(request, party_world, "combat") else []
    history_events = (
        db.query(CalendarEvent).filter(CalendarEvent.party_id == party.id)
        .order_by(CalendarEvent.day.desc()).all()
    ) if world_can_view_section(request, party_world, "calendar") else []
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
        "can_view_sessions": can_sessions,
        "levelup_names": levelup_names,
        "condition_summary": condition_summary,
        "unclaimed_loot": unclaimed_loot,
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
async def party_edit(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    party, world = _party_for_write(request, db, party_id, active_world)
    level = _party_edit_level(request, db, world, party)
    if level == "none":
        raise HTTPException(403)
    form = await request.form()
    party.notes = str(form.get("notes", "")).strip()
    party.goals = str(form.get("goals", "")).strip()
    if level == "full":
        # Name and membership are structural — only a GM/edit-level
        # assistant may change them; a member-level player may only touch
        # notes (see _party_edit_level's own docstring).
        party.name = str(form.get("name", party.name)).strip() or party.name
        try:
            pc_ids = [int(v) for v in form.getlist("member_pc_ids")]
            entity_ids = [int(v) for v in form.getlist("member_entity_ids")]
        except ValueError:
            raise HTTPException(400, "Member ids must be numbers")
        # Members must live in this party's world — a forged id from another
        # world is dropped (GM included; the editor can't produce one).
        if pc_ids:
            own_pcs = {r[0] for r in db.query(PlayerCharacter.id).filter(
                PlayerCharacter.id.in_(pc_ids), PlayerCharacter.world_id == party.world_id)}
            pc_ids = [i for i in pc_ids if i in own_pcs]
        allowed = _visible_entity_ids(db, request, entity_ids, party.world_id)
        entity_ids = [i for i in entity_ids if i in allowed]
        if not _viewer_is_gm(request):
            # An assistant's editor only ever LISTED the companions they may
            # see, so what they submit is just the visible subset (kept
            # above). Carry over the hidden companions the GM added
            # untouched — otherwise saving the form would silently drop
            # every secret NPC from the party.
            old_ids = json.loads(party.member_entity_ids_json or "[]")
            seen_old = _visible_entity_ids(db, request, old_ids, party.world_id)
            entity_ids += [i for i in old_ids if i not in seen_old]
        party.member_pc_ids_json = json.dumps(pc_ids)
        party.member_entity_ids_json = json.dumps(entity_ids)
    db.commit()
    live.touch(party.world_id)
    return RedirectResponse(f"/parties/{party_id}", status_code=303)


@router.post("/parties/{party_id}/delete")
def party_delete(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    party, world = _party_for_write(request, db, party_id, active_world)
    if _party_edit_level(request, db, world, party) != "full":
        raise HTTPException(403)
    db.query(Quest).filter(Quest.assigned_party_id == party_id).update({"assigned_party_id": None})
    db.delete(party)
    db.commit()
    live.touch(party.world_id)
    return RedirectResponse("/parties", status_code=303)


@router.post("/api/parties/{party_id}/loot")
async def party_loot(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    party, world = _party_for_write(request, db, party_id, active_world)
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
async def party_set_location(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    party, world = _party_for_write(request, db, party_id, active_world)
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
def party_launch_combat(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    party, world = _party_for_write(request, db, party_id, active_world)
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


# ── AI party oracle ──────────────────────────────────────────────────────────
# Reads the party's roster (names, races, professions, levels — no secrets)
# and proposes bonds/tensions/hooks between them. GM suggestion panel;
# writes nothing.

@router.post("/parties/{party_id}/ai-insights")
async def party_ai_insights(party_id: int, request: Request,
                            db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Suggest inter-party bonds, tensions and a personal hook per member
    from the roster. GM-only; returns a draft, writes nothing."""
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    party = db.query(Party).filter(Party.id == party_id, Party.world_id == world.id).first()
    if not party:
        raise HTTPException(404)
    from .. import ai as _ai
    if not _ai.effective_llm_api_key():
        raise HTTPException(400, "No AI backend configured — set UNSLOTH_API_KEY (Settings → System)")

    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    ent_ids = json.loads(party.member_entity_ids_json or "[]")
    roster = []
    for pc in db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids or [])).all():
        roster.append(f"- {pc.name} ({pc.race or '?'} {pc.char_class or '?'}{f' {pc.subclass}' if pc.subclass else ''}, level {pc.level})"
                      + (f", played by {pc.player_name}" if pc.player_name else ""))
    for e in db.query(Entity).filter(Entity.id.in_(ent_ids or [])).all():
        roster.append(f"- {e.name} (NPC ally, {e.kind})")
    if len(roster) < 2:
        raise HTTPException(400, "A party needs at least two members for insights")

    system = (
        "You are a TTRPG party-dynamics assistant. From the roster, propose what makes this "
        "party tick. Reply ONLY with JSON, no fences:\n"
        '{"bonds": [str], "tensions": [str], "hooks": [{"who": str, "hook": str}]}\n'
        "Rules: 2-3 bonds and 1-3 tensions (each one sentence, grounded in who these "
        "characters are, never contradicting the roster); exactly one hook per member — "
        "a personal side-quest seed in one sentence; keep everything setting-neutral."
    )
    try:
        raw = await _ai.generate_chat(
            [{"role": "user", "content": f"Party: {party.name}\n" + "\n".join(roster)}],
            system=system, options={"num_predict": 900}, think=False,
        )
    except Exception as exc:
        raise HTTPException(502, f"AI call failed: {exc}") from exc
    if _ai.is_failure_sentinel(raw or ""):
        raise HTTPException(502, raw)
    from .boards_generate import _extract_json_object
    data = _extract_json_object(raw) or {}
    out = {
        "bonds": [str(b)[:240] for b in data.get("bonds", []) if str(b).strip()][:4],
        "tensions": [str(t)[:240] for t in data.get("tensions", []) if str(t).strip()][:4],
        "hooks": [{"who": str(h.get("who"))[:80], "hook": str(h.get("hook"))[:240]}
                  for h in data.get("hooks", []) if isinstance(h, dict) and h.get("who") and h.get("hook")][:8],
    }
    if not (out["bonds"] or out["tensions"] or out["hooks"]):
        raise HTTPException(502, "The AI reply wasn't usable — try again.")
    return out


# ── Quick-add companion (vitals strip) ───────────────────────────────────────
# Mid-session hiring shouldn't require scrolling to the editor and a full
# Save round-trip. One POST toggles one membership.

@router.post("/api/parties/{party_id}/members/toggle")
async def party_member_toggle(party_id: int, request: Request, db: Session = Depends(get_db),
                              active_world: str = Cookie(None)):
    """Toggle one member's presence in the party ({"kind": "pc"|"entity",
    "id": int}) — the vitals strip's quick-add. Full-edit tier (structural
    change, same as the membership form)."""
    world, _ = get_world_ctx(request, db, active_world)
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party or not world or party.world_id != world.id:
        raise HTTPException(404)
    if _party_edit_level(request, db, world, party) != "full":
        raise HTTPException(403)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    kind = str(body.get("kind") or "")
    if kind not in ("pc", "entity"):
        raise HTTPException(400, "kind must be 'pc' or 'entity'")
    # Either an id or a name: the quick-add UI sends the name picked from
    # the datalist (names resolve world-scoped, so an ambiguous name is a
    # 404 rather than a wrong-party write).
    member_id = int(body.get("id") or 0)
    name = str(body.get("name") or "").strip()
    if kind == "pc":
        exists = db.get(PlayerCharacter, member_id) if member_id else             db.query(PlayerCharacter).filter(
                PlayerCharacter.world_id == party.world_id,
                PlayerCharacter.name == name).first() if name else None
    else:
        exists = db.get(Entity, member_id) if member_id else             db.query(Entity).filter(
                Entity.world_id == party.world_id,
                Entity.name == name).first() if name else None
        # A non-GM can't add (or probe the existence of, by name) an entity
        # they aren't allowed to see — same answer as "doesn't exist".
        if exists and not _viewer_is_gm(request) and exists.id not in _visible_entity_ids(
                db, request, [exists.id], party.world_id):
            exists = None
    if not exists or exists.world_id != party.world_id:
        raise HTTPException(404, "No such member in this world")
    member_id = exists.id
    field = "member_pc_ids_json" if kind == "pc" else "member_entity_ids_json"
    ids = json.loads(getattr(party, field) or "[]")
    if member_id in ids:
        ids.remove(member_id)
        added = False
    else:
        ids.append(member_id)
        added = True
    setattr(party, field, json.dumps(ids))
    db.commit()
    live.touch(party.world_id)
    return {"ok": True, "added": added}


# ── Rest (N&D rules) ─────────────────────────────────────────────────────────
# core_rules.md §10: Rest restores ½ PP and MP (rounded down) and ALL Shock;
# HP is medical-treatment territory (stims, capped at 3/rest) and is left
# alone. The per-character PP/MP routes already implement exactly this math
# (characters.py's action=="rest" branches) — the party route applies the
# same formulas across the roster and returns a snapshot the client holds
# for one-click Undo.

@router.post("/api/parties/{party_id}/rest")
async def party_rest(party_id: int, request: Request, db: Session = Depends(get_db),
                     active_world: str = Cookie(None)):
    """Apply a rules Rest (core_rules.md §10) to every member PC: +½ max
    PP and MP (rounded down), all Shock restored. HP is untouched — that's
    stims/medical. Returns the per-PC snapshot for one-click Undo (the
    client POSTs it back to /rest/undo). Full-edit tier."""
    world, _ = get_world_ctx(request, db, active_world)
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party or not world or party.world_id != world.id:
        raise HTTPException(404)
    if _party_edit_level(request, db, world, party) != "full":
        raise HTTPException(403)
    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    member_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all() if pc_ids else []
    snapshot, applied = [], []
    for pc in member_pcs:
        m = pc_maxima(pc)
        if not m["native"]:
            continue  # custom-sheet characters track their own resources; PP/MP/Shock aren't theirs to rest
        pp_max, mp_max = m["pp"], m["mp"]
        snapshot.append({"id": pc.id, "name": pc.name,
                         "pp_current": pc.pp_current or 0,
                         "mp_current": pc.mp_current or 0,
                         "shock_current": pc.shock_current or 0})
        new_pp = min(pp_max, (pc.pp_current or 0) + pp_max // 2)
        new_mp = min(mp_max, (pc.mp_current or 0) + mp_max // 2)
        pc.pp_current, pc.mp_current = new_pp, new_mp
        pc.shock_current = m["shock"]
        applied.append({"id": pc.id, "name": pc.name,
                        "pp_current": new_pp, "pp_max": pp_max,
                        "mp_current": new_mp, "mp_max": mp_max})
    db.commit()
    live.touch(party.world_id)
    return {"applied": applied, "snapshot": snapshot}


@router.post("/api/parties/{party_id}/rest/undo")
async def party_rest_undo(party_id: int, request: Request, db: Session = Depends(get_db),
                          active_world: str = Cookie(None)):
    """Undo a Rest: the client POSTs back the snapshot /rest returned.
    Only member PCs of THIS party are touched, and only the three fields
    the rest changed."""
    world, _ = get_world_ctx(request, db, active_world)
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party or not world or party.world_id != world.id:
        raise HTTPException(404)
    if _party_edit_level(request, db, world, party) != "full":
        raise HTTPException(403)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    snapshot = body.get("snapshot")
    if not isinstance(snapshot, list):
        raise HTTPException(400, "snapshot must be a list")
    member_ids = set(json.loads(party.member_pc_ids_json or "[]"))
    restored = 0
    for entry in snapshot:
        if not isinstance(entry, dict):
            continue
        pc_id = entry.get("id")
        if not isinstance(pc_id, int) or pc_id not in member_ids:
            continue  # refuse to touch non-members
        pc = db.get(PlayerCharacter, pc_id)
        if not pc:
            continue
        pc.pp_current = max(0, int(entry.get("pp_current") or 0))
        pc.mp_current = max(0, int(entry.get("mp_current") or 0))
        pc.shock_current = max(0, int(entry.get("shock_current") or 0))
        restored += 1
    db.commit()
    live.touch(party.world_id)
    return {"ok": True, "restored": restored}


# ── Printable summary ────────────────────────────────────────────────────────

@router.get("/parties/{party_id}/summary", response_class=HTMLResponse)
def party_summary(party_id: int, request: Request, db: Session = Depends(get_db),
                  active_world: str = Cookie(None)):
    """One-page printable party card: vitals, conditions, goals, loot,
    quests, notes. Same visibility as the detail page; the template's
    print CSS makes it a clean handout."""
    world, worlds = get_world_ctx(request, db, active_world)
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party or not world_row_visible(request, db, party.world_id, "parties"):
        raise HTTPException(404)
    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    entity_ids = json.loads(party.member_entity_ids_json or "[]")
    member_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all() if pc_ids else []
    visible_ids = _visible_entity_ids(db, request, entity_ids, party.world_id)
    member_entities = db.query(Entity).filter(Entity.id.in_(visible_ids)).all() if visible_ids else []
    party_world = world if (world and world.id == party.world_id) else db.get(World, party.world_id)
    assigned_quests = _party_quests(db, request, party, party_world)
    return templates.TemplateResponse("parties/summary.html", {
        "request": request, "world": world, "party": party,
        "member_pcs": member_pcs, "member_entities": member_entities,
        "member_vitals": _member_vitals(db, member_pcs),
        "loot": json.loads(party.loot_json or "[]"),
        "assigned_quests": assigned_quests,
        "member_names": {p.id: p.name for p in member_pcs},
    })
