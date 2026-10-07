"""Reading a Universal VTT file (.dd2vtt / .uvtt / .df2vtt - Dungeondraft, Dungeon Alchemist, Arkenforge and others write
them) into what a schematic needs: a background picture, a square grid, walls and doors.

A leaf module (no router imports). The file is untrusted input that other people's browsers will draw, so everything is
checked: finite numbers only, the picture is decoded and re-encoded by Pillow (never stored as uploaded), sizes and counts
are capped, and unknown parts are ignored with a warning rather than guessed at. What the format says is documented in
docs/map-creator-research/FORMATS.md (read off a real Dungeondraft 1.0.1.3 export):

  resolution.map_origin / map_size   squares; every coordinate in the file is ABSOLUTE squares, so a point's pixel is
                                     (x - map_origin.x) * pixels_per_grid
  line_of_sight                      polylines of {x, y} (walls); a closed ring repeats its first point
  objects_line_of_sight              outlines of objects that block light (pillars, furniture) - walls here, simplified
  portals                            doors (windows are portals too in this format): position, bounds [a, b], closed
  lights                             position, range (squares), colour "aarrggbb", intensity -> map lights
  environment.ambient_light          how bright the map is where no light reaches -> the map's darkness
"""
import base64
import binascii
import io
import json
import math

MAX_FILE_BYTES = 80 * 1024 * 1024
MAX_IMAGE_BYTES = 60 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_SIDE = 20000                  # the schematic canvas limit
MIN_SIDE = 100
MAX_POLYLINES = 5000
MAX_POINTS = 20000


class UvttError(ValueError):
    """The file cannot be used; the message is safe to show the GM."""


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _pt(p):
    if not isinstance(p, dict):
        return None
    x, y = _num(p.get("x")), _num(p.get("y"))
    return None if x is None or y is None else (x, y)


def _simplify(points, tol):
    """Douglas-Peucker; the end points are kept."""
    if len(points) <= 2 or tol <= 0:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        ax, ay = points[lo]; bx, by = points[hi]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        worst, at = -1.0, -1
        for i in range(lo + 1, hi):
            px, py = points[i]
            d = math.hypot(px - ax, py - ay) if not l2 else abs((px - ax) * dy - (py - ay) * dx) / math.sqrt(l2)
            if d > worst:
                worst, at = d, i
        if worst > tol:
            keep[at] = True
            stack.append((lo, at)); stack.append((at, hi))
    return [p for p, k in zip(points, keep) if k]


def _load_image(b64, want_w_squares, want_h_squares):
    """(webp_bytes, width_px, height_px) from the embedded picture, decoded and re-encoded."""
    from PIL import Image, UnidentifiedImageError
    if not isinstance(b64, str) or not b64.strip():
        raise UvttError("The file has no embedded picture, so there is no map to draw. Export it again with the image included.")
    try:
        raw = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        raise UvttError("The embedded picture is not valid base64")
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise UvttError("The embedded picture is missing or too large")
    try:
        im = Image.open(io.BytesIO(raw))
        w, h = im.size
        if w * h > MAX_PIXELS:
            raise UvttError("The map picture has too many pixels")
        im.load()
    except UvttError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise UvttError("The embedded picture is not an image this app can read")
    return im, w, h


def parse_uvtt(raw: bytes) -> dict:
    """Everything needed to create the schematic, or UvttError.

    Returns {"image": PIL image, "width": px, "height": px, "cell": px per square, "walls": [...walls in map_walls format...],
             "warnings": [...], "stats": {...}}. Coordinates are pixels of the returned image."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise UvttError("The file is empty")
    if len(raw) > MAX_FILE_BYTES:
        raise UvttError("That file is too large")
    try:
        d = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise UvttError("That is not a Universal VTT file (it is not JSON)")
    if not isinstance(d, dict) or not isinstance(d.get("resolution"), dict):
        raise UvttError("That is not a Universal VTT file (no resolution)")
    warnings = []
    fmt = _num(d.get("format"))
    if fmt is not None and fmt > 0.3:
        warnings.append(f"The file is format {fmt}, newer than 0.3; parts this app does not know are ignored.")
    res = d["resolution"]
    size = _pt(res.get("map_size"))
    ppg = _num(res.get("pixels_per_grid"))
    origin = _pt(res.get("map_origin")) or (0.0, 0.0)
    if not size or size[0] <= 0 or size[1] <= 0 or size[0] > 1000 or size[1] > 1000:
        raise UvttError("resolution.map_size is missing or not sensible")
    if not ppg or not (4 <= ppg <= 2000):
        raise UvttError("resolution.pixels_per_grid is missing or not sensible")

    im, iw, ih = _load_image(d.get("image"), size[0], size[1])
    # the picture is the truth about pixels: squares are mapped onto it, whatever the header claims
    sx, sy = iw / size[0], ih / size[1]
    if abs(sx - ppg) > 1 or abs(sy - ppg) > 1:
        warnings.append(f"The picture is {iw}x{ih}px but the header says {size[0]}x{size[1]} squares at {ppg}px; the picture was used as is.")
    # the schematic canvas holds at most MAX_SIDE px; a bigger picture is scaled down together with the walls
    k = min(1.0, MAX_SIDE / max(iw, ih))
    if max(iw * k, ih * k) < MIN_SIDE:
        raise UvttError("The map picture is too small")
    if k < 1.0:
        warnings.append(f"The picture was larger than {MAX_SIDE}px, so it was scaled down to {round(iw * k)}x{round(ih * k)}.")
        from PIL import Image
        im = im.resize((max(1, round(iw * k)), max(1, round(ih * k))), Image.LANCZOS)
        iw, ih = im.size
        sx, sy = iw / size[0], ih / size[1]
    cell = (sx + sy) / 2

    def px(p):
        return [round((p[0] - origin[0]) * sx, 2), round((p[1] - origin[1]) * sy, 2)]

    walls, total, dropped = [], 0, 0

    def add(points, kind, state=None):
        nonlocal total, dropped
        if len(walls) >= MAX_POLYLINES or total + len(points) > MAX_POINTS:
            dropped += 1
            return
        if len(points) < 2:
            return
        w = {"id": f"u{len(walls) + 1}", "pts": points, "kind": kind}
        if state:
            w["state"] = state
        walls.append(w)
        total += len(points)

    def polylines(key, tol_squares, label):
        lst = d.get(key)
        if lst is None:
            return 0
        if not isinstance(lst, list):
            warnings.append(f"{label} was not a list and was skipped.")
            return 0
        n = 0
        for line in lst:
            pts = [_pt(p) for p in line] if isinstance(line, list) else []
            if len(pts) < 2 or any(p is None for p in pts):
                continue
            pts = _simplify(pts, tol_squares) if tol_squares else pts
            add([px(p) for p in pts], "wall")
            n += 1
        return n

    n_walls = polylines("line_of_sight", 0.0, "line_of_sight")
    n_objects = polylines("objects_line_of_sight", 0.05, "objects_line_of_sight")

    n_doors = 0
    for p in (d.get("portals") or []) if isinstance(d.get("portals"), list) else []:
        if not isinstance(p, dict) or not isinstance(p.get("bounds"), list) or len(p["bounds"]) != 2:
            continue
        a, b = _pt(p["bounds"][0]), _pt(p["bounds"][1])
        if not a or not b or a == b:
            continue
        add([px(a), px(b)], "door", "closed" if p.get("closed", True) is not False else "open")
        n_doors += 1
    if n_doors:
        warnings.append("Windows are doors in this format, so any window in the file is now a door you can open.")
    lights = []
    raw_lights = d.get("lights") if isinstance(d.get("lights"), list) else []
    for l in raw_lights[:60]:
        if not isinstance(l, dict):
            continue
        pos, rng, k = _pt(l.get("position")), _num(l.get("range")), _num(l.get("intensity"))
        if not pos or not rng or rng <= 0:
            continue
        c = l.get("color")
        colour = "#" + c[2:].lower() if isinstance(c, str) and len(c) == 8 and all(ch in "0123456789abcdefABCDEF" for ch in c) else "#ffd9a0"
        x, y = px(pos)
        lights.append({"id": f"u{len(lights) + 1}", "x": x, "y": y, "range": min(rng, 60.0), "color": colour,
                       "intensity": 1.0 if k is None else min(1.0, max(0.1, k)), "on": True})
    if len(raw_lights) > 60:
        warnings.append("Only the first 60 lights were imported.")
    # the map's darkness: how dim "no light" is. A file with baked lighting already has it in the picture, so none is added.
    darkness = 0.0
    env = d.get("environment") if isinstance(d.get("environment"), dict) else {}
    amb = env.get("ambient_light")
    if isinstance(amb, str) and len(amb) == 8 and all(ch in "0123456789abcdefABCDEF" for ch in amb) and not env.get("baked_lighting"):
        r_, g_, b_ = (int(amb[i:i + 2], 16) for i in (2, 4, 6))
        darkness = round(min(0.95, max(0.0, 1 - (r_ + g_ + b_) / 765)), 2)
    if lights and env.get("baked_lighting"):
        warnings.append("The picture already has its lighting painted in; the file's lights were imported but add on top of it.")
    elif lights and darkness == 0:
        warnings.append(f"The file has {len(lights)} light(s); they show once you raise Darkness in Walls & fog.")
    if dropped:
        warnings.append(f"{dropped} wall line(s) were skipped because the limits ({MAX_POLYLINES} lines / {MAX_POINTS} points) were reached.")
    if not walls:
        warnings.append("The file has no walls, so fog of war has nothing to work with.")

    return {"image": im, "width": iw, "height": ih, "cell": round(cell, 2), "walls": walls, "warnings": warnings,
            "lights": lights, "darkness": darkness,
            "stats": {"walls": n_walls, "object_outlines": n_objects, "doors": n_doors, "lights": len(lights),
                      "squares": [size[0], size[1]]}}


def encode_image(im) -> bytes:
    """The map picture as WebP (the original bytes are never stored)."""
    buf = io.BytesIO()
    im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "WEBP", quality=92, method=4)
    return buf.getvalue()
