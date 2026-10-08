"""Walls, doors and windows of a map, and the fog-of-war settings: validation and the player-facing view.

A leaf module (no router imports). The geometry is what line of sight and fog are computed from, so it is validated like
any other input that other people's browsers will draw: finite numbers only, bounded to the canvas, capped in size.

A wall is {"id", "pts": [[x, y], ...], "kind": "wall" | "door" | "window" | "secret", "state": "closed" | "open"}:
  wall    blocks sight and movement
  door    blocks both while closed ("state": "open" lets both through)
  window  blocks movement, not sight
  secret  looks and acts like a wall to everyone; the GM knows it is a door. Players are NEVER sent the word:
          for_player() turns it into a plain wall, so it cannot be found in the page source. When the GM opens it (the party
          found it) it becomes an ordinary open door for players, and sight and movement pass.
"""
import math
import re

KINDS = ("wall", "door", "window", "secret")
MAX_WALLS = 5000            # polylines
MAX_POINTS_PER_WALL = 200
MAX_POINTS_TOTAL = 20000
MAX_FOG_RANGE_CELLS = 100
MAX_LIGHTS = 60
MAX_LIGHT_RANGE_SQUARES = 60

_ID = re.compile(r"[^A-Za-z0-9_.:-]")


def _num(v):
    """A finite float or None (booleans, strings, NaN and infinities are not numbers here)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def clean_walls(data, canvas_w=2000, canvas_h=1500):
    """(walls, warnings) from untrusted input. ValueError when `data` is not a list at all."""
    if not isinstance(data, list):
        raise ValueError("walls must be a list")
    cw, ch = max(1.0, float(canvas_w or 2000)), max(1.0, float(canvas_h or 1500))
    out, warnings, total, seen = [], [], 0, set()
    dropped = 0
    for i, w in enumerate(data):
        if len(out) >= MAX_WALLS:
            warnings.append(f"Only the first {MAX_WALLS} walls were kept.")
            break
        if not isinstance(w, dict) or not isinstance(w.get("pts"), list):
            dropped += 1
            continue
        pts = []
        for p in w["pts"][:MAX_POINTS_PER_WALL]:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue
            x, y = _num(p[0]), _num(p[1])
            if x is None or y is None:
                continue
            q = [round(min(max(x, 0.0), cw), 2), round(min(max(y, 0.0), ch), 2)]
            if not pts or pts[-1] != q:
                pts.append(q)
        if len(pts) < 2 or total + len(pts) > MAX_POINTS_TOTAL:
            dropped += 1
            continue
        total += len(pts)
        kind = w.get("kind") if w.get("kind") in KINDS else "wall"
        wid = _ID.sub("", str(w.get("id") or ""))[:40] or f"w{i + 1}"
        while wid in seen:
            wid += "x"
        seen.add(wid)
        item = {"id": wid, "pts": pts, "kind": kind}
        if kind in ("door", "secret"):
            item["state"] = "open" if w.get("state") == "open" else "closed"
        out.append(item)
    if dropped:
        warnings.append(f"{dropped} wall(s) were skipped because they were not usable.")
    return out, warnings


def for_player(walls):
    """What a player's browser may be told: a secret door is an ordinary wall, and nothing else about it survives."""
    out = []
    for w in walls or []:
        if not isinstance(w, dict):
            continue
        if w.get("kind") == "secret":
            # closed: an ordinary wall, nothing to find. Opened by the GM (the party found it): an open door, which lets sight through
            if w.get("state") == "open":
                out.append({"id": w.get("id"), "pts": w.get("pts"), "kind": "door", "state": "open"})
            else:
                out.append({"id": w.get("id"), "pts": w.get("pts"), "kind": "wall"})
        else:
            out.append({k: w[k] for k in ("id", "pts", "kind", "state") if k in w})
    return out


def segments(walls):
    """Polylines -> [{x1, y1, x2, y2, kind, state, id}] for the vision code. A secret door blocks like a wall."""
    segs = []
    for w in walls or []:
        pts = w.get("pts") or []
        kind = w.get("kind", "wall")
        if kind == "secret":                                   # a closed secret door is a wall; once the GM opens it, a door that is open
            kind = "door" if w.get("state") == "open" else "wall"
        for a, b in zip(pts, pts[1:]):
            segs.append({"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1], "kind": kind, "state": w.get("state", "closed"), "id": w.get("id")})
    return segs


def toggle_door(walls, wall_id):
    """The wall list with that door's state flipped. Returns (walls, new_state) or (walls, None) when it is not a door."""
    for w in walls:
        if w.get("id") == wall_id and w.get("kind") in ("door", "secret"):
            w["state"] = "closed" if w.get("state") == "open" else "open"
            return walls, w["state"]
    return walls, None


def clean_fog(data):
    """The fog and lighting settings:
    {"enabled": bool,        fog of war on/off
     "range": squares,       how far characters see (0 = as far as the walls allow)
     "epoch": int,           raised by the GM's reset; browsers that remember what they explored forget it when it changes
     "darkness": 0..0.95,    how dark the map is where no light reaches (0 = no lighting effect at all)
     "personal": squares,    the light every character carries (a lantern); 0 = none
     "strict": bool}         with fog on: the SERVER withholds what the party has not seen (app/map_strict.py)"""
    d = data if isinstance(data, dict) else {}
    rng = _num(d.get("range"))
    ep = d.get("epoch")
    dark = _num(d.get("darkness"))
    pers = _num(d.get("personal"))
    return {
        "enabled": bool(d.get("enabled")) if isinstance(d.get("enabled"), bool) else False,
        "range": 0 if rng is None else int(min(max(rng, 0), MAX_FOG_RANGE_CELLS)),
        "epoch": ep if isinstance(ep, int) and not isinstance(ep, bool) and 0 <= ep < 10 ** 9 else 0,
        "darkness": 0.0 if dark is None else round(min(max(dark, 0.0), 0.95), 2),
        "personal": 2.0 if "personal" not in d else (0.0 if pers is None else round(min(max(pers, 0.0), 20.0), 1)),
        "strict": d.get("strict") is True,
    }


_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_lights(data, canvas_w=2000, canvas_h=1500):
    """(lights, warnings): [{"id", "x", "y", "range" (squares), "color" "#rrggbb", "intensity" 0.1..1, "on": bool, "label"}].
    Finite numbers only, kept on the canvas, at most MAX_LIGHTS. ValueError when `data` is not a list."""
    if not isinstance(data, list):
        raise ValueError("lights must be a list")
    cw, ch = max(1.0, float(canvas_w or 2000)), max(1.0, float(canvas_h or 1500))
    out, warnings, seen, dropped = [], [], set(), 0
    for i, l in enumerate(data):
        if len(out) >= MAX_LIGHTS:
            warnings.append(f"Only the first {MAX_LIGHTS} lights were kept.")
            break
        if not isinstance(l, dict):
            dropped += 1
            continue
        x, y, r = _num(l.get("x")), _num(l.get("y")), _num(l.get("range"))
        if x is None or y is None or r is None or r <= 0:
            dropped += 1
            continue
        k = _num(l.get("intensity"))
        lid = _ID.sub("", str(l.get("id") or ""))[:40] or f"l{i + 1}"
        while lid in seen:
            lid += "x"
        seen.add(lid)
        out.append({
            "id": lid, "x": round(min(max(x, 0.0), cw), 2), "y": round(min(max(y, 0.0), ch), 2),
            "range": round(min(max(r, 0.5), MAX_LIGHT_RANGE_SQUARES), 2),
            "color": l["color"] if isinstance(l.get("color"), str) and _COLOUR.match(l["color"]) else "#ffd9a0",
            "intensity": 1.0 if k is None else round(min(max(k, 0.1), 1.0), 2),
            "on": l.get("on") is not False,
            "label": " ".join(str(l.get("label") or "").split())[:60],
        })
    if dropped:
        warnings.append(f"{dropped} light(s) were skipped because they were not usable.")
    return out, warnings
