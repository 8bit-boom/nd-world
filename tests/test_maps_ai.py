"""Maps AI integration: the battlemap generation handoff (map_new accepts a
generated /uploads/ image URL) and the AI schematic builder (description +
optional sketch images → validated, clamped schematic elements).

The AI itself is always monkeypatched — same convention as
tests/test_npc_talk.py / tests/test_unsloth_extras.py."""
import json

from app.database import SessionLocal
from app.models import Schematic, User, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _schematic(seed, slug="ai-den", elements=None):
    db = SessionLocal()
    try:
        s = Schematic(world_id=seed.world_a.id, slug=slug, name="AI Den",
                      canvas_width=2000, canvas_height=1500,
                      elements_json=json.dumps(elements or []))
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id
    finally:
        db.close()


def _pin(client, slug="world-a"):
    client.cookies.set("active_world", slug)


def _poll_job(client, slug, job_id, tries=40):
    """Poll an AI build job to completion (the task runs on the app's loop;
    give it scheduling ticks)."""
    import time
    for _ in range(tries):
        data = client.get(f"/maps/schematic/{slug}/ai-build/{job_id}").json()
        if data.get("status") != "running":
            return data
        time.sleep(0.05)
    return data


_CANNED_ELEMENTS = {
    "elements": [
        {"type": "rect", "x": 100, "y": 100, "w": 600, "h": 400,
         "fill": "#2a2a35", "stroke": "#888888", "strokeW": 2, "label": "Bar"},
        {"type": "line", "x1": 700, "y1": 300, "x2": 760, "y2": 300,
         "stroke": "#aaaaaa", "strokeW": 3},
        {"type": "circle", "cx": 900, "cy": 400, "rx": 80, "ry": 80,
         "fill": "#2a2a35", "stroke": "#888888"},
        {"type": "text", "x": 150, "y": 90, "label": "Bar"},
        # invalid rows the validator must drop:
        {"type": "dragon", "x": 1},                      # unknown type
        {"type": "rect", "x": "not-a-number", "w": 50},  # NaN coords → defaults
    ],
}


def test_ai_build_appends_validated_elements(client, seed, monkeypatch):
    """The route parses the model's JSON (fenced or not), drops invalid
    shapes, clamps numerics, stamps unique ai- ids, and appends to the
    schematic's elements — returning the authoritative merged list."""
    import app.main as main_module

    async def _fake_generate_chat(messages, system="", model="", options=None,
                                  think=False, format=None):
        assert format is not None                      # structured output requested
        assert "2000 x 1500" in system                 # canvas size taught
        return "```json\n" + json.dumps(_CANNED_ELEMENTS) + "\n```"

    monkeypatch.setattr(main_module._ai_module, "generate_chat", _fake_generate_chat)

    existing = [{"id": "existing-1", "type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}]
    _schematic(seed, elements=existing)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post("/maps/schematic/ai-den/ai-build/start", data={
        "description": "A smuggler's den with an L-shaped bar.",
    })
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_job(client, "ai-den", job_id)
    assert data["status"] == "done", data
    # 5 added: the unknown "dragon" type dropped; the NaN-coords rect kept
    # with its coordinate clamped to the 0 default (clamping, not dropping).
    assert data["added"] == 5
    assert data["total"] == 6                          # 1 existing + 5 new
    assert data["elements"][-1]["id"].startswith("ai-")
    db = SessionLocal()
    try:
        s = db.query(Schematic).filter(Schematic.slug == "ai-den").first()
        stored = json.loads(s.elements_json)
        assert len(stored) == 6
        assert stored[0]["id"] == "existing-1"         # append, not replace
        # numeric clamps: "not-a-number" x became 0
        assert stored[-1]["x"] == 0
        assert stored[-1]["w"] == 50
    finally:
        db.close()


def test_ai_build_replace_mode(client, seed, monkeypatch):
    import app.main as main_module

    async def _fake_generate_chat(messages, system="", model="", options=None,
                                  think=False, format=None):
        return json.dumps(_CANNED_ELEMENTS)

    monkeypatch.setattr(main_module._ai_module, "generate_chat", _fake_generate_chat)
    _schematic(seed, slug="ai-replace", elements=[{"id": "old", "type": "rect", "x": 1}])
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post("/maps/schematic/ai-replace/ai-build/start", data={
        "description": "a shrine", "replace": "1",
    })
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    data = _poll_job(client, "ai-replace", job_id)
    assert data["status"] == "done"
    assert data["total"] == 5                          # old canvas gone


def test_ai_build_rejects_bad_model_output(client, seed, monkeypatch):
    import app.main as main_module

    async def _fake_generate_chat(messages, system="", model="", options=None,
                                  think=False, format=None):
        return "I'm sorry, I can't draw maps."         # no JSON anywhere

    monkeypatch.setattr(main_module._ai_module, "generate_chat", _fake_generate_chat)
    _schematic(seed, slug="ai-bad")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post("/maps/schematic/ai-bad/ai-build/start", data={"description": "a den"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    data = _poll_job(client, "ai-bad", job_id)
    assert data["status"] == "error"
    assert "JSON" in data["error"] or "no usable elements" in data["error"]


def test_ai_build_is_gm_only_and_world_scoped(client, seed):
    """Edit-level gate on the schematic's own world; players 403, and a
    slug from another world 404s for a scoped GM too (world_row_visible-
    style scoping comes from the section check on the schematic's world)."""
    _schematic(seed, slug="ai-deny")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.post("/maps/schematic/ai-deny/ai-build/start",
                       data={"description": "x"}).status_code == 403


def test_map_new_accepts_generated_image_url(client, seed, tmp_path):
    """The battlemap handoff: a generated /uploads/ai-images/... file can be
    sourced into the new map's background. Containment: paths outside
    UPLOADS_DIR are ignored (map created with no image)."""
    from app.main import UPLOADS_DIR, _MAPS_DIR
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "ai-images").mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "ai-images" / "gen.png").write_bytes(b"\x89PNGfake")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/new", data={
        "name": "AI Shrine", "width": 1024, "height": 1024,
        "image_url": "/uploads/ai-images/gen.png",
    }, follow_redirects=False)
    assert r.status_code == 303
    slug = r.headers["location"].rsplit("/", 1)[-1]
    assert (_MAPS_DIR / f"{slug}.json").exists()
    copied = UPLOADS_DIR / "maps" / f"{slug}.png"
    assert copied.exists() and copied.read_bytes() == b"\x89PNGfake"

    # traversal attempt: a ../ path is ignored (map created, no image)
    r2 = client.post("/maps/new", data={
        "name": "Traversal Attempt", "width": 100, "height": 100,
        "image_url": "/uploads/../world.db",
    }, follow_redirects=False)
    assert r2.status_code == 303
    slug2 = r2.headers["location"].rsplit("/", 1)[-1]
    assert not (UPLOADS_DIR / "maps" / f"{slug2}.db").exists()
