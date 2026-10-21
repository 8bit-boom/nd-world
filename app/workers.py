"""How many background picture jobs run at once - sized to the machine (docs/HARDWARE.md).

Picture work (thumbnails, opened-picture copies, the thumbnail remake) is mostly Pillow resizing and encoding, which releases Python's
interpreter lock, so threads really do run in parallel. Page serving is unaffected: these are background threads, and half the cores stay
free. Map tiling holds one whole picture in memory (~0.7 GB for a 72 MP map), so a second tiling worker only comes with enough RAM.

    ND_IMAGE_WORKERS   picture jobs at once (default: half the usable cores, at least 1, at most 8)
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


def image_workers(cpus=None) -> int:
    n = _env_int("ND_IMAGE_WORKERS")
    if n:
        return min(n, 32)
    cpus = cpus or usable_cpus()
    return max(1, min(8, cpus // 2))


def tile_workers(ram_gb=None) -> int:
    n = _env_int("ND_TILE_WORKERS")
    if n:
        return min(n, 8)
    return 2 if (ram_gb if ram_gb is not None else _ram_gb()) >= 16 else 1
