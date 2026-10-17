import json
import uuid

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import combat_rules, combat_state, combat_turns, live
from ..database import get_db
from ..deps import get_world_ctx, paginate, world_can_view_section, world_row_visible
from ..models import CombatSession, Entity, GameSession, PlayerCharacter, SheetTemplate, World
from ..pc_stats import clean_conditions, pc_maxima
from ..sheet_systems import hp_track, parse_custom_fields, resource_tracks, system_meta, template_fields
from ..templating import templates

router = APIRouter()

_COMBATANT_KINDS = ("character", "creature")


def _pc_conditions(pc: PlayerCharacter) -> list:
    try:
        conds = json.loads(pc.conditions_json or "[]")
    except ValueError:
        return []
    return [c for c in conds if isinstance(c, str)] if isinstance(conds, list) else []


def _vital_track(db, pc: PlayerCharacter):
    """(custom_fields dict, vital track) for a character on a custom system whose
    template declares an HP resource (Health / Flesh ...), else None — the
    combatant's HP comes from that track since such a character has no real HP
    column (see app/sheet_systems.py)."""
    if db is None or pc_maxima(pc)["native"] or not pc.sheet_template_id:
        return None
    tpl = db.get(SheetTemplate, pc.sheet_template_id)
    if not tpl:
        return None
    cf = parse_custom_fields(pc.custom_fields_json)
    track = hp_track(template_fields(tpl), cf, system_meta(tpl))
    return (cf, track) if track else None


def _extra_tracks(db, pc: PlayerCharacter) -> dict:
    """Every resource track of a custom-system character OTHER than its vital one (Asterion's Spark Shield and Ichor, Hunt's Stamina ...):
    {"sparkShield": {"cur": 3, "max": 3, "label": "Spark Shield"}} - so a fight can soak with the shield and spend Ichor, and a sync can
    write them back to the sheet."""
    m = pc_maxima(pc)
    if m["native"]:
        # a native Neon & Dragons / Worm sheet keeps Physical and Mental Points in columns (the Asterion-engine conversions spend them)
        return {"pp": {"cur": pc.pp_current or 0, "max": m["pp"], "label": "PP"},
                "mp": {"cur": pc.mp_current or 0, "max": m["mp"], "label": "MP"}}
    if db is None or not pc.sheet_template_id:
        return {}
    tpl = db.get(SheetTemplate, pc.sheet_template_id)
    if not tpl:
        return {}
    meta = system_meta(tpl)
    cf = parse_custom_fields(pc.custom_fields_json)
    out = {}
    for t in resource_tracks(template_fields(tpl), cf, meta):
        if meta.get("hp") and t["id"] == meta["hp"]:
            continue
        mx = t["max"] or 0
        out[t["id"]] = {"cur": t["current"] if t["current"] is not None else mx, "max": mx, "label": t["label"]}
    return out


def pc_to_combatant(pc: PlayerCharacter, db=None) -> dict:
    m = pc_maxima(pc)
    vital = _vital_track(db, pc)
    if vital:
        hp, max_hp = vital[1]["current"], vital[1]["max"]
    else:
        hp, max_hp = pc.current_hp or 0, m["hp"]
    return {
        "res": _extra_tracks(db, pc),
        "id": "c-" + uuid.uuid4().hex[:8],
        "name": pc.name,
        "source": "pc",
        "pc_id": pc.id,
        "entity_id": None,
        "initiative": 0,
        "max_hp": max_hp,
        "hp": hp,
        "max_shock": m["shock"],
        "shock": pc.shock_current or 0,
        "armor": 0,
        "conditions": _pc_conditions(pc),
        "notes": "",
    }


def entity_to_combatant(entity: Entity) -> dict:
    fields = json.loads(entity.custom_fields_json or "{}")
    def _int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    # the creature templates name it differently (health / hp / flesh ...); 5 only when the entity says nothing at all
    health = next((_int(fields[k], 5) for k in ("health", "hp", "flesh", "max_hp", "max_health", "hit_points") if fields.get(k) not in (None, "")), 5)
    armor = _int(fields.get("armor"), 0)
    return {
        "id": "c-" + uuid.uuid4().hex[:8],
        "name": entity.name,
        "source": "entity",
        "pc_id": None,
        "entity_id": entity.id,
        "initiative": 0,
        "max_hp": health,
        "hp": health,
        "max_shock": 0,
        "shock": 0,
        "armor": armor,
        "conditions": [],
        "notes": "",
    }


def manual_combatant(name: str) -> dict:
    return {
        "id": "c-" + uuid.uuid4().hex[:8], "name": name or "Combatant", "source": "manual",
        "pc_id": None, "entity_id": None, "initiative": 0,
        "max_hp": 10, "hp": 10, "max_shock": 0, "shock": 0, "armor": 0,
        "conditions": [], "notes": "",
    }


def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _candidates(db: Session, world_id: int):
    pcs = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world_id).order_by(PlayerCharacter.name).all()
    entities = db.query(Entity).filter(
        Entity.world_id == world_id, Entity.kind.in_(_COMBATANT_KINDS)
    ).order_by(Entity.name).all()
    pc_payload = []
    for pc in pcs:
        m = pc_maxima(pc)
        vital = _vital_track(db, pc)
        pc_payload.append({
            "id": pc.id, "name": pc.name,
            "max_hp": vital[1]["max"] if vital else m["hp"],
            "hp": vital[1]["current"] if vital else (pc.current_hp or 0),
            "max_shock": m["shock"], "shock": pc.shock_current or 0,
            "conditions": _pc_conditions(pc),
            "portrait_url": pc.portrait_url or "",
            "res": _extra_tracks(db, pc),
        })
    entity_payload = []
    for e in entities:
        prefill = entity_to_combatant(e)
        entity_payload.append({
            "id": e.id, "name": e.name, "kind": e.kind,
            "max_hp": prefill["max_hp"], "armor": prefill["armor"],
            "image_url": e.image_url or "",
        })
    return pcs, entities, pc_payload, entity_payload


def _is_gm(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.is_gm)


def _scoped(db: Session, request: Request, active_world, combat_id: int):
    """(world, encounter) for a route that CHANGES an encounter: the encounter must belong to the active world."""
    world, _ = get_world_ctx(request, db, active_world)
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not world or not cs or cs.world_id != world.id:
        raise HTTPException(404)
    return world, cs


def default_ruleset(db: Session, world_id: int) -> str:
    """The rule system a world's characters play: Hunt or Asterion if any character's sheet is one of those, else Neon & Dragons."""
    slugs = []
    for pc in db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world_id).all():
        tpl = db.get(SheetTemplate, pc.sheet_template_id) if pc.sheet_template_id else None
        slugs.append(tpl.slug if tpl is not None else None)
    return combat_rules.ruleset_for_slugs(slugs) or "nd"


def _full_state(cs: CombatSession) -> dict:
    """Everything the GM's tracker holds for this encounter (never sent to a player)."""
    combatants = combat_turns.load(cs)
    try:
        log = json.loads(cs.log_json or "[]")
    except ValueError:
        log = []
    return {
        "combatants": combatants, "round_num": cs.round_num or 1, "active_idx": cs.active_idx or 0,
        "active_id": combat_turns.active_combatant_id(cs, combatants), "rev": cs.rev or 0, "name": cs.name,
        "ruleset": cs.ruleset or "", "turn": combat_turns.turn_state(cs), "log": log if isinstance(log, list) else [],
    }


def _player_state(cs: CombatSession) -> dict:
    """What a player may know of an encounter (combat_state.player_view). `combatants` is only a list of ids in turn order with their
    initiative - enough for the map pages, which mark the active token - and carries no names, numbers or notes."""
    combatants = combat_turns.load(cs)
    fluid = combat_turns.is_fluid(cs)
    order = list(range(len(combatants))) if fluid else combat_turns.order(combatants)
    ts = combat_turns.turn_state(cs)
    active_id = combat_turns.active_combatant_id(cs, combatants)
    view = combat_state.player_view(combatants, order, active_id, ts["acted"] if fluid else (), ts["picker"] if fluid else None)
    return {
        "rows": view["rows"], "active_id": view["active_id"], "picker": view["picker"], "round_num": cs.round_num or 1,
        "rev": cs.rev or 0, "name": cs.name, "ruleset": cs.ruleset or "", "fluid": fluid,
        "combatants": [{"id": combatants[i].get("id"), "initiative": combatants[i].get("initiative", 0)} for i in order],
        "active_idx": cs.active_idx or 0,
    }


@router.get("/combat", response_class=HTMLResponse)
def combat_list(request: Request, page: int = 1, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world_can_view_section(request, world, "combat"):
        raise HTTPException(403)
    base_q = db.query(CombatSession).filter(
        CombatSession.world_id == (world.id if world else 1)
    ).order_by(CombatSession.updated_at.desc())
    sessions, page, total_pages = paginate(base_q, page)
    combatant_counts = {s.id: len(combat_turns.load(s)) for s in sessions}
    return templates.TemplateResponse("combat/list.html", {
        "request": request, "world": world, "worlds": worlds,
        "sessions": sessions, "combatant_counts": combatant_counts,
        "page": page, "total_pages": total_pages,
        "rule_labels": {k: v["label"] for k, v in combat_rules.RULESETS.items()},
    })


@router.post("/combat/new")
def combat_create(request: Request, name: str = Form("New Encounter"), ruleset: str = Form(""),
                  db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    rules = ruleset if ruleset in combat_rules.RULESET_IDS else default_ruleset(db, world_id)
    cs = CombatSession(world_id=world_id, name=name.strip()[:256] or "New Encounter", ruleset=rules)
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return RedirectResponse(f"/combat/{cs.id}", status_code=303)


@router.get("/combat/{combat_id}", response_class=HTMLResponse)
def combat_detail(combat_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not cs or not world_row_visible(request, db, cs.world_id, "combat"):
        raise HTTPException(404)
    if not _is_gm(request):
        # a player gets the read-only view, built from the sanitized state - never the GM's page or its data
        return templates.TemplateResponse("combat/player.html", {
            "request": request, "world": world, "worlds": worlds, "combat": cs,
            "state_json": json.dumps(_player_state(cs)),
            "rules_json": json.dumps({k: {"label": v["label"], "turns": v["turns"], "hp_label": v["hp_label"]} for k, v in combat_rules.RULESETS.items()}),
        })
    if not cs.ruleset:                       # an encounter from before rulesets existed: settle on the party's system once
        cs.ruleset = default_ruleset(db, cs.world_id)
        db.commit()
    _, _, pc_payload, entity_payload = _candidates(db, cs.world_id)
    game_sessions = db.query(GameSession).filter(GameSession.world_id == cs.world_id).order_by(GameSession.session_num.desc()).all()
    linked_session = db.query(GameSession).filter(GameSession.id == cs.game_session_id).first() if cs.game_session_id else None
    state = _full_state(cs)
    return templates.TemplateResponse("combat/detail.html", {
        "request": request, "world": world, "worlds": worlds, "combat": cs,
        "combatants_json": json.dumps(state["combatants"]),
        "state_json": json.dumps(state),
        "rules_json": json.dumps(combat_rules.RULESETS),
        "pc_payload_json": json.dumps(pc_payload),
        "entity_payload_json": json.dumps(entity_payload),
        "game_sessions": game_sessions, "linked_session": linked_session,
    })


@router.get("/api/combat/recent")
def combat_recent(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Recent combat sessions as JSON for the GM Cockpit's combat panel
    picker. GM-only via middleware (not in _is_player_safe); the cockpit is
    a GM surface."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    rows = (
        db.query(CombatSession)
        .filter(CombatSession.world_id == world.id)
        .order_by(CombatSession.created_at.desc(), CombatSession.id.desc())
        .limit(10)
        .all()
    )
    return {"combats": [{
        "id": cs.id, "name": cs.name, "round_num": cs.round_num or 1,
    } for cs in rows]}


@router.get("/api/combat/{combat_id}/state")
def combat_get_state(combat_id: int, request: Request, db: Session = Depends(get_db)):
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not cs or not world_row_visible(request, db, cs.world_id, "combat"):
        raise HTTPException(404)
    return _full_state(cs) if _is_gm(request) else _player_state(cs)


@router.post("/combat/{combat_id}/state")
async def combat_save_state(combat_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Save the tracker. The body is checked and cleaned (combat_state.normalize_state). If it carries the `rev` the tracker last saw and
    the encounter has moved on since (a player rolled initiative, the map pushed HP, another tab saved), nothing is written and the
    answer is 409 with the current state, so the page can merge its edits onto it instead of overwriting the others."""
    _, cs = _scoped(db, request, active_world, combat_id)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "The body must be JSON.")
    try:
        state = combat_state.normalize_state(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    sent_rev = body.get("rev") if isinstance(body, dict) else None
    if isinstance(sent_rev, int) and sent_rev != (cs.rev or 0):
        return JSONResponse({"detail": "This encounter changed while you were editing it.", "state": _full_state(cs)}, status_code=409)
    cs.combatants_json = json.dumps(state["combatants"])
    cs.round_num = state["round_num"]
    cs.active_idx = state["active_idx"]
    if "name" in state:
        cs.name = state["name"]
    if "ruleset" in state:
        cs.ruleset = state["ruleset"]
    if "turn" in state:
        combat_turns.set_turn_state(cs, state["turn"])
    if "log" in state:
        cs.log_json = json.dumps(state["log"])
    combat_turns.touch(cs)
    db.commit()
    live.touch(cs.world_id)
    return {"ok": True, "rev": cs.rev}


@router.post("/combat/{combat_id}/delete")
def combat_delete(combat_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _, cs = _scoped(db, request, active_world, combat_id)
    db.delete(cs)
    db.commit()
    return RedirectResponse("/combat", status_code=303)


@router.post("/combat/{combat_id}/link-session")
async def combat_link_session(combat_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, cs = _scoped(db, request, active_world, combat_id)
    form = await request.form()
    session_id = form.get("game_session_id")
    if session_id:
        gs = db.query(GameSession).filter(GameSession.id == _as_int(session_id), GameSession.world_id == world.id).first()
        if not gs:
            raise HTTPException(404)
        cs.game_session_id = gs.id
    else:
        cs.game_session_id = None
    db.commit()
    return RedirectResponse(f"/combat/{combat_id}", status_code=303)


@router.post("/combat/{combat_id}/unlink-session")
def combat_unlink_session(combat_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _, cs = _scoped(db, request, active_world, combat_id)
    cs.game_session_id = None
    db.commit()
    return RedirectResponse(f"/combat/{combat_id}", status_code=303)


@router.post("/api/combat/{combat_id}/sync-characters")
def combat_sync_characters(combat_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Push the encounter's numbers for every player character onto their sheets: HP, Shock, conditions, and - for a custom system -
    every other resource track (Spark Shield, Ichor, Stamina ...)."""
    _, cs = _scoped(db, request, active_world, combat_id)
    combatants = combat_turns.load(cs)
    synced, skipped = [], []
    for c in combatants:
        if c.get("source") != "pc" or not c.get("pc_id"):
            skipped.append(c.get("name"))
            continue
        pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == c["pc_id"], PlayerCharacter.world_id == cs.world_id).first()
        if not pc:
            skipped.append(c.get("name"))
            continue
        m = pc_maxima(pc)
        vital = _vital_track(db, pc)
        if vital:
            # Custom system: HP lives in the template's vital track, not a column.
            cf, track = vital
            hp = max(0, min(track["max"], _as_int(c.get("hp"), track["current"])))
            cf[f"{track['id']}_current"] = hp
            tracks = {t["id"]: t for t in resource_tracks(template_fields(db.get(SheetTemplate, pc.sheet_template_id)), cf)}
            for tid, t in (c.get("res") or {}).items():
                if tid in tracks and isinstance(t, dict):
                    top = tracks[tid]["max"]
                    val = max(0, _as_int(t.get("cur"), 0))
                    cf[f"{tid}_current"] = min(top, val) if top else val
            pc.custom_fields_json = json.dumps(cf)
        else:
            max_hp = m["hp"] or _as_int(c.get("max_hp"), 0)
            pc.current_hp = max(0, min(max_hp, _as_int(c.get("hp"), pc.current_hp or 0)))
            res = c.get("res") if isinstance(c.get("res"), dict) else {}
            for key, top, col in (("pp", m["pp"], "pp_current"), ("mp", m["mp"], "mp_current")):
                if isinstance(res.get(key), dict):
                    setattr(pc, col, max(0, min(top, _as_int(res[key].get("cur"), getattr(pc, col) or 0))) if top else max(0, _as_int(res[key].get("cur"), 0)))
        pc.shock_current = max(0, min(m["shock"], _as_int(c.get("shock"), pc.shock_current or 0)))
        pc.conditions_json = json.dumps(clean_conditions(c.get("conditions")))        # the sheet shows each condition once (stacks live in the fight)
        synced.append(pc.name)
    db.commit()
    live.touch(cs.world_id)
    return {"synced": synced, "skipped": skipped}


# ── AI tactics assist ────────────────────────────────────────────────────────
# Reads the CURRENT combat state (combatants, HP, round — no dice, no
# math) and proposes NPC tactics + a dramatic beat for the GM to use or
# ignore. Deterministic mechanics untouched; this is a suggestion panel.

@router.post("/combat/{combat_id}/ai-tactics")
async def combat_ai_tactics(combat_id: int, request: Request,
                            db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Suggest NPC tactics and a dramatic beat from the live combat state.
    Returns a draft ({tactics: [{who, move}], beat}) — writes nothing, so
    the GM stays the only author of the encounter. GM-only (in-handler,
    not allowlisted)."""
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    cs = db.query(CombatSession).filter(
        CombatSession.id == combat_id, CombatSession.world_id == world.id).first()
    if not cs:
        raise HTTPException(404)
    from .. import ai as _ai
    if not _ai.effective_llm_api_key():
        raise HTTPException(400, "No AI backend configured — set UNSLOTH_API_KEY (Settings → System)")
    try:
        combatants = json.loads(cs.combatants_json or "[]")
    except ValueError:
        combatants = []
    roster = []
    for c in combatants[:20]:
        if not isinstance(c, dict):
            continue
        hp = c.get("hp")
        roster.append(f"- {c.get('name', '?')}: hp {hp if hp is not None else '?'}"
                      + (f", {c.get('notes')}" if c.get("notes") else ""))
    if not roster:
        raise HTTPException(400, "No combatants in this fight yet")

    system = (
        "You are a tactically sharp TTRGM assistant. From the combat roster and round, "
        "suggest how the NPCs/monsters should act THIS round and one dramatic beat to keep "
        "the fight exciting. Reply ONLY with JSON, no fences:\n"
        '{"tactics": [{"who": str (combatant name), "move": str (one concrete action, '
        '<=160 chars)}], "beat": str (one dramatic twist/turn of the battle, <=200 chars)}\n'
        "Rules: 2-4 tactics, only for NON-player combatants (names come from the roster; "
        "PCs are the players' to run); exploit positioning/statuses mentioned; the beat "
        "should escalate, not end the fight unless it's clearly nearly over."
    )
    try:
        raw = await _ai.generate_chat(
            [{"role": "user", "content": f"Fight: {cs.name}\nRound {cs.round_num}\n"
                                         + "\n".join(roster)}],
            system=system, options={"num_predict": 700}, think=False,
        )
    except Exception as exc:
        raise HTTPException(502, f"AI call failed: {exc}") from exc
    if _ai.is_failure_sentinel(raw or ""):
        raise HTTPException(502, raw)
    from .boards_generate import _extract_json_object
    data = _extract_json_object(raw)
    tactics = [t for t in (data or {}).get("tactics", [])
               if isinstance(t, dict) and t.get("who") and t.get("move")][:6]
    if not tactics and not (data or {}).get("beat"):
        raise HTTPException(502, "The AI reply wasn't usable — try again.")
    return {"tactics": [{"who": str(t["who"])[:80], "move": str(t["move"])[:200]} for t in tactics],
            "beat": str((data or {}).get("beat") or "")[:300]}
