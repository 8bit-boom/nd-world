"""Image discovery for the /images gallery tab: finds every image already
referenced somewhere in a world's content (entity portraits, inline
body/note markdown embeds, player character portraits/backstory/notes) so
the GM can browse and organize them into albums (see ImageAlbum in
app/models.py) without having to hunt through individual entities."""
import json
import re

from sqlalchemy.orm import Session, selectinload

from .models import Entity, ImageAlbum, MediaTitle, PlayerCharacter, World

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")

# Matches the random hex prefix app/uploads.py's unique_upload_filename()
# puts on every stored filename (e.g. "a1b2c3d4e5f6-goblin-portrait.jpg") so
# it can be stripped back off, revealing the uploader's original name
# instead of a meaningless hex string.
_UPLOAD_PREFIX_RE = re.compile(r"^[0-9a-f]{12}-")


def _extract_md_images(text):
    if not text:
        return []
    return _MD_IMG_RE.findall(text)


def image_display_name(url: str, uses: list = None) -> str:
    """Best human-readable label for an image: the name of the first place
    it's used (e.g. "Portrait NPC" — what a GM actually thinks of the image
    as), or its (de-prefixed) filename if it isn't used anywhere yet (e.g. a
    fresh upload sitting only in an album)."""
    if uses:
        return uses[0]["label"]
    fname = url.rsplit("/", 1)[-1]
    return _UPLOAD_PREFIX_RE.sub("", fname) or fname


def title_overrides(db: Session, world_id: int) -> dict:
    """{url: title} - the names given to images by hand or by the AI renamer (MediaTitle). They win over the name
    the gallery would derive from where an image is used or from its file name."""
    return {url: title for url, title in db.query(MediaTitle.url, MediaTitle.title).filter(MediaTitle.world_id == world_id).all()}


def world_image_names(db: Session, world: World, urls: list) -> dict:
    """{url: display name} for `urls`: a chosen title, else where it is used, else its file name."""
    overrides = title_overrides(db, world.id)
    used = {e["url"]: e["name"] for e in discover_world_images(db, world)}
    return {u: overrides.get(u) or used.get(u) or image_display_name(u) for u in urls}


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

    # selectinload(Entity.notes): without it, the loop below's `e.notes`
    # lazy-loads once per entity — one extra SQL query per entity in the
    # world, every single /images page load. This batches all of them into
    # one additional query (a single `WHERE entity_id IN (...)`) instead.
    entities = (
        db.query(Entity).options(selectinload(Entity.notes))
        .filter(Entity.world_id == world.id).all()
    )
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

    # Pictures that go with a session (app/session_media.py): used there, so the gallery names them after the session and
    # refuses to delete them from under it.
    from .session_media import media_items
    from .models import GameSession
    for gs in db.query(GameSession).filter(GameSession.world_id == world.id).all():
        for it in media_items(gs):
            if it["kind"] == "image":
                _add(it["url"], f"{gs.title} (session media)", f"/sessions/{gs.id}")

    overrides = title_overrides(db, world.id)
    for entry in found.values():
        entry["name"] = overrides.get(entry["url"]) or image_display_name(entry["url"], entry["uses"])
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
    overrides = title_overrides(db, world.id)
    albums = db.query(ImageAlbum).filter(ImageAlbum.world_id == world.id).all()
    for album in albums:
        try:
            album_urls = json.loads(album.image_urls_json or "[]")
        except (TypeError, ValueError):
            album_urls = []
        if isinstance(album_urls, list):
            for u in album_urls:
                if isinstance(u, str) and u not in names:
                    names[u] = overrides.get(u) or image_display_name(u)
    return [{"url": u, "name": names[u]} for u in sorted(names)]
