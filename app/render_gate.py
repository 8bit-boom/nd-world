"""The page-render gate with a limit that tunes itself (app/http_perf.py holds a slot while a page is built).

Building a page is Python work that holds the interpreter lock, so more pages at once than the machine can really run in parallel only take
turns slowly - on 4 cores 2 at once gave 28 pages/s and 8 or more 6 pages/s. What is best depends on the machine and the pages, so with no
fixed value (ND_RENDERS in .env or Settings -> System -> Server resources) the limit finds it under load:

  * only busy periods count: a 5 s window in which pages were waiting for a slot and at least 8 were built;
  * the pages per second of the current limit are remembered; one more (and one fewer) is tried in turn, and the limit moves to a
    neighbour only when that was >5 % faster - so it settles on the best and stays there;
  * a measurement older than 5 minutes is tried again, so a change in load or pages is followed;
  * the limit stays between 1 and half the usable cores (at least 2), and starts at 2 - the measured best on small machines.

`Tuner` is the pure decision part (tests/test_render_gate_tuning.py drives it with made-up numbers); `Gate` is the asyncio part.
"""
import asyncio
import collections
import time

WINDOW_S = 5.0
MIN_PAGES = 8
STALE_S = 300.0
BETTER = 1.05


class Tuner:
    def __init__(self, hi: int, start: int = 2, lo: int = 1):
        self.lo, self.hi = lo, max(lo, hi)
        self.limit = min(self.hi, max(self.lo, start))
        self.rates = {}                                   # limit -> (pages per second, when measured)

    def observe(self, rate: float, now: float) -> int:
        """A busy window's pages per second at the current limit; returns the limit to use next."""
        old = self.rates.get(self.limit)
        self.rates[self.limit] = ((old[0] + rate) / 2 if old and now - old[1] < STALE_S else rate, now)
        fresh = lambda n: n in self.rates and now - self.rates[n][1] < STALE_S
        for n in (self.limit + 1, self.limit - 1):        # a neighbour never measured (or long ago): try it
            if self.lo <= n <= self.hi and not fresh(n):
                self.limit = n
                return n
        best = self.limit
        for n in (self.limit - 1, self.limit + 1):
            if fresh(n) and self.rates[n][0] > self.rates[best][0] * BETTER:
                best = n
        self.limit = best
        return best


class Gate:
    """At most `limit` holders at once; the limit can change while requests wait. One per event loop."""

    def __init__(self, limit: int, tuner: "Tuner" = None):
        self.limit = limit
        self.tuner = tuner
        self.active = 0
        self.waiters = collections.deque()
        self._win_start = time.monotonic()
        self._win_done = 0
        self._win_busy = False

    async def acquire(self):
        if self.active < self.limit and not self.waiters:
            self.active += 1
            return
        self._win_busy = True
        fut = asyncio.get_running_loop().create_future()
        self.waiters.append(fut)
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():        # it was handed a slot just as it gave up: pass it on
                self.release()
            else:
                try:
                    self.waiters.remove(fut)
                except ValueError:
                    pass
            raise

    def release(self):
        self.active -= 1
        self._win_done += 1
        self._tick()
        self._wake()

    def set_limit(self, n: int):
        self.limit = max(1, n)
        self._wake()

    def _wake(self):
        while self.waiters and self.active < self.limit:
            fut = self.waiters.popleft()
            if not fut.done():
                self.active += 1
                fut.set_result(None)

    def _tick(self):
        if self.tuner is None:
            return
        now = time.monotonic()
        span = now - self._win_start
        if span < WINDOW_S:
            return
        if self._win_busy and self._win_done >= MIN_PAGES:
            self.set_limit(self.tuner.observe(self._win_done / span, now))
        self._win_start, self._win_done, self._win_busy = now, 0, bool(self.waiters)
