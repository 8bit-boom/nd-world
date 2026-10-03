"""AI tasks run one at a time, in the order they were asked for.

One Studio on one GPU: two model jobs at once fight over VRAM and each takes longer than both would back to back (a
chat model and a TTS model swapping in and out is the worst case). Every BACKGROUND AI task takes its turn here:

  * the 202 + poll tasks (app/ai_background.py),
  * the durable job engines (audio/session recaps, chat, image, video — `_run_job`),
  * the in-memory job runners (character drafts, quest sync, map markers, cockpit find, auto-tag, …).

A task waits in a first-come line and holds the single slot for its WHOLE run (all the chunks of a long recap stay
together), then hands it to the next. Interactive streams (live chat, Ask AI, the NPC conversation) are deliberately not
queued: someone is watching them type, and they must never sit behind a ten-minute recap.

Safety nets, because one stuck holder would otherwise freeze every AI feature:
  * a task cancelled (or crashing) while waiting or holding just leaves the line;
  * a holder that has had the slot longer than MAX_HOLD_SECONDS is cancelled by the next in line;
  * state left by a dead event loop (tests, a reloaded server) is dropped instead of blocking the new loop.

Never take the slot from code that is itself running inside a queued task (it would wait for itself): queue at the job
level only — which is where it is applied.
"""
import asyncio
import functools
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

_log = logging.getLogger("nd.ai_queue")

# A holder that has not finished in this long is treated as hung and cancelled so the line moves. Generous: a cold
# model load plus a long recap legitimately takes many minutes.
MAX_HOLD_SECONDS = float(os.environ.get("AI_QUEUE_MAX_HOLD_SECONDS", "7200"))
_WATCH_INTERVAL = 30.0     # how often a waiter checks whether the holder is stuck


class Ticket:
    __slots__ = ("label", "kind", "since", "started", "future", "task")

    def __init__(self, label: str = "", kind: str = ""):
        self.label = label
        self.kind = kind
        self.since = time.time()           # when it joined the line
        self.started: Optional[float] = None   # when it got the slot
        self.future: Optional[asyncio.Future] = None
        self.task: Optional[asyncio.Task] = None

    def describe(self) -> dict:
        now = time.time()
        return {"label": self.label, "kind": self.kind,
                "waited": int(((self.started or now) - self.since)),
                "running_for": int(now - self.started) if self.started else 0}


class AiQueue:
    def __init__(self):
        self._running: Optional[Ticket] = None
        self._waiting: deque = deque()
        self._loop = None

    def reset(self) -> None:
        self._running = None
        self._waiting.clear()
        self._loop = None

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            if self._loop is not None and (self._running or self._waiting):
                _log.warning("AI queue: dropping %d ticket(s) left by a previous event loop",
                             len(self._waiting) + (1 if self._running else 0))
            self._running = None
            self._waiting.clear()
            self._loop = loop

    # ── introspection ──────────────────────────────────────────────────────────────────────────
    def waiting_count(self) -> int:
        return len(self._waiting)

    def position(self, ticket: Ticket) -> int:
        """0 = has the slot, 1 = next in line, … ; -1 = not in the queue."""
        if ticket is self._running:
            return 0
        for i, t in enumerate(self._waiting, 1):
            if t is ticket:
                return i
        return -1

    def snapshot(self) -> dict:
        return {"running": self._running.describe() if self._running else None,
                "waiting": [t.describe() for t in self._waiting]}

    # ── taking a turn ──────────────────────────────────────────────────────────────────────────
    # enter() joins the line at once (synchronously, so the position is known the moment a task is accepted),
    # wait_turn() waits for the slot, leave() gives it up. slot() / serialized() wrap all three. leave() is idempotent
    # and also safe to hang on a task's done-callback: a task cancelled before it ever ran never reaches a `finally`.
    def enter(self, label: str = "", kind: str = "") -> Ticket:
        self._bind_loop()
        t = Ticket(label, kind)
        if self._running is None and not self._waiting:
            self._take(t)
        else:
            t.future = asyncio.get_running_loop().create_future()
            self._waiting.append(t)
        return t

    def busy(self) -> bool:
        return self._running is not None or bool(self._waiting)

    async def wait_turn(self, t: Ticket) -> None:
        t.task = asyncio.current_task()
        if t.future is None or self._running is t:
            return
        try:
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(t.future), _WATCH_INTERVAL)
                    return
                except asyncio.TimeoutError:
                    self._reap_stuck_holder()
        except BaseException:
            self.leave(t)
            raise

    def leave(self, t: Ticket) -> None:
        if t in self._waiting:
            self._waiting.remove(t)
        self._release(t)

    @asynccontextmanager
    async def slot(self, label: str = "", kind: str = ""):
        """Wait for the slot, hold it for the body, give it to the next."""
        t = self.enter(label, kind)
        try:
            await self.wait_turn(t)
            yield t
        finally:
            self.leave(t)

    def _take(self, t: Ticket) -> None:
        self._running = t
        t.started = time.time()

    def _release(self, t: Ticket) -> None:
        if self._running is t:
            self._running = None
        while self._running is None and self._waiting:
            nxt = self._waiting.popleft()
            if nxt.future is None or nxt.future.done():
                continue
            self._take(nxt)
            nxt.future.set_result(None)

    def _reap_stuck_holder(self) -> None:
        h = self._running
        if h is None or h.started is None or time.time() - h.started <= MAX_HOLD_SECONDS:
            return
        _log.warning("AI queue: %r has held the slot for over %ss — cancelling it so the line can move",
                     h.label, int(MAX_HOLD_SECONDS))
        if h.task is not None and not h.task.done():
            h.task.cancel()


queue = AiQueue()
slot = queue.slot


def serialized(label: str = "", kind: str = ""):
    """Decorator: the async function takes its turn in the AI queue for its whole run."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            async with queue.slot(label or fn.__name__, kind):
                return await fn(*args, **kwargs)
        wrapper._ai_serialized = True
        return wrapper
    return deco
