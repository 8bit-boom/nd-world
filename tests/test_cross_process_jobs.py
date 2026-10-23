"""The groundwork for the gateway's own background jobs (docs/RUST_JOBS.md): one AI slot for both processes (the `ai_slot` lease in
shared_state) and job rows the gateway holds (`jobowner:<table>:<id>`), which this process's sweeps leave alone until the gateway has
been silent for ADOPT_AFTER_SECONDS."""
import asyncio
import json
import time

import pytest

from app import ai_queue, job_owner, shared_state


def _put(key, value):
    shared_state.update_json(key, lambda old: value)


@pytest.fixture(autouse=True)
def _clean():
    ai_queue.queue.reset()
    yield
    ai_queue.queue.reset()
    shared_state.delete_prefix("ai_slot")
    shared_state.delete_prefix(job_owner.PREFIX)


@pytest.mark.asyncio
async def test_a_turn_waits_for_the_gateways_lease_and_hands_it_back(monkeypatch):
    monkeypatch.setattr(ai_queue, "LEASE_POLL_SECONDS", 0.05)
    _put("ai_slot", {"owner": "gateway:1", "until": time.time() + 30, "label": "recap"})
    q = ai_queue.AiQueue()
    started = asyncio.Event()

    async def job():
        async with q.slot("assist"):
            started.set()
            assert shared_state.get_json("ai_slot")["owner"] == ai_queue.LEASE_OWNER
    task = asyncio.create_task(job())
    await asyncio.sleep(0.3)
    assert not started.is_set()                       # the gateway holds the GPU
    _put("ai_slot", {})                                # ... and gives it back
    await asyncio.wait_for(task, 5)
    assert started.is_set()
    assert shared_state.get_json("ai_slot") == {}      # given back when done


@pytest.mark.asyncio
async def test_a_lease_that_ran_out_is_taken_over(monkeypatch):
    monkeypatch.setattr(ai_queue, "LEASE_POLL_SECONDS", 0.05)
    _put("ai_slot", {"owner": "gateway:dead", "until": time.time() - 1})
    q = ai_queue.AiQueue()
    async with q.slot("x"):
        assert shared_state.get_json("ai_slot")["owner"] == ai_queue.LEASE_OWNER


def test_sweeps_leave_the_gateways_jobs_alone_until_it_has_been_silent(seed):
    from app import chat_jobs
    from app.database import SessionLocal
    from app.models import ChatJob
    db = SessionLocal()
    try:
        held = ChatJob(world_id=seed.world_a.id, status="generating", messages_json="[]")
        stale = ChatJob(world_id=seed.world_a.id, status="generating", messages_json="[]")
        mine = ChatJob(world_id=seed.world_a.id, status="generating", messages_json="[]")
        db.add_all([held, stale, mine])
        db.commit()
        ids = (held.id, stale.id, mine.id)
    finally:
        db.close()
    _put(f"jobowner:chat_jobs:{ids[0]}", {"owner": "gateway", "until": time.time() + 60})
    _put(f"jobowner:chat_jobs:{ids[1]}", {"owner": "gateway", "until": time.time() - job_owner.ADOPT_AFTER_SECONDS - 5})
    assert job_owner.gateway_ids("chat_jobs") == {ids[0]}
    chat_jobs.sweep_interrupted_jobs()
    db = SessionLocal()
    try:
        status = {i: db.get(ChatJob, i).status for i in ids}
    finally:
        db.close()
    assert status == {ids[0]: "generating", ids[1]: "interrupted", ids[2]: "interrupted"}, status
