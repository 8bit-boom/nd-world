"""The authoring state of the room-drawing tool: which grid cells are floor, which room each belongs to, and which wall
edges are doors, windows or secret doors.

A leaf module (no router imports). The map's floors and walls are DERIVED from this state in the browser
(static/js/map-rooms.js: "an edge between two different spaces is a wall") and written out as ordinary elements and
wall data, so everything that already understands those - the player view, fog of war, the TV, exports - works on a
hand-drawn map unchanged. This state exists so the drawing can be edited again.

State: {"cell": px, "ox": px, "oy": px, "cols": n, "rows": n,
        "cells": [id, run, id, run, ...]          row-major run lengths, id 0 = empty; the runs add up to cols*rows,
        "spaces": [{"id": n, "name": str, "kind": str}],
        "marks": [[x, y, "h"|"v", "door"|"window"|"secret"|"open"], ...]}
An "h" mark is the edge along the TOP of cell (x, y); a "v" mark the edge along its LEFT.
"""
import math
import re

KINDS = ("hall", "tavern", "bedroom", "kitchen", "storage", "corridor", "cell", "shrine", "library", "armory", "cave", "outdoor", "other")
MARK_KINDS = ("door", "window", "secret", "open")
MAX_CELLS = 40000
MAX_SIDE = 400
MAX_SPACES = 80
MAX_MARKS = 5000
MAX_NAME = 60


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def clean_rooms(data):
    """(state, warnings) or ValueError. An empty/None `data` is the empty state (nothing drawn)."""
    if data in (None, {}, []):
        return {}, []
    if not isinstance(data, dict):
        raise ValueError("rooms must be an object")
    warnings = []
    cell, ox, oy = _num(data.get("cell")), _num(data.get("ox")) or 0.0, _num(data.get("oy")) or 0.0
    cols, rows = _int(data.get("cols")), _int(data.get("rows"))
    if cell is None or not (8 <= cell <= 400):
        raise ValueError("cell must be a size between 8 and 400 px")
    if cols is None or rows is None or not (1 <= cols <= MAX_SIDE and 1 <= rows <= MAX_SIDE) or cols * rows > MAX_CELLS:
        raise ValueError(f"the room grid must be at most {MAX_SIDE} squares a side and {MAX_CELLS} squares in all")
    ox, oy = min(max(ox, 0.0), cell), min(max(oy, 0.0), cell)

    spaces, ids = [], set()
    for sp in (data.get("spaces") if isinstance(data.get("spaces"), list) else [])[:MAX_SPACES]:
        if not isinstance(sp, dict):
            continue
        sid = _int(sp.get("id"))
        if sid is None or not (1 <= sid <= 9999) or sid in ids:
            continue
        name = " ".join(str(sp.get("name") or "").split())[:MAX_NAME] or f"Room {sid}"
        kind = sp.get("kind") if sp.get("kind") in KINDS else "other"
        ids.add(sid)
        spaces.append({"id": sid, "name": name, "kind": kind})

    runs, total = [], 0
    raw = data.get("cells")
    if not isinstance(raw, list) or len(raw) % 2:
        raise ValueError("cells must be a list of [id, run, ...] pairs")
    for i in range(0, len(raw), 2):
        sid, n = _int(raw[i]), _int(raw[i + 1])
        if sid is None or n is None or n < 1 or sid < 0:
            raise ValueError("cells must hold whole numbers")
        total += n
        if total > cols * rows:
            raise ValueError("cells hold more squares than the grid has")
        if sid and sid not in ids:
            sid = 0                                                 # a cell of a room that does not exist is empty
        if runs and runs[-2] == sid:
            runs[-1] += n
        else:
            runs += [sid, n]
    if total != cols * rows:
        raise ValueError("cells hold fewer squares than the grid has")

    marks, seen = [], set()
    for m in (data.get("marks") if isinstance(data.get("marks"), list) else [])[:MAX_MARKS]:
        if not isinstance(m, (list, tuple)) or len(m) != 4:
            continue
        x, y, axis, kind = _int(m[0]), _int(m[1]), m[2], m[3]
        if x is None or y is None or axis not in ("h", "v") or kind not in MARK_KINDS:
            continue
        if not (0 <= x <= cols and 0 <= y <= rows) or (axis == "h" and x >= cols) or (axis == "v" and y >= rows):
            continue
        key = (x, y, axis)
        if key in seen:
            continue
        seen.add(key)
        marks.append([x, y, axis, kind])
    used = set(runs[0::2]) - {0}
    spaces = [s for s in spaces if s["id"] in used]                 # a room with no floor left is gone
    return {"cell": round(cell, 2), "ox": round(ox, 2), "oy": round(oy, 2), "cols": cols, "rows": rows, **({"clear": True} if data.get("clear") is True else {}),
            "cells": runs, "spaces": spaces, "marks": marks}, warnings
