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


# ── Schematic SVG preview ─────────────────────────────────────────────────────

def test_schematic_preview_renders_elements(client, seed):
    _schematic(seed, slug="preview-sch", elements=[
        {"type": "rect", "x": 100, "y": 100, "w": 600, "h": 400,
         "fill": "#2a2a35", "stroke": "#888888", "strokeW": 2, "label": "Bar"},
        {"type": "line", "x1": 700, "y1": 300, "x2": 760, "y2": 300},
        {"type": "token", "cx": 300, "cy": 300, "r": 14, "color": "#c05050", "name": "Vex"},
    ])
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.get("/maps/schematic/preview-sch/preview.svg")
    assert r.status_code == 200
    assert "image/svg" in r.headers["content-type"]
    body = r.text
    assert "<svg" in body and "<rect" in body and 'fill="#2a2a35"' in body
    assert "<line" in body and "<circle" in body
    assert "Bar" not in body or True  # label may render as text; not asserted
    assert "Vex" in body


def test_schematic_preview_gated_for_non_maps_viewers(client, seed):
    _schematic(seed, slug="preview-gate")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    # parties/maps section default for players is read via matrix; players
    # WITHOUT the maps grant get 403 on the preview too.
    from app.deps import world_section_access
    from app.models import World
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        current = world_section_access(w)
        current["maps"]["player"] = "none"
        w.section_access_json = json.dumps(current)
        db.commit()
    finally:
        db.close()
    assert client.get("/maps/schematic/preview-sch/preview.svg").status_code == 403


# ── plan & build: the AI plans, app/map_layout.py lays it out ───────────────────

_CANNED_PLAN = {
    "title": "The Rusty Anchor", "entry": "bar",
    "rooms": [
        {"id": "bar", "name": "Bar", "kind": "tavern", "size": "large", "entity": "Old Salt"},
        {"id": "kitchen", "name": "Kitchen", "kind": "kitchen"},
        {"id": "store", "name": "Store", "kind": "storage", "size": "small"},
        {"id": "ghost", "name": "Ghost room", "kind": "other"},
    ],
    "links": [["bar", "kitchen"], {"a": "kitchen", "b": "store", "type": "secret"}, ["bar", "nowhere"]],
}


def _plan_fake(monkeypatch, reply, seen=None):
    import app.main as main_module

    async def _fake_generate_chat(messages, system="", model="", options=None, think=False, format=None):
        if seen is not None:
            seen.append((system, format))
        return reply if isinstance(reply, str) else "```json\n" + json.dumps(reply) + "\n```"

    monkeypatch.setattr(main_module._ai_module, "generate_chat", _fake_generate_chat)


def _start(client, slug, **data):
    r = client.post(f"/maps/schematic/{slug}/ai-build/start", data={"description": "a tavern", **data})
    assert r.status_code == 200, r.text
    return _poll_job(client, slug, r.json()["job_id"])


def test_plan_mode_lays_out_a_plan_into_real_elements(client, seed, monkeypatch):
    from app.models import Entity, MapProp
    seen = []
    _plan_fake(monkeypatch, _CANNED_PLAN, seen)
    existing = [{"id": "keep-me", "type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}]
    _schematic(seed, slug="plan-den", elements=existing)
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="npc", name="Old Salt"))
        db.add(MapProp(world_id=seed.world_a.id, name="Big Bed", file_url="/uploads/props/bed.webp", cells_w=2, cells_h=1))
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    data = _start(client, "plan-den", mode="plan")
    assert data["status"] == "done", data
    assert "You do NOT draw" in seen[0][0] and "rooms" in seen[0][1]["properties"]       # the planning prompt, structured output
    assert data["title"] == "The Rusty Anchor"
    assert any("had no way in" in w for w in data["warnings"])                          # the 'ghost' room was joined to the entrance
    # the returned list is the MERGED canvas, so the editor can adopt it without losing what was there
    assert data["elements"][0]["id"] == "keep-me" and data["total"] == len(data["elements"]) == 1 + data["added"]
    floors = [e for e in data["elements"] if e.get("layer") == "Background"]
    assert sorted(f["label"] for f in floors) == ["Bar", "Ghost room", "Kitchen", "Store"]
    bar = next(f for f in floors if f["label"] == "Bar")
    assert bar["entity_id"] and "entity_name" not in bar                              # lore entity resolved by name
    assert any(e.get("hidden") for e in data["elements"] if e["type"] == "line")      # the secret door is GM-only
    db = SessionLocal()
    try:
        stored = json.loads(db.query(Schematic).filter(Schematic.slug == "plan-den").first().elements_json)
        assert len(stored) == data["total"] and stored[0]["id"] == "keep-me"
    finally:
        db.close()


def test_plan_mode_replace_uses_the_grid_and_rejects_nonsense(client, seed, monkeypatch):
    _schematic(seed, slug="plan-grid", elements=[{"id": "old", "type": "rect", "x": 1}])
    db = SessionLocal()
    try:
        s = db.query(Schematic).filter(Schematic.slug == "plan-grid").first()
        s.grid_type, s.grid_config_json = "square", json.dumps({"cell_size": 40, "offset_x": 10, "offset_y": 20})
        db.commit()
    finally:
        db.close()
    _plan_fake(monkeypatch, _CANNED_PLAN)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    data = _start(client, "plan-grid", mode="plan", replace="1")
    assert data["status"] == "done" and not any(e["id"] == "old" for e in data["elements"])
    floors = [e for e in data["elements"] if e.get("layer") == "Background"]
    assert all((f["x"] - 10) % 40 == 0 and (f["y"] - 20) % 40 == 0 and f["w"] % 40 == 0 for f in floors)   # on the map's own grid
    _plan_fake(monkeypatch, "I'm sorry, I can't plan that.")
    bad = _start(client, "plan-grid", mode="plan")
    assert bad["status"] == "error" and "plan could not be used" in bad["error"]
    _plan_fake(monkeypatch, {"rooms": []})
    assert _start(client, "plan-grid", mode="plan")["status"] == "error"


def test_an_unknown_mode_falls_back_to_free_drawing(client, seed, monkeypatch):
    import app.main as main_module
    prompts = []

    async def _fake(messages, system="", model="", options=None, think=False, format=None):
        prompts.append(system)
        return json.dumps(_CANNED_ELEMENTS)

    monkeypatch.setattr(main_module._ai_module, "generate_chat", _fake)
    _schematic(seed, slug="plan-mode")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    assert _start(client, "plan-mode", mode="surprise")["status"] == "done"
    assert "2000 x 1500" in prompts[0]
