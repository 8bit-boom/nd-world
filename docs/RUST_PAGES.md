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
| `GET /sessions`, `GET /quests` | batch 5: paging, party names; quests with search, category, party filter, hidden quests, sub-quest progress, status groups, edit rights - same HTML for three roles over 12 URLs |
| `GET /entity/<id>` (`entities/detail.html`) | batch 4: the entity page - markdown body with headings / sections / autolinks (names, aliases, derived short forms), `[gmonly]` / `[for]` / `[reveal]` for players, notes, template custom fields, connections and backlinks with visibility, a hidden-but-shared entity, the GM's `?as=player` preview, the AI panel's context and the world's AI instructions. Same HTML as Python for three roles and seven URLs; an HTML (rich-editor) note or a non-ASCII name in the autolink list leaves the page to Python. 46 -> 183 pages/s |
| `GET /login` (`auth/login.html`) | no database: the session cookie is read (signed in = 303 to `next`), `next` is clamped like `auth.safe_next_url`, the form is rendered. The POST (rate limiter, passwords, 2FA) stays in Python. |

## Differences between MiniJinja and Jinja2 that the work found (and how each is handled)

| | |
|---|---|
| All 128 templates **compile** in MiniJinja | `nd-render/tests/compat.rs` (compile only: a missing filter or a Python-only method fails at render time, not before) |
| Escaping: MiniJinja writes `&#x2f;` for `/` and `&quot;`, Jinja2 `/` and `&#34;` | same HTML, different bytes; `nd-render` installs Jinja2's (markupsafe's) escaping so pages compare byte for byte |
| `{{ none }}` / booleans print as `none` / `true` in MiniJinja, `None` / `True` in Jinja2 | not handled yet: each page that prints one needs a check; the golden test finds it |
| Python methods on values (`.items()`, `.get()`, `.startswith()`, `loop.changed`, ...) | MiniJinja has a `pycompat` add-on; not enabled yet, needed from the first page that uses them |
| 20 filters / globals written in Python (`md`, `thumb`, `display`, `wq`, `asset_v`, `can_edit`, `site_layout`, `view_spec`, `saved_view`, ...) | only `asset_v` is ported. `md` needs a markdown renderer that matches `markdown2` + extras + `nh3` sanitising |

## Why the other pages are not done yet (measured on the code)

135 `TemplateResponse` calls in 78 modules; 92 templates extend `base.html`. A signed-in page is not just its template: the layout around it
(`base.html`) takes its context from code that has to be ported first, and that code is where the permission rules are:

| Needed by every signed-in page | Python today | Size |
|---|---|---|
| Who is signed in, banned/stale-session checks, GM / assistant / world-owner role | `auth_gate`, `_auth_lookup`, `_active_membership`, `_is_player_safe` / `_is_assistant_safe` / `_is_owner_safe` (`main.py`) | the allowlists are the riskiest code in the app |
| The active world and the worlds this person may see | `deps.get_world_ctx`, `auth.accessible_world_ids` | small |
| Section access per world (`world_can_view_section`) | `deps.py` | the permission matrix |
| Navigation menus, custom kinds, theme | `nav_menus.py` (299 lines), `deps.effective_kinds`, `theme_presets.py` (367), world settings JSON | medium |
| Site layout and saved view layouts per person | `site_layout.py`, `view_prefs.py` (323), `UserViewPref` rows | medium |
| App settings flags | `database.get_app_settings_flags_cached` | small |

Then each page's own data and logic (entity lists with visibility filtering, `[gmonly]` stripping, rules rendering with 555 lines of its own
markdown handling, search, maps, calendar ...). The plan's estimate for this phase (8-12 weeks full time) stands; one page needs the whole
table above first, and every page after it is cheaper.

## Where to go next

1. Port the table above as a Rust `layout` module, with the Python code as the specification and a **permission matrix test** (every route x
   role x section access, run against both servers) written *before* the code.
2. First authenticated page: a small one (`/dice`: one table, `world_can_view_section`) in "shadow" mode - the gateway renders it, compares with
   Python's answer, sends Python's, and logs differences - before it is switched on.
3. Then the big read pages (entity lists, entity detail, rules, search), where the speed gain is.

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
