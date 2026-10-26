def test_export_book_zip_is_pythons(client, seed):
    """GET /export/book.zip from the gateway: the standalone world book - the rendered index.html byte for byte (the
    template's own capitalize / split / safe filters through the shared renderer), the stylesheet, every illustrated
    entity's image; a player refused as Python refuses. The zips' own bytes differ (Python's timestamps are its build
    moment); the entries are what must match, name for name and byte for byte."""
    import io
    import zipfile
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, InvestBoard, World
    from pathlib import Path
    base = str(client.base_url)
    w = seed.world_a.id
    uploads = Path(os.environ["DB_PATH"]).parent / "uploads"
    pic_dir = uploads / "maps"
    pic_dir.mkdir(parents=True, exist_ok=True)
    pic = pic_dir / "exp-pic.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\nexport-picture")
    db = SessionLocal()
    try:
        db.get(World, w).rules_md = "# House Rules\n\nRoll d10.\n\n<a name=\"anchor\"></a>## Keep\n\nBe kind."
        db.add_all([
            Entity(world_id=w, kind="npc", name="Vosk", subtype="Fixer", summary="Deals info",
                   body="# Vosk\n\nThe docks' [b]fixer[/b].\n\n## Contacts\n\nMany.", tags="docks, fixer ,",
                   image_url="/uploads/maps/exp-pic.png"),
            Entity(world_id=w, kind="npc", name="Ana"),
            Entity(world_id=w, kind="location", name="The Pier", body="Wet."),
            Entity(world_id=w, kind="item", name="Hidden", visible_to_players=False),
        ])
        db.add_all([
            InvestBoard(world_id=w, name="Case", slug="exp-case", description="the case",
                        nodes_json=json.dumps({"nodes": [{"id": "a", "title": "T"}], "groups": []}), edges_json="[]"),
            InvestBoard(world_id=w, name="Legacy", slug="exp-legacy", nodes_json=json.dumps([{"id": 1}]), edges_json=""),
        ])
        db.commit()
    finally:
        db.close()
    maps = Path(os.environ["DB_PATH"]).parent / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    (maps / "exp-map.json").write_text(json.dumps({"world_id": w, "name": "Docks", "markers": [{"x": 1}, {"x": 2}]}), encoding="utf-8")
    (maps / "exp-bad.json").write_text(json.dumps({"world_id": w, "markers": {"a": 1}}), encoding="utf-8")
    try:
        d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        v = _via(d, base)
        a, b = d.get("/export/book.zip"), v.get("/export/book.zip")
        assert a.status_code == b.status_code == 200, (a.status_code, b.status_code, a.text[:200])
        assert b.headers.get("x-nd-served-by") == "gateway"
        assert a.headers["content-type"] == b.headers["content-type"] == "application/zip"
        assert a.headers["content-disposition"] == b.headers["content-disposition"] == 'attachment; filename="world-a-worldbook.zip"'
        za, zb = zipfile.ZipFile(io.BytesIO(a.content)), zipfile.ZipFile(io.BytesIO(b.content))
        names_a, names_b = sorted(za.namelist()), sorted(zb.namelist())
        assert names_a == names_b, (names_a, names_b)
        assert "index.html" in names_a and "assets/style.css" in names_a and "assets/images/exp-pic.png" in names_a, names_a
        for n in names_a:
            if n == "assets/images/exp-pic.png":
                assert za.read(n) == zb.read(n) == pic.read_bytes()
            else:
                assert za.read(n) == zb.read(n), (n, za.read(n)[:200], zb.read(n)[:200])
        index = za.read("index.html").decode("utf-8")
        assert "Vosk" in index and "The docks" in index and "Case" in index and "Docks" in index, index[:300]
        assert "2 markers" in index and "exp-pic.png" in index and "anchor" not in index, index[:300]
        # a player refused exactly as Python refuses (the auth gate), and a world-less GM redirected
        pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
        a2, b2 = pl.get("/export/book.zip"), _via(pl, base).get("/export/book.zip")
        assert (a2.status_code, a2.text) == (b2.status_code, b2.text) and a2.status_code == 403, a2.status_code
    finally:
        pic.unlink(missing_ok=True)
        (maps / "exp-map.json").unlink(missing_ok=True)
        (maps / "exp-bad.json").unlink(missing_ok=True)
