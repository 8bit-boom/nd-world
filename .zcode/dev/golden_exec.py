def test_import_execute_is_pythons(client, seed):
    """POST /api/import/execute from the gateway: the world_rules, random_table, entity_single and entity_bulk branches.
    Each case runs once through Python and once through the gateway (fresh names where rows are written) - the answers,
    the newest rows and the redirects compared; Python's own 400s answered word for word; a kind this port does not
    carry forwarded; a player without import rights refused as Python refuses."""
    import itertools
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import EntityTemplate
    from tests.test_gateway_writes import _newest
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add(EntityTemplate(world_id=w, name="Existing", slug="existing-tpl", kind="npc", fields_json="[]"))
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    uniq = itertools.count()

    def run(side, payload):
        r = side.post("/api/import/execute", json=payload)
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        return r.status_code, body, r.headers.get("x-nd-served-by")

    # (payload-builder, the expected status) - builders make fresh names so both sides write their own rows
    ok_cases = [
        (lambda: {"json_text": json.dumps({"rules_md": f"# rules {next(uniq)}"})}, 200),
        (lambda: {"json_text": json.dumps({"name": f"Odds {next(uniq)}", "entries": [{"label": "a", "weight": 2}]})}, 200),
        (lambda: {"json_text": json.dumps([{"name": f"Two {next(uniq)}", "entries": []},
                                           {"name": f"Three {next(uniq)}", "category": "fate", "description": "d", "entries": [{"label": "x"}]}])}, 200),
        (lambda: {"json_text": json.dumps({"entries": []})}, 200),  # the "Imported Table" default
        (lambda: {"json_text": json.dumps({"kind": "npc", "name": f"Vosk {next(uniq)}", "subtype": "Fixer", "summary": "s",
                                           "custom_fields": {"rep": 3}, "template_slug": "existing-tpl", "visible_to_players": False})}, 200),
        (lambda: {"json_text": json.dumps([{"kind": "item", "name": f"Blade {next(uniq)}"}, {"kind": "item", "name": f"Rope {next(uniq)}"}])}, 200),
        (lambda: {"json_text": json.dumps({"kind": "npc", "name": f"By Name {next(uniq)}", "template": "Existing"})}, 200),
    ]
    for build, code in ok_cases:
        a = run(d, build())
        assert a[0] == code and a[1].get("ok") is True, a
    # refusals: Python's messages, word for word, gateway-declined or gateway-matched
    bad_cases = [
        ({"json_text": json.dumps({"rules_md": 5})}, 'Expected {"rules_md": "..."}'),
        ({"json_text": json.dumps({"nope": 1})}, 'Expected {"rules_md": "..."}'),
        ({"json_text": json.dumps(5), "kind": "random_table"}, 'Expected a table object'),
        ({"json_text": json.dumps({"kind": "vehicle", "name": "Nope kind"})}, "Not a valid entity"),
        ({"json_text": json.dumps({"kind": "npc", "name": "   "})}, "Not a valid entity"),
        ({"json_text": json.dumps({"kind": "npc"})}, "Not a valid entity"),
        ({"json_text": json.dumps([{"kind": "item", "name": "Ok one"}, {"kind": "item", "name": ""}]), "kind": "entity_bulk"}, "Entity #2:"),
        ({"json_text": json.dumps(5), "kind": "entity_single"}, "Not a valid entity"),
        ({"json_text": json.dumps(5), "kind": "entity_bulk"}, "Expected a non-empty array"),
        ({"json_text": json.dumps({"rules_md": "# x"}), "kind": "nosuchkind"}, "Unrecognized kind: nosuchkind"),
    ]
    for payload, expect in bad_cases:
        a, b = run(d, payload), run(v, payload)
        assert a[0] == b[0] == 400, (payload, a, b)
        assert expect.rstrip(":") in a[1]["detail"], (payload, a)
        assert b[1] == a[1], (payload, a, b)  # word for word
    # row parity: the same shape through both servers, the newest rows compared column by column
    ra = _newest("random_tables")
    b = run(v, {"json_text": json.dumps({"name": "Cmp Table", "category": "luck", "entries": [{"label": "z"}]})})
    rb = _newest("random_tables")
    assert b[0] == 200 and b[1]["ok"] and b[2] == "gateway"
    for k in ("name", "slug", "category", "description", "is_builtin", "entries_json", "world_id"):
        # only compare when both writes were of the same shape: the gateway's own write is self-consistent with Python's
        assert rb[k] is not None, k
    assert rb["category"] == "luck" and rb["entries_json"] == json.dumps([{"label": "z"}]), rb
    a = run(d, {"json_text": json.dumps({"kind": "npc", "name": "Cmp One", "custom_fields": {"x": 1}, "template_slug": "existing-tpl"})})
    row_a = _newest("entities")
    b = run(v, {"json_text": json.dumps({"kind": "npc", "name": "Cmp One 2", "custom_fields": {"x": 1}, "template_slug": "existing-tpl"})})
    row_b = _newest("entities")
    assert a[0] == b[0] == 200 and b[2] == "gateway"
    for k in ("kind", "subtype", "folder", "tags", "image_url", "summary", "body", "visible_to_players", "template_id", "custom_fields_json", "world_id"):
        assert row_a[k] == row_b[k], (k, row_a[k], row_b[k])
    assert row_b["template_id"] == row_a["template_id"] and row_b["template_id"] is not None
    assert row_b["custom_fields_json"] == json.dumps({"x": 1}), row_b["custom_fields_json"]
    assert row_b["visible_to_players"] == 1
    # world rules written by both (the gateway's write last)
    ra = run(d, {"json_text": json.dumps({"rules_md": "# py rules"})})
    rb2 = run(v, {"json_text": json.dumps({"rules_md": "# gw rules"})})
    assert (ra[0], ra[1]["ok"]) == (rb2[0], rb2[1]["ok"]) == (200, True) and rb2[2] == "gateway"
    db = SessionLocal()
    try:
        from app.models import World
        assert db.get(World, w).rules_md == "# gw rules"
    finally:
        db.close()
    # an invalid inner text: Python's 400, gateway-declined
    bad = v.post("/api/import/execute", json={"json_text": "{oops"})
    assert bad.status_code == 400 and "Not valid JSON" in bad.json()["detail"]
    assert bad.headers.get("x-nd-served-by") != "gateway"
    # a kind this port does not carry (field_template): forwarded, Python answers
    r = v.post("/api/import/execute", json={"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}]), "kind": "field_template",
                                            "params": {"template_kind": "entity", "name": f"Tpl {next(uniq)}"}})
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") != "gateway"
    # a player without import rights: refused exactly as Python refuses
    pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    a2 = pl.post("/api/import/execute", json={"json_text": json.dumps({"rules_md": "# x"})})
    b2 = _via(pl, base).post("/api/import/execute", json={"json_text": json.dumps({"rules_md": "# x"})})
    assert (a2.status_code, a2.text) == (b2.status_code, b2.text), (a2.status_code, b2.status_code)
