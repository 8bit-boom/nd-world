"""Tests for the /pages library (app/routers/pages.py, PageDoc/PageAlbum in
app/models.py) — mirrors tests/test_video.py's album-tree/visibility/
chunked-upload shape (minus anything video-specific: no conversion, no
poster frame). GET /pages and /pages/albums/{id} are player-safe; upload/
edit/delete/album-management stay GM-only, enforced both by the middleware
(no POST /pages/* entry there) and by an explicit check in each handler.

Also covers the one thing video.py's own tests have no analog for: an
uploaded .html file can carry its own <script> and is served same-origin
(see main.py's serve_upload), so /pages/{id}'s viewer and the raw file
response both need real isolation — checked here, not assumed.
"""
import io

import pytest

from app.database import SessionLocal
from app.models import PageAlbum, PageDoc

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_HTML_BYTES = b"<!DOCTYPE html><html><head><title>Test</title></head><body>Hello</body></html>"


def _html_file(name="doc.html"):
    return {"file": (name, io.BytesIO(_HTML_BYTES), "text/html")}


def _add_album(world_id, **kw):
    db = SessionLocal()
    try:
        a = PageAlbum(world_id=world_id, name=kw.pop("name", "Album"), **kw)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id
    finally:
        db.close()


def _add_doc(world_id, **kw):
    db = SessionLocal()
    try:
        d = PageDoc(world_id=world_id, name=kw.pop("name", "Doc"),
                    file_url=kw.pop("file_url", "/uploads/pages/x.html"), **kw)
        db.add(d)
        db.commit()
        db.refresh(d)
        return d.id
    finally:
        db.close()


# ── Upload ────────────────────────────────────────────────────────────────

def test_pages_upload_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/upload", data={"name": "Moonfall Calendar", "description": "Lunar almanac"},
                     files=_html_file(), follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        docs = db.query(PageDoc).filter(PageDoc.world_id == seed.world_a.id).all()
        assert len(docs) == 1
        assert docs[0].name == "Moonfall Calendar"
        assert docs[0].description == "Lunar almanac"
        assert docs[0].visible_to_players is False  # checkbox not sent in this request
        assert docs[0].file_url.startswith("/uploads/pages/")
    finally:
        db.close()


def test_pages_upload_defaults_name_to_filename(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/pages/upload", files=_html_file("hunt_calendar.html"))
    db = SessionLocal()
    try:
        doc = db.query(PageDoc).filter(PageDoc.world_id == seed.world_a.id).first()
        assert doc.name == "hunt_calendar"
    finally:
        db.close()


def test_pages_upload_accepts_htm_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/upload", files=_html_file("doc.htm"), follow_redirects=False)
    assert r.status_code == 303


def test_pages_upload_rejects_bad_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/upload",
                     files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert r.status_code == 400


def test_pages_upload_rejects_svg(client, seed):
    """Same reasoning as the site-wide SVG-can-carry-<script> rule this
    library is the .html analog of — SVG stays firmly out of scope here."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/upload",
                     files={"file": ("evil.svg", io.BytesIO(b"<svg/>"), "image/svg+xml")})
    assert r.status_code == 400


def test_pages_upload_forbidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/upload", files=_html_file())
    assert r.status_code == 403


def test_pages_page_reachable_by_gm_and_player(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/pages").status_code == 200
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/pages").status_code == 200


def test_pages_upload_form_shows_configured_size_limit(client, seed, monkeypatch):
    import app.routers.pages as pages_module

    monkeypatch.setattr(pages_module, "_MAX_PAGE_BYTES", 75 * 1024 * 1024)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/pages")
    assert "Up to 75 MB" in r.text


def test_pages_upload_rejects_file_over_configured_limit(client, seed, monkeypatch):
    import app.routers.pages as pages_module

    monkeypatch.setattr(pages_module, "_MAX_PAGE_BYTES", 10)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/upload", files=_html_file())
    assert r.status_code == 413
    db = SessionLocal()
    try:
        assert db.query(PageDoc).filter(PageDoc.world_id == seed.world_a.id).count() == 0
    finally:
        db.close()


def test_pages_upload_file_input_allows_multiple(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/pages")
    assert '<input type="file" name="file" id="pages-upload-file" accept=".html,.htm" multiple required/>' in r.text


def test_pages_bulk_upload_sequential_requests_create_separate_docs(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for fname in ("first_doc.html", "second_doc.html", "third_doc.html"):
        r = client.post(
            "/pages/upload",
            data={"description": "Shared batch description", "visible_to_players": "1"},
            files=_html_file(fname),
            follow_redirects=False,
        )
        assert r.status_code == 303
    db = SessionLocal()
    try:
        docs = db.query(PageDoc).filter(PageDoc.world_id == seed.world_a.id).order_by(PageDoc.name).all()
        assert [d.name for d in docs] == ["first_doc", "second_doc", "third_doc"]
        assert all(d.description == "Shared batch description" for d in docs)
        assert all(d.visible_to_players for d in docs)
        assert len({d.file_url for d in docs}) == 3  # distinct stored files, no collision
    finally:
        db.close()


def test_pages_player_only_sees_visible_docs(client, seed):
    _add_doc(seed.world_a.id, name="Visible Doc", visible_to_players=True)
    _add_doc(seed.world_a.id, name="GM Secret Doc", visible_to_players=False)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/pages")
    assert "Visible Doc" in r.text
    assert "GM Secret Doc" in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/pages")
    assert "Visible Doc" in r.text
    assert "GM Secret Doc" not in r.text


# ── Edit / delete ────────────────────────────────────────────────────────

def test_pages_edit_updates_fields(client, seed):
    did = _add_doc(seed.world_a.id, name="Old Name", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/{did}/edit",
                     data={"name": "New Name", "description": "Updated", "visible_to_players": "1"},
                     follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        doc = db.get(PageDoc, did)
        assert doc.name == "New Name"
        assert doc.description == "Updated"
        assert doc.visible_to_players is True
    finally:
        db.close()


def test_pages_edit_forbidden_for_player(client, seed):
    did = _add_doc(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/{did}/edit", data={"name": "Hacked"})
    assert r.status_code == 403


def test_pages_delete_removes_row_and_file(client, seed, tmp_path, monkeypatch):
    import app.routers.pages as pages_module
    monkeypatch.setattr(pages_module, "_UPLOADS_DIR", tmp_path)
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    f = pages_dir / "doc123.html"
    f.write_bytes(b"<html></html>")
    did = _add_doc(seed.world_a.id, file_url="/uploads/pages/doc123.html")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/{did}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not f.exists()
    db = SessionLocal()
    try:
        assert db.get(PageDoc, did) is None
    finally:
        db.close()


def test_pages_delete_forbidden_for_player(client, seed):
    did = _add_doc(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/{did}/delete")
    assert r.status_code == 403
    db = SessionLocal()
    try:
        assert db.get(PageDoc, did) is not None
    finally:
        db.close()


def test_pages_edit_cross_world_404s(client, seed):
    did = _add_doc(seed.world_b.id, name="Other World Doc")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/{did}/edit", data={"name": "Hijacked"})
    assert r.status_code == 404


# ── Albums and sub-albums ───────────────────────────────────────────────────

def test_pages_album_create_top_level(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/albums/new", data={"name": "Calendars"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        album = db.query(PageAlbum).filter(PageAlbum.world_id == seed.world_a.id).first()
        assert album.name == "Calendars"
        assert album.parent_id is None
    finally:
        db.close()


def test_pages_album_create_sub_album(client, seed):
    parent_id = _add_album(seed.world_a.id, name="Parent")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/albums/new", data={"name": "Child", "parent_id": str(parent_id)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/pages/albums/")
    db = SessionLocal()
    try:
        child = db.query(PageAlbum).filter(PageAlbum.name == "Child").first()
        assert child.parent_id == parent_id
    finally:
        db.close()


def test_pages_album_create_forbidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/albums/new", data={"name": "Hacked"})
    assert r.status_code == 403


def test_pages_album_detail_page_reachable_by_gm_and_player(client, seed):
    aid = _add_album(seed.world_a.id, name="Calendars")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/albums/{aid}").status_code == 200
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/albums/{aid}").status_code == 200


def test_pages_album_breadcrumb_shows_full_chain(client, seed):
    root_id = _add_album(seed.world_a.id, name="Root")
    mid_id = _add_album(seed.world_a.id, name="Middle", parent_id=root_id)
    leaf_id = _add_album(seed.world_a.id, name="Leaf", parent_id=mid_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/albums/{leaf_id}")
    assert r.status_code == 200
    assert "Root" in r.text
    assert "Middle" in r.text
    assert "Leaf" in r.text


def test_pages_album_shows_only_own_docs_and_sub_albums(client, seed):
    aid = _add_album(seed.world_a.id, name="Album A")
    sub_id = _add_album(seed.world_a.id, name="Sub Album", parent_id=aid)
    _add_doc(seed.world_a.id, name="In Album", album_id=aid, visible_to_players=True)
    _add_doc(seed.world_a.id, name="Top Level Doc", album_id=None, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/albums/{aid}")
    assert "In Album" in r.text
    assert "Top Level Doc" not in r.text
    assert "Sub Album" in r.text


def test_pages_album_player_only_sees_visible_docs_inside(client, seed):
    aid = _add_album(seed.world_a.id, name="Album A")
    _add_doc(seed.world_a.id, name="Visible In Album", album_id=aid, visible_to_players=True)
    _add_doc(seed.world_a.id, name="Hidden In Album", album_id=aid, visible_to_players=False)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/albums/{aid}")
    assert "Visible In Album" in r.text
    assert "Hidden In Album" not in r.text


def test_pages_album_rename(client, seed):
    aid = _add_album(seed.world_a.id, name="Old Name")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/albums/{aid}/rename", data={"name": "New Name"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(PageAlbum, aid).name == "New Name"
    finally:
        db.close()


def test_pages_album_delete_cascades_to_sub_albums_and_docs(client, seed, tmp_path, monkeypatch):
    import app.routers.pages as pages_module
    monkeypatch.setattr(pages_module, "_UPLOADS_DIR", tmp_path)
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    f = pages_dir / "nested.html"
    f.write_bytes(b"<html></html>")

    root_id = _add_album(seed.world_a.id, name="Root")
    child_id = _add_album(seed.world_a.id, name="Child", parent_id=root_id)
    doc_in_root_id = _add_doc(seed.world_a.id, name="Root Doc", album_id=root_id,
                               file_url="/uploads/pages/nested.html")
    doc_in_child_id = _add_doc(seed.world_a.id, name="Child Doc", album_id=child_id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/albums/{root_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/pages"
    assert not f.exists()

    db = SessionLocal()
    try:
        assert db.get(PageAlbum, root_id) is None
        assert db.get(PageAlbum, child_id) is None
        assert db.get(PageDoc, doc_in_root_id) is None
        assert db.get(PageDoc, doc_in_child_id) is None
    finally:
        db.close()


def test_pages_album_delete_redirects_to_parent(client, seed):
    root_id = _add_album(seed.world_a.id, name="Root")
    child_id = _add_album(seed.world_a.id, name="Child", parent_id=root_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/albums/{child_id}/delete", follow_redirects=False)
    assert r.headers["location"] == f"/pages/albums/{root_id}"


def test_pages_album_delete_forbidden_for_player(client, seed):
    aid = _add_album(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/albums/{aid}/delete")
    assert r.status_code == 403


def test_pages_album_cross_world_404s(client, seed):
    aid = _add_album(seed.world_b.id, name="Other World Album")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/albums/{aid}")
    assert r.status_code == 404


def test_pages_upload_into_album(client, seed):
    aid = _add_album(seed.world_a.id, name="Calendars")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/upload", data={"album_id": str(aid)}, files=_html_file(), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/pages/albums/{aid}"
    db = SessionLocal()
    try:
        doc = db.query(PageDoc).filter(PageDoc.world_id == seed.world_a.id).first()
        assert doc.album_id == aid
    finally:
        db.close()


def test_pages_edit_moves_doc_between_albums(client, seed):
    aid = _add_album(seed.world_a.id, name="Destination")
    did = _add_doc(seed.world_a.id, name="Doc", album_id=None)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/{did}/edit", data={"name": "Doc", "album_id": str(aid)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/pages/albums/{aid}"
    db = SessionLocal()
    try:
        assert db.get(PageDoc, did).album_id == aid
    finally:
        db.close()


# ── Chunked upload ───────────────────────────────────────────────────────

def test_pages_chunked_upload_creates_doc(client, seed, tmp_path, monkeypatch):
    import app.routers.pages as pages_module
    monkeypatch.setattr(pages_module, "_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(pages_module, "_CHUNKS_ROOT", tmp_path / "pages" / "_chunks")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "7" * 32
    r0 = client.post("/pages/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files={"file": ("part", io.BytesIO(b"<html>" + b"x" * 100), "application/octet-stream")})
    assert r0.status_code == 200
    r = client.post("/pages/upload/complete", data={
        "upload_id": upload_id, "filename": "calendar.html", "total_chunks": "1",
    })
    assert r.status_code == 200
    db = SessionLocal()
    try:
        doc = db.query(PageDoc).filter(PageDoc.world_id == seed.world_a.id).first()
        assert doc is not None
        assert doc.file_url.endswith(".html")
    finally:
        db.close()


def test_pages_chunk_upload_forbidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/upload/chunk", data={"upload_id": "a" * 32, "chunk_index": "0"},
                     files={"file": ("part", io.BytesIO(b"x"), "application/octet-stream")})
    assert r.status_code == 403


# ── /pages/{id} full-page viewer ─────────────────────────────────────────

def test_pages_viewer_reachable_by_gm_and_player_when_visible(client, seed):
    did = _add_doc(seed.world_a.id, name="Calendar", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/{did}").status_code == 200
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/{did}").status_code == 200


def test_pages_viewer_404s_for_player_when_hidden(client, seed):
    did = _add_doc(seed.world_a.id, name="GM Secret", visible_to_players=False)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/{did}")
    assert r.status_code == 404


def test_pages_viewer_reachable_by_gm_when_hidden(client, seed):
    did = _add_doc(seed.world_a.id, name="GM Secret", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/{did}").status_code == 200


def test_pages_viewer_cross_world_404s(client, seed):
    did = _add_doc(seed.world_b.id, name="Other World Doc")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/{did}")
    assert r.status_code == 404


# ── /pages/{id}/download ──────────────────────────────────────────────────
# The raw file_url (what 'Open'/the viewer iframe use) deliberately does
# NOT force a download (see main.py's serve_upload) — this route serves the
# identical file with Content-Disposition: attachment instead, gated by the
# exact same visibility rule as the viewer (a page a viewer can already
# open is exactly the set they may also download).

def _add_downloadable_doc(tmp_path, monkeypatch, world_id, **kw):
    from app.routers import pages as pages_module
    monkeypatch.setattr(pages_module, "_UPLOADS_DIR", tmp_path)
    (tmp_path / "pages").mkdir(exist_ok=True)
    fname = kw.pop("fname", "doc.html")
    (tmp_path / "pages" / fname).write_bytes(_HTML_BYTES)
    return _add_doc(world_id, file_url=f"/uploads/pages/{fname}", **kw)


def test_pages_download_gm(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_doc(tmp_path, monkeypatch, seed.world_a.id, name="Calendar", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/{did}/download")
    assert r.status_code == 200
    assert r.content == _HTML_BYTES
    assert 'attachment; filename="Calendar.html"' in r.headers["content-disposition"]


def test_pages_download_player_when_visible(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_doc(tmp_path, monkeypatch, seed.world_a.id, name="Calendar", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/{did}/download")
    assert r.status_code == 200
    assert r.content == _HTML_BYTES


def test_pages_download_404s_for_player_when_hidden(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_doc(tmp_path, monkeypatch, seed.world_a.id, name="GM Secret", visible_to_players=False)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/{did}/download").status_code == 404


def test_pages_download_reachable_by_gm_when_hidden(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_doc(tmp_path, monkeypatch, seed.world_a.id, name="GM Secret", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/{did}/download").status_code == 200


def test_pages_download_cross_world_404s(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_doc(tmp_path, monkeypatch, seed.world_b.id, name="Other World Doc")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/{did}/download").status_code == 404


def test_pages_download_sanitizes_special_characters_in_filename(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_doc(tmp_path, monkeypatch, seed.world_a.id,
                                 name="Hunt: Moonlight/Chase?.html", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/{did}/download")
    assert r.status_code == 200
    disposition = r.headers["content-disposition"]
    assert "/" not in disposition and ":" not in disposition and "?" not in disposition


def test_pages_download_missing_file_on_disk_404s(client, seed, tmp_path, monkeypatch):
    """The DB row exists but its file is gone (e.g. manually deleted from
    disk) — a 500 would leak a stack trace, 404 matches every other
    doesn't-actually-exist case this router already returns."""
    from app.routers import pages as pages_module
    monkeypatch.setattr(pages_module, "_UPLOADS_DIR", tmp_path)
    did = _add_doc(seed.world_a.id, name="Ghost Doc", visible_to_players=True, file_url="/uploads/pages/gone.html")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/{did}/download").status_code == 404


def test_pages_viewer_iframe_is_sandboxed_without_allow_same_origin(client, seed):
    """The one thing an HTML library needs that video/audio never did — see
    page_viewer.html's own comment for why allow-same-origin must never be
    added here (combined with allow-scripts, that's the documented
    iframe-sandbox escape)."""
    did = _add_doc(seed.world_a.id, name="Calendar", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/{did}")
    assert 'sandbox="allow-scripts allow-popups"' in r.text
    assert "allow-same-origin" not in r.text


# ── Served-file security headers (main.py's serve_upload) ────────────────

def test_uploaded_html_gets_sandboxed_csp_and_frame_headers(client, seed, tmp_path, monkeypatch):
    from app import main as main_module
    monkeypatch.setattr(main_module, "UPLOADS_DIR", tmp_path)
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "doc.html").write_bytes(_HTML_BYTES)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/uploads/pages/doc.html")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == "sandbox allow-scripts allow-popups"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["content-type"].startswith("text/html")


def test_uploaded_htm_also_gets_sandboxed_headers(client, seed, tmp_path, monkeypatch):
    from app import main as main_module
    monkeypatch.setattr(main_module, "UPLOADS_DIR", tmp_path)
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "doc.htm").write_bytes(_HTML_BYTES)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/uploads/pages/doc.htm")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == "sandbox allow-scripts allow-popups"


def test_other_upload_types_do_not_get_html_sandbox_headers(client, seed, tmp_path, monkeypatch):
    """Regression guard: the new .html/.htm branch in serve_upload must not
    leak its headers onto unrelated uploads (images, audio, video)."""
    from app import main as main_module
    monkeypatch.setattr(main_module, "UPLOADS_DIR", tmp_path)
    (tmp_path / "video").mkdir()
    (tmp_path / "video" / "clip.mp4").write_bytes(b"fake mp4")

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/uploads/video/clip.mp4")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert "x-frame-options" not in r.headers


# ── Nav visibility ───────────────────────────────────────────────────────────

def test_nav_shows_pages_link_to_everyone(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/pages"' in r.text
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/pages"' in r.text
