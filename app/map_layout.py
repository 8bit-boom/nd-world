"""From a PLAN to a map: the AI says which rooms there are and how they connect; this code decides where everything goes.

Why the split: a language model is good at "a smugglers' den with a hidden back room behind the cellar" and bad at
coordinates - asked for raw positions it overlaps rooms, forgets doors and leaves walls floating. So the model returns a
small spec (rooms, kinds, sizes, links) and this deterministic layout turns it into ordinary schematic elements: a floor
per room, wall segments with gaps where doors are, doors (locked ones red, secret ones hidden from players), and furniture
chosen by room kind - taken from the world's prop library when it has a matching object, drawn as a plain shape otherwise.
The same spec and seed always give the same map. A leaf module (no router imports).

    spec, warnings = clean_map_spec(model_json)
    elements, warnings = layout(spec, cell=50, canvas_w=2000, canvas_h=1500, props=[...])
"""
import hashlib
import json
import math
import random
import re
import uuid

MAX_ROOMS = 40
MAX_LINKS = 120
MAX_NAME = 60
MAX_NOTES = 200

KINDS = ("hall", "tavern", "bedroom", "kitchen", "storage", "corridor", "cell", "shrine", "library", "armory", "cave", "other")
_KIND_ALIASES = {
    "inn": "tavern", "bar": "tavern", "pub": "tavern", "common room": "tavern", "dining": "tavern", "dining hall": "hall",
    "great hall": "hall", "throne room": "hall", "lobby": "hall", "foyer": "hall", "entrance": "hall", "courtyard": "hall",
    "room": "other", "chamber": "other", "office": "other", "study": "library", "archive": "library",
    "bedchamber": "bedroom", "dormitory": "bedroom", "quarters": "bedroom", "barracks": "bedroom", "guest room": "bedroom",
    "pantry": "kitchen", "larder": "storage", "cellar": "storage", "warehouse": "storage", "vault": "storage", "closet": "storage",
    "hallway": "corridor", "passage": "corridor", "tunnel": "corridor", "stairs": "corridor",
    "prison": "cell", "jail": "cell", "dungeon": "cell", "temple": "shrine", "chapel": "shrine", "crypt": "shrine", "altar": "shrine",
    "weapon room": "armory", "smithy": "armory", "forge": "armory", "workshop": "armory",
}
SIZES = {"small": (4, 4), "medium": (6, 5), "large": (9, 7)}
_CORRIDOR = {"small": (5, 2), "medium": (8, 2), "large": (12, 2)}
LINK_TYPES = ("door", "open", "locked", "secret")
_LINK_ALIASES = {"doorway": "open", "arch": "open", "archway": "open", "opening": "open", "gap": "open",
                 "lock": "locked", "locked door": "locked", "barred": "locked", "hidden": "secret", "secret door": "secret",
                 "hidden door": "secret", "normal": "door", "wooden": "door", "closed": "door"}

_FLOOR = {"hall": "#3a3a4a", "tavern": "#4a3a2a", "bedroom": "#3a3a52", "kitchen": "#4a4538", "storage": "#3c352c",
          "corridor": "#2f3138", "cell": "#303236", "shrine": "#40384f", "library": "#35402f", "armory": "#3b3b3b",
          "cave": "#3a342a", "other": "#34363f"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:30] or "room"


def _clean_text(v, cap) -> str:
    return " ".join(str(v or "").split())[:cap]


PLAN_SYSTEM = (
    "You plan buildings, dungeons and other enclosed spaces for a tabletop RPG map. You do NOT draw and you give NO "
    "coordinates: you only decide which rooms exist and how they connect, and software lays them out.\n"
    "Reply with ONE JSON object and nothing else:\n"
    '{"title": "short name", "entry": "<id of the room the party enters from outside>", '
    '"rooms": [{"id": "short-id", "name": "Name shown on the map", "kind": "<kind>", "size": "small|medium|large", '
    '"notes": "one line for the GM", "entity": "optional: the exact name of a lore entity this room is"}], '
    '"links": [{"a": "<room id>", "b": "<room id>", "type": "door|open|locked|secret"}]}\n'
    "kind is one of: " + ", ".join(KINDS) + ". Sizes: small is about 4x4 squares, medium 6x5, large 9x7.\n"
    "Rules: usually 4 to 16 rooms (never more than " + str(MAX_ROOMS) + "); every room appears in at least one link and the whole place is "
    "connected; use kind corridor for passages that join rooms; make the entry a sensible first room (a common room, gate "
    "hall, lobby); use locked or secret links only where the description implies something guarded or hidden; open means "
    "an archway with no door. Match the setting, names and architecture of the world lore if any is given. "
    "No comments, no markdown fences."
)


def extract_json(text: str):
    """The first JSON object in a model reply, tolerating a code fence or chatter around it. ValueError if there is none."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError("no JSON object in the reply")


# ── the spec ─────────────────────────────────────────────────────────────────────────────────────

def clean_map_spec(data):
    """(spec, warnings) from whatever the model returned; ValueError when there is nothing to build.

    Accepts the usual loose shapes (`rooms` under another key, `connections` for `links`, kinds and sizes in the model's own
    words) and normalises them, drops what makes no sense (links to rooms that do not exist, duplicate links, a room linked
    to itself) and joins rooms that would be unreachable to the entrance. Never trusts a number: sizes come only from the
    SIZES table, so a model cannot ask for a 10,000-cell room."""
    warnings = []
    if not isinstance(data, dict):
        raise ValueError("The plan is not a JSON object")
    raw_rooms = data.get("rooms") or data.get("areas") or data.get("spaces")
    if not isinstance(raw_rooms, list):
        lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        raw_rooms = lists[0] if lists else None
    if not isinstance(raw_rooms, list) or not raw_rooms:
        raise ValueError("The plan has no rooms")
    if len(raw_rooms) > MAX_ROOMS:
        warnings.append(f"Only the first {MAX_ROOMS} rooms were used.")

    rooms, ids = [], {}
    for i, r in enumerate(raw_rooms[:MAX_ROOMS]):
        if isinstance(r, str):
            r = {"name": r}
        if not isinstance(r, dict):
            continue
        name = _clean_text(r.get("name") or r.get("title") or r.get("label") or f"Room {i + 1}", MAX_NAME) or f"Room {i + 1}"
        kind_raw = str(r.get("kind") or r.get("type") or r.get("purpose") or "").strip().lower()
        kind = kind_raw if kind_raw in KINDS else _KIND_ALIASES.get(kind_raw)
        if kind is None:                                   # guess from the name's words, e.g. "Back kitchen", "Old cellar"
            low = name.lower()
            words = set(re.findall(r"[a-z]+", low))
            kind = (next((k for k in KINDS if k in words), None)
                    or next((v for a, v in _KIND_ALIASES.items() if re.search(r"\b" + re.escape(a) + r"\b", low)), "other"))
        size_raw = str(r.get("size") or "").strip().lower()
        size = size_raw if size_raw in SIZES else {"tiny": "small", "little": "small", "big": "large", "huge": "large",
                                                   "grand": "large", "vast": "large", "normal": "medium"}.get(size_raw, "medium")
        rid_base = _slug(r.get("id") or name)
        rid, n = rid_base, 2
        while rid in ids:
            rid = f"{rid_base}-{n}"; n += 1
        room = {"id": rid, "name": name, "kind": kind, "size": size, "notes": _clean_text(r.get("notes") or r.get("description"), MAX_NOTES),
                "entity": _clean_text(r.get("entity") or r.get("lore"), MAX_NAME)}
        ids[rid] = room
        # the model may refer to a room by id OR by name
        ids.setdefault(_slug(name), room)
        rooms.append(room)
    if not rooms:
        raise ValueError("The plan has no usable rooms")

    def resolve(ref):
        if isinstance(ref, dict):
            ref = ref.get("id") or ref.get("name")
        room = ids.get(_slug(ref)) if ref is not None else None
        return room["id"] if room else None

    links, seen = [], set()
    raw_links = data.get("links") or data.get("connections") or data.get("doors") or data.get("edges") or []
    if not isinstance(raw_links, list):
        raw_links = []
    for lk in raw_links[:MAX_LINKS]:
        if isinstance(lk, (list, tuple)) and len(lk) >= 2:
            lk = {"a": lk[0], "b": lk[1], "type": lk[2] if len(lk) > 2 else "door"}
        if not isinstance(lk, dict):
            continue
        a = resolve(lk.get("a", lk.get("from", lk.get("source"))))
        b = resolve(lk.get("b", lk.get("to", lk.get("target"))))
        if not a or not b or a == b:
            warnings.append("A connection named a room that is not in the plan and was skipped.") if not (a and b) else None
            continue
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        t = str(lk.get("type") or lk.get("kind") or "door").strip().lower()
        t = t if t in LINK_TYPES else _LINK_ALIASES.get(t, "door")
        links.append({"a": a, "b": b, "type": t})

    entry = resolve(data.get("entry") or data.get("entrance") or data.get("start")) or rooms[0]["id"]

    # join anything unreachable from the entrance so the map is one connected place
    adj = {r["id"]: set() for r in rooms}
    for lk in links:
        adj[lk["a"]].add(lk["b"]); adj[lk["b"]].add(lk["a"])
    reached, stack = {entry}, [entry]
    while stack:
        for nb in adj[stack.pop()]:
            if nb not in reached:
                reached.add(nb); stack.append(nb)
    for r in rooms:
        if r["id"] not in reached:
            warnings.append(f"“{r['name']}” had no way in; it was joined to the entrance with an open doorway.")
            links.append({"a": entry, "b": r["id"], "type": "open"})
            reached.add(r["id"])
            adj[entry].add(r["id"]); adj[r["id"]].add(entry)
            # rooms behind it are now reachable too
            stack = [r["id"]]
            while stack:
                for nb in adj[stack.pop()]:
                    if nb not in reached:
                        reached.add(nb); stack.append(nb)
    title = _clean_text(data.get("title") or data.get("name"), MAX_NAME)
    return {"title": title, "entry": entry, "rooms": rooms, "links": links}, warnings


# ── placement on a grid of cells ─────────────────────────────────────────────────────────────────

def _dims(room, rng, flip=None):
    if room["kind"] == "corridor":
        w, h = _CORRIDOR[room["size"]]
    else:
        w, h = SIZES[room["size"]]
        if room["kind"] == "cave":
            w, h = w + 1, h + 1
    if flip is None:
        flip = rng.random() < 0.5
    return (h, w) if flip else (w, h)


def _overlap(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _candidates(parent, w, h):
    """Every rectangle of size w x h that touches `parent` along at least 2 cells of one of its sides."""
    px, py, pw, ph = parent
    out = []
    for dy in range(-h + 2, ph - 1):
        out.append((px + pw, py + dy, w, h, "E"))
        out.append((px - w, py + dy, w, h, "W"))
    for dx in range(-w + 2, pw - 1):
        out.append((px + dx, py + ph, w, h, "S"))
        out.append((px + dx, py - h, w, h, "N"))
    return out


def _bbox_area(rects):
    x0 = min(r[0] for r in rects); y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects); y1 = max(r[1] + r[3] for r in rects)
    return (x1 - x0) * (y1 - y0)


def _shared(a, b):
    """The unit edge cells where rectangles a and b touch: ('v', x, [y...]) or ('h', y, [x...]) or None."""
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    if ax + aw == bx or bx + bw == ax:
        x = bx if ax + aw == bx else ax
        lo, hi = max(ay, by), min(ay + ah, by + bh)
        if hi - lo >= 1:
            return ("v", x, list(range(lo, hi)))
    if ay + ah == by or by + bh == ay:
        y = by if ay + ah == by else ay
        lo, hi = max(ax, bx), min(ax + aw, bx + bw)
        if hi - lo >= 1:
            return ("h", y, list(range(lo, hi)))
    return None


def place_rooms(spec, rng):
    """({room_id: (x, y, w, h)}, warnings, extra_links) in cells. Rooms are placed breadth-first from the entrance; each new
    room takes the touching position (never overlapping) that keeps the whole plan most compact. A room is tried against
    its parent first, then its other placed neighbours, and only as a last resort against any placed room - in which case
    an open doorway is added so it is still reachable (and the warning says so)."""
    warnings, extra = [], []
    by_id = {r["id"]: r for r in spec["rooms"]}
    adj = {rid: [] for rid in by_id}
    for lk in spec["links"]:
        adj[lk["a"]].append(lk["b"]); adj[lk["b"]].append(lk["a"])
    placed = {}
    first = by_id[spec["entry"]]
    w, h = _dims(first, rng, flip=False)
    placed[first["id"]] = (0, 0, w, h)
    queue = [first["id"]]
    order = {first["id"]: 0}

    def best_spot(room, hosts):
        best = None
        for host in hosts:
            for flip in (False, True):
                cw, ch = _dims(room, rng, flip=flip)
                for cand in _candidates(placed[host], cw, ch):
                    rect = cand[:4]
                    if any(_overlap(rect, other) for other in placed.values()):
                        continue
                    cost = _bbox_area(list(placed.values()) + [rect]) + rng.random() * 0.5
                    if best is None or cost < best[0]:
                        best = (cost, rect)
            if best is not None:
                return best[1]
        return None

    while queue:
        pid = queue.pop(0)
        for cid in sorted(adj[pid], key=lambda c: (order.get(c, 1 << 30), c)):
            if cid in placed:
                continue
            room = by_id[cid]
            neighbours = [n for n in adj[cid] if n in placed]
            hosts = [pid] + [n for n in neighbours if n != pid]
            rect = best_spot(room, hosts)
            if rect is None:
                px, py, pw, ph = placed[pid]
                others = sorted((r for r in placed if r not in hosts),
                                key=lambda r: abs(placed[r][0] - px) + abs(placed[r][1] - py))
                rect = best_spot(room, others)
                if rect is not None:
                    host = next(r for r in others if _shared(rect, placed[r]) and len(_shared(rect, placed[r])[2]) >= 1)
                    extra.append({"a": host, "b": cid, "type": "open"})
                    warnings.append(f"“{room['name']}” would not fit next to “{by_id[pid]['name']}”, so it was put beside "
                                    f"“{by_id[host]['name']}” with an open doorway.")
            if rect is None:
                continue
            placed[cid] = rect
            order[cid] = len(order)
            queue.append(cid)
    for r in spec["rooms"]:
        if r["id"] not in placed:
            warnings.append(f"There was no room on the map for “{r['name']}”.")
    return placed, warnings, extra


# ── walls and doors ──────────────────────────────────────────────────────────────────────────────

def _edges_of(rect):
    x, y, w, h = rect
    out = []
    for i in range(w):
        out.append(("h", y, x + i)); out.append(("h", y + h, x + i))
    for j in range(h):
        out.append(("v", x, y + j)); out.append(("v", x + w, y + j))
    return out


def _merge(edges):
    """Unit edges -> long segments [(kind, fixed, start, end)] so a wall is one element, not dozens."""
    segs = []
    for kind in ("h", "v"):
        lines = {}
        for k, fixed, pos in edges:
            if k == kind:
                lines.setdefault(fixed, []).append(pos)
        for fixed, ps in sorted(lines.items()):
            ps.sort()
            start = prev = ps[0]
            for p in ps[1:]:
                if p != prev + 1:
                    segs.append((kind, fixed, start, prev + 1)); start = p
                prev = p
            segs.append((kind, fixed, start, prev + 1))
    return segs


def door_for(link, a, b):
    """The unit edge where the link's door goes, or None when the rooms do not touch."""
    sh = _shared(a, b)
    if not sh:
        return None
    kind, fixed, cells = sh
    pos = cells[(len(cells) - 1) // 2]
    return (kind, fixed, pos)


# ── furniture ────────────────────────────────────────────────────────────────────────────────────

# name -> (footprint w x h in cells with the long side first, shape, colours)
ITEMS = {
    "bed": ((1, 2), "bed"), "cot": ((1, 2), "bed"), "table": ((2, 1), "rect"), "round table": ((1, 1), "circle"),
    "bar counter": ((3, 1), "rect"), "counter": ((2, 1), "rect"), "stove": ((1, 1), "rect"), "barrel": ((1, 1), "circle"),
    "crate": ((1, 1), "rect"), "chest": ((1, 1), "rect"), "shelf": ((2, 1), "rect"), "bookshelf": ((2, 1), "rect"),
    "altar": ((2, 1), "rect"), "statue": ((1, 1), "circle"), "bench": ((2, 1), "rect"), "stool": ((1, 1), "circle"),
    "weapon rack": ((2, 1), "rect"), "bucket": ((1, 1), "circle"), "desk": ((2, 1), "rect"), "pillar": ((1, 1), "circle"),
}
_COLOURS = {"bed": "#6b4b3a", "cot": "#5a4a3a", "table": "#7a5a3a", "round table": "#7a5a3a", "bar counter": "#6a4a2a",
            "counter": "#6a5a4a", "stove": "#4a4a4f", "barrel": "#8a5a2b", "crate": "#8a6a3a", "chest": "#9a7a2a",
            "shelf": "#4a3a2a", "bookshelf": "#4a3a2a", "altar": "#8c8c9c", "statue": "#9a9aa4", "bench": "#6a5030",
            "stool": "#7a6040", "weapon rack": "#555", "bucket": "#6a6a74", "desk": "#6a4a2a", "pillar": "#8a8a92"}
# synonyms used to find a matching object in the world's prop library
_SYNONYMS = {"bed": ("bed", "cot", "bunk"), "cot": ("cot", "bed", "bunk"), "table": ("table",), "round table": ("table",),
             "bar counter": ("bar", "counter"), "counter": ("counter", "bar"), "stove": ("stove", "oven", "hearth", "fireplace"),
             "barrel": ("barrel", "cask", "keg"), "crate": ("crate", "box"), "chest": ("chest", "trunk"),
             "shelf": ("shelf", "bookcase", "bookshelf"), "bookshelf": ("bookshelf", "bookcase", "shelf"),
             "altar": ("altar",), "statue": ("statue",), "bench": ("bench", "pew"), "stool": ("stool", "chair"),
             "weapon rack": ("rack", "weapon"), "bucket": ("bucket", "pail"), "desk": ("desk", "table"), "pillar": ("pillar", "column")}

# kind -> [(item, how many (min, max), where: wall | centre | corner | any)]
FURNISHING = {
    "tavern": [("bar counter", (1, 1), "wall"), ("table", (2, 4), "centre"), ("stool", (2, 5), "any"), ("barrel", (2, 3), "corner")],
    "hall": [("table", (1, 3), "centre"), ("bench", (2, 4), "wall"), ("pillar", (0, 2), "centre")],
    "bedroom": [("bed", (1, 3), "wall"), ("chest", (1, 1), "corner"), ("table", (0, 1), "wall")],
    "kitchen": [("counter", (2, 3), "wall"), ("stove", (1, 1), "wall"), ("table", (1, 1), "centre"), ("barrel", (1, 2), "corner")],
    "storage": [("crate", (3, 6), "any"), ("barrel", (2, 4), "corner"), ("shelf", (1, 3), "wall")],
    "cell": [("cot", (1, 2), "wall"), ("bucket", (1, 1), "corner")],
    "shrine": [("altar", (1, 1), "centre"), ("statue", (1, 2), "corner"), ("bench", (0, 3), "wall")],
    "library": [("bookshelf", (3, 6), "wall"), ("table", (1, 2), "centre"), ("stool", (1, 3), "any")],
    "armory": [("weapon rack", (2, 4), "wall"), ("crate", (1, 3), "any"), ("table", (0, 1), "centre")],
    "cave": [("barrel", (0, 1), "corner"), ("crate", (0, 2), "any")],
    "other": [("table", (0, 1), "centre"), ("chest", (0, 1), "corner")],
    "corridor": [],
}


def _find_prop(item, props):
    words = _SYNONYMS.get(item, (item,))
    for p in props or []:
        hay = set(re.findall(r"[a-z]+", (str(p.get("name", "")) + " " + str(p.get("tags", ""))).lower()))
        if any(w in hay for w in words):
            return p
    return None


def _furnish_room(room, rect, doors_in_room, rng, props, cell, ox, oy):
    """Elements for the furniture of one room. `doors_in_room` is the set of unit edges (kind, fixed, pos) that hold a door
    on this room's boundary; the cells just inside them stay free so a door can always be walked through."""
    x, y, w, h = rect
    plan = FURNISHING.get(room["kind"], [])
    if not plan or w < 3 or h < 3:
        return []
    free = {(cx, cy) for cx in range(x, x + w) for cy in range(y, y + h)}
    inside = lambda c: x <= c[0] < x + w and y <= c[1] < y + h
    for kind, fixed, pos in doors_in_room:                       # clearance: the cell inside the door + the one beyond it
        for side, step in ((-1, -1), (0, 1)):                    # the two cells either side of the door edge; keep the room's own
            c = (fixed + side, pos) if kind == "v" else (pos, fixed + side)
            if inside(c):
                free.discard(c)
                free.discard((c[0] + step, c[1]) if kind == "v" else (c[0], c[1] + step))
    out = []
    corner_cells = [(x, y), (x + w - 1, y), (x, y + h - 1), (x + w - 1, y + h - 1)]
    for item, (lo, hi), where in plan:
        (fw, fh), shape = ITEMS[item]
        for _ in range(rng.randint(lo, hi)):
            spots = []
            for horizontal in ((True, False) if fw != fh else (True,)):
                iw, ih = (fw, fh) if horizontal else (fh, fw)
                for cx in range(x, x + w - iw + 1):
                    for cy in range(y, y + h - ih + 1):
                        cells = {(cx + i, cy + j) for i in range(iw) for j in range(ih)}
                        if not cells <= free:
                            continue
                        on_wall = cx == x or cy == y or cx + iw == x + w or cy + ih == y + h
                        inner = cx > x and cy > y and cx + iw < x + w and cy + ih < y + h
                        if where == "wall" and not on_wall:
                            continue
                        # a long item hugs the wall it lies along
                        if where == "wall" and iw >= ih and not (cy == y or cy + ih == y + h):
                            continue
                        if where == "wall" and ih > iw and not (cx == x or cx + iw == x + w):
                            continue
                        if where == "corner" and not any(c in cells for c in corner_cells):
                            continue
                        if where == "centre" and not inner:
                            continue
                        spots.append((cx, cy, iw, ih, cells))
            if not spots:
                continue
            cx, cy, iw, ih, cells = spots[rng.randrange(len(spots))]
            free -= cells
            if where == "centre":                                 # keep a walkway around centre pieces
                for (a, b) in list(cells):
                    for da in (-1, 0, 1):
                        for db in (-1, 0, 1):
                            free.discard((a + da, b + db))
            out.extend(_item_elements(item, shape, cx, cy, iw, ih, props, cell, ox, oy))
    return out


def _item_elements(item, shape, cx, cy, iw, ih, props, cell, ox, oy):
    px, py, pw, ph = ox + cx * cell, oy + cy * cell, iw * cell, ih * cell
    inset = max(2, cell * 0.08)
    prop = _find_prop(item, props)
    if prop:
        horizontal_item = iw >= ih
        native_wide = (prop.get("cells_w") or 1) >= (prop.get("cells_h") or 1)
        rot = 0 if (horizontal_item == native_wide or iw == ih) else 90
        w, h = (pw - 2 * inset, ph - 2 * inset)
        if rot:                                                    # the picture is drawn upright, then turned a quarter
            w, h = h, w
        ccx, ccy = px + pw / 2, py + ph / 2
        return [{"id": "ai-" + uuid.uuid4().hex[:10], "type": "image", "x": ccx - w / 2, "y": ccy - h / 2, "w": w, "h": h,
                 "rot": rot, "href": prop["url"], "prop_id": prop.get("id"), "opacity": 1, "fill": "none", "stroke": "none",
                 "strokeW": 0, "label": "", "dash": "", "layer": "Tracks"}]
    colour = _COLOURS.get(item, "#777777")
    base = {"fill": colour, "stroke": "#1d1d22", "strokeW": 1, "layer": "Tracks"}
    if shape == "circle":
        r = min(pw, ph) / 2 - inset
        return [{"id": "ai-" + uuid.uuid4().hex[:10], "type": "circle", "cx": px + pw / 2, "cy": py + ph / 2, "rx": r, "ry": r, **base}]
    els = [{"id": "ai-" + uuid.uuid4().hex[:10], "type": "rect", "x": px + inset, "y": py + inset,
            "w": pw - 2 * inset, "h": ph - 2 * inset, **base}]
    if shape == "bed":                                             # a pillow at the head end
        if ph >= pw:
            els.append({"id": "ai-" + uuid.uuid4().hex[:10], "type": "rect", "x": px + pw * 0.2, "y": py + inset + 2,
                        "w": pw * 0.6, "h": ph * 0.22, "fill": "#d8d8e2", "stroke": "#1d1d22", "strokeW": 1, "layer": "Tracks"})
        else:
            els.append({"id": "ai-" + uuid.uuid4().hex[:10], "type": "rect", "x": px + inset + 2, "y": py + ph * 0.2,
                        "w": pw * 0.22, "h": ph * 0.6, "fill": "#d8d8e2", "stroke": "#1d1d22", "strokeW": 1, "layer": "Tracks"})
    return els


# ── the whole map ────────────────────────────────────────────────────────────────────────────────

def _seed_of(spec, seed):
    if seed is not None:
        return int(seed) & 0xFFFFFFFF
    blob = repr([(r["id"], r["kind"], r["size"]) for r in spec["rooms"]]) + repr([(l["a"], l["b"], l["type"]) for l in spec["links"]])
    return int(hashlib.sha1(blob.encode()).hexdigest()[:8], 16)


def layout(spec, cell=50, canvas_w=2000, canvas_h=1500, props=None, seed=None, origin=(0, 0)):
    """(elements, warnings): the spec laid out as schematic elements on a canvas of canvas_w x canvas_h with squares of `cell`
    pixels. `origin` is the grid's offset, so everything lands on the map's own grid lines."""
    rng = random.Random(_seed_of(spec, seed))
    warnings = []
    cell = max(8, int(cell or 50))
    placed, w1, extra_links = place_rooms(spec, rng)
    warnings += w1
    links = list(spec["links"]) + extra_links
    if not placed:
        return [], warnings
    x0 = min(r[0] for r in placed.values()); y0 = min(r[1] for r in placed.values())
    bw = max(r[0] + r[2] for r in placed.values()) - x0
    bh = max(r[1] + r[3] for r in placed.values()) - y0
    if (bw + 2) * cell > canvas_w or (bh + 2) * cell > canvas_h:
        fit = int(min(canvas_w / (bw + 2), canvas_h / (bh + 2)))
        if fit < 8:
            warnings.append("The plan is too big for this canvas even at the smallest size; make the canvas larger.")
            fit = 8
        else:
            warnings.append(f"The plan is larger than the canvas, so squares were drawn {fit}px instead of {cell}px.")
        cell = fit
    ox = (int(((canvas_w - bw * cell) / 2) // cell) * cell) - x0 * cell + int(origin[0] % cell)
    oy = (int(((canvas_h - bh * cell) / 2) // cell) * cell) - y0 * cell + int(origin[1] % cell)

    by_id = {r["id"]: r for r in spec["rooms"]}
    els = []
    # floors
    for rid, (x, y, w, h) in placed.items():
        room = by_id[rid]
        el = {"id": "ai-" + uuid.uuid4().hex[:10], "type": "rect", "x": ox + x * cell, "y": oy + y * cell, "w": w * cell, "h": h * cell,
              "fill": _FLOOR.get(room["kind"], _FLOOR["other"]), "stroke": "none", "strokeW": 0, "label": room["name"], "layer": "Background"}
        if room.get("entity"):
            el["entity_name"] = room["entity"]
        els.append(el)

    # doors: one per link between rooms that ended up touching
    edge_owner = {}
    for rid, rect in placed.items():
        for e in _edges_of(rect):
            edge_owner.setdefault(e, []).append(rid)
    door_cells, doors_of_room = {}, {rid: set() for rid in placed}
    for lk in links:
        if lk["a"] not in placed or lk["b"] not in placed:
            continue
        d = door_for(lk, placed[lk["a"]], placed[lk["b"]])
        if d is None:
            warnings.append(f"“{by_id[lk['a']]['name']}” and “{by_id[lk['b']]['name']}” do not touch, so there is no door between them.")
            continue
        door_cells[d] = lk["type"]
        doors_of_room[lk["a"]].add(d); doors_of_room[lk["b"]].add(d)

    # an outside door on the entrance room: the middle of its longest free wall
    entry_rect = placed[spec["entry"]]
    free_edges = [e for e in _edges_of(entry_rect) if len(edge_owner[e]) == 1 and e not in door_cells]
    if free_edges:
        segs = sorted(_merge(free_edges), key=lambda s: -(s[3] - s[2]))
        kind, fixed, a, b = segs[0]
        pos = a + (b - a - 1) // 2
        door_cells[(kind, fixed, pos)] = "door"
        doors_of_room[spec["entry"]].add((kind, fixed, pos))

    wall_edges = [e for e in edge_owner if e not in door_cells or door_cells[e] == "secret"]
    stroke_w = max(3, round(cell * 0.1))
    for kind, fixed, a, b in _merge(wall_edges):
        if kind == "h":
            x1, y1, x2, y2 = ox + a * cell, oy + fixed * cell, ox + b * cell, oy + fixed * cell
        else:
            x1, y1, x2, y2 = ox + fixed * cell, oy + a * cell, ox + fixed * cell, oy + b * cell
        els.append({"id": "ai-" + uuid.uuid4().hex[:10], "type": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "stroke": "#cfd6e6", "strokeW": stroke_w, "layer": "Tracks"})
    colours = {"door": "#c98a3d", "locked": "#d94b4b", "secret": "#c98a3d"}
    for (kind, fixed, pos), t in sorted(door_cells.items(), key=lambda kv: kv[0]):
        if t == "open":
            continue
        pad = cell * 0.12
        if kind == "h":
            x1, y1, x2, y2 = ox + pos * cell + pad, oy + fixed * cell, ox + (pos + 1) * cell - pad, oy + fixed * cell
        else:
            x1, y1, x2, y2 = ox + fixed * cell, oy + pos * cell + pad, ox + fixed * cell, oy + (pos + 1) * cell - pad
        d = {"id": "ai-" + uuid.uuid4().hex[:10], "type": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
             "stroke": colours[t], "strokeW": stroke_w + 2, "layer": "Tracks"}
        if t == "secret":
            d["hidden"] = True                                       # the GM sees it; players see an unbroken wall
            d["dash"] = "4 3"
        els.append(d)

    # furniture
    for rid, rect in placed.items():
        els.extend(_furnish_room(by_id[rid], rect, doors_of_room[rid], rng, props, cell, ox, oy))
    return els, warnings
