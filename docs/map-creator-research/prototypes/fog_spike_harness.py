"""Does fog of war fit inside the EXISTING player view? Boots the real app on a scratch database, builds a generated dungeon as
ordinary schematic elements, opens the real /maps/schematic/{slug}/view page as a player, injects fog-svg.js into it, and measures.

    python3 fog_spike_harness.py [--shots DIR]        (needs: node, playwright + chromium, the app's own requirements)

What is real: the server, the schematic routes, the player view template and schematic-render.js, the per-player payload.
What is not: the walls reach the page as a JSON literal the harness injects (a real feature would put them in the payload), and
there is no GPU here - Chromium paints in software, so absolute frame times are pessimistic. The point is the SHAPE of the cost.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
GM = ("gm@fog.local", "gm-fog-12345")
PLAYER = ("player@fog.local", "player-fog-12345")


def scene(w, h, seed, props):
    out = subprocess.run(["node", os.path.join(HERE, "export_scene.js"), str(w), str(h), str(seed), str(props)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def boot(db_dir):
    env = dict(os.environ, DB_PATH=os.path.join(db_dir, "world.db"), GM_EMAIL=GM[0], GM_PASSWORD=GM[1], SECRET_KEY="fog-spike")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
                            cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/login", timeout=1)
            return proc, env
        except Exception:
            time.sleep(1)
    proc.terminate()
    raise SystemExit("server did not start")


def seed_player(env):
    code = """
import sys; sys.path.insert(0, %r)
from app import auth
from app.database import SessionLocal
from app.models import User, World, WorldMembership, PlayerCharacter
db = SessionLocal(); world = db.query(World).first()
u = User(email=%r, password_hash=auth.hash_password(%r), display_name='Player', is_gm=False); db.add(u); db.commit(); db.refresh(u)
db.add(WorldMembership(world_id=world.id, user_id=u.id)); pc = PlayerCharacter(world_id=world.id, owner_user_id=u.id, name='Hero'); db.add(pc); db.commit()
print(world.slug, pc.id)
""" % (REPO, PLAYER[0], PLAYER[1])
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, check=True).stdout.split()
    return out[0], int(out[1])


def login(page, who):
    page.goto(f"{BASE}/login")
    page.fill("input[name=email]", who[0]); page.fill("input[name=password]", who[1])
    page.click("button[type=submit]"); page.wait_for_load_state("networkidle")


MEASURE = """
async ({ sources, reps }) => {
  const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const t = [];
  await frame();
  const base = []; for (let i = 0; i < reps; i++) { const a = performance.now(); await frame(); base.push(performance.now() - a); }
  let info;
  const upd = [], js = [];
  for (let i = 0; i < reps; i++) {
    // wiggle the sources a little so every run really recomputes
    const s = sources.map(p => ({ x: p.x + (i % 2) * 7, y: p.y, range: p.range }));
    const a = performance.now(); info = window.__fog.update(s, window.__tokens); const b = performance.now(); await frame(); upd.push(performance.now() - a); js.push(b - a);
  }
  const med = a => a.slice().sort((x, y) => x - y)[a.length >> 1];
  const rd = []; for (let i = 0; i < Math.min(reps, 5); i++) { const a = performance.now(); redraw(); rd.push(performance.now() - a); }
  return { emptyFrameMs: med(base), jsMs: med(js), updateToFrameMs: med(upd), redrawMs: med(rd), info };
}
"""


LIGHTS = """
async ({ n, hero, reps }) => {
  const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const svg = document.getElementById('sch-svg');
  const layer = ndMapLight.create(svg, { width: __fog_canvas.w, height: __fog_canvas.h, walls: window.__walls, darkness: 0.85, before: document.getElementById('token-layer') });
  const colours = ['#ffb066', '#66b2ff', '#ff6680', '#80ff99'];
  const lights = Array.from({ length: n }, (_, i) => ({ x: hero[0] + (i % 10) * 90 + (i >> 3) * 31, y: hero[1] + ((i * 37) % 7) * 40, range: 8 * 50, color: colours[i % 4], intensity: 1 }));
  await frame();
  const upd = [], js = [], base = [];
  for (let i = 0; i < reps; i++) { const a = performance.now(); await frame(); base.push(performance.now() - a); }
  let info;
  for (let i = 0; i < reps; i++) {
    const ls = lights.map(l => ({ ...l, x: l.x + (i % 2) * 5 }));
    const a = performance.now(); info = layer.update(ls); const b = performance.now(); await frame(); upd.push(performance.now() - a); js.push(b - a);
  }
  layer.destroy();
  const med = a => a.slice().sort((x, y) => x - y)[a.length >> 1];
  return { emptyFrameMs: med(base), jsMs: med(js), updateToFrameMs: med(upd), info };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None)
    ap.add_argument("--throttle", type=float, default=1.0, help="slow the player's CPU down by this factor (4 is roughly a mid-range phone)")
    ap.add_argument("--quick", action="store_true", help="two map sizes only")
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="fogspike-")
    proc, env = boot(tmp)
    try:
        world_slug, pc_id = seed_player(env)
        results = []
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--no-sandbox"])
            gm_ctx = browser.new_context(viewport={"width": 1400, "height": 900}); gm = gm_ctx.new_page(); login(gm, GM)
            gm_ctx.add_cookies([{"name": "active_world", "value": world_slug, "url": BASE}])
            pl_ctx = browser.new_context(viewport={"width": 1400, "height": 900}); pl = pl_ctx.new_page(); login(pl, PLAYER)
            pl_ctx.add_cookies([{"name": "active_world", "value": world_slug, "url": BASE}])
            slug = "fog-spike"
            if args.throttle != 1.0:
                pl_ctx.new_cdp_session(pl).send("Emulation.setCPUThrottlingRate", {"rate": args.throttle})
            scripts = [os.path.join(HERE, f) for f in ("geom.js", "vision.js", "fog-svg.js", "light-svg.js")]
            configs = [(60, 40, 101, 1000), (100, 70, 102, 0)] if args.quick else [(60, 40, 101, 0), (60, 40, 101, 1000), (60, 40, 101, 3000), (60, 40, 101, 6000), (100, 70, 102, 0), (100, 70, 102, 3000)]
            for (w, h, seed, props) in configs:
                sc = scene(w, h, seed, props)
                for e in sc["elements"]:
                    if e.get("pc_id") == "__PC__":
                        e["pc_id"] = pc_id
                gm.request.post(f"{BASE}/maps/schematic/{slug}/delete")          # recreate at this map's size
                gm.request.post(f"{BASE}/maps/schematic/new", form={"name": "fog spike", "canvas_width": str(sc["canvas"]["w"]), "canvas_height": str(sc["canvas"]["h"]), "canvas_bg": "dark"}, max_redirects=0)
                gm.request.post(f"{BASE}/maps/schematic/{slug}/grid", data=json.dumps({"grid_type": "square", "config": {"cell_size": 50, "offset_x": 0, "offset_y": 0, "unit_per_cell": 5, "unit_label": "ft"}}),
                                headers={"Content-Type": "application/json"})
                gm.request.post(f"{BASE}/maps/schematic/{slug}/elements", data=json.dumps({"elements": sc["elements"]}), headers={"Content-Type": "application/json"})
                pl.goto(f"{BASE}/maps/schematic/{slug}/view"); pl.wait_for_load_state("networkidle")
                for s in scripts:
                    pl.add_script_tag(path=s)
                setup = pl.evaluate("""({ walls, canvas, cell }) => {
                    window.__walls = walls; window.__fog_canvas = canvas;
                    const svg = document.getElementById('sch-svg');
                    const hero = elements.find(e => e.id === 'tk-hero');
                    window.__fog = ndMapFog.create(svg, { width: canvas.w, height: canvas.h, cell, walls, tokenLayer: document.getElementById('token-layer') });
                    window.__tokens = elements.filter(e => e.type === 'token').map(e => ({ x: e.x, y: e.y, always: e.id === 'tk-hero', node: document.querySelector('#token-layer [data-id="' + e.id + '"]') }));
                    return { elements: elements.length, nodes: document.getElementById('el-layer').childElementCount + document.getElementById('token-layer').childElementCount, hero: [hero.x, hero.y], walls: walls.length };
                }""", {"walls": sc["walls"], "canvas": sc["canvas"], "cell": sc["cell"]})
                hero = setup["hero"]
                for label, sources in (("1 source, range 16 cells", [{"x": hero[0], "y": hero[1], "range": 16 * 50}]),
                                       ("1 source, unlimited range", [{"x": hero[0], "y": hero[1], "range": float("inf")}]),
                                       ("4 sources, range 16 cells", [{"x": hero[0] + 50 * i, "y": hero[1], "range": 16 * 50} for i in range(4)]),
                                       ("8 sources, range 16 cells", [{"x": hero[0] + 25 * i, "y": hero[1], "range": 16 * 50} for i in range(8)])):
                    js_sources = [dict(s, range=(s["range"] if s["range"] != float("inf") else 1e9)) for s in sources]
                    m = pl.evaluate(MEASURE, {"sources": js_sources, "reps": 7})
                    results.append({"map": f"{w}x{h}", "elements": setup["elements"], "svgNodes": setup["nodes"], "walls": setup["walls"], "case": label, **{k: round(v, 1) if isinstance(v, float) else v for k, v in m.items()}})
                    print(json.dumps(results[-1]), flush=True)
                if (w, h, props) == (60, 40, 1000):
                    for n_lights in (1, 10, 30):
                        m = pl.evaluate(LIGHTS, {"n": n_lights, "hero": hero, "reps": 7})
                        results.append({"map": f"{w}x{h}", "elements": setup["elements"], "walls": setup["walls"], "case": f"{n_lights} coloured light(s), range 8 cells", **{k: round(v, 1) if isinstance(v, float) else v for k, v in m.items()}})
                        print(json.dumps(results[-1]), flush=True)
            if args.shots:                                  # pictures: a small map so the walls and tokens are legible
                os.makedirs(args.shots, exist_ok=True)
                w, h, seed, props = 30, 20, 7, 160
                sc = scene(w, h, seed, props)
                for e in sc["elements"]:
                    if e.get("pc_id") == "__PC__":
                        e["pc_id"] = pc_id
                gm.request.post(f"{BASE}/maps/schematic/{slug}/delete")
                gm.request.post(f"{BASE}/maps/schematic/new", form={"name": "fog spike", "canvas_width": str(sc["canvas"]["w"]), "canvas_height": str(sc["canvas"]["h"]), "canvas_bg": "dark"}, max_redirects=0)
                gm.request.post(f"{BASE}/maps/schematic/{slug}/grid", data=json.dumps({"grid_type": "square", "config": {"cell_size": 50, "offset_x": 0, "offset_y": 0, "unit_per_cell": 5, "unit_label": "ft"}}),
                                headers={"Content-Type": "application/json"})
                gm.request.post(f"{BASE}/maps/schematic/{slug}/elements", data=json.dumps({"elements": sc["elements"]}), headers={"Content-Type": "application/json"})
                gm.goto(f"{BASE}/maps/schematic/{slug}"); gm.wait_for_load_state("networkidle")
                gm.locator("#sch-svg").screenshot(path=os.path.join(args.shots, "fog-gm-view.png"))
                pl.goto(f"{BASE}/maps/schematic/{slug}/view"); pl.wait_for_load_state("networkidle")
                for sfile in scripts:
                    pl.add_script_tag(path=sfile)
                pl.evaluate("""({ walls, canvas, cell }) => {
                    const svg = document.getElementById('sch-svg');
                    window.__fog = ndMapFog.create(svg, { width: canvas.w, height: canvas.h, cell, walls, tokenLayer: document.getElementById('token-layer') });
                    window.__refresh = (x, y, range) => {
                        window.__tokens = elements.filter(e => e.type === 'token').map(e => ({ x: e.x, y: e.y, always: e.id === 'tk-hero', node: document.querySelector('#token-layer [data-id="' + e.id + '"]') }));
                        return window.__fog.update([{ x, y, range }], window.__tokens);
                    };
                }""", {"walls": sc["walls"], "canvas": sc["canvas"], "cell": sc["cell"]})
                pl.evaluate("""({ walls, canvas, rooms }) => {
                    window.__walls = walls;
                    const svg = document.getElementById('sch-svg');
                    window.__lightLayer = ndMapLight.create(svg, { width: canvas.w, height: canvas.h, walls, darkness: 0.88, before: document.getElementById('token-layer') });
                    const cols = ['#ffb066', '#66b2ff', '#ff6680', '#80ff99'];
                    window.__lightLayer.update(rooms.slice(0, 4).map((r, i) => ({ x: r.centre[0], y: r.centre[1], range: 9 * 50, color: cols[i], intensity: 1 })));
                }""", {"walls": sc["walls"], "canvas": sc["canvas"], "rooms": sc["rooms"]})
                pl.locator("#sch-svg").screenshot(path=os.path.join(args.shots, "lights-only.png"))
                pl.evaluate("() => window.__lightLayer.destroy()")
                steps = [sc["hero"]] + [r["centre"] for r in sc["rooms"][1:4]]
                for i, (x, y) in enumerate(steps):
                    pl.evaluate("([x, y]) => { const h = elements.find(e => e.id === 'tk-hero'); h.x = x; h.y = y; redraw(); window.__refresh(x, y, 12 * 50); }", [x, y])
                    pl.locator("#sch-svg").screenshot(path=os.path.join(args.shots, f"fog-player-step{i}.png"))
            browser.close()
        print("DONE")
        json.dump(results, open(os.path.join(tmp, "results.json"), "w"), indent=1)
        print("results:", os.path.join(tmp, "results.json"))
    finally:
        os.kill(proc.pid, signal.SIGTERM)


if __name__ == "__main__":
    main()
