"""Inline <script> blocks in rendered pages must at least PARSE.

A Jinja template that emits a raw newline inside a single-quoted JS string
(easy to do by writing '\\n' through a Python string) is a SyntaxError that
kills the whole script block: every function in it is undefined and the
buttons silently do nothing. Python tests never see that, so this renders the
main pages with realistic data and runs every inline script through node's
parser (skipped when node isn't installed).

Also pins the world-qualified API URL shape: `wq` appends `?w=slug`, so it
must wrap the COMPLETE path — `('/api/x/1' ~ '/rest')|wq` — not a prefix of it
(`/api/x/1?w=slug/rest` is a 404).
"""
import json
import re
import shutil
import subprocess

import pytest

from app.database import SessionLocal
from app.models import Entity, GameSession, Party, PlayerCharacter, Quest, SheetTemplate

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

_CHECKER = r"""
const vm = require('vm');
const items = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const bad = [];
for (const it of items) {
  try { new vm.Script(it.code); }
  catch (e) { bad.push({page: it.page, n: it.n, error: String(e.message), head: it.code.trim().slice(0, 90)}); }
}
console.log(JSON.stringify(bad));
"""
_SCRIPT_RE = re.compile(r"<script([^>]*)>(.*?)</script>", re.S)
_JS_TYPES = ("", "text/javascript", "application/javascript")


def _inline_scripts(page, html):
    out = []
    for n, m in enumerate(_SCRIPT_RE.finditer(html)):
        attrs, code = m.group(1), m.group(2)
        if "src=" in attrs or not code.strip():
            continue
        t = re.search(r'type\s*=\s*["\']([^"\']*)["\']', attrs)
        if (t.group(1).lower() if t else "") not in _JS_TYPES:
            continue
        out.append({"page": page, "n": n, "code": code})
    return out


def _world(client, seed):
    client.cookies.set("active_world", seed.world_a.slug)


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def test_rendered_pages_have_parseable_inline_scripts(client, seed):
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Hero"))
    db = SessionLocal()
    try:
        custom = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first()
        custom_tpl = custom.id if custom else None
    finally:
        db.close()
    custom_pc = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id,
                                     name="Custom Hero", sheet_template_id=custom_tpl))
    npc = _add(Entity(world_id=seed.world_a.id, kind="character", name="Cook"))
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc]),
                       member_entity_ids_json=json.dumps([npc])))
    _add(Quest(world_id=seed.world_a.id, title="Errand", assigned_party_id=party))
    # the quest page's AI-sync panel (and its script) only exists once a session does
    _add(GameSession(world_id=seed.world_a.id, title="Session one"))

    pages = {
        "gm": ["/", "/parties", f"/parties/{party}", f"/parties/{party}/summary", "/characters",
               f"/characters/{pc}", f"/characters/{custom_pc}", "/quests", "/calendar", "/calendar/year", "/combat",
               "/cockpit", "/player-cockpit", "/sessions", "/tables", "/settings", "/npc-talk",
               "/new", f"/entity/{npc}/edit", "/races", "/professions"],
        "player": [f"/characters/{pc}", f"/characters/{custom_pc}", f"/parties/{party}", "/player-cockpit", "/npc-talk"],
    }
    scripts, checked = [], []
    for who, paths in pages.items():
        login(client, seed.gm.email if who == "gm" else seed.player_a.email,
              GM_PASSWORD if who == "gm" else PLAYER_PASSWORD)
        _world(client, seed)
        for path in paths:
            r = client.get(path)
            if r.status_code != 200:
                continue
            checked.append(f"{who}:{path}")
            scripts += _inline_scripts(f"{who}:{path}", r.text)

    assert len(checked) >= 12, f"too few pages rendered to be meaningful: {checked}"
    res = subprocess.run([NODE, "-e", _CHECKER], input=json.dumps(scripts), capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    bad = json.loads(res.stdout)
    assert not bad, "inline script(s) with syntax errors:\n" + "\n".join(
        f"  {b['page']} script #{b['n']}: {b['error']}  <- {b['head']!r}" for b in bad)


def test_party_page_api_urls_are_world_qualified_correctly(client, seed):
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, name="Hero"))
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc])))
    login(client, seed.gm.email, GM_PASSWORD)
    _world(client, seed)
    html = client.get(f"/parties/{party}").text
    for tail in ("members/toggle", "rest/undo", "rest"):
        assert f"/api/parties/{party}/{tail}?w=" in html, f"{tail}: qualifier must come AFTER the full path"
    assert not re.search(r"/api/parties/\d+\?w=[^'\"]*/", html), "a path segment was appended after ?w=slug"
