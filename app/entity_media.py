"""Pictures, audio and video on a lore entity - the leaf module (no router imports) the entity page, the Media tab's routes and the gallery
share.

An entity's Media tab (templates/entities/_media.html) shows three things:
  * pictures - the portrait, the pictures kept on the entity itself (Entity.media_images_json) and the ones written into its body and notes;
  * audio and video - clips of the /audio and /video libraries whose entity_id is this entity (a clip belongs to one entity at a time).

A picture kept on the entity is only a URL, as in the gallery's albums (ImageAlbum): taking it off never deletes a file, and the gallery counts
it as used, so it cannot be deleted from under the entity. Each has a uid (what the tab's routes name it by), a caption and its own "players can
see" switch; audio and video keep the library's own switch.
"""
import json
import re
import uuid

MAX_PICTURES = 100        # the most pictures one entity keeps (a longer stored list is cut when it is read)
MAX_CAPTION = 200
MAX_NAME = 200
_URL_SCHEMES = ("http://", "https://")


# A picture written into text as markdown: ![alt](url)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def md_images(text) -> list:
    """The picture addresses markdown image tags in `text` point at, in order."""
    return _MD_IMG_RE.findall(text) if text else []


def new_uid() -> str:
    return uuid.uuid4().hex[:10]


def _kept_url(url) -> bool:
    """A picture address this tab shows: a file of this app, or a web address - never anything else (data:, javascript:, ...)."""
    return isinstance(url, str) and 0 < len(url) <= 512 and (url.startswith(("/uploads/", "/static/")) or url.startswith(_URL_SCHEMES))


def pictures(entity) -> list:
    """The entity's kept pictures, cleaned (load_pictures on what it stores)."""
    return load_pictures(getattr(entity, "media_images_json", None))


def load_pictures(stored) -> list:
    """A stored picture list, cleaned: junk and repeats dropped, every one given a uid, at most MAX_PICTURES."""
    try:
        raw = json.loads(stored or "[]")
    except (TypeError, ValueError):
        return []
    out, seen = [], set()
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict) or not _kept_url(it.get("url")) or it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append({"uid": str(it.get("uid") or new_uid())[:32], "url": it["url"],
                    "caption": str(it.get("caption") or "")[:MAX_CAPTION], "visible": bool(it.get("visible"))})
    return out[:MAX_PICTURES]


def store_pictures(entity, items: list) -> None:
    entity.media_images_json = json.dumps(items[:MAX_PICTURES])


def picture_dict(item: dict) -> dict:
    return {"uid": item["uid"], "url": item["url"], "caption": item["caption"], "visible_to_players": bool(item["visible"])}


def visible_pictures(entity, *, is_gm: bool) -> list:
    """What a viewer may see: everything for a GM, only the ticked ones for anyone else."""
    return [picture_dict(i) for i in pictures(entity) if is_gm or i["visible"]]


def add_picture(entity, url: str, caption: str = "", visible: bool = False) -> bool:
    """Keep a picture on the entity. False when it is already there or the entity is full - the list is left as it was."""
    items = pictures(entity)
    if any(i["url"] == url for i in items) or len(items) >= MAX_PICTURES:
        return False
    items.append({"uid": new_uid(), "url": url, "caption": (caption or "")[:MAX_CAPTION], "visible": bool(visible)})
    store_pictures(entity, items)
    return True


def attached_audio(db, entity, *, is_gm: bool) -> list:
    """The audio clips that belong to this entity (its own world), newest first; a player only the ones marked visible."""
    from .models import AudioClip
    q = db.query(AudioClip).filter(AudioClip.entity_id == entity.id, AudioClip.world_id == entity.world_id)
    if not is_gm:
        q = q.filter(AudioClip.visible_to_players.is_(True))
    return q.order_by(AudioClip.created_at.desc(), AudioClip.id.desc()).all()


def attached_video(db, entity, *, is_gm: bool) -> list:
    """The video clips that belong to this entity - the same rule as attached_audio."""
    from .models import VideoClip
    q = db.query(VideoClip).filter(VideoClip.entity_id == entity.id, VideoClip.world_id == entity.world_id)
    if not is_gm:
        q = q.filter(VideoClip.visible_to_players.is_(True))
    return q.order_by(VideoClip.created_at.desc(), VideoClip.id.desc()).all()


def detach_clips(db, entity_ids) -> None:
    """Deleting entities: their clips go back to the libraries unattached (SQLite reuses an id, so a stale link would otherwise land on
    the next entity made)."""
    from .models import AudioClip, VideoClip
    ids = [i for i in entity_ids if isinstance(i, int)]
    if not ids:
        return
    for model in (AudioClip, VideoClip):
        db.query(model).filter(model.entity_id.in_(ids)).update({"entity_id": None}, synchronize_session=False)


# ── the page ───────────────────────────────────────────────────────────────────────────────────────────────────────────

# How a picture on the page says where it comes from
WHERE_LABEL = {"portrait": "Portrait", "body": "In the text", "note": "In a note"}


def page_pictures(entity, body, note_texts, *, is_gm: bool) -> list:
    """Every picture the viewer's page shows, each once, in this order: the pictures kept on the entity, the portrait, then the ones written
    into `body` and into `note_texts` - which must be the texts AS THIS VIEWER GETS THEM (already stripped of what they may not see), so the
    tab can never show a picture hidden inside a GM-only part. A kept picture the viewer may not see is left out too. A picture that is in
    two places is listed once, where it comes first (so one that is also kept keeps its controls)."""
    out, seen = [], set()

    def add(url, caption, where, item=None):
        if not url or url in seen:
            return
        seen.add(url)
        out.append({"uid": item["uid"] if item else "", "url": url, "caption": caption, "where": where,
                    "label": WHERE_LABEL.get(where, ""), "kept": item is not None,
                    "visible": True if item is None else bool(item["visible"])})

    for item in pictures(entity):
        if is_gm or item["visible"]:
            add(item["url"], item["caption"], "kept", item)
    add(getattr(entity, "image_url", None), "", "portrait")
    for url in md_images(body):
        add(url, "", "body")
    for text in note_texts:
        for url in md_images(text):
            add(url, "", "note")
    return out


def media_tab(db, entity, body, note_texts, *, is_gm: bool) -> dict:
    """What the entity page's Media tab is given: the pictures (page_pictures) and the audio and video clips the viewer may see."""
    return {"pictures": page_pictures(entity, body, note_texts, is_gm=is_gm),
            "audio": attached_audio(db, entity, is_gm=is_gm), "video": attached_video(db, entity, is_gm=is_gm)}
