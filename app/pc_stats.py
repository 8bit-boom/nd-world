"""One definition of a character's HP / Shock / PP / MP ceilings.

N&D derives its maxima from the eight stats — PP = STR+DEX+BOD+PER, MP =
WIL+INT+CHA+ITU, HP = PP + 10, Shock = MP — and lets a sheet override HP and
Shock by storing a non-zero max (0 means "use the derived value"). The sheet
page, the HP/Shock/PP/MP quick-edit routes, the party vitals strip, party
Rest, the combat tracker and the player cockpit each re-derived this on their
own, and most skipped the "0 means derived" rule: a character on auto HP showed
`20/0`, could never be flagged DOWN, entered combat with max HP 0 and was
clamped to 0 HP by a combat sync.

Leaf module (no router imports) so every one of those callers can use it.
"""
import json


def stat_values(pc) -> dict:
    """{stat_id: int} from stats_json, tolerant of bad JSON and bad entries."""
    try:
        stats = json.loads(getattr(pc, "stats_json", None) or "[]")
    except ValueError:
        return {}
    out = {}
    if not isinstance(stats, list):
        return out
    for s in stats:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        try:
            out[s["id"]] = int(float(s.get("value") or 0))
        except (TypeError, ValueError):
            out[s["id"]] = 0
    return out


def pc_maxima(pc) -> dict:
    """{"hp", "shock", "pp", "mp", "phys", "ment", "native"} ceilings.

    `native` is False for a character on a custom sheet (a sheet template
    attached but no N&D stats entered): its resources live in custom fields,
    so the stat-derived ceilings are 0 ("unknown") rather than an invented
    `10`, and only an explicitly stored max_hp / shock_max counts."""
    stat_val = stat_values(pc)
    native = bool(stat_val) or not getattr(pc, "sheet_template_id", None)
    phys = (stat_val.get("str", 0) + stat_val.get("dex", 0)
            + stat_val.get("bod", 0) + stat_val.get("per", 0)) if native else 0
    ment = (stat_val.get("wil", 0) + stat_val.get("int", 0)
            + stat_val.get("cha", 0) + stat_val.get("itu", 0)) if native else 0
    stored_hp = getattr(pc, "max_hp", 0) or 0
    stored_shock = getattr(pc, "shock_max", 0) or 0
    return {
        "hp": stored_hp if stored_hp > 0 else ((phys + 10) if native else 0),
        "shock": stored_shock if stored_shock > 0 else ment,
        "pp": phys,
        "mp": ment,
        "phys": phys,
        "ment": ment,
        "native": native,
    }


def int_field(body: dict, key: str, default: int = 0) -> int:
    """int(body[key]) for the quick-edit JSON routes, raising ValueError (the
    routes turn that into a 400) instead of an unhandled 500 on junk like
    "abc" or {}."""
    raw = body.get(key, default) if isinstance(body, dict) else default
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be a number")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a whole number")


MAX_CONDITIONS = 12
MAX_CONDITION_LEN = 40


def clean_condition(raw) -> str:
    """One condition label: a string, whitespace-collapsed, control characters
    dropped, capped at MAX_CONDITION_LEN. '' when nothing usable is left."""
    if not isinstance(raw, str):
        return ""
    text = "".join(ch for ch in raw if ch.isprintable())
    return " ".join(text.split())[:MAX_CONDITION_LEN].strip()


def clean_conditions(seq) -> list:
    """A condition list as stored: cleaned labels, de-duplicated
    case-insensitively (first spelling wins), at most MAX_CONDITIONS."""
    out, seen = [], set()
    for raw in seq if isinstance(seq, list) else []:
        c = clean_condition(raw)
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out[:MAX_CONDITIONS]
