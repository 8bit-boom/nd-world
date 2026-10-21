"""Remake existing thumbnails in the format Settings -> "Thumbnails and opened pictures" asks for (app/imaging.py preview_format).

Thumbnails made before AVIF ones (`<name>_thumb.webp`) - or AVIF ones after a switch to WebP - are found under the given folders, made again
from their picture in the chosen format, and the old file is deleted (make_thumbnail removes the other format). It runs in one background
thread at startup and when the setting is saved, a few pictures at a time (app/workers.py: half the cores) with a short pause after each, so the site stays responsive; a
run that is already going is not started twice, and a picture whose source is gone keeps its thumbnail (nothing else would show).

    python -m app.thumb_migrate [folder ...]        # the same, in the foreground (default: the uploads folder, static/maps and static/races)
"""
import logging
import threading
import time
from pathlib import Path

from . import imaging

_log = logging.getLogger("nd.thumb_migrate")
_lock = threading.Lock()
_running = False
_SOURCE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif")


def _source_for(thumb: Path):
    stem = thumb.name[: -len(thumb.name.split("_thumb.", 1)[1]) - len("_thumb.")]
    for ext in _SOURCE_EXTS + tuple(e.upper() for e in _SOURCE_EXTS):
        cand = thumb.with_name(stem + ext)
        if cand.is_file():
            return cand
    return None


def stale_thumbnails(*roots: Path):
    """Thumbnails not in the chosen format (with their picture), lazily."""
    want = imaging._copy_spec()[1]                                     # "_thumb.avif" or "_thumb.webp"
    for root in roots:
        if not root.is_dir():
            continue
        for suffix in imaging.THUMBNAIL_SUFFIXES:
            if suffix == want:
                continue
            for thumb in root.rglob("*" + suffix):
                src = _source_for(thumb)
                if src is not None:
                    yield thumb, src


def regenerate(*roots: Path, pause: float = 0.0, workers: int = 1) -> dict:
    """Remake every stale thumbnail under `roots` with `workers` threads at once; {"done": n, "failed": n}."""
    stats = {"done": 0, "failed": 0}
    count = threading.Lock()

    def one(item):
        thumb, src = item
        made = imaging.make_thumbnail(src)
        with count:
            stats["done" if made is not None and made != thumb else "failed"] += 1
        if pause:
            time.sleep(pause)

    if workers <= 1:
        for item in stale_thumbnails(*roots):
            one(item)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nd-thumb-remake") as pool:
            list(pool.map(one, stale_thumbnails(*roots)))
    return stats


def start(*roots: Path, pause: float = 0.05) -> bool:
    """Run regenerate in a background thread unless one is already running; True if this call started it."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True

    def run():
        global _running
        try:
            from .workers import image_workers
            stats = regenerate(*roots, pause=pause, workers=image_workers())
            if stats["done"] or stats["failed"]:
                _log.info("thumbnails remade as %s: %s", imaging.preview_format(), stats)
        except Exception:
            _log.warning("remaking thumbnails failed", exc_info=True)
        finally:
            with _lock:
                _running = False
    threading.Thread(target=run, name="nd-thumb-migrate", daemon=True).start()
    return True


if __name__ == "__main__":
    import sys
    base = Path(__file__).resolve().parent.parent
    import os
    uploads = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
    roots = [Path(a) for a in sys.argv[1:]] or [uploads, base / "static" / "maps", base / "static" / "races"]
    print("thumbnails remade:", regenerate(*roots))
