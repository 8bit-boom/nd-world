"""Shared upload helpers.

Lives in its own module rather than main.py so app/routers/* can import it —
routers can't import from main.py without a circular import.
"""

import os
import re
import shutil
import time
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


# ── Client-side-split large-file uploads ────────────────────────────────────
#
# A big file (a whole session recording, a long ambiance track, a large
# voice-memo attachment) can be blocked by a reverse proxy/CDN's own
# per-request body cap even once this app's own max_bytes is raised —
# Cloudflare's free tier is a fixed 100 MB with no way to raise it (see
# docs/DEPLOYMENT.md's "Upload size limit" section). The browser-side upload
# (static/js/chunked-upload.js's ndChunkedUpload) works around that by
# splitting a large file into sub-100MB parts and sending each as its own
# request; the two helpers below receive those parts and reassemble them
# server-side, so the result is identical to what a small direct upload
# would produce. Originally written once for the Audio Library
# (app/routers/audio.py); factored out here once a second and third caller
# (AI attachments, session audio recap) needed the exact same pattern.
CHUNK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_CHUNK_INDEX = 2000  # generous upper bound on parts per upload — catches a runaway/misbehaving client, not a real ceiling on file size
STALE_CHUNK_SESSION_SECONDS = 24 * 60 * 60  # sweep an abandoned session (browser closed mid-upload) next time a new one starts, rather than running a background job for this


def chunk_session_dir(chunks_root: Path, upload_id: str) -> Path:
    return chunks_root / upload_id


def sweep_stale_chunk_sessions(chunks_root: Path) -> None:
    if not chunks_root.is_dir():
        return
    cutoff = time.time() - STALE_CHUNK_SESSION_SECONDS
    for child in chunks_root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def save_upload_chunk(chunks_root: Path, upload_id: str, chunk_index: int, file: UploadFile, max_bytes: int) -> None:
    """Stash one part of a large file (see reassemble_upload_chunks) on disk
    under its upload_id — a single part can never legitimately exceed the
    whole file's own cap, so max_bytes here is normally the same value
    reassemble_upload_chunks will enforce on the total."""
    if not CHUNK_ID_RE.match(upload_id):
        raise HTTPException(400, "Invalid upload id")
    if not (0 <= chunk_index <= MAX_CHUNK_INDEX):
        raise HTTPException(400, "Invalid chunk index")
    session_dir = chunk_session_dir(chunks_root, upload_id)
    if chunk_index == 0 and not session_dir.exists():
        sweep_stale_chunk_sessions(chunks_root)
    session_dir.mkdir(parents=True, exist_ok=True)
    dest = session_dir / f"{chunk_index:06d}.part"
    copy_upload_bounded(file, dest, max_bytes=max_bytes)


def reassemble_upload_chunks(chunks_root: Path, upload_id: str, total_chunks: int, dest: Path, max_bytes: int) -> None:
    """Concatenate every part written by save_upload_chunk into `dest`, in
    order, raising 400 if any part is missing (client should retry the whole
    upload with a fresh id) or 413 if the reassembled total exceeds
    max_bytes (the partial `dest` is removed either way). Always cleans up
    the chunk session directory afterward, success or failure."""
    if not CHUNK_ID_RE.match(upload_id):
        raise HTTPException(400, "Invalid upload id")
    if not (1 <= total_chunks <= MAX_CHUNK_INDEX + 1):
        raise HTTPException(400, "Invalid total_chunks")
    session_dir = chunk_session_dir(chunks_root, upload_id)
    parts = [session_dir / f"{i:06d}.part" for i in range(total_chunks)]
    if not session_dir.is_dir() or not all(p.is_file() for p in parts):
        raise HTTPException(400, "Upload incomplete — one or more parts are missing. Please retry the upload.")
    try:
        total_bytes = 0
        with dest.open("wb") as out:
            for part in parts:
                with part.open("rb") as pf:
                    while True:
                        buf = pf.read(_CHUNK)
                        if not buf:
                            break
                        total_bytes += len(buf)
                        if total_bytes > max_bytes:
                            raise HTTPException(413, f"File too large — limit is {max_bytes // (1024 * 1024)} MB")
                        out.write(buf)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)
