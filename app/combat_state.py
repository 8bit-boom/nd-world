"""What the combat tracker stores and what a player may see of it (a leaf module: no router imports, no database).

A CombatSession keeps its combatants as JSON. Everything that writes it (the tracker page, the player hub, the map sync) and everything
that reads it for a PLAYER goes through here, so the shape is checked in one place and an enemy's name or exact HP cannot reach a player
by way of one forgotten route.

  normalize_combatant / normalize_state - clamp and clean what the browser sent (types, lengths, counts); raise ValueError when the
                                          whole body is unusable
  player_view                           - the sanitized encounter a player is shown: GM-hidden combatants left out, an enemy's name
                                          replaced by "Enemy A" unless the GM marked it known, its HP given as a word, its armor,
                                          conditions and notes withheld
  hp_word                               - Unhurt / Hurt / Bloodied / Down
"""
import uuid
from typing import Optional

from .combat_rules import RULESET_IDS

MAX_COMBATANTS = 60
MAX_LOG = 300
MAX_NAME = 120
MAX_NOTES = 2000
MAX_CONDITIONS = 30
_BIG = 100_000
SOURCES = ("pc", "entity", "manual")


def _int(v, default: int = 0, lo: int = -_BIG, hi: int = _BIG) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            return default
    return max(lo, min(hi, n))


def _opt_id(v) -> Optional[int]:
    n = _int(v, 0, 0, 2**31 - 1)
    return n or None


MAX_STACK = 9


def _conditions(raw) -> list:
    """Condition names; the same name more than once is a STACK (Neon & Dragons: "conditions stack"), up to MAX_STACK of one."""
    out, count = [], {}
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, str):
            continue
        name = " ".join(c.split())[:40]
        key = name.casefold()
        if name and count.get(key, 0) < MAX_STACK:
            count[key] = count.get(key, 0) + 1
            out.append(name)
        if len(out) >= MAX_CONDITIONS:
            break
    return out


def _res(raw) -> dict:
    """Extra resource tracks beyond Health / Shock: {"sparkShield": {"cur": 3, "max": 3, "label": "Spark Shield"}, "ichor": ...} - read from
    the character's own sheet for a custom system (Asterion, Hunt) so a fight can spend and soak with them."""
    out = {}
    for k, v in raw.items() if isinstance(raw, dict) else []:
        if not isinstance(k, str) or not isinstance(v, dict) or len(out) >= 12:
            continue
        mx = _int(v.get("max"), 0, 0, 9999)
        out[k[:32]] = {"cur": _int(v.get("cur"), mx, 0, mx), "max": mx, "label": " ".join(str(v.get("label") or k).split())[:40]}
    return out


def _counts(raw, cap: int = 99) -> dict:
    """{"label": n} - small named counters (legendary actions, a Boss's Beast Action pool, whatever the GM wants to count)."""
    out = {}
    for k, v in raw.items() if isinstance(raw, dict) else []:
        if isinstance(k, str) and k.strip() and len(out) < 12:
            out[" ".join(k.split())[:40]] = _int(v, 0, 0, cap)
    return out


def normalize_combatant(c) -> Optional[dict]:
    """One combatant, cleaned. None when it is not a dict at all."""
    if not isinstance(c, dict):
        return None
    source = c.get("source") if c.get("source") in SOURCES else "manual"
    max_hp = _int(c.get("max_hp"), 0, 0)
    max_shock = _int(c.get("max_shock"), 0, 0)
    conditions = _conditions(c.get("conditions"))
    durations = {}
    raw_dur = c.get("cond_dur")
    if isinstance(raw_dur, dict):
        by_name = {x.casefold(): x for x in conditions}
        for k, v in raw_dur.items():
            if isinstance(k, str) and k.casefold() in by_name:
                n = _int(v, 0, 0, 99)
                if n:
                    durations[by_name[k.casefold()]] = n
    return {
        "id": str(c.get("id") or "")[:40] or "c-" + uuid.uuid4().hex[:8],
        "name": " ".join(str(c.get("name") or "").split())[:MAX_NAME] or "Combatant",
        "source": source,
        "pc_id": _opt_id(c.get("pc_id")),
        "entity_id": _opt_id(c.get("entity_id")),
        "initiative": _int(c.get("initiative"), 0, -9999, 9999),
        "tiebreak": _int(c.get("tiebreak"), 0, -9999, 9999),
        "max_hp": max_hp,
        "hp": _int(c.get("hp"), max_hp, 0, max(max_hp, 0)),
        "temp_hp": _int(c.get("temp_hp"), 0, 0, 9999),
        "max_shock": max_shock,
        "shock": _int(c.get("shock"), 0, 0, max_shock),
        "armor": _int(c.get("armor"), 0, 0, 999),
        "conditions": conditions,
        "cond_dur": durations,
        "res": _res(c.get("res")),
        "used": _counts(c.get("used"), 9),                           # action economy spent this turn / round: {"A": 1, "R": 0}
        "saves": {"ok": _int((c.get("saves") or {}).get("ok") if isinstance(c.get("saves"), dict) else 0, 0, 0, 3),
                  "fail": _int((c.get("saves") or {}).get("fail") if isinstance(c.get("saves"), dict) else 0, 0, 0, 3),
                  "ko": _int((c.get("saves") or {}).get("ko") if isinstance(c.get("saves"), dict) else 0, 0, 0, 3)},
        "wounds": _int(c.get("wounds"), 0, 0, 99),
        "press_on": bool(c.get("press_on")),
        "counters": _counts(c.get("counters")),
        "notes": str(c.get("notes") or "")[:MAX_NOTES],
        "status": "dead" if c.get("status") == "dead" else "",
        "visible": c.get("visible") is not False,                    # shown to players unless the GM hid it
        "name_known": bool(c.get("name_known", source == "pc")),     # an enemy's real name is shown only once the GM allows it
    }


def normalize_turn(raw, ids: set) -> dict:
    """Fluid-initiative state: who is acting (None while the last actor is still choosing), who has acted this round, who is choosing."""
    raw = raw if isinstance(raw, dict) else {}
    acted = []
    for i in raw.get("acted") if isinstance(raw.get("acted"), list) else []:
        if isinstance(i, str) and i in ids and i not in acted:
            acted.append(i)
    pick = lambda v: v if isinstance(v, str) and v in ids else None
    return {"active_id": pick(raw.get("active_id")), "acted": acted, "picker": pick(raw.get("picker"))}


def normalize_log(raw) -> list:
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and e.get("text"):
            out.append({"t": _int(e.get("t"), 0, 0, 2**53), "round": _int(e.get("round"), 1, 1, 9999),
                        "text": " ".join(str(e["text"]).split())[:200]})
    return out[-MAX_LOG:]


def normalize_state(body) -> dict:
    """The tracker's save body -> {combatants, round_num, active_idx, name?, log?}. ValueError when it cannot be a state."""
    if not isinstance(body, dict):
        raise ValueError("The state must be an object.")
    raw = body.get("combatants")
    if not isinstance(raw, list):
        raise ValueError("combatants must be a list.")
    if len(raw) > MAX_COMBATANTS:
        raise ValueError(f"At most {MAX_COMBATANTS} combatants in one encounter.")
    combatants, seen = [], set()
    for c in raw:
        n = normalize_combatant(c)
        if n is None:
            raise ValueError("Every combatant must be an object.")
        if n["id"] in seen:                                           # an id is how the active turn and the map find a combatant
            n["id"] = "c-" + uuid.uuid4().hex[:8]
        seen.add(n["id"])
        combatants.append(n)
    out = {
        "combatants": combatants,
        "round_num": _int(body.get("round_num"), 1, 1, 9999),
        "active_idx": _int(body.get("active_idx"), 0, 0, max(len(combatants) - 1, 0)),
    }
    if "ruleset" in body:
        out["ruleset"] = body["ruleset"] if body["ruleset"] in ("",) + tuple(RULESET_IDS) else ""
    if "turn" in body:
        out["turn"] = normalize_turn(body.get("turn"), {c["id"] for c in combatants})
    if isinstance(body.get("name"), str) and body["name"].strip():
        out["name"] = " ".join(body["name"].split())[:256]
    if "log" in body:
        out["log"] = normalize_log(body.get("log"))
    return out


def hp_word(c: dict) -> str:
    max_hp, hp = _int(c.get("max_hp"), 0, 0), _int(c.get("hp"), 0, 0)
    if c.get("status") == "dead" or (max_hp > 0 and hp <= 0):
        return "Down"
    if max_hp <= 0 or hp >= max_hp:
        return "Unhurt"
    return "Bloodied" if hp * 2 <= max_hp else "Hurt"


def _letter(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def player_view(combatants: list, order: list, active_id: Optional[str], acted=(), picker: Optional[str] = None) -> dict:
    """What a player may see. `order` is the indices of `combatants` in display order (initiative order, or as added for a fluid fight).

    Returns {"rows": [...], "active_id", "picker"}: a PC row carries its numbers and conditions; an enemy row carries a name (the real one
    only when the GM marked it known, else "Enemy A"), a word for its health, and nothing else - no HP numbers, armor, conditions or notes;
    a GM-hidden combatant is not mentioned at all (it keeps its turn - the marker just is not shown). `picker` is the name of the PC who is
    choosing who acts next, or "" when it is an enemy or nobody."""
    rows, enemy_n, seen_pick = [], 0, ""
    acted = set(acted or ())
    shown_active = None
    for i in order:
        c = combatants[i]
        is_enemy = c.get("source") != "pc"
        label = None
        if is_enemy:
            label = _letter(enemy_n)
            enemy_n += 1
        visible = c.get("visible", True)
        if c.get("id") == picker and visible and not is_enemy:
            seen_pick = c.get("name") or ""
        if c.get("id") == active_id and visible:
            shown_active = active_id
        if not visible:
            continue
        base = {"id": c.get("id"), "active": c.get("id") == active_id, "acted": c.get("id") in acted, "state": hp_word(c)}
        if is_enemy:
            rows.append(dict(base, kind="enemy", name=c.get("name") if c.get("name_known") else f"Enemy {label}"))
        else:
            rows.append(dict(base, kind="pc", name=c.get("name"), hp=c.get("hp", 0), max_hp=c.get("max_hp", 0),
                             shock=c.get("shock", 0), max_shock=c.get("max_shock", 0),
                             conditions=list(c.get("conditions") or [])))
    return {"rows": rows, "active_id": shown_active, "picker": seen_pick}
