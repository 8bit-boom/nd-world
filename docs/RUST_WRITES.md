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
