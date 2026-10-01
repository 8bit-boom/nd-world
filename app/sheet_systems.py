"""How a character's *system* (sheet template) plugs into the rest of the app.

N&D characters keep HP / Shock / PP / MP / level / XP in real columns, and every
list, party strip, combat tracker and XP award knows them. A character on a
custom system (Hunt in the Moonlight, Asterion, a GM's own template) keeps its
numbers in `custom_fields_json` under template-defined ids, so each of those
places used to either ignore the character ("no HP tracked", combat HP 0/0, XP
awards that vanish) or apply N&D rules to it (AC, level-up prompts).

This leaf module (no router imports) is the one place that bridges the two:

* **resource tracks** — current/max pairs, defaulted from the template exactly as
  the sheet shows them (an unsaved track is `default_value` "5/5", not "missing");
* **vital** — the track that *is* the character's hit points (DOWN at 0, the
  combatant's HP);
* **binds** — template fields that merely mirror a PlayerCharacter column
  ("Hunter Name" = `name`), which the sheet should not ask for twice;
* **xp** — the numeric fields party XP awards add to.

A template declares these per field (`"vital": "hp"`, `"binds": "name"`,
`"xp": true`) so any GM-made template can opt in. The built-in systems are
resolved here by slug instead, so databases that already hold the built-in rows
pick the behaviour up with no migration (and a GM-customised copy keeps working
as long as its field ids are unchanged).
"""
import json
import re

# template field id -> what the built-in system maps it to
BUILTIN_SYSTEMS = {
    # Hunt in the Moonlight, Revision 1.11. A 4-hour Rest and an 8-hour Long Rest both
    # restore all Stamina and clear Strain; Health is never restored by resting
    # (treatment), and Hunger / Arcane Knowledge fall only through play.
    "hunt-in-the-moonlight": {
        "hp": "health",
        "binds": {"hunter": "name", "player": "player_name"},
        "xp": ["xpCurrent", "xpLifetime"],
        "conditions": ["Bleeding", "Burning", "Blinded", "Marked", "Prone", "Restrained", "Stunned", "Weakened", "Vulnerable"],
        "rest": {"long": [("full", "stamina"), ("set", "strain", "0"), ("set", "staminaSpent", 0)]},
    },
    # Asterion (Game of Gods rules). Short Rest: Spark Shield full + 2 Ichor.
    # Long Rest: all Flesh and all Ichor.
    "asterion": {
        "hp": "flesh",       # Spark Shield soaks first, Flesh is the body: 0 = Shattered
        "binds": {},
        "xp": ["glory"],
        "conditions": ["Blinded", "Burning", "Bleeding", "Restrained", "Stunned", "Weakened", "Vulnerable"],
        "rest": {
            "short": [("full", "sparkShield"), ("add", "ichor", 2)],
            "long": [("full", "sparkShield"), ("full", "flesh"), ("full", "ichor")],
        },
    },
    # Neon & Dragons Player's Guide: the native sheet keeps HP/Shock/PP/MP in columns
    # (party Rest handles those); the template adds the guide's stim limit.
    "nd-default": {
        "hp": None, "binds": {}, "xp": [],
        "conditions": ["Burn", "Freeze", "Toxin", "Bleeding", "Blind", "Yellow", "Charm", "Daze", "Stunned"],
        "rest": {"long": [("empty", "stims")]},
    },
}

BIND_COLUMNS = ("name", "player_name")


def template_fields(tpl) -> list:
    """The template's field definitions as a list of dicts (bad JSON -> [])."""
    try:
        fields = json.loads(getattr(tpl, "fields_json", None) or "[]")
    except (TypeError, ValueError):
        return []
    return [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []


def system_meta(tpl) -> dict:
    """{"hp": field id | None, "binds": {field id: column}, "xp": [field ids]}.

    Per-field attributes on the template win; the built-in system table fills in
    whatever the template left undeclared, but only for ids the template really
    has (so a customised built-in can't be pointed at a field that's gone)."""
    fields = template_fields(tpl)
    ids = {f.get("id") for f in fields}
    meta = {"hp": None, "binds": {}, "xp": []}
    for f in fields:
        if f.get("vital") == "hp" and f.get("type") == "resource" and meta["hp"] is None:
            meta["hp"] = f.get("id")
        if f.get("binds") in BIND_COLUMNS:
            meta["binds"][f["id"]] = f["binds"]
        if f.get("xp") and f.get("type") == "number":
            meta["xp"].append(f["id"])
    builtin = BUILTIN_SYSTEMS.get(getattr(tpl, "slug", None)) if getattr(tpl, "is_builtin", False) else None
    if builtin:
        if meta["hp"] is None and builtin["hp"] in ids:
            meta["hp"] = builtin["hp"]
        for fid, col in builtin["binds"].items():
            if fid in ids:
                meta["binds"].setdefault(fid, col)
        if not meta["xp"]:
            meta["xp"] = [fid for fid in builtin["xp"] if fid in ids]
    return meta


def enrich_fields(tpl) -> list:
    """The template's fields with the resolved metadata written onto them, for
    pages that render fields client- or server-side (so templates only ever check
    `f.binds` / `f.vital` / `f.xp`, whether the template or the built-in table
    declared it)."""
    meta = system_meta(tpl)
    out = []
    for f in template_fields(tpl):
        f = dict(f)
        if f.get("id") == meta["hp"]:
            f["vital"] = "hp"
        if f.get("id") in meta["binds"]:
            f["binds"] = meta["binds"][f["id"]]
        if f.get("id") in meta["xp"]:
            f["xp"] = True
        out.append(f)
    return out


def xp_field_ids(meta: dict) -> list:
    return list(meta.get("xp") or [])


_HINT_RE = re.compile(r"\s*[(\[—–].*$")


def short_label(label) -> str:
    """"Health (0 = Broken — Wound + Press On)" -> "Health": the rules hint in a
    track's label is for the sheet, not for a one-line chip."""
    if not label or not isinstance(label, str):
        return ""
    return _HINT_RE.sub("", label).strip() or label.strip()


def _num(v):
    """int/float from a stored value (number, numeric string) or None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            f = float(s)
        except ValueError:
            return None
        return int(f) if f.is_integer() else f


def _default_pair(default_value) -> tuple:
    """(current, max) defaults of a resource field, mirroring custom_sheet.html:
    "5/5" -> (5, 5); a bare "3" -> current 3, max 0."""
    dv = default_value or "0/0"
    cur = _num(dv.split("/")[0])
    mx = _num(dv.split("/")[1]) if "/" in dv else 0
    return (cur if cur is not None else 0, mx if mx is not None else 0)


def resource_tracks(fields: list, custom_fields: dict, meta: dict = None) -> list:
    """Every `resource` field as {"id","label","current","max"}; a half the
    character hasn't saved falls back to the template default, as on the sheet.
    With `meta`, the vital (HP) track is moved to the front."""
    cf = custom_fields if isinstance(custom_fields, dict) else {}
    out = []
    for f in fields or []:
        if not isinstance(f, dict) or f.get("type") != "resource" or not f.get("id"):
            continue
        dcur, dmax = _default_pair(f.get("default_value"))
        cur, mx = _num(cf.get(f"{f['id']}_current")), _num(cf.get(f"{f['id']}_max"))
        out.append({"id": f["id"], "label": short_label(f.get("label") or f["id"]),
                    "current": cur if cur is not None else dcur, "max": mx if mx is not None else dmax})
    if meta and meta.get("hp"):
        out.sort(key=lambda t: 0 if t["id"] == meta["hp"] else 1)  # stable: template order otherwise
    return out


def hp_track(fields: list, custom_fields: dict, meta: dict):
    """The vital track {"id","label","current","max"} or None."""
    if not meta or not meta.get("hp"):
        return None
    return next((t for t in resource_tracks(fields, custom_fields) if t["id"] == meta["hp"]), None)


def parse_custom_fields(raw) -> dict:
    try:
        cf = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return cf if isinstance(cf, dict) else {}


def apply_xp_award(tpl, custom_fields: dict, delta: int) -> dict:
    """`custom_fields` with a party XP award of `delta` (negative = a correction)
    applied to the template's own XP / Glory fields (never below 0). A field that
    hasn't been saved yet starts from its template default."""
    cf = dict(custom_fields) if isinstance(custom_fields, dict) else {}
    by_id = {f.get("id"): f for f in template_fields(tpl)}
    for fid in xp_field_ids(system_meta(tpl)):
        base = _num(cf.get(fid))
        if base is None:
            base = _num((by_id.get(fid) or {}).get("default_value")) or 0
        cf[fid] = max(0, base + delta)
    return cf


# ── Conditions & Rest ────────────────────────────────────────────────────────

ND_CONDITIONS = ["Burn", "Freeze", "Toxin", "Bleeding", "Blind", "Yellow", "Charm", "Daze", "Stunned"]
GENERIC_CONDITIONS = ["Bleeding", "Burning", "Blinded", "Restrained", "Stunned", "Weakened", "Vulnerable"]


def system_conditions(tpl) -> list:
    """The condition chips a sheet offers: the rulebook's own list for a built-in
    system (a Hunter is Marked or Prone, never Yellow-tainted; an Asterion god is
    Vulnerable, never Frozen), the N&D list for the standard sheet, and a neutral
    set for a GM's custom system (they can always add their own)."""
    if tpl is None:
        return list(ND_CONDITIONS)
    builtin = BUILTIN_SYSTEMS.get(getattr(tpl, "slug", None)) if getattr(tpl, "is_builtin", False) else None
    if builtin and builtin.get("conditions"):
        return list(builtin["conditions"])
    if getattr(tpl, "sheet_mode", "nd") != "custom":
        return list(ND_CONDITIONS)
    return list(GENERIC_CONDITIONS)


def rest_ops(tpl, kind: str = "long") -> list:
    """The built-in system's Rest rules as ops: ("full", resource) refills a track,
    ("add", resource, n) tops it up, ("empty", resource) zeroes its current value,
    ("set", field, value) writes a plain field. `kind` is "short" or "long"; a
    system with a single Rest lists only "long" and a short one falls back to it."""
    builtin = BUILTIN_SYSTEMS.get(getattr(tpl, "slug", None)) if getattr(tpl, "is_builtin", False) else None
    spec = (builtin or {}).get("rest") or {}
    return list(spec.get(kind) or spec.get("long") or [])


def rest_touched_keys(tpl) -> set:
    """Every custom-field key a Rest of this system can change (either kind) — what
    Undo is allowed to put back, and nothing more."""
    builtin = BUILTIN_SYSTEMS.get(getattr(tpl, "slug", None)) if getattr(tpl, "is_builtin", False) else None
    keys = set()
    for ops in ((builtin or {}).get("rest") or {}).values():
        for op in ops:
            if op[0] in ("full", "add", "empty"):
                keys |= {f"{op[1]}_current", f"{op[1]}_max"}
            elif op[0] == "set":
                keys.add(op[1])
    return keys


def has_short_rest(tpl) -> bool:
    """True when the built-in system defines a Short Rest distinct from its Rest."""
    builtin = BUILTIN_SYSTEMS.get(getattr(tpl, "slug", None)) if getattr(tpl, "is_builtin", False) else None
    return bool(((builtin or {}).get("rest") or {}).get("short"))


def apply_rest(tpl, custom_fields: dict, kind: str = "long") -> dict:
    """`custom_fields` after the system's Rest rules; unknown ops / fields ignored."""
    cf = dict(custom_fields) if isinstance(custom_fields, dict) else {}
    tracks = {t["id"]: t for t in resource_tracks(template_fields(tpl), cf)}
    for op in rest_ops(tpl, kind):
        name, target = op[0], op[1]
        if name in ("full", "add", "empty"):
            t = tracks.get(target)
            if not t:
                continue
            if name == "full":
                cf[f"{target}_current"], cf[f"{target}_max"] = t["max"], t["max"]
            elif name == "add":
                cf[f"{target}_current"], cf[f"{target}_max"] = min(t["max"], t["current"] + op[2]), t["max"]
            else:
                cf[f"{target}_current"], cf[f"{target}_max"] = 0, t["max"]
        elif name == "set":
            cf[target] = op[2]
    return cf
