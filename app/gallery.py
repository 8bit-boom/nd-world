"""Image discovery for the /images gallery tab: finds every image already
referenced somewhere in a world's content (entity portraits, inline
body/note markdown embeds, player character portraits/backstory/notes) so
the GM can browse and organize them into albums (see ImageAlbum in
app/models.py) without having to hunt through individual entities."""
import json
import re

from sqlalchemy.orm import Session

from .models import Entity, EntityNote, ImageAlbum, MediaTitle, PlayerCharacter, World

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
    # Plain columns, not Entity objects: only the portrait, the body text and the notes matter here, and the text is pre-filtered in SQL to rows that
    # contain a markdown image at all ("![") - building 5,000 ORM objects and regex-scanning every body made this the slowest part of /maps for a GM.
    names = {}
    portraits = {}
    for eid, name, image_url in db.query(Entity.id, Entity.name, Entity.image_url).filter(Entity.world_id == world.id).order_by(Entity.id).all():
        names[eid] = name
        if image_url:
            portraits[eid] = image_url
    body_urls = {eid: body for eid, body in db.query(Entity.id, Entity.body).filter(Entity.world_id == world.id, Entity.body.contains("![")).all()}
    note_rows: dict = {}
    for eid, content in (
        db.query(EntityNote.entity_id, EntityNote.content)
        .join(Entity, Entity.id == EntityNote.entity_id)
        .filter(Entity.world_id == world.id, EntityNote.content.contains("!["))
        .order_by(EntityNote.id).all()
    ):
        note_rows.setdefault(eid, []).append(content)
    for eid, name in names.items():
        href = f"/entity/{eid}"
        if eid in portraits:
            _add(portraits[eid], name, href)
        for url in _extract_md_images(body_urls.get(eid)):
            _add(url, name, href)
        for content in note_rows.get(eid, ()):
            for url in _extract_md_images(content):
                _add(url, f"{name} (note)", href)

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

    # Pictures a character kept from what the table was shown (hub > Kept): used there, so the gallery will not delete them. A player's OWN
    # uploads are not listed here - they are theirs, not part of the world's gallery.
    from .models import CharacterKeepsake
    pc_names = {pc.id: pc.name for pc in pcs}
    for k in db.query(CharacterKeepsake).filter(CharacterKeepsake.world_id == world.id, CharacterKeepsake.kind == "image",
                                                CharacterKeepsake.own.is_(False)).all():
        _add(k.url, f"{pc_names.get(k.character_id, 'A character')} (kept)", f"/characters/{k.character_id}")

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


_DERIVED_RE = re.compile(r"^(.*)_(thumb|display)\.(webp|avif)$", re.I)


def resolve_sendable_image(db: Session, world: World, url: str, entries: dict):
    """(url, name) of the picture a GM may send to the players for `url`, or None. `entries` is {url: name} from all_world_image_urls().
    A picture the page shows as its small / display copy (`x_thumb.webp`, `x_display.webp`, `x.png?display=1`) is the original it was made from;
    a picture the GM wrote into this world's own text by hand (an HTML <img>, a sized markdown embed the gallery does not list) is the world's
    too. Anything else on disk is not."""
    base = url.split("?", 1)[0].split("#", 1)[0]
    if base in entries:
        return base, entries[base]
    m = _DERIVED_RE.match(base)
    if m:
        for u in sorted(entries):
            if u.rsplit(".", 1)[0] == m.group(1):
                return u, entries[u]
    if base.startswith("/uploads/") and ".." not in base and "\\" not in base:
        from sqlalchemy import func, or_
        hit = db.query(Entity.id).filter(Entity.world_id == world.id, or_(
            func.instr(func.coalesce(Entity.body, ""), base) > 0, func.instr(func.coalesce(Entity.summary, ""), base) > 0,
            func.instr(func.coalesce(Entity.image_url, ""), base) > 0)).first()
        if not hit:
            hit = (db.query(EntityNote.id).join(Entity, Entity.id == EntityNote.entity_id)
                   .filter(Entity.world_id == world.id, func.instr(func.coalesce(EntityNote.content, ""), base) > 0).first())
        if not hit:
            hit = db.query(PlayerCharacter.id).filter(PlayerCharacter.world_id == world.id, or_(
                func.instr(func.coalesce(PlayerCharacter.backstory, ""), base) > 0, func.instr(func.coalesce(PlayerCharacter.notes, ""), base) > 0)).first()
        if hit:
            return base, title_overrides(db, world.id).get(base) or image_display_name(base)
    return None
