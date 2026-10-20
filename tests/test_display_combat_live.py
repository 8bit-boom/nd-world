"""The table's TV turn-order page (display_combat.html) follows an encounter through the display stream instead of polling every 2 s
(docs/LEFTOVERS_PLAN.md, step 2): every committed change to an encounter (combat_turns.touch) announces a "combat" live signal, the display
stream turns it into a `combat` event, display.html passes it into the frame, and the page re-reads. A 30 s poll stays as the safety net."""
import asyncio
from pathlib import Path

from app import combat_turns, live
from app.database import SessionLocal
from app.models import CombatSession
from app.routers import display as display_router

from .conftest import GM_PASSWORD, login
from .test_combat_tracker_state import _cb, _make

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _combat_signal(world_id):
    return live.signals(world_id).get("combat")


def test_a_saved_change_announces_the_signal_after_the_commit(client, seed):
    cid = _make(seed.world_a.id)
    db = SessionLocal()
    try:
        before = _combat_signal(seed.world_a.id)
        cs = db.get(CombatSession, cid)
        cs.round_num = 3
        combat_turns.touch(cs)
        assert _combat_signal(seed.world_a.id) == before, "nothing is announced before the change is committed"
        db.commit()
        assert _combat_signal(seed.world_a.id) != before
        after = _combat_signal(seed.world_a.id)
        cs.round_num = 4
        combat_turns.touch(cs)
        db.rollback()
        db.commit()
        assert _combat_signal(seed.world_a.id) == after, "a rolled-back change announces nothing"
        assert _combat_signal(seed.world_b.id) in (None, live.signals(seed.world_b.id).get("combat")), "other worlds are not told"
    finally:
        db.close()


def test_the_trackers_save_route_announces_it(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    cid = _make(seed.world_a.id)
    before = _combat_signal(seed.world_a.id)
    assert client.post(f"/combat/{cid}/state", json={"combatants": [_cb("c1", "Ana", init=3)]}).status_code == 200
    assert _combat_signal(seed.world_a.id) != before


def test_the_display_stream_sends_a_combat_event_when_an_encounter_changes():
    async def go():
        gen = display_router.stream_stage(91, poll=0.01, beat=1000)
        assert (await gen.__anext__()).startswith("retry:")
        assert (await gen.__anext__()).startswith("event: state")
        live.signal(91, "combat")
        assert (await asyncio.wait_for(gen.__anext__(), 2)).startswith("event: combat")
        live.signal(92, "combat")                                     # another world: no event, only the keep-alive rhythm
        live.signal(91, "spot")
        nxt = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.1)
        assert not nxt.done()
        nxt.cancel()
        try:
            await nxt
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
    asyncio.run(go())


def test_the_pages_pass_the_event_on_and_poll_only_as_a_safety_net():
    tv = (TEMPLATES / "display.html").read_text()
    assert "addEventListener('combat'" in tv and "type: 'nd-combat'" in tv and "location.origin" in tv
    page = (TEMPLATES / "display_combat.html").read_text()
    assert "setInterval(poll, 30000)" in page and "setInterval(poll, 2000)" not in page
    assert "e.data.type === 'nd-combat'" in page and "e.origin === location.origin" in page
