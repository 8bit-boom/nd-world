def test_admin_backup_zip_is_pythons(client, seed):
    """GET /admin/backup.zip from the gateway: the VACUUM INTO snapshot of the database, the manifest with the table
    counts (created_at and the filename's stamp are the build moment - normalized), every uploaded file and map json
    under its prefix; a player refused as Python refuses. The entries are compared name for name and byte for byte."""
    import io
    import re
    import zipfile
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity
    from pathlib import Path
    base = str(client.base_url)
    w = seed.world_a.id
    data = Path(os.environ["DB_PATH"]).parent
    up = data / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    (up / "bk-one.txt").write_bytes(b"first")
    (up / "sub").mkdir(exist_ok=True)
    (up / "sub" / "bk-two.bin").write_bytes(b"\x00\x01second")
    maps = data / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    (maps / "bk-map.json").write_text(json.dumps({"world_id": w, "name": "Bk"}), encoding="utf-8")
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=w, kind="item", name="Bk thing")])
        db.commit()
    finally:
        db.close()
    try:
        d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        v = _via(d, base)
        a, b = d.get("/admin/backup.zip"), v.get("/admin/backup.zip")
        assert a.status_code == b.status_code == 200, (a.status_code, b.status_code)
        assert b.headers.get("x-nd-served-by") == "gateway"
        assert a.headers["content-type"] == b.headers["content-type"] == "application/zip"
        assert re.fullmatch(r'attachment; filename="nd-world-backup-\d{8}-\d{6}\.zip"', b.headers["content-disposition"])
        za, zb = zipfile.ZipFile(io.BytesIO(a.content)), zipfile.ZipFile(io.BytesIO(b.content))
        names = sorted(za.namelist())
        assert names == sorted(zb.namelist()), (names, sorted(zb.namelist()))
        assert "world.db" in names and "manifest.json" in names, names
        assert "uploads/bk-one.txt" in names and "uploads/sub/bk-two.bin" in names and "maps/bk-map.json" in names, names
        for n in names:
            if n == "manifest.json":
                ma, mb = json.loads(za.read(n)), json.loads(zb.read(n))
                for m in (ma, mb):
                    m["created_at"] = "<stamp>"
                assert ma == mb, (ma, mb)
                assert ma["app"] == "nd-world" and ma["tables"]["entities"] >= 1, ma
            else:
                assert za.read(n) == zb.read(n), n  # world.db too: VACUUM INTO of an unchanged database is deterministic
        # a player refused exactly as Python refuses
        pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
        a2, b2 = pl.get("/admin/backup.zip"), _via(pl, base).get("/admin/backup.zip")
        assert (a2.status_code, a2.text) == (b2.status_code, b2.text) and a2.status_code == 403, a2.status_code
    finally:
        (up / "bk-one.txt").unlink(missing_ok=True)
        (up / "sub" / "bk-two.bin").unlink(missing_ok=True)
        (maps / "bk-map.json").unlink(missing_ok=True)
