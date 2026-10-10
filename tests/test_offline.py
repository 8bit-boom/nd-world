"""Offline at the table: the service worker (static/sw.js, served at /sw.js), its routing rules, and the queue of HP / Shock
changes (static/js/nd-offline-core.js, run under Node)."""
import json
import shutil
import subprocess

import pytest

from .test_character_hub import _as, _pc

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def _node(script: str):
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, cwd=".")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_worker_is_served_from_the_root_without_a_login(client):
    r = client.get("/sw.js", follow_redirects=False)
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["service-worker-allowed"] == "/" and r.headers["cache-control"] == "no-cache"
    assert "nd-pages-" in r.text


def test_logout_clears_the_offline_copies(client, seed):
    from .conftest import PLAYER_PASSWORD, login
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303 and "cache" in r.headers["clear-site-data"] and "storage" in r.headers["clear-site-data"]


def test_the_character_page_loads_the_offline_scripts_before_anything_can_fetch(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _as(client, seed.player_a)
    html = client.get(f"/characters/{pc}").text
    assert html.index("nd-offline-core.js") < html.index("nd-offline.js") < html.index('id="pc-hub"') + 99999
    assert html.index("nd-offline.js") < html.index("function hpAjax")      # the fetch wrapper is in place before the sheet's code runs


@needs_node
def test_worker_routing_only_touches_the_sheet_the_strip_and_static():
    r = _node("""const sw = require('./static/sw.js');
      const out = {
        asset: sw.route('GET', '/static/js/x.js', 'no-cors', ''),
        sheet: sw.route('GET', '/characters/12', 'navigate', '?w=world'),
        sheet_slash: sw.route('GET', '/characters/12/', 'navigate', ''),
        embedded: sw.route('GET', '/characters/12', 'navigate', '?embed=1'),
        subresource: sw.route('GET', '/characters/12', 'cors', ''),
        edit: sw.route('GET', '/characters/12/edit', 'navigate', ''),
        now: sw.route('GET', '/api/characters/12/hub/now', 'cors', ''),
        other_api: sw.route('GET', '/api/characters/12/vitals', 'cors', ''),
        post: sw.route('POST', '/api/characters/12/hp-async', 'cors', ''),
        key: sw.cacheKey('https://x.test/characters/12/?w=a&embed=0'),
        injected: sw.injectOffline('<html><head><title>t</title></head></html>')};
      console.log(JSON.stringify(out));""")
    assert r["asset"] == "asset" and r["sheet"] == "page" and r["sheet_slash"] == "page" and r["now"] == "api"
    assert r["embedded"] is None and r["subresource"] is None and r["edit"] is None and r["other_api"] is None and r["post"] is None
    assert r["key"] == "https://x.test/characters/12"
    assert r["injected"].startswith("<html><head><script>window.__ND_OFFLINE__=true;</script>")


@needs_node
def test_only_hp_and_shock_deltas_are_queueable():
    r = _node("""const c = require('./static/js/nd-offline-core.js');
      console.log(JSON.stringify([
        c.classify('POST', '/api/characters/7/hp-async', {action: 'delta', value: -3}),
        c.classify('POST', '/api/characters/7/shock', {action: 'delta', value: 2}),
        c.classify('POST', '/api/characters/7/hp-async', {action: 'set', value: 4}),
        c.classify('POST', '/api/characters/7/xp', {action: 'delta', value: 4}),
        c.classify('GET', '/api/characters/7/hp-async', {action: 'delta', value: 1}),
        c.classify('POST', '/api/characters/7/hp-async', {action: 'delta', value: 0})]));""")
    assert r[0] == {"pc": 7, "kind": "hp", "delta": -3} and r[1] == {"pc": 7, "kind": "shock", "delta": 2}
    assert r[2:] == [None, None, None, None]


@needs_node
def test_offline_changes_show_at_once_stay_in_bounds_and_merge():
    r = _node("""const c = require('./static/js/nd-offline-core.js');
      const s = {hp: 10, max_hp: 12, temp_hp: 0, shock: 1, shock_max: 4};
      const a = c.apply(s, {kind: 'hp', delta: -4});
      const b = c.apply(s, {kind: 'hp', delta: -9});
      const h = c.apply(s, {kind: 'hp', delta: 50});
      const sh = c.apply(s, {kind: 'shock', delta: 9});
      let q = [];
      c.enqueue(q, {pc: 1, kind: 'hp', delta: -2}); c.enqueue(q, {pc: 1, kind: 'hp', delta: -1}); c.enqueue(q, {pc: 1, kind: 'shock', delta: 1});
      c.enqueue(q, {pc: 1, kind: 'shock', delta: -1});                       // cancels out: nothing to send
      c.enqueue(q, {pc: 2, kind: 'hp', delta: 3});
      console.log(JSON.stringify({a, b, h, sh, q, req: c.requestFor(q[0])}));""")
    assert r["a"] == {"current_hp": 6, "max_hp": 12, "temp_hp": 0, "queued": True}
    assert r["b"]["current_hp"] == 0 and r["h"]["current_hp"] == 12 and r["sh"]["shock_current"] == 4
    assert r["q"] == [{"pc": 1, "kind": "hp", "delta": -3}, {"pc": 2, "kind": "hp", "delta": 3}]
    assert r["req"] == {"url": "/api/characters/1/hp-async", "body": {"action": "delta", "value": -3}}


@needs_node
def test_offline_scripts_parse():
    for f in ("static/sw.js", "static/js/nd-offline.js", "static/js/nd-offline-core.js"):
        out = subprocess.run([NODE, "--check", f], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
