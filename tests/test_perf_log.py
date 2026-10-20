"""Per-request timing (performance plan, step 10): what is recorded, what must never be, the GM-only endpoints, and the render_md cache."""
from app import perf_log, rendering

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_requests_are_recorded_by_route_template_with_query_counts(client, seed):
    perf_log.reset()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.get("/rules?secret=hunter2")
    rows = [r for r in perf_log.recent(50) if r["route"] == "/rules"]
    assert rows and rows[-1]["status"] == 200 and rows[-1]["queries"] >= 1 and rows[-1]["ms"] > 0
    blob = repr(perf_log.recent(1000))
    assert "hunter2" not in blob and seed.gm.email not in blob          # no query strings, no names, no cookies
    client.get("/static/style.css")
    assert not any(r["route"].startswith("/static") for r in perf_log.recent(1000))


def test_ids_in_a_path_are_recorded_as_the_route_not_the_url(client, seed):
    perf_log.reset()
    login(client, seed.gm.email, GM_PASSWORD)
    client.get("/entity/987654")
    routes = {r["route"] for r in perf_log.recent(50)}
    assert "/entity/{entity_id}" in routes or "/entity/{n}" in routes
    assert not any("987654" in r for r in routes)


def test_summary_reports_percentiles_and_the_worst_query_count():
    perf_log.reset()
    for ms, q in ((10, 2), (20, 3), (30, 9), (500, 4)):
        perf_log._ring.append({"t": 0, "method": "GET", "route": "/x", "status": 200, "ms": ms, "queries": q, "db_ms": 1.0})
    perf_log._ring.append({"t": 0, "method": "GET", "route": "/x", "status": 500, "ms": 40, "queries": 1, "db_ms": 1.0})
    s = {r["route"]: r for r in perf_log.summary()}["/x"]
    assert s["calls"] == 5 and s["max_ms"] == 500 and s["max_queries"] == 9 and s["errors"] == 1 and s["p50_ms"] == 30
    perf_log.reset()


def test_the_timing_endpoints_are_gm_only(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/perf/summary")
    assert r.status_code == 200 and "routes" in r.json() and r.headers["cache-control"] == "no-store"
    assert client.get("/api/perf/recent?limit=5").status_code == 200
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/perf/summary").status_code == 403
    assert client.get("/api/perf/recent").status_code == 403


def test_render_md_is_cached_but_stays_a_pure_function():
    rendering._render_md_cached.cache_clear()
    a = rendering.render_md("**bold** and <b>raw</b>")
    b = rendering.render_md("**bold** and <b>raw</b>")
    assert a == b and "<strong>bold</strong>" in a and "<b>raw</b>" not in a          # raw HTML is still escaped
    assert rendering._render_md_cached.cache_info().hits == 1
    assert rendering.render_md("") == "" and rendering.render_md(None) == ""
    big = "x" * 30000
    rendering.render_md(big)
    assert rendering._render_md_cached.cache_info().currsize == 1                      # very long texts bypass the cache
