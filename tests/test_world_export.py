"""Tests for the two GM export routes that embed real image files/data and
so share admin_backup's ("Full Backup") streaming fix — building the whole
payload in memory before sending a single byte meant an illustrated
world's images all got read (and, for World JSON, base64-encoded) before
any bytes reached the browser:

- GET /export/book.zip ("World Book" — a zip of readable HTML + images)
- GET /worlds/{id}/export ("World JSON" single file — entities with
  images embedded as base64 data URIs)

See app/streaming_export.py (and tests/test_streaming_export.py) for the
shared mechanism, and tests/test_ux_audit.py / test_gm_assistant.py /
test_player_safe.py for the Export hub's own nav/permission coverage this
doesn't duplicate."""
import base64
import io
import json
import zipfile

from app.database import SessionLocal
from app.main import UPLOADS_DIR
from app.models import Entity, World

from .conftest import GM_PASSWORD, login


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _add_illustrated_entity(world, name="Elyra", image_bytes=b"fake-png-bytes"):
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{name.lower()}.png"
    (UPLOADS_DIR / fname).write_bytes(image_bytes)
    db = SessionLocal()
    try:
        e = Entity(world_id=world.id, kind="character", name=name, image_url=f"/uploads/{fname}")
        db.add(e)
        db.commit()
        db.refresh(e)
        return e
    finally:
        db.close()


# ── World Book (/export/book.zip) ───────────────────────────────────────────

def test_world_book_no_active_world_redirects_to_worlds(client, seed):
    """/export/book.zip is GM-only, and a GM's accessible_world_ids() is
    None (unfiltered) — get_world_ctx only ever returns world=None for a GM
    when the DB has zero World rows at all, so that's what this constructs
    directly rather than relying on the active_world cookie (which falls
    back to worlds[0] whenever it doesn't match, and so can't produce this
    state on its own)."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        db.query(World).delete()
        db.commit()
    finally:
        db.close()
    r = client.get("/export/book.zip", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/worlds"


def test_world_book_returns_a_valid_zip_with_index_and_images(client, seed):
    _add_illustrated_entity(seed.world_a)
    _login_gm(client, seed)
    r = client.get("/export/book.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.testzip() is None
    names = zf.namelist()
    assert "index.html" in names
    image_entries = [n for n in names if n.startswith("assets/images/")]
    assert len(image_entries) == 1
    assert zf.read(image_entries[0]) == b"fake-png-bytes"


def test_world_book_index_html_references_the_embedded_image(client, seed):
    ent = _add_illustrated_entity(seed.world_a)
    _login_gm(client, seed)
    r = client.get("/export/book.zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    index_html = zf.read("index.html").decode()
    assert ent.name in index_html
    assert "assets/images/" in index_html


def test_world_book_with_no_images_still_produces_a_valid_zip(client, seed):
    _login_gm(client, seed)
    r = client.get("/export/book.zip")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.testzip() is None
    assert "index.html" in zf.namelist()
    assert not any(n.startswith("assets/images/") for n in zf.namelist())


# ── World JSON single file (/worlds/{id}/export) ────────────────────────────

def test_world_export_returns_valid_json_with_world_header(client, seed):
    _login_gm(client, seed)
    r = client.get(f"/worlds/{seed.world_a.id}/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    data = json.loads(r.content)
    assert data["world"]["name"] == seed.world_a.name
    assert data["world"]["slug"] == seed.world_a.slug
    assert data["entities"] == []


def test_world_export_embeds_uploaded_images_as_base64(client, seed):
    ent = _add_illustrated_entity(seed.world_a, name="Gareth", image_bytes=b"another-fake-image")
    _login_gm(client, seed)
    r = client.get(f"/worlds/{seed.world_a.id}/export")
    data = json.loads(r.content)
    assert len(data["entities"]) == 1
    exported = data["entities"][0]
    assert exported["name"] == "Gareth"
    assert exported["image_data"].startswith("data:image/png;base64,")
    b64 = exported["image_data"].split(",", 1)[1]
    assert base64.b64decode(b64) == b"another-fake-image"


def test_world_export_entity_without_image_has_null_image_data(client, seed):
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="note", name="Plain Note"))
        db.commit()
    finally:
        db.close()
    _login_gm(client, seed)
    r = client.get(f"/worlds/{seed.world_a.id}/export")
    data = json.loads(r.content)
    assert data["entities"][0]["image_data"] is None


def test_world_export_unknown_world_404s(client, seed):
    _login_gm(client, seed)
    r = client.get("/worlds/999999/export")
    assert r.status_code == 404


def test_world_export_round_trips_through_import(client, seed):
    """The exported JSON must still be exactly what /worlds/{id}/import
    (world_import in app/main.py) expects — unchanged by switching to
    streamed generation."""
    _add_illustrated_entity(seed.world_a, name="Roundtrip Character")
    _login_gm(client, seed)
    exported = client.get(f"/worlds/{seed.world_a.id}/export").json()

    r = client.post(
        f"/worlds/{seed.world_b.id}/import",
        files={"file": ("export.json", io.BytesIO(json.dumps(exported).encode()), "application/json")},
    )
    assert r.status_code in (200, 303, 307)

    db = SessionLocal()
    try:
        imported = db.query(Entity).filter(
            Entity.world_id == seed.world_b.id, Entity.name == "Roundtrip Character",
        ).first()
        assert imported is not None
        assert imported.image_url and imported.image_url.startswith("/uploads/")
    finally:
        db.close()
