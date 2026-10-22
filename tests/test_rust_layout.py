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
    from app.rendering import body_summary, entry_text, parse_stats, strip_md
    rng = random.Random(7)
    bodies = ["", "plain"] + ["\n".join(rng.choice(_BODY_LINES) for _ in range(rng.randint(1, 14))) for _ in range(2500)]
    for p in sorted(ROOT.glob("app/**/*.md"))[:200]:
        bodies.append(p.read_text(errors="ignore"))
    qs = [{"op": op, "text": b} for b in bodies for op in ("summary", "entry", "stats", "strip_md")]
    got = ask(qs)
    want = []
    for b in bodies:
        want += [body_summary(b), entry_text(b), json.loads(json.dumps(parse_stats(b))), strip_md(b)]
    bad = [(q["op"], q["text"], w, g) for q, w, g in zip(qs, want, got) if w != g]
    assert not bad, f"{len(bad)} of {len(qs)} differ: {bad[:3]}"


_NAMES = ["Bob", "Bob Smith", "Hunter Edmund Vosk, the Greyfather", "The King in Yellow", "Rusty Anchor", "Al", "bob", "Senior Resident Hunter Ann Lee",
          "x.y (z)", "C++ Guild", "  Spaced  ", "Ünïcode Name", "O'Neil", "Anne-Marie Dupont"]
_PROSE = ["Bob went to the Rusty Anchor.", "<p>Bob Smith met bob and BOB.</p>", "<a href='/x'>Bob</a> and <code>Bob</code>", "<pre>Bob</pre> Bob",
          "Edmund Vosk spoke", "Bobby is not Bob_", "<img alt=\"Bob\"/> Bob", "<h2>Rusty Anchor</h2>", "C++ Guild rules", "Ann Lee and Anne-Marie Dupont",
          "<script>Bob</script>Bob", "<p>x.y (z) is here</p>", "dressed in yellow"]


def test_autolinks_name_variants_headings_and_sections_match_python():
    from app.main import _rules_toc
    from app.rendering import autolink_entities, derive_name_variants
    from app.rules_render import split_rules_sections
    rng = random.Random(8)
    qs, want = [], []
    for n in _NAMES:
        qs.append({"op": "variants", "text": n}); want.append(derive_name_variants(n))
    for _ in range(600):
        names = {}
        for n in rng.sample(_NAMES, rng.randint(1, 6)):
            names.setdefault(n, rng.randint(1, 50))
        text = " ".join(rng.choice(_PROSE) for _ in range(rng.randint(1, 4)))
        qs.append({"op": "autolink", "text": text, "names": [[k, v] for k, v in names.items()]}); want.append(autolink_entities(text, names))
    heads = ["<h1>Part I &amp; more</h1>", "<h2>Overview</h2>", "<h2>Overview</h2>", "<h3>Hooks <em>x</em></h3>", "<p>text</p>", "<h4>deep</h4>", "<h2>!!!</h2>",
             "<h2>Ünïcödé Tïtle</h2>", "\n", "<h1>Two\nlines</h1>"]
    for _ in range(400):
        html = "".join(rng.choice(heads) for _ in range(rng.randint(0, 8)))
        h, toc = _rules_toc(html, levels="123")
        qs.append({"op": "toc", "text": html, "levels": "123"}); want.append(json.loads(json.dumps([h, toc, split_rules_sections(h, include_h1=True)])))
    got = ask(qs)
    unsure = sum(1 for q, g in zip(qs, got) if q["op"] == "autolink" and g is None)
    bad = [(q, w, g) for q, w, g in zip(qs, want, got) if g is not None and w != g]
    assert not bad, f"{len(bad)} of {len(qs)} differ: {json.dumps(bad[:2])[:2000]}"
    assert unsure < 200, unsure


def test_json_dumps_is_pythons_floats_included():
    """nd_render::py_dumps (pages that embed json.dumps(...) of stored JSON): Python's separators, ensure_ascii and float repr."""
    import random
    import struct
    rnd = random.Random(7)
    floats = [0.0, -0.0, 1.0, 100.0, 0.1, 1e-7, 1e-4, 1.5e-4, 1e16, 1e15, 123456789012345.6, 1.5e16, 2.5e-300, 1.7976931348623157e308,
              5e-324, 3.14159, -42.5, 1e22, 0.00012, 9999999999999998.0]
    for _ in range(2000):
        f = struct.unpack("d", struct.pack("Q", rnd.getrandbits(64)))[0]
        if f == f and abs(f) != float("inf"):
            floats.append(f)
        floats.append(rnd.uniform(-1e6, 1e6))
    values = [floats, {"b": 1, "a": [True, None, "é\u2028<x>\x7f\"\\"], "c": {"z": 2.5, "y": -3}}, [1, 2, [3, {"k": 1e-9}]], "plain", 7]
    got = ask([{"op": "dumps", "value": v} for v in values])
    assert got == [json.dumps(v) for v in values]


_RULES_PIECES = ["## Combat\n\n", "### Initiative\n\nRoll **d20**.\n\n", ":::tip\nUse cover.\n:::\n\n", ":::gm Secret stuff\nThe *twist*.\n:::\n\n",
                 ":::collapse More <b>\nHidden **text**\n:::\n\n", ":::weird Typo'd\nbody\n:::\n\n", ":::note\nunclosed note\n",
                 "```statblock\nGoblin\n**AC**: 15\nHP: 7 (2d6)\nTime: 10:30\nFree text line\n\nmore free\n```\n\n", "```python\n:::tip\nnot a block\n```\n\n",
                 "Plain paragraph with <tags> & ampersands.\n\n", "## Repeated\n\n", "## Repeated\n\n", "text right after\n:::lore\nlore!\n:::\n"]


def test_rules_content_is_pythons():
    """::: blocks, statblocks and heading ids (main.rules_page's pipeline) - same tolerance as the markdown renderer itself. (No list
    pieces: loose lists and lazy continuation lines are the accepted CommonMark differences, not this pipeline's.)"""
    import importlib.util
    import random
    from app import rules_render
    from app.main import _rules_toc
    from app.rendering import render_md as _render_md
    spec = importlib.util.spec_from_file_location("golden", ROOT / "scripts" / "golden.py")
    golden = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(golden)

    def py(md, is_gm):
        skeleton, blocks = rules_render.extract_blocks(md)
        content, _ = _rules_toc(_render_md(skeleton))
        return rules_render.restore_blocks(content, blocks, is_gm)
    rng = random.Random(9)
    cases = [("".join(rng.choice(_RULES_PIECES) for _ in range(rng.randint(1, 7))), rng.random() < 0.5) for _ in range(600)]
    got = ask([{"op": "rules_content", "text": t, "is_gm": g} for t, g in cases])
    n = lambda h: golden.normalise("text/html", h.encode())
    bad = [(t, g) for (t, gm), g in zip(cases, got) if n(py(t, gm)) != n(g)]
    assert len(bad) < len(cases) * 0.05, f"{len(bad)} of {len(cases)} differ, e.g. {bad[:2]}"
    # the parts that are not markdown at all must match exactly
    for t, gm in cases:
        assert ("NDRULESBLOCK" in py(t, gm)) == ("NDRULESBLOCK" in got[cases.index((t, gm))])


def test_rules_sections_and_overlay_are_pythons():
    import random
    from app import rules_render
    html = ('<p>intro</p><h2 id="combat">Combat &amp; more</h2><p>a</p><h3 id="init">Init</h3><p>b</p><h2 id="magic">Magic</h2>'
            '<p>c</p><h2 id="gear">Gear</h2><p>d</p>')
    assert ask([{"op": "rules_sections", "html": html}])[0] == rules_render.split_rules_sections(html)
    sections = rules_render.split_rules_sections(html)
    rng = random.Random(4)
    slugs = ["combat", "init", "magic", "gear", "nope", ""]

    def rand_overlay():
        sec = {}
        for s in rng.sample(slugs, rng.randint(0, 4)):
            sec[s] = rng.choice([None, {}, {"icon": "⚔"}, {"title": "New <t>"}, {"icon": "🔮", "title": "Both"}, {"players_visible": False},
                                 {"order": rng.choice([1, 2, 0.5, -3])}, {"order": True}, {"title": 3}, "bad"])
        ov = {"sections": sec}
        if rng.random() < 0.6:
            ov["tabs"] = [rng.choice([{"label": "Core", "sections": ["combat", "init"]}, {"id": "x", "label": "Magic", "sections": ["magic", "magic"]},
                                      {"label": "  ", "sections": []}, {"label": "Dup", "id": "x", "sections": ["gear"]}, {"label": "Ghost", "sections": ["nope"]},
                                      {"label": "Bad", "sections": [1]}, "junk", {"label": "Ünï Côde!", "sections": "gear"}])
                         for _ in range(rng.randint(0, 3))]
        return json.dumps(ov)
    raws = [rand_overlay() for _ in range(400)] + ["", "  ", "{not json", "[]", '{"sections": []}', '{"tabs": {}}', "null"]
    for gm in (True, False):
        got = ask([{"op": "rules_overlay", "raw": r, "sections": sections, "is_gm": gm} for r in raws])
        for r, g in zip(raws, got):
            ov, _err = rules_render.parse_rules_overlay(r)
            flat, tabs = rules_render.apply_rules_overlay(sections, ov, gm)
            if g == "python":
                continue
            assert g == json.loads(json.dumps({"flat": flat, "tabs": tabs})), (r, gm)


def test_rules_unescape_is_pythons_or_left_to_python():
    import html
    texts = ["plain", "a &amp; b", "&lt;b&gt; &quot;q&quot; &apos;", "&nbsp;x", "&#65;&#x42;&#X43;", "R&D", "a & b", "&amp", "&#0;", "&#128;",
             "&#x1F600;", "&copy;", "&;", "& ", "&", "x&#9;y", "&#xD800;", "&#65", "&&amp;", "&#1114111;"]
    for t, g in zip(texts, ask([{"op": "unescape", "text": t} for t in texts])):
        assert g is None or g == html.unescape(t), t
    assert ask([{"op": "unescape", "text": "a &amp; b &#65;"}])[0] == "a & b A"
