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

Census from the latest gateway suite (logs of two runs combined, so counts are roughly doubled):
**359 route patterns answered only by the gateway, 695 mixed, 654 still Python-only.** Phase 6 steps:

| Step | State |
|---|---|
| 1. Authentication (login, 2FA, sign-out, sessions) | done; 2FA setup page and confirm done; password change / 2FA disable / backup-code regenerate wait for the switch (below) |
| 2. Busiest Python-only routes | in progress (see "Left" below) |
| 3. Mixed routes: refusals / edge cases | started: character quick-edits, party stash (incl. give), saved views, conditions, currency |
| 4. Migrations, DB healing, seeding, background loops | not started |
| 5. Dockerfile without Python | not started |

Ported in the latest batches (details in `RUST_SWITCH_OFF.md`): settings page, session planner, character hub tabs (quests, world,
now, schedule, log, notes), uploads access, party pages, calendar search, race/profession catalogs, account page and 2FA setup,
name / token routes, world-context smart + player, Studio prefs, entity markdown download, Session Log list, NPC talk page, world
JSON exports, character sheets, and the app-settings row creation (it used to send most first pages to Python).

## Left to port

### Python-only routes (busiest first; full list `docs/census/python_only.txt`)

Already ported since the census was taken (ignore in the list): `/pages/sheets*`, `/npc-talk`, `/session-log`, `/worlds/{}/export`.

- AI attachments: `POST /api/ai/attachments/upload`, `/upload/chunk`, `/upload/complete`, `/api/ai/attachments/audio-jobs*`
- Image generation for players: `POST /api/ai/imagegen/player/generate`, `GET /api/ai/imagegen/player/jobs/{}`, `/api/ai/imagegen/jobs/{}`, `/model-templates`, `GET /image-gen`
- Job status polls: `GET /api/media-rename/runs/{}`, `GET /api/sessions/ai/audio-jobs/{}`
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
- Python has no `POST /logout` (a 405 answer could be ported)

### Mixed routes (gateway answers the common case; Python still answers the rest)

Busiest in `docs/census/mixed.txt` (`python/gateway` counts). Typical remaining cases are refusals (404/403/400 with Python's
message), pydantic coercions, and data the port declines on. Top: `POST /api/characters/{}/equipment|feats|hp-async|conditions|currency`
(remaining: odd stored data, coercions), `POST /api/parties/{}/loot`, `GET /sessions/{}`, `POST /tables/{}/edit`, `GET /entity/{}`,
`POST /api/entity/{}/markup`, `GET /`, `POST /api/cockpit/workspace`, `GET /calendar`, `POST /api/display/show`,
`POST /characters/new`, `POST /parties/{}/edit`, `POST /quests/{}/edit`, `POST /calendar/config`, `POST /characters/{}/owner|edit`,
`GET /api/schedule/state`, `POST /worlds/{}/edit`, `POST /new`, `POST /api/calendar/events`, `POST /entity/{}/edit`, ...
To find why a route declines, add temporary `eprintln!` markers at its `?` returns (and in `layout::build`), run the one test, and
read the gateway's stderr.

### Must move only at the switch (Python keeps state in memory)

- `POST /settings`, `/settings/system*`, `/settings/resources`: Python caches resource sizing, LLM overrides and Ollama tuning.
- `POST /account/password`, `/account/2fa/disable`, `/account/2fa/backup-codes/regenerate`: Python counts failed password attempts
  in memory (`_failed_password_changes`, lockout after 5); give the gateway its own counter and move them together.
- Any route whose Python handler uses an in-process cache (see `tests/inprocess_only.txt` for the caches tests know about).

### Phase 6 steps 4-5 (not started)

- **Startup work**: `app/database.py` `_migrate`, `_heal_table`, seeding (default world, templates, app settings), FTS table upkeep;
  the 2FA / session secret handling.
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
