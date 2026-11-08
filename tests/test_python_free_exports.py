"""Every download of the Export & Backup hub is answered by a gateway with no Python behind it (ND_PYTHON_FREE), also for a world with accented names,
custom kinds, rich notes and odd stored values - not the "Not available here" page."""
from app.database import SessionLocal
from app.models import Entity, EntityNote

from .conftest import GM_PASSWORD
from .test_gateway_golden import _python_free_gateway
from .training_bundle_seed import seed_rich_world

HUB_DOWNLOADS = ["/admin/backup.zip", "/export/book.zip", "/export/rules-and-notes.md", "/export/training.ndtrain", "/export/foundry.json"]


def test_every_hub_download_works_without_python(client, seed):
    seed_rich_world(seed.world_a.id)
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="side_quest", name="Œuvre — Ñandú", body="# T\n**b** “q” 日本語", tags="é,ü",
                   custom_fields_json='{"a": null, "b": [1, {"c": 2}]}', image_url="/uploads/does-not-exist.png")
        db.add(e)
        db.add(Entity(world_id=seed.world_a.id, kind="note", name="Café", subtype="récit", body="Naïve"))
        for bad in db.query(Entity).filter(Entity.custom_fields_json == "not json"):
            bad.custom_fields_json = "{}"                 # Python's own world export is a 500 on unreadable JSON, so there is nothing to match
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Odd", custom_fields_json="[1,2]", body=None, summary=None))
        db.commit()
        db.add(EntityNote(entity_id=e.id, content="Pinned — note", visible_to_players=False))
        db.commit()
        wid = seed.world_a.id
    finally:
        db.close()
    proc, url, g = _python_free_gateway(seed)
    try:
        paths = HUB_DOWNLOADS + [f"/worlds/{wid}/export", f"/worlds/{wid}/export/split"]
        bad = {}
        for p in paths:
            r = g.get(p)
            if r.status_code != 200 or "Not available here" in r.text[:2000]:
                bad[p] = (r.status_code, r.text[:120])
        assert not bad, bad
        page = g.get(f"/worlds/{wid}/export/split")
        assert "rules.json" in page.text and "player-characters.json" in page.text and "/export/entities/note.json" in page.text
        assert page.headers.get("x-nd-served-by") == "gateway"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
