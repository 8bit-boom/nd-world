import io
import json
import uuid
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from .. import auth
from ..database import get_db
from ..deps import (
    filter_visible_entities, get_world_ctx, paginate, world_can_edit_section,
    world_can_view_section, world_row_visible,
)
from .. import live
from ..party_refs import (
    LOOT_NAME_MAX, LOOT_NOTES_MAX, LOOT_QTY_MAX, load_loot, member_ids as party_member_ids_raw,
)
from ..pc_stats import pc_maxima
from ..sheet_systems import (
    apply_rest, field_value_text, has_short_rest, hp_track, parse_custom_fields, resource_tracks, rest_ops,
    rest_touched_keys, roster_field_groups, short_label, system_meta, template_fields,
)
from ..pc_digest import pc_digest_line
from ..models import CalendarEvent, CombatSession, Entity, GameSession, Party, PlayerCharacter, Quest, SheetTemplate, World
from .characters import (  # cross-router imports, per AGENTS.md
    _derived, _levelup_ready as _pc_levelup_ready, _pc_to_ndc_dict, _safe_export_filename,
)
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


def _member_vitals(db: Session, member_pcs: list, resource_limit: Optional[int] = 4) -> list:
    """The live member-vitals strip, shared by the party detail page, the
    /api/parties/{id}/vitals JSON (live-sync refetches), the GM Cockpit, the
    roster and the hub. Conditions stay on the sheet (freeform JSON).

    System-aware (see app/sheet_systems.py): an N&D character reads HP/AC off
    its columns against the effective maxima; a character on a custom system
    (Asterion, Hunt in the Moonlight, a GM's own template) has no AC, takes its
    HP from the template's vital track (Health / Flesh, DOWN at 0) and lists its
    other resource tracks, with the template's defaults applied to anything not
    yet saved — exactly what the sheet itself shows. `resource_limit` caps the
    chips on the compact strip (None = all of them)."""
    member_vitals = []
    tpl_cache = {}

    def tpl_info(tpl_id):
        if tpl_id not in tpl_cache:
            tpl = db.get(SheetTemplate, tpl_id) if tpl_id else None
            tpl_cache[tpl_id] = (tpl, template_fields(tpl) if tpl else [], system_meta(tpl) if tpl else None)
        return tpl_cache[tpl_id]

    for pc in sorted(member_pcs, key=lambda p: p.name or ""):
        try:
            conds = json.loads(pc.conditions_json or "[]")
        except ValueError:
            conds = []
        m = pc_maxima(pc)
        tpl, fields, meta = tpl_info(getattr(pc, "sheet_template_id", None))
        cf = parse_custom_fields(pc.custom_fields_json)
        tracks = resource_tracks(fields, cf, meta) if tpl else []
        hp_id, hp_label = None, "HP"
        if m["native"]:
            hp, hp_max, ac = pc.current_hp, m["hp"], pc.armor_class
        else:
            vital = hp_track(fields, cf, meta) if tpl else None
            hp, hp_max, ac = None, 0, None  # AC is an N&D stat; no column HP on a custom system
            if vital:
                hp, hp_max, hp_id, hp_label = vital["current"], vital["max"], vital["id"], vital["label"]
        member_vitals.append({
            "id": pc.id, "name": pc.name, "native": m["native"],
            "hp": hp, "max_hp": hp_max, "hp_label": hp_label, "hp_id": hp_id,
            "temp_hp": pc.temp_hp if m["native"] else 0,
            # level is an N&D concept; a custom system shows its own name instead
            "ac": ac, "level": pc.level if m["native"] else None,
            "system": "" if m["native"] or tpl is None else tpl.name,
            "down": hp_max > 0 and (hp or 0) <= 0,
            "levelup": _pc_levelup_ready(pc),
            "conditions": [c for c in conds if isinstance(c, str)][:4],
            "resources": tracks if resource_limit is None else tracks[:resource_limit],
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
    loot = _load_loot(party)

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
        "vitals_by_id": {v["id"]: v for v in member_vitals},
        "xp_ledger": list(reversed(json.loads(party.xp_json or "[]"))),
        "history_sessions": history_sessions,
        "history_combats": history_combats,
        "history_events": history_events,
        "can_view_sessions": can_sessions,
        "levelup_names": levelup_names,
        "condition_summary": condition_summary,
        "unclaimed_loot": unclaimed_loot,
        # offer a Short Rest button only if someone's system actually has one (Asterion)
        "short_rest_available": any(
            has_short_rest(db.get(SheetTemplate, tid))
            for tid in {pc.sheet_template_id for pc in member_pcs if pc.sheet_template_id}),
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


def party_member_ids(party: Party) -> list:
    return party_member_ids_raw(party.member_pc_ids_json)


_LOOT_NAME_MAX, _LOOT_NOTES_MAX, _LOOT_QTY_MAX = LOOT_NAME_MAX, LOOT_NOTES_MAX, LOOT_QTY_MAX
_load_loot = load_loot


def _loot_int(body: dict, key: str, default=None, lo: int = None, hi: int = None) -> int:
    raw = body.get(key, default)
    if isinstance(raw, bool) or raw is None:
        raise HTTPException(400, f"{key} must be a whole number")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{key} must be a whole number")
    if (lo is not None and val < lo) or (hi is not None and val > hi):
        raise HTTPException(400, f"{key} is out of range")
    return val


def _loot_index(loot: list, body: dict) -> int:
    """Index of the item a request names: `lid` wins (409 if it has since
    disappeared — someone else removed/gave it), else the legacy `index`."""
    lid = body.get("lid")
    if lid is not None:
        for i, item in enumerate(loot):
            if item["lid"] == lid:
                return i
        raise HTTPException(409, "That loot item no longer exists — it was removed or given away. Refresh the page.")
    idx = _loot_int(body, "index", -1)
    if not (0 <= idx < len(loot)):
        raise HTTPException(400, "Invalid loot item")
    return idx


@router.post("/api/parties/{party_id}/loot")
async def party_loot(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Edit the shared stash. Actions: add {name, qty?, notes?}, remove, claim /
    unclaim {pc_id}, give {pc_id, qty?} (moves the item — or `qty` of it — into
    that member's equipment). Items are addressed by `lid` (or legacy `index`)."""
    party, world = _party_for_write(request, db, party_id, active_world)
    level = _party_edit_level(request, db, world, party)
    if level == "none":
        raise HTTPException(403)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    action = body.get("action")
    loot = _load_loot(party)
    user = getattr(request.state, "user", None)
    member_ids = party_member_ids(party)

    def _own_member_ids() -> set:
        if not user or not member_ids:
            return set()
        return {row[0] for row in db.query(PlayerCharacter.id).filter(
            PlayerCharacter.id.in_(member_ids), PlayerCharacter.owner_user_id == user.id).all()}

    def _member_target() -> int:
        """pc_id from the body: must be a party member, and a member-level
        player may only act for their OWN character (full-level may for any)."""
        pc_id = _loot_int(body, "pc_id")
        if pc_id not in member_ids:
            raise HTTPException(400, "That character is not in this party")
        if level != "full" and pc_id not in _own_member_ids():
            raise HTTPException(403, "You can only do that for your own character.")
        return pc_id

    touched_pc_world = None
    if action == "add":
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "Item name is required")
        notes = body.get("notes", "")
        if not isinstance(notes, str):
            raise HTTPException(400, "notes must be text")
        loot.append({"lid": uuid.uuid4().hex[:8], "name": name.strip()[:_LOOT_NAME_MAX],
                     "qty": _loot_int(body, "qty", 1, 1, _LOOT_QTY_MAX), "notes": notes.strip()[:_LOOT_NOTES_MAX],
                     "claimed_by": []})
    elif action == "remove":
        loot.pop(_loot_index(loot, body))
    elif action in ("claim", "unclaim"):
        idx = _loot_index(loot, body)
        pc_id = _member_target()
        claimed = loot[idx]["claimed_by"]
        if action == "claim" and pc_id not in claimed:
            claimed.append(pc_id)
        elif action == "unclaim" and pc_id in claimed:
            claimed.remove(pc_id)
    elif action == "give":
        idx = _loot_index(loot, body)
        pc_id = _member_target()
        item = loot[idx]
        n = _loot_int(body, "qty", item["qty"], 1, _LOOT_QTY_MAX)
        if n > item["qty"]:
            raise HTTPException(400, f"Only {item['qty']} available")
        pc = db.get(PlayerCharacter, pc_id)
        if not pc or not pc_maxima(pc)["native"]:
            raise HTTPException(400, "Custom-sheet characters keep their inventory in the sheet's own fields — "
                                     "add it there instead.")
        try:
            gear = json.loads(pc.equipment_json or "[]")
        except ValueError:
            gear = []
        gear = gear if isinstance(gear, list) else []
        match = next((g for g in gear if isinstance(g, dict)
                      and str(g.get("name", "")).strip().lower() == item["name"].lower()), None)
        if match is not None:
            try:
                match["qty"] = int(match.get("qty") or 0) + n
            except (TypeError, ValueError):
                match["qty"] = n
        else:
            gear.append({"name": item["name"], "qty": n, "weight": 0, "equipped": False, "notes": item["notes"]})
        pc.equipment_json = json.dumps(gear)
        if n == item["qty"]:
            loot.pop(idx)
        else:
            item["qty"] -= n
    else:
        raise HTTPException(400, "action must be add, remove, claim, unclaim or give")
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
        combatants.append(pc_to_combatant(pc, db))
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
        # each character by its OWN system (Hunt in the Moonlight / Asterion / N&D), with its backstory
        roster.append("- " + pc_digest_line(pc, pc.sheet_template if pc.sheet_template_id else None, detail=True))
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

def apply_pc_rest(db: Session, pc: PlayerCharacter, kind: str, tpl_cache: dict = None):
    """Apply a Rest to ONE character by its own system's rules -> (result, snapshot entry), or None when its system has
    no Rest rules. Does NOT commit (callers do)."""
    tpl_cache = tpl_cache if tpl_cache is not None else {}
    m = pc_maxima(pc)
    if pc.sheet_template_id not in tpl_cache:
        tpl_cache[pc.sheet_template_id] = db.get(SheetTemplate, pc.sheet_template_id) if pc.sheet_template_id else None
    tpl = tpl_cache[pc.sheet_template_id]
    ops = rest_ops(tpl, kind) if tpl is not None else []
    if not m["native"] and not ops:
        return None  # a custom system without Rest rules tracks its own resources; nothing to apply
    entry = {"id": pc.id, "name": pc.name}
    result = {"id": pc.id, "name": pc.name}
    if m["native"]:
        entry.update(pp_current=pc.pp_current or 0, mp_current=pc.mp_current or 0,
                     shock_current=pc.shock_current or 0)
        new_pp = min(m["pp"], (pc.pp_current or 0) + m["pp"] // 2)
        new_mp = min(m["mp"], (pc.mp_current or 0) + m["mp"] // 2)
        pc.pp_current, pc.mp_current = new_pp, new_mp
        pc.shock_current = m["shock"]
        result.update(pp_current=new_pp, pp_max=m["pp"], mp_current=new_mp, mp_max=m["mp"], shock_current=m["shock"])
    if ops:
        entry["custom_fields_json"] = pc.custom_fields_json or "{}"
        cf = apply_rest(tpl, parse_custom_fields(pc.custom_fields_json), kind)
        pc.custom_fields_json = json.dumps(cf)
        result["tracks"] = resource_tracks(template_fields(tpl), cf, system_meta(tpl))
    return result, entry


def restore_pc_snapshot(db: Session, pc: PlayerCharacter, entry: dict) -> None:
    """Put back what a Rest changed on one character (the snapshot entry apply_pc_rest returned). Only the fields a Rest of
    this system can change are touched, so a crafted snapshot cannot rewrite anything else. Does NOT commit."""
    for col in ("pp_current", "mp_current", "shock_current"):
        if col in entry:
            try:
                setattr(pc, col, max(0, int(entry.get(col) or 0)))
            except (TypeError, ValueError):
                pass
    raw = entry.get("custom_fields_json")
    tpl = db.get(SheetTemplate, pc.sheet_template_id) if pc.sheet_template_id else None
    if isinstance(raw, str) and len(raw) <= _MAX_SNAPSHOT_FIELDS_BYTES and tpl is not None:
        try:
            before = json.loads(raw)
        except ValueError:
            before = None
        if isinstance(before, dict):
            cf = parse_custom_fields(pc.custom_fields_json)
            for key in rest_touched_keys(tpl):
                if key not in before:
                    cf.pop(key, None)  # it wasn't saved before the Rest: unsave it again
                elif isinstance(before[key], (int, float)) or (isinstance(before[key], str) and len(before[key]) <= 100):
                    cf[key] = before[key]
            pc.custom_fields_json = json.dumps(cf)


def apply_party_rest(db: Session, party: Party, kind: str):
    """Apply a Rest to every member of `party`, each by their own system's rules; returns
    (applied, snapshot) for the response / one-click Undo. Does NOT commit (callers do)."""
    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    member_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all() if pc_ids else []
    snapshot, applied = [], []
    tpl_cache = {}
    for pc in member_pcs:
        done = apply_pc_rest(db, pc, kind, tpl_cache)
        if done is None:
            continue
        applied.append(done[0])
        snapshot.append(done[1])
    return applied, snapshot


_MAX_SNAPSHOT_FIELDS_BYTES = 200_000


@router.post("/api/parties/{party_id}/rest")
async def party_rest(party_id: int, request: Request, db: Session = Depends(get_db),
                     active_world: str = Cookie(None)):
    """Apply a Rest to every member, each by THEIR system's rules. An N&D sheet
    gets the core-rules Rest (core_rules.md §10): +½ max PP and MP (rounded down),
    all Shock; HP is untouched — that's stims/medical. A character on a built-in
    system (app/sheet_systems.py BUILTIN_SYSTEMS) gets that rulebook's Rest —
    Asterion's Short/Long Rest, Hunt in the Moonlight's Stamina/Strain reset, the N&D
    stim counter. Body {"kind": "short"|"long"} (default long) picks Asterion's
    variant; systems with a single Rest apply it either way. A custom system with no
    Rest rules is left alone. Returns the per-PC snapshot for one-click Undo (the
    client POSTs it back to /rest/undo). Full-edit tier."""
    world, _ = get_world_ctx(request, db, active_world)
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party or not world or party.world_id != world.id:
        raise HTTPException(404)
    if _party_edit_level(request, db, world, party) != "full":
        raise HTTPException(403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = body.get("kind", "long") if isinstance(body, dict) else "long"
    if kind not in ("short", "long"):
        raise HTTPException(400, "kind must be short or long")
    applied, snapshot = apply_party_rest(db, party, kind)
    db.commit()
    live.touch(party.world_id)
    return {"applied": applied, "snapshot": snapshot, "kind": kind}


@router.post("/api/parties/{party_id}/rest/undo")
async def party_rest_undo(party_id: int, request: Request, db: Session = Depends(get_db),
                          active_world: str = Cookie(None)):
    """Undo a Rest: the client POSTs back the snapshot /rest returned.
    Only member PCs of THIS party are touched, and only the fields the rest
    changed (PP/MP/Shock for an N&D sheet, the sheet's custom fields for a
    custom system). Malformed entries are skipped, never applied."""
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
    snapshot = body.get("snapshot") if isinstance(body, dict) else None
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
        restore_pc_snapshot(db, pc, entry)
        restored += 1
    db.commit()
    live.touch(party.world_id)
    return {"ok": True, "restored": restored}


# ── Roster comparison + bundle export ────────────────────────────────────────

_ROSTER_STATS = (("str", "STR"), ("dex", "DEX"), ("bod", "BOD"), ("per", "PER"),
                 ("wil", "WIL"), ("int", "INT"), ("cha", "CHA"), ("itu", "ITU"))


def _cell(text="", *, down=False, prefix="", chips=None, na=False):
    """One roster cell: plain text (optionally flagged down / prefixed), a chip list, or n/a."""
    if na:
        return {"kind": "na"}
    if chips is not None:
        return {"kind": "chips", "chips": chips}
    return {"kind": "text", "text": text, "down": down, "prefix": prefix}


def _roster_table(db: Session, member_pcs: list) -> dict:
    """The side-by-side roster as data the template renders blindly:
    {"columns": [{id, name, sub}], "groups": [{"title", "rows": [{"label", "cells": [...]}]}]}.

    Every row is built from what each member's OWN system has: the vital (HP / Health /
    Flesh) and conditions for everyone; Shock, PP/MP, attributes, CA/Speed, edges and
    cyberware only when some member is a native N&D sheet; and for custom-sheet members
    their template's resource tracks plus the fields the system says are worth
    comparing (BUILTIN_SYSTEMS[slug]["roster"], or the first number/select fields).
    A member whose system lacks a row shows a dash, never another system's value."""
    pcs = sorted(member_pcs, key=lambda p: p.name or "")
    vitals = {v["id"]: v for v in _member_vitals(db, pcs, resource_limit=None)}
    tpls = {}

    def tpl_of(pc):
        tid = getattr(pc, "sheet_template_id", None)
        if tid not in tpls:
            tpls[tid] = db.get(SheetTemplate, tid) if tid else None
        return tpls[tid]

    columns, info = [], []
    for pc in pcs:
        v, m, tpl = vitals[pc.id], pc_maxima(pc), tpl_of(pc)
        if m["native"]:
            sub = " · ".join(x for x in (f"Lv {pc.level}", pc.race, pc.char_class, pc.player_name) if x)
        else:
            sub = " · ".join(x for x in ((tpl.name if tpl else ""), pc.player_name) if x)
        columns.append({"id": pc.id, "name": pc.name, "sub": sub})
        info.append({"pc": pc, "v": v, "m": m, "tpl": tpl, "native": m["native"],
                     "cf": parse_custom_fields(pc.custom_fields_json)})

    groups = []

    def add(title, label, cells):
        grp = next((g for g in groups if g["title"] == title), None)
        if grp is None:
            grp = {"title": title, "rows": []}
            groups.append(grp)
        grp["rows"].append({"label": label, "cells": cells})

    any_native = any(i["native"] for i in info)
    labels = {i["v"]["hp_label"] for i in info if i["v"]["max_hp"]}
    mixed = len(labels) > 1
    add("Condition", "HP / vital" if mixed else (next(iter(labels)) if labels else "HP"), [
        _cell(f"{i['v']['hp']} / {i['v']['max_hp']}" + (f" (+{i['v']['temp_hp']} temp)" if i["v"]["temp_hp"] else ""),
              down=i["v"]["down"], prefix=i["v"]["hp_label"] if mixed else "")
        if i["v"]["max_hp"] else _cell(na=True) for i in info])
    if any_native:
        add("Condition", "Shock", [_cell(f"{i['pc'].shock_current or 0} / {i['m']['shock']}") if i["native"] else _cell(na=True) for i in info])
        add("Condition", "PP / MP", [
            _cell(f"{i['pc'].pp_current or 0}/{i['m']['pp']} · {i['pc'].mp_current or 0}/{i['m']['mp']}")
            if i["native"] else _cell(na=True) for i in info])
    add("Condition", "Conditions", [_cell(chips=list(i["v"]["conditions"])) for i in info])

    if any_native:
        derived = {i["pc"].id: _derived(i["pc"]) for i in info if i["native"]}
        stat_val = {pid: {st["id"]: st.get("value") for st in d["stats"] if isinstance(st, dict) and st.get("id")}
                    for pid, d in derived.items()}
        for sid, label in _ROSTER_STATS:
            add("Attributes", label, [
                _cell(str(stat_val[i["pc"].id][sid])) if i["native"] and stat_val[i["pc"].id].get(sid) is not None
                else _cell(na=True) for i in info])
        add("Attributes", "Cyber Adapt.", [_cell(str(derived[i["pc"].id]["ca_derived"])) if i["native"] else _cell(na=True) for i in info])
        add("Attributes", "Speed", [_cell(str(derived[i["pc"].id]["speed_derived"])) if i["native"] else _cell(na=True) for i in info])
        add("Build", "Edges", [_cell(chips=[e for e in (derived[i["pc"].id]["minor_edge"], derived[i["pc"].id]["major_edge"]) if e])
                               if i["native"] else _cell(na=True) for i in info])
        add("Build", "Cyberware", [
            _cell(chips=[str(c.get("name")) for c in derived[i["pc"].id]["cyberware"] if isinstance(c, dict) and c.get("name")])
            if i["native"] else _cell(na=True) for i in info])

    # custom-sheet members: resource tracks (minus the vital shown above), then the system's own comparison fields
    track_rows, field_rows = {}, {}
    for idx, i in enumerate(info):
        if i["native"]:
            continue
        for t in i["v"]["resources"]:
            if t["id"] == i["v"]["hp_id"]:
                continue
            track_rows.setdefault(t["label"], {})[idx] = _cell(f"{t['current']} / {t['max']}")
        if i["tpl"] is not None:
            for title, fields in roster_field_groups(i["tpl"]):
                for f in fields:
                    if f.get("type") == "resource":
                        continue  # already a track row
                    txt = field_value_text(f, i["cf"])
                    field_rows.setdefault((title, short_label(f.get("label") or f.get("id"))), {})[idx] = (
                        _cell(txt) if txt else _cell(na=True))
    for label, cells in track_rows.items():
        add("Resources", label, [cells.get(n, _cell(na=True)) for n in range(len(info))])
    for (title, label), cells in field_rows.items():
        add(title, label, [cells.get(n, _cell(na=True)) for n in range(len(info))])
    return {"columns": columns, "groups": groups}


@router.get("/parties/{party_id}/roster", response_class=HTMLResponse)
def party_roster(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Members side by side: stats, effective maxima, CA/Speed, edges, cyberware,
    conditions, custom-sheet resource tracks; prints cleanly. The full tier (GM,
    or an assistant with Parties:edit in their own active world) always sees it;
    everyone else needs the world's "players see the party" switch ON as well as
    read access to Parties. GM companions are shown to the GM only."""
    world, worlds = get_world_ctx(request, db, active_world)
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party or not world_row_visible(request, db, party.world_id, "parties"):
        raise HTTPException(404)
    party_world = world if (world and world.id == party.world_id) else db.get(World, party.world_id)
    is_gm = _viewer_is_gm(request)
    full_tier = is_gm or (world is not None and world.id == party.world_id and _can_manage_parties(request, party_world))
    if not (full_tier or getattr(party_world, "players_see_party", False)):
        raise HTTPException(404)
    pc_ids = party_member_ids(party)
    member_pcs = (db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids),
                                                   PlayerCharacter.world_id == party.world_id).all() if pc_ids else [])
    companions = []
    if is_gm:
        ent_ids = [i for i in party_member_ids_raw(party.member_entity_ids_json)]
        companions = db.query(Entity).filter(Entity.id.in_(ent_ids)).all() if ent_ids else []
    return templates.TemplateResponse("parties/roster.html", {
        "request": request, "world": world, "worlds": worlds, "party": party,
        "table": _roster_table(db, member_pcs), "companions": companions, "is_gm": is_gm,
    })


@router.get("/parties/{party_id}/export.ndc")
def party_export_ndc(party_id: int, request: Request, db: Session = Depends(get_db)):
    """GM-only bundle: every member character in the same .ndc JSON array a
    single-character export uses, so the whole party imports into the Android
    app / desktop editor in one go."""
    if not _viewer_is_gm(request):
        raise HTTPException(403)
    party = db.get(Party, party_id)
    if not party:
        raise HTTPException(404)
    pc_ids = party_member_ids(party)
    pcs = (db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids),
                                            PlayerCharacter.world_id == party.world_id)
           .order_by(PlayerCharacter.name).all() if pc_ids else [])
    # .ndc is the N&D app's format: members on a custom sheet can't be expressed in it
    native_pcs = [pc for pc in pcs if pc_maxima(pc)["native"]]
    skipped = [pc.name for pc in pcs if not pc_maxima(pc)["native"]]
    payload = json.dumps([_pc_to_ndc_dict(pc) for pc in native_pcs], ensure_ascii=False, indent=2)
    headers = {"Content-Disposition": f'attachment; filename="{_safe_export_filename(party.name or "party")}.ndc"'}
    if skipped:
        headers["X-Skipped-Custom-Sheets"] = ", ".join(skipped).encode("ascii", "ignore").decode() or "custom sheets"
    return StreamingResponse(io.BytesIO(payload.encode("utf-8")), media_type="application/json", headers=headers)


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
