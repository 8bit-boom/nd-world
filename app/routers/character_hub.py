"""The player's character hub API — the data behind the tabs on a character's
page (Journey / Quests / Notes / World / Schedule) so the page is a player's
home base, not just a sheet.

Access has two levels, both enforced in this module because /api/characters/
is a blanket player-safe prefix in app/main.py's _is_player_safe:

* Notes and every write (journal/goal entries, loot claims) are OWNER-ONLY:
  the caller must be the account that owns the PlayerCharacter
  (PlayerCharacter.owner_user_id == caller), full stop — _owned_pc(). A GM who
  does not own the character gets the same 404 as any other player; the
  diary and the GM's private notes to that player are theirs alone.
* The shared READ tabs (Quests & goals, Known world, Schedule) also open to a
  global GM — _hub_pc() — who sees them from the PLAYER's point of view (the
  character's owner's section levels and reveals, never the GM's unfiltered
  world), read-only. Assistants are not GMs here: they see what players see.

The hub shows a player what they could already reach elsewhere, gathered in
one place — it never widens visibility. Quests honor the same
visible_to_players flag and the per-world section access matrix the /quests
page does; the calendar honors its section level; entities go through
deps.filter_visible_entities (the one filter every player-facing entity list
must use); GM-only [gmonly] text is stripped before anything leaves the
server. Evaluated as the PLAYER role for the character's own world (not the
request's active world), since this is "what the player sees".
"""
import json
import re
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import auth, char_extras, combat_turns, live
from ..database import get_db
from ..deps import filter_visible_entities, with_world, world_can_view_section, world_section_access
from ..models import (
    CalendarEvent, CharacterJournalEntry, CombatSession, Entity, GameSession, Party, PlayerCharacter,
    PrivateNote, Quest, SessionPlanVote, World, WorldCalendar, entity_player_access,
)
from ..nav_menus import build_catalog
from ..party_refs import load_loot, parties_for_pc
from ..pc_stats import pc_maxima
from ..rendering import strip_gm_only, strip_md
from .calendar import _default_config, date_label
from .schedule import next_confirmed, polls_waiting

router = APIRouter()

_MAX_TITLE = 200
_MAX_BODY = 20000
_KINDS = ("journal", "goal")
GOAL_STATUSES = ("active", "achieved", "failed", "dropped")
_LIST_CAP = 25


# ── Access ───────────────────────────────────────────────────────────────────

def _owned_pc(request: Request, db: Session, pc_id: int):
    """(user, pc, world) for a character the caller OWNS, else 404 — the same
    answer for "no such character" and "not yours" so ids can't be probed."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401)
    pc = db.get(PlayerCharacter, pc_id)
    if not pc or pc.owner_user_id != user.id:
        raise HTTPException(404)
    world = db.get(World, pc.world_id)
    if not world or not auth.user_can_access_world(db, user, world):
        raise HTTPException(404)
    return user, pc, world


def _hub_pc(request: Request, db: Session, pc_id: int):
    """(user, pc, world, is_owner) for the shared READ tabs: the owning player, or
    a global GM looking in. Anyone else gets the same 404 as "no such character"."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401)
    pc = db.get(PlayerCharacter, pc_id)
    is_owner = bool(pc and pc.owner_user_id == user.id)
    if not pc or not (is_owner or user.is_gm):
        raise HTTPException(404)
    world = db.get(World, pc.world_id)
    if not world or not auth.user_can_access_world(db, user, world):
        raise HTTPException(404)
    return user, pc, world, is_owner


def _player_level(world, section_id: str) -> str:
    """The PLAYER role's level ("none"/"read"/"edit") for a section of this
    world — explicit player role, independent of the request's active world."""
    entry = world_section_access(world).get(section_id)
    return entry["player"] if entry else "none"


def _party_ids(db: Session, pc: PlayerCharacter) -> list:
    """Ids of the parties in this character's world that include it."""
    return [p.id for p in parties_for_pc(db, pc.world_id, pc.id)]


def _party_sessions(db: Session, pc: PlayerCharacter, party_ids: list) -> list:
    if not party_ids:
        return []
    return (
        db.query(GameSession)
        .filter(GameSession.world_id == pc.world_id, GameSession.party_id.in_(party_ids))
        .order_by(GameSession.session_num.desc()).limit(60).all()
    )


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    return body


def delete_character_journal(db: Session, pc_id: int) -> None:
    """Drop every journal/goal row of a character being deleted (character_
    delete / retire-to-NPC in routers/characters.py). SQLite's foreign_keys
    pragma is off here, so nothing cascades on its own."""
    db.query(CharacterJournalEntry).filter(
        CharacterJournalEntry.character_id == pc_id
    ).delete(synchronize_session=False)
    char_extras.delete_for_character(db, pc_id)


# ── Journey tab (server-rendered) + in-place loot claiming ───────────────────

def _loot_view(loot: list, pc_id: int, names: dict) -> list:
    """The stash as the hub shows it: each item with whether it's claimed, by
    whom (names of party members), and whether this character holds a claim."""
    return [{
        "lid": i["lid"], "name": i["name"], "qty": i["qty"], "notes": i["notes"],
        "claimed": bool(i["claimed_by"]), "mine": pc_id in i["claimed_by"],
        "claimers": [names[c] for c in i["claimed_by"] if c in names],
    } for i in loot]


def journey_context(db: Session, request: Request, pc: PlayerCharacter, world) -> dict:
    """Everything the Journey tab renders for the OWNER of `pc`: their party, its
    stash (with this character's claims), teammate roster, recent XP awards and —
    only if the viewer may see the Sessions section — recent sessions. Teammates
    are PlayerCharacters only; GM companions never appear here."""
    ctx = {"hub_party": None, "hub_loot": [], "hub_roster": [], "hub_xp_ledger": [], "hub_sessions": []}
    mine = parties_for_pc(db, pc.world_id, pc.id)
    if not mine:
        return ctx
    party = ctx["hub_party"] = mine[0]
    try:
        member_ids = [i for i in json.loads(party.member_pc_ids_json or "[]") if isinstance(i, int)]
    except ValueError:
        member_ids = []
    members = (db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(member_ids),
                                                PlayerCharacter.world_id == pc.world_id).all()
               if member_ids else [])
    ctx["hub_loot"] = _loot_view(load_loot(party), pc.id, {m.id: m.name for m in members})
    from .parties import _member_vitals  # lazy: parties imports characters, which imports this module
    for v in _member_vitals(db, [m for m in members if m.id != pc.id]):
        by_id = next(m for m in members if m.id == v["id"])
        ctx["hub_roster"].append({
            "id": v["id"], "name": v["name"], "native": v["native"],
            "level": v["level"] if v["native"] else None,
            "line": " · ".join(x for x in (by_id.race, by_id.char_class) if x),
            "hp": v["hp"] or 0, "max_hp": v["max_hp"], "hp_label": v["hp_label"],
            "down": v["down"], "conditions": v["conditions"],
        })
    try:
        ledger = json.loads(party.xp_json or "[]")
    except ValueError:
        ledger = []
    ctx["hub_xp_ledger"] = list(reversed(ledger))[:5]
    if world_can_view_section(request, world, "sessions"):
        ctx["hub_sessions"] = (
            db.query(GameSession).filter(GameSession.party_id == party.id)
            .order_by(GameSession.session_num.desc()).limit(5).all()
        )
    return ctx


@router.post("/api/characters/{pc_id}/hub/loot")
async def hub_loot_claim(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Claim / unclaim a party-stash item for the caller's OWN character from the
    Journey tab. Owner-only (a body `pc_id` is ignored — a claim is always for
    the character in the URL). Deliberately not tied to the Parties section
    level: that gate is for browsing/editing parties, while the Journey tab
    already shows a player their own party's stash. A claim is only a marker;
    handing the item over (give) stays on the party page."""
    user, pc, world = _owned_pc(request, db, pc_id)
    body = await _json_body(request)
    action, lid = body.get("action"), body.get("lid")
    if action not in ("claim", "unclaim"):
        raise HTTPException(400, "action must be claim or unclaim")
    if not isinstance(lid, str) or not lid:
        raise HTTPException(400, "lid is required")
    parties = parties_for_pc(db, pc.world_id, pc.id)
    if not parties:
        raise HTTPException(404, "This character is not in a party")
    for party in parties:
        loot = load_loot(party)
        item = next((i for i in loot if i["lid"] == lid), None)
        if item is not None:
            break
    else:
        raise HTTPException(409, "That loot item no longer exists — refresh the page.")
    if action == "claim" and pc.id not in item["claimed_by"]:
        item["claimed_by"].append(pc.id)
    elif action == "unclaim" and pc.id in item["claimed_by"]:
        item["claimed_by"].remove(pc.id)
    party.loot_json = json.dumps(loot)
    db.commit()
    live.touch(party.world_id)
    names = {m.id: m.name for m in db.query(PlayerCharacter).filter(
        PlayerCharacter.id.in_([c for i in loot for c in i["claimed_by"]] or [0])).all()}
    return {"loot": _loot_view(loot, pc.id, names)}


# ── Serializers ──────────────────────────────────────────────────────────────

def _entry_out(e: CharacterJournalEntry, session_labels: dict) -> dict:
    return {
        "id": e.id, "kind": e.kind, "title": e.title or "", "body": e.body or "",
        "status": e.status or "", "session_id": e.session_id,
        "session": session_labels.get(e.session_id),
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def _session_labels(sessions: list) -> dict:
    return {s.id: f"#{s.session_num} {s.title}" for s in sessions}


def _entity_out(e: Entity, world) -> dict:
    return {
        "id": e.id, "name": e.name, "kind": e.kind,
        "summary": strip_gm_only(e.summary or ""),
        "href": with_world(f"/entity/{e.id}", world),
    }


def _excerpt(text: str, limit: int = 280) -> str:
    plain = strip_md(re.sub(r"<[^>]+>", " ", text or ""))
    return plain if len(plain) <= limit else plain[: limit - 1].rstrip() + "…"


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ── Read tabs ────────────────────────────────────────────────────────────────

@router.get("/api/characters/{pc_id}/hub/quests")
def hub_quests(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Quests tab: the player-visible quests (same visible_to_players flag and
    section matrix as /quests — a world that closes Quests to players shows
    none here), with the character's own party's quests flagged, plus this
    character's personal goals."""
    user, pc, world, _ = _hub_pc(request, db, pc_id)
    level = _player_level(world, "quests")
    party_ids = _party_ids(db, pc)
    quests = []
    if level != "none":
        rows = (
            db.query(Quest)
            .filter(Quest.world_id == world.id, Quest.visible_to_players.isnot(False))
            .order_by(Quest.title).all()
        )
        party_names = {p.id: p.name for p in db.query(Party).filter(Party.world_id == world.id).all()}
        subs_total: dict = {}
        subs_done: dict = {}
        for q in rows:
            if q.parent_id:
                subs_total[q.parent_id] = subs_total.get(q.parent_id, 0) + 1
                if (q.status or "") == "complete":
                    subs_done[q.parent_id] = subs_done.get(q.parent_id, 0) + 1
        for q in rows:
            if q.parent_id:
                continue
            quests.append({
                "id": q.id, "title": q.title, "status": q.status or "active",
                "category": q.category or "main", "summary": q.summary or "",
                "party": party_names.get(q.assigned_party_id),
                "mine": q.assigned_party_id in party_ids,
                "subs_done": subs_done.get(q.id, 0), "subs_total": subs_total.get(q.id, 0),
                "href": with_world(f"/quests/{q.id}", world),
            })
        quests.sort(key=lambda d: (d["status"] != "active", not d["mine"], d["title"].lower()))
    goals = (
        db.query(CharacterJournalEntry)
        .filter(CharacterJournalEntry.character_id == pc.id, CharacterJournalEntry.kind == "goal")
        .order_by(CharacterJournalEntry.created_at.desc()).all()
    )
    goals.sort(key=lambda g: g.status != "active")
    return {
        "section": level,
        "quests": quests,
        "quests_href": with_world("/quests", world) if level != "none" else None,
        "goals": [_entry_out(g, {}) for g in goals],
        "goal_statuses": list(GOAL_STATUSES),
    }


@router.get("/api/characters/{pc_id}/hub/notes")
def hub_notes(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Notes tab: the GM's private notes to this player (excerpts + a link to
    the full thread) and this character's own journal."""
    user, pc, world = _owned_pc(request, db, pc_id)
    notes = (
        db.query(PrivateNote)
        .filter(PrivateNote.world_id == world.id, PrivateNote.player_user_id == user.id)
        .order_by(PrivateNote.updated_at.desc()).limit(50).all()
    )
    sessions = _party_sessions(db, pc, _party_ids(db, pc))
    labels = _session_labels(sessions)
    entries = (
        db.query(CharacterJournalEntry)
        .filter(CharacterJournalEntry.character_id == pc.id, CharacterJournalEntry.kind == "journal")
        .order_by(CharacterJournalEntry.created_at.desc()).all()
    )
    return {
        "gm_notes": [{
            "id": n.id, "title": n.title or "", "excerpt": _excerpt(n.content),
            "updated_at": n.updated_at.isoformat() if n.updated_at else None,
        } for n in notes],
        "thread_href": with_world(f"/worlds/{world.id}/notes/{user.id}", world) if notes else None,
        "journal": [_entry_out(e, labels) for e in entries],
        "sessions": [{"id": s.id, "label": labels[s.id]} for s in sessions],
    }


@router.get("/api/characters/{pc_id}/hub/world")
def hub_world(pc_id: int, request: Request, q: str = "", db: Session = Depends(get_db)):
    """Known-world tab: entities the GM revealed to this player specifically
    (hidden from everyone else), recently updated player-visible ones, and a
    search — through deps.filter_visible_entities for the owner and the same
    rule keyed on the owning player for a GM looking in, scoped to the
    character's world."""
    user, pc, world, is_owner = _hub_pc(request, db, pc_id)
    # Whose eyes: the owner's own, or — for a GM looking in — the owning player's
    # (a GM's unfiltered view would show the very secrets this tab exists to omit).
    viewer_id = pc.owner_user_id
    base = db.query(Entity).filter(Entity.world_id == world.id)
    if is_owner:
        base = filter_visible_entities(base, request)
    elif viewer_id is not None:
        shared_any = db.query(entity_player_access.c.entity_id).filter(entity_player_access.c.user_id == viewer_id)
        base = base.filter(or_(Entity.visible_to_players.isnot(False), Entity.id.in_(shared_any)))
    else:
        base = base.filter(Entity.visible_to_players.isnot(False))
    shared = db.query(entity_player_access.c.entity_id).filter(
        entity_player_access.c.user_id == (viewer_id if viewer_id is not None else -1))
    revealed = (
        base.filter(Entity.visible_to_players.is_(False), Entity.id.in_(shared))
        .order_by(Entity.updated_at.desc()).limit(_LIST_CAP).all()
    )
    term = (q or "").strip()[:80]
    results, recent = [], []
    if term:
        like = _like(term)
        results = (
            base.filter(or_(
                Entity.name.ilike(like, escape="\\"), Entity.aliases.ilike(like, escape="\\"),
                Entity.tags.ilike(like, escape="\\"), Entity.summary.ilike(like, escape="\\"),
            )).order_by(Entity.name).limit(_LIST_CAP).all()
        )
    else:
        recent = (
            base.filter(Entity.visible_to_players.isnot(False))
            .order_by(Entity.updated_at.desc()).limit(12).all()
        )
    return {
        "q": term,
        "revealed": [_entity_out(e, world) for e in revealed],
        "recent": [_entity_out(e, world) for e in recent],
        "results": [_entity_out(e, world) for e in results],
    }


@router.get("/api/characters/{pc_id}/hub/schedule")
def hub_schedule(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Schedule tab: upcoming calendar events pinned to this character or to a
    party it belongs to (honoring the calendar section level), and the next
    sessions its parties have a date for."""
    user, pc, world, _ = _hub_pc(request, db, pc_id)
    party_ids = _party_ids(db, pc)
    level = _player_level(world, "calendar")
    events, today_label = [], None
    if level != "none":
        cal = db.query(WorldCalendar).filter(WorldCalendar.world_id == world.id).first()
        try:
            config = json.loads(cal.config_json or "{}") if cal else {}
        except ValueError:
            config = {}
        config = config or _default_config()
        try:
            current_day = int(config.get("current_day", 1))
        except (TypeError, ValueError):
            current_day = 1

        def _label(day_num: int) -> str:
            return date_label(config, day_num)

        today_label = _label(current_day)
        pinned = [CalendarEvent.character_id == pc.id]
        if party_ids:
            pinned.append(CalendarEvent.party_id.in_(party_ids))
        rows = (
            db.query(CalendarEvent)
            .filter(CalendarEvent.world_id == world.id, CalendarEvent.day >= current_day, or_(*pinned))
            .order_by(CalendarEvent.day).limit(20).all()
        )
        events = [{
            "id": e.id, "title": e.title, "notes": e.notes or "", "color": e.color,
            "day": e.day, "days_away": e.day - current_day, "date": _label(e.day),
        } for e in rows]
    today = date.today()
    upcoming = []
    for s in _party_sessions(db, pc, party_ids):
        try:
            when = date.fromisoformat((s.session_date or "")[:10])
        except ValueError:
            continue
        if when >= today:
            upcoming.append((when, s))
    upcoming.sort(key=lambda t: t[0])
    # The session planner (app/routers/schedule.py) is open to every member of the world: the next confirmed session,
    # and how many open polls this character's PLAYER has not answered yet.
    asker = pc.owner_user_id or user.id
    nxt = next_confirmed(db, world.id)
    next_session = None
    if nxt:
        plan, slot = nxt
        mine = db.query(SessionPlanVote).filter(SessionPlanVote.slot_id == slot.id, SessionPlanVote.user_id == asker).first()
        next_session = {
            "id": plan.id, "title": plan.title, "location": plan.location or "",
            "starts_at": slot.starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "duration_min": plan.duration_min,
            "my": mine.choice if mine else None,
        }
    return {
        "calendar_section": level,
        "calendar_href": with_world("/calendar", world) if level != "none" else None,
        "schedule_href": with_world("/schedule", world),
        "next_session": next_session,
        "polls_waiting": polls_waiting(db, world.id, asker),
        "today": today_label,
        "events": events,
        "sessions": [{
            "id": s.id, "title": s.title, "num": s.session_num, "date": when.isoformat(),
            "href": with_world(f"/sessions/{s.id}", world),
        } for when, s in upcoming[:3]],
    }


# ── Always-visible strip + the places launcher ───────────────────────────────

_COMBAT_FRESH = timedelta(hours=12)      # an encounter untouched for longer than this is not "now"


def _pc_encounter(db: Session, pc: PlayerCharacter):
    """(CombatSession, combatants, index of this character) of the freshest live encounter that includes it, else None."""
    since = datetime.utcnow() - _COMBAT_FRESH
    rows = (db.query(CombatSession).filter(CombatSession.world_id == pc.world_id, CombatSession.updated_at >= since)
            .order_by(CombatSession.updated_at.desc()).limit(8).all())
    for cs in rows:
        combatants = combat_turns.load(cs)
        mine = combat_turns.find_pc(combatants, pc.id)
        if mine is not None:
            return cs, combatants, mine
    return None


def _combat_now(db: Session, pc: PlayerCharacter):
    """Where this character stands in a live encounter, or None. Deliberately minimal: the round, and whether the turn is
    this character's, another player character's (named), or "the enemy" (never named - a GM-run combatant's name can be
    a spoiler)."""
    found = _pc_encounter(db, pc)
    if not found:
        return None
    cs, combatants, mine = found
    turn_order = combat_turns.order(combatants)
    n = len(combatants)
    pos = (cs.active_idx or 0) % n
    cur = combatants[turn_order[pos]]
    nxt = combatants[turn_order[(pos + 1) % n]]
    if cur.get("pc_id") == pc.id:
        turn, name = "me", None
    elif cur.get("source") == "pc":
        turn, name = "pc", str(cur.get("name") or "")[:60]
    else:
        turn, name = "enemy", None
    return {"round": cs.round_num or 1, "turn": turn, "name": name, "next_is_me": nxt.get("pc_id") == pc.id and turn != "me",
            "my_initiative": combat_turns._init(combatants[mine]), "can_roll_initiative": combat_turns._init(combatants[mine]) == 0}


def _speed(pc: PlayerCharacter) -> int:
    """Initiative = Speed (core rules): Dexterity + Intuition."""
    try:
        stats = {str(s.get("id")): int(s.get("value") or 0) for s in json.loads(pc.stats_json or "[]") if isinstance(s, dict)}
    except (ValueError, TypeError):
        stats = {}
    return stats.get("dex", 0) + stats.get("itu", 0)


@router.post("/api/characters/{pc_id}/hub/initiative")
def hub_roll_initiative(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Roll this character's initiative (Speed + d10) into the live encounter and the shared dice log. Owner only, once:
    after that the GM edits it."""
    from . import dice as _dice
    user, pc, world = _owned_pc(request, db, pc_id)
    if not pc_maxima(pc)["native"]:
        raise HTTPException(400, "Initiative is rolled from the sheet's own rules for this system")
    found = _pc_encounter(db, pc)
    if not found:
        raise HTTPException(409, "No encounter is running for this character")
    cs, combatants, mine = found
    if combat_turns._init(combatants[mine]):
        raise HTTPException(409, "Initiative is already rolled - ask your GM to change it")
    speed = _speed(pc)
    roll = _dice._store_roll(db, request, world, "1d10" + (f"+{speed}" if speed else ""), label=f"Initiative \u2014 {pc.name}")
    combat_turns.set_initiative(cs, combatants, mine, max(1, roll.total))
    db.commit()
    live.touch(pc.world_id)
    return {"initiative": roll.total, "roll": _dice._roll_response(roll)}


@router.post("/api/characters/{pc_id}/hub/end-turn")
def hub_end_turn(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Pass the turn on - only while it is this character's turn."""
    _user, pc, _world = _owned_pc(request, db, pc_id)
    found = _pc_encounter(db, pc)
    if not found:
        raise HTTPException(409, "No encounter is running for this character")
    cs, combatants, _mine = found
    if combat_turns.current_index(cs, combatants) != combat_turns.find_pc(combatants, pc.id):
        raise HTTPException(409, "It is not this character's turn")
    combat_turns.advance(cs, combatants)
    db.commit()
    live.touch(pc.world_id)
    return {"ok": True, "round": cs.round_num, "combat": _combat_now(db, pc)}


def _rest_kinds(db: Session, pc: PlayerCharacter) -> list:
    """Which Rests this character's system has: N&D has one (shown as "Rest"); a custom system lists the ones it defines."""
    from ..models import SheetTemplate
    from ..sheet_systems import rest_ops
    if pc_maxima(pc)["native"]:
        return ["long"]
    tpl = db.get(SheetTemplate, pc.sheet_template_id) if pc.sheet_template_id else None
    return [k for k in ("short", "long") if tpl is not None and rest_ops(tpl, k)]


@router.post("/api/characters/{pc_id}/hub/rest")
async def hub_rest(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Take a Rest on the character's own system's rules (what the party Rest does for everyone, for one character):
    N&D = +half PP/MP and all Shock; a custom system = its Rest rules. Returns the snapshot for one-tap Undo."""
    from .parties import apply_pc_rest
    _user, pc, _world = _owned_pc(request, db, pc_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = body.get("kind", "long") if isinstance(body, dict) else "long"
    if kind not in ("short", "long"):
        raise HTTPException(400, "kind must be short or long")
    if kind not in _rest_kinds(db, pc):
        raise HTTPException(400, "This system has no such Rest")
    done = apply_pc_rest(db, pc, kind)
    if done is None:
        raise HTTPException(400, "This system has no Rest rules")
    db.commit()
    live.touch(pc.world_id)
    return {"kind": kind, "result": done[0], "snapshot": done[1]}


@router.post("/api/characters/{pc_id}/hub/rest/undo")
async def hub_rest_undo(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Undo that Rest: the client posts back the snapshot /hub/rest returned (only for this character)."""
    from .parties import restore_pc_snapshot
    _user, pc, _world = _owned_pc(request, db, pc_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    snap = body.get("snapshot") if isinstance(body, dict) else None
    if not isinstance(snap, dict) or snap.get("id") != pc.id:
        raise HTTPException(400, "snapshot must be the one this character's Rest returned")
    restore_pc_snapshot(db, pc, snap)
    db.commit()
    live.touch(pc.world_id)
    return {"ok": True}


@router.get("/api/characters/{pc_id}/hub/now")
def hub_now(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """The strip that stays on screen over every tab: this character's live vitals (HP / Shock / conditions - read-only
    for a GM looking in), whose turn it is in a live encounter, and the next game night."""
    from .parties import _member_vitals          # the one system-aware vitals rule (N&D columns vs a custom template's track)
    user, pc, world, is_owner = _hub_pc(request, db, pc_id)
    v = _member_vitals(db, [pc], resource_limit=None)[0]
    m = pc_maxima(pc)
    try:
        conds = [c for c in json.loads(pc.conditions_json or "[]") if isinstance(c, str)][:12]
    except ValueError:
        conds = []
    me = {
        "id": pc.id, "name": pc.name, "native": v["native"], "system": v["system"],
        "hp": v["hp"], "max_hp": v["max_hp"], "hp_label": v["hp_label"], "temp_hp": v["temp_hp"], "down": v["down"],
        "shock": (pc.shock_current or 0) if v["native"] else None, "shock_max": m["shock"] if v["native"] else None,
        "conditions": conds, "carry": char_extras.carry_info(db, pc) if v["native"] else None, "hp_id": v["hp_id"], "levelup": bool(v["levelup"]),
        # a custom system's other tracks (Stamina, Hunger...), without the vital shown separately as HP
        "resources": [r for r in v["resources"] if r["id"] != v["hp_id"]],
    }
    asker = pc.owner_user_id or user.id
    nxt = next_confirmed(db, world.id)
    next_session = None
    if nxt:
        plan, slot = nxt
        mine = db.query(SessionPlanVote).filter(SessionPlanVote.slot_id == slot.id, SessionPlanVote.user_id == asker).first()
        next_session = {"title": plan.title, "location": plan.location or "",
                        "starts_at": slot.starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "my": mine.choice if mine else None}
    return {"me": me, "can_edit": is_owner, "combat": _combat_now(db, pc), "next_session": next_session,
            "rest_kinds": _rest_kinds(db, pc) if is_owner else [],
            "polls_waiting": polls_waiting(db, world.id, asker),
            "schedule_href": with_world("/schedule", world)}


# Sections that are the character page itself (or need a desktop / admin) - not offered as "places".
_PLACES_SKIP = {"characters", "cockpit", "schedule", "androidapp", "character_sheets", "editor", "studio_console"}


@router.get("/api/characters/{pc_id}/hub/places")
def hub_places(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Every page this player may open, so they never have to leave their character page: the same visibility the
    navigation bar uses, evaluated as the PLAYER role (a GM looking in sees the player's list). The page opens each one
    inside the hub (an in-page viewer), not in a new window."""
    _user, _pc, world, _own = _hub_pc(request, db, pc_id)
    tools, kinds = [], []
    for item in build_catalog(world):
        if item["id"] in _PLACES_SKIP or item.get("condition") in ("dreamlands_enabled", "king_in_yellow_enabled"):
            continue
        section = item.get("player_section")
        if section:
            if _player_level(world, section) == "none":
                continue
        elif item.get("gm_only"):
            continue
        cond = item.get("condition")
        if cond and not getattr(world, cond, False):
            continue
        row = {"id": item["id"], "label": item["label"], "icon": item["icon"], "href": with_world(item["href"], world)}
        (kinds if item["id"].startswith("kind_") else tools).append(row)
    groups = []
    if tools:
        groups.append({"label": "Tools & logs", "items": tools})
    if kinds:
        groups.append({"label": "The world", "items": kinds})
    return {"groups": groups}


# ── Journal / goals writes ───────────────────────────────────────────────────

def _clean_text(body: dict, key: str, limit: int) -> Optional[str]:
    if key not in body:
        return None
    val = str(body.get(key) or "").strip()
    if len(val) > limit:
        raise HTTPException(400, f"{key} is too long (max {limit} characters)")
    return val


def _session_choice(body: dict, allowed_ids: set) -> Optional[int]:
    """None = leave unchanged / not supplied; 0 = clear the link."""
    if "session_id" not in body:
        return None
    raw = body.get("session_id")
    if raw in (None, "", 0):
        return 0
    try:
        sid = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "session_id must be a number")
    if sid not in allowed_ids:
        raise HTTPException(400, "That session isn't one of your party's")
    return sid


def _owned_entry(db: Session, pc: PlayerCharacter, entry_id: int) -> CharacterJournalEntry:
    e = db.get(CharacterJournalEntry, entry_id)
    if not e or e.character_id != pc.id:
        raise HTTPException(404)
    return e


@router.post("/api/characters/{pc_id}/hub/entries")
async def hub_entry_create(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Create a journal entry or a goal for this character (owner only).
    Body: {kind: "journal"|"goal", title, body, status?(goal), session_id?(journal)}."""
    user, pc, world = _owned_pc(request, db, pc_id)
    body = await _json_body(request)
    kind = body.get("kind", "journal")
    if kind not in _KINDS:
        raise HTTPException(400, "kind must be 'journal' or 'goal'")
    title = _clean_text(body, "title", _MAX_TITLE) or ""
    text = _clean_text(body, "body", _MAX_BODY) or ""
    if not title and not text:
        raise HTTPException(400, "Write a title or some text first")
    status = ""
    session_id = None
    sessions = _party_sessions(db, pc, _party_ids(db, pc))
    if kind == "goal":
        status = body.get("status", "active")
        if status not in GOAL_STATUSES:
            raise HTTPException(400, "Unknown goal status")
    else:
        session_id = _session_choice(body, {s.id for s in sessions}) or None
    entry = CharacterJournalEntry(
        world_id=world.id, character_id=pc.id, kind=kind, title=title, body=text,
        status=status, session_id=session_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _entry_out(entry, _session_labels(sessions))


@router.post("/api/characters/{pc_id}/hub/entries/{entry_id}")
async def hub_entry_update(pc_id: int, entry_id: int, request: Request, db: Session = Depends(get_db)):
    """Edit a journal entry or goal (owner only). Only the fields present in
    the body change; kind can't change."""
    user, pc, world = _owned_pc(request, db, pc_id)
    entry = _owned_entry(db, pc, entry_id)
    body = await _json_body(request)
    title = _clean_text(body, "title", _MAX_TITLE)
    text = _clean_text(body, "body", _MAX_BODY)
    new_title = entry.title if title is None else title
    new_text = entry.body if text is None else text
    if not new_title and not new_text:
        raise HTTPException(400, "Write a title or some text first")
    if "status" in body:
        if entry.kind != "goal":
            raise HTTPException(400, "Only goals have a status")
        if body["status"] not in GOAL_STATUSES:
            raise HTTPException(400, "Unknown goal status")
        entry.status = body["status"]
    sessions = _party_sessions(db, pc, _party_ids(db, pc))
    if "session_id" in body:
        if entry.kind != "journal":
            raise HTTPException(400, "Only journal entries link to a session")
        choice = _session_choice(body, {s.id for s in sessions})
        entry.session_id = choice or None
    entry.title, entry.body = new_title, new_text
    db.commit()
    db.refresh(entry)
    return _entry_out(entry, _session_labels(sessions))


@router.post("/api/characters/{pc_id}/hub/entries/{entry_id}/delete")
def hub_entry_delete(pc_id: int, entry_id: int, request: Request, db: Session = Depends(get_db)):
    user, pc, world = _owned_pc(request, db, pc_id)
    entry = _owned_entry(db, pc, entry_id)
    db.delete(entry)
    db.commit()
    return {"ok": True}
