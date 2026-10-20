"""Thumbnails for pictures that never went through the upload pipeline (efficiency plan, step 1).

The bundled map and race pictures under static/maps and static/races, and map images (uploads/maps, saved under a stable name and overwritten in
place), had no `_thumb.webp`, so every card, the home page's Maps strip and the /maps grid downloaded the originals - up to 9.4 MB each, 25 MB for
the home page. `ensure(...)` writes the missing or out-of-date thumbnails (the image build runs `python -m app.static_thumbs`, the server does it
again in the background at startup), and `request(path)` queues one when the template filter meets a picture without a fresh thumbnail. The
originals are never touched: the map viewer and the lightbox keep using them."""
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .imaging import make_thumbnail, thumbnail_path_for

_log = logging.getLogger("nd.static_thumbs")
_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nd-thumbs")
_queued: set = set()
_lock = threading.Lock()


def is_fresh(src: Path) -> bool:
    t = thumbnail_path_for(src)
    try:
        return t.stat().st_mtime_ns >= src.stat().st_mtime_ns
    except OSError:
        return False


def _make(src: Path) -> None:
    try:
        if src.is_file() and not is_fresh(src):
            make_thumbnail(src)
    except Exception:                       # best effort: without a thumbnail the original is shown, as before
        _log.debug("thumbnail failed for %s", src, exc_info=True)
    finally:
        with _lock:
            _queued.discard(str(src))


def request(src: Path) -> None:
    """Queue a thumbnail for `src` (one worker thread, each file once at a time)."""
    with _lock:
        if str(src) in _queued:
            return
        _queued.add(str(src))
    _pool.submit(_make, src)


def ensure(*roots: Path) -> int:
    """Write every missing / stale thumbnail under `roots` now. Returns how many were written."""
    n = 0
    for root in roots:
        if not root.is_dir():
            continue
        for src in sorted(root.rglob("*")):
            if src.suffix.lower() in _EXTS and src.is_file() and not src.stem.endswith("_thumb") and not is_fresh(src):
                if make_thumbnail(src) is not None:
                    n += 1
    return n


# Bundled map pictures in the image build: AVIF at quality 80 measured ~17% smaller than WebP at quality 90 for the same fidelity (PSNR 39.7
# against 39.6 over the six biggest maps, 20.2 MB against 24.3 MB) and encoded 3x faster. Tiles, thumbnails and display copies stay WebP:
# at 256 px AVIF was no smaller at equal quality, ~7x slower to encode and slower to decode (docs/EFFICIENCY_PLAN.md, section 5).
_MAP_FORMATS = {"avif": ("AVIF", ".avif", 80), "webp": ("WEBP", ".webp", 90)}
# Bigger pictures keep their file: the Dreamlands map (72 MP, already WebP) took 2.5 GB and 29 s to encode as AVIF and came out no smaller, and
# a picture that size is shown as tiles (app/map_tiles.py) anyway, so its original is only the viewer's fallback.
MAX_MAP_PIXELS = 40_000_000


def compact_maps(folder: Path, fmt: str = "avif", quality: Optional[int] = None) -> tuple:
    """Image build only (efficiency plan, step 12): re-save the bundled map pictures as AVIF (or WebP) when that is smaller, and drop the
    original - every lookup of a map picture tries each extension, and http_perf redirects old links. The repository keeps the originals.
    Falls back to WebP when this Pillow cannot write AVIF. Returns (files converted, bytes before, bytes after)."""
    from PIL import Image, features
    if fmt == "avif" and not features.check("avif"):
        fmt = "webp"
    pil_fmt, ext, default_q = _MAP_FORMATS[fmt]
    n = before = after = 0
    for src in sorted(folder.glob("*")):
        if src.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".avif") or src.suffix.lower() == ext or src.stem.endswith("_thumb"):
            continue
        dest = src.with_suffix(ext)
        if dest.exists():
            continue
        with Image.open(src) as im:
            if im.width * im.height > MAX_MAP_PIXELS:
                continue
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
            im.save(dest, format=pil_fmt, quality=quality or default_q, **({"method": 6} if pil_fmt == "WEBP" else {}))
        if dest.stat().st_size < src.stat().st_size:
            before += src.stat().st_size
            after += dest.stat().st_size
            src.unlink()
            n += 1
        else:
            dest.unlink()
    return n, before, after


if __name__ == "__main__":
    static = Path(__file__).resolve().parent.parent / "static"
    if "--avif-maps" in sys.argv or "--webp-maps" in sys.argv:
        fmt = "avif" if "--avif-maps" in sys.argv else "webp"
        print(f"bundled maps as {fmt} (files, bytes before, after):", compact_maps(static / "maps", fmt))
    roots = [Path(a) for a in sys.argv[1:] if not a.startswith("--")] or [static / "maps", static / "races"]
    print("static thumbnails written:", ensure(*roots))
