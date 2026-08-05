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
    assert "toUpload.length > BULK_IMAGE_MAX_FILES" in r.text
    assert "async function parseJsonResponse(res)" in r.text
    assert r.text.count("parseJsonResponse(res)") >= 2  # both fetch handlers use it


def test_import_page_flags_same_name_different_kind_as_ambiguous(client, seed):
    """Regression guard, source-level (no JS runtime in this test suite).
    matchEntityByFilename() used to resolve an exact filename match with
    Array.find(), silently taking whichever entity came first in the
    kind-then-name-sorted ENTITIES list — so a "Darro" race and a "Darro"
    lore note (a normal thing for a world to have, not a data error) meant
    art meant for the race silently landed on the note instead, with no
    warning at all, since only *fuzzy* matches got the "check match" badge.
    Locks in: (1) the client detects when more than one entity shares an
    exact normalized name, (2) it deprioritizes kinds unlikely to be the
    intended portrait target (note, event) when picking a default, and (3)
    it surfaces a distinct, visible warning either way rather than staying
    silent just because the name match itself was exact."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    _make_entity(seed.world_a, name="Darro", kind="race")
    _make_entity(seed.world_a, name="Darro", kind="note")

    r = client.get("/import")
    assert r.status_code == 200
    assert "const MATCH_KIND_DEPRIORITIZED" in r.text
    assert "'ambiguous'" in r.text
    assert "exactMatches.length > 1" in r.text
    assert "same name in" in r.text  # the warning badge text
    # Both same-named entities must actually reach the client for the
    # ambiguity check to have anything to detect (entities_json is
    # double-JSON-encoded into the page via Jinja's |tojson, so check for
    # the name text itself rather than an exact quoting/spacing pattern).
    assert r.text.count("Darro") >= 2


def test_import_page_bulk_image_upload_is_chunked_by_size(client, seed):
    """A real live-site batch of 100 portrait/art files (several MB each)
    sent as one multipart request regularly exceeded a reverse proxy's body-
    size limit in front of the app — the app's own per-file MAX_UPLOAD_BYTES
    never even came into play, since the proxy rejected the request before
    FastAPI saw it, returning a non-JSON 413 (which parseJsonResponse — see
    the test above — already turned into a clean message instead of a raw
    crash, but the upload itself still just failed outright). The client now
    splits into byte-bounded chunks and uploads them as separate sequential
    requests to /api/import/images, so no single request need be anywhere
    near that size, and one bad chunk doesn't sink files that already
    succeeded via earlier chunks. Source-level guard (no JS runtime in this
    test suite)."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert "function chunkUploadItems(items)" in r.text
    assert "IMG_UPLOAD_CHUNK_MAX_BYTES" in r.text
    # Chunking must be driven by cumulative byte size, not just a fixed file
    # count — a handful of large files can be just as oversized as a hundred
    # small ones.
    assert "currentBytes + item.file.size > IMG_UPLOAD_CHUNK_MAX_BYTES" in r.text
    # Each chunk goes through the same fetch, and a failed chunk (network
    # error, non-JSON response, or a server-side error) must be recorded per
    # file and the loop must continue to the next chunk rather than aborting
    # the whole batch.
    idx = r.text.index("function chunkUploadItems(items)")
    handler_idx = r.text.index("img-import-btn').addEventListener('click'", idx)
    handler_end = r.text.index("\n});", handler_idx)
    handler_src = r.text[handler_idx:handler_end]
    assert "for (const chunk of chunks)" in handler_src
    assert "allResults.push" in handler_src
