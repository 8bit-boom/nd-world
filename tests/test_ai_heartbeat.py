"""Unit test for _with_heartbeat (app/routers/ai.py) in isolation, without
going through the whole HTTP stack — the route wires it up with a 12s
interval to dodge Cloudflare's 524 "origin timeout" while Ollama is slow to
produce a first token; here we use a tiny interval so the test stays fast.
"""
import asyncio

import pytest

from app.routers.ai import _with_heartbeat


@pytest.mark.asyncio
async def test_with_heartbeat_pings_while_waiting_then_forwards_items():
    async def slow_gen():
        await asyncio.sleep(0.05)
        yield "first"
        yield "second"

    results = [item async for item in _with_heartbeat(slow_gen(), interval=0.01)]

    assert None in results  # at least one heartbeat fired before "first" arrived
    assert results[-2:] == ["first", "second"]


@pytest.mark.asyncio
async def test_with_heartbeat_no_pings_for_a_fast_generator():
    async def fast_gen():
        yield "a"
        yield "b"

    results = [item async for item in _with_heartbeat(fast_gen(), interval=5.0)]
    assert results == ["a", "b"]
