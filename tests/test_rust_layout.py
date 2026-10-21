"""The page-layout pieces ported to Rust (rust/nd-render/src/app.rs, docs/RUST_PAGES.md) answer what Python answers: a saved layout choice
made safe (`view_prefs.clean`) and the navigation a viewer sees (`nav_menus.resolve_nav_menus`). Differential, random inputs; runs when the
checker is built (cargo build -p nd-render), as in CI's gateway job."""
import json
import os
import random
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
_C = [os.environ.get("ND_RENDER_CHECK", ""), str(ROOT / "rust/target/release/nd-render-check"), str(ROOT / "rust/target/debug/nd-render-check")]
BIN = next((c for c in _C if c and Path(c).is_file()), None)
pytestmark = pytest.mark.skipif(BIN is None, reason="rust/nd-render not built")


def ask(qs):
    out = subprocess.run([BIN], input="".join(json.dumps(q) + "\n" for q in qs), capture_output=True, text=True, check=True).stdout.splitlines()
    return [json.loads(x) for x in out]


_JUNK = [None, True, False, 0, 1, 2, 3, 7, -1, 2.5, "2", " 3 ", "x", "", "-name", "name", "-x", [], {}, ["a"], "tray", "log", "quick", "grid",
         "rail", "top", "wide", "touch", "110", 110, "dark", "a-b", "UPPER", "x" * 50]


def _raw(rng, sec):
    keys = ["mode", "size", "sort", "group", "filters", "options", "pins"] + list((sec.get("lists") or {}).keys())
    raw = {}
    for k in rng.sample(keys, rng.randint(0, len(keys))):
        if k in ("filters", "pins") or k in (sec.get("lists") or {}):
            pool = list(sec.get("filters", [])) + ["a", "b", "c-d", "X", 3, None, "a"] + list((sec.get("options") or {}).get("nav", []))
            raw[k] = [rng.choice(pool) for _ in range(rng.randint(0, 9))] if rng.random() < .9 else rng.choice(_JUNK)
        elif k == "options":
            opts = sec.get("options") or {"nav": ["top"]}
            raw[k] = {o: rng.choice(list(v) + _JUNK) for o, v in opts.items() if rng.random() < .7} if rng.random() < .9 else rng.choice(_JUNK)
        else:
            pool = list(sec.get("modes", [])) + list(sec.get("sorts", [])) + ["-" + s for s in sec.get("sorts", [])] + list(sec.get("groups", [])) + _JUNK
            raw[k] = rng.choice(pool)
    return raw if rng.random() < .95 else rng.choice(_JUNK)


def test_layout_choices_are_cleaned_like_python():
    from app import view_prefs
    rng = random.Random(4)
    qs, want = [], []
    for section, sec in view_prefs.SECTIONS.items():
        for _ in range(150):
            raw = _raw(rng, sec)
            qs.append({"op": "clean", "section": section, "raw": raw}); want.append(json.loads(json.dumps(view_prefs.clean(section, raw))))
    got = ask(qs)
    unsure = sum(1 for g in got if g is None)
    bad = [(q["section"], q["raw"], w, g) for q, w, g in zip(qs, want, got) if g is not None and w != g]
    assert not bad, f"{len(bad)} of {len(qs)} differ: {bad[:4]}"
    assert unsure < len(qs) * 0.02, unsure


def _world(rng):
    from app.nav_menus import DEFAULT_NAV_MENUS, STATIC_CATALOG
    ids = [i["id"] for i in STATIC_CATALOG] + ["kind_character", "kind_note", "kind_custom_a", "nope"]
    menus = None
    if rng.random() < .6:
        menus = json.dumps([{"id": f"m{i}", "label": f"Menu {i}", "icon": "x", "item_ids": rng.sample(ids, rng.randint(0, 8))} for i in range(rng.randint(0, 4))]
                           + ([rng.choice(["junk", 3, None])] if rng.random() < .2 else []))
    elif rng.random() < .3:
        menus = rng.choice(["", "not json", "{}", "[]", json.dumps(DEFAULT_NAV_MENUS)])
    levels = ["none", "read", "edit", "bad"]
    access = json.dumps({s: {"player": rng.choice(levels), "assistant": rng.choice(levels)} for s in rng.sample(
        ["maps", "dice", "rules", "boards", "quests", "kind_character", "kind_custom_a", "characters", "audio", "sessions"], rng.randint(0, 6))})
    kinds = rng.choice([None, "", json.dumps([{"id": "custom_a", "label": "Alpha", "icon": "A"}, {"id": "custom_b", "label": "Beta"}])])
    w = {"id": 3, "slug": "w", "nav_menus_json": menus, "section_access_json": access, "custom_kinds_json": kinds}
    for flag in ("players_can_use_ai_chat", "players_can_ask_ai", "players_can_use_image_gen"):
        w[flag] = rng.choice([True, False, 1, 0, None])
    return w


def test_navigation_menus_are_resolved_like_python():
    from app import nav_menus
    rng = random.Random(5)
    qs, want = [], []
    for _ in range(400):
        w = _world(rng) if rng.random() < .95 else None
        for is_gm, role in ((True, None), (False, "player"), (False, "assistant")):
            dl, ky = rng.random() < .5, rng.random() < .5
            req = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_gm=is_gm), is_assistant=role == "assistant"))
            world = SimpleNamespace(**w) if w else None
            try:
                m, u = nav_menus.resolve_nav_menus(world, dl, ky, req)
                py = json.loads(json.dumps([m, u]))
            except Exception as e:
                py = f"error {type(e).__name__}"
            qs.append({"op": "nav", "is_gm": is_gm, "role": role, "world": w, "dreamlands": dl, "king": ky}); want.append(py)
    got = ask(qs)
    unsure = sum(1 for g in got if g is None)
    bad = [(q, w, g) for q, w, g in zip(qs, want, got) if g is not None and w != g]
    assert not bad, f"{len(bad)} of {len(qs)} differ; first: {json.dumps(bad[0])[:1500]}"
    assert unsure < len(qs) * 0.05, unsure


_MD_PIECES = ["Hello world", "**bold**", "*it*", "[color=red]red[/color]", "[color=#0f0]x[/color]", "[color=evil;]y[/color]", "[mark]m[/mark]",
              "[mark=gold]g[/mark]", "[u]u[/u]", "[read]Read this aloud[/read]", "[spoiler]twist[/spoiler]", "[gmonly]secret[/gmonly]",
              "[for=1,2]yours[/for]", "[reveal=2026-01-01]later[/reveal]", "[reveal=2026-02-30]bad[/reveal]", "\n\n", " ", "\n\n- item\n\n",
              "![pic](/uploads/a.png \"size:50\")", "![s](/uploads/a.mp3 \"audio\")", "![v](/uploads/a.mp4 \"video\")", "[link](/entity/3)", "# Title\n"]


def test_the_apps_own_markdown_tags_render_like_python():
    """The part of |md after the markdown step is a line-by-line port: on text where the two markdown libraries agree (no underscores,
    no fenced code), the whole result must be Python's. (Where the libraries differ the owner chose the Rust one - docs/RUST_PAGES.md.)"""
    import importlib.util
    from app.rendering import _render_md
    spec = importlib.util.spec_from_file_location("golden", ROOT / "scripts" / "golden.py")
    golden = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(golden)
    rng = random.Random(6)
    texts = ["".join(rng.choice(_MD_PIECES) for _ in range(rng.randint(1, 8))) for _ in range(1500)]
    got = ask([{"op": "md", "text": t} for t in texts])
    n = lambda h: golden.normalise("text/html", h.encode())
    bad = [(t, _render_md(t), g) for t, g in zip(texts, got) if n(_render_md(t)) != n(g)]
    assert len(bad) < len(texts) * 0.05, f"{len(bad)} of {len(texts)} differ, e.g. {bad[:3]}"


_BODY_LINES = ["## Attributes", "## Entry", "## Profile", "## Other", "* **Damage**: 2d6", "* **Rarity[1]**: Rare", "* **Cost**: *500* cr",
               "\\*\\*Armor\\*\\* = 3", "\\*\\*Range\\*\\*: 10 m\\", "**Strength:** 4", "Points: 12", "Difficulty to craft:", "", "  ", "next value",
               "---", "![img](/a.png)", "| a | b |", "Special conditions: none", "* **Special conditions**: only at night", "* **Visibility**: hidden",
               "* **Type**: weapon", "Some *plain* text with [a link](/x) and \\*\\*bold\\*\\*.", "- a list line", "\\- escaped dash", "&amp; &lt;b&gt;",
               "* **Effect**: {template}", "* **Max PP**: 7", "Rank 2 thing", "##Heading", "Ünïcödé: wert", "\x1c", "a\r\nb", "\tTabbed: x"]


def test_card_text_helpers_match_python():
    """body_summary, entry_text and parse_stats (cards, list tables) - random bodies built from the formats the app's content uses."""
    from app.rendering import body_summary, entry_text, parse_stats
    rng = random.Random(7)
    bodies = ["", "plain"] + ["\n".join(rng.choice(_BODY_LINES) for _ in range(rng.randint(1, 14))) for _ in range(2500)]
    for p in sorted(ROOT.glob("app/**/*.md"))[:200]:
        bodies.append(p.read_text(errors="ignore"))
    qs = [{"op": op, "text": b} for b in bodies for op in ("summary", "entry", "stats")]
    got = ask(qs)
    want = []
    for b in bodies:
        want += [body_summary(b), entry_text(b), json.loads(json.dumps(parse_stats(b)))]
    bad = [(q["op"], q["text"], w, g) for q, w, g in zip(qs, want, got) if w != g]
    assert not bad, f"{len(bad)} of {len(qs)} differ: {bad[:3]}"
