"""A request must never hold two database connections at once (performance plan, step 1).

Rendering a page used to open a second session for the template context processor, `saved_view` and the site layout while the route's own
session was still checked out; with enough renders in flight every worker thread held one connection and waited for another, until the pool
timeout. The route's session now rides on `request.state.db` and everything that runs during the render reads through it."""
import threading

from sqlalchemy import event

from app.database import engine

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


class _Depth:
    """The most connections any single thread had checked out at the same moment."""

    def __init__(self):
        self.local = threading.local()
        self.max = 0

    def _out(self, *a):
        self.local.n = getattr(self.local, "n", 0) + 1
        self.max = max(self.max, self.local.n)

    def _in(self, *a):
        self.local.n = max(0, getattr(self.local, "n", 0) - 1)

    def __enter__(self):
        event.listen(engine, "checkout", self._out)
        event.listen(engine, "checkin", self._in)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "checkout", self._out)
        event.remove(engine, "checkin", self._in)


PAGES = ["/", "/rules", "/facts", "/sessions", "/maps", "/calendar", "/dice", "/tables", "/account", "/quests", "/search?q=a"]


def test_a_page_render_holds_one_connection_for_gm_and_player(client, seed):
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        client.cookies.clear()
        login(client, email, pw)
        client.cookies.set("active_world", seed.world_a.slug)
        client.get("/")                                    # warm: the first render of a process fills its caches
        with _Depth() as depth:
            for path in PAGES:
                client.get(path)
        assert depth.max <= 1, f"{email} held {depth.max} connections at once"


def test_the_pool_is_sized_for_the_worker_threads_and_fails_fast():
    assert engine.pool.size() + engine.pool._max_overflow >= 40 and engine.pool.size() <= 10   # 40 worker threads, one connection each; 10 kept idle
    assert engine.pool._timeout <= 10                                           # a stall is an error in seconds, not a 30 s hang


def test_saved_layouts_are_read_with_one_query_per_page(client, seed):
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/view-prefs/rules", json={"mode": "book"})
    client.get("/rules")
    seen = []

    def on_exec(conn, cur, statement, *a):
        if "user_view_prefs" in statement:
            seen.append(statement)
    event.listen(engine, "before_cursor_execute", on_exec)
    try:
        client.get("/rules")
    finally:
        event.remove(engine, "before_cursor_execute", on_exec)
    assert len(seen) <= 1, seen
