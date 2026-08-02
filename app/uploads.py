"""Shared upload helpers.

Lives in its own module rather than main.py so app/routers/* can import it —
routers can't import from main.py without a circular import.
"""

import os
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
