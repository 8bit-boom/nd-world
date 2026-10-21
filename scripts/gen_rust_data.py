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
    from app import constants, nav_menus, view_prefs
    return {
        "view_sections": view_prefs.SECTIONS, "view_sizes": list(view_prefs.SIZES), "view_default_size": view_prefs.DEFAULT_SIZE,
        "nav_static_catalog": nav_menus.STATIC_CATALOG, "nav_default_menus": nav_menus.DEFAULT_NAV_MENUS,
        "kinds": constants.KINDS, "kind_icons": constants.KIND_ICONS,
    }


def text() -> str:
    return json.dumps(make(), indent=1, ensure_ascii=False, sort_keys=True) + "\n"


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if OUT.read_text() == text() else "rust/nd-render/data/app_data.json is stale: run scripts/gen_rust_data.py")
    OUT.write_text(text())
    print("wrote", OUT)
