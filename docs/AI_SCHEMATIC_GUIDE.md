# Creating schematics programmatically (for AI assistants)

This is a reference for an AI (Claude Code or otherwise) that needs to create
or edit an nd-world **schematic** — the SVG floor-plan/blueprint editor at
`/maps/schematic/{slug}` — without a human clicking through the UI. It
documents the actual data format the editor reads and writes
(`app/templates/schematic.html`), verified directly against that code, not
guessed from the UI.

If you just want to *use* the editor as a human would, you don't need this —
open a schematic and use the toolbar. This doc is for scripting it.

---

## What a schematic is

A `Schematic` row (`app/models.py`) belongs to one world and holds:

- `name`, `slug` (URL-safe, auto-generated from name, unique)
- `canvas_width`, `canvas_height` — pixel dimensions of the drawing surface
  (defaults 2000×1500)
- `canvas_bg` — one of `dark` | `blueprint` | `grid-light` | `light`
  (a solid background fill color, not an image)
- `elements_json` — a JSON array of drawing elements; **this is the entire
  content of the schematic** and the only thing you need to construct

Two fields exist on the model but are **not used by the current editor** —
don't bother setting them: `image_url` (set by an upload endpoint but never
rendered) and `markers_json` (unused). If you want an image in a schematic,
use an `image`-type element inside `elements_json` (see below).

Everything here is **GM-only** — there is no player-facing view of schematics
yet, and every route below requires an authenticated GM session cookie.

---

## Coordinate system

Plain pixel space, origin top-left, no transform baked in:
- `(0, 0)` is the top-left corner of the canvas
- x increases rightward, y increases downward
- Valid range is roughly `0..canvas_width` and `0..canvas_height`, but nothing
  enforces this server-side — going slightly outside just means part of the
  shape is off-screen until the GM scrolls/zooms

There's no grid unit conversion — "20px" is a reasonable minor-grid unit if
you want to eyeball alignment (the editor's own snap-to-grid uses 20px), but
you're free to use any coordinates.

---

## Workflow

1. **Log in as GM** and keep the session cookie — every route below requires
   it (`POST /login` with `email`/`password` form fields).
2. **Create the schematic** — `POST /maps/schematic/new` (form-encoded) —
   this makes an empty schematic (`elements_json: "[]"`) and you get its slug
   back via the redirect.
3. **Build your `elements` array** in memory following the schema below.
4. **Save it** — `POST /maps/schematic/{slug}/elements` with JSON body
   `{"elements": [...]}`.
5. To **edit an existing** schematic, see "Editing an existing schematic"
   below — the save endpoint replaces the whole array, so you need the
   current contents first if you want to add rather than overwrite.

### Example: create + populate in two calls

```bash
COOKIE=cookies.txt
curl -s -c $COOKIE -X POST https://your-nd-world/login \
  -d "email=gm@example.com&password=yourpassword"

# 1. Create the shell
curl -s -b $COOKIE -X POST https://your-nd-world/maps/schematic/new \
  -d "name=Old Warehouse&description=Docks warehouse, ground floor&canvas_width=1200&canvas_height=900&canvas_bg=blueprint"
# → redirects to /maps/schematic/old-warehouse

# 2. Push elements (see the worked example further down for real content)
curl -s -b $COOKIE -X POST https://your-nd-world/maps/schematic/old-warehouse/elements \
  -H "Content-Type: application/json" \
  -d '{"elements": [ ... ]}'
```

### Alternative: the general importer (`/import`)

nd-world also has a general-purpose JSON importer (`/import` in the UI, but
fully scriptable too) that can create the schematic *and* populate it in one
call, instead of the two-step flow above:

```bash
# Optional: confirm how a JSON blob gets classified before importing it
curl -s -b $COOKIE -X POST https://your-nd-world/api/import/detect \
  -H "Content-Type: application/json" \
  -d '{"json_text": "[{\"id\":\"r1\",\"type\":\"rect\",\"x\":0,\"y\":0,\"w\":100,\"h\":50}]"}'
# → {"kind":"schematic_elements","summary":"1 schematic element(s)","count":1,"needs":["schematic_slug"]}

# Create a brand-new schematic and populate it in one call:
curl -s -b $COOKIE -X POST https://your-nd-world/api/import/execute \
  -H "Content-Type: application/json" \
  -d '{
    "json_text": "[{\"id\":\"r1\",\"type\":\"rect\",\"x\":0,\"y\":0,\"w\":100,\"h\":50}]",
    "kind": "schematic_elements",
    "params": {"schematic_slug":"__new__","new_schematic_name":"Old Warehouse","new_canvas_width":"1200","new_canvas_height":"900"}
  }'
# → {"ok":true,"redirect":"/maps/schematic/old-warehouse"}

# Or target an EXISTING schematic instead of creating one:
#   "params": {"schematic_slug":"old-warehouse"}
```

This is a genuine improvement over the raw `/elements` endpoint for one
important reason: it **merges elements by `id`** into whatever's already on
the schematic (a matching `id` gets replaced in place, a new `id` gets
appended) instead of wholesale-replacing the array. That means it's safe to
re-run the same import — unlike the raw endpoint, which requires you to
fetch-and-merge yourself first (see "Editing an existing schematic" below)
or you'll wipe out everything already there.

Stick with the raw two-call API when you need `canvas_bg`/`canvas_width`
control beyond the importer's basic new-schematic params, or when your data
isn't a plain elements array (the importer only recognizes that one shape
for this kind — see `docs/AI_ENTITY_GUIDE.md` for what else it recognizes).

---

## API reference

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/login` | form: `email`, `password` | get a session cookie first |
| POST | `/maps/schematic/new` | form: `name` (required), `description`, `canvas_width` (default 2000), `canvas_height` (default 1500), `canvas_bg` (default `dark`) | creates, redirects (303) to `/maps/schematic/{slug}` |
| GET | `/maps/schematic/{slug}` | — | full HTML editor page; `elements_json` is embedded in a `<script>` tag as `let elements = JSON.parse(...)` — see "reading existing elements" below |
| POST | `/maps/schematic/{slug}/elements` | JSON: `{"elements": [...]}` | **replaces the entire array** — not a merge. Returns `{"ok": true}` |
| POST | `/maps/schematic/{slug}/upload` | multipart `file` | sets a background reference image — currently unused by the renderer, skip this |
| POST | `/maps/schematic/{slug}/delete` | — | deletes the schematic, redirects to `/maps` |

Slugs are generated from `name`: lowercased, non-alphanumeric runs collapsed
to single hyphens, trimmed. Collisions get `-2`, `-3`, etc. appended.

### Reading existing elements

There's no clean JSON GET for a schematic's contents — `GET
/maps/schematic/{slug}` returns the full editor HTML page. To read the
current elements before appending to them, fetch that page and find this
line:

```js
let elements = JSON.parse("[{\"id\": \"wall-n\", \"type\": \"rect\", ...}]");
```

**The embedded value is double-encoded** — Jinja renders `elements_json`
(already a JSON string server-side) through the `tojson` filter, which wraps
it as a quoted JS string literal with escaped inner quotes. A browser handles
this transparently (the JS engine parses the string literal, then
`JSON.parse` parses that string into the array), but if you're scraping the
raw HTML text you must decode it **twice**: once to unescape the outer
string literal back into a plain string, and again to parse that string into
the actual elements array. In Python: `json.loads(json.loads(matched_text))`.
Getting only one decode pass gives you back a string, not a list.

(`const WORLD_PARTIES` / `let partyPins` appear nearby too — those are
unrelated live party-tracking data, not schematic content; don't touch them.)

### Editing an existing schematic

Because the save endpoint replaces the whole array:
1. GET the schematic page, extract and parse the current `elements` array.
2. Append/modify/remove elements in that array (never reuse an existing
   `id` unless you intend to replace that specific element).
3. POST the full modified array back to `/elements`.

Skipping step 1 and posting only your new elements will **delete everything
already on the schematic**.

---

## Element schema

Every element is a flat JSON object with a `type` discriminator. All of them
support these common fields (though not all are visually meaningful for
every type — extras are harmless):

| Field | Type | Meaning |
|---|---|---|
| `id` | string | **Must be unique** within the schematic. Any short unique string works (e.g. `"wall-1"`, or a random token) |
| `type` | string | One of the types below |
| `layer` | string | Groups elements for show/hide (see "Layers" below). Any string is valid |
| `label` | string | Optional text rendered on/near the shape (not used by `text`, `image`, line-like types) |
| `fill` | string | CSS color (hex or `rgba(...)`), or `"none"` |
| `stroke` | string | CSS color for the outline/line |
| `strokeW` | number | Stroke width in px |
| `opacity` | number | 0–1 |
| `dash` | string | SVG `stroke-dasharray` value (e.g. `"6 3"`), or `""` for solid |

Reasonable defaults if you omit the style fields: `fill: "#334455"`,
`stroke: "#00f0ff"`, `strokeW: 2`, `opacity: 0.9`.

### `rect` — rectangle (walls, rooms, furniture blocks)

```json
{"id":"r1","type":"rect","x":100,"y":100,"w":300,"h":200,"rx":0,
 "fill":"#334455","stroke":"#00f0ff","strokeW":2,"opacity":0.9,"label":"","dash":"","layer":"Background"}
```
`x,y` = top-left corner. `w,h` = width/height. `rx` = corner radius (0 for sharp corners).

### `circle` — circle or ellipse

```json
{"id":"c1","type":"circle","cx":400,"cy":300,"rx":40,"ry":40,
 "fill":"#334455","stroke":"#00f0ff","strokeW":2,"opacity":0.9,"label":"","dash":"","layer":"Background"}
```
`cx,cy` = center. `rx,ry` = radii (equal = circle, unequal = ellipse). **Note:
this is `rx`/`ry`, not a single `r`.**

### `line` — straight or multi-segment line

```json
{"id":"l1","type":"line","x1":0,"y1":0,"x2":200,"y2":0,
 "stroke":"#00f0ff","strokeW":2,"opacity":0.9,"dash":"","layer":"Background"}
```
`x1,y1` → `x2,y2` is enough for a straight segment — the renderer
auto-derives endpoints if you omit `points`. For a multi-segment line/hallway
path, add `"points":[{"x":0,"y":0},{"x":100,"y":0},{"x":100,"y":80}]` (array
of **objects**, first/last matching x1/y1/x2/y2).

### `arrow` — same as `line`, plus an arrowhead at the end

Identical schema to `line` with `"type":"arrow"`. Renders with an arrowhead
at `(x2,y2)`.

### `poly` — filled polygon (irregular rooms, rubble, terrain)

```json
{"id":"p1","type":"poly","points":[[0,30],[15,5],[40,0],[55,18],[50,35],[20,40]],
 "fill":"#555","stroke":"#888","strokeW":1.5,"opacity":0.95,"label":"","layer":"Background"}
```
**`points` here is an array of `[x, y]` pairs (arrays), NOT `{x,y}` objects —
this is a different shape than `line`/`arrow`'s `points`. Easy to mix up.**

### `path` — freehand line (pencil tool)

```json
{"id":"pa1","type":"path","pts":[[0,0],[10,4],[22,3],[30,10]],
 "fill":"none","stroke":"#00f0ff","strokeW":2,"opacity":0.9,"layer":"Background"}
```
Field is `pts` (not `points`), also array of `[x, y]` pairs. Rarely worth
hand-authoring — prefer `poly`/`line` for anything geometric; use `path` only
if you're deliberately drawing a wavy/organic line.

### `text` — a text label

```json
{"id":"t1","type":"text","x":120,"y":90,"text":"Loading Dock","size":22,"color":"#f0f0f0","bold":false,
 "fill":"none","stroke":"#f0f0f0","strokeW":0,"opacity":1,"label":"","dash":"","layer":"Labels"}
```
`x,y` is the text baseline's start (left edge). `text` is the string content.
`size` is font size in px. `color` is a hex string. The `fill`/`stroke`/etc.
fields are boilerplate for structural consistency — only `text`, `size`,
`color`, `bold` actually affect rendering.

### `pin` — a map-style pin marker with an optional label

```json
{"id":"pn1","type":"pin","x":300,"y":250,"stroke":"#ff4466","label":"Trapped chest",
 "fill":"#334455","strokeW":2,"opacity":0.9,"dash":"","layer":"Background"}
```
`x,y` is the point the pin's tip touches. `stroke` is the pin's color.
`label`, if set, renders as text to the right of the pin.

### `image` — an embedded image

```json
{"id":"im1","type":"image","x":50,"y":50,"w":400,"h":300,"href":"https://.../floor.png",
 "opacity":1,"fill":"none","stroke":"none","strokeW":0,"label":"","dash":"","layer":"Background"}
```
`href` must be a URL the browser can load directly, or a `data:` URI. There's
no server-side upload endpoint that produces a usable `href` for this
purpose (see the note on `/upload` above) — either point at an
already-hosted image, or inline a small image as a base64 data URI. For most
floor plans, prefer building it from vector shapes instead.

### `measure` — a temporary distance annotation

```json
{"id":"m1","type":"measure","x1":0,"y1":0,"x2":100,"y2":0,"opacity":1,"layer":"Background"}
```
Renders in a fixed orange color with the pixel distance and angle as a label.
This is meant as a scratch UI tool (the human "M" hotkey) — rarely worth
persisting, but harmless if you do.

---

## Layers

`layer` is just a string on each element. The side panel's Layers section has
four **fixed, built-in** toggle rows: `Background`, `Tracks`, `Stations`,
`Labels` — elements using any of those four names get individual show/hide
and lock controls in the UI. Elements using any *other* layer name (e.g.
`"Walls"`, `"Furniture"`) still render completely normally — they just won't
have their own row in that panel (no error, no hidden behavior). For a
typical building/dungeon floor plan, `Background` (walls/floors) and
`Labels` (room names) map naturally to the two most relevant built-in names;
invent additional ones freely for your own organization if useful.

---

## Reusable furniture/symbol vocabulary

The editor's built-in "Symbols" stamp palette composes these exact element
groups (verified from `SYMBOLS` in `schematic.html`). Copy and offset
(`x`/`y`/`cx`/`cy`) these as a starting vocabulary instead of inventing your
own furniture styling from scratch:

**Door** (60×10 footprint):
```json
{"type":"rect","x":0,"y":0,"w":60,"h":10,"fill":"#5a4a3a","stroke":"#fff","strokeW":2,"opacity":0.95}
```

**Window** (60×8 footprint):
```json
[{"type":"rect","x":0,"y":0,"w":60,"h":8,"fill":"rgba(100,180,255,0.3)","stroke":"#88ddff","strokeW":2,"opacity":0.95},
 {"type":"line","x1":0,"y1":4,"x2":60,"y2":4,"stroke":"#88ddff","strokeW":1}]
```

**Stairs** (80×80 footprint):
```json
[{"type":"rect","x":0,"y":0,"w":80,"h":80,"fill":"#2a2a2a","stroke":"#888","strokeW":1.5,"opacity":0.95},
 {"type":"line","x1":0,"y1":16,"x2":80,"y2":16,"stroke":"#aaa","strokeW":1},
 {"type":"line","x1":0,"y1":32,"x2":80,"y2":32,"stroke":"#aaa","strokeW":1},
 {"type":"line","x1":0,"y1":48,"x2":80,"y2":48,"stroke":"#aaa","strokeW":1},
 {"type":"line","x1":0,"y1":64,"x2":80,"y2":64,"stroke":"#aaa","strokeW":1}]
```

**Table** (120×60):
```json
{"type":"rect","x":0,"y":0,"w":120,"h":60,"fill":"#7a5a3a","stroke":"#fff","strokeW":2,"opacity":0.95,"label":"Table"}
```

**Chair** (40×44):
```json
[{"type":"rect","x":0,"y":4,"w":40,"h":40,"fill":"#5a4a3a","stroke":"#fff","strokeW":1.5,"opacity":0.95},
 {"type":"rect","x":0,"y":0,"w":40,"h":6,"fill":"#333","stroke":"#fff","strokeW":1,"opacity":0.95}]
```

**Bed** (80×120):
```json
[{"type":"rect","x":0,"y":0,"w":80,"h":120,"fill":"#3a3a55","stroke":"#fff","strokeW":2,"opacity":0.95},
 {"type":"rect","x":8,"y":6,"w":64,"h":20,"fill":"#aaaacc","stroke":"#fff","strokeW":1,"opacity":0.95,"label":"Bed"}]
```

**Tree** (r=25, centered):
```json
{"type":"circle","cx":0,"cy":0,"rx":25,"ry":25,"fill":"#3a6a3a","stroke":"#5a8a5a","strokeW":2,"opacity":0.95}
```

**Rock**:
```json
{"type":"poly","points":[[0,30],[15,5],[40,0],[55,18],[50,35],[20,40]],"fill":"#555","stroke":"#888","strokeW":1.5,"opacity":0.95}
```

**Desk** (100×50):
```json
[{"type":"rect","x":0,"y":0,"w":100,"h":50,"fill":"#4a3a2a","stroke":"#fff","strokeW":2,"opacity":0.95},
 {"type":"rect","x":6,"y":8,"w":24,"h":34,"fill":"#222","stroke":"#888","strokeW":1,"opacity":0.95}]
```

Every element in these snippets still needs its own unique `id` and a
`layer` added before it's valid — the snippets above omit those since
they're positioned relative to `(0,0)` and meant to be offset per placement.

---

## Worked example: a small guard room

A 400×300 room with two walls-as-rects framing it, a door, a table with two
chairs, and a room label.

```json
{
  "elements": [
    {"id":"wall-n","type":"rect","x":100,"y":100,"w":400,"h":20,"fill":"#444","stroke":"#888","strokeW":1,"opacity":1,"label":"","dash":"","layer":"Background"},
    {"id":"wall-s","type":"rect","x":100,"y":380,"w":400,"h":20,"fill":"#444","stroke":"#888","strokeW":1,"opacity":1,"label":"","dash":"","layer":"Background"},
    {"id":"wall-w","type":"rect","x":100,"y":100,"w":20,"h":300,"fill":"#444","stroke":"#888","strokeW":1,"opacity":1,"label":"","dash":"","layer":"Background"},
    {"id":"wall-e","type":"rect","x":480,"y":100,"w":20,"h":300,"fill":"#444","stroke":"#888","strokeW":1,"opacity":1,"label":"","dash":"","layer":"Background"},

    {"id":"door-1","type":"rect","x":260,"y":105,"w":60,"h":10,"fill":"#5a4a3a","stroke":"#fff","strokeW":2,"opacity":0.95,"label":"","dash":"","layer":"Background"},

    {"id":"table-1","type":"rect","x":250,"y":220,"w":120,"h":60,"fill":"#7a5a3a","stroke":"#fff","strokeW":2,"opacity":0.95,"label":"Table","dash":"","layer":"Background"},
    {"id":"chair-1","type":"rect","x":220,"y":224,"w":40,"h":40,"fill":"#5a4a3a","stroke":"#fff","strokeW":1.5,"opacity":0.95,"label":"","dash":"","layer":"Background"},
    {"id":"chair-2","type":"rect","x":380,"y":224,"w":40,"h":40,"fill":"#5a4a3a","stroke":"#fff","strokeW":1.5,"opacity":0.95,"label":"","dash":"","layer":"Background"},

    {"id":"label-1","type":"text","x":250,"y":140,"text":"Guard Room","size":22,"color":"#f0f0f0","bold":true,
     "fill":"none","stroke":"#f0f0f0","strokeW":0,"opacity":1,"label":"","dash":"","layer":"Labels"}
  ]
}
```

POST that whole object as the body of `POST /maps/schematic/{slug}/elements`.

---

## Linking a map pin to a schematic

If the schematic represents a building/location that also has a pin on a
world map, you can wire the two together from the map side: on
`/maps/{map-slug}`, a custom marker (added via the "+ Marker" edit tool) has
an optional `link_schematic` field holding the schematic's slug. This is set
through the marker dialog's "Link to Schematic" dropdown in the UI, or
programmatically via `POST /api/maps/{map-slug}/overlay` with a
`custom_markers` array where one entry includes
`"link_schematic": "old-warehouse"`. Clicking that marker's popup then shows
an "Open Schematic →" link. There's no reverse link (schematic → map) —
schematics don't currently know which map they're associated with.

---

## Gotchas summary

- The `/elements` save endpoint **replaces the whole array** — always fetch
  and merge if you're editing rather than creating from scratch.
- `poly.points` is `[[x,y], ...]`; `path.pts` is also `[[x,y], ...]`;
  `line`/`arrow`'s optional `points` is `[{x,y}, ...]` — three different
  shapes across three element types, easy to transpose.
- `circle` uses `rx`/`ry`, never a bare `r`.
- Every `id` must be unique within the schematic (no server-side check —
  duplicate ids will confuse the editor's selection/undo logic if a human
  opens it later).
- All of this is GM-only; authenticate as a GM account first.
- `image_url` and `markers_json` on the `Schematic` model are dead fields —
  don't bother with them.
