# Map interchange formats — what the primary sources say

Companion to [`../MAP_CREATOR_RESEARCH.md`](../MAP_CREATOR_RESEARCH.md). The first round of this research described the formats
from search summaries and one third-party document, and warned to verify before writing import/export code. This round did that:
every statement below says what it rests on.

| Tag | Meaning |
|---|---|
| **[file]** | read off a real exported file (a Dungeondraft 1.0.1.3 export, format 0.3, 3.8 MB, and the native map it was exported from) |
| **[types]** | read from the `foundry-vtt-types` 13.346 package (TypeScript types and constants derived from Foundry's own source) or the `@owlbear-rodeo/sdk` 3.1.0 package |
| **[code]** | read from working converter code that other people ship (two independent ones, see below) |
| **[tested]** | a prototype in `prototypes/` does it and a test pins it (`node --test "docs/map-creator-research/prototypes/*.test.js"`) |
| **[secondary]** | a search-result summary, or a page the research sandbox could not open (arkenforge.com, foundryvtt.com, docs.owlbear.rodeo, web.archive.org and several community sites are blocked here) |
| **[unverified]** | behaviour of a program I could not run (Foundry, Roll20, Owlbear, Dungeondraft themselves) |

Sources actually opened: the sample file `exampleMaps/sampleMap.dd2vtt` + `sampleMap.dungeondraft_map` from
[Imagix/uvtt2fgu](https://github.com/Imagix/uvtt2fgu) (BSD-3-Clause; a stripped copy is in `prototypes/fixtures/` with its notice);
`dungeondraft-mcp` 0.1.0 on npm (MIT; reads `.dd2vtt`, writes format 0.2, converts to a Foundry v13 scene);
[shadowfray/donjon2uvtt](https://github.com/shadowfray/donjon2uvtt) (writes format 0.3);
`@league-of-foundry-developers/foundry-vtt-types` 13.346.0-beta; `@owlbear-rodeo/sdk` 3.1.0 (MIT).

---

## 1. Universal VTT (`.dd2vtt`, `.uvtt`, `.df2vtt`)

One JSON document with the map image inside as base64. The three extensions are the same format named after the program that
wrote it (Dungeondraft, Dungeon Fog, Arkenforge). **[secondary]** — the Arkenforge specification page could not be opened; everything
below was reconstructed from the real file and two implementations, and they agree.

### The real file **[file]**

```
top level keys, in order:  format, resolution, line_of_sight, objects_line_of_sight, portals, environment, lights, image
format                      0.3                         (a JSON number, not a string)
resolution.map_origin       { "x": 2, "y": 1 }          squares - NOT zero in a real export
resolution.map_size         { "x": 10, "y": 10 }        squares
resolution.pixels_per_grid  256                         whole number; 10 x 256 = 2560 = the PNG's width and height
line_of_sight               4 polylines, 11 points      [[{x,y}, {x,y}], ...]  squares; one closed ring repeats its first point
objects_line_of_sight       1 polyline, 60 points       the outline of a decorative round object (0.79 squares across)
portals                     2                           { position{x,y}, bounds[{x,y},{x,y}], rotation, closed, freestanding }
environment                 { "baked_lighting": false, "ambient_light": "ffffffff" }
lights                      2                           { position{x,y}, range, intensity, color "ffeccd8b", shadows }
image                       base64 PNG, 2,836,658 bytes  (the whole file is 3.8 MB)
```

What the numbers teach:

1. **Coordinates are absolute squares including `map_origin`.** The map's origin is (2, 1) and its walls run x 3–11, y 2–10 — one
   square in from the top-left of the image. A pixel position is `(x − map_origin.x) × pixels_per_grid`. Dungeondraft crops the
   export to the drawn area (the native map is 12 × 11 squares; the export is 10 × 10 at origin (2, 1)). **[file]**, **[code]**,
   **[tested]** (`uvtt.test.js`: "coordinates in the file are absolute"). The first round of this research did not say this.
2. **A door is a gap in the walls plus a portal.** The dividing wall (x = 7) comes as three 2-point polylines because it is cut where
   the two portals sit; the portal's `bounds` are the two ends of the gap (7, 2.5)–(7, 3.5), `position` its middle, `rotation`
   π/2 (1.570796) for a vertical door. `closed: true` is a shut door, `false` an open one. **[file]**
3. **A window is a portal too.** In the native map the second portal's texture is `window_11.png`; in the export it is an ordinary
   portal with `closed: false`. A UVTT file cannot say "blocks walking but not seeing", so a window imported into Foundry/Roll20
   is an open door. **[file]**, matches the open question in `dungeondraft-mcp`'s notes **[code]**.
4. **Lights are `aarrggbb`** — eight hex digits, alpha first, no `#`: `ffeccd8b` is an opaque #eccd8b. `range` is in squares,
   `shadows` a boolean. **[file]**
5. **`objects_line_of_sight` is real and small objects are expensive.** Objects that "block light" (a pillar, a table) are
   written as outlines; this one is 60 points, i.e. **59 wall segments** if an importer turns it into walls. Simplifying the outline
   by 0.05 squares leaves 9 **[tested]** (`foundry.test.js`). Imported naively, a furnished Dungeondraft map can carry thousands of
   walls for its props alone.
6. **Version.** Dungeondraft 1.0.1.3 writes 0.3 with `objects_line_of_sight`; `donjon2uvtt` writes 0.3 with it empty; `dungeondraft-mcp`
   writes 0.2 without it. Readers should treat `line_of_sight`, `portals`, `lights` and `objects_line_of_sight` as optional.
   **[file]**, **[code]**

### What a safe reader checks **[tested]**

`prototypes/uvtt.js` `validate()`: `format` is a number; `map_size` and `pixels_per_grid` positive; every polyline an array of at least
two `{x, y}` with finite numbers (NaN/Infinity are refused — the same class of bug as the move-token defect in
[CURRENT_STACK_AUDIT.md](CURRENT_STACK_AUDIT.md)); portals have a position and two bounds; colours are `aarrggbb`; unknown newer formats
are accepted with a warning. A file whose image is missing is readable (the fixture has none) and says so.

---

## 2. Dungeondraft's own map (`.dungeondraft_map`) **[file]**

JSON whose values are Godot variant strings: `"Vector2( 1792, 768 )"`, `"PoolVector2Array( 1792, 512, 1792, 2560 )"`, `"PoolIntArray(…)"`.

```
header      creation_build "1.0.1.3 awaken dryad", creation_date, uses_default_assets, asset_manifest[], editor_state
world       format 3, width 12, height 11 (squares), grid{color,texture}, msi{…}, levels{ "0": { … } }
level       label, environment{baked_lighting, ambient_light}, layers{…}, shapes{polygons, walls},
            tiles{cells PoolIntArray, colors[], lookup}, patterns[], walls[], portals[], cave{bitmap PoolByteArray…},
            terrain{splat PoolByteArray…}, water, materials, paths[], objects[], lights[], roofs, texts[]
wall        points PoolVector2Array (pixels), texture "res://textures/walls/battlements.png", color "ff726e65", loop, type, joint,
            shadow, node_id, portals[ { position, rotation, scale, direction, texture "res://textures/portals/door_06.png",
            radius 128, wall_id, wall_distance 0.125, closed, node_id } ]
object      position, rotation, scale, mirror, texture "res://textures/objects/furniture/tables/small_table_04.png", layer, shadow,
            block_light, custom_color
light       position, range 5, intensity, color, texture, shadows, node_id
```

Everything visual is a `res://…` path into an asset pack, so a native map cannot be *displayed* without the same packs; what can be reused
is the structure (walls with doors cut in, lights). A Foundry module that imports native maps exists (`moo-man/FVTT-DD-Import`, MIT)
**[secondary]**. Conclusion for nd-world: import **UVTT**, not the native map.

---

## 3. Foundry VTT v13 scene data **[types]**

Field rules for the documents a converter writes. (The numbers below correct the first round of this research, which gave `move` the
same five values as the senses and did not mention integers.)

### Wall (`walls[]`)

| Field | Rule |
|---|---|
| `c` | `[x0, y0, x1, y1]`, **four integers** — "must be a length-4 array of integer coordinates" |
| `move` | **`0` none or `20` normal — nothing else** (`WALL_MOVEMENT_TYPES`) |
| `light`, `sight`, `sound` | `0` none · `10` limited · `20` normal · `30` proximity · `40` distance (`WALL_SENSE_TYPES`) |
| `dir` | `0` both · `1` left · `2` right |
| `door` | `0` none · `1` door · `2` secret |
| `ds` | `0` closed · `1` open · `2` locked |
| `doorSound`, `threshold{light,sight,sound,attenuation}`, `animation{…}` | optional extras (thresholds are positive numbers or null) |

A door is a wall with `door: 1` — there is no separate door document. **[types]**

### Ambient light (`lights[]`)

`x`, `y` **integers**; `rotation`; `walls` (true: blocked by walls); `vision`; `hidden`; `config`: `dim` and `bright` (radii, in the
scene's distance units), `color` (a CSS colour or null), `alpha` 0–1, `angle` 0–360, `luminosity` 0–1, `attenuation` 0–1,
**`shadows` a number 0–1 (not a boolean)**, `animation{…}`, `darkness{min,max}`, `negative`, `priority`. **[types]**

### Scene

`name`, `background.src`, `width`/`height` (positive integers), `padding` 0–0.5 (default 0.25), `initial{x,y,scale}`, `backgroundColor`,
`grid{ type, size, style, thickness, color, alpha, distance, units }` with `size` an **integer ≥ 20** (`GRID_MIN_SIZE`) and
`type` 0 gridless · 1 square · 2/3 hex rows (pointy, odd/even) · 4/5 hex columns (flat, odd/even); `tokenVision`; `fog{exploration,…}`;
`environment{ darknessLevel, darknessLock, globalLight{enabled,…}, base{…}, dark{…} }`; embedded `walls`, `lights`, `tokens`,
`tiles`, `drawings`, `notes`, `regions`, `sounds`, `templates`. **[types]**

### Where a UVTT point lands **[code]**, **[tested]**

```
pixel = (square − map_origin) × pixels_per_grid + paddingOffset
paddingOffset = ceil(width × padding / grid) × grid      (Foundry places the map inside a padded canvas)
```

With padding 0 there is no offset (the converter in `prototypes/foundry.js` defaults to 0; Foundry's own default of 0.25 gives a
768-pixel shift on the 2560-pixel sample). Both converters round to integers — mandatory. A third-party note says Foundry **v14** moves the
background from the scene to its first *Level* **[secondary]**; the schema above is v13.

### Result on the real sample **[tested]**

```
7 wall segments (4 polylines, 11 points) + 2 doors + 2 lights, 2560 x 2560 px, grid 256
walls[0].c        [1280, 256, 1280, 384]          (file (7,2)-(7,2.5), origin (2,1))
doors             c [1280,384,1280,640] ds 0 (closed)   and   c [1280,1951,1280,2145] ds 1 (the window-portal, open)
lights[0]         x 1682  y 2048  dim 25  bright 12.5  color #eccd8b  alpha 0.5  shadows 0.5     (range 5 squares x 5 ft)
with padding 0.25 every coordinate shifts by +768; with objectWalls "sight-only": +9 walls for the pillar outline
```

`prototypes/foundry.js` `validateScene()` checks every rule in the three tables and passes on 32 generated maps (4 grid sizes × 8 seeds) in the tests.

---

## 4. Owlbear Rodeo (SDK 3.1.0, MIT) **[types]**

Items: `WALL { points: Vector2[], doubleSided, blocking }`, `LIGHT { attenuationRadius, sourceRadius, falloff, innerAngle, outerAngle,
lightType: PRIMARY | SECONDARY | AUXILIARY }`, plus image/shape/line/path/curve/text/label/note/effect/ruler/pointer items.
Grid: `{ dpi, type: SQUARE | HEX_VERTICAL | HEX_HORIZONTAL | DIMETRIC | ISOMETRIC, measurement: CHEBYSHEV | ALTERNATING | EUCLIDEAN |
MANHATTAN, scale: "5ft" }`. Importers that read UVTT and Foundry scene JSON exist as extensions ("Scene Importer", GPL-3.0; Smoke & Spectre;
Dynamic Fog) **[secondary]** — so a UVTT export reaches Owlbear without any Owlbear-specific work. Two things the SDK knows that the other formats
do not: **isometric/dimetric grids** and a **grid scale as one string** (`"5ft"`).

---

## 5. What maps to what

| nd-world concept (prototype data) | UVTT | Foundry v13 | Owlbear |
|---|---|---|---|
| wall (grid edge run) | `line_of_sight` polyline (merged, cut at doors) | wall `c`, all four senses `20`, `move 20` | `WALL` `blocking` |
| door, closed / open | portal `closed` true/false + gap in the wall | wall `door 1`, `ds 0/1` | wall item (extension) |
| locked door | portal `closed true` — **locked is lost** | `ds 2` | – |
| secret door | **no equivalent** — export as a wall (and warn) | `door 2` | – |
| window (blocks walking, not sight) | **no equivalent** — wall (default) or open gap | wall `move 20`, `sight 0`, `light 0` | – |
| light-blocking prop | `objects_line_of_sight` outline | wall `move 0` (sight-only) after simplifying | – |
| light (colour, radius in cells) | `lights[]` `aarrggbb`, `range` squares | ambient light `dim`/`bright` (units), `color`, `shadows` 0–1 | `LIGHT` |
| scale (cell = 5 ft) | `pixels_per_grid` only; units are not in the file | `grid.distance` + `grid.units` | `scale: "5ft"` |
| square / hex grid | **square only** | square, 4 hex types, gridless | square, 2 hex, 2 isometric |
| token, HP, conditions | – | token documents (not converted here) | image items |
| lore link, notes | – | journal entries (nd-world already exports these) | – |

Everything in the "lost" cells is a **report line**, not a silent drop: `uvtt.fromGrid` returns `warnings`, `foundry.toScene` returns `warnings`.

## 6. Pitfalls found while building the converters (each has a test)

1. Origin ≠ 0 in real files; coordinates are absolute. 2. Foundry coordinates and light positions must be integers — round, and drop walls that
collapse to a point. 3. `move` accepts only 0 and 20. 4. Foundry `shadows` is a number. 5. Padding shifts everything unless set to 0.
6. A window is a portal; a secret door is nothing. 7. A round prop's outline is dozens of tiny walls — simplify. 8. The image is the cost, not the walls
(next section). 9. A closed ring repeats its first point; a polyline that continues through a door gap is two polylines. 10. A token standing at a cell centre must
never be on a wall — true for lattice walls, **false** for smoothed cave outlines unless the outline is chamfered (`gridwalls.contourRings`, see
[PROTOTYPES.md](PROTOTYPES.md)).

## 7. Export cost — measured in the browser **[measured]**

Rendering the 44 × 30-cell furnished dungeon (76 props) to PNG and wrapping it: SVG → `<img>` → canvas → PNG → base64, headless Chromium, software rendering.

| px per cell | image | PNG | `.dd2vtt` | PNG encode |
|---|---|---|---|---|
| 30 | 1320 × 900 (1.2 Mpx) | 1.0 MB | 1.4 MB | 0.22 s |
| 50 | 2200 × 1500 (3.3 Mpx) | 2.1 MB | 2.8 MB | 0.55 s |
| 70 | 3080 × 2100 (6.5 Mpx) | 3.5 MB | 4.7 MB | 0.98 s |
| 100 | 4400 × 3000 (13.2 Mpx) | 5.9 MB | 7.9 MB | 2.0 s |

The walls, doors and lights are 131 segments (110 + 21) in **4.7 KB of JSON (0.75 KB gzipped)**; the rest of the file is image. All four files read back through `uvtt.parse`, convert to a
valid Foundry scene (110 walls, 21 doors, no schema problems). So an in-browser export is cheap; what limits it is browser canvas size
(commonly 16,384 px per side; **[unverified]** for each browser) and the importing program's upload limit **[unverified]**. A 100 × 70-cell map at 70 px is 7000 × 4900 (34 Mpx).

## 8. Still unverified

Whether Foundry/Roll20/Owlbear accept the files this prototype writes (no installation available); the Arkenforge specification text itself; Dungeon Scrawl's and
Dungeon Alchemist's UVTT dialects; `objects_line_of_sight` semantics inside Roll20 (movement or only light?); Foundry v14 data model. **Before shipping an exporter:
import its output into a real Foundry world and a real Roll20 game once.**
