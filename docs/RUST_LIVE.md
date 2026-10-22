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
