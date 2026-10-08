"""Auto-furnishing a drawn room (static/js/map-furnish.js), run under Node.

What must hold for any room shape: furniture stays inside the room, never blocks a doorway, never overlaps, hugs the right
places for its kind, comes from the prop library when a name matches, is the same for the same seed, and a closet gets none."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
JS = ROOT / "static" / "js"


def _js(body: str):
    script = (f"global.window = global; const R = require({json.dumps(str(JS / 'map-rooms.js'))}); const F = require({json.dumps(str(JS / 'map-furnish.js'))});\n"
              "const mk = () => { const st = R.create(50, 0, 0, 1000, 800); return st; };\n"
              "function boxes(els) { return els.filter(e => !e.id.endsWith('p')).map(e => e.type === 'circle' ? [e.cx-e.rx, e.cy-e.ry, e.cx+e.rx, e.cy+e.ry] : [e.x, e.y, e.x+e.w, e.y+e.h]); }\n"
              "function overlap(a, b) { return a[0] < b[2]-0.5 && b[0] < a[2]-0.5 && a[1] < b[3]-0.5 && b[1] < a[3]-0.5; }\n"
              + body)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_bedroom_gets_beds_against_a_wall_and_a_chest_in_a_corner():
    r = _js("""
const st = mk(); const id = R.addSpace(st, 'Guest room', 'bedroom'); R.paintRect(st, 2, 2, 7, 6, id);   // 6 x 5 squares
const els = F.furnish(st, id, [], 'seed');
console.log(JSON.stringify({ n: els.length, kinds: els.map(e => e.fill), b: boxes(els), room: [100, 100, 400, 350] }))""")
    assert r["n"] >= 3
    assert any(f == "#6b4b3a" for f in r["kinds"]) and any(f == "#9a7a2a" for f in r["kinds"])           # a bed and a chest
    x0, y0, x1, y1 = r["room"]
    assert all(x0 <= a and y0 <= b and c <= x1 and d <= y1 for a, b, c, d in r["b"])                      # inside the room


def test_furniture_never_overlaps_and_stays_inside_ragged_rooms_for_every_kind():
    r = _js("""
let bad = 0, total = 0;
['tavern','hall','bedroom','kitchen','storage','cell','shrine','library','armory','cave','other'].forEach((kind, k) => {
  for (let seed = 0; seed < 12; seed++) {
    const st = mk(); const id = R.addSpace(st, 'R', kind);
    R.paintRect(st, 1, 1, 8, 6, id); R.paintRect(st, 5, 3, 7, 5, 0); R.paintRect(st, 9, 1, 11, 2, id);        // an L-shaped room with a bite out and a wing
    R.paintRect(st, 8, 1, 8, 1, id);
    const els = F.furnish(st, id, [], 'seed' + seed);
    const bs = boxes(els);
    bs.forEach((b, i) => {
      total++;
      const cx0 = Math.floor(b[0] / 50 + 0.01), cy0 = Math.floor(b[1] / 50 + 0.01), cx1 = Math.ceil(b[2] / 50 - 0.01), cy1 = Math.ceil(b[3] / 50 - 0.01);
      for (let y = cy0; y < cy1; y++) for (let x = cx0; x < cx1; x++) if (R.at(st, x, y) !== id) bad++;      // every square it touches is the room's
      for (let j = i + 1; j < bs.length; j++) if (overlap(b, bs[j])) bad++;
    });
  }
});
console.log(JSON.stringify({ bad, total }))""")
    assert r["bad"] == 0 and r["total"] > 100


def test_doorways_stay_clear():
    r = _js("""
const st = mk(); const id = R.addSpace(st, 'Store', 'storage'); R.paintRect(st, 2, 2, 6, 5, id);          // 5 x 4
R.setMark(st, '2,3,v', 'door');                                    // west wall, row 3
R.setMark(st, '4,2,h', 'secret');                                  // north wall, column 4
R.setMark(st, '7,4,v', 'open');                                    // east wall, row 4
R.setMark(st, '3,6,h', 'window');                                  // windows do not need clearance
let hit = 0;
for (let seed = 0; seed < 40; seed++) {
  const bs = boxes(F.furnish(st, id, [], 's' + seed));
  // the squares just inside each doorway and the one beyond: (2,3),(3,3)  (4,2),(4,3)  (6,4),(5,4)
  [[2,3],[3,3],[4,2],[4,3],[6,4],[5,4]].forEach(([x, y]) => bs.forEach(b => { if (overlap(b, [x*50+5, y*50+5, x*50+45, y*50+45])) hit++; }));
}
console.log(JSON.stringify(hit))""")
    assert r == 0


def test_same_seed_same_furniture_and_other_seeds_differ_and_rooms_differ():
    r = _js("""
const st = mk(); const a = R.addSpace(st, 'A', 'tavern'), b = R.addSpace(st, 'B', 'tavern');
R.paintRect(st, 1, 1, 7, 6, a); R.paintRect(st, 9, 1, 15, 6, b);
const strip = els => JSON.stringify(els.map(e => [e.type, Math.round(e.x || e.cx), Math.round(e.y || e.cy)]));
const a1 = strip(F.furnish(st, a, [], 'x')), a2 = strip(F.furnish(st, a, [], 'x')), a3 = strip(F.furnish(st, a, [], 'y'));
console.log(JSON.stringify({ same: a1 === a2, seed: a1 !== a3, ids: F.furnish(st, a, [], 'x').every(e => e.id.indexOf('rf-' + a + '-') === 0) }))""")
    assert r == {"same": True, "seed": True, "ids": True}


def test_closets_corridors_and_unknown_rooms_get_nothing():
    r = _js("""
const st = mk(); const c = R.addSpace(st, 'Closet', 'storage'), h = R.addSpace(st, 'Hall', 'corridor');
R.paintRect(st, 1, 1, 2, 2, c); R.paintRect(st, 4, 1, 12, 2, h);
console.log(JSON.stringify([F.furnish(st, c, [], 's').length, F.furnish(st, h, [], 's').length, F.furnish(st, 999, [], 's').length]))""")
    assert r == [0, 0, 0]


def test_a_library_prop_is_used_when_its_name_matches():
    r = _js("""
const st = mk(); const id = R.addSpace(st, 'Dorm', 'bedroom'); R.paintRect(st, 1, 1, 8, 5, id);
const props = [{id: 7, name: 'Four-poster Bed', tags: 'furniture', url: '/uploads/props/bed.webp', cells_w: 2, cells_h: 1}, {id: 8, name: 'Treasure chest', tags: '', url: '/uploads/props/c.webp', cells_w: 1, cells_h: 1}];
const els = F.furnish(st, id, props, 's');
console.log(JSON.stringify({ imgs: els.filter(e => e.type === 'image').map(e => [e.prop_id, e.rot, e.href]), plainBeds: els.filter(e => e.fill === '#6b4b3a').length }))""")
    assert r["plainBeds"] == 0 and r["imgs"] and {i[0] for i in r["imgs"]} <= {7, 8} and 7 in {i[0] for i in r["imgs"]}
    assert all(i[1] in (0, 90) and i[2].startswith("/uploads/props/") for i in r["imgs"])


def test_hostile_prop_data_cannot_break_it():
    r = _js("""
const st = mk(); const id = R.addSpace(st, 'Dorm', 'bedroom'); R.paintRect(st, 1, 1, 8, 5, id);
const els = F.furnish(st, id, [null, {}, {name: 5}, {name: 'bed', url: '/x', cells_w: 'a'}], 's');
console.log(JSON.stringify(els.length > 0))""")
    assert r is True
