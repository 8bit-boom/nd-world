"""Per-character page data that is not the sheet: preferences (carry limit, pinned pages), the change log, share links.

A leaf module (models only, no routers), so any router can record a change or read a preference. Nothing here commits:
the caller's own commit saves it together with the change it describes."""
import json
import secrets
from typing import Optional

from sqlalchemy.orm import Session

from .models import CharacterLog, CharacterPref, CharacterShare

LOG_KEEP = 200            # entries kept per character (oldest dropped)
PINS_MAX = 8


# ── preferences ──────────────────────────────────────────────────────────────

def prefs(db: Session, pc_id: int) -> dict:
    row = db.query(CharacterPref).filter(CharacterPref.character_id == pc_id).first()
    try:
        data = json.loads(row.prefs_json or "{}") if row else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def set_prefs(db: Session, pc, **values) -> dict:
    """Merge `values` into the character's preferences (a None value removes the key)."""
    row = db.query(CharacterPref).filter(CharacterPref.character_id == pc.id).first()
    if row is None:
        row = CharacterPref(world_id=pc.world_id, character_id=pc.id, prefs_json="{}")
        db.add(row)
    data = prefs(db, pc.id) if row.id else {}
    for k, v in values.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    row.prefs_json = json.dumps(data)
    return data


# ── change log ───────────────────────────────────────────────────────────────

def log(db: Session, pc, actor: str, kind: str, text: str, undo: Optional[dict] = None) -> None:
    """Record one change. `undo` = {"op": "...", ...} says how to take it back (None = not undoable)."""
    db.add(CharacterLog(world_id=pc.world_id, character_id=pc.id, actor=(actor or "Someone")[:120], kind=kind[:24],
                        text=text[:300], undo_json=json.dumps(undo or {})))
    db.flush()
    old = (db.query(CharacterLog.id).filter(CharacterLog.character_id == pc.id)
           .order_by(CharacterLog.id.desc()).offset(LOG_KEEP).all())
    if old:
        db.query(CharacterLog).filter(CharacterLog.id.in_([r[0] for r in old])).delete(synchronize_session=False)


def actor_name(user) -> str:
    return (getattr(user, "display_name", "") or getattr(user, "email", "") or "Someone") if user else "Someone"


# ── share links ──────────────────────────────────────────────────────────────

def new_share(db: Session, pc) -> CharacterShare:
    share = CharacterShare(world_id=pc.world_id, character_id=pc.id, token=secrets.token_urlsafe(24))
    db.add(share)
    db.flush()
    return share


def delete_for_character(db: Session, pc_id: int) -> None:
    """Drop everything this module keeps for a character being deleted (SQLite's foreign_keys pragma is off here)."""
    for model in (CharacterPref, CharacterLog, CharacterShare):
        db.query(model).filter(model.character_id == pc_id).delete(synchronize_session=False)


# ── carrying ─────────────────────────────────────────────────────────────────

def carry_info(db: Session, pc) -> dict:
    """What the character carries against what it can: weight is the sum of weight x qty over its equipment; the limit is
    the player's own number when set, otherwise 5 x (STR + BOD) - a table default, not a core rule (the core rules have no
    carrying limit), which is why the sheet says "default" until the player or GM sets one."""
    try:
        equipment = json.loads(pc.equipment_json or "[]")
    except ValueError:
        equipment = []
    used = 0.0
    for it in equipment if isinstance(equipment, list) else []:
        if isinstance(it, dict):
            try:
                qty = it.get("qty")
                used += float(it.get("weight") or 0) * float(1 if qty is None else qty)      # a used-up item (qty 0) weighs nothing
            except (TypeError, ValueError):
                pass
    try:
        stats = {str(x.get("id")): float(x.get("value") or 0) for x in json.loads(pc.stats_json or "[]") if isinstance(x, dict)}
    except (ValueError, TypeError):
        stats = {}
    auto = int(5 * (stats.get("str", 0) + stats.get("bod", 0)))
    custom = prefs(db, pc.id).get("carry_limit")
    limit = int(custom) if isinstance(custom, (int, float)) and custom > 0 else auto
    return {"used": round(used, 1), "limit": limit, "default": not (isinstance(custom, (int, float)) and custom > 0),
            "over": limit > 0 and used > limit}


# ── undo ─────────────────────────────────────────────────────────────────────

def _undo_key(spec: dict) -> str:
    op = str(spec.get("op") or "")
    if op == "currency":
        return "currency:" + str(spec.get("abbr") or "").lower()
    if op == "cf":
        return "cf:" + ",".join(sorted((spec.get("values") or {}).keys()))
    return op


def undo_spec(entry) -> dict:
    try:
        spec = json.loads(entry.undo_json or "{}")
    except ValueError:
        return {}
    return spec if isinstance(spec, dict) and spec.get("op") else {}


def can_undo(db: Session, entry) -> bool:
    """An entry can be undone while nothing later has changed the same thing: undoing "HP 12 -> 8" after "HP 8 -> 3" would
    wipe out the later change, so only the newest change to each number is undoable."""
    spec = undo_spec(entry)
    if not spec or entry.undone:
        return False
    key = _undo_key(spec)
    later = (db.query(CharacterLog).filter(CharacterLog.character_id == entry.character_id, CharacterLog.id > entry.id,
                                           CharacterLog.undone == False).all())  # noqa: E712
    return not any(_undo_key(undo_spec(e)) == key for e in later if undo_spec(e))


_NUM_COLUMNS = {"hp": "current_hp", "shock": "shock_current", "pp": "pp_current", "mp": "mp_current", "xp": "xp", "level": "level"}


def undo(db: Session, pc, entry) -> Optional[str]:
    """Put a logged change back. Returns an error message, or None when it worked (the caller commits)."""
    if entry.character_id != pc.id:
        return "Not this character's entry"
    if not can_undo(db, entry):
        return "A later change touched this too - undo that one first (or it was already undone)"
    spec = undo_spec(entry)
    op, value = spec["op"], spec.get("value")
    if op in _NUM_COLUMNS:
        if not isinstance(value, int) or value < 0:
            return "Nothing to restore"
        setattr(pc, _NUM_COLUMNS[op], value)
    elif op == "currency":
        try:
            coins = json.loads(pc.currency_json or "[]")
        except ValueError:
            coins = []
        key = str(spec.get("abbr") or "").lower()
        coin = next((c for c in coins if isinstance(c, dict) and key in ((c.get("abbr") or "").lower(), (c.get("label") or "").lower())), None)
        if coin is None or not isinstance(value, int):
            return "That currency is gone"
        coin["value"] = value
        pc.currency_json = json.dumps(coins)
    elif op == "conditions":
        if not isinstance(value, list):
            return "Nothing to restore"
        pc.conditions_json = json.dumps([str(c)[:40] for c in value][:12])
    elif op == "equipment":
        if not isinstance(value, str):
            return "Nothing to restore"
        try:
            json.loads(value)
        except ValueError:
            return "Nothing to restore"
        pc.equipment_json = value
    elif op == "advance":
        if not isinstance(spec.get("stats_json"), str) or not isinstance(spec.get("feats_json"), str):
            return "Nothing to restore"
        try:
            json.loads(spec["stats_json"]); json.loads(spec["feats_json"])
        except ValueError:
            return "Nothing to restore"
        pc.stats_json, pc.feats_json = spec["stats_json"], spec["feats_json"]
        set_prefs(db, pc, xp_spent=int(spec.get("spent") or 0) or None)
    elif op == "cf":
        values = spec.get("values")
        if not isinstance(values, dict):
            return "Nothing to restore"
        try:
            cf = json.loads(pc.custom_fields_json or "{}")
        except ValueError:
            cf = {}
        for k, v in values.items():
            if isinstance(v, (int, float)) or (isinstance(v, str) and len(v) <= 100):
                cf[str(k)] = v
        pc.custom_fields_json = json.dumps(cf)
    else:
        return "This change cannot be undone"
    entry.undone = True
    return None
