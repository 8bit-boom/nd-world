"""A character as ONE system-aware sentence (or short paragraph) for AI prompts.

Every AI surface that mentions the party — chat RAG, party insights, session prep, audio-job
hints — used to print "{race} {class}, level N", which reads "? ?, level 1" for a Hunt in the
Moonlight or Asterion character and says nothing about how they are doing. This leaf module (no
router imports) builds the line from the character's OWN system: its vital (HP / Health /
Flesh), other resources, conditions, and — with detail=True — the system's comparison fields and a
backstory excerpt.
"""
import json

from .pc_stats import clean_conditions, pc_maxima
from .rendering import strip_gm_only
from .sheet_systems import (
    field_value_text, hp_track, parse_custom_fields, resource_tracks, roster_field_groups, short_label,
    system_label, system_meta, template_fields,
)

BACKSTORY_EXCERPT = 320


def _conditions(pc) -> list:
    try:
        return clean_conditions(json.loads(getattr(pc, "conditions_json", None) or "[]"))
    except (ValueError, TypeError):
        return []


def pc_digest(pc, tpl=None) -> dict:
    """{system, native, identity, vital, resources, conditions, key_fields} for one character."""
    if tpl is None and getattr(pc, "sheet_template_id", None):
        tpl = getattr(pc, "sheet_template", None)
    m = pc_maxima(pc)
    native = m["native"]
    cf = parse_custom_fields(getattr(pc, "custom_fields_json", None))
    vital, resources, key_fields, identity = None, [], [], ""
    if native:
        if m["hp"]:
            vital = {"label": "HP", "current": pc.current_hp or 0, "max": m["hp"]}
        if m["shock"]:
            resources.append({"label": "Shock", "current": pc.shock_current or 0, "max": m["shock"]})
        if m["pp"]:
            resources.append({"label": "PP", "current": pc.pp_current or 0, "max": m["pp"]})
            resources.append({"label": "MP", "current": pc.mp_current or 0, "max": m["mp"]})
        identity = " ".join(x for x in (pc.race, pc.char_class) if x)
        if pc.level:
            identity = (identity + f", level {pc.level}").strip(", ")
    elif tpl is not None:
        fields = template_fields(tpl)
        meta = system_meta(tpl)
        v = hp_track(fields, cf, meta)
        if v:
            vital = {"label": v["label"], "current": v["current"], "max": v["max"]}
        resources = [{"label": t["label"], "current": t["current"], "max": t["max"]}
                     for t in resource_tracks(fields, cf, meta) if not v or t["id"] != v["id"]]
        for title, group in roster_field_groups(tpl):
            for f in group:
                if f.get("type") == "resource":
                    continue
                text = field_value_text(f, cf)
                if not text:
                    continue
                if title == "Identity" and not identity:
                    identity = text
                else:
                    key_fields.append((short_label(f.get("label") or f.get("id")), text))
    return {"system": system_label(tpl, native), "native": native, "identity": identity, "vital": vital,
            "resources": resources, "conditions": _conditions(pc), "key_fields": key_fields}


def pc_digest_line(pc, tpl=None, *, detail: bool = False, viewer_is_gm: bool = True) -> str:
    """"Anders (Hunt in the Moonlight, Human, played by Archie) — Health 3/5; Stamina 2/5; conditions: Stunned".
    detail=True appends the system's key fields and a backstory excerpt ([gmonly] removed unless the
    viewer is the GM)."""
    d = pc_digest(pc, tpl)
    who = [d["system"]] if not d["native"] else []
    if d["identity"]:
        who.append(d["identity"])
    if getattr(pc, "player_name", ""):
        who.append(f"played by {pc.player_name}")
    line = f"{pc.name}" + (f" ({', '.join(who)})" if who else "")
    state = []
    if d["vital"]:
        state.append(f"{d['vital']['label']} {d['vital']['current']}/{d['vital']['max']}")
    state += [f"{r['label']} {r['current']}/{r['max']}" for r in d["resources"]]
    if state:
        line += " — " + ", ".join(state)
    if d["conditions"]:
        line += "; conditions: " + ", ".join(d["conditions"])
    if detail:
        if d["key_fields"]:
            line += ". " + "; ".join(f"{k} {v}" for k, v in d["key_fields"])
        story = getattr(pc, "backstory", "") or ""
        if story and not viewer_is_gm:
            story = strip_gm_only(story)
        story = " ".join(story.split())
        if story:
            line += ". Backstory: " + (story[:BACKSTORY_EXCERPT].rstrip() + ("…" if len(story) > BACKSTORY_EXCERPT else ""))
    return line
