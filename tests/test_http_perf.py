"""Tests for the HTTP-layer performance middlewares (app/http_perf.py) and
the maps-JSON cache: gzip negotiation, the SSE no-gzip carve-out, immutable
/static caching, and mtime-keyed invalidation."""
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import http_perf
from app.http_perf import map_json_cached


def test_gzip_negotiated_for_static(client):
    r = client.get("/static/js/cockpit.js", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    # and the immutable cache header rides along on the compressed response
    assert "immutable" in r.headers.get("cache-control", "")


def test_static_cache_control_header(client):
    r = client.get("/static/js/cockpit.js")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "public" in cc and "max-age=31536000" in cc and "immutable" in cc


def test_non_static_paths_get_no_immutable_header(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "immutable" not in r.headers.get("cache-control", "")


def test_sse_paths_are_not_gzipped():
    """The carve-out's whole job: SSE responses must stream UNcompressed
    even when the client advertises gzip. Uses a finite stream on the
    actual /api/live path — a finite SSE response exercises the identical
    middleware decision without deadlocking the TestClient on an infinite
    generator."""
    from fastapi import FastAPI
    from fastapi.middleware.gzip import GZipMiddleware
    from fastapi.responses import StreamingResponse
    from starlette.testclient import TestClient

    from app.http_perf import PerfMiddleware

    app = FastAPI()

    @app.get("/api/live")
    async def live():
        def frames():
            nl = chr(10)
            yield "retry: 3000" + nl + nl
            yield "event: version" + nl + "data: 0" + nl + nl
        return StreamingResponse(frames(), media_type="text/event-stream")

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(PerfMiddleware)
    c = TestClient(app)
    with c.stream("GET", "/api/live", headers={"accept-encoding": "gzip"}) as r:
        assert r.status_code == 200
        assert r.headers.get("content-encoding", "") != "gzip"
        body = b"".join(r.iter_bytes()).decode()
    assert "retry" in body and "event: version" in body


def test_stream_suffix_paths_also_carved_out():
    """Every SSE route ends in /stream (AI chat, NPC talk) — same carve-out
    by suffix."""
    from fastapi import FastAPI
    from fastapi.middleware.gzip import GZipMiddleware
    from fastapi.responses import StreamingResponse
    from starlette.testclient import TestClient

    from app.http_perf import PerfMiddleware

    app = FastAPI()

    @app.post("/api/ai/stream")
    async def stream():
        def frames():
            yield "data: [DONE]" + chr(10) + chr(10)
        return StreamingResponse(frames(), media_type="text/event-stream")

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(PerfMiddleware)
    c = TestClient(app)
    with c.stream("POST", "/api/ai/stream", headers={"accept-encoding": "gzip"}) as r:
        assert r.status_code == 200
        assert r.headers.get("content-encoding", "") != "gzip"


def test_html_gzipped(client):
    r = client.get("/login", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


# ── maps JSON cache ──────────────────────────────────────────────────────────

def test_map_json_cache_roundtrip_and_invalidation(tmp_path, monkeypatch):
    from app import main as main_module

    maps_dir = tmp_path / "maps"
    maps_dir.mkdir()
    monkeypatch.setattr(main_module, "_MAPS_DIR", maps_dir)
    http_perf._MAP_JSON_CACHE.clear()

    jf = maps_dir / "yorm.json"
    jf.write_text(json.dumps({"world_id": 1, "name": "Yorm", "markers": [{"n": 1}]}),
                  encoding="utf-8")

    first = main_module._map_data(jf)
    assert first and first["name"] == "Yorm"
    # cached second read
    assert main_module._map_data(jf) is first

    # an edit (name change + mtime bump) must bust the cache — a stale map
    # would be a correctness bug, not just a perf miss
    time.sleep(0.01)
    jf.write_text(json.dumps({"world_id": 1, "name": "Yorm Renamed", "markers": []}),
                  encoding="utf-8")
    updated = main_module._map_data(jf)
    assert updated["name"] == "Yorm Renamed"
    assert updated is not first

    # a deleted file reads as None, like _map_data's original contract
    jf.unlink()
    assert main_module._map_data(jf) is None
    assert map_json_cached(jf) is None
    http_perf._MAP_JSON_CACHE.clear()


def test_map_iter_world_maps_uses_cache(client, seed, tmp_path, monkeypatch):
    """_iter_world_maps (the /maps page) benefits from the same cache."""
    from app import main as main_module

    maps_dir = tmp_path / "maps"
    maps_dir.mkdir()
    monkeypatch.setattr(main_module, "_MAPS_DIR", maps_dir)
    monkeypatch.setattr(main_module, "BASE_DIR", tmp_path)  # static/maps probe dir
    (tmp_path / "static" / "maps").mkdir(parents=True)
    http_perf._MAP_JSON_CACHE.clear()

    jf = maps_dir / "city.json"
    jf.write_text(json.dumps({"world_id": seed.world_a.id, "name": "City"}),
                  encoding="utf-8")
    got = list(main_module._iter_world_maps(seed.world_a.id))
    assert len(got) == 1 and got[0][1]["name"] == "City"
    # second call served from cache — same parsed object
    again = list(main_module._iter_world_maps(seed.world_a.id))
    assert again[0][1] is got[0][1]
    http_perf._MAP_JSON_CACHE.clear()
