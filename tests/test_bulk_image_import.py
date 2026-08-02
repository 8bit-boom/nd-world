"""Tests for POST /api/import/images (bulk portrait/art import matched to
entities by filename, from the /import page). Covers the same concerns as
every other upload path in this app: size limits, extension allowlist, and
that entity_id is re-validated against the active world server-side rather
than trusted from the client.
"""
import io

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _png_bytes(size=2000):
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * size


def _make_entity(world, name="Gandalf", kind="character"):
    db = SessionLocal()
    try:
        ent = Entity(world_id=world.id, kind=kind, name=name)
        db.add(ent)
        db.commit()
        db.refresh(ent)
        return ent
    finally:
        db.close()


def test_bulk_image_import_assigns_to_entity(client, seed):
    ent = _make_entity(seed.world_a, name="Gandalf")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(
        "/api/import/images",
        files=[("files", ("Gandalf.png", io.BytesIO(_png_bytes()), "image/png"))],
        data={"entity_ids": [str(ent.id)]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 1
    assert body["results"][0]["status"] == "ok"

    db = SessionLocal()
    try:
        refreshed = db.query(Entity).filter(Entity.id == ent.id).first()
        assert refreshed.image_url and refreshed.image_url.startswith("/uploads/")
    finally:
        db.close()


def test_bulk_image_import_multiple_files(client, seed):
    ent_a = _make_entity(seed.world_a, name="Aragorn")
    ent_b = _make_entity(seed.world_a, name="Boromir")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(
        "/api/import/images",
        files=[
            ("files", ("Aragorn.png", io.BytesIO(_png_bytes()), "image/png")),
            ("files", ("Boromir.png", io.BytesIO(_png_bytes()), "image/png")),
        ],
        data={"entity_ids": [str(ent_a.id), str(ent_b.id)]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 2
    assert all(res["status"] == "ok" for res in body["results"])


def test_bulk_image_import_skips_blank_entity_id(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(
        "/api/import/images",
        files=[("files", ("Unmatched.png", io.BytesIO(_png_bytes()), "image/png"))],
        data={"entity_ids": [""]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 0
    assert body["results"][0]["status"] == "skipped"


def test_bulk_image_import_rejects_entity_from_other_world(client, seed):
    """The client's matching UI only ever offers entities from the active
    world, but the server must not trust that — an entity_id for a
    different world must be rejected, not silently reassigned."""
    other_world_ent = _make_entity(seed.world_b, name="Sauron")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(
        "/api/import/images",
        files=[("files", ("Sauron.png", io.BytesIO(_png_bytes()), "image/png"))],
        data={"entity_ids": [str(other_world_ent.id)]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 0
    assert body["results"][0]["status"] == "error"

    db = SessionLocal()
    try:
        refreshed = db.query(Entity).filter(Entity.id == other_world_ent.id).first()
        assert refreshed.image_url is None
    finally:
        db.close()


def test_bulk_image_import_rejects_oversized_file(client, seed):
    ent = _make_entity(seed.world_a, name="Legolas")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    oversized = _png_bytes(1_048_576 + 200_000)  # conftest sets MAX_UPLOAD_BYTES=1MiB
    r = client.post(
        "/api/import/images",
        files=[("files", ("Legolas.png", io.BytesIO(oversized), "image/png"))],
        data={"entity_ids": [str(ent.id)]},
    )
    assert r.status_code == 413


def test_bulk_image_import_rejects_svg(client, seed):
    ent = _make_entity(seed.world_a, name="Gimli")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(
        "/api/import/images",
        files=[("files", ("Gimli.svg", io.BytesIO(b"<svg onload='alert(1)'></svg>"), "image/svg+xml"))],
        data={"entity_ids": [str(ent.id)]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 0
    assert body["results"][0]["status"] == "error"


def test_bulk_image_import_is_gm_only(client, seed):
    ent = _make_entity(seed.world_a, name="Frodo")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(
        "/api/import/images",
        files=[("files", ("Frodo.png", io.BytesIO(_png_bytes()), "image/png"))],
        data={"entity_ids": [str(ent.id)]},
    )
    assert r.status_code == 403

    db = SessionLocal()
    try:
        refreshed = db.query(Entity).filter(Entity.id == ent.id).first()
        assert refreshed.image_url is None
    finally:
        db.close()


def test_bulk_image_import_rejects_mismatched_lengths(client, seed):
    ent = _make_entity(seed.world_a, name="Samwise")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(
        "/api/import/images",
        files=[("files", ("Samwise.png", io.BytesIO(_png_bytes()), "image/png"))],
        data={"entity_ids": [str(ent.id), "999"]},
    )
    assert r.status_code == 400


def test_bulk_image_import_rejects_batch_over_max_files(client, seed):
    """Server-side enforcement of BULK_IMAGE_MAX_FILES (shared from
    app/uploads.py — see test_import_page_precheck_matches_server_cap for
    the client-side half of this: warning before upload rather than only
    after the whole oversized batch has already gone over the wire)."""
    from app.uploads import BULK_IMAGE_MAX_FILES
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    n = BULK_IMAGE_MAX_FILES + 1
    files = [("files", (f"img{i}.png", io.BytesIO(_png_bytes(50)), "image/png")) for i in range(n)]
    r = client.post(
        "/api/import/images",
        files=files,
        data={"entity_ids": [""] * n},
    )
    assert r.status_code == 400
    assert "Too many files" in r.json()["detail"]


def test_import_page_precheck_matches_server_cap(client, seed):
    """Regression guard, source-level (no JS runtime in this test suite).
    The bulk-image click handler previously always awaited
    res.json() unconditionally, so if a reverse proxy in front of the app
    rejected an oversized/slow batch and returned its own HTML error page
    instead of JSON, the user saw a raw
    "Import failed: JSON.parse: unexpected character…" SyntaxError instead
    of anything actionable. Locks in: (1) the client checks the file count
    against the server's own BULK_IMAGE_MAX_FILES (rendered into the page,
    not a separately-hardcoded number that could drift) before uploading
    anything, and (2) both fetch handlers parse the response defensively
    instead of assuming it's always JSON."""
    from app.uploads import BULK_IMAGE_MAX_FILES
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert f"const BULK_IMAGE_MAX_FILES = {BULK_IMAGE_MAX_FILES};" in r.text
    assert "count > BULK_IMAGE_MAX_FILES" in r.text
    assert "async function parseJsonResponse(res)" in r.text
    assert r.text.count("parseJsonResponse(res)") >= 2  # both fetch handlers use it
