"""Phase-1 live sync — a tiny per-world change counter pushed to browsers
over Server-Sent Events.

Every route that mutates "table state" a GM keeps an eye on (PC vitals,
party membership/loot/location, quest status, awarded XP) calls
`live.touch(world_id)` after committing. The SSE endpoint below streams that
counter to every open page for the active world; static/js/nd-live.js
(elected per-browser leader) relays it to every other open window — the main
tab, floated panels, anything — as a window `nd-live` event, where pages
re-fetch their own server-rendered partials.

Design notes:
- An in-memory counter, not a pub/sub queue: mutators only ever touch an
  int, so publishing works from both async handlers and sync handlers
  running in the threadpool (a plain dict store is all that's needed — no
  cross-thread loop signaling). Single-process by design (SQLite, one
  uvicorn worker), same assumption the background-job stores make.
- The stream polls the counter once a second rather than awaiting a queue:
  it costs one dict lookup per open browser per second and makes the
  endpoint immune to slow-consumer backpressure — a stalled reader just
  misses intermediate versions, which is fine because the payload carries no
  data, only "something changed, re-fetch".
- Heartbeat comments keep Cloudflare/proxies from buffering or timing the
  connection out (same reason ai.py's streams send them).
"""

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .database import get_db
from .deps import get_world_ctx

router = APIRouter()

# world_id -> monotonically increasing int. A GIL-atomic dict store — no
# locking needed for "bump an int from many threads, read it from one loop".
_VERSIONS: dict = {}

_HEARTBEAT_EVERY = 15  # seconds between ": keep-alive" comments
_POLL_EVERY = 1.0  # seconds between counter checks


def touch(world_id) -> None:
    """Record that something in `world_id` changed. Safe to call from async
    handlers and sync (threadpool) handlers alike."""
    if world_id is None:
        return
    _VERSIONS[world_id] = _VERSIONS.get(world_id, 0) + 1


def version(world_id) -> int:
    return _VERSIONS.get(world_id, 0)


async def stream_world_events(world_id) -> AsyncIterator[str]:
    """The SSE event body: `event: version` frames whenever the counter
    moves, `: keep-alive` comments every _HEARTBEAT_EVERY seconds so
    proxies (Cloudflare Tunnel included) never buffer or time the
    connection out. Exposed as a module-level function rather than a
    closure so tests can drive it directly — a TestClient streaming test
    of an infinite generator leaves the portal thread stuck."""
    last = None
    since_beat = 0.0
    # retry: reconnect quickly if the proxy drops us; nd-live.js's leader
    # election means only one connection per browser pays this cost.
    yield "retry: 3000\n\n"
    while True:
        v = version(world_id)
        if v != last:
            last = v
            yield f"event: version\ndata: {v}\n\n"
            since_beat = 0.0
        else:
            since_beat += _POLL_EVERY
            if since_beat >= _HEARTBEAT_EVERY:
                since_beat = 0.0
                yield ": keep-alive\n\n"
        await asyncio.sleep(_POLL_EVERY)


@router.get("/api/live")
async def live_stream(
    request: Request,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    """SSE stream of the active world's change counter. Player-safe (the
    /api/live allowlist entry in main.py) because the payload is a bare
    integer for a world the viewer can already access — `get_world_ctx`
    enforces membership, exactly like every page render."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)

    return StreamingResponse(
        stream_world_events(world.id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
