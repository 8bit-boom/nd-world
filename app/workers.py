"""How many background picture jobs run at once - sized to the machine (docs/HARDWARE.md).

Picture work (thumbnails, opened-picture copies, the thumbnail remake) is mostly Pillow resizing and encoding, which releases Python's
interpreter lock, so threads really do run in parallel. Page serving is unaffected: these are background threads, and half the cores stay
free. Map tiling holds one whole picture in memory (~0.7 GB for a 72 MP map), so a second tiling worker only comes with enough RAM.

    ND_IMAGE_WORKERS   picture jobs at once (default: half the cores, at least 1, at most 6)
    ND_TILE_WORKERS    map pyramids cut at once (default: 2 with 16 GB of RAM or more, else 1)
"""
import os


def _env_int(name: str):
    try:
        v = int(os.environ.get(name, "") or 0)
    except ValueError:
        return None
    return v if v > 0 else None


def _ram_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except (AttributeError, ValueError, OSError):
        return 0.0


def image_workers(cpus=None) -> int:
    n = _env_int("ND_IMAGE_WORKERS")
    if n:
        return min(n, 32)
    cpus = cpus or os.cpu_count() or 1
    return max(1, min(6, cpus // 2))


def tile_workers(ram_gb=None) -> int:
    n = _env_int("ND_TILE_WORKERS")
    if n:
        return min(n, 8)
    return 2 if (ram_gb if ram_gb is not None else _ram_gb()) >= 16 else 1
