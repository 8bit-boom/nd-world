def test_world_import_is_pythons(client, seed):
    """POST /worlds/<id>/import from the gateway: a world JSON export's entities restored - created and updated counted
    in the redirect, the embedded data-URI image written back under a fresh uploads name, every field set outright on an
    upsert (a blank in the backup overwrites), the template resolved slug/name; a missing world 404, an invalid file
    Python's 400; run through both servers on identical payloads and the rows compared."""
    import base64 as b64mod
    from app.database import SessionLocal
    from app.models import Entity, EntityTemplate, World
    from tests.test_gateway_writes import _row_by
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    png = b64mod.b64encode(b"\x89PNG-restored").decode()
    export = {"entities": [
        {"kind": "item", "name": "Imp Sword", "subtype": "W", "summary": "s", "body": "b", "tags": "t",
         "custom_fields_json": {"dmg": 6}, "image_data": f"data:image/png;base64,{png}"},
        {"kind": "npc", "name": "Imp Vosk", "template_slug": "imp-tpl"},
        {"kind": "note", "name": "", "body": "skipped"},
        {"kind": "note", "name": "Imp Hidden", "visible_to_players": False},
    ]}
    db = SessionLocal()
    try:
        db.add(EntityTemplate(world_id=seed.world_a.id, name="Imp Tpl", slug="imp-tpl", kind="npc", fields_json="[]"))
        db.commit()
    finally:
        db.close()

    def run(side):
        for n in ("Imp Sword", "Imp Vosk", "Imp Hidden"):
            row = _row_by("entities", "name", n)
            if row:
                db = SessionLocal()
                try:
                    db.query(Entity).filter(Entity.id == row["id"]).delete()
                    db.commit()
                finally:
                    db.close()
        r = side.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("export.json", json.dumps(export).encode(), "application/json")}, follow_redirects=False)
        rows = {n: _row_by("entities", "name", n) for n in ("Imp Sword", "Imp Vosk", "Imp Hidden")}
        return r, rows

    ra, rows_a = run(d)
    rb, rows_b = run(v)
    assert ra.status_code == rb.status_code == 303, (ra.status_code, rb.status_code, ra.text[:200])
    assert rb.headers.get("x-nd-served-by") == "gateway"
    la, lb = ra.headers["location"], rb.headers["location"]
    # the same counts, the same world slug (the ids in the query differ per run? no - the query carries counts only)
    import re
    qa = re.search(r"imported=(\d+)&updated=(\d+)", la).groups()
    qb = re.search(r"imported=(\d+)&updated=(\d+)", lb).groups()
    assert qa == qb == ("3", "0"), (la, lb)
    assert la.split("&")[0] == lb.split("&")[0], (la, lb)
    # the rows: every passthrough column equal, the image restored by both
    for n in rows_a:
        x, y = rows_a[n], rows_b[n]
        assert x is not None and y is not None, n
        for k in ("kind", "subtype", "summary", "body", "tags", "visible_to_players", "template_id", "custom_fields_json", "world_id"):
            assert x[k] == y[k], (n, k, x[k], y[k])
    assert rows_a["Imp Sword"]["image_url"] and rows_b["Imp Sword"]["image_url"], (rows_a["Imp Sword"]["image_url"], rows_b["Imp Sword"]["image_url"])
    assert rows_b["Imp Sword"]["image_url"].startswith("/uploads/")
    assert json.loads(rows_b["Imp Sword"]["custom_fields_json"]) == {"dmg": 6}
    assert rows_b["Imp Hidden"]["visible_to_players"] == 0
    assert rows_b["Imp Vosk"]["template_id"] is not None
    # the restored image really is the decoded bytes
    from pathlib import Path
    img = Path(os.environ["DB_PATH"]).parent / "uploads" / Path(rows_b["Imp Sword"]["image_url"]).name
    assert img.read_bytes() == b"\x89PNG-restored", img
    # the upsert: a second import of the same payload updates, not duplicates (the gateway's side)
    r2, rows2 = run(v)
    assert r2.status_code == 303 and "updated=3" in r2.headers["location"], r2.headers["location"]
    assert rows2["Imp Sword"]["id"] == rows_b["Imp Sword"]["id"], (rows_b["Imp Sword"]["id"], rows2["Imp Sword"]["id"])
    # a blank in the backup overwrites: update the summary, re-import with it empty
    export2 = {"entities": [{"kind": "item", "name": "Imp Sword", "summary": "", "body": ""}]}
    r3 = v.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("export.json", json.dumps(export2).encode(), "application/json")}, follow_redirects=False)
    assert r3.status_code == 303 and "updated=1" in r3.headers["location"]
    row3 = _row_by("entities", "name", "Imp Sword")
    assert row3["summary"] is None and row3["body"] is None, (row3["summary"], row3["body"])
    # refusals: a missing world, an invalid file
    a, b = d.post("/worlds/999999/import", files={"file": ("x.json", b"{}", "application/json")}, follow_redirects=False), \
        v.post("/worlds/999999/import", files={"file": ("x.json", b"{}", "application/json")}, follow_redirects=False)
    assert (a.status_code, a.text) == (b.status_code, b.text) == (404, '{"detail":"Not Found"}'), (a.status_code, b.status_code)
    a, b = d.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("x.json", b"not json", "application/json")}, follow_redirects=False), \
        v.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("x.json", b"not json", "application/json")}, follow_redirects=False)
    assert (a.status_code, a.text) == (b.status_code, b.text) == (400, '{"detail":"Invalid JSON file"}'), (a.status_code, b.status_code)
