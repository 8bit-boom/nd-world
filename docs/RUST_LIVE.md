# Live updates from the gateway

Phase 4 of `docs/RUST_REWRITE_PLAN.md` (live and table tools). One piece so far: `GET /api/live`, the stream every open page keeps to hear
"something in this world changed" (`app/live.py`). Off by default; switch it on with `live` in `ND_RUST_ROUTES` (e.g. `login,dice_roll,live`).

## What it does

`rust/nd-gateway/src/live.rs`. The same frames as Python: `retry: 3000`, `event: version` at the start and whenever the world's counter moves,
`event: signal` (`spot`, `dice`, `combat`, ...) for every named signal that moved since the stream opened, `: keep-alive` after 15 quiet seconds;
`Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`. The counters are read from `shared_state`
(the table both servers write, phase 0) by one background task every 250 ms; each stream checks that copy once a second.

Who may open it is decided like every gateway route: session, user, `session_version`, the active world (`?w=`, cookie, first world) and the
allowlist from `rust/nd-perm`. Anything else - no session, no world - goes to Python, which answers its own 401 / 404.

## Checked by

`tests/test_gateway_golden.py`: a stream opened directly and one through the gateway, while a change and a signal happen in world A and a change in
world B: both streams send exactly the same version and signal frames (and nothing about world B); a stream without a session is refused with
Python's 401 either way. CI runs the whole suite through the gateway with `live` on.

## Measured - and what it means

`scripts/bench_live.py`, 200 streams at once on the 4-core test machine:

| | memory the serving process grew | change seen by all 200: median / last |
|---|---|---|
| Python | +5.2 MB | 727 ms / 1,166 ms |
| gateway | +4.5 MB | 758 ms / 1,235 ms |

No real gain: Python's streams were already cheap (asyncio tasks, one table read per 0.25 s for all of them), and both sides check once a
second by design. What moving it buys is that open browsers no longer hold connections to the Python process (which matters for a Python
restart: the gateway keeps the streams open and they continue when Python is back). It stays opt-in.

## Not done (phase 4's exit criteria are NOT met)

The other streams and table tools - the second screen's stream (`/api/display/stream`), AI and chronicler streams, cockpit, combat tracker,
character hub, schematic maps (walls, fog, tokens, buy / pick-up with their locking) - are Python's. Two-browser tests for each live feature
are the exit criterion; this is one stream.

## Phase 4, full port: the table tools' writes

Behind `writes` in `ND_RUST_ROUTES`, answered like the phase 3 writes (`docs/RUST_WRITES.md`: success only, one transaction, everything
else to Python). Every batch also checks that the gateway wakes the same live listeners as Python: the world's `live:<id>` counter and
the named signals (`sig:<world>:combat` ...) in `shared_state` move by the same amount (`tests/test_gateway_writes.py`).

| Routes | Batch |
|---|---|
| `POST /combat/<id>/state` (`combat_state.normalize_state` ported; a stale `rev` - the 409 with the current state - and values `int()` / `float()` / `casefold()` would read their own way: Python's), `/combat/new` (with `default_ruleset`), `/combat/<id>/delete`, `/link-session`, `/unlink-session`, `/api/combat/<id>/finish` (the summary fact), `/reopen` | P4-1 |
| `POST /combat/<id>/duplicate`, `/combat/<id>/save-template`, `/combat/templates/<id>/run` (the world's characters added as combatants), `/api/combat/<id>/sync-characters` (HP, Shock, PP / MP, conditions back to the sheets) - for native sheets; a world with a custom-system character (vital and extra tracks from the template's sheet system): Python's | P4-2 |
| `POST /api/display/show` (pictures, entity portraits, maps and fights on the second screen; a text card - markdown2's HTML - and every refusal: Python's), `/api/display/clear` (the stage in `shared_state` stage:<world>, read-modify-write inside the request's transaction), and `GET /api/display/stream` (with `live` in `ND_RUST_ROUTES`: the same `state` / `combat` frames as Python, checked with two screens - one stream direct, one through the gateway - in `tests/test_gateway_golden.py`) | P4-3 |
| `POST /api/maps/schematic/<slug>/move-token`, `/pickup-item`, `/buy-item` (the players' moves on a map; the gateway's BEGIN IMMEDIATE is the same SQLite lock Python takes, checked by a race of six players - half through Python, half through the gateway - for one item and the last stock: exactly one winner, nothing duplicated) | P4-4 |
| `POST /api/characters/<id>/hub/end-turn`, `/hub/pass-turn` (the player's turn controls, `combat_turns.advance` / `pick_next` ported, both turn systems; the answer's `_combat_now`) | P4-5 |
| `POST /api/characters/<id>/hub/handouts/seen`, `POST /hub/companions`, `PATCH` / `DELETE /hub/companions/<id>` | P4-6 |
| `POST /api/characters/<id>/hub/entries`, `PATCH` / `DELETE /hub/entries/<id>` (the player's journal, goals and notes; timestamps as Python's `isoformat()`) | P4-7 |
| `POST /api/characters/<id>/hub/pins` (pin / unpin a page; the players' page list, `_place_rows`, ported) | P4-8 |
| `POST /api/characters/<id>/hub/rest`, `/hub/rest/undo` (an N&D character without a sheet template; a system's own Rest rules stay Python's) | P4-9 |
| `POST /api/characters/<id>/hub/log/<entry>/undo` (numbers, coins, conditions, resource columns; the equipment / advance / custom-field undos and every refusal stay Python's) | P4-10 |
| `POST /api/characters/<id>/hub/keepsakes` (a handout, or a GM's raw file / text), `/hub/keepsakes/<id>` (rename), `/hub/keepsakes/<id>/delete` (not a player's own upload: its file goes too, Python's) | P4-11 |
| `POST /api/characters/<id>/hub/loot` (claim / unclaim a party-stash item; a stash `load_loot` would repair stays Python's) | P4-12 |
| `POST /api/characters/<id>/hub/initiative` (Speed + d10 into the dice log and the encounter, the active combatant kept after the re-sort; fluid fights and other systems stay Python's) | P4-13 |
