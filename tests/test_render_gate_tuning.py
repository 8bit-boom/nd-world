"""The render gate's self-tuning limit (app/render_gate.py): it tries the neighbours of the current limit under load, moves only to a
clearly faster one, re-checks stale measurements, stays in its bounds; a fixed value in .env or Settings turns it off."""
import asyncio

from app import render_gate, workers


def _settle(tuner, rate_of, start=0.0, rounds=20):
    t = start
    for _ in range(rounds):
        tuner.observe(rate_of(tuner.limit), t)
        t += 6
    return tuner.limit, t


def test_it_climbs_while_more_is_faster_and_stops_at_the_best():
    curve = {1: 10, 2: 20, 3: 26, 4: 27, 5: 22, 6: 15}          # 4 is best; 3 -> 4 is under the 5 % bar from 4's side
    limit, _ = _settle(render_gate.Tuner(hi=6), lambda n: curve[n])
    assert limit in (3, 4)
    curve2 = {1: 10, 2: 20, 3: 30, 4: 40, 5: 50, 6: 60}
    assert _settle(render_gate.Tuner(hi=6), lambda n: curve2[n])[0] == 6, "never above the upper bound"


def test_it_stays_at_two_where_two_is_best():
    curve = {1: 15, 2: 28, 3: 20, 4: 9}                            # the 4-core measurement: more at once is slower
    assert _settle(render_gate.Tuner(hi=4), lambda n: curve[n])[0] == 2


def test_it_follows_a_change_after_the_measurements_go_stale():
    curve = {1: 10, 2: 28, 3: 20, 4: 9}
    tuner = render_gate.Tuner(hi=4)
    limit, t = _settle(tuner, lambda n: curve[n])
    assert limit == 2
    curve.update({3: 40, 4: 45})                                   # heavier pages now gain from more at once
    limit, _ = _settle(tuner, lambda n: curve[n], start=t + render_gate.STALE_S + 1, rounds=40)
    assert limit in (3, 4)


def test_the_gate_lets_in_only_the_limit_and_takes_a_new_one_at_once():
    async def go():
        gate = render_gate.Gate(2)
        inside, peak = 0, 0
        release = asyncio.Event()

        async def page():
            nonlocal inside, peak
            await gate.acquire()
            inside += 1
            peak = max(peak, inside)
            await release.wait()
            inside -= 1
            gate.release()
        tasks = [asyncio.create_task(page()) for _ in range(6)]
        await asyncio.sleep(0.05)
        assert peak == 2 and len(gate.waiters) == 4
        gate.set_limit(4)
        await asyncio.sleep(0.05)
        assert peak == 4
        release.set()
        await asyncio.gather(*tasks)
        assert gate.active == 0 and not gate.waiters
    asyncio.run(go())


def test_a_waiter_that_gives_up_frees_its_place():
    async def go():
        gate = render_gate.Gate(1)
        await gate.acquire()
        t = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0.01)
        t.cancel()
        await asyncio.sleep(0.01)
        gate.release()
        assert gate.active == 0 and not gate.waiters
        await asyncio.wait_for(gate.acquire(), 1)
    asyncio.run(go())


def test_fixed_values_turn_the_tuning_off(monkeypatch):
    monkeypatch.delenv("ND_RENDERS", raising=False)
    assert not workers.render_slots_fixed() and workers.render_slots_max(16) == 8 and workers.render_slots_max(2) == 2
    try:
        workers.apply(render_slots=3)
        assert workers.render_slots_fixed() and workers.render_slots() == 3
    finally:
        workers.apply()
    monkeypatch.setenv("ND_RENDERS", "4")
    assert workers.render_slots_fixed() and workers.render_slots() == 4
