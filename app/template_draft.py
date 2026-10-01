"""AI-drafted character-sheet templates (leaf module: no router imports).

A model — the in-app one, or an MCP client — proposes a whole template: fields, the system hooks
(HP track, XP, name/player binds, conditions, Rest, pages, roster) and a rules digest. Its JSON is
untrusted, so `clean_template_draft` turns it into a template that is guaranteed to work: valid unique
field ids, known field types, selects with options, a single HP track, system references that point at
real fields. Anything it had to change or drop comes back as a warning for the GM to see.

The second half reads a long rulebook in pieces (`split_rules_text` / `pick_chunks`) and builds the
prompts, so the router only has to run the model."""
import re

from app.sheet_systems import BIND_COLUMNS, _num, clean_system_spec

FIELD_TYPES = ("text", "textarea", "number", "select", "resource", "list")
ITEM_TYPES = ("text", "textarea", "number", "select")
DRAFT_FIELDS_CAP, DRAFT_ITEM_FIELDS_CAP, DRAFT_OPTIONS_CAP = 120, 8, 30
DRAFT_NAME_CAP, DRAFT_DESC_CAP, DRAFT_RULES_CAP = 80, 300, 20000
_ID_CAP, _LABEL_CAP, _SECTION_CAP, _OPTION_CAP = 40, 80, 40, 60
_ID_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _text(v, cap: int) -> str:
    """A short single-line string: whitespace collapsed, angle brackets removed, capped."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return ""
    return " ".join(v.replace("<", "").replace(">", "").split())[:cap]


def _sanitise_id(raw) -> str:
    """A valid field id (letter first, then letters/digits/underscores) from whatever the model wrote."""
    s = raw if isinstance(raw, str) else ""
    if _ID_OK.match(s) and len(s) <= _ID_CAP:
        return s
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:_ID_CAP].strip("_")
    if not s:
        return ""
    return s if s[0].isalpha() else ("f_" + s)[:_ID_CAP]


def _unique(base: str, taken: set) -> str:
    cand, n = base, 2
    while cand in taken:
        suffix = f"_{n}"
        cand = base[:_ID_CAP - len(suffix)] + suffix
        n += 1
    taken.add(cand)
    return cand


def _fmt(n) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _options(raw) -> list:
    out = []
    for o in raw if isinstance(raw, list) else []:
        o = _text(o, _OPTION_CAP)
        if o and o not in out:
            out.append(o)
    return out[:DRAFT_OPTIONS_CAP]


def _default_for(ftype: str, raw, options: list) -> str:
    """The default_value a field of this type can hold: "cur/max" for resources, a number for numbers,
    one of the options for selects."""
    if ftype == "resource":
        s = _text(raw, 30)
        if not s:
            return "0/0"
        cur, _, mx = s.partition("/")
        c, m = _num(cur), (_num(mx) if "/" in s else None)
        if c is None or (m is None and "/" in s):
            return "0/0"
        if m is None:  # a bare number is the full value
            m = c
        c, m = max(0, min(c, 9999)), max(0, min(m, 9999))
        return f"{_fmt(min(c, m))}/{_fmt(m)}"
    if ftype == "number":
        n = _num(raw) if raw not in (None, "") else 0
        return _fmt(max(-99999, min(n, 99999))) if n is not None else "0"
    if ftype == "select":
        s = _text(raw, _OPTION_CAP)
        return s if s in options else ""
    if ftype == "textarea":
        return (raw if isinstance(raw, str) else "").replace("<", "").replace(">", "")[:2000].strip()
    return _text(raw, 400)


def _clean_item_fields(raw, warnings: list) -> list:
    out, taken = [], set()
    for sf in raw if isinstance(raw, list) else []:
        if not isinstance(sf, dict) or len(out) >= DRAFT_ITEM_FIELDS_CAP:
            continue
        label = _text(sf.get("label"), _LABEL_CAP) or _text(sf.get("id"), _LABEL_CAP)
        sid = _sanitise_id(sf.get("id")) or _sanitise_id(label)
        if not sid:
            continue
        stype = sf.get("type") if sf.get("type") in ITEM_TYPES else "text"
        item = {"id": _unique(sid, taken), "label": label or sid, "type": stype}
        if stype == "select":
            opts = _options(sf.get("options"))
            if opts:
                item["options"] = opts
            else:
                item["type"] = "text"
        out.append(item)
    if not out:
        out = [{"id": "name", "label": "Name", "type": "text"}, {"id": "notes", "label": "Notes", "type": "text"}]
        warnings.append("a list field had no columns; gave it Name and Notes")
    return out


def clean_template_fields(raw) -> tuple:
    """(fields, warnings): the model's field list as fields that load and work on a custom sheet."""
    fields, _, warnings = _clean_fields_mapped(raw)
    return fields, warnings


def _clean_fields_mapped(raw) -> tuple:
    """(fields, id_map {id as written -> final id}, warnings)."""
    warnings, fields, id_map = [], [], {}
    taken, has_hp, binds_used = set(), False, set()
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        if len(fields) >= DRAFT_FIELDS_CAP:
            warnings.append(f"kept the first {DRAFT_FIELDS_CAP} fields")
            break
        label = _text(f.get("label"), _LABEL_CAP)
        written = f.get("id") if isinstance(f.get("id"), str) else ""
        base = _sanitise_id(written) or _sanitise_id(label)
        if not base or not (label or f.get("type") in FIELD_TYPES or f.get("type") == "table"):
            continue  # a bare {"id": "x"} says nothing about what the field is
        label = label or _text(written.replace("_", " "), _LABEL_CAP) or base
        fid = _unique(base, taken)
        if written and fid != written:
            warnings.append(f"renamed the field id {written!r} to {fid!r}")
        elif not written:
            warnings.append(f"gave the field {label!r} the id {fid!r}")
        for key in {written, _sanitise_id(written)} - {""}:
            id_map.setdefault(key, fid)

        ftype = f.get("type")
        if ftype == "table":
            ftype = "list"
        if ftype not in FIELD_TYPES:
            warnings.append(f"{fid!r} had the unknown type {f.get('type')!r}; made it text")
            ftype = "text"
        out = {"id": fid, "label": label, "type": ftype, "section": _text(f.get("section"), _SECTION_CAP) or "General"}

        if ftype == "select":
            opts = _options(f.get("options"))
            if not opts:
                warnings.append(f"{fid!r} is a select with no options; made it text")
                out["type"], ftype = "text", "text"
            else:
                out["options"] = opts
        if ftype == "list":
            out["item_fields"] = _clean_item_fields(f.get("item_fields") or f.get("columns"), warnings)
        else:
            out["default_value"] = _default_for(ftype, f.get("default_value"), out.get("options") or [])
            if ftype == "number" and f.get("default_value") not in (None, "") and _num(f["default_value"]) is None:
                warnings.append(f"{fid!r} had a non-numeric default; set it to 0")

        if f.get("vital") == "hp" and ftype == "resource" and not has_hp:
            out["vital"] = "hp"
            has_hp = True
        if f.get("xp") is True and ftype == "number":
            out["xp"] = True
        bind = f.get("binds")
        if bind in BIND_COLUMNS and ftype == "text" and bind not in binds_used:
            out["binds"] = bind
            binds_used.add(bind)
        fields.append(out)
    return fields, id_map, warnings


def _remap_system(system, id_map: dict):
    """The model's `system` with every field reference rewritten to the final (sanitised) id."""
    if not isinstance(system, dict):
        return {}

    def m(x):
        return id_map.get(x, x) if isinstance(x, str) else x

    s = dict(system)
    rest = s.get("rest")
    if isinstance(rest, dict):
        s["rest"] = {k: [([op[0], m(op[1])] + list(op[2:])) if isinstance(op, (list, tuple)) and len(op) >= 2 else op
                         for op in ops] if isinstance(ops, (list, tuple)) else ops
                     for k, ops in rest.items()}
    roster = s.get("roster")
    if isinstance(roster, list):
        s["roster"] = [[g[0], [m(i) for i in g[1]]] if isinstance(g, (list, tuple)) and len(g) == 2
                       and isinstance(g[1], (list, tuple)) else g for g in roster]
    if isinstance(s.get("hp"), str):
        s["hp"] = m(s["hp"])
    if isinstance(s.get("xp"), list):
        s["xp"] = [m(i) for i in s["xp"]]
    if isinstance(s.get("binds"), dict):
        s["binds"] = {m(k): v for k, v in s["binds"].items()}
    return s


def clean_template_draft(raw) -> tuple:
    """(draft | None, warnings). `draft` is {name, description, sheet_mode:"custom", fields, system,
    rules_md}, ready to store as a SheetTemplate; None when the model gave nothing usable."""
    if not isinstance(raw, dict):
        return None, ["the draft was not a JSON object"]
    fields, id_map, warnings = _clean_fields_mapped(raw.get("fields"))
    if not fields:
        return None, warnings + ["the draft had no usable fields"]
    name = _text(raw.get("name"), DRAFT_NAME_CAP)
    if not name:
        name = "AI-drafted system"
        warnings.append("the draft had no name; called it 'AI-drafted system'")
    system, sys_warnings = clean_system_spec(_remap_system(raw.get("system"), id_map), fields)
    rules = raw.get("rules_md")
    rules = rules.replace("\x00", "").strip()[:DRAFT_RULES_CAP] if isinstance(rules, str) else ""
    return {
        "name": name,
        "description": _text(raw.get("description"), DRAFT_DESC_CAP),
        "sheet_mode": "custom",
        "fields": fields,
        "system": system,
        "rules_md": rules,
    }, warnings + sys_warnings


# ── reading a long rulebook in pieces ────────────────────────────────────────

_HEADING_SPLIT = re.compile(r"(?m)^(?=#{1,6}[ \t])")
_PARA_SPLIT = re.compile(r"\n\s*\n")
_TOKEN = re.compile(r"\S+\s*")


def _pack(units: list, max_chars: int, joiner: str = "\n\n") -> list:
    out, cur = [], ""
    for u in units:
        if cur and len(cur) + len(joiner) + len(u) <= max_chars:
            cur = cur + joiner + u
        else:
            if cur:
                out.append(cur)
            cur = u
    if cur:
        out.append(cur)
    return out


def _hard_split(block: str, max_chars: int) -> list:
    """Cut a block that is longer than max_chars at whitespace (a word that alone exceeds it is cut)."""
    pieces = []
    for tok in _TOKEN.findall(block):
        while len(tok) > max_chars:
            pieces.append(tok[:max_chars])
            tok = tok[max_chars:]
        if tok:
            pieces.append(tok)
    return [c.strip() for c in _pack(pieces, max_chars, joiner="") if c.strip()]


def split_rules_text(text: str, max_chars: int = 6000) -> list:
    """Split a rulebook into chunks of at most `max_chars`, losing no text (only whitespace at the
    seams). Boundaries fall before headings first, then between paragraphs, and only in the middle of a
    paragraph when a single paragraph is bigger than a chunk."""
    max_chars = max(200, int(max_chars))
    text = (text or "").replace("\r\n", "\n").replace("\x00", "")
    units = []
    for section in _HEADING_SPLIT.split(text):
        section = section.strip()
        if not section:
            continue
        if len(section) <= max_chars:
            units.append(section)
            continue
        paras = []
        for p in _PARA_SPLIT.split(section):
            p = p.strip()
            if p:
                paras.extend([p] if len(p) <= max_chars else _hard_split(p, max_chars))
        units.extend(_pack(paras, max_chars))
    return _pack(units, max_chars)


# Words that say "this part of the rulebook defines what a character sheet holds".
_SHEET_WORDS = (
    "character sheet", "character creation", "create a character", "attribute", "ability score", "skill",
    "hit point", "health", "stamina", "resource", "stat", "defense", "defence", "armor", "armour", "hp",
    "rest", "recover", "heal", "experience", "xp", "level up", "advancement", "condition", "status",
    "class", "origin", "background", "talent", "feat", "perk", "inventory", "equipment", "starting",
)


def _score(chunk: str) -> int:
    low = chunk.lower()
    return sum(min(low.count(w), 6) for w in _SHEET_WORDS)


def pick_chunks(chunks: list, cap: int) -> list:
    """At most `cap` chunks: all of them when they fit, otherwise the ones that talk most about what a
    sheet tracks (character creation, resources, resting, advancement), kept in document order."""
    chunks = list(chunks or [])
    cap = max(1, int(cap))
    if len(chunks) <= cap:
        return chunks
    best = sorted(range(len(chunks)), key=lambda i: (-_score(chunks[i]), i))[:cap]
    return [chunks[i] for i in sorted(best)]


# ── prompts ──────────────────────────────────────────────────────────────────

NOTES_SYSTEM = (
    "You are reading part of a tabletop RPG rulebook to prepare a character sheet for it. Write short notes "
    "(plain text, at most 25 bullet lines) on ONLY what a character sheet needs: the stats/attributes and their "
    "ranges, derived values, resources that go up and down (hit points, stamina, mana, stress, luck…) with their "
    "maximums, what resting or healing refills, status conditions, how experience or levels work, the choices made "
    "at character creation (class, origin, background), and the lists a character carries (skills, talents, gear, "
    "spells). Quote the book's own names. Skip lore, monsters and adventures. If this part has nothing of that kind, "
    "answer exactly: NOTHING."
)

DRAFT_SYSTEM = (
    "You design digital character sheets for tabletop RPGs. From the rulebook notes you are given, produce ONE JSON "
    "object and nothing else:\n"
    '{"name": "<system name>", "description": "<one sentence>", "fields": [...], "system": {...}, "rules_md": "<markdown>"}\n\n'
    "FIELDS — every entry: {\"id\", \"label\", \"type\", \"section\", \"default_value\"}. id: short, letters/digits/"
    "underscore, starting with a letter, unique. type is one of:\n"
    "- text, textarea (free text)\n"
    "- number (a single integer, e.g. a stat or level; default_value \"0\" or the book's starting value)\n"
    "- select (needs \"options\": [..]; default_value is one of them or empty)\n"
    "- resource (a current/max track such as hit points; default_value \"6/6\")\n"
    "- list (rows the player adds, e.g. skills, gear, spells; needs \"item_fields\": [{\"id\",\"label\",\"type\"}] with "
    "type text, textarea, number or select)\n"
    "Group fields into 3-8 short sections (\"section\") in the order a player fills them in: identity first, then "
    "core stats, resources, then lists, then notes. Keep to the fields the rules actually use (usually 15-45).\n"
    "Integration flags (use each at most where it fits; leave them off when the book has no such thing):\n"
    "- \"vital\": \"hp\" on the ONE resource that means 'is the character still up' (hit points, vigor, health).\n"
    "- \"xp\": true on number fields that hold experience points.\n"
    "- \"binds\": \"name\" on the text field for the character's name, \"player_name\" on the one for the player's name.\n\n"
    "SYSTEM — {\"conditions\": [status names the rules use, e.g. \"Poisoned\"], \"rest\": {\"short\": [ops], \"long\": [ops]}, "
    "\"pages\": [{\"label\", \"icon\", \"sections\": [section names]}], \"roster\": [[\"Group title\", [field ids to compare "
    "across the party]]]}. A Rest op is [\"full\", resource id] (refill), [\"empty\", resource id], [\"add\", resource id, n] "
    "or [\"set\", field id, value]; only list what the book's short/long rest (or equivalent) actually restores — omit "
    "\"short\" if the system has no short rest. Pages group the sections into 2-6 tabs for a phone; every section name must "
    "be spelled exactly as in the fields. Roster ids must be number/text/select fields (resources are always shown).\n\n"
    "RULES_MD — a compact markdown digest (under 6000 characters) of how the game is played that another AI can use to "
    "review or create characters: the core roll, how stats and resources work, the character-creation steps, how rest and "
    "advancement work. Use the book's own terms; do not invent rules the notes don't state.\n\n"
    "Never invent a mechanic the notes don't mention. Output the JSON object only."
)


def notes_prompt(chunk: str, index: int, total: int) -> str:
    return f"Rulebook part {index} of {total}:\n\n{chunk}"


def draft_prompt(notes: list, system_name: str = "", wishes: str = "") -> str:
    head = f"System name: {system_name}\n" if system_name else ""
    if wishes:
        head += f"What the GM asked for: {wishes[:1000]}\n"
    body = "\n\n".join(n for n in notes if n)
    return f"{head}\nRulebook notes:\n{body}\n\nNow output the template JSON."
