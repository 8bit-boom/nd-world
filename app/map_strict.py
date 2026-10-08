"""Strict secrecy for a map ("S2"): the server decides what a player's browser may be told, instead of sending everything and
letting the browser's fog hide it.

Table-trust fog (static/js/map-fog*.js) draws a black shroud over a map the browser already holds in full, so a player who opens
the developer tools can read it all. With strict secrecy switched on (fog on + "strict"), the server keeps its own record of what
the party has seen and sends players ONLY that:

  * elements, walls and lights that touch squares the characters can see now or have explored
  * creatures (non-character tokens) only while they stand in a square a character can see right now
  * the map picture only as a masked copy (see mask_image) - the original file is refused to players
  * the explored area itself, so a second phone or the TV shows the same fog

"Seen" is computed here with the same angular sweep the browser uses (static/js/map-vision.js), from the tokens that carry a
character (pc_id), the GM's real walls (a secret door blocks like a wall, a closed door blocks, an open one does not) and the
fog's view range (0 = as far as MAX_VIEW_SQUARES). The explored area lives in Schematic.explored_json on the same cell grid as the
browser's memory (ndMapFog.memoryCell), is cumulative, and starts afresh when the GM resets the fog (the epoch changes).

It is recorded when a view of the map is loaded - the players' live view, or the TV - not on every drag, so a path walked while
nobody was looking at the map is not explored. What it does NOT hide: the size of the canvas, a floor/room shape that touches an
explored square is sent whole, and darkness is a browser effect (a dark square a character can see by line of sight is sent).

A leaf module (no router imports); pure apart from reading the stored JSON.
"""
import io
import json
import math

MAX_VIEW_SQUARES = 30
ARC_STEPS = 64
EPS = 1e-6


def memory_cell(width, height, cell):
    """The explored-memory grid's cell size - the same rule as ndMapFog.memoryCell, so the browser can load the runs as they are."""
    c = max(20.0, float(cell or 50))
    while math.ceil(width / c) * math.ceil(height / c) > 40000:
        c *= 2
    return c


def blocks_sight(s):
    if s["kind"] == "window":
        return False
    if s["kind"] == "door":
        return s.get("state") != "open"
    return True


def _hit(ox, oy, dx, dy, ax, ay, bx, by):
    ex, ey = bx - ax, by - ay
    den = dx * ey - dy * ex
    if -1e-12 < den < 1e-12:
        return math.inf
    px, py = ax - ox, ay - oy
    t = (px * ey - py * ex) / den
    u = (px * dy - py * dx) / den
    return t if t >= 0 and 0 <= u <= 1 else math.inf


def visibility_polygon(origin, segs, view_range):
    """The area `origin` sees within `view_range` px, as [(x, y), ...] by angle (a port of ndMapVision.visibilityPolygon)."""
    ox, oy = origin
    x0, y0, x1, y1 = ox - view_range, oy - view_range, ox + view_range, oy + view_range
    cand = [s for s in segs if blocks_sight(s)
            and not (max(s["x1"], s["x2"]) < x0 or min(s["x1"], s["x2"]) > x1 or max(s["y1"], s["y2"]) < y0 or min(s["y1"], s["y2"]) > y1)]
    cand += [dict(x1=x0, y1=y0, x2=x1, y2=y0), dict(x1=x1, y1=y0, x2=x1, y2=y1), dict(x1=x1, y1=y1, x2=x0, y2=y1), dict(x1=x0, y1=y1, x2=x0, y2=y0)]
    seen, angles = set(), []
    for s in cand:
        for px, py in ((s["x1"], s["y1"]), (s["x2"], s["y2"])):
            if (px, py) in seen:
                continue
            seen.add((px, py))
            a = math.atan2(py - oy, px - ox)
            angles += [a - EPS, a, a + EPS]
    angles += [-math.pi + 2 * math.pi * i / ARC_STEPS for i in range(ARC_STEPS)]
    pts = []
    for a in angles:
        dx, dy = math.cos(a), math.sin(a)
        best = view_range
        for c in cand:
            t = _hit(ox, oy, dx, dy, c["x1"], c["y1"], c["x2"], c["y2"])
            if t < best:
                best = t
        pts.append((a, ox + dx * best, oy + dy * best))
    pts.sort()
    return [(x, y) for _a, x, y in pts]


def cells_in_polygon(poly, cell, cols, rows):
    """Indexes of the grid cells whose CENTRE lies inside the polygon (scanline fill, even-odd)."""
    out = set()
    n = len(poly)
    if n < 3:
        return out
    ys = [p[1] for p in poly]
    r0, r1 = max(0, int(min(ys) // cell)), min(rows - 1, int(max(ys) // cell))
    for r in range(r0, r1 + 1):
        cy = (r + 0.5) * cell
        xs = []
        for i in range(n):
            (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
            if (ay <= cy < by) or (by <= cy < ay):
                xs.append(ax + (cy - ay) / (by - ay) * (bx - ax))
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            c0 = max(0, math.ceil(xs[k] / cell - 0.5))
            c1 = min(cols - 1, math.floor(xs[k + 1] / cell - 0.5))
            for c in range(c0, c1 + 1):
                out.add(r * cols + c)
    return out


def to_runs(cells):
    runs, prev, start = [], None, None
    for i in sorted(cells):
        if prev is not None and i == prev + 1:
            prev = i
            continue
        if prev is not None:
            runs += [start, prev - start + 1]
        start = prev = i
    if prev is not None:
        runs += [start, prev - start + 1]
    return runs


def from_runs(runs, size):
    out = set()
    if not isinstance(runs, list):
        return out
    for i in range(0, len(runs) - 1, 2):
        s, n = runs[i], runs[i + 1]
        if isinstance(s, int) and isinstance(n, int) and not isinstance(s, bool) and not isinstance(n, bool) and s >= 0 and n > 0:
            out.update(range(s, min(size, s + n)))
    return out


def is_active(fog):
    return bool(fog.get("enabled") and fog.get("strict"))


class View:
    """What the party has seen: `visible` (right now) and `explored` (ever, includes visible) as sets of cell indexes."""

    def __init__(self, cell, cols, rows, visible, explored, changed):
        self.cell, self.cols, self.rows = cell, cols, rows
        self.visible, self.explored, self.changed = visible, explored, changed

    def known(self):
        return self.explored | self.visible

    def runs(self):
        return {"cell": self.cell, "cols": self.cols, "rows": self.rows, "runs": to_runs(self.explored | self.visible)}

    def touches(self, x0, y0, x1, y1, cells=None):
        """Does the box overlap a known (or given) cell?"""
        cells = self.known() if cells is None else cells
        if not (math.isfinite(x0) and math.isfinite(y0) and math.isfinite(x1) and math.isfinite(y1)):
            return False
        c0, c1 = max(0, int(x0 // self.cell)), min(self.cols - 1, int(x1 // self.cell))
        r0, r1 = max(0, int(y0 // self.cell)), min(self.rows - 1, int(y1 // self.cell))
        if c1 < c0 or r1 < r0:
            return False
        if (c1 - c0 + 1) * (r1 - r0 + 1) > len(cells):
            return any(c0 <= i % self.cols <= c1 and r0 <= i // self.cols <= r1 for i in cells)
        return any((r * self.cols + c) in cells for r in range(r0, r1 + 1) for c in range(c0, c1 + 1))


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def character_positions(elements):
    out = []
    for e in elements or []:
        if isinstance(e, dict) and e.get("type") == "token" and e.get("pc_id"):
            x, y = _num(e.get("x")), _num(e.get("y"))
            if x is not None and y is not None:
                out.append((x, y))
    return out


def compute(elements, walls, fog, stored, canvas_w, canvas_h, grid_cell):
    """The View for these tokens and walls, merged with the stored explored record (a dict or None; reset when the fog epoch or
    the grid changed). `walls` are the GM's real walls (map_walls format)."""
    from . import map_walls
    cw, ch = float(canvas_w or 2000), float(canvas_h or 1500)
    cell = memory_cell(cw, ch, grid_cell)
    cols, rows = math.ceil(cw / cell), math.ceil(ch / cell)
    squares = fog.get("range") or MAX_VIEW_SQUARES
    reach = min(squares, MAX_VIEW_SQUARES) * float(grid_cell or 50)
    segs = map_walls.segments(walls)
    visible = set()
    for pos in character_positions(elements):
        visible |= cells_in_polygon(visibility_polygon(pos, segs, reach), cell, cols, rows)
        c, r = int(pos[0] // cell), int(pos[1] // cell)              # the square a character stands on is always seen
        if 0 <= c < cols and 0 <= r < rows:
            visible.add(r * cols + c)
    explored, valid = set(), False
    if isinstance(stored, dict) and stored.get("epoch") == fog.get("epoch") and stored.get("cell") == cell \
            and stored.get("cols") == cols and stored.get("rows") == rows:
        explored, valid = from_runs(stored.get("runs"), cols * rows), True
    changed = (not valid and bool(visible)) or not visible <= explored
    return View(cell, cols, rows, visible, explored | visible, changed)


def stored_record(view, epoch):
    return {"epoch": epoch, "cell": view.cell, "cols": view.cols, "rows": view.rows, "runs": to_runs(view.explored)}


# ── what a player may be sent ───────────────────────────────────────────────────────────────

def element_box(e):
    """(x0, y0, x1, y1) around an element, generously, or None when it cannot be told (such an element is not sent)."""
    t = e.get("type")
    g = lambda k: _num(e.get(k))
    try:
        if t in ("rect", "image"):
            x, y, w, h = g("x"), g("y"), g("w"), g("h")
            if None in (x, y, w, h):
                return None
            if t == "image" and e.get("rot"):
                pad = abs(w) + abs(h)
                return (x - pad, y - pad, x + w + pad, y + h + pad)
            return (min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h))
        if t == "circle":
            cx, cy, rx, ry = g("cx"), g("cy"), g("rx"), g("ry")
            return None if None in (cx, cy, rx, ry) else (cx - abs(rx), cy - abs(ry), cx + abs(rx), cy + abs(ry))
        if t in ("line", "arrow", "measure", "aoe"):
            x1, y1, x2, y2 = g("x1"), g("y1"), g("x2"), g("y2")
            if None in (x1, y1, x2, y2):
                return None
            pad = math.hypot(x2 - x1, y2 - y1) if t == "aoe" else 0
            return (min(x1, x2) - pad, min(y1, y2) - pad, max(x1, x2) + pad, max(y1, y2) + pad)
        if t in ("poly", "path"):
            pts = e.get("points") or e.get("pts")
            xs = [_num(p[0]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
            ys = [_num(p[1]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
            if not xs or None in xs or None in ys:
                return None
            return (min(xs), min(ys), max(xs), max(ys))
        if t in ("text", "pin", "token"):
            x, y = g("x"), g("y")
            if x is None or y is None:
                return None
            r = max(40.0, (_num(e.get("r")) or 20.0) + 4) if t == "token" else 200.0
            return (x - r, y - r, x + r, y + r)
    except (TypeError, IndexError, AttributeError):
        return None
    return None


def filter_elements(elements, view):
    """The elements a player may be sent under strict secrecy (call after player_visible)."""
    out = []
    for e in elements:
        if e.get("type") == "token":
            if e.get("pc_id"):
                out.append(e)
                continue
            x, y = _num(e.get("x")), _num(e.get("y"))
            if x is not None and y is not None and view.touches(x, y, x, y, view.visible):
                out.append(e)
            continue
        box = element_box(e)
        if box and view.touches(*box):
            out.append(e)
    return out


def filter_walls(walls, view):
    out = []
    for w in walls:
        xs = [p[0] for p in w.get("pts", [])]
        ys = [p[1] for p in w.get("pts", [])]
        if xs and view.touches(min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1):
            out.append(w)
    return out


def filter_lights(lights, view):
    return [l for l in lights if view.touches(l["x"], l["y"], l["x"], l["y"])]


# ── the map picture ──────────────────────────────────────────────────────────────────────────

MAX_MASKED_SIDE = 2400


def mask_image(path, view, canvas_w, canvas_h, bg_hex="#111111"):
    """WebP bytes of the map picture with every unknown cell replaced by the background colour. The result is scaled so its
    longer side is at most MAX_MASKED_SIDE. The original file is never sent to a player."""
    from PIL import Image, ImageColor
    im = Image.open(path)
    im.load()
    im = im.convert("RGB")
    scale = min(1.0, MAX_MASKED_SIDE / max(canvas_w, canvas_h))
    w, h = max(1, round(canvas_w * scale)), max(1, round(canvas_h * scale))
    im = im.resize((w, h), Image.LANCZOS)
    m = Image.new("L", (view.cols, view.rows), 0)
    px = m.load()
    for i in view.known():
        px[i % view.cols, i // view.cols] = 255
    full = view.cell * view.cols, view.cell * view.rows
    m = m.resize((max(1, round(full[0] * scale)), max(1, round(full[1] * scale))), Image.NEAREST).crop((0, 0, w, h))
    base = Image.new("RGB", (w, h), ImageColor.getrgb(bg_hex))
    base.paste(im, (0, 0), m)
    buf = io.BytesIO()
    base.save(buf, "WEBP", quality=88, method=4)
    return buf.getvalue()
