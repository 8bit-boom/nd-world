"""The AI media renamer: look at what is known about each audio clip, video clip and image in the library - its name, file
name, the tags and lyrics inside the file, a transcript, where an image is used, optionally the picture itself - and
propose a better name. Proposals are only proposals: nothing is renamed until the GM reviews them and applies, and every
applied batch can be undone.

  gather_signals  - everything known about one item (reads the file, bounded; never writes)
  suggest         - signals -> prompt -> model -> cleaned proposals (text items in batches, pictures one per call)
  apply_renames   - the reviewed renames: clips get a new `name`, images a MediaTitle label (the file itself is never
                    renamed - that would break every portrait and markdown embed pointing at it)
  undo_batch      - restores a batch, except what was renamed again since

Clips are addressed by id, images by their /uploads/... URL. The routes live in app/routers/media_rename.py.
"""
import base64
import io
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from . import media_meta
from .media_albums import UPLOADS_DIR
from .models import AudioAlbum, AudioClip, Entity, ImageAlbum, MediaRenameLog, MediaTitle, User, VideoAlbum, VideoClip

KINDS = ("audio", "video", "image")
MAX_NAME = 80                  # what the model's proposal is cut to; clips may be given up to 256 by hand
MAX_STORED_NAME = 256
TEXT_BATCH = 8                 # items per model call when no picture is involved
MAX_APPLY = 500
PICTURE_MAX_SIDE = 768
LISTEN_MAX_BYTES = 24 * 1024 * 1024
LISTEN_MAX_SECONDS = 600
_LYRICS_IN_PROMPT = 700
_TRANSCRIPT_IN_PROMPT = 700

STYLE_PRESETS = {
    "descriptive": "Plain, descriptive names: say what the thing is or shows, in Title Case.",
    "short": "Keep names very short: two to four words.",
    "lore": "Names that read as part of the setting - use the world's own places, people and tone when the clues support it.",
    "music": "For music use 'Artist - Title' when both are known, otherwise just the title.",
    "sfx": "For ambience and sound effects name what the sound IS and where it fits, e.g. 'Tavern Crowd Murmur', 'Door Creak'.",
}

_EXT = r"(?:mp3|wav|ogg|oga|opus|flac|m4a|aac|wma|aiff?|mp4|m4v|mov|mkv|webm|avi|wmv|png|jpe?g|gif|webp|avif|bmp|tiff?)"
_EXT_RE = re.compile(rf"\.{_EXT}$", re.I)
_PREFIX_RE = re.compile(r"^[0-9a-f]{12}-")
_GENERIC_WORDS = (
    "img", "image", "images", "dsc", "dscn", "dcim", "pxl", "pic", "picture", "photo", "screenshot", "screen shot",
    "screen capture", "capture", "snapshot", "untitled", "new", "copy of", "video", "vid", "movie", "mov", "clip",
    "audio", "audio clip", "sound", "recording", "record", "voice", "voice memo", "memo", "track", "file", "download",
    "downloads", "whatsapp audio", "whatsapp video", "whatsapp image", "unknown", "temp", "tmp", "output", "result",
    "generated", "render", "new recording", "new video", "new audio",
)
# a generic word, then only numbers / ids / dates / "(copy)" - "Track 07", "IMG_2041", "Screenshot 2024-05-01 at 10.22.33"
_GENERIC_RE = re.compile(
    r"^(?:(?:" + "|".join(re.escape(w) for w in sorted(_GENERIC_WORDS, key=len, reverse=True)) + r")[\s_\-.]*)+"
    r"(?:(?:\(\d+\)|\(copy\)|copy|\d[\d\-_.:\s]*|at\s[\d.:\sapm]+|[0-9a-f]{6,})[\s_\-.]*)*$",
    re.I,
)
_HEXISH = re.compile(r"^[0-9a-f]{8,}$", re.I)
_NUMERIC = re.compile(r"^[\d\s\-_.:()]+$")


# ── names ─────────────────────────────────────────────────────────────────────────────────────────

def readable_filename(url_or_name: str) -> str:
    """The upload's file name without the random prefix and the extension, as words: ".../a1b2c3d4e5f6-ember-waltz.mp3"
    -> "ember waltz". "" when there is nothing."""
    name = str(url_or_name or "").rsplit("/", 1)[-1]
    name = _PREFIX_RE.sub("", name)
    name = _EXT_RE.sub("", name)
    name = re.sub(r"[_\-]+", " ", name) if re.search(r"[_\-]", name) and " " not in name else name.replace("_", " ")
    return re.sub(r"\s+", " ", name).strip()


def looks_generic(name: str, filename: str = "") -> bool:
    """True when a name says nothing a person would search for: empty, a camera / recorder default ("IMG_2041",
    "Recording 12"), an id, a date stamp, or a file name (an extension, underscores). A plain-words name that happens
    to match the file ("Door Creak" from door-creak.mp3) is fine - `filename` is accepted for callers' convenience."""
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    if not n:
        return True
    if _EXT_RE.search(n) or _HEXISH.match(n) or _NUMERIC.match(n) or _GENERIC_RE.match(n):
        return True
    return " " not in n and "_" in n


_LEAD_LABEL = re.compile(r"^(?:suggested\s+)?(?:name|title)\s*:\s*", re.I)
_LEAD_NUMBER = re.compile(r"^\d+[.)]\s+")


def clean_suggestion(text) -> str:
    """A model's proposal as a safe, tidy name: one line, no quotes / extension / leading "Title:" or list number /
    path characters / control characters, at most MAX_NAME characters (cut at a word). "" when nothing usable is left."""
    s = str(text if text is not None else "")
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    s = s.strip().splitlines()[0] if s.strip() else ""
    s = s.strip().strip("`\"'“”‘’«»").strip()
    s = _LEAD_LABEL.sub("", s)
    s = _LEAD_NUMBER.sub("", s)
    s = _EXT_RE.sub("", s.strip())
    s = s.replace("_", " ")
    s = re.sub(r"[/\\<>:|?*\"]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().strip("`'“”‘’")
    s = s.strip(" .,;:-–—_")
    if not re.search(r"\w", s):
        return ""
    if len(s) > MAX_NAME:
        cut = s[:MAX_NAME]
        s = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return s.strip(" .,;:-–—_")


def style_text(preset: str = "", free: str = "") -> str:
    parts = []
    if preset in STYLE_PRESETS:
        parts.append(STYLE_PRESETS[preset])
    free = re.sub(r"\s+", " ", str(free or "")).strip()
    if free:
        parts.append(free)
    return " ".join(parts)[:400]


def _loads_loose(raw):
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener in ("{", "["):
        start = text.find(opener)
        if start >= 0:
            try:
                return json.JSONDecoder().raw_decode(text[start:])[0]
            except ValueError:
                continue
    return None


def parse_names(raw, expected_ids: list, current: Optional[dict] = None) -> dict:
    """{id: cleaned name} from the model's reply - {"names":[{"id","name"}]}, a bare list, or an {id: name} mapping.
    Unknown ids, empty names and anything that is not JSON are dropped. `current` ({id: the item's present name}): a
    reply that repeats the present name verbatim ("keep it") is returned as is, not tidied into a different string."""
    data = _loads_loose(raw)
    if isinstance(data, dict) and "names" in data:
        data = data["names"]
    pairs = []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and "id" in entry:
                pairs.append((entry.get("id"), entry.get("name")))
    elif isinstance(data, dict):
        pairs = list(data.items())
    wanted = {str(i) for i in expected_ids}
    out = {}
    for key, value in pairs:
        key = str(key)
        if key in wanted and isinstance(value, (str, int, float)):
            if current and isinstance(value, str) and current.get(key) and value.strip() == current[key]:
                out[key] = current[key]
                continue
            cleaned = clean_suggestion(value)
            if cleaned:
                out[key] = cleaned
    return out


# ── the prompt ──────────────────────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You rename media files in a tabletop-RPG game master's library so they can be found again. For each item you get "
    "what is known about it: its current name, its file name, tags read from inside the file, lyrics or a transcript "
    "excerpt, where it is used, and sometimes a picture.\n"
    "Propose ONE better name per item: specific, short (two to six words, at most 60 characters), no file extension, "
    "no quotes, no emoji, no leading list number. Name music by its title (from the tags or the lyrics); name ambience "
    "and sound effects by what they ARE; name videos by what happens in them; name pictures by what they show - a person, "
    "place, creature, scene or handout - and if the picture contains readable text (a sign, a map title, a document "
    "heading) use it. If the current name is already good, return it unchanged. Never invent facts that no clue "
    "supports: with nothing to go on, return the current name. Write in the language of the clues.\n"
    'Reply ONLY with JSON: {"names":[{"id":"<the number in brackets>","name":"<new name>"}]}'
)

NAMES_SCHEMA = {
    "type": "object",
    "properties": {"names": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}, "required": ["id", "name"],
    }}},
    "required": ["names"],
}


def _clock(seconds) -> str:
    s = int(round(float(seconds)))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def describe_item(sig: dict, label) -> str:
    """One item as the block of clues the model reads."""
    meta = sig.get("meta") or {}
    lines = [f"[{label}] {sig['kind']}", f"current name: {sig.get('name') or '(none)'}"]
    if sig.get("filename"):
        lines.append(f"file name: {sig['filename']}")
    if sig.get("album"):
        lines.append(f"folder: {sig['album']}")
    if sig.get("albums"):
        lines.append("in albums: " + ", ".join(sig["albums"][:5]))
    if sig.get("attached"):
        lines.append(f"attached to: {sig['attached']}")
    if sig.get("uses"):
        lines.append("used as / in: " + "; ".join(sig["uses"][:5]))
    if sig.get("description"):
        lines.append(f"description: {sig['description'][:300]}")
    tags = [f'{k}="{meta[k]}"' for k in ("title", "artist", "album", "genre", "year", "description", "comment", "keywords", "software")
            if meta.get(k)]
    if meta.get("duration_s"):
        tags.append(f"length={_clock(meta['duration_s'])}")
    if meta.get("width") and meta.get("height"):
        tags.append(f"size={meta['width']}x{meta['height']}")
    if tags:
        lines.append("tags: " + "; ".join(t if len(t) < 320 else t[:320] + '…"' for t in tags))
    if meta.get("prompt"):
        lines.append(f"made from the prompt: {meta['prompt'][:500]}")
    if meta.get("lyrics"):
        lines.append(f"lyrics: {meta['lyrics'][:_LYRICS_IN_PROMPT]}")
    if sig.get("transcript"):
        lines.append(f"transcript: {sig['transcript'][:_TRANSCRIPT_IN_PROMPT]}")
    if sig.get("picture_b64"):
        lines.append("(a picture of it is attached)")
    return "\n".join(lines)


def build_prompt(sigs: list, style: str = "", world_name: str = "", world_about: str = ""):
    """(system, user) for one model call over `sigs`, numbered 1..n in the order given."""
    head = []
    if world_name:
        head.append(f"Setting: {world_name}" + (f" - {world_about[:300]}" if world_about else ""))
    if style:
        head.append(f"Naming style: {style}")
    blocks = [describe_item(sig, i) for i, sig in enumerate(sigs, 1)]
    return _SYSTEM, "\n".join(head + ([""] if head else []) + ["\n\n".join(blocks)])


def _basis(sig: dict) -> list:
    meta = sig.get("meta") or {}
    out = []
    if any(meta.get(k) for k in ("title", "artist", "album", "genre", "description", "comment", "keywords")):
        out.append("tags")
    if meta.get("lyrics"):
        out.append("lyrics")
    if sig.get("transcript"):
        out.append("listened" if sig.get("listened") else "transcript")
    if meta.get("prompt"):
        out.append("prompt")
    if sig.get("picture_b64"):
        out.append("picture")
    if sig.get("uses") or sig.get("attached"):
        out.append("usage")
    if sig.get("filename") and not looks_generic(sig["filename"]):
        out.append("filename")
    return out


# ── signals ──────────────────────────────────────────────────────────────────────────────────────────

def _upload_path(url: str) -> Optional[Path]:
    """The file behind an /uploads/... URL, or None for anything outside the uploads folder."""
    if not isinstance(url, str) or not url.startswith("/uploads/") or len(url) <= len("/uploads/"):
        return None
    try:
        root = UPLOADS_DIR.resolve()
        path = (root / url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return path if path.is_relative_to(root) and path.is_file() else None


def picture_b64(path: Optional[Path]) -> Optional[str]:
    """A file as a small JPEG (longest side PICTURE_MAX_SIDE) in base64 - what a vision model is sent - or None."""
    if not path:
        return None
    try:
        from PIL import Image
        with Image.open(path) as img:
            img.seek(0)
            img = img.convert("RGB")
            img.thumbnail((PICTURE_MAX_SIDE, PICTURE_MAX_SIDE))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _thumb(url) -> Optional[str]:
    """The small preview of an upload (the thumbnail written beside it when there is one), for the review table."""
    from .templating import thumb_url
    return thumb_url(url) if isinstance(url, str) and url else None


def image_usage(db: Session, world) -> dict:
    """{url: {"uses": [where it is used], "albums": [its albums], "name": its display name}} for every image of the world
    (used somewhere, or in an album), computed in one pass - a request that touches many images calls this once."""
    from .gallery import discover_world_images, image_display_name, title_overrides
    usage: dict = {}
    names: dict = {}
    for entry in discover_world_images(db, world):
        usage.setdefault(entry["url"], {"uses": [], "albums": []})["uses"] = [u["label"] for u in entry["uses"]]
        names[entry["url"]] = entry["name"]
    for album in db.query(ImageAlbum).filter(ImageAlbum.world_id == world.id).all():
        try:
            urls = json.loads(album.image_urls_json or "[]")
        except (TypeError, ValueError):
            urls = []
        for u in urls if isinstance(urls, list) else []:
            if isinstance(u, str):
                usage.setdefault(u, {"uses": [], "albums": []})["albums"].append(album.name)
    overrides = title_overrides(db, world.id)
    for url, info in usage.items():
        info["name"] = overrides.get(url) or names.get(url) or image_display_name(url)
    return usage


async def _listen(path: Path, meta: dict) -> str:
    """A short transcript of an untranscribed clip - only for modest files, never stored. "" on any trouble."""
    try:
        if path.stat().st_size > LISTEN_MAX_BYTES or (meta.get("duration_s") or 0) > LISTEN_MAX_SECONDS:
            return ""
        from . import ai as _ai
        text, _ = await _ai.transcribe_audio_with_subtitles(path)
        return re.sub(r"\s+", " ", text or "").strip()[:1200]
    except Exception:
        return ""


async def gather_signals(db: Session, world, kind: str, ref: dict, *, usage: Optional[dict] = None,
                         pictures: bool = False, listen: bool = False) -> Optional[dict]:
    """Everything known about one item, or None when it is not in this world. Reads the file; writes nothing."""
    if kind in ("audio", "video"):
        model, album_model = (AudioClip, AudioAlbum) if kind == "audio" else (VideoClip, VideoAlbum)
        try:
            row = db.query(model).filter(model.id == int(ref.get("id")), model.world_id == world.id).first()
        except (TypeError, ValueError):
            return None
        if not row:
            return None
        album = db.get(album_model, row.album_id) if row.album_id else None
        entity = db.get(Entity, row.entity_id) if row.entity_id else None
        path = _upload_path(row.file_url)
        meta = media_meta.read_metadata(path, kind) if path else {}
        transcript = re.sub(r"\s+", " ", row.transcript or "").strip()
        listened = False
        if not transcript and listen and path and kind == "audio":
            transcript = await _listen(path, meta)
            listened = bool(transcript)
        poster = _upload_path(row.poster_url) if kind == "video" and getattr(row, "poster_url", None) else None
        return {
            "kind": kind, "id": row.id, "url": None, "name": row.name or "", "filename": readable_filename(row.file_url),
            "album": album.name if album else "", "attached": entity.name if entity else "",
            "description": row.description or "", "meta": meta, "transcript": transcript, "listened": listened,
            "uses": [], "albums": [], "picture_b64": picture_b64(poster) if pictures else None,
            "thumb": _thumb(row.poster_url) if kind == "video" and getattr(row, "poster_url", None) else None,
        }
    if kind == "image":
        url = ref.get("url")
        path = _upload_path(url)
        if not path:
            return None
        if usage is None:
            usage = image_usage(db, world)
        info = usage.get(url)
        if info is None:                                  # not one of this world's images: never read it
            return None
        return {
            "kind": "image", "id": None, "url": url, "name": info["name"],
            "filename": readable_filename(url), "album": "", "attached": "", "description": "",
            "meta": media_meta.read_metadata(path, "image"), "transcript": "", "listened": False,
            "uses": info["uses"], "albums": info["albums"], "picture_b64": picture_b64(path) if pictures else None,
            "thumb": _thumb(url),
        }
    return None


# ── suggesting ─────────────────────────────────────────────────────────────────────────────────────────

def _ref_of(item: dict) -> dict:
    return {"id": item.get("id"), "url": item.get("url")}


def _result(sig: dict, new: str, note: str = "") -> dict:
    changed = bool(new) and new != sig["name"]
    return {
        "kind": sig["kind"], "id": sig["id"], "url": sig["url"], "old": sig["name"], "new": new, "changed": changed,
        "basis": _basis(sig), "generic": looks_generic(sig["name"], sig.get("filename", "")), "note": note,
        "thumb": sig.get("thumb"),
    }


async def suggest(db: Session, world, items: list, *, style: str = "", preset: str = "", pictures: bool = False,
                  listen: bool = False) -> dict:
    """Proposals for `items` ([{"kind", "id" | "url"}]): {"results": [...], "errors": [...]}. Nothing is written."""
    from . import ai as _ai
    errors, signals = [], []
    needs_usage = any(isinstance(i, dict) and i.get("kind") == "image" for i in items)
    usage = image_usage(db, world) if needs_usage else None
    for item in items:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            errors.append(f"Not a media item: {str(item)[:80]}")
            continue
        sig = await gather_signals(db, world, item["kind"], _ref_of(item), usage=usage, pictures=pictures, listen=listen)
        if sig is None:
            errors.append(f"Could not find {item['kind']} {item.get('id') or item.get('url')} in this world.")
        else:
            signals.append(sig)

    guidance = style_text(preset, style)
    about = (getattr(world, "description", "") or "").strip()
    results = []

    async def ask(group: list):
        system, user = build_prompt(group, guidance, world.name, about)
        message = {"role": "user", "content": user}
        pics = [s["picture_b64"] for s in group if s.get("picture_b64")]
        if pics:
            message["images"] = pics
        try:
            raw = await _ai.generate_chat([message], system=system, options={"num_predict": 900}, think=False, format=NAMES_SCHEMA)
        except Exception as exc:
            errors.append(f"The model call failed: {exc}")
            return {}
        if _ai.is_failure_sentinel(raw or ""):
            errors.append(str(raw)[:300])
            return {}
        return parse_names(raw, [str(i) for i in range(1, len(group) + 1)], {str(n): sig["name"] for n, sig in enumerate(group, 1)})

    with_picture = [s for s in signals if s.get("picture_b64")]
    plain = [s for s in signals if not s.get("picture_b64")]
    groups = [[s] for s in with_picture] + [plain[i:i + TEXT_BATCH] for i in range(0, len(plain), TEXT_BATCH)]
    answers = {}
    for group in groups:
        named = await ask(group)
        for n, sig in enumerate(group, 1):
            answers[id(sig)] = named.get(str(n), "")
    for sig in signals:
        new = answers.get(id(sig), "")
        results.append(_result(sig, new, "" if new else "The model gave no name for this one."))
    return {"results": results, "errors": errors}


# ── applying and undoing ─────────────────────────────────────────────────────────────────────────────────────

def _stored_name(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or ""))).strip()[:MAX_STORED_NAME]


def apply_renames(db: Session, world, user_id: Optional[int], renames: list) -> dict:
    """Rename what the GM approved. Clips get a new `name`; an image gets a MediaTitle (an empty name clears it). Each
    change is logged under one batch id for undo. Returns {"batch_id", "applied", "skipped": [{"ref", "reason"}]};
    raises ValueError for an oversized batch."""
    if not isinstance(renames, list):
        raise ValueError("renames must be a list")
    if len(renames) > MAX_APPLY:
        raise ValueError(f"At most {MAX_APPLY} renames at a time.")
    from .gallery import all_world_image_urls
    batch = uuid.uuid4().hex[:16]
    applied, skipped = 0, []
    known_images = None                                   # {url: the name it shows now}, built once if an image comes up

    def skip(ref, reason):
        skipped.append({"ref": str(ref)[:120], "reason": reason})

    for entry in renames:
        if not isinstance(entry, dict):
            skip(entry, "Not a rename.")
            continue
        kind, name = entry.get("kind"), _stored_name(entry.get("name"))
        if kind in ("audio", "video"):
            model = AudioClip if kind == "audio" else VideoClip
            try:
                row = db.query(model).filter(model.id == int(entry.get("id")), model.world_id == world.id).first()
            except (TypeError, ValueError):
                row = None
            if not row:
                skip(f"{kind} {entry.get('id')}", "Not found in this world.")
            elif not name:
                skip(f"{kind} {row.id}", "A name cannot be empty.")
            elif name == row.name:
                skip(f"{kind} {row.id}", "Already has that name.")
            else:
                db.add(MediaRenameLog(world_id=world.id, batch_id=batch, kind=kind, ref_id=row.id, old_name=row.name or "",
                                      new_name=name, user_id=user_id))
                row.name = name
                applied += 1
        elif kind == "image":
            url = entry.get("url")
            if known_images is None:
                known_images = {e["url"]: e["name"] for e in all_world_image_urls(db, world)}
            if not isinstance(url, str) or url not in known_images:
                skip(url, "Not an image of this world.")
                continue
            existing = db.query(MediaTitle).filter(MediaTitle.world_id == world.id, MediaTitle.url == url).first()
            if not name:
                if not existing:
                    skip(url, "It has no chosen title to clear.")
                    continue
                db.add(MediaRenameLog(world_id=world.id, batch_id=batch, kind="image", ref_url=url, old_name=existing.title,
                                      new_name="", user_id=user_id))
                db.delete(existing)
                applied += 1
                continue
            if name == known_images[url]:
                skip(url, "Already has that name.")
                continue
            db.add(MediaRenameLog(world_id=world.id, batch_id=batch, kind="image", ref_url=url,
                                  old_name=existing.title if existing else "", new_name=name, user_id=user_id))
            known_images[url] = name
            if existing:
                existing.title, existing.updated_at = name, datetime.utcnow()
            else:
                db.add(MediaTitle(world_id=world.id, url=url, title=name))
            applied += 1
        else:
            skip(entry.get("id") or entry.get("url"), "Unknown kind.")
    db.commit()
    return {"batch_id": batch, "applied": applied, "skipped": skipped}


def undo_batch(db: Session, world, batch_id: str) -> dict:
    """Put a batch back: each item returns to its old name unless it was renamed again since (then it is left and
    listed). A batch is only undone once."""
    rows = db.query(MediaRenameLog).filter(
        MediaRenameLog.world_id == world.id, MediaRenameLog.batch_id == str(batch_id), MediaRenameLog.undone_at.is_(None),
    ).all()
    restored, skipped = 0, []
    for log in rows:
        if log.kind in ("audio", "video"):
            model = AudioClip if log.kind == "audio" else VideoClip
            row = db.query(model).filter(model.id == log.ref_id, model.world_id == world.id).first()
            if row and row.name == log.new_name:
                row.name = log.old_name
                restored += 1
            else:
                skipped.append({"ref": f"{log.kind} {log.ref_id}", "reason": "It was renamed again since." if row else "It no longer exists."})
        else:
            title = db.query(MediaTitle).filter(MediaTitle.world_id == world.id, MediaTitle.url == log.ref_url).first()
            current = title.title if title else ""
            if current == log.new_name:
                if log.old_name:
                    if title:
                        title.title, title.updated_at = log.old_name, datetime.utcnow()
                    else:
                        db.add(MediaTitle(world_id=world.id, url=log.ref_url, title=log.old_name))
                elif title:
                    db.delete(title)
                restored += 1
            else:
                skipped.append({"ref": str(log.ref_url), "reason": "It was renamed again since."})
        log.undone_at = datetime.utcnow()
    db.commit()
    return {"restored": restored, "skipped": skipped}


def recent_batches(db: Session, world, limit: int = 10) -> list:
    """The latest applied batches, newest first: id, when, who, how many renames, whether it was undone, a few examples."""
    logs = db.query(MediaRenameLog).filter(MediaRenameLog.world_id == world.id).order_by(MediaRenameLog.id.desc()).limit(2000).all()
    batches: dict = {}
    for log in logs:
        b = batches.setdefault(log.batch_id, {"batch_id": log.batch_id, "at": log.created_at, "user_id": log.user_id,
                                              "count": 0, "undone": True, "examples": []})
        b["count"] += 1
        b["undone"] = b["undone"] and log.undone_at is not None
        if len(b["examples"]) < 3:
            b["examples"].append({"old": log.old_name, "new": log.new_name})
    ordered = list(batches.values())[:limit]
    users = {u.id: u for u in db.query(User).filter(User.id.in_([b["user_id"] for b in ordered if b["user_id"]] or [0])).all()}
    for b in ordered:
        u = users.get(b["user_id"])
        b["by"] = (u.display_name or u.email.split("@")[0]) if u else "someone"
        b["at"] = b["at"].strftime("%Y-%m-%dT%H:%M:%SZ") if b["at"] else None
        del b["user_id"]
    return ordered
