#!/usr/bin/env python3
"""Writes rust/nd-render/data/app_data.json: the app's own tables of DATA the Rust side needs to build the same pages (layout sections and their
allowed values, the navigation catalog and default menus, entity kinds and icons). Data, not logic - copied, never retyped, so the two
sides cannot drift; tests/test_rust_data.py fails when it is stale.      python scripts/gen_rust_data.py [--check]"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "rust" / "nd-render" / "data" / "app_data.json"


def column_defaults() -> dict:
    """{table: {column: {"default": value} | {"default": "__utcnow__"}, "onupdate": "__utcnow__"?}} - SQLAlchemy fills these in Python, not
    the database, so a write the gateway makes has to fill them the same way (rust/nd-gateway/src/orm.rs)."""
    from app.models import Base
    out = {}
    for t in Base.metadata.sorted_tables:
        cols = {}
        for c in t.columns:
            entry = {}
            for what in ("default", "onupdate"):
                d = getattr(c, what)
                if d is None:
                    continue
                if d.is_scalar:
                    entry[what] = d.arg
                elif d.is_callable and getattr(d.arg, "__name__", "") == "utcnow":
                    entry[what] = "__utcnow__"
                else:
                    raise SystemExit(f"{t.name}.{c.name}: a {what} the gateway cannot reproduce")
            if entry:
                cols[c.name] = entry
        out[t.name] = cols
    return out


def theme_presets_clean() -> list:
    from app.main import THEME_PRESETS, _sanitize_theme
    return [[k, list(_sanitize_theme(v).items())] for k, v in THEME_PRESETS.items()]


def builtin_catalog(kind: str) -> list:
    if kind == "races":
        from app.routers.races import _load_builtin_races as load
    else:
        from app.routers.professions import _load_builtin_professions as load
    from app.rendering import render_md
    # raw_html: render_md(raw) - what /races/add-builtin answers for an entity whose body is still the catalog's
    return [{**{k: e[k] for k in ("slug", "tier", "name", "summary", "raw", "image_url")}, "raw_html": render_md(e["raw"]) if e["raw"] else ""}
            for e in load()]


def feat_lookup() -> dict:
    """`_ability_cards`' two catalog lookups (characters.py): by str(id) and by lower-cased name, last one winning as in Python"""
    from app import game_catalog
    feats = game_catalog.catalog_payload()["feats"]
    slim = lambda x: {"category": x.get("category"), "rank": x.get("rank"), "description": x.get("description")}
    catalog = {str(x.get("id")): x for x in feats}
    by_name = {str(x.get("name", "")).lower(): x for x in catalog.values()}
    return {"by_id": {k: slim(v) for k, v in catalog.items()}, "by_name": {k: slim(v) for k, v in by_name.items()}}


def advance_feats() -> list:
    """advance.options' feat rows that do not depend on the character, in its final order (rank, category, name): the catalog's buyable
    feats with their cost and trimmed description, plus what the per-character filters read"""
    from app import advance, game_catalog
    out = []
    for f in game_catalog.catalog_payload()["feats"]:
        cat, rk = f.get("category"), advance.rank_num(f.get("rank"))
        if cat not in advance.FEAT_MULT or not (1 <= rk <= advance.MAX_RANK):
            continue
        out.append({"id": f["id"], "name": f["name"], "category": cat, "rank": rk, "cost": advance.feat_cost(cat, rk),
                    "description": str(f.get("description") or "")[:400].replace("\n---", "").strip(),
                    "race": f.get("associatedRace"), "profession": f.get("associatedProfession"),
                    "idl": str(f.get("id") or "").lower(), "namel": str(f.get("name") or "").lower()})
    out.sort(key=lambda x: (x["rank"], x["category"], x["name"]))
    return out


def make() -> dict:
    from app import calendar_config, combat_rules, constants, nav_menus, view_prefs
    from app.routers import quests
    return {
        "view_sections": view_prefs.SECTIONS, "view_sizes": list(view_prefs.SIZES), "view_default_size": view_prefs.DEFAULT_SIZE,
        "nav_static_catalog": nav_menus.STATIC_CATALOG, "nav_default_menus": nav_menus.DEFAULT_NAV_MENUS,
        "kinds": constants.KINDS, "kind_icons": constants.KIND_ICONS, "subtypes": constants.SUBTYPES,
        # Python's own key order of those two dicts (templates iterate them; the file itself is written with sorted keys)
        "kind_icons_order": list(constants.KIND_ICONS), "subtypes_order": list(constants.SUBTYPES),
        "view_specs": {s: view_prefs.spec(s) for s in view_prefs.SECTIONS},
        # Python's key order of a section's "options" and "lists" (view_prefs.clean writes them in that order; the file sorts keys)
        "view_options_order": {s: list(v["options"]) for s, v in view_prefs.SECTIONS.items() if v.get("options")},
        "view_lists_order": {s: list(v["lists"]) for s, v in view_prefs.SECTIONS.items() if v.get("lists")},
        "view_presets": list(view_prefs.PRESETS),
        # the built-in world themes as /worlds/<id>/theme/preset stores them (_sanitize_theme already applied), key order kept as pairs
        "theme_presets_clean": theme_presets_clean(),
        "column_defaults": column_defaults(),
        # a list of pairs, not a dict: the file is written with sorted keys and the ruleset menu keeps Python's order
        "quest_statuses": quests.STATUSES, "quest_categories": quests.CATEGORIES,
        "calendar_preset_labels": [[k, v["label"]] for k, v in calendar_config.CALENDAR_PRESETS.items()],
        # the calendar settings page's presets: label + the cleaned calendar, as ordered pairs (the file is written with sorted keys)
        "calendar_presets_clean": [[k, list({"label": v["label"], **calendar_config.clean_calendar_config(v)}.items())]
                                   for k, v in calendar_config.CALENDAR_PRESETS.items()],
        # what /worlds/new stores for a picked calendar preset: the exact config_json text, per preset key
        "calendar_presets_world": [[k, json.dumps({**calendar_config.clean_calendar_config(v), "current_day": 1})]
                                   for k, v in calendar_config.CALENDAR_PRESETS.items()],
        # the bundled race / profession catalogs (app/races, app/professions) as /races/add-builtin and /professions/add-builtin store them
        "feat_lookup": feat_lookup(), "advance_feats": advance_feats(),
        "builtin_races": builtin_catalog("races"), "builtin_professions": builtin_catalog("professions"),
        # per ruleset: what the combat routes read (label, hp_label, fluid turns, who a drop to 0 HP takes out of the turn order)
        "combat_rulesets": {k: {"label": v["label"], "hp_label": v["hp_label"], "fluid": v["turns"] == combat_rules.TURNS_FLUID,
                                "skip": v.get("skip_down", combat_rules.SKIP_ENEMIES)} for k, v in combat_rules.RULESETS.items()},
        "combat_template_rulesets": combat_rules._BY_TEMPLATE_SLUG,
        "combat_rule_labels": [[k, v["label"]] for k, v in combat_rules.RULESETS.items()],
        # the tracker pages' rules_json, exactly as json.dumps spells it (the GM's whole table; a player's three keys of it)
        "combat_rules_json": json.dumps(combat_rules.RULESETS),
        "combat_player_rules_json": json.dumps({k: {"label": v["label"], "turns": v["turns"], "hp_label": v["hp_label"]} for k, v in combat_rules.RULESETS.items()}),
    }


def text() -> str:
    return json.dumps(make(), indent=1, ensure_ascii=False, sort_keys=True) + "\n"


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if OUT.read_text() == text() else "rust/nd-render/data/app_data.json is stale: run scripts/gen_rust_data.py")
    OUT.write_text(text())
    print("wrote", OUT)
