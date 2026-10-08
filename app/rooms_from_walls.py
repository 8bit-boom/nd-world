"""Rooms found in a map's walls: the enclosed areas become spaces the room tool (static/js/map-rooms.js) can edit.

A map imported from Universal VTT is a painted picture plus wall lines (app/uvtt_import.py): the floors and rooms exist only in
the picture. This reads them back out. The map is cut into grid squares; two neighbouring squares are in the same room unless a wall
(a door and a window included) runs between their centres; each connected patch of squares is a room; the patch that is the open
outside (it fills the border) is not. A door, secret door or window on the line between two rooms (or a room and the outside)
becomes the matching mark on that edge. Squares are the map's own grid, so a wall that does not follow the grid is approximated by
the nearest square edges - the result is the room tool's version of the map, not a copy of the picture's.

A leaf module (no router imports); the output is app/map_rooms.py's stored form (validate it with clean_rooms).
"""
import math

MAX_SPACES = 80
MIN_ROOM_SQUARES = 2
BLOCKING = ("wall", "door", "secret", "window")


def _cross(ax, ay, bx, by, cx, cy, dx, dy):
    """Do segments ab and cd properly cross?"""
    def orient(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1, d2 = orient(cx, cy, dx, dy, ax, ay), orient(cx, cy, dx, dy, bx, by)
    d3, d4 = orient(ax, ay, bx, by, cx, cy), orient(ax, ay, bx, by, dx, dy)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0


def detect(walls, canvas_w, canvas_h, cell, ox=0.0, oy=0.0):
    """(state, info) or (None, reason). `walls` are map_walls records in canvas pixels."""
    cell = float(cell or 50)
    ox, oy = ox % cell, oy % cell
    cols, rows = int((canvas_w - ox) // cell), int((canvas_h - oy) // cell)
    if not (1 <= cols <= 400 and 1 <= rows <= 400 and cols * rows <= 40000):
        return None, "The map has too many squares for the room tool (at most 400 a side, 40,000 in all)."
    segs = []
    for w in walls or []:
        if w.get("kind") not in BLOCKING:
            continue
        pts = w.get("pts") or []
        for a, b in zip(pts, pts[1:]):
            try:
                if not all(math.isfinite(float(v)) for v in (a[0], a[1], b[0], b[1])):
                    continue
            except (TypeError, ValueError, IndexError):
                continue
            segs.append((a[0], a[1], b[0], b[1], w.get("kind"), w.get("id")))
    if not segs:
        return None, "The map has no walls to find rooms in."

    # which segments can matter to which square: a spatial hash on the squares each segment's box touches
    near = {}
    for i, (x1, y1, x2, y2, _k, _i) in enumerate(segs):
        c0, c1 = int((min(x1, x2) - ox) // cell) - 1, int((max(x1, x2) - ox) // cell) + 1
        r0, r1 = int((min(y1, y2) - oy) // cell) - 1, int((max(y1, y2) - oy) // cell) + 1
        for r in range(max(0, r0), min(rows - 1, r1) + 1):
            for c in range(max(0, c0), min(cols - 1, c1) + 1):
                near.setdefault(r * cols + c, []).append(i)

    def centre(c, r):
        return ox + (c + 0.5) * cell, oy + (r + 0.5) * cell

    def blocked(c0, r0, c1, r1):
        ax, ay = centre(c0, r0)
        bx, by = centre(c1, r1)
        for i in near.get(r0 * cols + c0, ()):
            x1, y1, x2, y2, _k, _i = segs[i]
            if _cross(ax, ay, bx, by, x1, y1, x2, y2):
                return True
        return False

    comp = [-1] * (cols * rows)
    sizes, touches_border = [], []
    for start in range(cols * rows):
        if comp[start] != -1:
            continue
        n = len(sizes)
        comp[start] = n
        stack, size, border = [start], 0, False
        while stack:
            cur = stack.pop()
            c, r = cur % cols, cur // cols
            size += 1
            if c in (0, cols - 1) or r in (0, rows - 1):
                border = True
            for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nc, nr = c + dc, r + dr
                if 0 <= nc < cols and 0 <= nr < rows and comp[nr * cols + nc] == -1 and not blocked(c, r, nc, nr):
                    comp[nr * cols + nc] = n
                    stack.append(nr * cols + nc)
        sizes.append(size)
        touches_border.append(border)

    total = cols * rows
    outside = {n for n, s in enumerate(sizes) if touches_border[n] and s * 2 > total}       # the big open area round the building
    keep = sorted((n for n in range(len(sizes)) if n not in outside and sizes[n] >= MIN_ROOM_SQUARES), key=lambda n: -sizes[n])
    notes = []
    if len(keep) > MAX_SPACES:
        notes.append(f"Only the {MAX_SPACES} largest of {len(keep)} areas were kept.")
        keep = keep[:MAX_SPACES]
    if not keep:
        return None, "No enclosed rooms were found: the walls do not close any area (or the map is one open space)."
    sid = {n: i + 1 for i, n in enumerate(sorted(keep))}
    ids = [sid.get(comp[i], 0) for i in range(total)]

    runs = []
    for v in ids:
        if runs and runs[-2] == v:
            runs[-1] += 1
        else:
            runs += [v, 1]

    def space_at(c, r):
        return ids[r * cols + c] if 0 <= c < cols and 0 <= r < rows else 0

    marks, seen = [], set()
    for x1, y1, x2, y2, kind, _i in segs:
        if kind == "wall":
            continue
        mx, my = ((x1 + x2) / 2 - ox) / cell, ((y1 + y2) / 2 - oy) / cell
        if abs(x2 - x1) >= abs(y2 - y1):                      # runs along x: a horizontal edge at the nearest grid line
            ex, ey, axis = int(math.floor(mx)), int(round(my)), "h"
            sides = (space_at(ex, ey - 1), space_at(ex, ey))
        else:
            ex, ey, axis = int(round(mx)), int(math.floor(my)), "v"
            sides = (space_at(ex - 1, ey), space_at(ex, ey))
        if sides[0] == sides[1] or not (0 <= ex <= cols and 0 <= ey <= rows) or (axis == "h" and ex >= cols) or (axis == "v" and ey >= rows):
            continue
        if (ex, ey, axis) in seen:
            continue
        seen.add((ex, ey, axis))
        marks.append([ex, ey, axis, {"door": "door", "secret": "secret", "window": "window"}[kind]])

    names = {}
    state = {
        "cell": round(cell, 2), "ox": round(ox, 2), "oy": round(oy, 2), "cols": cols, "rows": rows, "cells": runs,
        "spaces": [{"id": sid[n], "name": f"Room {sid[n]}", "kind": "other"} for n in sorted(keep)],
        "marks": marks, "clear": True,
    }
    return state, {"rooms": len(keep), "doors": sum(1 for m in marks if m[3] in ("door", "secret")), "windows": sum(1 for m in marks if m[3] == "window"),
                   "outside_squares": sum(sizes[n] for n in outside), "notes": notes}
