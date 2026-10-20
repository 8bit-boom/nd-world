"""Turn order of a CombatSession, shared by the GM's tracker view, the player hub and the maps (a leaf module: no router imports).

Two ways to run turns (app/combat_rules.py says which a ruleset uses):

* INITIATIVE (Neon & Dragons, Chronicles of the Worm, generic): `combatants` stay in the order they were added and `active_idx` is a
  POSITION in the initiative-sorted order (highest first, then the GM's `tiebreak`, then added order - static/js/combat-core.js sorts the
  same way, tests/test_combat_core.py runs both on the same cases). Anything that wants "whose turn is it" must sort first. Changing an
  initiative re-sorts, so the active combatant is re-found by id afterwards.
* FLUID (Hunt in the Moonlight, Asterion): there is no number. Whoever starts the violence acts first; after a turn, THAT player chooses who
  acts next; once everyone has acted the round ends and its last actor chooses the next round's first. The state is `turn_json`:
  {"active_id", "acted": [...], "picker"} - while someone is choosing, active_id is None and `picker` says who."""
import json
from typing import Optional

from . import combat_rules


def _init(c) -> int:
    try:
        return int((c or {}).get("initiative") or 0)
    except (TypeError, ValueError):
        return 0


def _tie(c) -> int:
    try:
        return int((c or {}).get("tiebreak") or 0)
    except (TypeError, ValueError):
        return 0


def order(combatants: list) -> list:
    """Indices of `combatants` in initiative order: highest first, then the higher `tiebreak`, then the order they were added."""
    return sorted(range(len(combatants)), key=lambda i: (-_init(combatants[i]), -_tie(combatants[i])))


def is_fluid(cs) -> bool:
    """Fluid initiative? The encounter's own choice (turn_json "mode", for a table that wants the other way - the Asterion-engine books
    allow a fixed order) wins; otherwise what the ruleset says."""
    mode = _raw_turn(cs).get("mode")
    if mode in ("initiative", "fluid"):
        return mode == "fluid"
    return combat_rules.is_fluid(getattr(cs, "ruleset", "") or "")


def _raw_turn(cs) -> dict:
    try:
        raw = json.loads(getattr(cs, "turn_json", None) or "{}")
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def is_out(cs, c) -> bool:
    """Cannot take a turn: dead, or down in a way this ruleset skips (combat_rules.is_out)."""
    return combat_rules.is_out(c, getattr(cs, "ruleset", "") or "")


def turn_state(cs) -> dict:
    """The fluid turn state, always with all three keys."""
    raw = _raw_turn(cs)
    return {"active_id": raw.get("active_id") if isinstance(raw.get("active_id"), str) else None,
            "acted": [x for x in raw.get("acted", []) if isinstance(x, str)] if isinstance(raw.get("acted"), list) else [],
            "picker": raw.get("picker") if isinstance(raw.get("picker"), str) else None,
            "mode": raw.get("mode") if raw.get("mode") in ("initiative", "fluid") else ""}


def set_turn_state(cs, state: dict) -> None:
    cs.turn_json = json.dumps({"active_id": state.get("active_id"), "acted": list(state.get("acted") or []),
                               "picker": state.get("picker"), "mode": state.get("mode") or ""})


def touch(cs) -> None:
    """Every write to an encounter's turn/state bumps its revision, so a tracker open elsewhere learns its copy is stale - and, once the
    change is committed, announces a "combat" signal on the live stream so the table's TV (display_combat.html) re-reads it at once
    instead of polling every 2 s."""
    cs.rev = (getattr(cs, "rev", 0) or 0) + 1
    from sqlalchemy.orm.exc import UnmappedInstanceError
    from sqlalchemy.orm import object_session
    try:
        sess = object_session(cs)
    except UnmappedInstanceError:                       # a plain object (the turn helpers are also used on stand-ins)
        sess = None
    if sess is not None and getattr(cs, "world_id", None) is not None:
        sess.info.setdefault("nd_live_signals", set()).add((cs.world_id, "combat"))


def load(cs) -> list:
    try:
        data = json.loads(cs.combatants_json or "[]")
    except ValueError:
        return []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def current_index(cs, combatants: list) -> Optional[int]:
    """Index into `combatants` of whoever has the turn, or None (an empty encounter, or - in a fluid fight - nobody yet / someone is choosing)."""
    if not combatants:
        return None
    if is_fluid(cs):
        aid = turn_state(cs)["active_id"]
        return next((i for i, c in enumerate(combatants) if aid and c.get("id") == aid), None)
    return order(combatants)[(cs.active_idx or 0) % len(combatants)]


def active_combatant_id(cs, combatants: list) -> Optional[str]:
    """The id of whoever has the turn, or None - what the maps mark."""
    i = current_index(cs, combatants)
    return None if i is None else combatants[i].get("id")


def find_pc(combatants: list, pc_id: int) -> Optional[int]:
    return next((i for i, c in enumerate(combatants) if c.get("pc_id") == pc_id), None)


def set_initiative(cs, combatants: list, idx: int, value: int) -> None:
    """Set one combatant's initiative, keeping the same combatant active after the re-sort."""
    holder = current_index(cs, combatants)
    combatants[idx]["initiative"] = int(value)
    if holder is not None and not is_fluid(cs):
        cs.active_idx = order(combatants).index(holder)
    cs.combatants_json = json.dumps(combatants)
    touch(cs)


def advance(cs, combatants: list) -> None:
    """End the current turn. Initiative order: the next combatant, and a new round after the last one. Fluid: the actor is marked as
    having acted and becomes the one who CHOOSES who goes next (the GM or the player then picks - see pick_next); when everyone has
    acted the round turns over and the same actor chooses the next round's first."""
    touch(cs)
    if is_fluid(cs):
        st = turn_state(cs)
        who = st["active_id"]
        if who and who not in st["acted"]:
            st["acted"].append(who)
        ids = [c.get("id") for c in combatants if not is_out(cs, c)]       # someone who cannot act never holds the round up
        if all(i in st["acted"] for i in ids):
            st["acted"] = []
            cs.round_num = (cs.round_num or 1) + 1
        st["picker"], st["active_id"] = who or st["picker"], None
        set_turn_state(cs, st)
        return
    if not combatants:
        cs.round_num = (cs.round_num or 1) + 1
        return
    turn_order = order(combatants)
    nxt = (cs.active_idx or 0) + 1
    for _ in range(len(combatants) + 1):                  # past whoever is down or dead (a full lap of nobody able: stop on the first)
        if nxt >= len(combatants):
            nxt, cs.round_num = 0, (cs.round_num or 1) + 1
        if not is_out(cs, combatants[turn_order[nxt]]):
            break
        nxt += 1
    else:
        nxt = 0
    cs.active_idx = nxt


def pick_next(cs, combatants: list, combatant_id: str) -> bool:
    """Fluid: the chooser names who acts next. Only someone who has not acted this round can be picked (at the start of a round anyone
    can). Returns False when the pick is not allowed."""
    st = turn_state(cs)
    target = next((c for c in combatants if c.get("id") == combatant_id), None)
    if target is None or combatant_id in st["acted"] or is_out(cs, target):
        return False
    st["active_id"], st["picker"] = combatant_id, None
    set_turn_state(cs, st)
    touch(cs)
    return True
