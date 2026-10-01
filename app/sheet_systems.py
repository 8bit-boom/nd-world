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
from pathlib import Path

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
        # what a side-by-side comparison of hunters should show beyond the resource tracks
        "roster": [("Identity", ["race"]),
                   ("Core", ["armor", "movement", "alteration", "overcharge", "strain"]),
                   ("Progress", ["xpCurrent", "xpLifetime", "crows", "marks"])],
        "pages": [
            {"id": "hunter", "label": "Hunter", "icon": "🧍", "sections": ["Identity"]},
            {"id": "oath", "label": "Oath", "icon": "🗡", "sections": ["Hunter's Oath"]},
            {"id": "status", "label": "Status", "icon": "❤", "conditions": True,
             "sections": ["Core Tracks", "Wounds", "Marks & Crows", "Experience"]},
            {"id": "abilities", "label": "Abilities", "icon": "⚔",
             "sections": ["Abilities", "Moon-Gifts & Occult Rites", "Body Modifications"]},
            {"id": "loadout", "label": "Loadout", "icon": "🎒",
             "sections": ["Hunter Tools & Loadout", "Mounts & Companions"]},
            {"id": "log", "label": "Hunt log", "icon": "🌙", "sections": ["Moon Calendar", "Session Record", "Notes"]},
        ],
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
        "roster": [("Identity", ["kind"]), ("Core", ["armor"]),
                   ("Progress", ["glory", "repScore", "domainRank", "drachma"])],
        "pages": [
            {"id": "hero", "label": "Hero", "icon": "🧍", "conditions": True, "sections": ["Identity", "Core Stats"]},
            {"id": "powers", "label": "Powers", "icon": "⚔", "sections": ["Abilities", "Progression"]},
            {"id": "inventory", "label": "Inventory", "icon": "🎒", "sections": ["Inventory & Equipment"]},
            {"id": "domain", "label": "Domain", "icon": "🏛", "sections": ["Domain", "Followers"]},
            {"id": "journal", "label": "Journal", "icon": "📜",
             "sections": ["Campaign Log", "Relationships & Allies", "Notes"]},
        ],
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


def _own_spec(tpl) -> dict:
    """The template's own `system_json` (conditions / rest / pages / roster …) as a dict, {} when
    absent or unreadable. Stored cleaned (clean_system_spec), but read defensively anyway."""
    raw = getattr(tpl, "system_json", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def system_spec(tpl) -> dict:
    """The system hooks that apply to this template: the built-in table entry (a built-in template,
    by slug) overlaid, key by key, with whatever the template declares itself in `system_json`. This
    is what gives a GM's own template the same integration — conditions, Rest, pages, roster
    comparison, even vital / XP / binds — that Hunt in the Moonlight and Asterion get."""
    spec = {}
    if tpl is not None and getattr(tpl, "is_builtin", False):
        spec.update(BUILTIN_SYSTEMS.get(getattr(tpl, "slug", None)) or {})
    spec.update({k: v for k, v in _own_spec(tpl).items() if v})
    return spec


def system_spec_public(tpl) -> dict:
    """The template's OWN stored spec (not the built-in table), JSON-safe — what the editor shows and
    what export/import carry."""
    return _own_spec(tpl)


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
    builtin = system_spec(tpl)
    if builtin:
        if meta["hp"] is None and builtin.get("hp") in ids:
            meta["hp"] = builtin["hp"]
        for fid, col in (builtin.get("binds") or {}).items():
            if fid in ids:
                meta["binds"].setdefault(fid, col)
        if not meta["xp"]:
            meta["xp"] = [fid for fid in (builtin.get("xp") or []) if fid in ids]
    return meta


# A custom template with at least this many sections gets a page per section when its
# built-in system doesn't say how to group them; shorter ones stay a single scroll.
MIN_SECTIONS_FOR_PAGES = 4


def sheet_pages(tpl, section_names: list):
    """How a custom sheet's sections are grouped into the pages of its Sheet tab.

    Returns None (one long page) or {"pages": [{id, label, icon}], "section_page":
    {section name: page id}, "conditions": page id, "linked": page id}. A built-in
    system lists its own groups (BUILTIN_SYSTEMS[slug]["pages"]); any section the
    template has that no group names — the GM added one, or renamed one — goes on a
    trailing "More" page, so nothing is ever hidden. Groups with no sections left are
    dropped. Any other template with MIN_SECTIONS_FOR_PAGES+ sections gets one page
    per section."""
    names = list(section_names or [])
    builtin = system_spec(tpl)
    spec = (builtin or {}).get("pages")
    pages, section_page, cond = [], {}, None
    if spec:
        for idx, page in enumerate(spec):
            pid = page.get("id") or f"p{idx}"
            mine = [n for n in page.get("sections", []) if n in names]
            if not mine:
                continue
            pages.append({"id": pid, "label": page.get("label") or pid, "icon": page.get("icon", "")})
            for n in mine:
                section_page[n] = pid
            if page.get("conditions"):
                cond = pid
        rest = [n for n in names if n not in section_page]
        if rest:
            pages.append({"id": "more", "label": "More", "icon": "➕"})
            for n in rest:
                section_page[n] = "more"
    elif len(names) >= MIN_SECTIONS_FOR_PAGES:
        for i, n in enumerate(names):
            pages.append({"id": f"s{i}", "label": n, "icon": ""})
            section_page[n] = f"s{i}"
    if len(pages) < 2:
        return None
    return {"pages": pages, "section_page": section_page,
            "conditions": cond or pages[0]["id"], "linked": pages[-1]["id"]}


_SYSTEMS_DIR = Path(__file__).parent / "game_data" / "systems"


def system_rules_markdown(tpl) -> str:
    """The rules text an AI should ground a character of this system in: a built-in system's digest
    (app/game_data/systems/<slug>.md) followed by the template's own `rules_md` (a GM's customisation
    of a built-in, or the whole text for a template they made). "" for a native N&D sheet, which
    uses the world's rules."""
    parts = []
    slug = getattr(tpl, "slug", None) if getattr(tpl, "is_builtin", False) else None
    if slug and "/" not in slug and "\\" not in slug and ".." not in slug:
        path = _SYSTEMS_DIR / f"{slug}.md"
        try:
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    own = (getattr(tpl, "rules_md", None) or "").strip()
    if own:
        parts.append(own)
    return "\n\n".join(parts)


def system_label(tpl, native: bool = False) -> str:
    """Human name of the system a character plays: "Neon & Dragons" for a native sheet,
    otherwise the template's own name."""
    if native or tpl is None:
        return "Neon & Dragons"
    return getattr(tpl, "name", None) or "a custom system"


# ── a template's own system spec (what a GM — or an AI — can declare) ────────

SPEC_CONDITIONS_CAP, SPEC_REST_OPS_CAP, SPEC_PAGES_CAP = 24, 12, 10
SPEC_ROSTER_GROUPS_CAP, SPEC_ROSTER_IDS_CAP = 6, 12
REST_OPS = ("full", "empty", "add", "set")
_SET_TYPES = ("number", "text", "select")
_ROSTER_TYPES = ("number", "text", "select")


def _clean_rest_op(op, by_id: dict, warnings: list):
    """One Rest op as a list, or None (with a warning) if it can't work on this template's fields."""
    if not isinstance(op, (list, tuple)) or len(op) < 2 or op[0] not in REST_OPS:
        warnings.append(f"ignored a Rest rule that is not [full|empty|add|set, field, …]: {op!r}"[:160])
        return None
    name, target = op[0], op[1]
    f = by_id.get(target) if isinstance(target, str) else None
    if f is None:
        warnings.append(f"ignored a Rest rule for the unknown field {target!r}")
        return None
    if name in ("full", "empty", "add"):
        if f.get("type") != "resource":
            warnings.append(f"ignored Rest '{name}' on {target!r}: only resource fields can be refilled")
            return None
        if name != "add":
            return [name, target]
        n = _num(op[2]) if len(op) > 2 else 1
        if not isinstance(n, int) or n <= 0 or n > 999:
            warnings.append(f"ignored Rest 'add' on {target!r}: the amount must be a whole number")
            return None
        return [name, target, n]
    # set
    if f.get("type") not in _SET_TYPES or len(op) < 3 or isinstance(op[2], (list, dict, bool)) or op[2] is None:
        warnings.append(f"ignored Rest 'set' on {target!r}")
        return None
    value = op[2] if isinstance(op[2], int) else str(op[2]).strip()[:100]
    if f.get("type") == "select" and str(value) not in [str(o) for o in (f.get("options") or [])]:
        warnings.append(f"ignored Rest 'set' on {target!r}: {value!r} is not one of its options")
        return None
    return ["set", target, value]


def clean_system_spec(raw, fields) -> tuple:
    """(spec, warnings): a template's `system_json` reduced to what works on its `fields`.

    Accepts conditions (≤24 short names), rest {"short"/"long": [[op, field, value?]…]}, pages
    [{label, icon, sections[], conditions?}], roster [[title, [field ids]]] and — as an alternative
    to the per-field flags — hp (a resource id), xp ([number ids]) and binds ({text id: name|
    player_name}). Anything referring to a field/section the template doesn't have is dropped and
    reported; a junk value yields {}."""
    warnings = []
    if not isinstance(raw, dict):
        return {}, warnings
    flist = [f for f in (fields or []) if isinstance(f, dict) and isinstance(f.get("id"), str) and f["id"]]
    by_id = {f["id"]: f for f in flist}
    sections = []
    for f in flist:
        sec = f.get("section") or "Custom"
        if sec not in sections:
            sections.append(sec)
    spec = {}

    conds = raw.get("conditions")
    if isinstance(conds, list):
        out, seen = [], set()
        for c in conds:
            c = " ".join(c.split())[:30] if isinstance(c, str) else ""
            if c and c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
        if len(out) > SPEC_CONDITIONS_CAP:
            warnings.append(f"kept the first {SPEC_CONDITIONS_CAP} conditions")
            out = out[:SPEC_CONDITIONS_CAP]
        if out:
            spec["conditions"] = out

    rest = raw.get("rest")
    if isinstance(rest, dict):
        clean = {}
        for kind in ("short", "long"):
            ops = rest.get(kind)
            if not isinstance(ops, (list, tuple)):
                continue
            good = [o for o in (_clean_rest_op(op, by_id, warnings) for op in ops) if o]
            if good:
                clean[kind] = good[:SPEC_REST_OPS_CAP]
        if clean:
            spec["rest"] = clean

    pages = raw.get("pages")
    if isinstance(pages, list):
        used, out = set(), []
        for page in pages[:SPEC_PAGES_CAP * 2]:
            if not isinstance(page, dict) or not isinstance(page.get("sections"), list):
                continue
            mine = []
            for sec in page["sections"]:
                if isinstance(sec, str) and sec in sections and sec not in used:
                    mine.append(sec)
                    used.add(sec)
                elif isinstance(sec, str):
                    warnings.append(f"page section {sec!r} is unknown or already on another page")
            if not mine:
                continue
            label = " ".join(str(page.get("label") or "").split())[:24] or mine[0][:24]
            out.append({"id": f"p{len(out)}", "label": label, "icon": str(page.get("icon") or "")[:4], "sections": mine,
                        "conditions": bool(page.get("conditions")) and not any(p["conditions"] for p in out)})
        if len(out) >= 2:
            spec["pages"] = out[:SPEC_PAGES_CAP]
        elif out:
            warnings.append("a single page is the same as no pages; ignored")

    roster = raw.get("roster")
    if isinstance(roster, list):
        out = []
        for grp in roster[:SPEC_ROSTER_GROUPS_CAP * 2]:
            if not isinstance(grp, (list, tuple)) or len(grp) != 2 or not isinstance(grp[1], (list, tuple)):
                continue
            ids = [i for i in grp[1] if isinstance(i, str) and i in by_id and by_id[i].get("type") in _ROSTER_TYPES]
            if len(ids) != len([i for i in grp[1] if isinstance(i, str)]):
                warnings.append("some roster fields were dropped (unknown, or resources, which are always compared)")
            if ids:
                out.append([" ".join(str(grp[0] or "Sheet").split())[:30] or "Sheet", ids[:SPEC_ROSTER_IDS_CAP]])
        if out:
            spec["roster"] = out[:SPEC_ROSTER_GROUPS_CAP]

    hp = raw.get("hp")
    if isinstance(hp, str) and by_id.get(hp, {}).get("type") == "resource":
        spec["hp"] = hp
    xp = raw.get("xp")
    if isinstance(xp, list):
        ids = [i for i in xp if isinstance(i, str) and by_id.get(i, {}).get("type") == "number"]
        if ids:
            spec["xp"] = ids
    binds = raw.get("binds")
    if isinstance(binds, dict):
        ok = {k: v for k, v in binds.items() if by_id.get(k, {}).get("type") == "text" and v in BIND_COLUMNS}
        if ok:
            spec["binds"] = ok
    return spec, warnings


# ── AI-drafted sheets ────────────────────────────────────────────────────────

AI_TEXT_CAP, AI_AREA_CAP, AI_NUM_CAP, AI_LIST_ROWS = 400, 4000, 999, 12


def ai_field_catalog(tpl) -> list:
    """The fields an AI may fill when drafting a character on this template: id, a trimmed
    label, type, select options, list columns. Fields that merely mirror a character column
    (name / player) are omitted — those are asked for separately."""
    meta = system_meta(tpl)
    out = []
    for f in template_fields(tpl):
        if not isinstance(f, dict) or not f.get("id") or f["id"] in meta["binds"]:
            continue
        entry = {"id": f["id"], "label": short_label(f.get("label") or f["id"])[:60], "type": f.get("type") or "text"}
        if f.get("section"):
            entry["section"] = f["section"]
        if entry["type"] == "select":
            entry["options"] = [str(o) for o in (f.get("options") or [])]
        if entry["type"] == "resource":
            cur, mx = _default_pair(f.get("default_value"))
            entry["default"] = {"current": cur, "max": mx}
        if entry["type"] == "list":
            entry["columns"] = [{"id": sf.get("id"), "type": sf.get("type") or "text",
                                 **({"options": [str(o) for o in sf.get("options") or []]} if sf.get("type") == "select" else {})}
                                for sf in (f.get("item_fields") or []) if isinstance(sf, dict) and sf.get("id")]
        out.append(entry)
    return out


def _clean_num(v):
    n = _num(v)
    return None if n is None else max(0, min(AI_NUM_CAP, int(n)))


def _clean_text(v, cap):
    if isinstance(v, (dict, list, tuple, set)) or v is None:
        return None
    return str(v).strip()[:cap]


def _clean_choice(v, options):
    text = _clean_text(v, AI_TEXT_CAP)
    if text is None:
        return None
    for o in options:
        if str(o).strip().lower() == text.lower():
            return str(o)
    return None


def clean_ai_fields(tpl, raw) -> dict:
    """Model output -> `custom_fields_json`-shaped dict for this template, or {} for junk.

    Untrusted input: unknown ids are dropped, selects must match a real option, numbers are
    clamped to 0..999, text is bounded, a resource is stored as {id}_current / {id}_max,
    a list keeps only its own columns and at most AI_LIST_ROWS rows."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for f in template_fields(tpl):
        if not isinstance(f, dict) or f.get("id") not in raw:
            continue
        fid, val, kind = f["id"], raw[f["id"]], f.get("type") or "text"
        if f.get("binds") or fid in system_meta(tpl)["binds"]:
            continue
        if kind == "resource":
            cur = mx = None
            if isinstance(val, dict):
                cur, mx = _clean_num(val.get("current")), _clean_num(val.get("max"))
            elif isinstance(val, str) and "/" in val:
                a, b = val.split("/", 1)
                cur, mx = _clean_num(a), _clean_num(b)
            elif not isinstance(val, (list, tuple)):
                cur = _clean_num(val)
            if cur is not None:
                out[f"{fid}_current"] = cur
            if mx is not None:
                out[f"{fid}_max"] = mx
        elif kind == "number":
            n = _clean_num(val)
            if n is not None:
                out[fid] = n
        elif kind == "select":
            choice = _clean_choice(val, f.get("options") or [])
            if choice is not None:
                out[fid] = choice
        elif kind == "list":
            if not isinstance(val, list):
                continue
            cols = {sf["id"]: sf for sf in (f.get("item_fields") or []) if isinstance(sf, dict) and sf.get("id")}
            rows = []
            for item in val[:AI_LIST_ROWS]:
                if not isinstance(item, dict):
                    continue
                row = {}
                for cid, col in cols.items():
                    if cid not in item:
                        continue
                    cell = (_clean_choice(item[cid], col.get("options") or []) if col.get("type") == "select"
                            else _clean_text(item[cid], AI_AREA_CAP if col.get("type") == "textarea" else AI_TEXT_CAP))
                    if cell:
                        row[cid] = cell
                if row:
                    rows.append(row)
            if rows:
                out[fid] = rows
        elif kind in ("text", "textarea"):
            text = _clean_text(val, AI_AREA_CAP if kind == "textarea" else AI_TEXT_CAP)
            if text:
                out[fid] = text
    return out


def ai_preview_lines(tpl, cleaned: dict) -> list:
    """[(label, text)] for the review step of an AI draft, in template order."""
    out = []
    for f in template_fields(tpl):
        if not isinstance(f, dict) or not f.get("id"):
            continue
        fid, label = f["id"], short_label(f.get("label") or f["id"])
        if f.get("type") == "resource":
            if f"{fid}_current" in cleaned or f"{fid}_max" in cleaned:
                out.append((label, field_value_text(f, cleaned)))
        elif f.get("type") == "list":
            rows = cleaned.get(fid)
            if rows:
                names = [str(next(iter(r.values()))) for r in rows if r][:3]
                out.append((label, f"{len(rows)} × " + ", ".join(names)))
        elif fid in cleaned:
            out.append((label, str(cleaned[fid])[:80]))
    return out


ROSTER_GENERIC_CAP = 10


def roster_field_groups(tpl) -> list:
    """[(group title, [field dicts])] — the non-resource fields worth comparing across a
    party for a custom-sheet template (resource tracks are listed separately). A built-in
    system names its own ("roster" in BUILTIN_SYSTEMS, ids the template really has);
    any other template gets its first number/select fields under "Sheet", capped."""
    fields = [f for f in template_fields(tpl) if isinstance(f, dict)]
    by_id = {f.get("id"): f for f in fields}
    builtin = system_spec(tpl)
    out = []
    if builtin and builtin.get("roster"):
        for title, ids in builtin["roster"]:
            picked = [by_id[i] for i in ids if i in by_id]
            if picked:
                out.append((title, picked))
        return out
    binds = system_meta(tpl)["binds"]
    picked = [f for f in fields if f.get("type") in ("number", "select") and f.get("id") not in binds
              and not f.get("xp")]
    return [("Sheet", picked[:ROSTER_GENERIC_CAP])] if picked else []


def field_value_text(field: dict, custom_fields: dict) -> str:
    """A field's stored value as one short display string ("" when empty/unsaved):
    resources read "cur / max" (template default when nothing is saved), everything else
    its value. Lists are not summarised here."""
    fid = field.get("id")
    if field.get("type") == "resource":
        cur, mx = _default_pair(field.get("default_value"))
        cur = custom_fields.get(f"{fid}_current", cur)
        mx = custom_fields.get(f"{fid}_max", mx)
        return f"{cur} / {mx}" if str(cur).strip() != "" or str(mx).strip() != "" else ""
    if field.get("type") == "list":
        return ""
    v = custom_fields.get(fid, field.get("default_value") or "")
    return "" if v is None else str(v).strip()


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


def find_track(tracks: list, name: str):
    """The resource track matching `name` by field id or (trimmed) label, case-insensitive."""
    key = (name or "").strip().lower()
    if not key:
        return None
    for t in tracks:
        if key in (str(t.get("id", "")).lower(), str(t.get("label", "")).lower(), short_label(t.get("label", "")).lower()):
            return t
    return None


def adjust_track(tpl, custom_fields: dict, resource: str, delta=None, value=None):
    """(new custom_fields, track after) with one resource track of a custom sheet changed:
    `value` sets it, `delta` adds to it; clamped to 0..max (just >= 0 when the max is 0).
    Raises ValueError naming the available tracks if `resource` matches none."""
    if (delta is None) == (value is None):
        raise ValueError("pass exactly one of delta or value")
    fields = template_fields(tpl)
    meta = system_meta(tpl)
    tracks = resource_tracks(fields, custom_fields, meta)
    track = find_track(tracks, resource)
    if track is None:
        raise ValueError("no such resource " + repr(resource) + "; this sheet has: " +
                         ", ".join(f"{t['label']} (id {t['id']})" for t in tracks))
    cur = track["current"] if isinstance(track["current"], (int, float)) else 0
    new = int(value) if value is not None else int(cur) + int(delta)
    mx = track["max"] if isinstance(track["max"], (int, float)) else 0
    new = max(0, min(int(mx), new)) if mx > 0 else max(0, new)
    cf = dict(custom_fields) if isinstance(custom_fields, dict) else {}
    cf[f"{track['id']}_current"] = new
    after = next(t for t in resource_tracks(fields, cf, meta) if t["id"] == track["id"])
    return cf, after


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
    builtin = system_spec(tpl)
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
    builtin = system_spec(tpl)
    spec = (builtin or {}).get("rest") or {}
    return list(spec.get(kind) or spec.get("long") or [])


def rest_touched_keys(tpl) -> set:
    """Every custom-field key a Rest of this system can change (either kind) — what
    Undo is allowed to put back, and nothing more."""
    builtin = system_spec(tpl)
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
    builtin = system_spec(tpl)
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
