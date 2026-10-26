def test_import_detect_is_pythons(client, seed):
    """POST /api/import/detect from the gateway: every detection branch byte for byte - the batch envelope and its
    Counter-ordered breakdown, world rules, map overlays, schematic elements (both shapes), random tables (list and
    single), field templates (both shapes, the quoted name), entities (bulk and single), player characters (bulk and
    single), and the unknown fallback; a text json.loads refuses is Python's own "invalid" answer (forwarded), and the
    world's custom kinds are recognized."""
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.get(World, w).custom_kinds_json = json.dumps([{"id": "vehicle", "label": "Vehicle", "icon": "\U0001f697"}])
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    payloads = [
        # batch: envelopes and bare blobs mixed, breakdown in count-then-first-seen order
        {"json_text": json.dumps({"imports": [
            {"kind": "entity_single", "data": {"kind": "item", "name": "A"}},
            {"kind": "item", "name": "B"},
            {"kind": "note", "name": "C"},
            {"kind": "item", "name": "D"},
        ]})},
        {"json_text": json.dumps({"rules_md": "# Rules\nBe kind."})},
        {"json_text": json.dumps({"custom_markers": [{"x": 1}], "custom_regions": []})},
        {"json_text": json.dumps({"custom_regions": [{"x": 1}, {"x": 2}]})},
        {"json_text": json.dumps({"elements": [{"type": "rect"}, {"type": "text"}]})},
        {"json_text": json.dumps([{"type": "pin"}, {"type": "token"}])},
        {"json_text": json.dumps([{"name": "T1", "entries": [{"label": "a"}, {"label": "b"}]}, {"name": "T2", "entries": []}])},
        {"json_text": json.dumps({"name": "One", "entries": [{"label": "x"}]})},
        {"json_text": json.dumps({"name": "Stats", "fields": [{"id": "hp", "label": "HP", "type": "resource"}]})},
        {"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}])},
        {"json_text": json.dumps([{"kind": "item", "name": "Sword"}, {"kind": "npc", "name": "Vosk"}])},
        {"json_text": json.dumps({"kind": "vehicle", "name": "Rover", "custom_fields": {}})},   # a custom kind
        {"json_text": json.dumps([{"name": "Kira", "stats": []}, {"name": "Zed", "level": 3}])},
        {"json_text": json.dumps({"name": "Kira", "charClass": "Rigger", "minor_edge": "x"})},
        {"json_text": json.dumps({"hello": "world"})},
        {"json_text": json.dumps([{"mystery": 1}])},
        {"json_text": json.dumps([])},
        {"json_text": ""},
        {"json_text": json.dumps(None)},
    ]
    served = 0
    for body in payloads:
        a, b = d.post("/api/import/detect", json=body), v.post("/api/import/detect", json=body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (body["json_text"][:60], a.text, b.text)
        if b.headers.get("x-nd-served-by") == "gateway" and a.status_code == 200:
            served += 1
    assert served >= 15, served
    # spot-check the branches that matter most
    def detect(text):
        return d.post("/api/import/detect", json={"json_text": json.dumps(text)}).json()
    assert detect({"imports": [{"kind": "item", "name": "A"}, {"kind": "note", "name": "B"}, {"kind": "item", "name": "C"}]}) == \
        {"kind": "batch", "summary": "3 item(s): 2 item, 1 note", "count": 3, "needs": []}
    assert detect({"custom_markers": [{"x": 1}], "custom_regions": []})["summary"] == "1 map marker(s), 0 region(s)"
    assert detect({"name": "Stats", "fields": [{"id": "hp", "label": "HP", "type": "resource"}]})["summary"] == '1 field(s) \u2014 "Stats"'
    assert detect({"kind": "vehicle", "name": "Rover"})["kind"] == "entity_single"  # the world's custom kind
    assert detect({"name": "Rover"})["kind"] == "unknown"
    assert detect({"name": "Kira", "level": 2})["kind"] == "player_character"
    # an invalid inner text: Python's own message, gateway-declined
    bad = v.post("/api/import/detect", json={"json_text": "{oops"})
    assert bad.status_code == 200 and bad.json()["kind"] == "invalid" and "Not valid JSON" in bad.json()["summary"]
    assert bad.headers.get("x-nd-served-by") != "gateway"
    # a missing json_text key parses as "" -> Python's invalid answer
    a, b = d.post("/api/import/detect", json={}), v.post("/api/import/detect", json={})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) and a.json()["kind"] == "invalid"
    # a player without import rights: refused exactly as Python refuses
    from .conftest import PLAYER_PASSWORD
    pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    a2, b2 = pl.post("/api/import/detect", json=payloads[0]), _via(pl, base).post("/api/import/detect", json=payloads[0])
    assert (a2.status_code, a2.text) == (b2.status_code, b2.text), (a2.status_code, b2.status_code)
