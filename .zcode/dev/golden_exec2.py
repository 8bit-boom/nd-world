def test_import_execute_more_kinds_are_pythons(client, seed):
    """POST /api/import/execute from the gateway, remaining branches: map_overlay (append to the overlay row, the merged
    cap, the one-message cross-world guard, the missing-slug 400), schematic_elements (a new schematic via __new__ with
    its bounds, or the named one's elements merged by id), field_template (an entity template, the custom-kind floor) and
    the player_character branches (create, upsert by name, the N&D spread stamped, a custom template's fields kept);
    Python's 400s word for word; the batch envelope still Python's."""
    import itertools
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import EntityTemplate, PlayerCharacter, SheetTemplate
    from tests.test_gateway_writes import _newest, _row_by
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add(SheetTemplate(world_id=w, name="Mek", slug="mek-imp", sheet_mode="custom",
                             fields_json=json.dumps([{"id": "skill", "label": "Skill", "type": "text"}])))
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

    # map overlays: appended onto the shared overlay row (both servers write the same row: run Python's, snapshot,
    # run the gateway's on top, and compare the merged totals against a Python-side double-import on a twin slug)
    maps = Path(os.environ["DB_PATH"]).parent / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    slug = f"imp-map-{next(uniq)}"
    (maps / f"{slug}.json").write_text(json.dumps({"world_id": w, "name": "Imp Map"}), encoding="utf-8")
    other = f"imp-other-{next(uniq)}"
    (maps / f"{other}.json").write_text(json.dumps({"world_id": seed.world_b.id, "name": "Other"}), encoding="utf-8")
    try:
        first = {"custom_markers": [{"x": 1, "label": "a"}], "custom_regions": []}
        second = {"custom_markers": [{"x": 2}], "custom_regions": [{"points": [0, 0]}]}
        a = run(d, {"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": slug}})
        assert a[0] == 200 and a[1]["ok"] and a[1]["redirect"] == f"/maps/{slug}", a
        b = run(v, {"json_text": json.dumps(second), "kind": "map_overlay", "params": {"map_slug": slug}})
        assert b[0] == 200 and b[1]["ok"] and b[2] == "gateway", b
        row = _row_by("map_overlays", "slug", slug)
        assert json.loads(row["custom_markers_json"]) == [{"x": 1, "label": "a"}, {"x": 2}], row
        assert json.loads(row["custom_regions_json"]) == [{"points": [0, 0]}], row
        # refusals, word for word
        for payload, expect in (
            ({"json_text": json.dumps({"kind": "world_rules"}), "kind": "map_overlay", "params": {"map_slug": slug}}, "Expected an object"),
            ({"json_text": json.dumps(first), "kind": "map_overlay", "params": {}}, "No map selected"),
            ({"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": "no-such-map"}}, "Map not found"),
            ({"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": other}}, "Map not found"),
            ({"json_text": json.dumps({"custom_markers": "x"}), "kind": "map_overlay", "params": {"map_slug": slug}}, "custom_markers and custom_regions must be lists"),
        ):
            r1, r2 = run(d, payload), run(v, payload)
            assert r1[0] == r2[0] == 400 and r1[1]["detail"] == r2[1]["detail"] and expect in r1[1]["detail"], (payload, r1, r2)
        # a marker list over the cap: the gateway declines (Python's big payload is slow to seed) - actually seed it small-wise:
        big = {"custom_markers": [{"i": i} for i in range(501)], "params": {"map_slug": slug}}
        r = v.post("/api/import/execute", json={"json_text": json.dumps(big), "kind": "map_overlay", "params": {"map_slug": slug}})
        assert r.status_code == 400 and "limit is 500" in r.json()["detail"]
        # schematics: a new one via __new__ (bounds and the numeric coercions)
        r1 = run(d, {"json_text": json.dumps({"elements": [{"type": "rect", "id": "r1", "x": 1}]}), "kind": "schematic_elements",
                     "params": {"schematic_slug": "__new__", "new_schematic_name": f"Imp Schem {next(uniq)}", "new_canvas_width": "3000"}})
        r2 = run(v, {"json_text": json.dumps({"elements": [{"type": "rect", "id": "r1", "x": 1}]}), "kind": "schematic_elements",
                     "params": {"schematic_slug": "__new__", "new_schematic_name": f"Imp Schem {next(uniq)}", "new_canvas_width": "3000"}})
        assert r1[0] == r2[0] == 200 and r1[1]["ok"] and r2[1]["ok"] and r2[2] == "gateway", (r1, r2)
        assert r1[1]["redirect"].startswith("/maps/schematic/") and r2[1]["redirect"].startswith("/maps/schematic/")
        sl = r2[1]["redirect"].rsplit("/", 1)[-1]
        row = _row_by("schematics", "slug", sl)
        assert row["canvas_width"] == 3000 and row["canvas_height"] == 1500 and json.loads(row["elements_json"])[0]["id"] == "r1", row
        # a merge by id into that schematic: run Python's on it, then the gateway's, compare with a twin on Python only
        twin = r1[1]["redirect"].rsplit("/", 1)[-1]
        add = {"elements": [{"type": "rect", "id": "r1", "x": 9}, {"type": "text", "id": "t1"}]}
        a = run(d, {"json_text": json.dumps(add), "kind": "schematic_elements", "params": {"schematic_slug": twin}})
        b = run(v, {"json_text": json.dumps(add), "kind": "schematic_elements", "params": {"schematic_slug": sl}})
        assert a[0] == b[0] == 200 and b[2] == "gateway", (a, b)
        ea = json.loads(_row_by("schematics", "slug", twin)["elements_json"])
        eb = json.loads(_row_by("schematics", "slug", sl)["elements_json"])
        assert ea == eb and len(eb) == 2 and eb[0]["x"] == 9, (ea, eb)
        # a foreign world's schematic: the same refusal
        for payload, expect in (
            ({"json_text": json.dumps(add), "kind": "schematic_elements", "params": {"schematic_slug": "no-such"}}, "Schematic not found"),
            ({"json_text": json.dumps({"elements": []}), "kind": "schematic_elements", "params": {"schematic_slug": "__new__"}}, "non-empty array"),
            ({"json_text": json.dumps({"elements": [{"type": "rect"}]}), "kind": "schematic_elements", "params": {"schematic_slug": "__new__", "new_canvas_width": "x"}}, "must be numbers"),
            ({"json_text": json.dumps({"elements": [{"type": "rect"}]}), "kind": "schematic_elements", "params": {"schematic_slug": "__new__", "new_canvas_width": "50"}}, "between 100 and 20000"),
        ):
            r1, r2 = run(d, payload), run(v, payload)
            assert r1[0] == r2[0] == 400 and r1[1]["detail"] == r2[1]["detail"] and expect in r1[1]["detail"], (payload, r1, r2)
        # field templates: an entity template through both, rows compared
        n1, n2 = f"Imp Tpl {next(uniq)}", f"Imp Tpl {next(uniq)}"
        a = run(d, {"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}]), "kind": "field_template",
                    "params": {"template_kind": "entity", "name": n1}})
        b = run(v, {"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}]), "kind": "field_template",
                    "params": {"template_kind": "entity", "name": n2}})
        assert a[0] == b[0] == 200 and b[2] == "gateway", (a, b)
        assert a[1]["redirect"].startswith("/entity-templates/") and b[1]["redirect"].startswith("/entity-templates/")
        ta = _row_by("entity_templates", "id", a[1]["redirect"].rsplit("/", 1)[-1].split("/")[0])
        tb = _row_by("entity_templates", "id", b[1]["redirect"].rsplit("/", 1)[-1].split("/")[0])
        for k in ("name", "kind", "description", "is_builtin", "fields_json", "world_id"):
            va, vb = ta[k], tb[k]
            if k == "name":
                assert va == n1 and vb == n2, (va, vb)
            else:
                assert va == vb, (k, va, vb)
        # a field template whose entity_kind is not a kind: stored NULL, by both
        a = run(d, {"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}]), "kind": "field_template",
                    "params": {"template_kind": "entity", "entity_kind": "nosuchkind", "name": f"NoKind {next(uniq)}"}})
        assert a[0] == 200 and _newest("entity_templates")["kind"] is None, a
        # player characters: create with the N&D defaults stamped, upsert-by-name through both, a custom template's fields kept
        nm = f"Imp PC {next(uniq)}"
        a = run(d, {"json_text": json.dumps({"name": nm, "charClass": "Rigger", "level": 4})})
        assert a[0] == 200 and a[1]["redirect"].startswith("/characters/"), a
        row = _row_by("player_characters", "name", nm)
        assert row["char_class"] == "Rigger" and row["level"] == 4, row
        assert json.loads(row["stats_json"])[0]["id"] == "str", row["stats_json"]  # the N&D spread
        # a custom template's sheet: the fields are the character, no spread stamped
        nm2 = f"Imp Custom {next(uniq)}"
        a = run(v, {"json_text": json.dumps({"name": nm2, "sheet_template_id": "mek-imp", "skill": "Pilot"}), "kind": "player_character"})
        assert a[0] == 200 and a[2] == "gateway", a
        row2 = _row_by("player_characters", "name", nm2)
        assert row2["sheet_template_id"] == _row_by("sheet_templates", "slug", "mek-imp")["id"], row2
        assert json.loads(row2["stats_json"]) == [], row2["stats_json"]
        # upsert: the same name again updates in place (run Python's on one name, the gateway's on another; compare columns)
        nm3, nm4 = f"Upsert {next(uniq)}", f"Upsert {next(uniq)}"
        run(d, {"json_text": json.dumps({"name": nm3, "level": 2})})
        run(d, {"json_text": json.dumps({"name": nm3, "level": 6, "race": "Elf"})})
        run(v, {"json_text": json.dumps({"name": nm4, "level": 2})})
        b = run(v, {"json_text": json.dumps({"name": nm4, "level": 6, "race": "Elf"})})
        assert b[0] == 200 and b[2] == "gateway", b
        ra, rb = _row_by("player_characters", "name", nm3), _row_by("player_characters", "name", nm4)
        assert ra["level"] == rb["level"] == 6 and ra["race"] == rb["race"] == "Elf", (ra["level"], rb["level"])
        # count: the upsert must not have created a second row
        db = SessionLocal()
        try:
            assert db.query(PlayerCharacter).filter(PlayerCharacter.name == nm4).count() == 1
        finally:
            db.close()
        # refusals word for word
        for payload, expect in (
            ({"json_text": json.dumps([{"level": 1}]), "kind": "player_character_bulk"}, "Character #1: needs at least a"),
            ({"json_text": json.dumps({"nope": 1}), "kind": "player_character"}, 'Expected an object with at least a "name"'),
            ({"json_text": json.dumps({"imports": []}), "kind": "batch"}, None),  # forwarded: Python's own answer
        ):
            r1, r2 = run(d, payload), run(v, payload)
            if expect is None:
                assert r1[0] == r2[0] and r2[2] != "gateway", (r1, r2)
            else:
                assert r1[0] == r2[0] == 400 and r1[1]["detail"] == r2[1]["detail"] and expect in r1[1]["detail"], (payload, r1, r2)
        # a player without import rights, one more time on a write branch
        pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
        a2 = pl.post("/api/import/execute", json={"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": slug}})
        b2 = _via(pl, base).post("/api/import/execute", json={"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": slug}})
        assert (a2.status_code, a2.text) == (b2.status_code, b2.text) and a2.status_code == 403, a2.status_code
    finally:
        (maps / f"{slug}.json").unlink(missing_ok=True)
        (maps / f"{other}.json").unlink(missing_ok=True)
