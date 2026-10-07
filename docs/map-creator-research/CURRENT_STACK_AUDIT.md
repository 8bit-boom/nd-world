# The current map stack — an audit (nd-world at `1774216`)

Companion to [`../MAP_CREATOR_RESEARCH.md`](../MAP_CREATOR_RESEARCH.md). Everything here was read in the code or run against the real app (a scratch database,
the repository's own test fixtures, headless Chromium). **Nothing in the application was changed**; the one real defect found has a verified patch you can apply (§3).

## 1. How the app is used — what the repository itself says

I cannot see how your table plays, so this is what the code and docs imply, and the research is written to hold for each of these:

| Mode | Evidence in the repo | What a map needs there |
|---|---|---|
| **In person, notebook + a second monitor / TV** | `/display` "second screen" (`app/routers/display.py`, `AGENTS.md`): the GM sends an *image or a text card* to a bare black window; `nd-stage.js` "send to screen" buttons | a map **on that screen**: true scale (1 square = 1 inch on the TV), fog and light, nothing else |
| **Remote, players on phones/laptops** | player live view `/maps/schematic/{slug}/view`: own token drag, pick up items, buy from merchants; Cloudflare-tunnel notes in the README; the Player Cockpit | a view that works on a phone (zoom/pan), live updates, secrecy |
| **Foundry as the table, nd-world as the binder** | `/export/foundry.json` and `/characters/{id}/export.foundry.json` export **journals and actors — not scenes** | UVTT/Foundry scene **export** so a map built here can be played there |
| **Self-hosted, offline-minded** | vendored three.js/cannon-es for the dice tray with a test that forbids CDNs; `AGENTS.md` | no CDN, small JS, no cloud |
| **AI-assisted prep** | AI Build for schematics, an AI "battlemap" image generator on the *new map* form (`map_form.html`: "top-down battlemap, no text, no grid overlay"), MCP server, lore RAG | maps that come from lore and can be edited afterwards |

Two facts stand out: the second screen **cannot show a live map at all** today, and the app already generates battlemap *pictures* with AI — pictures with no scale, grid or walls, which therefore cannot be played with tokens or fog.

## 2. Inventory

| Piece | Where | Size | Notes |
|---|---|---|---|
| Schematic editor (GM) | `app/templates/schematic.html` | 2,356 lines, one inline script, ~110 functions | SVG; `redraw()` rebuilds every node, the element list, minimap and layers panel |
| Player live view | `app/templates/schematic_view.html` + `static/js/schematic-render.js` (415 lines, shared with the editor) | 277 lines | polls `view.json` every 4 s (`ndPoll`, skips hidden tabs); **no zoom, pan or pinch**; own-token drag calls `redraw()` on every pointer move |
| Server | `app/main.py` ~3188–3970 (routes), 3411 `_schematic_player_payload`, 7235 AI Build | | whole `elements_json` rewritten on every save and every token move (`BEGIN IMMEDIATE` read-modify-write) |
| Model | `Schematic` (`models.py:1958`) | | `slug` is **globally** unique; `grid_type`, `grid_config_json` (`cell_size`, `offset`, `orientation`, **`unit_per_cell`, `unit_label`**); `combat_session_id`; `updated_at` |
| Preview thumbnails | `/maps/schematic/{slug}/preview.svg` | | a **second, Python renderer** of a subset of the element types |
| Image maps | `map_viewer.html` (702 lines), `app/maps/*.json`, `MapOverlay` | | Leaflet **loaded from unpkg** (lines 4–5) — the only external script left in the templates |
| Combat link | `link-combat`, `pull-combat`, `push-combat` | | tokens ↔ `CombatSession.combatants_json` |
| Importer / AI | `app/routers/importer.py` kind `schematic_elements`; `SYSTEM_TEMPLATE` (`main.py:7235`) | | AI Build asks a model for raw coordinates; its jobs live in an in-memory dict (by design, per the code comment: a restart means clicking Build again) |
| MCP | `app/mcp_server.py` | | **no map or schematic tool** (the tool list was checked) |
| Tests | `test_schematics` 18, `test_schematic_combat_sync` 9, `test_schematic_hex_grid` 1, `test_schematic_shape_rendering` 1 (runs `schematic-render.js` under Node with a DOM shim), `test_maps` 44, `test_maps_ai` 7, `test_maps_gallery_picker` 7 | | the editor's own logic (tools, undo, snapping) has **no tests** — it lives in one inline script |

## 3. Defects and drift found

### D1 — one request from a player breaks the map for everybody (verified) — **fix this first**

`POST /api/maps/schematic/{slug}/move-token` takes `x` and `y` through `float(...)` and stores them. Python's `json` module accepts `NaN` and `Infinity`
(and `1e999` becomes `inf`), `json.dumps` writes them back as the bare words `NaN` / `Infinity`, which are **not valid JSON**. Consequences, each reproduced:

| After `{"token_id":"…","x":NaN,"y":5}` from a player who has a token on the map | result |
|---|---|
| `GET /maps/schematic/{slug}/view.json` (the 4-second poll) | **500** (`Out of range float values are not JSON compliant`) |
| the player view page | 200, but `JSON.parse(...)` of the embedded elements **throws** — a dead page for every player |
| the GM editor page | 200, but its `JSON.parse({{ elements_json|tojson }})` **throws** at the top of the one big inline script — the **editor is dead**, so the GM cannot repair it in the UI |

`1e999` behaves the same; `1e300` and `-99999` are accepted too (a token thrown off the canvas). Needs only a logged-in player with a character on the map; recovery is database surgery.

*Fix, verified:* `docs/map-creator-research/patches/move-token-finite-numbers.patch` — numbers only (no booleans/strings/lists), `math.isfinite`, clamped to the canvas, 400 otherwise,
plus a regression test. With it applied the 18 existing `test_schematics` tests and the new one pass, as do the 224 tests in `test_schematic_combat_sync`, `test_maps`, `test_maps_ai`
and `test_player_safe`. **It is not applied** (this was a research task; `git apply docs/map-creator-research/patches/move-token-finite-numbers.patch` applies it).
The same validation belongs in every future player-writable numeric field (door toggles, fog updates).

### D2 — no player-view zoom, pan or pinch; heavy redraw while dragging

`schematic_view.html` has no wheel, pinch or touch-action handling; the SVG is just scaled to fit its container. A 3000 × 2000 px canvas on a phone is unreadable
(the screenshots in [PROTOTYPES.md](PROTOTYPES.md) show the effect even on a laptop). While a player drags their token `redraw()` rebuilds every node on every pointer move.

### D3 — polling instead of the live bus

The app has a change counter and an SSE stream (`app/live.py`, `nd-live.js`), used by sheets, parties and the second screen. **No map route calls `live.touch`**; every player downloads
the full payload every 4 seconds. The app gzips, so a realistic 1,000–4,000-element map costs ≈ 10–40 KB per poll per player (a generated 100 × 70 dungeon: 135 KB raw / 10 KB gzipped without props, 573 KB / 39 KB with 3,000);
at 12,000 objects it is ~2 MB raw (~190 KB gzipped) (measured in the first round). Wasteful rather than broken.

### D4 — drift between documents and code

- `docs/AI_SCHEMATIC_GUIDE.md:32` says "Everything here is GM-only — there is no player-facing view of schematics". There is one (above).
- `docs/API_REFERENCE.md` lists `/maps/schematic/{}/preview.svg` as "GM / Player*". The player allowlist (`_is_player_safe`) does **not** include it, so a player gets 403 (verified); only
  the GM and assistants see the preview thumbnails. (I checked whether it leaked hidden elements to players — it cannot be reached by them, so no leak.)
- `preview.svg` handles element types `poly` and `pencil`; the editor writes freehand strokes as **`path`** (`pts`) and polygons are drawn unfilled — freehand lines never appear in a preview,
  and `aoe`, `image` and `measure` are not drawn.
- `schematic-render.js` prints a "r 0.6 ft" size label on **every circle** when a grid unit is set — fine for a hand-drawn template, noise for a prop.

### D5 — the first round of this research said "no scale"; that was wrong

The grid dialog has **units per cell and a unit label** (`gdlg-unit-per-cell`, `gdlg-unit-label`, default 5 ft), a **📐 Calibrate** tool (drag across one known cell), and
`pxToUnits` feeds the measure tool, AoE templates and the circle label. What is missing is **geometry stored in cells** (everything is canvas pixels, snapping is a fixed 20 px,
not the cell), walls/doors as things, and a way to use the scale for anything but labels.

## 4. What to keep, what to replace

| Keep (it works and is wired in) | Replace or add |
|---|---|
| tokens ↔ characters/entities ↔ combat link, merchant and pickup flows | the **drawing core**: element model without walls/doors/lights, whole-array save, one 2,356-line script |
| the secrecy rule: hidden elements are dropped **server-side** (`_schematic_player_payload`) | move-token validation (D1); the extra server-side renderer (`preview.svg`) → one shared drawing path |
| SVG rendering (`schematic-render.js`), grid math incl. hex | a **walls layer** (data) and a **runtime layer** (door states, explored cells, light switches) separate from decoration |
| background AI jobs, importer, MCP patterns | live updates through `live.touch` + SSE; zoom/pan; the `/display` map mode; MCP map tools |
| `Schematic.grid_*` and the scale dialog | cell-based coordinates for new element types; `revision` for optimistic saves |

Whatever is added must follow the repository's existing rules: new player-reachable routes go in `_is_player_safe`, assistant-editable ones in `_is_assistant_safe`, every route gets a
row in `docs/API_REFERENCE.md` (a test enforces it), new columns need a `_heal_table` entry, new AI routes go in `TASK_PATH_PATTERNS`, inline scripts must parse
(`tests/test_template_scripts.py`), no CDN.
