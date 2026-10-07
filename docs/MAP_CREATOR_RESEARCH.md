# From schematic editor to a full TTRPG map creator — research

Question: *how do we turn the schematic maps into a full-blown TTRPG map creator?*

This is a research document, not a plan that has been started. Nothing in the app changed. It says what the schematic
editor is today, what a "full" map creator actually contains (from how the established tools work), what technology it
would take, what could be measured here, and a phased route with the decisions that need a human.

**How to read the evidence tags.**
**[measured]** = run in this repository's environment while writing this (numbers and caveats given);
**[source]** = taken from a web page or package named in [Sources](#sources) (secondary summaries are marked);
**[inferred]** = my reasoning from the code or the sources, worth checking before relying on it.
Several sites (Arkenforge, Foundry's docs, Roll20's help centre, Red Blob Games, Dungeon Scrawl's blog) could not be
opened from the research sandbox, so what is said about them rests on search-result summaries and on one third-party
package's documentation. **Verify the interchange formats against their own specs before writing import/export code.**

---

## 1. Short answer

1. **Today's editor is a drawing tool with live tokens, not a map-making tool.** It has shapes, a snap grid, nine stamps, a
   hex/square overlay, tokens linked to characters and combat, and a player view. It has no scale (a cell is not "5 ft"),
   no walls/doors/lights as objects, no textures or asset library, no fog or vision, no generators and no import/export
   with the rest of the ecosystem. [source: code, §2]
2. **A "full-blown" creator is six capabilities that every serious tool shares:** (1) a grid- and scale-first canvas;
   (2) walls with doors/windows as *smart objects*; (3) floors/terrain with textures plus an asset library with scatter
   brushes; (4) lights, vision and fog of war; (5) generators; (6) export to the VTT ecosystem (Universal VTT, Foundry
   scenes, print). [source, §3]
3. **Do not try to out-art Dungeondraft.** Build around what only nd-world has: maps that are **linked to the lore**
   (rooms ↔ entities, pins ↔ locations), **connected to live play** (tokens, combat tracker, player view), **self-hosted
   and offline**, **AI-assisted from the world's own lore**, and **controllable through MCP**. Import/export of
   Universal VTT gives access to the existing ecosystem without rebuilding every feature. [inferred]
4. **The renderer has to change before features are added.** The current SVG editor rebuilds every node on every change;
   in this sandbox one redraw took ~0.2–0.7 s at 1,000 objects and ~1–4 s at 12,000, and zooming fell from 53 fps (200
   objects) to 3 fps (12,000). A real dungeon with walls, props and scatter easily has thousands of objects.
   [measured, caveats in §5.1] Recommended: a **PixiJS (WebGL)** renderer for the new editor and player view, SVG kept
   for legacy maps and for export. The sandbox has no GPU, so WebGL speed could **not** be measured here; a ready-made
   benchmark to run on real hardware is in `docs/map-creator-research/render-bench.html`.
5. **The hard algorithms are cheap.** Vision polygons for 6 tokens over 500 wall segments took ~11 ms, over 2,000
   segments ~51 ms; merging 800 overlapping rooms into one outline took ~340 ms [measured, Node, CPU only]. Fog of war and
   "draw rooms, walls appear" are feasible in the browser without special infrastructure.
6. **Let the AI plan, not draw.** The current AI Build asks the model for raw coordinates (`SYSTEM_TEMPLATE`,
   `app/main.py:7235`), which models are poor at. Better: the model returns a *spec* (rooms, sizes, connections, contents,
   linked lore entities) and deterministic code lays it out. [inferred]
7. **First shippable value (Phase 1–3 in §7):** scale-aware canvas, walls + doors, floors, an asset library, then
   Universal VTT export — a map made here runs in Foundry/Roll20 — and live fog in nd-world's own player view.
8. **Decisions needed from you** are listed in §8 (battle maps vs world maps first, art direction, whether live
   fog/vision inside nd-world matters, asset sourcing, collaboration, mobile).

---

## 2. Where nd-world is today

Two map systems plus a legacy one, all GM-edited and player-viewed:

| | Maps (`/maps`) | Schematics (`/maps/schematic/{slug}`) | Legacy HTML schematics |
|---|---|---|---|
| Content | a background image + markers + region overlays; Leaflet | vector elements on a canvas; SVG | static `.html` files in `static/schematics/` |
| Editing | markers/regions in the viewer | full editor, 2,356-line `app/templates/schematic.html` (inline JS) | none |
| Live play | read-only viewer | tokens, combat link, player view that polls every 4 s | none |

What the schematic editor has (verified in `schematic.html`, `schematic-render.js`, `AI_SCHEMATIC_GUIDE.md`):

- Tools: rect, circle, line, arrow, polygon, freehand path, text, pin, image, measure, token, area-of-effect
  (`ICONS`, `schematic.html:771`). Align/distribute/rotate/flip, z-order, copy/paste, undo/redo, snap + alignment guides
  (`SNAP = 20`, `:653`), minimap, SVG/PNG export.
- Battle-map layer: square or hex grid overlay with configurable cell size/offset; tokens with size categories, HP,
  conditions; **pull from / push to the Combat Tracker**; merchant tokens, item pick-up; party pins.
- Player view (`schematic_view.html`): hidden elements are dropped **server-side** (`_schematic_player_payload`,
  `app/main.py:3411`) — a good secrecy rule to keep. Players move only their own token.
- AI Build: lore-grounded (RAG) background job returning elements as JSON; the general importer (`/import`, `/api/import/execute`) can also add elements through the API.
- Interchange: SVG/PNG export, the JSON element importer. Nothing else.

What is missing for a map *creator*, and what limits growth:

| Gap | Evidence |
|---|---|
| **No scale.** Everything is canvas pixels; the grid is an overlay "purely a rendering/snapping aid" (`models.py`, `Schematic.grid_type`). A 5 ft square means nothing to the data. | `AI_SCHEMATIC_GUIDE.md` "Coordinate system" |
| **Walls, doors, windows, lights are just rects/lines.** Nothing knows a door blocks movement or sight. | element schema |
| **Four fixed layer names** (`Background, Tracks, Stations, Labels` — metro-map leftovers) and four solid backgrounds. | `schematic.html:1897`, `canvas_bg` |
| **Nine hand-written stamps**, no asset library, no textures or patterns, no upload-and-place. | `SYMBOLS`, `schematic.html:1851` |
| **Whole map rebuilt on every change** (`redraw()` clears and recreates all nodes, then the element list, minimap and layer panel). | `schematic.html:773` — see §5.1 for the cost |
| **Whole map re-downloaded every 4 s by every player**, and every token move read-modify-writes the entire `elements_json`. At 12,000 objects the JSON is ~2 MB raw / ~190 KB gzipped *per poll per player*. | `schematic_view.html:275`, `move-token` handler, [measured] sizes |
| **Last-writer-wins saving**: `POST …/elements` replaces the whole array; two editors overwrite each other. | `AI_SCHEMATIC_GUIDE.md` |
| **No fog, vision, lighting.** Players see every non-hidden element. | `_schematic_player_payload` |
| **No import/export** to Universal VTT, Foundry, print-at-scale. | — |
| **Editor is one inline script**, hard to test; pure geometry is only partly in `schematic-render.js`. | `app/templates/schematic.html` |
| **The image-map viewer loads Leaflet from `unpkg.com`**, which breaks the app's own "must work with no internet" rule (the dice tray, for instance, vendors its libraries). | `app/templates/map_viewer.html:4-5` |
| **Unused fields**: `Schematic.image_url` and `markers_json` exist but the editor does not use them. | `AI_SCHEMATIC_GUIDE.md` |

The conclusion for design: keep what works (tokens ↔ characters ↔ combat, the player-view secrecy rule, background AI
jobs, the importer) and replace the *drawing core* underneath it.

---

## 3. What a "full-blown" map creator is — the landscape

Three scales of map, with different tools. nd-world already has a foot in each:

| Scale | Typical tools | Typical content |
|---|---|---|
| **World / region** | Inkarnate, Wonderdraft, Azgaar's Fantasy Map Generator | terrain painting, biomes, rivers, roads, settlement icons, labels |
| **Town / building** | Watabou's generators, Dungeondraft, Inkarnate city maps | street/plot layouts, building interiors, furniture |
| **Battle map / dungeon** | Dungeondraft, Dungeon Scrawl, Dungeon Alchemist, Foundry/Roll20 scene tools | rooms, walls, doors, props, lighting, tokens, fog |

Facts about the established tools (secondary sources, see [Sources](#sources)):

- **Dungeondraft** — loadable art packs of terrain, objects, walls, paths, lights; "smart" tools that place walls and
  floors, scatter props, and an integrated lighting engine; exports walls and lights in **Universal VTT** files for
  Foundry, Roll20 and Fantasy Grounds. [source]
- **Dungeon Scrawl** — free, in the browser; polygon and cave/tunnel brushes, layers with opacity/blend, copy/mirror,
  square/hex/isometric grids, built-in styles, a random dungeon tool (imports Donjon TSV), asset import scaled by
  pixels-per-cell, export to PNG/WebP/PDF (true scale across pages) and **UVTT**, direct Roll20 connection. [source]
- **Dungeon Alchemist** — "procedural AI" (not generative) that fills rooms with props from a drawn layout, 3D preview,
  exports images/video and VTT packages for Foundry, Roll20, Fantasy Grounds and UVTT; $44.99 on Steam per the review
  page. [source]
- **Inkarnate / Wonderdraft** — world and regional maps; Inkarnate in the browser (pro ≈ $25/year per a comparison page),
  Wonderdraft a desktop app (≈ $29.99 one-off); both asset-library driven with random generation. Prices change — check
  before quoting. [source]
- **Foundry VTT** — WebGL (PixiJS) canvas; walls with separate movement/sight/light/sound restrictions, doors with
  states, ambient lights, vision and an explored-area fog; an event-driven visibility layer recomputes only what a change
  affects. [source]
- **Owlbear Rodeo** — static fog since the start (GMs draw and cut away hidden shapes); dynamic fog (walls, doors,
  lights; the map is revealed by what tokens can see) through extensions, faster since its "Warp Core" update. [source]
- **Open-source references**: Azgaar's Fantasy Map Generator (**MIT**, verified from its `LICENSE`), Red Blob Games'
  mapgen4 (**Apache-2.0** per search summaries), Dungeon Revealer (an open-source self-hosted fog-of-war table app;
  licence not verified).

### Feature taxonomy and where nd-world stands

| Capability | Dungeondraft | Dungeon Scrawl | Foundry scene | nd-world today |
|---|---|---|---|---|
| Grid + real scale (px per cell, ft per cell) | ✔ | ✔ | ✔ | overlay only, no units |
| Walls / doors / windows as objects | ✔ | UVTT export (what it contains was not verified) | ✔ (restriction types, door states) | ✗ |
| Floors, terrain, textures, patterns | ✔ | ✔ (brushes, styles) | tiles | solid fills |
| Asset library + scatter | ✔ (packs) | ✔ (image library) | tiles/tokens | 9 stamps |
| Layers (user-defined, opacity, lock) | ✔ | ✔ | limited | 4 fixed names |
| Lights, vision, fog of war | lights authored; export to VTT | export only | ✔ | ✗ (manual "hidden" flag) |
| Tokens, combat link | – | – | ✔ | ✔ (a real strength) |
| Generators | – | random dungeon import | – | AI Build (coordinates) |
| Import / export (UVTT, Foundry, print) | UVTT export | PNG/PDF/UVTT | UVTT import | SVG/PNG only |
| Links to world lore | – | – | journals | tokens ↔ entities/characters |

---

## 4. Where nd-world can win (positioning)

Competing on art would be a losing, endless race. The leverage is connection:

1. **Lore-linked maps**: a room, building or pin *is* an entity (location/organization) with notes; opening it from the
   map opens the lore, and the lore page shows its map. Nothing in the standalone tools does this. [inferred]
2. **One place for the whole table**: tokens ↔ player characters ↔ combat tracker ↔ session notes already exist.
3. **Self-hosted, offline-first**: vendored libraries, no CDN, works at a table with no internet.
4. **AI that knows the world**: generation grounded in the world's lore (the app's RAG), with the model producing a
   *design spec*, not pixels.
5. **MCP/API-controllable**: the app already exposes MCP tools; map tools ("add room", "place asset", "run generator")
   make maps scriptable by an AI assistant. The Dungeondraft ecosystem is already exploring exactly this (an
   MCP server that edits `.dungeondraft_map` files and converts `.dd2vtt` to Foundry scenes exists as an npm
   package). [source]
6. **Interchange instead of lock-in**: import UVTT from Dungeondraft/Dungeon Scrawl and *run* it with nd-world's tokens
   and combat link; export UVTT to use elsewhere.

---

## 5. Technology options

### 5.1 Rendering

Measured on the existing editor with generated maps (rects, circles, lines, polygons) in headless Chromium **with software
rendering and no GPU** [measured]. Treat the absolute values as pessimistic; the *growth* is the finding.

| Objects | One `redraw()` (5 runs, ms) | SVG nodes | Zoom toggle (fps) |
|---|---|---|---|
| 200 | 39–259 | 200 | 53 |
| 1,000 | 191–675 | 1,000 | 33 |
| 3,000 | 292–1,255 | 3,000 | 9 |
| 6,000 | 562–2,429 | 6,000 | 4 |
| 12,000 | 1,120–3,845 | 12,000 | 3 |

`redraw()` includes rebuilding the element list, minimap and layer panel, so it is more than SVG painting — which is the
point: the editor's structure (rebuild everything) is as much the problem as SVG itself.

| Option | Size (min) | Licence | Fits because | Costs |
|---|---|---|---|---|
| **Keep SVG, make it incremental** | 0 | – | crisp, styleable, trivially exported, no new dependency | DOM cost still grows with objects; no shaders for lighting/fog; ceiling ≈ 2–3k visible objects |
| **Canvas 2D (+ Konva)** | Konva 194 KB | MIT | retained scene graph, hit-testing, layers, caching; viewport culling is easy | CPU raster; lighting/fog via compositing tricks; fine to a few thousand objects |
| **WebGL (PixiJS 8)** | 841 KB min bundle (tree-shakeable via ESM) | MIT | GPU sprites/batching, filters (blur, colour, masks) for light and fog, thousands of textured objects; **what Foundry uses** | bundle weight; needs WebGL (software fallback is slow — see the sandbox); text and accessibility need care; one more renderer to maintain |
| Fabric.js / Paper.js / two.js | 21 MB / 12 MB / 2 MB unpacked | MIT | editor-style object models | heavier or less suited to many-object, shader-driven maps |

Sizes from `npm pack` of each package's published minified bundle; licences from the npm registry. [measured]

**Recommendation [inferred]:** PixiJS for the new editor and the player view; keep `schematic-render.js` SVG for legacy
schematics and for SVG export. WebGL has precedent in the app now (the dice tray uses three.js), so a no-WebGL fallback
message pattern already exists. **Do a one-day spike on real hardware first** with `docs/map-creator-research/render-bench.html`
(Canvas 2D vs Konva vs Pixi; open it with `?n=1000`, `?n=5000`, `?n=12000`, `?n=30000`, on a laptop and a mid-range phone) — the sandbox could only prove the
page runs, not how fast it is.

### 5.2 Data model

Replace "one flat array of pixel shapes" with a versioned document in **grid units** (so scale is built in), separate from
runtime state. Sketch [inferred]:

```jsonc
{
  "version": 2,
  "scale": { "px_per_cell": 70, "unit": "ft", "units_per_cell": 5, "grid": "square|hex-pointy|hex-flat|none" },
  "size": { "w": 40, "h": 30 },                      // cells
  "levels": [{                                        // floors of a building / dungeon depth
    "id": "l1", "name": "Ground floor",
    "background": { "color": "#111", "image": null },
    "floors":  [{ "id": "f1", "polygon": [[x,y],...], "texture": "stone", "tint": "#888" }],
    "walls":   [{ "id": "w1", "points": [[x,y],...], "type": "stone|wood|fence|cave", "blocks": {"move":1,"sight":1,"light":1,"sound":1} }],
    "portals": [{ "id": "p1", "wall": "w1", "t": 0.4, "kind": "door|window|secret|gate", "state": "closed|open|locked" }],
    "objects": [{ "id": "o1", "asset": "pack/table_round", "x": 12.5, "y": 8, "rot": 30, "scale": 1, "layer": "furniture", "shadow": true }],
    "paths":   [{ "id": "r1", "asset": "road_dirt", "points": [[x,y],...], "width": 1.2 }],
    "lights":  [{ "id": "t1", "x": 10, "y": 6, "range": 6, "color": "#ffaa55", "intensity": 0.8, "shadows": true }],
    "labels":  [{ "id": "n1", "text": "Armoury", "x": 5, "y": 4, "link": { "entity": 123 } }]
  }],
  "layers": [{ "id": "furniture", "name": "Furniture", "visible": true, "locked": false, "opacity": 1, "gm_only": false }],
  "environment": { "ambient": "#222233", "darkness": 0.4 },
  "legacy": [ /* the old elements, rendered by the SVG renderer in a layer, so no schematic is lost */ ]
}
```

Runtime state lives apart from geometry: `tokens`, `door states`, `fog exploration`, `revealed regions`. That fixes two
problems at once: a token move no longer rewrites megabytes, and static geometry can be cached by the player's browser
with a **revision/ETag** instead of being re-sent every 4 s. [inferred] The app already has a change bus
(`app/live.py`, SSE counter → `nd-live` event) that can replace the 4-second poll. A migration reads old
`elements_json` into `legacy` and keeps working. New columns need `database._heal_table` entries (see AGENTS.md).

Optimistic concurrency (`updated_at`/revision on save, 409 on conflict) is a cheap upgrade over today's last-writer-wins
and a prerequisite for assistants editing too.

### 5.3 Walls, vision and fog

**Algorithms** [source: Red Blob Games' visibility article via search summary; library from npm]: a visibility polygon is
found by sweeping a ray around the observer's position over the sorted wall endpoints while tracking the nearest wall —
O(n log n) per source. `visibility-polygon` (MIT, 5.5 KB min) implements it.

**Measured** (Node, CPU only, random walls — worse than real maps, which mostly meet at endpoints) [measured]:

| Wall segments | `breakIntersections` (once per edit) | 1 source | 6 sources (one move of 6 tokens) |
|---|---|---|---|
| 100 | 8 ms | 2 ms | 5 ms |
| 500 | 14 ms | 4 ms | 11 ms |
| 2,000 | 155 ms | 16 ms | 51 ms |
| 5,000 | 927 ms | 95 ms | 519 ms |

Up to ~2,000 segments is comfortable on token moves (throttle to once per frame); 5,000+ wants spatial culling
(`rbush`/`flatbush`, both tiny and MIT/ISC) to consider only walls within a source's range.

`polygon-clipping` (MIT, 29 KB min) unions floors into outlines: **50 rooms 10 ms, 200 rooms 34 ms, 800 rooms 337 ms**
[measured] — enough for "draw rooms, the walls appear around them" and for merging reveal regions.

**Secrecy design — the important decision.** The app's player view already filters server-side. Fog has three tiers:

1. **Static fog (GM-drawn)**: the GM paints/erases hidden regions; the server sends players only elements outside hidden
   regions. No vision math. *Cheapest and most useful first step* (Owlbear and Dungeon Revealer started here).
2. **Dynamic vision, GM-authoritative**: the GM's browser (open anyway during play) computes the union of visible regions
   for the player tokens and posts reveal deltas; players get the revealed area. Secure from players, simple.
   Player-initiated moves are applied when the GM page recomputes.
3. **Dynamic vision, server-authoritative**: the server holds walls and computes visibility itself (needs a Python port of
   the algorithm, tested against the JS one). Players need nothing from the GM page. Most work.

**Caveat for imported images [inferred]:** a map that is one raster image (e.g. a Universal VTT import) is entirely
visible to a browser that receives it; Foundry itself trusts clients with this. For native vector maps the server can omit
unseen geometry (true secrecy). For image maps, either accept "table-trust fog" (client mask) or have the server cut the
image into tiles and send only revealed ones. Decide per use case and say so in the UI.

### 5.4 Assets and licences

An asset library is what makes maps look finished. Options [source, secondary]:

| Source | Licence | Use |
|---|---|---|
| **Kenney** packs | CC0 (no attribution) | bundle a small core set |
| **game-icons.net** | CC BY 3.0 (credit required) | token/pin/symbol icons; show an attribution list in the app |
| **2-Minute Tabletop** tiles | CC BY-NC 4.0 | **do not bundle** — non-commercial terms muddy an MIT repo and a published Docker image |
| **Dungeondraft packs** | creator's own terms; `pack.json` has an `allow_3rd_party_mapping_software_to_read` flag | possible importer, **only** for packs that opt in |
| **GM-uploaded assets** | the GM's responsibility | the practical default: per-world packs (image + category + footprint in cells + anchor + shadow) |
| **AI-generated textures/tokens** | depends on the model | the app already has Image Studio (SwarmUI); "generate a cobblestone floor" is a natural, differentiating feature |

Watabou's generators export JSON/SVG, but their licence terms were not found; treat them as inspiration and as an
*import* path only after checking.

### 5.5 Generators

All deterministic and seedable (a seed makes a map reproducible and testable) [inferred]:

- **Dungeon**: BSP or random room placement + corridors (minimum spanning tree over room centres, plus a few extra loops).
- **Caves**: cellular automata, smoothed, then walls from the outline (`polygon-clipping`).
- **Buildings**: from a **room graph** (rooms with sizes and adjacency) — treemap/BSP layout, doors on shared walls.
- **Town / region**: Voronoi/Delaunay (`d3-delaunay` ISC 19 KB, `delaunator` ISC 8 KB) for districts and plots;
  `simplex-noise` (MIT) for terrain; rivers/biomes as in mapgen4 (Apache-2.0, a design reference) or by importing
  Azgaar's exports (MIT).

**LLM role:** return the *spec* ("a smuggler's den: bar, two storerooms, hidden back door; rooms linked to entities X and
Y") through the Studio structured-output path the app already uses (`format=` in `generate_chat`), and let the layout code
place it. The model never produces coordinates. That also lets the result be *edited* afterwards, since it is real walls
and objects, not a picture.

### 5.6 Interchange formats

**Universal VTT (`.dd2vtt`, `.uvtt`, `.df2vtt`)** — JSON with the map image embedded as base64. As documented by a
third-party package that verified it against Dungeondraft samples [source]:

```
format                 0.2 (Dungeondraft); the Arkenforge spec also documents 0.3
resolution             { map_origin{x,y}, map_size{x,y} in SQUARES, pixels_per_grid }
line_of_sight          [[{x,y},{x,y},…], …]   wall polylines, in squares
objects_line_of_sight  [[…]]                  light-blocking objects (newer exports)
portals                [{ position{x,y}, bounds[{x,y},{x,y}], rotation (rad), closed, freestanding }]
environment            { baked_lighting, ambient_light (ARGB) }
lights                 [{ position{x,y}, range (squares), intensity, color (ARGB), shadows }]
image                  base64 PNG/WEBP
```

Everything is in squares (× `pixels_per_grid` = image pixels); colours are ARGB hex. Roll20 documents importing walls,
doors, windows and lights from these files, with ~70 px per grid as the standard. [source]
**Windows are not distinguished from doors** in the format.

**Foundry wall document** [source: Foundry API]: `c: [x0,y0,x1,y1]`, `move`/`sight`/`light`/`sound` restriction
(0 none, 10 limited, 20 normal, 30 proximity, 40 distance), `dir`, `door` (type), `ds` (door state), `threshold`. A
conversion of UVTT → Foundry is: one wall per `line_of_sight` segment with `move/sight/light/sound = 20`, doors from
`portals` with `door: 1`, lights → ambient lights (dim = range × unit distance; colours ARGB → drop the alpha pair). [source]

Export needs a **rendered image** of the map at `pixels_per_grid` — with a PixiJS renderer that is
`renderer.extract` of the scene, with the image-size ceilings of browsers in mind (the Dungeondraft tooling notes a
16,384 px export cap). Print export (1 inch per square, tiled across pages) is a separate, simpler path from the same
scale data.

---

## 6. Proposed architecture

```
 Map v2 document (grid units, versioned)  ──►  validator + migration (Python + JS, shared test vectors)
        │                                          ▲
        ├─ geometry modules (pure JS, Node-tested): grid/hex math, walls, snapping, polygon ops,
        │      visibility, generators (seeded), UVTT import/export
        ├─ renderer: PixiJS scene (editor + player view);  legacy SVG renderer for old schematics + SVG export
        ├─ runtime state (tokens, door states, fog)  ── separate rows/JSON, small, frequently written
        ├─ server: save with revision (409 on conflict), view payload = static geometry (ETag) + state;
        │      secrecy filter (no hidden/unseen geometry in player payloads); nd-live bus replaces the 4 s poll
        └─ AI/MCP: spec → generator → editable map; MCP tools for maps
```

Cross-cutting rules that already exist in this repo and should carry over: pure logic in `static/js/*.js` modules tested under
Node (as `dice-geometry.js`, `live-health.js`); vendored libraries with their licences in `static/vendor/` and no CDN
(`tests/test_dice_tray_page.py` pattern); a new HTTP route gets a row in `docs/API_REFERENCE.md`; background AI jobs for
anything over ~100 s; inline scripts must parse (`tests/test_template_scripts.py`).

Suggested performance budgets (to make "done" testable) [inferred]: 5,000 objects at 60 fps on a mid-range laptop iGPU;
2,000 on a mid-range phone; initial load of a 3,000-object map under 2 s; vision update for 6 tokens under 50 ms at 2,000
wall segments (measured feasible above); player payload for an unchanged map ≈ a `304`.

---

## 7. Phased roadmap

Sizes are relative (S ≈ a few days, M ≈ 1–3 weeks, L ≈ 3–6 weeks, XL longer) for one developer with AI assistance. They are
estimates, not commitments; Phase 0's spike should recalibrate them.

### Phase 0 — Groundwork (M)
- Vendor Leaflet (no `unpkg`), and any library the next phases need, with licences.
- Extract the pure parts of `schematic.html` (geometry, grid, element model, undo) into Node-tested modules.
- Define Map v2 (§5.2): schema, validator, migration that keeps old schematics alive in `legacy`.
- Save with a revision (409 on conflict); gzip/ETag for the view payload.
- **Renderer spike on real hardware** (`render-bench.html`) → decide Pixi vs Konva vs incremental SVG.
- *Done when:* old schematics open unchanged; schema + migration have round-trip tests; renderer decision recorded here.

### Phase 1 — Authoring core (L)
Scale/grid-first canvas (cells, ft per cell, scale bar, measure in ft) → **wall tool** (polylines/closed rooms, snaps to
grid and endpoints) → **doors/windows/secret doors** on walls → **floor polygons** with textures/patterns, auto-outline
from rooms (`polygon-clipping`) → **asset library** (small CC0 core + GM uploads, categories, footprint in cells, rotate/
scale/mirror, scatter brush) → user-defined **layers** → patch-based undo/redo, copy/paste, align, keyboard shortcuts →
PNG/SVG export at scale and **print-to-scale**.
- *Done when:* a 30×30 dungeon with ~2,000 objects is drawn, saved, reloaded and exported; budgets from §6 met on the
  target devices; old tools still available via the legacy layer.

### Phase 2 — Live play (L)
Split runtime state from geometry; tokens gain vision/light radii; **lights**; **static fog first** (2a), then **dynamic
vision** (2b, GM-authoritative, §5.3); door toggling; player payload = geometry (ETag) + state, updated by the existing
`nd-live` bus instead of polling; per-player explored memory.
- *Done when:* a player's network payload contains no geometry they cannot see (**tested**), and a 6-token move updates
  fog in < 50 ms at 2,000 segments.

### Phase 3 — Interchange (M)
**Universal VTT export** (image + walls + portals + lights) and **import** (so a Dungeondraft/Dungeon Scrawl map can be run
with nd-world's tokens, combat link and fog); Foundry scene JSON; print tiles. Verify against real `.dd2vtt` samples
before coding (see the caveat at the top).
- *Done when:* a map round-trips nd-world → UVTT → nd-world with walls/doors/lights intact, and a UVTT sample from another
  tool imports and plays.

### Phase 4 — Generators and AI (L)
Seeded dungeon/cave/building/town generators; **"describe it" → spec → layout** with lore RAG and room↔entity links;
"populate this room" (props from the library); MCP map tools; regenerate-a-region.
- *Done when:* the same seed gives the same map (**tested**); an AI-built map is fully editable walls and objects, not
  an opaque drawing.

### Phase 5 — World and region maps (L–XL)
Terrain/biome painting, rivers/roads, settlement icons and curved labels; pins ↔ entities ↔ battle maps ("zoom in");
decide whether to retire Leaflet for a Pixi world view (tile pyramid for very large images) or keep both; optional
import of Azgaar exports (MIT).

### Phase 6 — Polish and collaboration (M–L)
Visual styles/themes (blueprint, ink, colour), lighting FX, touch/tablet editing, multi-editor (locking first; a CRDT
such as Yjs only if real simultaneous editing is wanted), version history, asset-pack importer for opt-in packs.

### No-regret quick wins (each independent, S–M)
1. Vendor Leaflet — removes the offline hole today.
2. Units on schematics (ft per cell) and a measure tool that shows feet.
3. User-defined layers instead of the four fixed names.
4. ETag + gzip on `view.json`, and use `nd-live` instead of polling every 4 s.
5. Replace AI Build's "coordinates" prompt with a spec + layout step (a mini Phase 4).
6. More stamps and an upload-and-place image asset tool.

---

## 8. Risks and decisions needed

**Decisions (my default in brackets):**
1. **Which scale first?** Battle maps/dungeons, or world/region maps too? [battle maps; regions in Phase 5]
2. **Art direction?** Keep the neon/blueprint vector look (cheapest, consistent with the app) or textured
   Dungeondraft-style (needs assets and shadows)? [vector first, textures as an option per map]
3. **Is live fog/vision inside nd-world a goal**, or is "make maps and export to Foundry/Roll20" enough? This swings
   Phase 2 between essential and optional. [fog yes, static first]
4. **Is ~1 MB extra JavaScript (Pixi) acceptable**, loaded only on map pages? [yes, lazy-loaded]
5. **Who edits?** GM only, or assistants/players too (needs revisions/locking)? [GM + assistants, locking]
6. **Tablet/phone editing?** [view and token play on phones; editing desktop-first]
7. **Assets:** a small CC0 core + GM uploads, with game-icons.net for symbols? [yes; no NC assets bundled]
8. **How important is AI generation**, given local models are weak at geometry? [valuable, but spec-based]

**Risks and mitigations:**

| Risk | Mitigation |
|---|---|
| Scope creep — "full-blown" has no end | the phases each have a *done when*; Phase 3 interchange buys features without building them |
| Renderer rewrite breaks live play | legacy layer + old renderer stay until the new one passes the same player-view tests |
| WebGL missing/slow (low-end devices, software rendering — as in the sandbox) | feature-detect, message, and keep the SVG view as a fallback for players |
| Asset licensing | CC0/CC BY only in the repo; GM-supplied assets are the GM's; attribution page generated from a manifest |
| Fog secrecy expectations | say plainly which tier each map has (§5.3); tests that a player payload lacks hidden geometry |
| Vision cost on big maps | spatial index, per-source range culling, throttle to once per frame; budgets in §6 |
| Interchange format drift (UVTT versions, Foundry v13/v14) | sample-file tests; treat as best-effort import with a report of what was ignored |
| Single-process, SQLite server | state rows small and write-light; no per-frame writes; ETag for geometry |

**Not measured here and worth measuring next:** WebGL/Canvas frame rates on real devices (the bench page); memory
for large textures on phones; Python-side visibility cost if server-authoritative vision is chosen; real UVTT files from
two or three tools.

---

## Sources

Web pages (read through search summaries unless marked *fetched*):

- Roll20 — [Universal Virtual Tabletop (UVTT) Support](https://help.roll20.net/hc/en-us/articles/41643201127831-Universal-Virtual-Tabletop-UVTT-Support)
- Dungeon Scrawl — [UVTT export](https://blog.dungeonscrawl.com/uvtt-export), [itch.io page](https://probabletrain.itch.io/dungeon-scrawl), [Pro features (Roll20 help)](https://help.roll20.net/hc/articles/16981022708247)
- Foundry VTT API — [WallData](https://foundryvtt.com/api/interfaces/foundry.documents.types.WallData.html), [CanvasVisibility](https://foundryvtt.com/api/classes/foundry.canvas.groups.CanvasVisibility.html)
- Owlbear Rodeo — [Realtime Dynamic Fog (2.3 launch)](https://blog.owlbear.rodeo/owlbear-rodeo-2-3-release-week-day-3/)
- Dungeondraft — [What is Dungeondraft?](https://dndungeon.com/blogs/faq/what-is-dungeondraft-the-rpg-map-editor-explained)
- Dungeon Alchemist — [review](https://www.keengamer.com/articles/reviews/software-reviews/dungeon-alchemist-review-bringing-your-campaign-to-life-pc/), [GamingOnLinux](https://gamingonlinux.com/2022/04/ai-powered-map-creator-dungeon-alchemist-is-now-on-steam)
- Inkarnate vs Wonderdraft — [comparison list](https://arcaneeye.com/dm-tools-5e/dnd-map-makers/)
- Azgaar's Fantasy Map Generator — [LICENSE (MIT), *fetched*](https://raw.githubusercontent.com/Azgaar/Fantasy-Map-Generator/master/LICENSE)
- mapgen4 — [redblobgames/mapgen4](https://github.com/redblobgames/mapgen4) (Apache-2.0 per search summaries)
- Red Blob Games — [2D visibility](https://www.redblobgames.com/articles/visibility/)
- Assets — [Kenney (CC0) and game-icons.net (CC BY 3.0), summary](https://app.cinevva.com/guides/free-2d-sprites-tilesets); [2-Minute Tabletop (CC BY-NC 4.0)](https://2minutetabletop.com/?p=18889)
- Watabou — [forum thread](https://forum.profantasy.com/discussion/comment/85704) (licence terms not found)
- Dungeon Revealer — [AlternativeTo](https://alternativeto.net/software/dungeon-revealer/about) (licence not verified)

Package documentation: the npm package `dungeondraft-mcp@0.1.0` (`docs/research.md`, `docs/foundry-import.md`) — a third-party
description of the Universal VTT structure, Dungeondraft's map/pack formats and the UVTT → Foundry conversion, verified by its
author against Dungeondraft samples. It also cites the Arkenforge spec (arkenforge.com/universal-vtt-files/), which could not be
opened from here.

Library facts (licence, published bundle sizes) — npm registry and `npm pack`, **[measured]**: pixi.js 8.22.0 (MIT, 841 KB
min), konva 10.7.1 (MIT, 194 KB), visibility-polygon 1.1.0 (MIT, 5.5 KB), polygon-clipping 0.15.7 (MIT, 29 KB), delaunator
5.1.0 (ISC, 8 KB), d3-delaunay 6.0.4 (ISC, 19 KB), earcut 3.2.4 (ISC, 10 KB), rbush 4.0.1 (MIT, 6 KB), flatbush 4.6.2 (ISC),
simplex-noise 4.0.3 (MIT), roughjs 4.6.6 (MIT), js-angusj-clipper 1.3.1 (MIT); clipper-lib 6.4.2 and clipper2-wasm 0.4.0 carry
Boost-style licences.

## Appendix: how the measurements were made

- **SVG editor** — `/maps/schematic/{slug}` loaded in headless Chromium (software rendering, 1400×900) with N generated
  elements saved through `POST …/elements`; `redraw()` timed five times with `performance.now()`; "zoom toggle" is
  `doZoom()` alternated for 1.5 s counting animation frames. CPU-only sandbox: absolute values are pessimistic.
- **Canvas 2D / Konva / PixiJS** — the same scene was run, but under software WebGL even 1,000 objects ran at single-digit fps
  in every renderer, so **no renderer comparison is claimed**. Use `docs/map-creator-research/render-bench.html` on real hardware.
- **Visibility and clipping** — Node 22, `visibility-polygon` and `polygon-clipping` from `npm pack`; random axis-aligned wall
  segments 40–200 units long in a 4000×3000 area (more crossings than a real map), median of 3–5 runs.
- **Payload sizes** — the same generated elements, `json.dumps` and gzip.
