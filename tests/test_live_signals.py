"""Named live signals (efficiency plan, step 4): the spotlight and the dice log are announced over /api/live instead of polled every 4-5 s."""
import asyncio

from app import live


def _frames(world_id, actions, n_ticks=3):
    async def run():
        out, gen = [], live.stream_world_events(world_id)
        out.append(await gen.__anext__())                        # retry:
        out.append(await gen.__anext__())                        # the first version frame
        for act in actions:
            act()
            out.append(await asyncio.wait_for(gen.__anext__(), 3))
        await gen.aclose()
        return out
    return asyncio.run(run())


def test_a_world_signal_and_an_every_world_signal_reach_the_stream(monkeypatch):
    monkeypatch.setattr(live, "_POLL_EVERY", 0.01)
    frames = _frames(91, [lambda: live.signal(91, "dice"), lambda: live.signal_all("spot")])
    assert frames[2] == "event: signal\ndata: dice\n\n" and frames[3] == "event: signal\ndata: spot\n\n"


def test_another_worlds_signal_is_not_sent(monkeypatch):
    monkeypatch.setattr(live, "_POLL_EVERY", 0.01)
    monkeypatch.setattr(live, "_HEARTBEAT_EVERY", 0.02)
    frames = _frames(92, [lambda: live.signal(93, "dice")])
    assert frames[2] == ": keep-alive\n\n"                        # nothing for world 92, just the heartbeat


def test_clearing_the_spotlight_cache_announces_it():
    from app import main
    before = live.signals(1).get("spot", (0, 0))[0]
    main._spotlight_cache.clear()
    assert live.signals(1)["spot"][0] == before + 1


def test_pages_poll_slowly_and_listen_for_the_signal():
    base = open("app/templates/base.html").read()
    dice = open("app/templates/dice.html").read()
    assert "ndPoll(pollSpotlight, 60000)" in base and "nd-signal" in base
    assert "ndPoll(refresh, 60000)" in dice and "'dice'" in dice
    assert "es.addEventListener('signal'" in open("static/js/nd-live.js").read()


def test_a_roll_signals_the_dice_log(client, seed):
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    before = live.signals(seed.world_a.id).get("dice", (0, 0))[1]
    assert client.post("/api/dice/roll", json={"notation": "1d20"}).status_code == 200
    from .conftest import wait_until
    assert wait_until(lambda: live.signals(seed.world_a.id).get("dice", (0, 0))[1] == before + 1)
