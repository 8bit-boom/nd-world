def test_kind_download_zips_are_pythons(client, seed):
    """GET /kind/<kind>/download.zip and /download-selected.zip from the gateway: one .md per entity (the same text the
    single-entity download writes, the leading name sanitized, the id appended), visibility and the download toggle
    enforced; the picked ids honored (a hidden id never smuggled in); a player in a world without downloads refused;
    the entries compared name for name and byte for byte."""
    import io
    import zipfile
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, EntityNote, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([
            Entity(world_id=w, kind="item", name="Blade <sharp>", summary="An edge", body="Cuts [gmonly]deep[/gmonly].",
                   image_url="/uploads/x.png"),
            Entity(world_id=w, kind="item", name="!!", body=None),
            Entity(world_id=w, kind="item", name="Hidden", visible_to_players=False),
            Entity(world_id=w, kind="note", name="Other kind"),
        ])
        db.flush()
        db.add(EntityNote(entity_id=1, content="Seen.", visible_to_players=True))
        db.commit()
        ids = {}
        for row in db.query(Entity).filter(Entity.world_id == w).all():
            ids[row.name] = row.id
    finally:
        db.close()
    try:
        d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        v = _via(d, base)
        for url in (f"/kind/item/download.zip", f"/kind/item/download-selected.zip?id={ids['Blade <sharp>']}&id={ids['!!']}",
                    f"/kind/item/download-selected.zip?id={ids['Blade <sharp>']}&id={ids['Hidden']}",
                    "/kind/nosuchkind/download.zip"):
            a, b = d.get(url), v.get(url)
            assert a.status_code == b.status_code == 200, (url, a.status_code, b.status_code, a.text[:120])
            assert b.headers.get("x-nd-served-by") == "gateway", url
            assert a.headers["content-type"] == b.headers["content-type"] == "application/zip"
            za, zb = zipfile.ZipFile(io.BytesIO(a.content)), zipfile.ZipFile(io.BytesIO(b.content))
            assert sorted(za.namelist()) == sorted(zb.namelist()), (url, za.namelist(), zb.namelist())
            for n in za.namelist():
                assert za.read(n) == zb.read(n), (url, n)
        # the GM's full-kind zip carries the hidden entity too; the selected one only the picked
        za = zipfile.ZipFile(io.BytesIO(d.get("/kind/item/download.zip").content))
        names = sorted(za.namelist())
        assert names == [f"!!-{ids['!!']}.md", f"Blade sharp-{ids['Blade <sharp>']}.md", f"Hidden-{ids['Hidden']}.md"], names
        body = za.read(names[1]).decode()
        assert "Cuts deep." in body and "## Notes" in body and "Seen." in body, body
        za2 = zipfile.ZipFile(io.BytesIO(d.get(f"/kind/item/download-selected.zip?id={ids['Blade <sharp>']}").content))
        assert sorted(za2.namelist()) == [f"Blade sharp-{ids['Blade <sharp>']}.md"], za2.namelist()
        # a player in a world without the download toggle: refused exactly as Python refuses
        pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
        a, b = pl.get("/kind/item/download.zip"), _via(pl, base).get("/kind/item/download.zip")
        assert (a.status_code, a.text) == (b.status_code, b.text) and a.status_code == 403
        # with the toggle on: the visible entities only, [gmonly] stripped
        db = SessionLocal()
        try:
            db.get(World, w).players_can_download_entities = True
            db.commit()
        finally:
            db.close()
        a, b = pl.get("/kind/item/download.zip"), _via(pl, base).get("/kind/item/download.zip")
        assert a.status_code == b.status_code == 200, a.status_code
        zp = zipfile.ZipFile(io.BytesIO(a.content))
        assert sorted(zp.namelist()) == [f"!!-{ids['!!']}.md", f"Blade sharp-{ids['Blade <sharp>']}.md"], zp.namelist()
        assert "deep" not in zp.read(f"Blade sharp-{ids['Blade <sharp>']}.md").decode()
        # an id value FastAPI would 422 on: Python's answer, gateway-declined
        r = v.get("/kind/item/download-selected.zip?id=x")
        assert r.status_code == 422 and r.headers.get("x-nd-served-by") != "gateway"
    finally:
        db = SessionLocal()
        try:
            db.get(World, w).players_can_download_entities = False
            db.commit()
        finally:
            db.close()
