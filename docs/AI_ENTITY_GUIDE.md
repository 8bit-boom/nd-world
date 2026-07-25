# Creating entities, stat blocks, and characters programmatically (for AI assistants)

This is a reference for an AI (Claude Code or otherwise) scripting content
creation in nd-world via HTTP — lore entities (NPCs, locations, creatures,
items...), the field-template systems that back stat blocks and character
sheets, and Player Characters. Every field name, route, and JSON shape below
was verified directly against `app/models.py`, `app/main.py`,
`app/routers/characters.py`, and `app/database.py`, and every worked example
was actually executed against a live instance — not inferred from the UI.

All routes here are **GM-only** unless noted otherwise; log in first
(`POST /login` with `email`/`password` form fields) and reuse the session
cookie.

---

## The content model, in one picture

nd-world has **two separate systems** that are easy to conflate:

| | **Entity** | **PlayerCharacter** |
|---|---|---|
| What it's for | Lore/world content: NPCs, locations, creatures, items, organizations, events, feats, notes | The players' own character sheets |
| Optional structured fields via | `EntityTemplate` (`/entity-templates`) | `SheetTemplate` (`/characters/templates`) |
| Field data lives in | `Entity.custom_fields_json` | `PlayerCharacter.custom_fields_json` |

They share the *same field-definition schema* (text/number/textarea/select/
list) but are otherwise independent models with independent template tables,
independent routes, and independent id sequences. A "Stat Block" you build
for a creature `Entity` and a "sheet" you build for a `PlayerCharacter` look
similar but are not interchangeable.

---

## 1. Entities (lore content)

### Model fields (`Entity`, `app/models.py`)

| Field | Type | Notes |
|---|---|---|
| `world_id` | int | set automatically from the active world |
| `kind` | string | one of the 8 kinds below — **required** |
| `subtype` | string | freeform-ish; see suggested values per kind below |
| `name` | string | required |
| `folder` | string | slash-separated for nesting, e.g. `"Bestiary/Vermin"` |
| `tags` | string | comma-separated, e.g. `"poison,ambush,sewer"` |
| `image_url` | string | external URL, or an uploaded file's `/uploads/...` path |
| `summary` | string | one-liner shown on cards |
| `body` | text | main content, rendered as Markdown |
| `visible_to_players` | bool | GM-only content vs. visible to the party |
| `template_id` | int, nullable | which `EntityTemplate` (if any) provides structured fields |
| `custom_fields_json` | JSON object | values for the template's fields — see §2 |

### Kinds and their suggested subtypes

```
character:    NPC, PC, villain, ally, neutral
location:     district, city, country, void station, moon, ruin, corp facility
organization: megacorp, syndicate, government, cult, secret society, gang, AI entity, family
creature:     mutant, animal, abomination, corp-enhanced, ice creature, undead
event:        corporate war, outbreak, disaster, political, yellow corruption, discovery
item:         weapon, armor, augment, bio-augmentation, drone, husk, vehicle, oddity, metal, item
feat:         common feat, origin feat, profession feat, profession ability, psy power, race feat
note:         lore, session note, rumor, prophecy, theory
```
`subtype` isn't validated server-side — these are just what the UI's dropdown
offers. Any string is accepted, or leave it blank.

### Routes

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/new?kind=creature&folder=...` | — | the create form (kind/folder pre-fill the form, not required for API use) |
| POST | `/new` | form-encoded | creates, redirects to `/entity/{id}` |
| GET | `/entity/{id}` | — | detail view |
| GET | `/entity/{id}/edit` | — | edit form |
| POST | `/entity/{id}/edit` | form-encoded | same fields as create |
| POST | `/entity/{id}/delete` | — | also cleans up links/notes/player-access rows |
| POST | `/entity/{id}/link/{target_id}` | — | adds a "related entity" cross-link (bidirectional in effect — shows on both) |
| POST | `/entity/{id}/unlink/{target_id}` | — | removes it |
| POST | `/entity/{id}/notes/new` | form: `content`, `visible` (any truthy value = visible to players) | adds a discrete `EntityNote`, independently hideable from the entity itself |
| POST | `/entity/{id}/notes/{note_id}/toggle` | — | flips that note's visibility |
| POST | `/entity/{id}/notes/{note_id}/delete` | — | |

**Create/edit form fields** (`POST /new` and `POST /entity/{id}/edit`, both
form-encoded, not JSON):

```
kind              required
subtype
name              required
folder
tags
image_url         paste a URL directly...
image_file        ...or upload a file (multipart) — takes priority over image_url
summary
body
visibility_mode   "everyone" (default) or "players" (only allowed_player_ids can see it)
allowed_player_ids  repeat the field per id, only used when visibility_mode=players
template_id       an EntityTemplate id, or omit/blank for none
custom_fields_json  JSON object string — see §2
```

### Worked example: a creature with a full stat block

```bash
COOKIE=cookies.txt
curl -s -c $COOKIE -X POST https://your-nd-world/login \
  -d "email=gm@example.com&password=yourpassword"

curl -s -b $COOKIE -X POST https://your-nd-world/new \
  --data-urlencode "kind=creature" \
  --data-urlencode "subtype=mutant" \
  --data-urlencode "name=Cistern Widow" \
  --data-urlencode "folder=Bestiary/Vermin" \
  --data-urlencode "tags=poison,ambush,sewer" \
  --data-urlencode "summary=A city-sized mutant spider lurking in the storm drains." \
  --data-urlencode "body=Found beneath Sector 7 since the last flood cycle." \
  --data-urlencode "visibility_mode=everyone" \
  --data-urlencode "template_id=2" \
  --data-urlencode 'custom_fields_json={"attackPool":"3d10","defensePool":"2d10","health":"18","armor":"2","speed":"30 ft, climb 20 ft","abilities":[{"name":"Venomous Bite","effect":"On hit, target makes a CON save or gains Poisoned (2 rounds)."},{"name":"Wall Crawler","effect":"Can move on vertical surfaces without a check."}]}'
# → redirects to /entity/{new id}
```
`template_id=2` is the built-in "Stat Block" template (see §2) — confirm its
actual id on your instance via `GET /entity-templates` rather than assuming
`2`, since ids depend on seed order and what else has been created.

---

## 2. Entity Templates (stat blocks & custom fields for entities)

An `EntityTemplate` is a reusable field schema. `world_id = NULL` means
global/available to every world (all built-ins are global); a world-scoped
template is only offered to entities in that world.

### Field schema (`fields_json`)

Each field is one of:

```json
{"id":"health","label":"Health / HP","type":"number","section":"Stat Block","default_value":"5"}
```
```json
{"id":"status","label":"Status","type":"select","section":"Details","options":["Alive","Dead","Missing","Unknown"],"default_value":"Alive"}
```
```json
{"id":"abilities","label":"Abilities","type":"list","section":"Stat Block","item_fields":[
  {"id":"name","label":"Name","type":"text"},
  {"id":"effect","label":"Effect","type":"textarea"}
]}
```

| Field | Meaning |
|---|---|
| `id` | key used in `custom_fields_json` — must be unique within the template |
| `label` | shown in the form |
| `type` | `text` \| `number` \| `textarea` \| `select` \| `list` |
| `section` | freeform heading used to group fields on the entity form |
| `default_value` | prefilled value (not used for `list`) |
| `options` | (`select` only) array of choice strings |
| `item_fields` | (`list` only) array of `{id, label, type}` for each column of a repeatable row — sub-field `type` is just `text` or `textarea` |

### How `custom_fields_json` maps to the schema

- Simple field (`text`/`number`/`textarea`/`select`): `{field_id: "string value"}`
  — note values are stored as strings even for `type: number`.
- `list` field: `{field_id: [{sub_id: "value", ...}, {sub_id: "value", ...}]}`
  — one object per row, keyed by the list's `item_fields` ids.

An entity's `custom_fields_json` is a flat object with one key per field id
across every section — sections are purely a form-layout grouping, not a
nesting level in the stored data.

### Built-in templates (seeded, `world_id: null`, `is_builtin: true`)

**`npc-details`** (slug), kind-restricted to `character`:
```
title (text) · age (text) · gender (text) · status (select: Alive/Dead/Missing/Unknown, default Alive)
— all in section "Details"
```

**`stat-block`** (slug), `kind: null` (usable on any kind — characters, creatures, items, whatever):
```
attackPool (text, default "1d10") · defensePool (text, default "1d10")
health (number, default "5") · armor (number, default "0") · speed (text)
abilities (list: name [text], effect [textarea])
— all in section "Stat Block"
```
This is the one to reach for a quick combat stat block regardless of kind.

Built-ins can have their **fields edited** (GM can extend the built-in stat
block) but their `name`/`description`/`kind` are locked, and they can't be
deleted.

### Routes

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/entity-templates` | — | list, grouped |
| GET | `/entity-templates/new` | — | create form |
| POST | `/entity-templates/new` | form: `name`, `description`, `kind` (one of the 8 kinds, or blank = any kind), `fields_json` | redirects to the edit page |
| GET | `/entity-templates/{id}/edit` | — | |
| POST | `/entity-templates/{id}/edit` | same fields | on a built-in, only `fields_json` is actually applied — name/description/kind are silently ignored |
| POST | `/entity-templates/{id}/delete` | — | 403 on built-ins; nulls out `template_id` on any entity using it first |

Slugs auto-generate from `name` (lowercase, non-alphanumeric → hyphens,
`-2`/`-3` on collision) and are otherwise not settable.

### Worked example: a custom template with every field type

```bash
curl -s -b $COOKIE -X POST https://your-nd-world/entity-templates/new \
  --data-urlencode "name=Vehicle Stats" \
  --data-urlencode "description=Stats for cars, bikes, and drones" \
  --data-urlencode "kind=item" \
  --data-urlencode 'fields_json=[
    {"id":"model","label":"Model / Make","type":"text","section":"Details","default_value":""},
    {"id":"condition","label":"Condition","type":"select","section":"Details","options":["Pristine","Worn","Junker","Wreck"],"default_value":"Worn"},
    {"id":"topSpeed","label":"Top Speed (mph)","type":"number","section":"Performance","default_value":"80"},
    {"id":"notes","label":"Notes","type":"textarea","section":"Details","default_value":""},
    {"id":"mods","label":"Installed Mods","type":"list","section":"Performance","item_fields":[
      {"id":"name","label":"Mod Name","type":"text"},
      {"id":"effect","label":"Effect","type":"textarea"}
    ]}
  ]'
```
Then create an `item` entity with `template_id` set to the returned id and
`custom_fields_json` like
`{"model":"Kessler Interceptor","condition":"Worn","topSpeed":"140","mods":[{"name":"Nitro Boost","effect":"+2 to chase rolls, once per scene"}]}`.

---

## 3. Player Characters

### The real field contract — read this before assuming the full model is settable

`PlayerCharacter` (`app/models.py`) has a *lot* of columns — ability scores,
`armor_class`, `alignment`, `background`, personality traits, appearance,
proficiencies, fixed-currency columns (`currency_cp`/`sp`/`ep`/`gp`/`pp`)...
**Most of these are dead weight left over from an earlier D&D-specific
design and are not settable through the create/edit routes at all.** Both
`POST /characters/new` and `POST /characters/{id}/edit` funnel through the
exact same `_apply_form()` (`app/routers/characters.py`), which only reads
this fixed list of fields — anything else you post is silently ignored and
the corresponding column stays at its model default forever:

```
name, player_name, race, race_id, char_class, profession_id, level, xp,
backstory, notes, max_hp, current_hp, shock_max, shock_current,
pp_current, mp_current, minor_edge, major_edge, minor_edge_count,
major_edge_count, sheet_template_id, custom_fields_json,
stats_json, skills_json, currency_json, equipment_json, feats_json,
attacks_json, cyberware_json, conditions_json
```
(Verified directly: creating a character with `currency_json` full of real
values still leaves `currency_cp` at `0`, and `str_score` stays `10`
regardless of what's posted — those columns are simply never touched.)

**Ability scores and currency are represented in the "universal" JSON fields
below, not the fixed columns** — this is the actual, current mechanism:

- `stats_json`: `[{"id":"str","label":"Strength","abbr":"STR","value":4}, ...]`
  — freeform list, not locked to 8 stats or any particular id set. N&D's
  default 8 stats are in `constants.ND_DEFAULT_STATS`, but a `sheet_mode:
  "custom"` template can define entirely different ones.
- `currency_json`: `[{"label":"Creds","abbr":"CR","value":350}, ...]`
  — same idea, freeform currency types. Default in `constants.ND_DEFAULT_CURRENCY`.
- `skills_json`: exists on the model (`[{id,label,stat_id,value}]`) but is
  **not used anywhere in N&D** — legacy, don't bother setting it.

Other JSON fields, all `[...]` arrays of objects:
- `equipment_json`: `[{"name","qty","weight","equipped","notes"}]`
- `feats_json`: `[{"name","source","description"}]`
- `attacks_json`: `[{"name","bonus","damage","dmg_type","notes"}]`
- `cyberware_json`: `[{"name","ca_cost","notes"}]`
- `conditions_json`: `["Bleeding", "Poisoned", ...]` — a flat list of freeform
  condition-name strings, no fixed catalog

### Ownership

- `owner_user_id`: `NULL` = GM-managed (an NPC-as-PC, or pre-dates player
  accounts). Set automatically to the logged-in user's id when a non-GM
  player creates their character — **one character per player per world**;
  a second attempt 400s. As GM, every character you create has
  `owner_user_id = NULL` regardless.

### Routes

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/characters` | — | roster |
| GET | `/characters/new?template_id=...` | — | creation form; if the template's `sheet_mode` is `custom`, renders the custom-fields form directly, otherwise the full N&D wizard |
| POST | `/characters/new` | form-encoded (+ optional `portrait` file) | see field list above |
| GET | `/characters/{id}` | — | sheet view |
| GET | `/characters/{id}/edit` | — | edit form (custom-mode sheets redirect straight to the detail page — they're edited in place) |
| POST | `/characters/{id}/edit` | same fields as create | |
| POST | `/characters/{id}/delete` | — | |
| GET | `/characters/{id}/export.ndc` | — | export |
| GET | `/api/characters/catalog` | — | races/professions/feats/equipment catalog used by the creation wizard UI — informational only, `race_id`/`profession_id` aren't validated against it server-side |

### Live-update AJAX endpoints (JSON body, not form-encoded)

| Path | Body | Returns |
|---|---|---|
| `POST /api/characters/{id}/hp-async` | `{"action":"delta"\|"set"\|"temp"\|"max","value":N}` | `{current_hp, max_hp, temp_hp, death_success, death_failure, secondary_current}` |
| `POST /api/characters/{id}/shock` | `{"action":"delta"\|"set","value":N}` | `{shock_current, shock_max}` |
| `POST /api/characters/{id}/pp` | `{"action":"delta"\|"set"\|"rest","value":N}` | `{pp_current, pp_max}` (max is derived from physical stats) |
| `POST /api/characters/{id}/mp` | `{"action":"delta"\|"set"\|"rest","value":N}` | `{mp_current, mp_max}` (max derived from mental stats) |
| `POST /api/characters/{id}/xp` | `{"delta":N}` | `{xp, xp_lo, xp_hi, xp_pct}` |
| `POST /api/characters/roll` | `{"expr":"1d20+3"}` | dice roll result |

`hp-async`'s `"delta"` clamps to `[0, max_hp + temp_hp]`; `"max"` also
re-clamps `current_hp` down if needed. All of these require GM or the
character's own owning player.

### Worked example: a full N&D character

```bash
curl -s -b $COOKIE -X POST https://your-nd-world/characters/new \
  --data-urlencode "name=Nyx Kessler" \
  --data-urlencode "player_name=Alex" \
  --data-urlencode "race=Corp-Enhanced Human" \
  --data-urlencode "char_class=Fixer" \
  --data-urlencode "level=3" \
  --data-urlencode "xp=900" \
  --data-urlencode "backstory=Former corpo runner gone independent." \
  --data-urlencode "max_hp=24" \
  --data-urlencode "current_hp=24" \
  --data-urlencode "shock_max=8" \
  --data-urlencode "shock_current=8" \
  --data-urlencode "sheet_template_id=1" \
  --data-urlencode 'stats_json=[{"id":"str","label":"Strength","abbr":"STR","value":4},{"id":"dex","label":"Dexterity","abbr":"DEX","value":5},{"id":"bod","label":"Body","abbr":"BOD","value":3},{"id":"per","label":"Perception","abbr":"PER","value":3},{"id":"wil","label":"Willpower","abbr":"WIL","value":2},{"id":"int","label":"Intellect","abbr":"INT","value":2},{"id":"cha","label":"Charisma","abbr":"CHA","value":2},{"id":"itu","label":"Intuition","abbr":"ITU","value":2}]' \
  --data-urlencode 'currency_json=[{"label":"Creds","abbr":"CR","value":350},{"label":"Tokens","abbr":"TK","value":2}]' \
  --data-urlencode 'equipment_json=[{"name":"Monoblade","qty":1,"weight":1,"equipped":true,"notes":"concealed"}]'
# → redirects to /characters/{new id}
```
`sheet_template_id=1` is the built-in `nd-default` template — confirm the
actual id on your instance (`GET /api/characters/templates`) rather than
assuming.

---

## 4. Sheet Templates (character sheet systems)

A `SheetTemplate` defines either extra fields layered onto the standard N&D
sheet, or (in `custom` mode) an entirely different character sheet system.

| `sheet_mode` | Meaning |
|---|---|
| `"nd"` (default) | The full N&D sheet (8 stats, HP/Shock/PP/MP, edges, cyberware, feats, conditions) **plus** this template's `fields_json` layered on top |
| `"custom"` | The N&D sheet is not shown at all — the character *is* just this template's fields. Use this for a different game system entirely (this is how the bundled "Asterion" system works) |

### Field schema (`fields_json`) — same idea as EntityTemplate, one more type

```json
{"id":"grit","label":"Grit","type":"resource","section":"Core","default_value":"5"}
```
Types: `number` \| `resource` \| `text` \| `textarea` \| `table` \| `list`.
`resource`/`table` exist in the schema (per the model's own doc comment) for
a sheet-specific rendering treatment; `list` works identically to
`EntityTemplate`'s (`item_fields` sub-schema, same `custom_fields_json`
shape: `{field_id: [{sub_id: value}, ...]}`).

### Built-in templates

**`nd-default`** — `sheet_mode: "nd"`, `fields_json: []` (no extra fields;
it *is* the base N&D sheet).

**`asterion`** — `sheet_mode: "custom"`, a full alternate dice-pool system
(Spark Shield/Flesh/Ichor resources, Origin/Spark/Deed abilities, Drachma
currency, Glory/Domain Rank progression, session log, relationships —  see
`_ASTERION_FIELDS` in `app/database.py` for the complete field list if you
want a real-world example of a large custom-mode template).

### Routes

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/characters/templates` | — | list |
| GET | `/characters/templates/new` | — | create form |
| POST | `/characters/templates/new` | form: `name`, `description`, `sheet_mode` (`"nd"` or `"custom"`), `fields_json` | |
| GET | `/characters/templates/{id}/edit` | — | |
| POST | `/characters/templates/{id}/edit` | same fields | on a built-in, only `fields_json` applies |
| POST | `/characters/templates/{id}/delete` | — | 403 on built-ins |
| GET | `/api/characters/templates` | — | clean JSON list: `[{id, name, is_builtin, fields}]` — use this to look up ids/fields programmatically instead of scraping HTML |

### Worked example: a lightweight custom sheet

```bash
curl -s -b $COOKIE -X POST https://your-nd-world/characters/templates/new \
  --data-urlencode "name=Ganger" \
  --data-urlencode "description=Stripped-down gang-member sheet" \
  --data-urlencode "sheet_mode=custom" \
  --data-urlencode 'fields_json=[
    {"id":"grit","label":"Grit","type":"resource","section":"Core","default_value":"5"},
    {"id":"reputation","label":"Reputation","type":"number","section":"Core","default_value":"0"},
    {"id":"crew","label":"Crew Role","type":"text","section":"Core","default_value":""},
    {"id":"contacts","label":"Contacts","type":"list","section":"Network","item_fields":[
      {"id":"name","label":"Name","type":"text"},
      {"id":"notes","label":"Notes","type":"textarea"}
    ]}
  ]'
# → redirects to /characters/templates/{new id}/edit

# Then create a character using it:
curl -s -b $COOKIE -X POST https://your-nd-world/characters/new \
  --data-urlencode "name=Rook" \
  --data-urlencode "sheet_template_id={that id}" \
  --data-urlencode 'custom_fields_json={"grit":"4","reputation":"12","crew":"Muscle","contacts":[{"name":"Vex","notes":"fence, back-alley market"}]}'
```

---

## Gotchas summary

- **Entities vs. PlayerCharacters are two separate systems** with separate
  template tables (`EntityTemplate` vs `SheetTemplate`), separate routes,
  and independent id sequences — a template id from one means nothing to
  the other.
- **`PlayerCharacter`'s ability-score/currency/appearance/proficiency
  columns are dead** — only the fields listed in §3's field contract are
  ever written by the app. Use `stats_json`/`currency_json` for stats and
  money, not `str_score`/`currency_cp`.
- **All create/edit routes here are form-encoded** (`--data-urlencode` in
  curl, or `application/x-www-form-urlencoded`), not JSON — only the AJAX
  stat endpoints (`/api/characters/{id}/hp-async` etc.) and the
  entity-template-free `/api/characters/templates` read are JSON.
- **`custom_fields_json` values are strings**, even for `type: "number"`
  fields — don't send a raw JSON number.
- Built-in templates (`is_builtin: true`) can't be deleted and can't have
  their `name`/`description`/`kind`/`sheet_mode` changed — only their
  `fields_json`, which mutates the one shared global row (affects every
  world using it).
- Confirm ids on your actual instance (`GET /entity-templates`,
  `GET /api/characters/templates`) rather than assuming the built-ins are
  ids `1`/`2` — seed order and any templates created before yours shift them.
