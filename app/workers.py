"""How many background picture jobs run at once - sized to the machine (docs/HARDWARE.md).

Picture work (thumbnails, opened-picture copies, the thumbnail remake) is mostly Pillow resizing and encoding, which releases Python's
interpreter lock, so threads really do run in parallel. Page serving is unaffected: these are background threads, and half the cores stay
free. Map tiling holds one whole picture in memory (~0.7 GB for a 72 MP map), so a second tiling worker only comes with enough RAM.

    ND_IMAGE_WORKERS   picture jobs at once (default: half the usable cores, at least 1, at most 8)
    ND_TILE_WORKERS    map pyramids cut at once (default: 2 with a 16 GB memory budget or more, else 1)
    ND_RENDERS         page builds at once (app/http_perf.py; default: tuned under load, app/render_gate.py)

Settings -> System -> "Server resources" sets the same three (picture jobs, page builds, a memory budget) without editing .env; a value
in .env wins over the setting, the setting wins over the automatic choice. These size nd-world's OWN work - a hard cap on the container's
cores and RAM is set where the container runs (TrueNAS: the app's Resources limits).
"""
import os
import threading

# Settings -> System (AppSettings.res_*); None = automatic. Set at startup and when the settings are saved (apply()).
_overrides = {"image_workers": None, "render_slots": None, "memory_mb": None}
_listeners = []
_lock = threading.Lock()

# what cutting a map costs: ~670 MB for a 72 MP picture (app/map_tiles.py, measured) - about 10 bytes a pixel with room to spare
TILE_BYTES_PER_PIXEL = 10


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


def usable_cpus(cgroup_root: str = "/sys/fs/cgroup") -> int:
    """The cores this process may really use: the CPUs it is allowed to run on, and - in a container started with a CPU limit (Docker
    `cpus:`) - that limit, which os.cpu_count() does not see (it reports every core of the host)."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    quota = None
    try:                                                       # cgroup v2: "max 100000" or "200000 100000"
        q, period = open(os.path.join(cgroup_root, "cpu.max")).read().split()[:2]
        if q != "max":
            quota = int(q) / int(period)
    except (OSError, ValueError):
        try:                                                   # cgroup v1
            q = int(open(os.path.join(cgroup_root, "cpu", "cpu.cfs_quota_us")).read())
            period = int(open(os.path.join(cgroup_root, "cpu", "cpu.cfs_period_us")).read())
            if q > 0 and period > 0:
                quota = q / period
        except (OSError, ValueError):
            pass
    if quota:
        import math
        n = min(n, max(1, math.ceil(quota)))
    return max(1, n)


def auto_image_workers(cpus=None) -> int:
    cpus = cpus or usable_cpus()
    return max(1, min(8, cpus // 2))


def image_workers(cpus=None) -> int:
    n = _env_int("ND_IMAGE_WORKERS") or _overrides["image_workers"]
    if n:
        return min(int(n), 32)
    return auto_image_workers(cpus)


def usable_ram_gb(cgroup_root: str = "/sys/fs/cgroup") -> float:
    """The machine's RAM, or the container's memory limit when it has a lower one (cgroup v2 memory.max / v1 limit_in_bytes)."""
    ram = _ram_gb()
    for f in ("memory.max", os.path.join("memory", "memory.limit_in_bytes")):
        try:
            v = open(os.path.join(cgroup_root, f)).read().strip()
            if v != "max" and int(v) < 2 ** 60:
                ram = min(ram, int(v) / 2 ** 30) if ram else int(v) / 2 ** 30
            break
        except (OSError, ValueError):
            continue
    return ram


def memory_budget_gb(ram_gb=None) -> float:
    """The memory nd-world plans its heavy work for: the setting, else the usable RAM."""
    if _overrides["memory_mb"]:
        return _overrides["memory_mb"] / 1024
    return ram_gb if ram_gb is not None else usable_ram_gb()


def tile_workers(ram_gb=None) -> int:
    n = _env_int("ND_TILE_WORKERS")
    if n:
        return min(n, 8)
    return 2 if memory_budget_gb(ram_gb) >= 16 else 1


def tiling_fits(pixels: int) -> bool:
    """Whether cutting a map of `pixels` on the server stays within the memory budget (with every tiling worker busy). A map that does not
    fit is shown as one picture, as before tiles existed."""
    budget = memory_budget_gb() * 2 ** 30
    return not budget or pixels * TILE_BYTES_PER_PIXEL * tile_workers() <= budget * 0.5


def render_slots() -> int:
    """A fixed number of pages built at once (ND_RENDERS, else the setting), or 2 - where automatic tuning starts (render_slots_fixed)."""
    n = _env_int("ND_RENDERS") or _overrides["render_slots"]
    return max(1, min(int(n), 16)) if n else 2


def render_slots_fixed() -> bool:
    return bool(_env_int("ND_RENDERS") or _overrides["render_slots"])


def render_slots_max(cpus=None) -> int:
    """The most the automatic tuning tries: half the usable cores, at least 2, at most 8 (app/render_gate.py)."""
    return max(2, min(8, (cpus or usable_cpus()) // 2))


def on_change(fn):
    """Call fn() after apply() changed the settings (the worker pools resize themselves)."""
    _listeners.append(fn)
    return fn


def apply(image_workers=None, render_slots=None, memory_mb=None) -> None:
    with _lock:
        _overrides["image_workers"] = image_workers or None
        _overrides["render_slots"] = render_slots or None
        _overrides["memory_mb"] = memory_mb or None
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass


def summary() -> dict:
    """What the Settings page shows: what was detected, what each value is now, and which come from .env."""
    return {
        "cpus": usable_cpus(), "host_cpus": os.cpu_count() or 1, "ram_gb": round(usable_ram_gb(), 1),
        "image_workers": image_workers(), "auto_image_workers": auto_image_workers(),
        "render_slots": render_slots(), "render_auto": not render_slots_fixed(), "render_slots_max": render_slots_max(),
        "tile_workers": tile_workers(), "memory_budget_gb": round(memory_budget_gb(), 1),
        "env": {k: bool(_env_int(k)) for k in ("ND_IMAGE_WORKERS", "ND_RENDERS", "ND_TILE_WORKERS")},
    }
