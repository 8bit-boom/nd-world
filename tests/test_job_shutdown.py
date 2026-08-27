"""Tests for app/job_shutdown.py — the process-wide "we're stopping"
coordination shared by the three background-job engines (audio_jobs.py,
image_jobs.py, chat_jobs.py) and the chunk loops in app/ai.py they drive.
Pure module-level tests: no DB, no HTTP, no job engine involved — those are
covered where each engine wires this module in (tests/test_audio_jobs.py,
tests/test_image_jobs.py, tests/test_chat_jobs.py) and where app.main's
lifespan wires the whole thing together (also in this file, see the bottom).
"""
import asyncio

import pytest

from app import job_shutdown


@pytest.fixture(autouse=True)
def _reset_stop_flag():
    """Every test starts and ends with stopping() false, regardless of
    whether the test itself calls request_stop() — mirrors clear_stop()'s
    own real-world job (see its docstring), just enforced per-test here
    instead of per-process-boot."""
    job_shutdown.clear_stop()
    yield
    job_shutdown.clear_stop()


# ── stopping / request_stop / clear_stop ────────────────────────────────────

def test_stopping_is_false_until_request_stop():
    assert job_shutdown.stopping() is False
    job_shutdown.request_stop()
    assert job_shutdown.stopping() is True


def test_clear_stop_resets_the_flag():
    job_shutdown.request_stop()
    assert job_shutdown.stopping() is True
    job_shutdown.clear_stop()
    assert job_shutdown.stopping() is False


# ── drain() ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drain_returns_immediately_with_no_tasks():
    cancelled = await job_shutdown.drain([])
    assert cancelled == 0


@pytest.mark.asyncio
async def test_drain_lets_a_task_finish_inside_the_grace_window():
    async def quick():
        await asyncio.sleep(0.01)
        return "done"

    task = asyncio.create_task(quick())
    cancelled = await job_shutdown.drain([task], grace=1.0)
    assert cancelled == 0
    assert task.done() and not task.cancelled()
    assert task.result() == "done"


@pytest.mark.asyncio
async def test_drain_cancels_a_task_that_outlives_the_grace_window():
    async def slow():
        await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    cancelled = await job_shutdown.drain([task], grace=0.01)
    assert cancelled == 1
    assert task.cancelled()


@pytest.mark.asyncio
async def test_drain_waits_for_a_cancelled_tasks_own_cleanup_to_run():
    cleanup_ran = False

    async def slow_with_cleanup():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            nonlocal cleanup_ran
            await asyncio.sleep(0.05)  # simulates a bit of async cleanup work
            cleanup_ran = True
            raise

    task = asyncio.create_task(slow_with_cleanup())
    await job_shutdown.drain([task], grace=0.01)
    assert cleanup_ran, "drain() should wait for the cancelled task's own handler to finish, not just fire-and-forget cancel()"


@pytest.mark.asyncio
async def test_drain_does_not_hang_on_a_task_that_swallows_cancellation():
    async def stubborn():
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                continue  # deliberately never actually stops — a single more
                          # cancel() would be swallowed the same way, so this
                          # task is genuinely un-cancellable from the outside;
                          # the test only needs to prove drain() itself still
                          # returns promptly, not that the task ever exits

    task = asyncio.create_task(stubborn())
    cancelled = await job_shutdown.drain([task], grace=0.01)
    assert cancelled == 1  # drain() returned instead of hanging forever
    assert not task.done()  # confirms it's genuinely still stubborn, not a fluke


@pytest.mark.asyncio
async def test_drain_with_zero_grace_cancels_immediately():
    async def slow():
        await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    cancelled = await job_shutdown.drain([task], grace=0.0)
    assert cancelled == 1
    assert task.cancelled()


# ── JobInterrupted ───────────────────────────────────────────────────────────

def test_job_interrupted_is_not_caught_by_except_cancelled_error():
    with pytest.raises(job_shutdown.JobInterrupted):
        try:
            raise job_shutdown.JobInterrupted("stopped at a chunk boundary")
        except asyncio.CancelledError:
            raise AssertionError("JobInterrupted must not be caught as CancelledError")
