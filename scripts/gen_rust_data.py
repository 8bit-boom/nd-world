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


def make() -> dict:
    from app import calendar_config, combat_rules, constants, nav_menus, view_prefs
    from app.routers import quests
    return {
        "view_sections": view_prefs.SECTIONS, "view_sizes": list(view_prefs.SIZES), "view_default_size": view_prefs.DEFAULT_SIZE,
        "nav_static_catalog": nav_menus.STATIC_CATALOG, "nav_default_menus": nav_menus.DEFAULT_NAV_MENUS,
        "kinds": constants.KINDS, "kind_icons": constants.KIND_ICONS, "subtypes": constants.SUBTYPES,
        "view_specs": {s: view_prefs.spec(s) for s in view_prefs.SECTIONS},
        # a list of pairs, not a dict: the file is written with sorted keys and the ruleset menu keeps Python's order
        "quest_statuses": quests.STATUSES, "quest_categories": quests.CATEGORIES,
        "calendar_preset_labels": [[k, v["label"]] for k, v in calendar_config.CALENDAR_PRESETS.items()],
        "combat_rule_labels": [[k, v["label"]] for k, v in combat_rules.RULESETS.items()],
    }


def text() -> str:
    return json.dumps(make(), indent=1, ensure_ascii=False, sort_keys=True) + "\n"


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if OUT.read_text() == text() else "rust/nd-render/data/app_data.json is stale: run scripts/gen_rust_data.py")
    OUT.write_text(text())
    print("wrote", OUT)
