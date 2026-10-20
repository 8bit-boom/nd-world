# Optional layouts for all of nd-world - plan

Status: phases 1 (the site shell, on the Account page), 2 (the entity page) and 3 (dice, random tables, combat tracker) and 4 (the home page) are built; the other phases follow in the order of section 6. It continues `docs/VIEW_LAYOUTS_PLAN.md` (list pages) and
`docs/HUB_COCKPIT_LAYOUTS_PLAN.md` (character page, GM Cockpit), which are done. Written from the code as of `e4a1819`.
Known setup: the GM uses a laptop (1280-1536 px), players use phones; every layout is the viewer's own choice, saved to their account.

## 1. What already exists (the parts this plan reuses)

| Piece | Where | What it gives |
|---|---|---|
| Layout registry | `app/view_prefs.py` (`SECTIONS`, `spec`, `clean`) | One list of what a section offers; the server validates every saved choice |
| Account storage | `UserViewPref`, `GET/POST /api/view-prefs[/{section}]` | Per-person, follows them across devices; player-safe |
| Collection engine | `_views.html` `view_bar`, `static/js/nd-views.js` | Layout / size / sort / group / filter for any list of `.nd-item`s |
| Layouts already shipped | images, album, audio, video, entities, notes, entity notes, connections, quests, sessions, session log, characters, board notes, hub (Tabs / Play / Dashboard / Split), cockpit (Free / Stage + rail / Tabs / Grid, scenes) | |
| Site-wide look | `base.html`: theme (Dark / Dim / Light), UI scale (`data-scale`), phone class `.mp` | Per browser (localStorage) only |
| Navigation | `app/nav_menus.py` (GM arranges which items go in which dropdown) | One top bar, a hamburger on a phone |

Gaps: the **site shell** (navigation, width, density) has one arrangement; theme / scale are per browser, not per account; most **detail
pages** (entity, rules, session, party, combat, map, settings) and several **lists** (maps, tables, facts, search, pages, handouts, boards,
worlds, combat, parties) have a single fixed layout.

## 2. Layer 1 - the site shell (every page, per person)

New registry section `site` (account-saved like the others; the browser keeps a copy so the first paint is right).

| Option | Choices | Why |
|---|---|---|
| **Navigation** | **Top bar** (today) · **Side rail** (the same menus as a left column, collapsible to icons; the laptop gets its height back) · **Compact top** (icons only, labels on hover) · on a phone: **Bottom bar** (4 pinned destinations + More, like the character page) or **Hamburger** (today) | The GM's laptop is short; a phone thumb reaches the bottom |
| **Pinned destinations** | Up to 4-6 nav items of your choice shown first (bottom bar on a phone, top of the rail) | Players use 3-4 pages all night |
| **Content width** | **Centered** (today, ~1100 px) · **Wide** (fills the screen) · **Reading** (~760 px for long text) | Wide screens waste half the page; long lore reads better narrow |
| **Density** | **Comfortable** (today) · **Compact** (tighter padding / rows, more on screen) · **Touch** (44-48 px targets everywhere) | Prep on a laptop vs. a phone at the table |
| **Theme / scale** | Dark · Dim · Light (+ a High contrast option) · 90-150 % | Exists - moved from per-browser to the account (browser still overrides) |
| **Motion** | Full · Reduced | Respects `prefers-reduced-motion` by default |

Also: a **Settings → Layouts** tab listing every section with its current choice, a preview thumbnail and *Reset*, plus *Reset all*.

## 3. Layer 2 - page families (each an optional switcher on that page)

`(today)` marks the current layout, which always stays the default.

### Home / world dashboard (`/`)
- **Classic** (today) - hero, welcome, quick links, AI summary, recent.
- **Widgets** - the same blocks as cards the person reorders and hides (drag, or ↑↓ on a phone).
- **Session night** - next game night, party vitals, active quests, last recap, dice - the table's front page.
- **Focus** - quick links + search + recent only.

### Entity page (`/entity/<id>`) - the most visited page
- **Classic long page** (today).
- **Tabbed** - Overview · Notes · Connections · Media · Fields (custom fields) as tabs; remembers the last tab per kind.
- **Two-column** - the text on the left, a sticky sidebar with picture, fields, tags, connections web and media on the right.
- **Reader** - prose only (Reading width, TOC on the left), everything else behind a drawer.
- Phone: Tabbed is the natural default; Classic stays available.

### Rules (`/rules`) and Pages / page viewer
- **Scroll + TOC** (today) · **Book** (one section per screen, ← → and swipe, progress) · **Two-column** (sticky TOC, wide text) · **Search-first** (a filter box that collapses non-matching sections).

### Facts (`/facts`)
- **By session** (today, accordions) · **Timeline** (one line per fact, by date) · **Table** (sortable: session, entity, tag, visibility) · **Cards grouped by entity / tag**.

### Search (`/search`)
- **List** (today) · **Grouped by kind** (Characters, Locations, Notes, Pages…) · **Cards with pictures**.

### Sessions (GM detail `/sessions/<id>`) and Session Log reader
- GM page: **Long page** (today) · **Tabs** (Prep · Recap · Media · Transcript · AI) · **Run mode** (prep checklist + NPCs + loot on one screen for the night).
- Recap reader (players): **Full text** (today) · **Sections** (H2/H3 become an outline you open one at a time) · **Cards** (pictures first).

### Combat tracker (`/combat/<id>`) and combat list
- Tracker: **Standard** (today) · **Compact rows** (one line per combatant, laptop) · **Cards** (portrait tiles) · **Big screen** (turn order + current actor, readable across the table) · phone **Turn view** (only who acts now and next, big buttons).
- List: cards / table, group by running / finished / templates.

### Maps (`/maps`) and map viewer
- List: **Grid of thumbnails** · **List** · **Large previews**.
- Viewer: **Full screen** (today) · **With side panel** (pins / legend / search list beside the map) · **Mini-map** corner.

### Calendar (`/calendar`) and Session Schedule
- Month / Year / Agenda exist → add **Week** and **Eras timeline**; remember the last view per person; schedule: **Calendar** · **List of proposals**.

### Random tables (`/tables`)
- **Cards** (today) · **List** · **Roll board** (each table one big roll button, results stack in a side log).

### Parties
- List: cards / table. Party page: **Long page** (today) · **Dashboard** (vitals, loot, XP, goals as tiles) · **Roster table**.

### Boards (`/boards`, board canvas)
- List: cards / list. Board: **Canvas** (today) · **Notes** (built) · **Outline** (nodes as a tree by connections) · **Timeline** (event nodes by date).

### Dice, AI chat, NPC talk
- Dice: **3D tray** (today) · **Quick buttons** (d4-d100 + saved formulas, no animation) · **History-first**.
- AI chat / NPC talk: **Chat** (today) · **Chat + context panel** (pinned entities, sources) · **Compact** (phone).

### Smaller lists (reuse the collection engine as-is)
Pages library, handouts gallery (grid / masonry / list - the image layouts), worlds (cards / list), entity templates, character sheet
templates, background jobs (table / compact), races / professions catalogs (cards / table / compact).

### Settings (`/settings`)
- **Tabs** (today) · **One page with a search box and a sticky TOC** (find a setting by name).

## 4. Layer 3 - presets (one click sets several choices)

| Preset | Sets |
|---|---|
| **Table night (GM laptop)** | Side rail nav, Compact density, cockpit Stage + rail, combat Compact rows |
| **Prep (GM)** | Wide content, entity Two-column, Facts Table |
| **Player phone** | Bottom bar nav, Touch density, character page Play, entity Tabbed, recap Sections |
| **Reading** | Reading width, entity Reader, Rules Book |

Optional later: **world defaults** - the GM picks a preset as the starting point for new players in a world (each player can still change it).

## 5. How it would be built

1. **Registry**: extend `view_prefs` with *page sections* (modes + a small validated `options` dict, e.g. pinned nav ids, width,
   density) - same `clean()` guarantee, same API; collections keep `nd-views.js`.
2. **One page-layout helper** (`static/js/nd-layout.js`): applies `<html data-layout-<section>="…">` before first paint (from the
   account value rendered into the page, then the browser copy), offers a small switcher, saves through `/api/view-prefs`; layouts are
   CSS keyed on those attributes wherever possible, JS only where behaviour changes (tabs, book paging, swipe).
3. **One query per request** for saved layouts (load all of the viewer's rows once into `request.state`, not one per switcher).
4. **No server-rendered content changes per layout** - the same HTML, so hidden / GM-only rules stay exactly as they are and a layout
   can never show a player something new.
5. **Tests per section**: registry + `clean`, pure helpers under Node, page wiring for GM and player, and a Chromium matrix
   (1900 / 1600 / 1280 / 390 px): no sideways overflow, controls reachable, the choice survives a reload.

## 6. Suggested order (given a laptop GM and phone players)

| Phase | Scope | Size |
|---|---|---|
| 1 | Site shell: Navigation (side rail, phone bottom bar + pins), Width, Density, account-saved theme/scale, Settings → Layouts tab | 2 sessions |
| 2 | Entity page: Tabbed / Two-column / Reader | 1-2 sessions |
| 3 | Table-night tools: combat tracker layouts, dice Quick buttons, tables Roll board | 2 sessions |
| 4 | Home: Widgets / Session night / Focus | 1 session |
| 5 | Reading: Rules Book / Two-column / Search-first, recap Sections, Facts Timeline / Table, Search grouped | 2 sessions |
| 6 | Maps list + viewer side panel, calendar Week / Eras, parties dashboard, boards Outline / Timeline, AI chat panel | 2-3 sessions |
| 7 | Smaller lists on the collection engine, Settings one-page, presets, world defaults | 1-2 sessions |

## 7. Questions that change the plan
1. Should the **site navigation** be part of this (side rail / bottom bar), or keep today's top bar and only add page layouts?
2. **World defaults**: may the GM set a starting layout for players, or is it strictly each person's own?
3. Which phase first - the shell (1), the entity page (2), or the table-night tools (3)?
4. Anything to leave out (e.g. Dreamlands / King in Yellow / Studio Console pages keep one layout)?
