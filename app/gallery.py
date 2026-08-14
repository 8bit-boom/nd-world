"""Image discovery for the /images gallery tab: finds every image already
referenced somewhere in a world's content (entity portraits, inline
body/note markdown embeds, player character portraits/backstory/notes) so
the GM can browse and organize them into albums (see ImageAlbum in
app/models.py) without having to hunt through individual entities."""
import json
import re

from sqlalchemy.orm import Session

from .models import Entity, ImageAlbum, PlayerCharacter, World

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def _extract_md_images(text):
    if not text:
        return []
    return _MD_IMG_RE.findall(text)


def image_display_name(url: str, uses: list = None) -> str:
    """Best human-readable label for an image: the name of the first place
    it's used (e.g. "Portrait NPC" — what a GM actually thinks of the image
    as), or its filename if it isn't used anywhere yet (e.g. a fresh upload
    sitting only in an album)."""
    if uses:
        return uses[0]["label"]
    return url.rsplit("/", 1)[-1]


def discover_world_images(db: Session, world: World) -> list:
    """Every image used anywhere in `world`, deduplicated by URL. Each
    entry is {"url": ..., "name": ..., "uses": [{"label": ..., "href": ...}, ...]} —
    "uses" lists every place that image appears so the gallery can show
    provenance and link back to the source; "name" is the display label
    (see image_display_name)."""
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

    for entry in found.values():
        entry["name"] = image_display_name(entry["url"], entry["uses"])
    return sorted(found.values(), key=lambda entry: entry["url"])


def all_world_image_urls(db: Session, world: World) -> list:
    """Every image available to pick from for `world`: everything
    discover_world_images() finds already in use, plus every image sitting
    in an album that isn't (yet) referenced anywhere else — e.g. a fresh
    gallery upload nobody has attached to an entity yet. Deduplicated,
    sorted by URL. Each entry is {"url": ..., "name": ...} — used by the
    entity form's "choose from gallery" image picker (see
    app/templates/entities/form.html) so a GM can reuse any image they've
    ever uploaded, not just ones already in use, and see a meaningful label
    for each rather than a bare UUID filename."""
    discovered = {entry["url"]: entry["name"] for entry in discover_world_images(db, world)}
    names = dict(discovered)
    albums = db.query(ImageAlbum).filter(ImageAlbum.world_id == world.id).all()
    for album in albums:
        try:
            album_urls = json.loads(album.image_urls_json or "[]")
        except (TypeError, ValueError):
            album_urls = []
        if isinstance(album_urls, list):
            for u in album_urls:
                if isinstance(u, str) and u not in names:
                    names[u] = image_display_name(u)
    return [{"url": u, "name": names[u]} for u in sorted(names)]
