"""Shared album-tree helpers for the four per-world media library routers
(app/routers/audio.py, video.py, pages.py, gallery.py). Each router keeps
its own Album/Item SQLAlchemy models (AudioAlbum/AudioClip, VideoAlbum/
VideoClip, PageAlbum/PageDoc, ImageAlbum) — the tree-walking and counting
logic itself was identical across all four modulo which model class it
queried, and had already begun to drift (audio.py named its counter
_clip_counts while pages.py said _doc_counts) before being centralized
here (docs/AUDIT_PLAN_NEXT.md item 15).

No router imports here — main.py imports every router, so a router
importing back from main.py would be circular; this module has nothing to
do with main.py at all, so it's safe for every router (and main.py itself)
to import from.
"""
import os
from pathlib import Path

from sqlalchemy import func

UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"


def breadcrumb(db, album_model, album) -> list:
    """Root-to-current chain of parent albums (not including `album`
    itself). Capped at 50 hops as cheap insurance against a corrupted
    parent_id chain — normal nesting never gets remotely this deep since
    each router's own per-world album cap bounds the whole tree anyway."""
    chain = []
    current = album
    for _ in range(50):
        if not current.parent_id:
            break
        parent = db.get(album_model, current.parent_id)
        if not parent:
            break
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


def descendant_albums(db, album_model, root_id: int) -> list:
    """Every `album_model` row nested (at any depth) under root_id, for
    cascade delete — deleting a folder removes its sub-albums (and
    whatever they contain) with it."""
    result = []
    frontier = [root_id]
    while frontier:
        children = db.query(album_model).filter(album_model.parent_id.in_(frontier)).all()
        if not children:
            break
        result.extend(children)
        frontier = [c.id for c in children]
    return result


def sub_album_counts(db, album_model, album_ids: list) -> dict:
    """{album_id: direct sub-album count} for the given albums — one GROUP
    BY query, not one COUNT per album."""
    if not album_ids:
        return {}
    rows = (
        db.query(album_model.parent_id, func.count(album_model.id))
        .filter(album_model.parent_id.in_(album_ids))
        .group_by(album_model.parent_id)
        .all()
    )
    return dict(rows)


def child_counts(db, item_model, album_ids: list, visible_only: bool = False) -> dict:
    """{album_id: item count} for the given albums — one GROUP BY query,
    not one COUNT per album. `visible_only` restricts to
    item_model.visible_to_players rows, for a non-GM viewer who must never
    see a count that includes content they can't open."""
    if not album_ids:
        return {}
    q = db.query(item_model.album_id, func.count(item_model.id)).filter(item_model.album_id.in_(album_ids))
    if visible_only:
        q = q.filter(item_model.visible_to_players.is_(True))
    return dict(q.group_by(item_model.album_id).all())
