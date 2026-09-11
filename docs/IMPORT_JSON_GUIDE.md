# How to build a JSON file for `/import`

A practical, "what do I actually type" companion to the `/import` page (📥
Import in the nav). That page accepts a JSON blob — pasted, uploaded, or
several files at once — figures out what kind of content it is, and creates
it in the active world. This guide shows the minimal shape for every kind it
recognizes, what gets auto-detected vs. what you have to pick manually, and
where the deeper field-by-field references live.

For the exhaustive field list on Entities/Player Characters (every column,
every `*_json` shape, `custom_fields_json` semantics) see
[`AI_ENTITY_GUIDE.md`](AI_ENTITY_GUIDE.md). For schematics/floor-plan element
shapes see [`AI_SCHEMATIC_GUIDE.md`](AI_SCHEMATIC_GUIDE.md). This guide is the
map of "which kind do I want and what's the smallest working example," not a
replacement for those.

---

## How it works

1. **Analyze** (`POST /api/import/detect`) — send `{"json_text": "<your JSON as a string>"}`; it returns `{"kind", "summary", "count", "needs"}`. `needs` lists any extra details (a map/schematic to attach to, a template's name) the UI will ask for before import can run.
2. **Execute** (`POST /api/import/execute`) — send `{"json_text": "...", "kind": "<from step 1, or override it>", "params": {...}}`. `kind` is optional if you're happy with what `detect` guessed; pass it explicitly to skip auto-detection or force a different interpretation of ambiguous JSON.
3. Multiple files can be imported in one pass via the "📦 Bulk JSON Import" section, or combined into a single request with the `batch` wrapper (see below).

Everything below is what goes in `json_text` (write it as real JSON — the
`"as a string"` phrasing above just means the HTTP body wraps it in quotes).

---

## Kind reference

### `entity_single` — one world entity (NPC, location, creature, item, event, organization, feat, race, profession, note)

```json
{
  "kind": "creature",
  "name": "Rustfang",
  "subtype": "Cyber-Wolf",
  "summary": "A scavenged pack-hunter with a chrome jaw.",
  "body": "## Description\nStalks the undercity scrapyards in packs of 3-5...",
  "tags": "undercity, pack-hunter",
  "folder": "Bestiary/Undercity",
  "visible_to_players": true
}
```
Only `kind` and `name` are required. `kind` must be one of this world's
recognized kinds (the 8 built-ins — `character, location, organization,
creature, event, item, feat, note` — plus `race`/`profession`, plus any
custom kinds this world has registered). Auto-detected whenever the JSON has
both a recognized `kind` and a `name`.

### `entity_bulk` — several entities at once

A JSON array of the shape above. **All-or-nothing**: if any entry fails
validation, nothing in the batch is created.

```json
[
  { "kind": "location", "name": "The Neon Bazaar", "summary": "A sprawling night market." },
  { "kind": "character", "name": "Elena the Merchant", "subtype": "NPC", "folder": "NPCs" }
]
```

### `player_character` — one player's character sheet

```json
{
  "name": "Anders de Valliere",
  "player_name": "Sam",
  "race": "Human",
  "char_class": "Widow-Bound Hunter",
  "level": 3,
  "xp": 450,
  "backstory": "Hunts the beasts that took his mother's life.",
  "notes": "## Signature Move\nThe Widow's Ember — a killing blow that burns the beast that made him.",
  "equipment": ["Grandcouteau (greatknife)", "Traveling pack", "Tinder kit"],
  "stats": [{"id": "str", "label": "Strength", "value": 3}]
}
```
Only `name` is required — everything else defaults sensibly. Every
`*_json` column (`stats`, `equipment`, `feats`, `attacks`, `cyberware`,
`conditions`, `currency`, `skills`, `custom_fields`) can be given either as
its full `_json`-suffixed name or the short alias shown above, and as a real
JSON array/object (not a pre-encoded string) — the importer encodes it for
you. Field names are matched leniently (`char_class`/`charClass`/`charclass`
all land on the same column), so an inconsistently-cased hand- or
AI-authored file doesn't silently drop data. Full field list, what each
`*_json` shape means, and how `sheet_template_id`/N&D-specific fields
(`shock_max`, `pp_current`, edges, etc.) work: see
[`AI_ENTITY_GUIDE.md`](AI_ENTITY_GUIDE.md#3-player-characters).

Detected whenever `"kind"` is absent or isn't a real Entity kind, **and**
at least one recognizable PC field (`race`, `char_class`, `level`, `stats`,
`equipment`, ...) is present — a bare `{"name": "..."}` with nothing else is
`unknown`, not silently guessed as a character.

### `player_character_bulk` — several characters at once

A JSON array of the shape above (each upserted by name — importing the same
name twice updates the existing character rather than creating a duplicate).

### `world_rules` — replace this world's Rules document

```json
{ "rules_md": "# House Rules\n\n## Combat\n..." }
```
Overwrites the world's current rules outright — there's no merge. Redirects
to `/rules`.

### `map_overlay` — add markers/regions to an existing map

```json
{
  "custom_markers": [{"x": 120, "y": 340, "label": "Ambush point", "icon": "⚔️"}],
  "custom_regions": [{"points": [[0,0],[100,0],[100,100]], "label": "Flood zone"}]
}
```
Requires `params: {"map_slug": "<the map's slug>"}` — always shows up in
`needs`, since there's no way to guess which map from the JSON alone.
Markers/regions are **appended** to whatever the map already has, not
replaced, up to a per-map cap.

### `schematic_elements` — add/update elements on a floor-plan schematic

```json
{ "elements": [{"id": "wall-1", "type": "line", "x1": 0, "y1": 0, "x2": 200, "y2": 0}] }
```
(A bare array of element objects also works, with no `{"elements": ...}`
wrapper.) Requires `params: {"schematic_slug": "<slug>"}`, or
`"schematic_slug": "__new__"` plus `new_schematic_name` (and optionally
`new_canvas_width`/`new_canvas_height`/`new_canvas_bg`) to create a brand
new schematic from the import. An element with an `id` that already exists
on the target schematic is updated in place; everything else is appended.
Full element-type reference (rect/circle/line/arrow/poly/path/text/pin/
image/measure/token, and every field each type takes): see
[`AI_SCHEMATIC_GUIDE.md`](AI_SCHEMATIC_GUIDE.md).

### `random_table` — one or more GM random tables

```json
{
  "name": "Undercity Encounters",
  "category": "encounters",
  "description": "Roll d10 on entering a scrapyard sector.",
  "entries": [
    {"roll": "1-3", "text": "A pack of cyber-wolves, hunting."},
    {"roll": "4-7", "text": "Scrap salvagers, wary but not hostile."},
    {"roll": "8-10", "text": "Nothing — the sector's gone quiet. Too quiet."}
  ]
}
```
An array of these objects imports several tables at once.

### `field_template` — a reusable stat-block/sheet field layout

```json
{
  "name": "Basic Stat Block",
  "fields": [
    {"id": "hp", "label": "Hit Points", "type": "number", "section": "Combat"},
    {"id": "abilities", "label": "Abilities", "type": "list", "section": "Combat",
     "item_fields": [{"id": "name", "label": "Name", "type": "text"}, {"id": "effect", "label": "Effect", "type": "textarea"}]}
  ]
}
```
Field `type` is one of `number, resource, text, textarea, table, list`
(`list` items nest their own `item_fields`, as shown above). Requires
`params: {"template_kind": "entity"|"sheet", "name": "..."}` (`name` falls
back to the JSON's own `"name"` if present); for `template_kind: "sheet"`
you can also pass `params.sheet_mode: "nd"|"custom"` (default `"nd"` — see
[`AI_ENTITY_GUIDE.md`](AI_ENTITY_GUIDE.md) for what that distinction means);
for `template_kind: "entity"` you can pass `params.entity_kind` to restrict
the template to one Entity kind.

### `batch` — several different kinds in one request

```json
{
  "imports": [
    { "kind": "location", "name": "The Neon Bazaar" },
    { "kind": "entity_single", "data": {"kind": "creature", "name": "Rustfang"} },
    { "kind": "player_character", "data": {"name": "Anders de Valliere"} },
    { "kind": "field_template", "data": {"fields": [...]}, "params": {"template_kind": "sheet", "name": "Hunter Sheet"} }
  ]
}
```
Each entry is either a bare blob (auto-detected the same as a standalone
import) or an explicit `{"kind", "data", "params"}` — use the explicit form
for anything `needs` extra `params` (field templates, map/schematic
imports). Best-effort, not all-or-nothing: each item is imported
independently and the response lists which succeeded and which didn't,
so one bad item doesn't block the rest.

---

## Two conveniences the importer has that the type-specific routes don't

1. **`template_id` can be a slug or a name.** For an entity's
   `template_id`, you can pass `template_slug` (matches the template's
   slug) or `template` (matches its name, case-insensitively) instead of a
   numeric id — useful since a hand- or AI-authored file has no way to know
   this instance's real template ids ahead of time.
2. **`custom_fields_json` can be spelled `custom_fields`**, and given as a
   real JSON object rather than a pre-encoded string — same dual-spelling
   applies to every `*_json` field on a player character (see the
   `player_character` section above).

**Limitation:** everything the importer creates is scoped to the active
world — there's no way to import a global/built-in (`world_id: null`)
template or table through it.

---

## Getting JSON without writing it by hand: "📷 Import from Photo (AI)"

The `/import` page also has an **Import from Photo (AI)** section: upload a
photo (or a few pages) of a physical/handwritten character sheet or handout,
and a vision-capable Ollama model reads it into an editable draft — no JSON
typing required. Under the hood it's the same two-step flow as everything
above: `POST /api/ai/character-from-images` or `/api/ai/entity-from-images`
returns a draft in exactly the `player_character`/`entity_single` shape
documented here (nothing is written yet), you review/edit it in the page,
and clicking Create sends that reviewed JSON straight to
`POST /api/import/execute`. Needs a vision-capable model (e.g.
`llama3.2-vision`, `llava`, `qwen2-vl`, `gemma3`) selected as the active
Ollama model — a text-only model will return a mostly-empty draft since it
can't actually see the photo.
