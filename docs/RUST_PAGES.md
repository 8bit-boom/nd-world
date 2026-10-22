# Pages built by the gateway (Rust)

Phase 2 of `docs/RUST_REWRITE_PLAN.md`. Which pages the gateway builds itself, how it is switched, and what stands between this and the other
~130 pages. The gateway (`docs/RUST_GATEWAY.md`) now has a `pages` module and a `nd-render` crate: MiniJinja over the app's own
`app/templates/`, so a template edit changes both servers.

## Switching

`ND_RUST_ROUTES` (comma separated, default `login`; empty = Python builds every page). A page answers only when the gateway is sure it
produces what Python would: it needs `SECRET_KEY` in its environment (to read the session cookie - without it every page goes to Python) and
a `GET`. Everything else, the POST of a form included, is passed on. Rollback of a page is removing its name and restarting; rollback of
all of it is `ND_RUST_ROUTES=` or `ND_GATEWAY=0`.

## Built so far

| Route | Notes |
|---|---|
| `GET /dice` (`dice.html` in `base.html`) | the first signed-in page; switched on with `pages` in `ND_RUST_ROUTES` (opt-in). Built from the layout context (below) plus the world's last 50 rolls. Same HTML as Python for a GM, a player and an assistant, with a custom kind, custom section access, a theme, a saved dice layout, `?embed=1`, `?error=`/`?n=`, `?w=` and `?q=` (`tests/test_gateway_golden.py`). 84 -> 237 pages/s, p50 165 -> 58 ms. |
| `/chronicler`, `/background-jobs`, `/tools/bulk-edit`, `/system`, `/boards` | batch 2: the layout plus a section check and at most one list - one row each in `SIMPLE` (`rust/nd-gateway/src/pages.rs`); same HTML or Python's refusal for three roles |
| `GET /kind/<kind>` (`entities/list.html`, `_card.html`) | batch 3: the entity lists - folders and parent folders, stat columns (`parse_stats`), visibility (hidden / shared entities), search, grid / table views and paging, thumbnails. Same HTML for a GM and two players over 17 URL variants. 69 -> 247 pages/s on a 55-entity folder |
| `/androidapp` | batch 3 |
| `GET /parties`, `GET /facts` | batch 6: party member counts with hidden companions, paging, create rights; facts grouped by session (unfiled first, newest session first), hidden facts, tag cloud - same HTML for three roles |
| `GET /tables` | batch 7: random tables grouped by category in query order (blank category → general), per-row edit rights (`can_edit_table`), create rights - same HTML for three roles. JSON maps keep insertion order (`preserve_order` in serde_json and MiniJinja) so dict order matches Python |
| `GET /combat` | batch 8: running fights first then finished, newest first, paging, combatant counts (non-object entries ignored, bad JSON counts 0), GM-only encounter templates, ruleset labels in Python's order (copied into app_data.json) - same HTML for GM and a player granted read |
| `GET /calendar` | batch 9: the month view with the calendar arithmetic ported (rust/nd-gateway/src/calendar.rs): month/year paging and clamping, weekday names, holidays, moon phases, events with linked labels, day icons, picker lists, manage/add rights. Answers only for a calendar in the stored shape the app writes; a world without a calendar row yet, odd JSON types, a query number Python's int() might read differently, or a confirmed session plan to show at the top stay with Python |
| 15 small pages (`SIMPLE` table) | batch 10: `/professions/new`, `/races/new`, `/boards/new`, `/characters/templates/new`, `/maps/schematic/new`, `/entity-templates/new`, `/tables/new` (needs tables edit), `/display` (GM), `/handouts`, `/dreamlands` and `/king-in-yellow` (only when the feature is on - the "disabled" page stays Python's), `/sessions/new`, `/quests/new` (needs quests edit), `/editor`, `/entity-templates`. Quest statuses/categories copied into app_data.json |
| `/tables/<id>/edit`, `/handout/<id>`, `/entity-templates/<id>/edit`, `/display/combat/<id>` | batch 11: `rust/nd-gateway/src/idpages.rs`, a table of pages addressed by a row id; the table edit check shared with `/tables`; the `display` filter (display copies of uploaded pictures) ported |
| `/characters/templates`, `/worlds`, `/worlds/<id>/kinds/edit`, `/worlds/<id>/rules/edit` | batch 12: `load_custom_kinds` ported; the rules editor only for a world with no overlay stored (one with an overlay shows Python's validation message); `fromjson` filter. Note: the context processor's world-aware `kinds` / `kind_icons` replace a handler's own (Starlette applies processors last), so the gateway keeps the layout's |
| `/pages/<id>`, `/export` | batch 13: the document viewer (hidden documents GM-only, other worlds' refused - Python's 404); the export hub unless a staged restore is waiting (its manifest shown: Python's) |
| `GET /facts/fragment` | batch 14: the closed groups of `/facts` on demand, from the same grouping code (keys: session ids and/or `none`; a key in another script's digits is Python's) |
| `GET /quests/<id>` | batch 15: the quest page for a quest of the viewer's active world (hidden quests GM-only; linked entities; edit rights by section and ownership); a quest of another world is Python's |
| `/pages`, `/pages/albums/<id>` | batch 16: `rust/nd-gateway/src/media.rs` - album trees and counts (`media_albums`) for the media libraries; the document library first (hidden documents and counts per viewer, sheet counts per template) |
| `/audio`, `/audio/albums/<id>`, `/video`, `/video/albums/<id>` | batch 17: one clip-library builder for both: hidden clips and counts per viewer, file size and date per clip (`_clip_facts`; a naive date read as UTC, like the image), the upload cap from Settings or the environment, the entity picker on the root page |
| `/images`, `/images/albums/<id>` | batch 18: `rust/nd-gateway/src/gallery.rs` - `discover_world_images` ported (portraits, markdown embeds in bodies / notes / backstories, session media, kept pictures, chosen titles), the first 200 cards plus the rest as JSON with thumbnails, album pages with names, file facts, sub-albums and move targets. `|thumb` now fails (page to Python) on a non-text value, as Python does |
| `/calendar/agenda`, `/calendar/year` | batch 19: the calendar loader shared by the month, agenda and year pages; agenda (up to 1,000 events and icons) and the year grid (holidays, event titles, icon days) |
| `/calendar/config` | batch 20: the calendar settings form (GM / assistant with calendar edit), presets cleaned in Python and copied into app_data.json |
| `/maps/new` | batch 21: the map form with the gallery picker (`all_world_image_urls`: pictures in use plus album pictures, by URL) |
| `/new` | batch 22: the empty entity form (members for the visibility list, folders of the kind, entity templates, the gallery picker). `kind_icons` / `subtypes` now keep Python's key order (recorded in app_data.json) |
| `/entity/<id>/edit` | batch 23: the entity form for an entity of the viewer's active world (players allowed to see a hidden one, custom fields, the gallery) |
| `/maps` | batch 24: `rust/nd-gateway/src/maps.rs` - the world's map files (JSON next to the database; one Python would read differently makes the page Python's), their pictures (bundled, then uploaded), schematics, the GM's gallery picker |
| `/maps/<slug>` | batch 25: the map viewer for a map of the active world - entity name links the viewer may see, schematics, parties with member counts and their pins, as Python's `json.dumps` writes them (`nd_render::py_dumps`); the tile pyramid's meta when one is already cut (none yet: Python queues one, so the page is Python's), the overlay row (a first visit creates it: Python's) |
| `/boards/<slug>`, `/boards/<slug>/notes` | batch 26: `rust/nd-gateway/src/boards.rs` - the canvas page (nodes, groups, edges and the entity list as Python's `json.dumps` writes them) and the reading list (titles, tags, links). JSON numbers now round-trip exactly (serde_json `float_roundtrip`) and floats print as Python's `repr` (differential test in tests/test_rust_layout.py). `/boards/<slug>/export` stays Python's: its template fills a dict with `.update()`, which MiniJinja cannot do |
| `GET /rules` | batch 27: `nd_render::rules` - ::: blocks (callouts, collapses, GM-only removed for players), statblocks, the rules.json overlay (renames, hidden sections, order, tabs) and a strict `html.unescape` (an entity it is not sure of makes the page Python's); differential tests in tests/test_rust_layout.py. The bundled `app/core_rules.md` gained blank lines before its `-----` rules and headings: markdown2 had been turning list lines into headings (bogus TOC entries) and nesting headings inside list items, and both renderers now give the intended page. Renderer: fenced code is a plain `<pre><code>` and table alignment reads `text-align:left;`, as markdown2 writes them |
| `GET /search` | batch 28: `rust/nd-gateway/src/search.rs` - entities by FTS5 with the same column weights (or the substring scan for a query without a 2-letter word), visibility and the secret-only-match filter for players, characters (own / party), quests and sessions (GM), entity notes, highlighted snippets. A database whose FTS index fails is Python's |
| `GET /` | batch 29: `rust/nd-gateway/src/home.rs` - the world dashboard: kind counts, recent entities, map previews, most-linked entities, the tag cloud, recent boards / schematics / sessions, active quests, the GM's home sections and pinned tiles (links resolved and filtered per viewer), hidden kinds |
| `GET /sessions`, `GET /quests` | batch 5: paging, party names; quests with search, category, party filter, hidden quests, sub-quest progress, status groups, edit rights - same HTML for three roles over 12 URLs |
| `GET /entity/<id>` (`entities/detail.html`) | batch 4: the entity page - markdown body with headings / sections / autolinks (names, aliases, derived short forms), `[gmonly]` / `[for]` / `[reveal]` for players, notes, template custom fields, connections and backlinks with visibility, a hidden-but-shared entity, the GM's `?as=player` preview, the AI panel's context and the world's AI instructions. Same HTML as Python for three roles and seven URLs; an HTML (rich-editor) note or a non-ASCII name in the autolink list leaves the page to Python. 46 -> 183 pages/s |
| `GET /login` (`auth/login.html`) | no database: the session cookie is read (signed in = 303 to `next`), `next` is clamped like `auth.safe_next_url`, the form is rendered. The POST (rate limiter, passwords, 2FA) stays in Python. |

## Differences between MiniJinja and Jinja2 that the work found (and how each is handled)

| | |
|---|---|
| All templates **compile** in MiniJinja | `nd-render/tests/compat.rs` (compile only: a missing filter or a Python-only method fails at render time, not before) |
| Escaping: MiniJinja writes `&#x2f;` for `/` and `&quot;`, Jinja2 `/` and `&#34;` | `nd-render` installs Jinja2's (markupsafe's) escaping so pages compare byte for byte |
| Python values: `None` / `True`, datetimes, dicts with int keys, dict order | `nd_render::pyvalue` (`{"__dt__"}`, `{"__intkeys__"}` markers); serde_json and MiniJinja keep insertion order (`preserve_order`); JSON numbers round-trip exactly (`float_roundtrip`) and floats print as Python's `repr` |
| Python methods on values (`.items()`, `.get()`, `.update()` ...) | `minijinja-contrib` `pycompat`; `dict.update()` cannot work on MiniJinja's immutable values, so `/boards/<slug>/export` (the one template that fills a dict that way) stays Python's |
| Filters / globals written in Python | ported: `md`, `md_for`, `body_summary`, `strip_md`, `entry_text`, `parse_stats`, `truncate`, `thumb`, `display`, `striptags`, `tojson`, `fromjson`, `urlencode`, `wq`, `asset_v`, `can_edit`, `can_edit_table`, `site_layout`, `saved_view`, `view_spec`. A filter that meets a value Python would treat differently fails, and the page goes to Python |
| Context processors run after the handler in Starlette | the layout's world-aware `kinds` / `kind_icons` win over a handler's own, as in Python |

## Phase 2 status: closed

The plan's phase 2 list (entity pages and lists, search, rules, maps viewer + tiles, calendar, facts, sessions list, home) is ported, plus
the other read-only pages in the table above - each with a golden test that compares the gateway's HTML with Python's for a GM and two
players (`tests/test_gateway_golden.py`) and, for ported logic, differential tests against the Python functions (`tests/test_rust_layout.py`).
Every page still answers only when sure: an unusual value, a row Python would create on the visit, a query parameter Python's `int()` reads
differently, a template feature MiniJinja lacks - the request goes to Python unchanged.

GET pages that stay Python's, and the phase they move with:

| Pages | Why later | Phase |
|---|---|---|
| `/characters`, `/characters/<id>`, `/characters/new`, `/characters/<id>/edit`, `/characters/templates/<id>/edit`, `/parties/<id>`, `/parties/<id>/roster`, `/parties/<id>/summary`, `/share/<token>`, `/pages/sheets`, `/pages/sheets/<id>` | built on the character-sheet engine (maxima, custom sheet systems, vitals) that the character edits port | 3 |
| `/sessions/<id>`, `/sessions/<id>`'s log pages (`/session-log`, `/session-log/<id>`), `/races`, `/professions`, `/worlds/<id>/edit`, `/worlds/<id>/home/edit`, `/worlds/<id>/notes/<user>`, `/worlds/<id>/export/split`, `/settings`, `/settings/visibility-rows`, `/account`, `/account/2fa/setup`, `/login/2fa`, `/join/<code>`, `/import`, `/media-rename`, `/tools/code-assist`, `/schedule` | forms and pages whose data comes from the write paths (recaps, catalog imports, account security, settings) | 3 |
| `/combat/<id>`, `/maps/schematic/<slug>`, `/maps/schematic/<slug>/view`, `/display/map/<slug>`, `/cockpit`, `/player-cockpit` | live table tools (fog, tokens, turn order) | 4 |
| `/ai`, `/ai-chat`, `/image-gen`, `/imagestudio`, `/studio`, `/npc-talk`, `/characters/ai-new`, `/characters/templates/ai-new` | AI and jobs | 5 |
| `/boards/<slug>/export` | `dict.update()` in its template (above) | 3, with the template reworked |

## The layout context (batch 1 of the full port)

Every signed-in page sits in `base.html`, which needs: the user and their session (`session_version`, the site layout the session carries), the
active world and the worlds the person may use, their role there, the route allowlist (`rust/nd-perm`), the world's theme, the navigation menus
(custom kinds, conditions, per-role section access), the hover-preview settings and the person's saved view layouts. `rust/nd-gateway/src/layout.rs`
builds all of it; `rust/nd-render/src/app.rs` holds the two pure pieces, checked against Python with random inputs (`tests/test_rust_layout.py`:
~7,000 layout choices, 1,200 navigation cases). The app's data tables (layout sections, nav catalog, kinds) are copied from Python by
`scripts/gen_rust_data.py` (`tests/test_rust_data.py` fails when stale). MiniJinja got Python's methods on values (`.get`, `.startswith`, ...),
Jinja2's exact `tojson` and `urlencode`, the `wq` filter and the per-page `site_layout` / `can_edit` / `saved_view` functions.

The gateway leaves a page to Python when: the session's site-layout copy is missing or older than 15 minutes (Python refreshes it in the cookie),
the cookie is older than 6 hours, the world shows its hero banner on every page (`_hero.html` is not ported), or anything else is unusual.

## Markdown (`|md`) - decided: the Rust (CommonMark) behaviour

`rust/nd-render/src/md.rs` is `render_md` with pulldown-cmark for the markdown step and a line-by-line port of everything after it (the app's own
tags, media titles, prose code blocks). `scripts/compare_markdown.py` feeds every markdown file of the repository (cut into sections, 984 texts)
to both: **73 % render the same**. The differences are markdown2's own: it turns underscores inside words into italics (`AI_ENTITY_GUIDE.md` ->
"AI*ENTITY*GUIDE.md"), puts no language class on fenced code, and reads some fenced blocks inside lists differently. So pages with written
content (entities, notes, facts, recaps, rules) would look different in places once Rust builds them - mostly better. The owner chose the Rust
behaviour (2026-10-21): `md` and `md_for` are now template filters on the Rust side. Pages that use them are compared with Python with that
difference allowed for; `tests/test_rust_layout.py` checks that the app's own tags (colours, marks, spoilers, [gmonly] / [for] / [reveal] boxes, media titles)
render exactly like Python's wherever the two markdown libraries agree.

Python dates in templates: database DATETIME columns reach templates as objects with Python's `strftime`, `isoformat`, `date()` and `str()`.

Batch 3 also ported the card helpers (`body_summary`, `entry_text`, `parse_stats` - checked against Python on 7,500 random and real bodies), Jinja2's
`truncate`, `thumb` (a bundled or map picture whose thumbnail Python would have to queue leaves the page to Python), `datetime.timestamp()`, and
the context processor's world-specific `kinds` / `kind_icons` / `subtypes`.

Batch 4 ported `autolink_entities`, `derive_name_variants`, `_rules_toc`, `split_rules_sections` (checked against Python with random names, prose and
headings) and markupsafe's `striptags`.
