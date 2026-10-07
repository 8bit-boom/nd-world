"""What would server-side vision cost in Python? The same angular-sweep idea as vision.js, in numpy, on the SAME generated maps.

The JS side writes the maps with `node export_for_py.js` (walls as JSON); this script times, per observer:
  - a numpy implementation (all rays against all candidate walls at once), and
  - a plain-Python loop (what the code would be without numpy).
It only matters for the "server decides what each player may see" design; a browser-side fog needs none of it.

    node export_for_py.js > /tmp/maps.json && python3 -I vision_bench.py /tmp/maps.json
"""
import json
import math
import sys
import time

import numpy as np

EPS = 1e-6


def candidates(segs, ox, oy, r):
    return [s for s in segs if s[6] and max(s[0], s[2]) >= ox - r and min(s[0], s[2]) <= ox + r and max(s[1], s[3]) >= oy - r and min(s[1], s[3]) <= oy + r]


def rays(cands, ox, oy, arc_steps=64):
    pts = {(s[0], s[1]) for s in cands} | {(s[2], s[3]) for s in cands}
    base = [math.atan2(y - oy, x - ox) for x, y in pts]
    out = []
    for a in base:
        out.extend((a - EPS, a, a + EPS))
    out.extend(-math.pi + 2 * math.pi * i / arc_steps for i in range(arc_steps))
    return out


def visibility_numpy(ox, oy, segs, r, w, h):
    box = [(ox - r, oy - r, ox + r, oy - r, 0, 0, True), (ox + r, oy - r, ox + r, oy + r, 0, 0, True),
           (ox + r, oy + r, ox - r, oy + r, 0, 0, True), (ox - r, oy + r, ox - r, oy - r, 0, 0, True)]
    c = candidates(segs, ox, oy, r) + box
    ang = np.array(rays(c, ox, oy))
    dx, dy = np.cos(ang)[:, None], np.sin(ang)[:, None]
    a = np.array([[s[0], s[1], s[2], s[3]] for s in c])
    ex, ey = (a[:, 2] - a[:, 0])[None, :], (a[:, 3] - a[:, 1])[None, :]
    den = dx * ey - dy * ex
    px, py = (a[:, 0] - ox)[None, :], (a[:, 1] - oy)[None, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (px * ey - py * ex) / den
        u = (px * dy - py * dx) / den
    ok = (np.abs(den) > 1e-12) & (t >= 0) & (u >= 0) & (u <= 1)
    best = np.where(ok, t, np.inf).min(axis=1)
    best = np.minimum(best, r)
    order = np.argsort(ang)
    return np.stack([ox + np.cos(ang) * best, oy + np.sin(ang) * best], axis=1)[order]


def visibility_python(ox, oy, segs, r, w, h):
    box = [(ox - r, oy - r, ox + r, oy - r, 0, 0, True), (ox + r, oy - r, ox + r, oy + r, 0, 0, True),
           (ox + r, oy + r, ox - r, oy + r, 0, 0, True), (ox - r, oy + r, ox - r, oy - r, 0, 0, True)]
    c = candidates(segs, ox, oy, r) + box
    out = []
    for a in rays(c, ox, oy):
        dx, dy = math.cos(a), math.sin(a)
        best = r
        for s in c:
            ex, ey = s[2] - s[0], s[3] - s[1]
            den = dx * ey - dy * ex
            if -1e-12 < den < 1e-12:
                continue
            px, py = s[0] - ox, s[1] - oy
            t = (px * ey - py * ex) / den
            u = (px * dy - py * dx) / den
            if t >= 0 and 0 <= u <= 1 and t < best:
                best = t
        out.append((a, ox + dx * best, oy + dy * best))
    out.sort()
    return out


def median_ms(fn, reps=5):
    fn()
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return ts[len(ts) // 2]


def main(path):
    maps = json.load(open(path))
    print(f"python {sys.version.split()[0]}, numpy {np.__version__}; median ms for ONE observer (6 observers averaged)\n")
    print("| map | wall segments | range 8: numpy | range 16: numpy | range 16: pure Python | range 40: numpy | range 40: pure Python |")
    print("|---|---|---|---|---|---|---|")
    for m in maps:
        segs, obs, w, h = m["segs"], m["observers"], m["w"], m["h"]
        row = []
        for r, impl in ((8, visibility_numpy), (16, visibility_numpy), (16, visibility_python), (40, visibility_numpy), (40, visibility_python)):
            row.append(sum(median_ms(lambda o=o: impl(o[0], o[1], segs, r, w, h), 3) for o in obs) / len(obs))
        print(f"| {m['name']} | {len(segs)} | " + " | ".join(f"{v:.1f}" for v in row) + " |")


if __name__ == "__main__":
    main(sys.argv[1])
