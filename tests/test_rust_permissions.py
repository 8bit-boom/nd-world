"""The permission rules ported to Rust (rust/nd-perm, docs/RUST_PERMISSIONS.md) answer exactly what the Python originals answer.

Differential: every route of the app, with many spellings of each path, every method, every role, plus random section matrices, custom kinds
and tagged texts, are put to both. Runs when the Rust checker is built (`cd rust && cargo build -p nd-perm`, or ND_PERM_CHECK=<binary>);
CI's gateway job builds it."""
import json
import os
import random
import re
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
_CANDIDATES = [os.environ.get("ND_PERM_CHECK", ""), str(ROOT / "rust/target/release/nd-perm-check"), str(ROOT / "rust/target/debug/nd-perm-check")]
BIN = next((c for c in _CANDIDATES if c and Path(c).is_file()), None)
pytestmark = pytest.mark.skipif(BIN is None, reason="rust/nd-perm not built")

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def ask(questions):
    """Every question to the Rust checker in one process; answers in order."""
    data = "".join(json.dumps(q) + "\n" for q in questions)
    out = subprocess.run([BIN], input=data, capture_output=True, text=True, check=True).stdout.splitlines()
    assert len(out) == len(questions)
    return [json.loads(x) for x in out]


def _route_paths():
    from app.main import app as asgi
    from app import main
    routes = getattr(main, "_fastapi_app", asgi).routes
    return sorted({getattr(r, "path", "") for r in routes if getattr(r, "path", "")})


_FILLS = ["1", "42", "007", "new", "abc", "a-b", "x.y", "1/notes", "٣", "", "1\n", "edit", "%2F", "über", "1/2"]


def path_corpus():
    """Every route path with its {params} filled in many ways, the routes' prefixes, and spellings that try to slip past a rule."""
    rng = random.Random(1)
    out = set()
    for p in _route_paths():
        out.add(p)
        params = re.findall(r"\{[^}]+\}", p)
        for fill in _FILLS:
            q = p
            for prm in params:
                q = q.replace(prm, fill if ":path" not in prm else fill + "/deep", 1)
            out.add(q)
        for _ in range(3):
            q = p
            for prm in params:
                q = q.replace(prm, rng.choice(_FILLS), 1)
            out.add(q)
        parts = p.split("/")
        for i in range(1, len(parts)):
            out.add("/".join(parts[:i]) or "/")
        out.update({p + "/", p + "x", p + "\n", p + "/new", p.upper(), "/" + p.lstrip("/") + "/1"})
    out.update({"", "/", "//", "/boards/new", "/boards/new/notes", "/boards/new\n", "/boards/newx", "/boards/x/notes", "/boards//notes",
                "/maps/schematic", "/maps/schematic/", "/maps/new", "/maps/x", "/characters/templates", "/charactersX", "/uploads/../world.db",
                "/worlds/7/edit", "/worlds/77/edit", "/worlds/7/notes/3", "/worlds/7/notes/3/4/delete", "/kind/", "/account", "/accounts"})
    return sorted(out)


def test_route_allowlists_match_python_for_every_route_method_and_spelling():
    from app import main
    paths = path_corpus()
    qs, want = [], []
    for path in paths:
        for method in METHODS:
            qs.append({"op": "player", "method": method, "path": path}); want.append(main._is_player_safe(method, path))
            qs.append({"op": "assistant", "method": method, "path": path}); want.append(main._is_assistant_safe(method, path))
            for wid in (7, 12):
                qs.append({"op": "owner", "method": method, "path": path, "world_id": wid}); want.append(main._is_owner_safe(method, path, wid))
    got = ask(qs)
    bad = [(q, w, g) for q, w, g in zip(qs, want, got) if w != g]
    assert not bad, f"{len(bad)} of {len(qs)} differ, e.g. {bad[:8]}"
    assert len(qs) > 20000                                     # the corpus really covers the app


def test_the_gate_decision_matches_the_real_auth_gate():
    """app/main.py's own _auth_gate_rest (the part of auth_gate after the session is known) is run for every path / method / role, with a
    stand-in for the route: did the request get through, or did the gate answer 403? The Rust gate must agree every time."""
    import asyncio
    from starlette.requests import Request
    from app import main

    async def passed(request):
        from starlette.responses import Response
        return Response("through")

    def py_gate(method, path, is_gm, role, wid):
        scope = {"type": "http", "method": method, "path": path, "headers": [], "query_string": b"", "state": {}}
        req = Request(scope)
        user = SimpleNamespace(is_gm=is_gm, id=5)
        is_assistant, is_owner = role in ("assistant", "owner"), role == "owner"
        resp = asyncio.run(main._auth_gate_rest(req, passed, path, user, is_assistant, is_owner, wid))
        return resp.status_code == 200 and resp.body == b"through"

    qs, want = [], []
    for path in path_corpus()[::2]:
        for method in ("GET", "POST", "DELETE"):
            for is_gm, role, wid in ((True, None, None), (False, None, None), (False, "player", 7), (False, "assistant", 7), (False, "owner", 7), (False, "owner", 12)):
                qs.append({"op": "gate", "method": method, "path": path, "is_gm": is_gm, "role": role, "world_id": wid})
                want.append(py_gate(method, path, is_gm, role, wid))
    got = ask(qs)
    bad = [(q, w, g) for q, w, g in zip(qs, want, got) if w != g]
    assert not bad, f"{len(bad)} of {len(qs)} differ: {bad[:8]}"
    assert sum(want) and not all(want)


def _access_samples(rng):
    from app.deps import SECTION_PERMISSION_IDS
    ids = list(SECTION_PERMISSION_IDS) + ["kind_character", "kind_note", "kind_custom_x", "kind_custom_y", "kind_nope", "nope", ""]
    levels = ["none", "read", "edit", "EDIT", "", None, 1, True, ["edit"], {"x": 1}]
    samples = [None, "", "{}", "[]", "null", "not json", '{"dice": "none"}', '{"dice": {"player": "none"}}', "NaN", '{"maps": NaN}']
    for _ in range(300):
        d = {}
        for sid in rng.sample(ids, rng.randint(0, 8)):
            d[sid] = rng.choice([{"player": rng.choice(levels), "assistant": rng.choice(levels)}, {"player": rng.choice(levels)}, "edit", None, []])
        samples.append(json.dumps(d))
    kinds = [None, "", "[]", "{}", "junk", json.dumps([{"id": "custom_x", "label": "X"}, {"id": "custom_y", "label": "  "}, {"id": "y", "label": "Y"},
                                                          {"id": "custom_z"}, "str", {"id": 5, "label": "n"}, {"id": "custom_w", "label": "\x1c"}])]
    return ids, samples, kinds


def test_section_levels_match_python():
    from app import deps
    rng = random.Random(2)
    ids, samples, kinds = _access_samples(rng)
    qs, want = [], []
    for access in samples:
        for k in kinds:
            world = SimpleNamespace(section_access_json=access, custom_kinds_json=k)
            for sid in rng.sample(ids, 12):
                for is_gm, role in ((True, None), (False, "player"), (False, "assistant"), (False, None)):
                    user = SimpleNamespace(is_gm=is_gm) if (is_gm or role) else None
                    req = SimpleNamespace(state=SimpleNamespace(user=user, is_assistant=role == "assistant"))
                    try:
                        py = deps.world_section_level(req, world, sid)
                    except Exception as e:                            # pragma: no cover - would be a finding in itself
                        py = f"error {e!r}"
                    qs.append({"op": "section", "is_gm": is_gm, "role": role if not is_gm else None, "access": access, "kinds": k, "section": sid})
                    want.append(py)
    got = ask(qs)
    unsure = sum(1 for g in got if g is None)
    bad = [(q, w, g) for q, w, g in zip(qs, want, got) if g is not None and w != g]
    assert not bad, f"{len(bad)} differ: {bad[:6]}"
    assert unsure < len(qs) * 0.05, f"Rust is unsure too often ({unsure} of {len(qs)})"


def test_row_ownership_matches_python():
    from app import deps
    qs, want = [], []
    for level in ("none", "read", "edit"):
        for is_gm in (False, True):
            for is_assistant in (False, True):
                for uid in (None, 1, 2):
                    for created in (None, 1, 2):
                        user = SimpleNamespace(is_gm=is_gm, id=uid) if uid is not None or is_gm else None
                        req = SimpleNamespace(state=SimpleNamespace(user=user, is_assistant=is_assistant))
                        orig = deps.world_can_edit_section
                        deps.world_can_edit_section = lambda r, w, s, _l=level: _l == "edit"
                        try:
                            py = deps.world_can_edit_row(req, None, "quests", created)
                        finally:
                            deps.world_can_edit_section = orig
                        qs.append({"op": "row", "level": level, "is_gm": bool(user and is_gm), "is_assistant": is_assistant, "user_id": uid if user else None, "created_by": created})
                        want.append(py)
    assert ask(qs) == want


_PIECES = ["[gmonly]", "[/gmonly]", "[GMONLY]", "[for=1]", "[for=2,3]", "[for=1, 2]", "[for=]", "[for=x]", "[/for]", "[reveal=2020-01-01]",
           "[reveal=2999-01-01]", "[reveal=2026-10-21T10:00]", "[reveal=2026-10-21 10:01]", "[reveal=2026-02-30]", "[reveal=2026-10-21t09:59]",
           "[/reveal]", "[REVEAL=2020-01-01]", "secret", "text", "\n", " ", "[", "]", "ä", "[spoiler]", "1", "[for=1][for=2]"]


def test_gm_only_stripping_matches_python():
    from app import rendering
    rng = random.Random(3)
    now = datetime(2026, 10, 21, 10, 0, 30)
    texts = ["", "plain", "a [gmonly]b[/gmonly] c", "[for=1]mine[/for]", "[for=1]a[for=2]b[/for]c[/for]", "[reveal=2020-01-01]old[/reveal]",
             "open [for=1]never closed", "stray [/for] close", "x" * 50 + "[gmonly]" + "y" * 50, "[for=٣]arabic digit[/for]"]
    for _ in range(3000):
        texts.append("".join(rng.choice(_PIECES) for _ in range(rng.randint(1, 14))))
    qs, want = [], []
    for t in texts:
        for viewer in (None, 1, 2, 3):
            qs.append({"op": "strip", "text": t, "viewer": viewer, "now": [now.year, now.month, now.day, now.hour, now.minute]})
            want.append(rendering.strip_gm_only(t, viewer, now=now))
    got = ask(qs)
    unsure = sum(1 for g in got if g is None)
    bad = [(q["text"], q["viewer"], w, g) for q, w, g in zip(qs, want, got) if g is not None and w != g]
    assert not bad, f"{len(bad)} differ: {bad[:5]}"
    assert unsure < len(qs) * 0.02
