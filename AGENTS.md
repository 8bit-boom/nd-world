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
  once against the same DB_PATH. `.github/workflows/docker-publish.yml`
  only builds/pushes the Docker image — it does not run this suite.

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
