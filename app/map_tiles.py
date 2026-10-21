"""Tiled map pictures (efficiency plan, step 3).

The map viewer used to load each map as ONE picture (Leaflet imageOverlay): a phone downloaded up to 9.4 MB and decoded up to 273 MB before
showing anything. Now each map picture is cut once, in the background, into a pyramid of 256 px WebP tiles laid out on the viewer's own grid
(Leaflet CRS.Simple, the same [[0, 0], [height, width]] bounds the overlay used, so pins, regions and every stored coordinate stay where they
are). The viewer then loads only the tiles in view at the current zoom; the deepest level keeps the picture's full resolution.

Layout: <data dir>/map_tiles/<slug>-<source mtime>/<z>/<x>_<y>.webp plus meta.json, written last (a pyramid without meta.json is unfinished and
is never served). A replaced picture has a new mtime, so a new pyramid is built and the old one removed. Until a pyramid exists the viewer shows
the single picture exactly as before."""
import json
import logging
import math
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from PIL import Image

_log = logging.getLogger("nd.map_tiles")
TILE = 256
MAX_NATIVE_ZOOM = 4                 # the viewer's maxZoom
Image.MAX_IMAGE_PIXELS = None        # our own bundled / GM-uploaded map pictures can be large (10k x 7k)

from . import workers as _workers
_pool = ThreadPoolExecutor(max_workers=_workers.tile_workers(), thread_name_prefix="nd-map-tiles")   # app/workers.py


@_workers.on_change
def _resize_pool():
    global _pool
    n = _workers.tile_workers()
    if n != _pool._max_workers:
        old, _pool = _pool, ThreadPoolExecutor(max_workers=n, thread_name_prefix="nd-map-tiles")
        old.shutdown(wait=False)
_building: set = set()
_lock = threading.Lock()


def root_dir() -> Path:
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "map_tiles"


# Pyramids of the bundled maps are cut when the Docker image is built (`python -m app.map_tiles`), so a server never spends the memory and
# minutes a 10k-pixel map takes at runtime; a GM's own uploads are cut on the server, in the background, the first time they are viewed.
PREBUILT_DIR = Path(__file__).resolve().parent.parent / "static" / "map_tiles"


def _find(key: str) -> Optional[Path]:
    for base in (PREBUILT_DIR, root_dir()):
        if (base / key / "meta.json").is_file():
            return base / key
    return None


def _key(slug: str, src: Path) -> str:
    """slug + a fingerprint of the picture (size and the first bytes), so a replaced picture gets a new pyramid while an unchanged bundled one
    keeps the key it was built with even though copying it into the image changed its modification time."""
    import zlib
    st = src.stat()
    with open(src, "rb") as f:
        head = f.read(65536)
    return f"{slug}-{st.st_size:x}{zlib.crc32(head):08x}"


def plan_levels(img_w: int, img_h: int, map_w: int, map_h: int) -> tuple:
    """(min zoom, native zoom): the native zoom is the level at which one map unit is one picture pixel or finer (0 when the picture is no
    bigger than the map's own size, up to MAX_NATIVE_ZOOM), the min zoom the level at which the whole map fits in one tile."""
    ratio = max(img_w / max(1, map_w), img_h / max(1, map_h))
    native = max(0, min(MAX_NATIVE_ZOOM, math.ceil(math.log2(ratio)) if ratio > 1 else 0))
    zmin = -max(0, math.ceil(math.log2(max(map_w, map_h) / TILE))) if max(map_w, map_h) > TILE else 0
    return zmin, native


def tile_range(map_w: int, map_h: int, z: int) -> tuple:
    """Leaflet's tile x / y ranges (inclusive) covering the map at zoom z in CRS.Simple: x from 0, y negative (latitude up = pixel y down)."""
    s = 2.0 ** z
    return (0, math.ceil(map_w * s / TILE) - 1), (math.floor(-map_h * s / TILE), -1)


def _cut(zdir: Path, xr, yr, oy: int, lw: int, lh: int, region) -> int:
    """Write one level's tiles; `region((x0, y0, x1, y1))` gives that part of the level (in level pixels) as an image."""
    n = 0
    for tx in range(xr[0], xr[1] + 1):
        for ty in range(yr[0], yr[1] + 1):
            box = (tx * TILE, ty * TILE + oy, tx * TILE + TILE, ty * TILE + oy + TILE)
            inner = (max(0, box[0]), max(0, box[1]), min(lw, box[2]), min(lh, box[3]))
            if inner[2] <= inner[0] or inner[3] <= inner[1]:
                continue
            tile = Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0))
            tile.paste(region(inner), (max(0, -box[0]), max(0, -box[1])))
            tile.save(zdir / f"{tx}_{ty}.webp", format="WEBP", quality=82, method=4)
            n += 1
    return n


def build(slug: str, src: Path, map_w: int, map_h: int, out_root: Optional[Path] = None) -> Optional[dict]:
    """Cut the pyramid now (blocking). Returns its meta dict, or None if the picture could not be read."""
    found = _find(_key(slug, src))
    if found is not None:
        return json.loads((found / "meta.json").read_text())
    out = (out_root or root_dir()) / _key(slug, src)
    tmp = out.with_name(out.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        im = Image.open(src)
        im.load()
        # used as decoded when it already is RGB / RGBA (no second full-size copy); RGB unless the picture really has transparency
        if im.mode in ("RGB", "RGBA"):
            img = im
        else:
            img = im.convert("RGBA") if (im.mode in ("LA", "PA") or "transparency" in im.info) else im.convert("RGB")
            im.close()
    except Exception:
        _log.warning("map %s: picture could not be read for tiling", slug, exc_info=True)
        return None
    zmin, native = plan_levels(img.width, img.height, map_w, map_h)
    n = 0
    prev = img
    for z in range(native, zmin - 1, -1):
        s = 2.0 ** z
        lw, lh = max(1, round(map_w * s)), max(1, round(map_h * s))
        (xr, yr) = tile_range(map_w, map_h, z)
        oy = lh                                    # picture row 0 sits at Leaflet pixel y = -lh
        zdir = tmp / str(z)
        zdir.mkdir(parents=True, exist_ok=True)
        if lw > prev.width or lh > prev.height:
            # The deepest level is usually LARGER than the picture (levels go in powers of two, rounded up so no detail is lost). It is
            # never made as one image - for a 72 MP map that was a 170 MP copy, ~0.5 GB more memory - each tile is resampled straight from
            # its own part of the picture instead (the same filter; Pillow reads the pixels around the box, so tiles meet without seams).
            fx, fy = prev.width / lw, prev.height / lh
            n += _cut(zdir, xr, yr, oy, lw, lh, lambda b: prev.resize((b[2] - b[0], b[3] - b[1]), Image.LANCZOS,
                                                                      box=(b[0] * fx, b[1] * fy, b[2] * fx, b[3] * fy)))
            continue
        # every other level is made from the one above it (half the size), and the bigger one is let go: memory stays near one picture
        level = prev.resize((lw, lh), Image.LANCZOS) if (lw, lh) != prev.size else prev
        if prev is not level:
            prev.close()
        prev = level
        n += _cut(zdir, xr, yr, oy, lw, lh, lambda b: level.crop(b))
    prev.close()
    meta = {"key": out.name, "w": map_w, "h": map_h, "zmin": zmin, "native": native, "tile": TILE, "tiles": n}
    (tmp / "meta.json").write_text(json.dumps(meta))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    for old in out.parent.glob(slug + "-*"):           # an older picture's pyramid
        if old.name != out.name and old.name.rsplit("-", 1)[0] == slug:
            shutil.rmtree(old, ignore_errors=True)
    _log.info("map %s: %d tiles, zoom %d..%d", slug, n, zmin, native)
    return meta


def meta_for(slug: str, src: Path, map_w: int, map_h: int) -> Optional[dict]:
    """The finished pyramid's meta for this picture, or None (and a build queued in the background) when there is none yet."""
    try:
        key = _key(slug, src)
    except OSError:
        return None
    found = _find(key)
    if found is not None:
        try:
            return json.loads((found / "meta.json").read_text())
        except (OSError, ValueError):
            return None
    out = root_dir() / key
    try:
        with Image.open(src) as probe:                     # the header only: is cutting this one within the memory budget?
            if not _workers.tiling_fits(probe.width * probe.height):
                _log.info("map %s: %dx%d is too big to cut within the memory budget; shown as one picture", slug, probe.width, probe.height)
                return None
    except Exception:
        return None
    with _lock:
        if out.name in _building:
            return None
        _building.add(out.name)

    def run():
        try:
            build(slug, src, map_w, map_h)
        except Exception:
            _log.warning("map %s: tiling failed", slug, exc_info=True)
        finally:
            with _lock:
                _building.discard(out.name)
    _pool.submit(run)
    return None


def tile_path(key: str, z: int, x: int, y: int) -> Optional[Path]:
    """The file for one tile of a FINISHED pyramid, or None."""
    if "/" in key or "\\" in key or key.startswith(".") or key.endswith(".part"):
        return None
    base = _find(key)
    if base is None:
        return None
    p = base / str(z) / f"{x}_{y}.webp"
    return p if p.is_file() else None


if __name__ == "__main__":
    # image build: cut the bundled maps into static/map_tiles (app/maps/*.json give each map's size)
    import sys
    app_dir = Path(__file__).resolve().parent
    static_maps = app_dir.parent / "static" / "maps"
    done = 0
    for jf in sorted((app_dir / "maps").glob("*.json")):
        d = json.loads(jf.read_text())
        pics = [p for p in static_maps.glob(jf.stem + ".*") if not p.stem.endswith("_thumb") and p.suffix.lower() in (".webp", ".jpg", ".jpeg", ".png", ".avif")]
        if pics and build(jf.stem, pics[0], int(d.get("width") or 0) or 1, int(d.get("height") or 0) or 1, out_root=PREBUILT_DIR):
            done += 1
    print("map pyramids:", done)
