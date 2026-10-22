# Writes made by the gateway (Rust)

Phase 3 of `docs/RUST_REWRITE_PLAN.md`: edits and uploads. One write route so far - `POST /api/dice/roll` - as the pattern the rest has to follow.
It is **off by default**: switch it on with `ND_RUST_ROUTES=login,dice_roll` (the default is `login`).

## The rule: the gateway answers only the success case

A write that Rust gets wrong can damage data or let someone do what they may not. So the gateway re-does every check Python does, and **if any
check fails, or anything about the request is unusual, it hands the request to Python unchanged** (body included) and Python answers, with its own
error, exactly as before. A mistake in the gateway can therefore only turn an answer into a refusal-by-Python, never a refusal into an answer.

For `POST /api/dice/roll` (`rust/nd-gateway/src/writes.rs`) the gateway answers only when all of these hold:

| Check | Python's code it repeats |
|---|---|
| a session cookie that verifies with `SECRET_KEY`, not older than 6 h (Python re-signs older ones, only it can) | `session_cookie.py` |
| the user exists and the cookie's `session_version` is the user's | `auth_gate` |
| the active world: `?w=` over the `active_world` cookie, else the first world the person may use (GM: every world; others: memberships, in id order) | `deps.get_world_ctx`, `_active_membership` |
| role in that world (assistant / owner count as assistant) and the world's `section_access_json["dice"][role]` is not `none` (default `read`) | `deps.world_section_level`, `_default_section_levels` |
| `Content-Type: application/json`, body at most 16 KB, `notation` a string, `label` a string if present | pydantic `RollBody` |
| notation / label are printable ASCII (Unicode whitespace and printability rules stay Python's) | `str.strip`, `isprintable` |
| the notation fits the grammar (`2d6+3`, at most 8 terms, 50 dice, 1000 sides) | `dice._evaluate` |

It then stores the row (the same columns, the stored time spelled like SQLAlchemy, the breakdown spelled like `json.dumps`), bumps the live
counter `sig:<world>:dice` in `shared_state` in the same transaction (so open dice pages refresh), and answers with the same JSON, same key order,
plus `X-Nd-Served-By: gateway`. SQLite is opened per request with `busy_timeout=30000` and WAL like Python's connection, so both servers write one file.

## Checked by

- `rust/nd-gateway/tests/writes.rs`: 16 refusal cases each reach Python with the body intact and store nothing; membership, `?w=`, cookie world,
  a world with dice switched off for players, assistants, an empty display name; 40 concurrent rolls all land and the counter is exact.
- `tests/test_gateway_golden.py`: for a GM, a player and a player of another world, 14 request bodies each sent to Python directly and through
  the gateway: same status, same error words, same fields; plus a section switched off, a stranger, and a changed password (`session_version`)
  refused identically; plus 40 rolls written by both servers at once (20 each) - every one stored, live counter exact.
- CI runs the whole Python suite through the gateway with `ND_RUST_ROUTES=login,dice_roll`.

## Measured

4 cores, 16 clients rolling `4d6+2`: Python 66 rolls/s (p50 200 ms), gateway 366 rolls/s (p50 39 ms) - `scripts/bench_dice.py`. The benchmark client
shares the machine, so read it as "several times faster", not as a limit.

## Phase 3: the form and JSON writes (`ND_RUST_ROUTES=...,writes`)

`rust/nd-gateway/src/edits.rs` holds the writes, one function per Python handler, routed by path pattern. The shared parts:

| Part | What it reproduces |
|---|---|
| `crate::layout::build(.., "POST", ..)` | who is asking, the active world, auth_gate's allowlists for a POST |
| `crate::form` | Starlette's urlencoded form (last value of a repeated name wins); a body Starlette could read differently (non-ASCII bytes, `;`, non-UTF-8 escapes) is Python's. `py_int`: Python's `int()` only for plain digits |
| `crate::orm` | rows as SQLAlchemy writes them: every model's Python-side `default=` on INSERT and `onupdate=` on UPDATE (copied from the models into `app_data.json` by `scripts/gen_rust_data.py`); no UPDATE - so no `updated_at` bump - when nothing changed; a bulk `query.update()` sets `onupdate` columns too (both checked against SQLAlchemy itself) |
| `orm::touch` | `live.touch(world)` where the Python handler calls it |
| One transaction per request (`edits::run`) | Python's single commit: `BEGIN IMMEDIATE` before the handler, `COMMIT` only when the gateway answers; a handler that gives up part-way rolls back, so Python never applies the same write twice |
| `uploads::serve` / `uploads::scan` | Upload bodies streamed to a temporary file under `<uploads>/_gateway` (never held in memory), multipart parts located in it, a file part copied into place from it; a request the gateway does not answer goes to Python with the body re-sent from the file. Files a handler wrote are removed when it gives up |
| `imaging::finish_upload` | `convert_image` + `make_thumbnail` for an uploaded picture: the configured format (ravif for AVIF, libwebp for WebP, the image crate for PNG / JPEG; bytes differ from Pillow's, format / pixel size / alpha / file names do not), the thumbnail at Pillow's `thumbnail()` size (checked on 400 random sizes). Animated pictures, GIFs, AVIF sources and pictures it cannot decode are Python's |
| answers | Starlette's 303 redirect, FastAPI's compact JSON |

A row of another world than the viewer's active one is left to Python (its cross-world access rule is not ported). Checked by
`tests/test_gateway_writes.py`: each case runs once through Python and once through the gateway on two identical rows, for a GM, a player and a
player of another world, and compares the answers and every column afterwards (timestamps as "changed or not").

| Routes | Batch |
|---|---|
| `POST /quests/new`, `/quests/<id>/edit`, `/quests/<id>/delete`, `/api/quests/<id>/status` | 1 |
| `POST /facts/new`, `/facts/<id>/edit`, `/facts/<id>/delete` (the recap watermark on the world) | 2 |
| `POST /sessions/new`, `/sessions/<id>/edit` (featured NPCs / characters), `/api/sessions/<id>/prep/toggle`, `/prep/add`, `/prep/<i>/delete` | 6 |
| `POST /entity/<id>/link/<target>`, `/entity/<id>/unlink/<target>`, `/entity/<id>/notes/new`, `/notes/<n>/toggle`, `/notes/<n>/edit`, `/notes/<n>/delete` | 7 |
| `POST /api/entity/<id>/field` (subtype), `/api/entity/<id>/rename`, `/api/entities/bulk-folder` (plain integer ids only) | 8 |
| `POST /new`, `/entity/<id>/edit` (urlencoded, or multipart with no file chosen: `form::parse_multipart`; an upload is Python's), `/entity/<id>/duplicate`, `/entity/<id>/delete` | 9 |
| `POST /api/entities/bulk-visibility`, `/api/entity/<id>/alias` (ASCII names: `casefold()` beyond is Python's), `/kind/<kind>/bulk-delete` | 10 |
| `POST /parties/new`, `/parties/<id>/edit` (the GM; a member player's notes / goals), `/parties/<id>/delete`, `/api/parties/<id>/location`, `/api/parties/<id>/members/toggle` (assistants' hidden-companion rules: Python's) | 11 |
| `POST /api/parties/<id>/loot` add / remove / claim / unclaim, on a clean stash (`give`, which writes the character's equipment by its system, and stashes `load_loot` would repair: Python's) | 12 |
| `POST /api/characters/<id>/hp-async`, `/shock`, `/pp`, `/mp` (`pc_maxima` and the character log ported; text stat values and int() of non-integers: Python's) | 13 |
| `POST /api/characters/<id>/xp`, `/conditions` (ASCII labels: `isprintable()` beyond is Python's), `/currency` (a purse stored as `json.dumps` writes it) | 14 |
| `POST /characters/new`, `/characters/<id>/edit` (`_apply_form`; urlencoded or multipart without a portrait), `/characters/<id>/owner`, `/characters/<id>/delete` (journal, prefs, log, shares, party members / loot claims, calendar links) | 15 |
| `POST /api/characters/<id>/level-up`, `/feats`, `/equipment` (int vs float weights as Python keeps them; `carry_info`), `/carry-limit` (character prefs) | 16 |
| `PUT /api/characters/<id>/sync`, `POST /api/worlds/<id>/characters/sync` (`_apply_sync_json`, the answer as `_pc_to_sync_dict`), `/characters/<id>/retire-to-npc`, `/api/characters/<id>/portrait-from-url`; PUT routes (`edits::PUT_ROUTES`) | 17 |
| `POST /sessions/<id>/delete` (with its raw-recording folder; a session with its own uploaded pictures: Python's), `/sessions/<id>/recap-publish`, `/api/sessions/<id>/live-transcript/clear`, `/loot/transfer`, `/xp` (custom-system characters: Python's) | 18 |
| `POST /calendar/config` (`calendar_config`'s cleaners: months, moons, holidays, weekday names, year format; `str()` / `int()` of values this port cannot read as Python does: Python's), `/api/calendar/icons/<id>/delete` (with its file) | 19 |
| `POST /api/facts/bulk` (dedup by `_normalized_fact_key`; non-ASCII facts, where Python's and Rust's lower() can differ: Python's), the facts_parse job's `draft_consumed` | 20 |
| `POST /tables/import` (a JSON file upload; two tables with one slug in a file: Python's), `/api/quests/apply` (reviewed suggestions: new quests and updates) | 21 |
| `POST /audio`, `/video`, `/pages`: `/albums/new`, `/albums/<id>/rename`, `/albums/<id>/delete` (nested albums, their items and files), `/<id>/edit`, `/<id>/delete` (`edits::Lib`; hiding / deleting the clip players are hearing, and page templates still in use: Python's) | 22 |
| `POST /images/albums/new`, `/images/albums/<id>/rename`, `/move`, `/delete`, `/add`, `/remove` (image albums hold urls; spotlight and image delete: Python's) | 23 |
| `POST /api/sessions/<id>/recap-audio`, `/recap-audio/<clip>/visibility`, `/remove`, `/media/attach`, `/media/<kind>/<ref>/visibility`, `/rename`, `/remove` (the panel as `session_media.panel`; items still waiting for a uid, and removing a picture uploaded for the session: Python's) | 24 |
| `POST /audio/upload/chunk`, `/video/upload/chunk`, `/pages/upload/chunk`, `/audio/upload/complete`, `/pages/upload/complete`, `/audio/upload`, `/pages/upload` (stored as `unique_upload_filename` names them; video files, which ffmpeg finishes, and over-limit files: Python's) | 25 |
| `POST /images/albums/<id>/upload`, `/images/albums/upload/chunk`, `/images/albums/upload/complete` (pictures converted and thumbnailed by `imaging`) | 26 |
| Pictures on ordinary forms: `POST /new` and `/entity/<id>/edit` (`image_file`), `/characters/new` and `/characters/<id>/edit` (`portrait`), `/api/calendar/days/<day>/icons` (converted, no thumbnail); streamed like an upload when bigger than 256 KB | 27 |
| `POST /folders/rename` (the same LIKE filter), `/entity-templates/new`, `/entity-templates/<id>/edit`, `/entity-templates/<id>/delete`, `/api/entity/<id>/image` | 28 |
| `POST /races/new`, `/professions/new` (with an optional picture), `/races/<id>/delete`, `/professions/<id>/delete` (the built-in lists, parsed from the rules markdown: Python's) | 29 |
| `POST /api/view-prefs/<section>`, `DELETE /api/view-prefs/<section>`, `POST /api/saved-views/<section>`, `DELETE /api/saved-views/<section>/<id>` (`view_prefs.clean` on the registry in app_data.json; the "site" layout and reset-all, which also live in the session cookie, and presets: Python's); DELETE routes, and requests with no body at all | 30 |
| `POST /worlds/<id>/home/edit` (with its background picture), `/api/worlds/<id>/home/quick-link`, `/pinned-tile`, `/pinned-tile/remove`, `/hide-kind` (the home sanitizers; the active world only) | 31 |
| `POST /account/name`, `/account/tokens/<id>/revoke`, `/account/trusted-devices/<id>/revoke` (password, 2FA and new tokens - hashing, one-time secrets on a rendered page: Python's) | 32 |
| `POST /worlds/<id>/edit` (the world settings form: accent, font, size, permissions, limits, preset) | 33 |
| `POST /worlds/<id>/invites/new`, `/invites/<id>/revoke`, `/members/<user>/remove`, `/members/<user>/role`, `/members/<user>/clear-2fa` (a reset password, shown once on a rendered page: Python's) | 34 |
| `POST /worlds/<id>/notes/<kind>/<id>/new`, `/notes/<kind>/<id>/<note>/delete` (private notes), `/worlds/<id>/theme/clear`, `/theme/preset` | 35 |
| `POST /worlds/new` (the `active_world` cookie as Starlette writes it; a picked calendar preset stored as the text `scripts/gen_rust_data.py` takes from Python), `/worlds/<id>/theme/import` (`_sanitize_theme` ported; a non-string field value: Python's) | 36 |
| `POST /worlds/<id>/rules/edit` (an overlay checked by the ported `parse_rules_overlay`; one it refuses: Python's 400), `/rules/import` (an overlay with a fractional `order`: Python's) | 37 |
| `POST /worlds/<id>/kinds/new`, `/kinds/edit`, `/kinds/<kind>/delete`, `/nav-menus/edit` (with `section_access_json`: Python walks a set, so its key order changes between processes; the gateway writes the sorted order) | 38 |
| `POST /worlds/<id>/ai-instructions/import`, `/ai-instructions/<id>/toggle`, `/toggle-players`, `/delete` (stored settings: no AI call) | 39 |
| `POST /races/add-builtin`, `/races/add-all-builtin`, `/professions/add-builtin`, `/professions/add-all-builtin` (the catalogs, and markdown2's HTML for each, taken from Python by `scripts/gen_rust_data.py`; a race whose body was edited since: Python's). `/images/spotlight` stays Python's: it clears Python's in-process spotlight cache | 40 |
| `POST /api/entity/<id>/markup` (wrap, unwrap, replace, restore, picture size) and `/api/entity/<id>/send-to-player`: `app/markup_locator.py` ported to `crate::markup` in code-point offsets; a refusal (its 422 message) and text whose `\w` / `\s` the two regex engines might read differently: Python's. Checked with fixed cases and 300 random selections | 41 |
| `POST /boards/new` (unique slug), `/boards/<slug>/save` (nodes and edges as `json.dumps` writes them), `/boards/<slug>/delete` | 5 |
| `POST /api/calendar/events`, `/api/calendar/events/<id>/delete`, `/api/calendar/advance`, `/api/calendar/set-date` (the stored config rewritten as `json.dumps` writes it) | 4 |
| `POST /tables/new` (Python's unique slug), `/tables/<id>/edit`, `/tables/<id>/delete`, `/api/tables/<id>/roll` (weighted pick; whole-number weights only) | 3 |

## Not done (phase 3's exit criteria are NOT met)

Phase 3 is the forms and JSON writes for entities, sessions, facts, quests, parties, characters, galleries, audio / video libraries, chunked
uploads and picture processing - hundreds of write routes. One is done. The rest need, in this order:

1. The rest of the permission layer in Rust (`_is_player_safe` / `_is_assistant_safe` / `_is_owner_safe` allowlists, per-row visibility,
   `[gmonly]` rules) - the dice route needed only the section matrix. Each route also needs its own refusal-parity test like the one above.
2. The failed-login limiter in `shared_state`, then the auth POSTs.
3. Multipart uploads and picture processing (the AVIF / WebP / tiles work is Python's Pillow today).
4. Routes that touch Python-only memory (`docs/RUST_MIGRATION_STATE.md`) stay in Python.

Also not covered: Python's request timing log and `AiBackgroundMiddleware` do not see a roll the gateway wrote; non-ASCII notation / label go to Python.
