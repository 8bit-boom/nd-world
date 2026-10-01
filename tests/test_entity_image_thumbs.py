"""Pages that show an image in a small box load its small WebP thumbnail (when one exists) and only
fetch the original when it is opened full size — the entity page used to download the whole original
(often a 1 MB+ PNG) into a 160px portrait, which is what made it slow to appear."""
import json

import pytest

from app.database import SessionLocal
from app.main import UPLOADS_DIR
from app.models import Entity, PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _upload(name, with_thumb=True):
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / f"{name}.png").write_bytes(b"png")
    if with_thumb:
        (UPLOADS_DIR / f"{name}_thumb.webp").write_bytes(b"webp")
    return f"/uploads/{name}.png"


def _entity(seed, url):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="npc", name="Portrait Test", summary="s", body="b", image_url=url)
        db.add(e)
        db.commit()
        return e.id
    finally:
        db.close()


def test_entity_page_loads_the_thumbnail_and_keeps_the_original_for_the_lightbox(client, seed):
    _gm(client, seed)
    eid = _entity(seed, _upload("heroic"))
    html = client.get(f"/entity/{eid}").text
    assert 'class="detail-img" src="/uploads/heroic_thumb.webp"' in html
    assert 'data-full="/uploads/heroic.png"' in html, "the second screen and the lightbox use the original"
    assert "openLightbox(this.dataset.full || this.src" in html


def test_entity_page_falls_back_to_the_original_when_there_is_no_thumbnail(client, seed):
    _gm(client, seed)
    eid = _entity(seed, _upload("old-upload", with_thumb=False))
    html = client.get(f"/entity/{eid}").text
    assert 'class="detail-img" src="/uploads/old-upload.png"' in html
    assert "_thumb.webp" not in html.split('class="detail-img"')[1].split(">")[0]
    eid = _entity(seed, "/static/races/standard/high-elveselves_6988428.png")
    assert 'class="detail-img" src="/static/races/standard/high-elveselves_6988428.png"' in client.get(f"/entity/{eid}").text


def test_hover_preview_offers_the_thumbnail_but_keeps_image_url_for_image_studio(client, seed):
    _gm(client, seed)
    eid = _entity(seed, _upload("hovered"))
    d = client.get(f"/api/entity/{eid}/preview").json()
    assert d["image_url"] == "/uploads/hovered.png" and d["thumb_url"] == "/uploads/hovered_thumb.webp"
    none = client.get(f"/api/entity/{_entity(seed, None)}/preview").json()
    assert not none["image_url"] and not none["thumb_url"]
    assert "data.thumb_url || data.image_url" in client.get("/").text


def test_character_sheets_use_the_thumbnail_portrait(client, seed):
    _gm(client, seed)
    url = _upload("portrait")
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=seed.world_a.id, name="Mini", slug="mini-thumb", sheet_mode="custom",
                            fields_json=json.dumps([{"id": "a", "label": "A", "type": "text", "section": "S"}]))
        db.add(tpl)
        db.commit()
        native = PlayerCharacter(world_id=seed.world_a.id, name="Native", portrait_url=url)
        custom = PlayerCharacter(world_id=seed.world_a.id, name="Custom", portrait_url=url, sheet_template_id=tpl.id)
        db.add_all([native, custom])
        db.commit()
        ids = (native.id, custom.id)
    finally:
        db.close()
    for pid in ids:
        html = client.get(f"/characters/{pid}").text
        assert 'src="/uploads/portrait_thumb.webp" data-full="/uploads/portrait.png"' in html, pid
        assert "openLightbox(this.dataset.full || this.src" in html
