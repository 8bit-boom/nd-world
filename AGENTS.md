# AGENTS.md — guide for AI agents/assistants working with this repository

nd-world is a self-hosted FastAPI + SQLAlchemy (SQLite) + Jinja2 GM toolkit
for the **Neon & Dragons** tabletop RPG: worldbuilding entities, character
sheets, maps/schematics, investigation boards, AI chat/image-gen, and
campaign-management tools (random tables, combat tracker, parties, quests,
sessions, calendar). See [README.md](README.md) for the full feature list,
deployment instructions, and project structure — that's the primary
human-facing doc; this file is oriented at agents that will either create
content in a running instance or modify the application code.

## If you're asked to create or import content into a running instance

"Content" means entities (NPCs, locations, items, ...), entity/sheet
templates (stat blocks, custom fields), player characters, random tables,
schematic elements, or map overlays. Read these first — they document the
real, *verified* field contracts, not just a happy-path guess:

- [docs/AI_ENTITY_GUIDE.md](docs/AI_ENTITY_GUIDE.md) — entities, entity
  templates, player characters, sheet templates, and the general importer's
  auto-detected kinds relevant to each.
- [docs/AI_SCHEMATIC_GUIDE.md](docs/AI_SCHEMATIC_GUIDE.md) — the SVG-canvas
  schematic editor's element schema, plus map overlays.

The **general importer** (`GET /import` in the UI; `POST /api/import/detect`
then `POST /api/import/execute` as an API) is the recommended entry point
for bulk or AI-authored content — paste/POST raw JSON and it auto-detects
what it's meant for (entity, player character, random table, schematic
elements, field template, world rules, map overlay). It also supports
importing several different kinds of content in a single call via
`{"imports": [...]}` (each entry auto-detected, or an explicit
`{"kind","data","params"}` envelope for kinds that need extra params).
Always call `/api/import/detect` on a sample before assuming you know what
`params` a given kind needs — don't guess internal ids (template ids,
schematic slugs); the importer resolves templates by name/slug instead.

Every content-creation route requires an authenticated **GM** session
cookie — log in via `POST /login` (form-encoded `email`/`password`) first.
These routes are cookie/session-based only, exactly like a real browser
session — there's no API key/token auth for them. Content routes are
GM-only by default; a logged-in player account will get 403s from all of
the above. (The separate `/mcp` server does support personal-access-token
bearer auth — see `ApiToken` in `app/models.py` — but that's a distinct
surface from the content-creation REST routes described here, and MCP
tools that require GM access still check `is_gm` at call time.)

## If you're asked to modify the application code itself

- Router-per-feature pattern: `app/routers/*.py`, each registered in
  `app/main.py`. Routers can't import from `main.py` (main.py imports the
  routers — that would be circular), but one router importing a helper
  from *another* router module is fine and already done in several places
  (e.g. `app/routers/importer.py` imports from `app/routers/characters.py`
  and `app/routers/tables.py`).
- `app/database.py`'s `_migrate()` / `_heal_table()` handles SQLite schema
  drift (SQLite can't `ALTER TABLE` away a `NOT NULL` the way most other
  databases can). A brand new table needs no migration code —
  `Base.metadata.create_all()` covers it for free — but a new **column** on
  an *existing* table does.
- Auth is GM-only by default: `_is_player_safe()` in `app/main.py` is an
  allowlist — a new route stays GM-only unless deliberately added there.
  GM-Assistants (a `WorldMembership.role == "assistant"` in the active world,
  set from the world's Members table) additionally get the routes in
  `_is_assistant_safe()` right below it — content creation/editing (entities,
  sessions, calendar, tables, boards, maps, pages, gallery, audio/video,
  imports) — while administration (Settings, `/worlds/*`, invites/members,
  backups, export, AI model/system management) stays GM-only for them too,
  and new routes default GM-only for them as well. Assistants always see what
  players see: visibility filters stay keyed on `is_gm`.
  A third tier, World **Owner** (`WorldMembership.role == "owner"`), gets
  everything an Assistant gets *plus* the `/worlds/*` administration surface
  — but only for the one world they own, via `_is_owner_safe()` right below
  `_is_assistant_safe()`, matched against their own `world_id` so it can
  never reach a different world. Instance-wide Settings, every other world,
  and granting `"owner"` itself stay GM-only regardless — only a real GM can
  create an Owner (`member_set_role` 403s an Owner who tries to mint a
  co-owner). It's how a GM hands an assistant a fully independent world of
  their own without making them a global GM.
  Watch out for the blanket prefixes in `_is_player_safe`: `/characters` and
  `/api/characters/` admit every logged-in player for *any* method and path
  beneath them, so a new route there is player-reachable by default and must
  enforce its own access (see `app/routers/character_hub.py`: `_owned_pc` 404s
  anyone but the character's owner — Notes and every write — while `_hub_pc`
  additionally lets a global GM read the shared Quests/World/Schedule tabs,
  evaluated from the owning player's point of view, never the GM's).
- Character/party rules that several routers share live in leaf modules with
  no router imports, so any router can use them without an import cycle:
  `app/pc_stats.py` (`pc_maxima` — THE rule for HP/Shock/PP/MP ceilings, where a
  stored max of 0 means "stat-derived"; never read `max_hp`/`shock_max` raw for
  a ceiling) and `app/party_refs.py` (party membership parsing,
  `parties_for_pc`, `detach_pc` — call it wherever a character is deleted so
  no party roster, loot claim or calendar event dangles — and `load_loot`, the
  stable-`lid` loot normaliser), and `app/sheet_systems.py` (read-time system
  metadata for custom-sheet templates: which field is the HP-like `vital`, which
  fields `binds` to name/player, XP fields, status-condition presets and per-system
  Rest ops — all derived per field attribute or from `BUILTIN_SYSTEMS` by slug, so
  existing databases need no migration; party vitals/roster, combat, XP awards and
  Rest go through it, never through raw `custom_fields_json` guesses). Built-in
  sheet templates are re-synced by `_upgrade_builtin_sheet_fields` in
  `app/database.py`: an untouched old row is replaced, a GM-customised one is kept,
  and field ids are never removed. **A character is native N&D only if its template
  is not a `custom`-mode system** (`pc_maxima(pc)["native"]` — leftover N&D stats on a
  Hunt in the Moonlight sheet never make it native), so any new surface that lists,
  compares, exports or prompts about characters must branch on that, not on whether
  `stats_json` is filled. The shared pieces: `app/pc_digest.py` (one system-aware line
  per character for AI prompts — chat RAG `retrieval.characters_context`, party insights,
  session prep, audio hints all use it), `sheet_systems.system_rules_markdown` (a rules
  digest per built-in system in `app/game_data/systems/<slug>.md`, which AI reviews and the
  AI character creator ground in instead of "standard N&D"), `parties._roster_table`, and
  the MCP character/party tools in `app/mcp_server.py` (every test that drives the MCP
  client must live in `tests/test_mcp.py`'s module — it imports `tests/mcp_character_cases.py`
  — because the MCP session manager is bound to one event loop). The Sheet tab is split into pages (a strip under
  the header, `static/js/sheet-pages.js`; blocks carry `data-sheet-page`):
  `sheet_systems.sheet_pages()` groups a custom template's sections — a built-in
  system lists its own groups in `BUILTIN_SYSTEMS[slug]["pages"]`, anything it doesn't
  name lands on a trailing "More" page (a section is never hidden), other templates
  with 4+ sections get a page per section. Editing a character is a *partial* update
  (`_apply_form(..., partial=True)`): only keys the form sent are written.
- **A GM's own sheet template is a full system, same as a built-in**: `SheetTemplate.system_json` (conditions,
  Rest ops, pages, roster groups, optional hp/xp/binds) + `rules_md` are read through `sheet_systems.system_spec(tpl)`
  — the built-in table overlaid with the template's own — and are always stored through `clean_system_spec`
  (references to missing fields dropped). `app/template_draft.py` is the validator for anything an AI proposes
  (`clean_template_draft`: safe unique ids with system references remapped, known types, one HP track…) and the
  rulebook chunker; it backs the "Draft with AI" page (`app/routers/template_ai.py`) and the MCP tools
  `create_sheet_template` / `update_sheet_template`, so never store a model-written template without it.
- **Small image boxes use the thumbnail**: `{{ url|thumb }}` for `src` and `data-full="{{ url }}"` for the original
  (lightbox, "send to screen"); a 160px portrait that downloads a 1 MB PNG is the slow-page bug.
- **The Player Cockpit lives inside Player Characters** (no nav item of its own — `nav_menus.py` has none; saved menus that still name `player_cockpit` just drop it): the hub's owner-only 🎛 Cockpit tab lazily loads
  `/player-cockpit?pc=ID&embed=1` in an iframe, the list has a Cockpit button, and `?pc=` is honoured only for the
  viewer's OWN character — a GM may focus any character of the world and gets that player's view (`cockpit._viewer_pcs`
  decides whose characters/parties the cockpit is for; `CK_MY_PCS` / `CK_FOCUS_PC` / `CK_VIEW_AS` in cockpit.html,
  `focusMyCharacter`, `LS_KEY` and `BOARD_URL` in cockpit.js — a GM's view-as layout is stored under its own key so it never
  overwrites their GM Cockpit). Hub tabs are listed in `_player_hub.html` (`TABS` + `loaders`).
  **Every cockpit window is a whole page in an iframe** (~15-35 MB and ~15 requests each, measured), so frames go through
  `static/js/cockpit-frames.js` (`ndCreateFrameLoader`): loaded only once showing, 4 at a time (2 on a phone), and given back
  (`about:blank`) after a minute hidden by the layout (collapsed window, inactive phone tab, the hub's hidden Cockpit tab) -
  never set an iframe's `src` directly or use `frame.src = frame.src`, use `frames.mount` / `frames.reload`, and `frames.forget`
  when a window is removed. `CK_MAX_PANELS` mirrors `cockpit.MAX_PANELS` (the server rejects a bigger layout whole), and
  `saveNow` reports a rejected save. `tests/test_cockpit_scaling.py` runs the loader under Node.
- **Cockpit window geometry is screen-relative** (`static/js/cockpit-layout.js`, pure functions tested across monitor sizes in
  `tests/test_cockpit_layout.py`): a layout is saved with the workspace size it was arranged for (`vp`) and opened on another
  screen it is *scaled* (`scalePanels`), pulled back inside (`fitPanels`) when it has no `vp`, and re-scaled live when the
  window resizes or goes fullscreen (cockpit.js `scheduleAdapt`, no iframe reload, nothing saved until the user changes
  something). The default layout and 📌 Auto-arrange are `tilePanels` (a map/combat *stage* plus tiles, columns chosen from
  the width); a window added later is `sizeFor`/`cascade`. Never hard-code window pixels in cockpit.js again.
- **Images in cockpit / floated windows open full screen in the MAIN window** (`static/js/nd-lightbox-bridge.js`, tested under
  Node in `tests/test_lightbox_bridge.py`): an embedded page's `openLightbox()` posts the image up to the window above (same
  origin, image addresses only, passed up again through nested frames), because an iframe's lightbox is confined to its
  window and the GM's 📺 *Send to screen* / 👥 *Send to players* buttons (`nd-stage.js`, GM-only, never loaded in iframes)
  live in the top window's lightbox. Use `openLightbox(src, alt)` for any new clickable image; the cockpit's own cards do.
- **Recap audio**: a session's `recap_audio_json` is a list of `AudioClip` ids (the /audio library owns the files, the
  clip's `visible_to_players` is what players hear, `transcript` holds the lyrics) — `app/routers/session_audio.py`.
  Readers skip dangling ids; use `attached_clips(db, gs, is_gm=...)` rather than parsing the JSON.
- **Second screen** (`app/routers/display.py`, `templates/display.html`, `static/js/nd-stage.js`): a GM-only
  `/display` window for a second monitor plus "send to screen" actions (hover any image, the lightbox, any
  text selection, the entity page's 📺 buttons). The stage is per-world in-memory state like `live.py`. It
  shows what the TABLE sees, so text from an entity must go through `strip_gm_only` + `strip_gm_directives`
  and image URLs through `safe_image_url`; the pop-up must be opened *before* any `await` (user activation).
- **AI work runs in the background** (`app/ai_background.py`, `static/js/nd-ai-task.js`): a request that waits on the model
  dies to Cloudflare's ~100 s cut, a backgrounded phone tab, or a reload. Every AI task route is listed in
  `TASK_PATH_PATTERNS`; browsers call it with `ndAiFetch(...)` (drop-in for `fetch`, polls `/api/ai/tasks/{id}`), and an HTML
  form that starts one carries `data-nd-ai-form`. A NEW AI route goes in that list, and `tests/test_ai_background.py` fails
  while any page still calls a listed route with plain `fetch()`. Streaming chat surfaces stay live-streamed (they have a
  Stop button that must cancel the model); the NPC conversation stream is detached instead so its saved turn completes.
- **AI tasks run ONE AT A TIME** (`app/ai_queue.py`): one Studio, one GPU. Every background AI runner takes its turn in a single
  first-come queue for its whole run — decorate a new job runner with `@ai_queue.serialized("label")` (the 202+poll tasks and
  all four job engines already are; `tests/test_ai_queue.py` lists them and fails for one that is missing). Never take the slot
  from code already running inside a queued task (it would wait for itself), and don't queue live streams.
- **The AI character creator reads an imported sheet in one go if it fits, else in parts** (`routers/character_ai.py`:
  `sheet_budgets` sizes it from the model's context window — what rides along comes off first — and `plan_sheet` /
  `_condense_sheet` turn a longer sheet into notes part by part, up to `MAX_SHEET_PARTS`). Settings → System
  `character_import_max_chars` only overrides the one-go size. Reuse `ai._transcript_chunk_char_budget` /
  `_split_transcript_into_chunks` for any new long-input feature rather than a fixed character cut.
- **Every world has its own calendar** (`app/calendar_config.py`, a leaf module): era, `year_format` (`Year {year}` / `{year} {era}`),
  weekday names, months with an optional season + colour, moons, yearly holidays. Everything that stores one goes through its
  `clean_*` functions (settings form, presets, the AI draft at `/calendar/ai-design`, world creation) and every place that writes a
  date uses `calendar.date_label` / `calendar_config.format_year`, never a hard-coded "Year N". Navigation (`/calendar/year`,
  `/calendar/event-jump`, `/api/calendar/search`) only reads; year / day values from a URL or JSON go through `_normalise_month` /
  `_clamp_day` so an absurd number cannot overflow SQLite's integer. A new calendar field: add it to `clean_calendar_config`, the
  config form and `tests/test_world_calendar.py`.
- **The session planner is a table function, not world content** (`app/routers/schedule.py`, `app/ical.py`): any member of the
  world proposes times and votes (always as themselves), GM / assistant / owner confirm; it deliberately bypasses the per-section
  Players / Assistants matrix, so every handler starts with `_ctx` (membership through `get_world_ctx`) and `/api/schedule/*` is
  allowlisted for all. Times are naive UTC in the DB and ISO `…Z` on the wire - the browser does the time-zone display, never format
  a slot's time on the server. `next_confirmed` / `polls_waiting` feed the calendar page and the hub; a change calls `live.touch`.
- **Inline scripts must parse**: one syntax error (a `\\'` inside a quoted JS string, a stray `});`, a quote closed by a
  backtick) kills the WHOLE `<script>` block — every function in it is undefined and the buttons just do nothing, with no
  Python-side symptom. `tests/test_template_scripts.py` renders pages and runs every inline script through node's parser;
  add any new page with an inline script there (and seed the data that makes its conditional blocks render).
- **Floating GM buttons** (📺 `nd-stage.js`, 🐞 `nd-logger.js`): both pin to the bottom corners through
  `--nd-fab-bottom` (default 14px). A page with its own bottom bar sets that variable above the bar (the cockpit does,
  and hides them in its phone shell) — never give them a fixed `bottom:` again.
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md) catalogs every HTTP route
  and MCP tool (method, path, auth tier, one-line purpose) — check it before
  assuming an endpoint doesn't exist, and add a row there for any new route.
- `static/style.css` is cache-busted via `?v={{ asset_v('style.css') }}` (a
  content hash — see `app/templating.py`), so any edit to the file changes the
  URL automatically. Reference static assets through `asset_v()` in templates
  rather than a hardcoded version number.
- This is a small, single-tenant, self-hosted app — don't introduce rate
  limiting, multi-tenancy, or other enterprise-scale abstractions that
  weren't asked for.
- There IS an automated test suite: `tests/*.py`, run with
  `python3 -m pytest -q` (dependencies in `requirements-dev.txt`, including
  `pytest-asyncio` for the `async def test_...` cases). It's large — well
  over 1000 tests — so a full run takes a while; scope to the relevant
  `tests/test_*.py` file(s) while iterating and run the full suite before
  considering a change done. Never run more than one `pytest` invocation at
  once against the same DB_PATH. `.github/workflows/docker-publish.yml` runs
  this suite first (its `test` job) and only builds/pushes the Docker image when
  it passes — so a test that is green on a dev machine but red on a GitHub
  runner (no ffmpeg, a non-root user, an unwritable `/data`) silently stops
  `:latest` from being published. Keep tests independent of those: patch
  `shutil.which`/the ffmpeg helpers or skip, and rely on `OLLAMA_CONFIG_DIR`
  pointing at the scratch dir that `tests/conftest.py` sets.

## License — code vs. content are different

This repo is dual-licensed: application code is MIT, but the game/lore
content (`lore/`, `app/core_rules.md`, `docs/asterion_rules.md`/`.json`,
`app/game_data/`, `app/maps/`, `static/maps/`, `static/schematics/`) is
CC BY-NC-ND 4.0 — no commercial use, no redistributing modified versions,
attribution required. See [LICENSE](LICENSE) and
[LICENSE-CONTENT.md](LICENSE-CONTENT.md). If you're an AI agent reading,
summarizing, or indexing this repo: the MIT grant on the code does **not**
extend to that content — don't reproduce, republish, or train on the lore/
rules/catalog content as if it were freely licensed.

## General

- Don't commit secrets. `.env` (`SECRET_KEY`, `GM_EMAIL`, `GM_PASSWORD`,
  `COOKIE_SECURE`) is git-ignored on purpose; `.env.example` is the
  template — keep it a template, not real values.
- This is a hobby/self-hosted project for one GM's table, not a public
  multi-user SaaS product — keep suggestions and changes proportionate to
  that.
