"""Image discovery for the /images gallery tab: finds every image already
referenced somewhere in a world's content (entity portraits, inline
body/note markdown embeds, player character portraits/backstory/notes) so
the GM can browse and organize them into albums (see ImageAlbum in
app/models.py) without having to hunt through individual entities."""
import re

from sqlalchemy.orm import Session

from .models import Entity, PlayerCharacter, World

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def _extract_md_images(text):
    if not text:
        return []
    return _MD_IMG_RE.findall(text)


def discover_world_images(db: Session, world: World) -> list:
    """Every image used anywhere in `world`, deduplicated by URL. Each
    entry is {"url": ..., "uses": [{"label": ..., "href": ...}, ...]} —
    "uses" lists every place that image appears so the gallery can show
    provenance and link back to the source."""
    found = {}

    def _add(url, label, href):
        if not url:
            return
        entry = found.setdefault(url, {"url": url, "uses": []})
        entry["uses"].append({"label": label, "href": href})

    entities = db.query(Entity).filter(Entity.world_id == world.id).all()
    for e in entities:
        href = f"/entity/{e.id}"
        if e.image_url:
            _add(e.image_url, e.name, href)
        for url in _extract_md_images(e.body):
            _add(url, e.name, href)
        for note in e.notes:
            for url in _extract_md_images(note.content):
                _add(url, f"{e.name} (note)", href)

    pcs = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world.id).all()
    for pc in pcs:
        href = f"/characters/{pc.id}"
        if pc.portrait_url:
            _add(pc.portrait_url, pc.name, href)
        for url in _extract_md_images(pc.backstory):
            _add(url, f"{pc.name} (backstory)", href)
        for url in _extract_md_images(pc.notes):
            _add(url, f"{pc.name} (notes)", href)

    return sorted(found.values(), key=lambda entry: entry["url"])
