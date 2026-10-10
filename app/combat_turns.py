"""Turn order of a CombatSession, shared by the GM's tracker view and the player hub (a leaf module: no router imports).

The tracker keeps `combatants` in the order they were added and `active_idx` as a POSITION in the initiative-sorted order
(highest first; ties keep their added order - the same stable sort the tracker page runs), so anything that wants "whose
turn is it" must sort first. Changing an initiative re-sorts, so the active combatant is re-found by id afterwards."""
import json
from typing import Optional


def order(combatants: list) -> list:
    """Indices of `combatants` in turn order (highest initiative first, stable)."""
    return sorted(range(len(combatants)), key=lambda i: -(_init(combatants[i])))


def _init(c) -> int:
    try:
        return int((c or {}).get("initiative") or 0)
    except (TypeError, ValueError):
        return 0


def load(cs) -> list:
    try:
        data = json.loads(cs.combatants_json or "[]")
    except ValueError:
        return []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def current_index(cs, combatants: list) -> Optional[int]:
    """Index into `combatants` of whoever has the turn, or None for an empty encounter."""
    if not combatants:
        return None
    return order(combatants)[(cs.active_idx or 0) % len(combatants)]


def find_pc(combatants: list, pc_id: int) -> Optional[int]:
    return next((i for i, c in enumerate(combatants) if c.get("pc_id") == pc_id), None)


def set_initiative(cs, combatants: list, idx: int, value: int) -> None:
    """Set one combatant's initiative, keeping the same combatant active after the re-sort."""
    holder = current_index(cs, combatants)
    combatants[idx]["initiative"] = int(value)
    if holder is not None:
        cs.active_idx = order(combatants).index(holder)
    cs.combatants_json = json.dumps(combatants)


def advance(cs, combatants: list) -> None:
    """End the current turn: next combatant, and a new round after the last one."""
    if not combatants:
        cs.round_num = (cs.round_num or 1) + 1
        return
    nxt = (cs.active_idx or 0) + 1
    if nxt >= len(combatants):
        nxt, cs.round_num = 0, (cs.round_num or 1) + 1
    cs.active_idx = nxt
