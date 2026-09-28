"""Tests for AI marker generation on file-based (Leaflet) maps."""
import json
import time
from pathlib import Path

import pytest

from app import main as main_module
from app import http_perf

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture
def map_env(tmp_path, monkeypatch, seed):
    maps_dir = tmp_path / "maps"
    maps_dir.mkdir()
    monkeypatch.setattr(main_module, "_MAPS_DIR", maps_dir)
    monkeypatch.setattr(main_module, "BASE_DIR", tmp_path)
    (tmp_path / "static" / "maps").mkdir(parents=True)
    http_perf._MAP_JSON_CACHE.clear()
    jf = maps_dir / "docks.json"
    jf.write_text(json.dumps({
        "world_id": seed.world_a.id, "name": "The Docks", "width": 2000, "height": 1000,
        "markers": [{"name": "Pier One"}],
    }), encoding="utf-8")
    return jf


def _patch_gen(monkeypatch, reply):
    from app import ai as ai_module

    async def fake_generate_chat(messages, **kw):
        return reply(messages, kw)

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)


def _start(client, payload):
    # ?w= pins the world: the active_world cookie set client-side doesn't
    # reliably override the one the login redirect sets in the jar, and
    # resolve_world_slug gives the query param precedence anyway.
    return client.post("/api/maps/docks/ai-markers/start?w=world-a", json=payload)


def _poll(client, job_id, seconds=10):
    deadline = time.time() + seconds
    data = {"status": "running"}
    while time.time() < deadline:
        data = client.get("/api/maps/docks/ai-markers/{}".format(job_id)).json()
        if data["status"] != "running":
            break
        time.sleep(0.05)
    return data


def test_start_requires_gm(client, seed, map_env):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert _start(client, {"prompt": "smugglers"}).status_code == 403


def test_start_404s_unknown_slug(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/maps/nope/ai-markers/start", json={"prompt": "x"})
    assert r.status_code == 404


def test_start_requires_prompt(client, seed, map_env):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert _start(client, {}).status_code == 400


def test_flow_clamps_coords_dedupes_and_caps(client, seed, map_env, monkeypatch):
    def reply(messages, kw):
        assert kw.get("think") is True  # thinking defaults on
        content = messages[0]["content"]
        assert "2000x1000" in content          # map bounds in the prompt
        assert "pier one" in content           # existing labels listed (lowercased for the dedupe check)
        return ('{"markers": ['
                '{"label": "Pier One", "note": "dup", "lat": 10, "lng": 10},'
                '{"label": "Smuggler Hole", "note": "crate stash", "lat": 5000, "lng": -20, "color": "red"},'
                '{"label": "Lookout", "note": "watch post", "lat": 400, "lng": 900, "color": "#44ff88"}'
                ']}')

    _patch_gen(monkeypatch, reply)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _start(client, {"prompt": "smuggler spots", "count": 5})
    assert r.status_code == 200, r.text
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "done", data
    markers = data["markers"]
    # "Pier One" deduped; out-of-bounds coords clamped; bad color defaulted
    labels = [m["label"] for m in markers]
    assert "Pier One" not in labels
    assert {"Smuggler Hole", "Lookout"} == set(labels)
    hole = next(m for m in markers if m["label"] == "Smuggler Hole")
    assert hole["lat"] == 1000 and hole["lng"] == 0      # clamped into bounds
    assert hole["color"] == "#ff4466"                     # invalid color defaulted
    assert hole["note"] == "crate stash"


def test_error_path(client, seed, map_env, monkeypatch):
    def reply(messages, kw):
        return "the model rambled without any JSON"

    _patch_gen(monkeypatch, reply)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _start(client, {"prompt": "anything"})
    assert r.status_code == 200
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "error"
    assert data["error"]


def test_poll_unknown_job_404(client, seed, map_env):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/maps/docks/ai-markers/999999").status_code == 404

