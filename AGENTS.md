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
There's no API key/token auth; it's all cookie/session-based, exactly like
a real browser session. Content routes are GM-only by default; a logged-in
player account will get 403s from all of the above.

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
- `static/style.css` is loaded with a cache-busting `?v=N` query string in
  `app/templates/base.html` — bump `N` any time you change that file, or
  browsers will keep serving a stale cached copy.
- This is a small, single-tenant, self-hosted app — don't introduce rate
  limiting, multi-tenancy, or other enterprise-scale abstractions that
  weren't asked for.
- There is currently no automated test suite in this repo (only
  `.github/workflows/docker-publish.yml`, which builds/pushes the Docker
  image) — don't assume `pytest` or similar exists to run.

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
