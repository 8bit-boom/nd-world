"""The page-render gate in app.http_perf.PerfMiddleware (performance plan, step 4): at most RENDER_SLOTS HTML page requests are being built at once,
a slot is given back when the response STARTS, and nothing else (APIs, uploads, static files, non-GET) is held up by it."""
import asyncio

from app import http_perf


def _scope(path="/", method="GET", accept="text/html,*/*"):
    return {"type": "http", "path": path, "method": method, "headers": [(b"accept", accept.encode())], "query_string": b""}


async def _drive(app, scopes):
    mw = http_perf.PerfMiddleware(app)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass
    await asyncio.gather(*[mw(s, receive, send) for s in scopes])


class _Probe:
    """An app that takes `work` seconds before its headers go out, then `tail` seconds to finish the body; records how many were busy at once."""

    def __init__(self, work=0.05, tail=0.0):
        self.work, self.tail, self.busy, self.peak, self.tail_busy, self.tail_peak = work, tail, 0, 0, 0, 0

    async def __call__(self, scope, receive, send):
        self.busy += 1
        self.peak = max(self.peak, self.busy)
        await asyncio.sleep(self.work)
        self.busy -= 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        self.tail_busy += 1
        self.tail_peak = max(self.tail_peak, self.tail_busy)
        await asyncio.sleep(self.tail)
        await send({"type": "http.response.body", "body": b"x"})
        self.tail_busy -= 1


def test_only_a_few_page_renders_run_at_once():
    probe = _Probe()
    asyncio.run(_drive(probe, [_scope("/")] * 12))
    assert 1 <= probe.peak <= http_perf.RENDER_SLOTS


def test_api_calls_uploads_and_non_get_requests_are_not_gated():
    for scope in (_scope("/api/dice/history", accept="application/json"), _scope("/uploads/a.png", accept="text/html"), _scope("/api/x", accept="text/html"),
                  _scope("/save", method="POST"), _scope("/data", accept="*/*")):
        probe = _Probe()
        asyncio.run(_drive(probe, [scope] * 8))
        assert probe.peak == 8, scope["path"]


def test_a_slot_is_free_again_once_the_response_has_started():
    probe = _Probe(work=0.01, tail=0.2)                        # a long body must not keep other pages waiting
    asyncio.run(_drive(probe, [_scope("/")] * 8))
    assert probe.tail_peak > http_perf.RENDER_SLOTS


def test_a_failing_request_gives_its_slot_back():
    class Boom:
        async def __call__(self, scope, receive, send):
            raise RuntimeError("boom")

    async def run():
        mw = http_perf.PerfMiddleware(Boom())

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            pass
        for _ in range(http_perf.RENDER_SLOTS + 3):
            try:
                await mw(_scope("/"), receive, send)
            except RuntimeError:
                pass
    asyncio.run(asyncio.wait_for(run(), 5))                    # would hang forever if a slot leaked
