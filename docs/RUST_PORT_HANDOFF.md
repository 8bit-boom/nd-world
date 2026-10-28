# Rust port: where it stands and what is left (handoff)

A self-contained brief for whoever (person or model) continues the port of nd-world from Python (FastAPI) to the Rust gateway.
Read this first, then `docs/RUST_SWITCH_OFF.md` (the phase 6 work list and what is done route by route) and the per-area notes
(`docs/RUST_GATEWAY.md`, `RUST_PAGES.md`, `RUST_WRITES.md`, `RUST_LIVE.md`, `RUST_AI.md`, `RUST_JOBS.md`, `RUST_MCP.md`,
`RUST_PERMISSIONS.md`, `RUST_FANS.md`).

## How the system works today

- **Two processes.** The Rust gateway (`rust/nd-gateway`, Axum) listens on the public port; Python (uvicorn, `app/`) runs behind it
  on 127.0.0.1. `docker/entrypoint.sh` starts both. Every request reaches the gateway first.
- **Answer or decline.** For each route the gateway has ported, a handler either answers (sets the header
  `x-nd-served-by: gateway`) or returns `None`, and then the request is forwarded to Python unchanged. A handler returns `None`
  whenever it is not *sure* its answer is byte-for-byte what Python would send: odd data, an edge case not ported yet, a refusal it
  cannot phrase exactly. **Declining is always safe; a wrong answer is a bug.**
- **Same database.** Both sides read and write the same SQLite file. The gateway writes rows the way SQLAlchemy would (`orm.rs`:
  `insert` fills the model's Python-side defaults, `update` writes only changed columns and bumps `updated_at`, `touch` signals the
  live view).
- **Same templates.** Pages render the same Jinja templates from `app/templates` with MiniJinja (`nd-render` crate). Python template
  globals/filters are re-implemented in `rust/nd-render/src/lib.rs`. App data tables Python keeps in code (layout sections, kinds,
  presets...) are exported to `rust/nd-render/data/app_data.json` by `scripts/gen_rust_data.py`; **regenerate it** (`python3
  scripts/gen_rust_data.py`) when you add a key, and commit it.
- **Route switches.** `ND_RUST_ROUTES` (comma list: `login,dice_roll,live,pages,writes,ai,mcp,fans,auth`) turns groups on. All on
  in production and in the test suites.

### Crates and main files

| Path | What it holds |
|---|---|
| `rust/nd-gateway/src/lib.rs` | the dispatcher: which paths go to which module, in order (first match wins; a branch that reads the body must forward itself when it declines) |
| `layout.rs` | `build()`: the page context every page needs (session check, user, active world, role, nav, site layout). Declines when the session needs Python (stale layout etc.) |
| `pages.rs`, `idpages.rs`, `catalog.rs`, `account.rs`, `settings.rs`, `home.rs`, `calendar.rs`, `schedule.rs`, `hub.rs`, `boards.rs`, `maps.rs`, `media.rs`, `gallery.rs`, `search.rs`, `charsheets.rs`, `export.rs` | pages and read APIs |
| `edits.rs` (big), `writes.rs`, `uploads.rs`, `upload_files.rs` | write routes (form posts and JSON APIs), uploads, `/uploads/` access |
| `apis.rs` | assorted GET/POST JSON APIs (`is_get`/`get`, `is_post`/`post`) |
| `auth.rs`, `session.rs`, `qr.rs` | sign-in, 2FA, the session cookie (Starlette-compatible signing), the QR code (module for module Python's `qrcode`) |
| `ai.rs`, `ai_more.rs`, `ai_runners.rs`, `jobs.rs`, `runners.rs`, `tasks.rs`, `stt.rs`, `tts.rs`, `retrieval.rs`, `template_draft.rs`, `media_rename.rs` | AI chat/streams, background jobs, RAG retrieval |
| `live.rs`, `rooms.rs` | live updates (SSE) |
| `mcp.rs` | the MCP server |
| `tuning.rs`, `fans.rs` | Ollama tuning, hardware, fan control |
| `rust/nd-render` | templates, markdown (`md.rs`, pulldown-cmark; whitespace may differ from markdown2 and tests normalise it), `py_dumps` / `py_dumps_indent_as` (Python `json.dumps` byte-compatible), `pyvalue.rs` (Python-like values incl. datetimes), `rules.rs`, `text.rs` (`py_strip`, `strip_md`, ...) |
| `rust/nd-perm` | permissions: section access matrix, `[gmonly]` stripping, the path gate |

### Conventions that keep answers identical

- Python's error body is `{"detail": "..."}`: use `crate::ai_more::detail(status, msg)` for a message, `crate::idpages::refusal(code)`
  for a bare `HTTPException(code)` (phrase: "Not Found", "Forbidden"...).
- JSON out: `crate::edits::json_response(&value)`; text Python stores with `json.dumps` must be written with `nd_render::py_dumps`
  (`", "` / `": "` separators, ASCII escapes); `indent=2` files with `py_dumps_indent` / `py_dumps_indent_as(v, 2, ensure_ascii)`.
- DATETIME columns come out of `layout::row_map` as `{"__dt__": "<text>"}`; templates see a Python-like datetime; JSON APIs use
  `edits::py_isoformat_pub` or `PyDateTime::parse(..).isoformat()/strftime()`.
- Dicts with int keys passed to templates: `json!({"__intkeys__": map})`.
- Visibility for a non-GM: `visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?)`
  (= `deps.filter_visible_entities`). `[gmonly]` text: `nd_perm::gmonly::strip_gm_only(text, viewer, now_minute)`.
- Request bodies: `crate::edits::json_body_refusal` / `json_body_refusal_as` answer Python's "Invalid JSON" 400s and decline bodies
  serde reads differently from `json.loads` (NaN, lone surrogates, BOM, out-of-range numbers, deep nesting, UTF-16). Forms:
  `crate::ai::small_form(headers, body)`.
- Anything beyond ASCII where Python's `str.lower()`, `isalnum()`, `capitalize()`, `title()` differ from Rust's: decline.
- Pydantic coercions (a string where an int is expected, etc.) are Python's 422s: decline.

## How to work (the loop that has been used for every batch)

1. Pick a route from `docs/census/python_only.txt` (busiest first) or a mixed route from `docs/census/mixed.txt`.
2. Read the Python handler and every helper it calls. Port it into the right module; wire it in `lib.rs`.
3. Write a **parity test** in `tests/test_gateway_golden.py`: run the same request against Python directly (`ND_GOLDEN_DIRECT_URL`)
   and through the gateway, assert `x-nd-served-by: gateway`, and compare status + body (pages via `_same_page`, which normalises
   HTML; writes by running on Python, putting the DB back, running through the gateway, and comparing the DB state). Cover refusals.
4. Run the dev servers + the test: build `cargo build --release -p nd-gateway`, start Python (`python -m tests.serve --port 8951`) and
   the gateway pointed at it (see `scripts/gateway_suite.sh` for the env), then
   `ND_GOLDEN_DIRECT_URL=http://127.0.0.1:8951 ND_TEST_BASE_URL=http://127.0.0.1:8952 pytest tests/test_gateway_golden.py -k <name>`.
   Tests against one pair of dev servers share one database: do not use `-n` there.
5. Full suites before pushing: `cargo test`, `pytest tests -n 4 --dist loadgroup` (in-process Python), and
   `scripts/gateway_suite.sh 4 all` (the whole suite through the gateway, 4 shards). Push only when all pass, then check CI
   (`.github/workflows/tests.yml`, docker publish).
6. Update `docs/RUST_SWITCH_OFF.md` ("Done so far") and regenerate the census:
   `python3 scripts/census.py --write tests/_data/shard*/forwarded.log` (the logs append across runs: delete them before a run for
   exact counts).
7. Tests that reach into Python's own process (monkeypatching, in-memory caches) cannot pass through the gateway: list them in
   `tests/inprocess_only.txt`.

## Progress

Census from the latest gateway suite (one full run, split into groups on one machine - scripts/run_group.sh):
(The census counts of the latest run are inflated - the gates script deleted the wrong shard logs, so several runs
accumulated; delete tests/_data/shard*/forwarded.log before the next full run for exact numbers.) Phase 6 steps:

| Step | State |
|---|---|
| 1. Authentication (login, 2FA, sign-out, sessions) | done; 2FA setup page and confirm done; password change / 2FA disable / backup-code regenerate wait for the switch (below) |
| 2. Busiest Python-only routes | in progress (see "Left" below) |
| 3. Mixed routes: refusals / edge cases | started: character quick-edits, party stash (incl. give), saved views, conditions, currency |
| 4. Migrations, DB healing, seeding, background loops | not started |
| 5. Dockerfile without Python | not started |

Ported in the latest batches (details in `RUST_SWITCH_OFF.md`): settings page, session planner, character hub tabs (quests, world,
now, schedule, log, notes, keepsakes, places, handouts), uploads access, party pages (incl. the Rest undo), calendar search,
race/profession catalogs, account page and 2FA setup, name / token routes, world-context smart + player, Studio prefs, entity
markdown download, Session Log list, NPC talk page, world JSON exports, character sheets, the app-settings row creation, the
player cockpit's live board, the character read APIs (vitals / sync / advance / ability cards) and .json / .md exports, the
character creation and edit forms (with the `tpl_fields_json` filter and Jinja2's `e`), the session page's npc-candidates,
AI attachment background jobs, the session summary / transcript downloads, the view-prefs site save (with the session
re-sign, `auth.rs resign`), the unknown map / board slug 404s, the AI character creator page, a new schematic (GET and
POST), the hover-preview config, the world book zip, the instance backup zip, the rules-and-notes export, and the
per-kind entity zips (`entity_markdown` shared by the single-entity download), and the importer's execution
(`POST /api/import/execute`: detect's own branches + map_overlay, schematic_elements, field_template, player_character
and player_character_bulk - `_normalize_pc_data`'s fuzzy key-fold, `_resolve_sheet_template`, the upsert-by-name through
the ported `_apply_form`; the batch envelope still Python's), and the world JSON restore (`POST /worlds/<id>/import`: the embedded data-URI
image written back under a fresh uploads name, the upsert by (name, kind) with every field set outright - absent key
NULL, explicit "" empty - the redirect counting created and updated). The zip exports' parity rule (Python's zips are
timestamp-dependent): compare the extracted entries - files byte for byte, the backup's database by its logical dump,
the book's index.html with the owner-chosen markdown whitespace collapsed. The account-security routes are the first
switch-off moves: the password change and 2FA disable / backup-code regenerate run on the gateway's own lockout
counter (Python's in-memory one is replaced), and the database boot (startup.rs) lets a gateway-only container create
the database from the dumped schema snapshot plus _seed rows.

## The dev environment: WSL, not Docker

Docker Desktop is gone from this machine; the dev loop runs in **WSL (Ubuntu-24.04, root)**. One-time setup is in
`.zcode/dev/wsl_setup2.sh` (rustup at /root/.cargo, the venv at /root/venv, node included). The loop:

- `wsl -d Ubuntu-24.04 -u root bash /mnt/f/GLM/nd-world/.zcode/dev/wsl_build.sh` - builds with
  `CARGO_TARGET_DIR=/root/target` (fast native fs) and copies the binary into the repo's `rust/target/release/` (the
  suite script expects it there).
- `.zcode/dev/wsl_pair.sh` - the dev pair (Python 9031, gateway 9051, data /root/devdata), like gateway_suite.sh's env.
- `.zcode/dev/wsl_run.sh '<-k filter>' [file...]` - pair + pytest + teardown in ONE wsl invocation (background
  processes die with the wsl session that started them - never split pair and tests across invocations).
- `.zcode/dev/wsl_gates.sh` - cargo test + the whole suite through the gateway (4 shards). Use script files for anything
  with `$VARS`: the Git Bash layer expands them before wsl.exe sees them.

Run all WSL commands with `MSYS_NO_PATHCONV=1` (Git Bash mangles `/mnt/...` paths otherwise). The pair and gates
scripts pin `TZ=UTC` - both servers must share a clock because Python reads naive datetimes as local time (a mixed
TZ showed up as data-date diffs on every entity card). Node 22 is installed (`/root/node22`, symlinked as `node`) -
the minifier test needs ESM auto-detection; WSL's stock node 18 rejects the minified ESM in a .js file. The census after the
latest suite: `wsl ... /root/venv/bin/python scripts/census.py --write tests/_data/shard*/forwarded.log`.

**Already ported since the census was taken (ignore in the list)**: the above plus `/api/view-prefs/site` and the unknown
`view-prefs` sections; the hub companions and live-audio reads; `POST /worlds/<id>/import`; `POST /api/import/detect`
and `POST /api/import/execute` (all branches but the batch envelope). Two mixed-route systematic declines are also
closed: a tables edit with unparseable entries JSON now resets to "[]" and answers 303 as Python does (a NaN payload
still reaches Python - its loads accepts it), and a party edit by a viewer with no edit rights answers the plain 403.

## Left to port

### Python-only routes (busiest first; full list `docs/census/python_only.txt`)

Already ported since the census was taken (ignore in the list): `/pages/sheets*`, `/npc-talk`, `/session-log`, `/worlds/{}/export`,
`GET /api/media-rename/runs/{}`, `GET /api/sessions/ai/audio-jobs/{}`, `GET /api/ai/imagegen/player/jobs/{}`, `POST /logout`.

- AI attachments: `POST /api/ai/attachments/upload`, `/upload/chunk`, `/upload/complete`, `/api/ai/attachments/audio-jobs*`
- Image generation for players: `POST /api/ai/imagegen/player/generate`, `/api/ai/imagegen/jobs/{}`, `/model-templates`, `GET /image-gen`
- `GET /api/cockpit/player-board`
- `POST /api/parties/{}/rest/undo`
- `POST /api/view-prefs/site`, `DELETE /api/view-prefs/<unknown>` (404 answer)
- Character pages and APIs: `GET /characters/new`, `/characters/ai-new`, `/characters/{}/edit`, `/characters/{}/export.json|.md`,
  `GET /api/characters/{}/advance`, `/ability-cards`, `/sync`, `/vitals`, `POST /api/characters/{}/roll`,
  hub tabs `keepsakes`, `places`, `handouts`
- Sessions: `GET /sessions/{}/npc-candidates`, `/summary.md`, `/transcript.md`, `/api/sessions/{}/live-audio`, `/live-audio/download`
- Exports / backups: `GET /export/book.zip`, `/export/rules-and-notes.md`, `/admin/backup.zip`, `/kind/<kind>/download.zip`, `/download-selected.zip`
- Import: `POST /api/import/detect`, `POST /api/import/execute` (large), `POST /worlds/{}/import`
- Maps: `POST /maps/schematic/new`, `GET /maps/<slug>` (incl. unknown slugs -> 404)
- `GET /api/ai/world-summary`, `POST /api/ai/entity-context`, `GET /tools/code-assist`, `GET /boards/<unknown>/notes`

### Mixed routes (gateway answers the common case; Python still answers the rest)

Busiest in `docs/census/mixed.txt` (`python/gateway` counts). Typical remaining cases are refusals (404/403/400 with Python's
message), pydantic coercions, and data the port declines on. Ported through since that list was written: the read-view
markup refusals (422s verbatim), the character quick edits' int_field coercions and their 400s, the party stash
(`load_loot` + `_loot_int`), the character roll (both dice engines), the display stage's refusals, the calendar events
(`_safe_int`, str() fields), the quest suggestions, the party roster's visibility filter (`_visible_entity_ids` with
the hidden-companion carry), the calendar row's creation on first use, the dice label's isprintable, the character
form's JSON fields through json.loads' fallback, and the quest/tables int() form fields with the links/entries
json.loads fallback. What still forwards on those routes is the parity tests' odd-stored-data variants, which Python
itself answers with a 500 (junk `equipment_json` etc.) — the decline is correct, and at the switch those become the
gateway's own 500. The python_only tail (216 routes, ~345 requests) is the AI imagegen / attachments / tools-page
family and the file downloads (`/pages/{}/download`, `/rules/download.md`) — file-serving routes with visibility
gates, a contained next batch.
To find why a route declines, run the gateway with `ND_DECLINE_TRACE=1` (edits.rs run() prints `DECLINE METHOD
path body=...` per decline) and read the gateway's stderr; the census
(`scripts/census.py --write tests/_data/shard*/forwarded.log`) re-ranks the rest after a full gateway suite.

### Must move only at the switch (Python keeps state in memory)

- `POST /settings/system*`: Python caches the LLM override and Ollama tuning; they move with the gateway owning those caches.
  DONE since that was written: `POST /settings/system` itself (settings_system.rs — URL validation, the number fields'
  parse/bounds messages, use_mmap / gpu_preset fallbacks, the API-key leave/clear/replace rules, the server-env sanitizer
  and the ollama.env writer, 400 re-renders through the shared page_context), `POST /settings/system/model-override` and
  `/delete` (settings_overrides.rs), and the Studio-key save (`studio_key.rs` — the paste cleaner, the live verify GET
  against the configured Studio, the clear; only the auth-failure timestamp banner stays Python's, and it is in-memory
  state that dies with the switch anyway).
- Any route whose Python handler uses an in-process cache (see `tests/inprocess_only.txt` for the caches tests know about).

Done since that list was written: `POST /settings` and `/settings/resources` (edits.rs — the display / hover form and the
resource sizing, byte for byte), and the password / 2FA account-security routes (the gateway's own lockout counter replaced
Python's in-memory one).

### Phase 6 steps 4-5 (partly done)

- **Startup work**: the database boot is DONE (startup.rs, see RUST_SWITCH_OFF.md). Still Python's at `_startup_tasks`
  (app/main.py 390): static-thumbnail warming, db_upkeep (daily PRAGMA optimize), `_load_preview_format` /
  `_remake_thumbnails` / `_seed_bundled_maps`, `_refresh_settings_overrides` / `_sync_ollama_server_env`, the five job
  engines' `sweep_interrupted_*` / `resume_interrupted_*`, `_backups.start`, `_fan_control.start`, `_diagnostics.start`.
  These run in exactly one process by design (RUST_MIGRATION_STATE.md) and only matter for the Python-free image —
  port them when the route census is empty, not before.
- **Background loops** Python runs: job sweeps, the watchdog, scheduled backups, anything started in `app/main.py`'s lifespan.
- **Libraries without a Rust twin**: reportlab (PDF export), the PDF/HTML sheet readers behind the AI drafters - may stay in a small
  sidecar per the plan.
- **Dockerfile**: drop Python, measure image size and memory against today's.

## Pitfalls seen so far

- The page context declines while the `app_settings` row is missing; it now creates it (`layout::app_settings_row`). Similar "Python
  creates it on first use" rows (e.g. a world's calendar) still make pages decline until Python made them.
- Starlette sends a fresh session cookie on most responses; tests compare bodies and statuses, not every header.
- Disk: release + debug target dirs grow to 20+ GB; delete `rust/target/debug` when space runs out.
- A shell `pgrep -f "<pattern>"` loop matches itself; use a `[x]` trick in the pattern.
- `scripts/census.py` needs the forward logs of a full gateway suite run (`scripts/gateway_suite.sh` writes them per shard).
