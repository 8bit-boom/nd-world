"""Shared upload helpers.

Lives in its own module rather than main.py so app/routers/* can import it —
routers can't import from main.py without a circular import.
"""

import os
import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

# 20 MB default. Portraits and entity art are small; this limit exists so an
# unbounded upload can't fill the /data volume, which would take the whole app
# down with it (SQLite writes fail once the disk is full).
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))

# Bulk portrait/art import (/api/import/images) caps how many files one
# request may carry — a batch this size is already a lot to review in one
# pass. Shared here (rather than living next to the route in main.py) so the
# /import page router can import it too and warn the user client-side
# *before* uploading a batch the server will just reject, instead of only
# finding out after every file has already gone over the wire.
BULK_IMAGE_MAX_FILES = 100

_CHUNK = 1024 * 1024

# Cap how much of the original filename survives into the stored one — long
# enough to stay recognizable, short enough that a pathologically long
# upload name can't produce an unwieldy path.
_STEM_MAX_LEN = 60
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Every unique_upload_filename() result starts with exactly this many hex
# chars followed by "-" — image_display_name() (app/gallery.py) strips this
# prefix back off to recover a clean display name for images that aren't
# attached anywhere yet.
UPLOAD_PREFIX_LEN = 12


def unique_upload_filename(original_filename: str, ext: str) -> str:
    """A storage filename that's still collision-safe (a random hex prefix,
    same guarantee a bare uuid4().hex gave before) but keeps a slugified
    version of the uploader's original filename appended, so the name
    survives display (image_display_name) and format conversion
    (app/imaging.py's convert_image_to only ever swaps the extension,
    leaving the rest of the stem untouched) instead of degrading into a
    meaningless hex string the moment an image is uploaded or re-encoded.
    Falls back to a bare hex filename if the original name has no
    alphanumeric characters to slugify (e.g. "😀.png", or no filename at
    all for a synthesized/restored image)."""
    stem = Path(original_filename or "").stem
    slug = _SLUG_RE.sub("-", stem.lower()).strip("-")[:_STEM_MAX_LEN]
    if not slug:
        return f"{uuid.uuid4().hex}{ext}"
    return f"{uuid.uuid4().hex[:UPLOAD_PREFIX_LEN]}-{slug}{ext}"


def copy_upload_bounded(file: UploadFile, dest: Path, max_bytes: int = None) -> None:
    """Stream an upload to `dest`, raising HTTP 413 if it exceeds the limit.

    Replaces a bare shutil.copyfileobj, which streams unbounded to disk. The
    partial file is removed on overflow (or any other error) so a rejected
    upload leaves nothing behind.
    """
    limit = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = file.file.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        413, f"File too large — limit is {limit // (1024 * 1024)} MB"
                    )
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def read_upload_bounded(file: UploadFile, max_bytes: int = None) -> bytes:
    """Same chunked-read-with-cap shape as copy_upload_bounded above, but for
    an upload that only needs to be held in memory and converted (e.g. an
    HTML note import) rather than saved to disk — avoids a giant multi-GB
    upload being fully buffered before the size check ever runs."""
    limit = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    chunks = []
    written = 0
    while True:
        chunk = file.file.read(_CHUNK)
        if not chunk:
            break
        written += len(chunk)
        if written > limit:
            raise HTTPException(413, f"File too large — limit is {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)
