"""Live host health for the System Monitor page: GPU (VRAM, temperature, power, load), CPU and RAM.

Everything is read, never written: /proc and /sys for the CPU and memory (inside a Docker container these show the HOST's
numbers - they are the same files), and the `nvidia-smi` command for GPUs. nvidia-smi is not part of this app's image: the NVIDIA
container runtime puts it inside any container that is given the GPU (the compose file's `deploy.resources.reservations.devices`
block with capability `utility`), so a container without the GPU reports `available: false` and says why instead of failing.

A leaf module (no router imports). Output of external programs and files is untrusted text: every field is parsed defensively,
a value that is not a number becomes None ("[N/A]", "[Not Supported]" and garbage alike), and nothing is passed to a shell.
"""
import csv
import io
import os
import re
import shutil
import subprocess
import threading
import time

CACHE_SECONDS = 1.0
_GPU_FIELDS = ("index", "name", "uuid", "utilization.gpu", "utilization.memory", "memory.used", "memory.total", "temperature.gpu",
               "power.draw", "power.limit", "fan.speed", "clocks.sm", "clocks.max.sm", "pstate",
               "power.min_limit", "power.max_limit", "power.default_limit")
_APP_FIELDS = ("pid", "process_name", "used_memory", "gpu_uuid")

_lock = threading.Lock()
_cache = {"t": 0.0, "data": None}
_prev_cpu = {"v": None}


def _read(path, limit=262144):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _num(v):
    """A finite float from text such as '61', '39.12', '[N/A]' or '' - None when it is not a plain number."""
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


# ── CPU ──────────────────────────────────────────────────────────────────────

def parse_cpu_times(stat_text):
    """([total, idle] for all CPUs, [[total, idle] per core]) from /proc/stat text; idle includes iowait."""
    allc, cores = None, []
    for line in (stat_text or "").splitlines():
        m = re.match(r"^cpu(\d*)\s+(.*)$", line)
        if not m:
            continue
        nums = [int(x) for x in m.group(2).split() if x.isdigit()][:8]
        if len(nums) < 5:
            continue
        pair = [sum(nums), nums[3] + nums[4]]
        if m.group(1) == "":
            allc = pair
        else:
            cores.append(pair)
    return allc, cores


def cpu_percent(prev, cur):
    """Busy percentage between two [total, idle] samples, 0-100, or None."""
    if not prev or not cur:
        return None
    dt, di = cur[0] - prev[0], cur[1] - prev[1]
    if dt <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (dt - di) / dt)), 1)


def _cpu_temperature():
    """CPU package temperature in C from the usual hwmon / thermal names, or None (virtual machines often have none)."""
    best, preferred = None, False
    try:
        for hw in sorted(os.listdir("/sys/class/hwmon")):
            base = "/sys/class/hwmon/" + hw
            if _read(base + "/name").strip() not in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "cpu-thermal"):
                continue
            for fn in sorted(os.listdir(base)):
                if not re.match(r"^temp\d+_input$", fn):
                    continue
                v = _num(_read(base + "/" + fn))
                if v is None:
                    continue
                label = _read(base + "/" + fn.replace("_input", "_label")).strip()
                is_pkg = label in ("Tctl", "Tdie", "Package id 0", "CPU")
                if best is None or (is_pkg and not preferred):      # the package reading beats a single core's
                    best, preferred = v / 1000.0, is_pkg
    except OSError:
        pass
    if best is None:
        try:
            for z in sorted(os.listdir("/sys/class/thermal")):
                if z.startswith("thermal_zone") and _read("/sys/class/thermal/" + z + "/type").strip() in ("x86_pkg_temp", "cpu-thermal", "cpu_thermal"):
                    v = _num(_read("/sys/class/thermal/" + z + "/temp"))
                    if v is not None:
                        best = v / 1000.0
                        break
        except OSError:
            pass
    return None if best is None or not (-50 < best < 200) else round(best, 1)


def cpu_stats():
    allc, cores = parse_cpu_times(_read("/proc/stat"))
    prev = _prev_cpu["v"]
    if prev is None:                                  # first reading: take a short second sample so the first answer has a percentage
        time.sleep(0.25)
        prev = (allc, cores)
        allc, cores = parse_cpu_times(_read("/proc/stat"))
    _prev_cpu["v"] = (allc, cores)
    per_core = [cpu_percent(p, c) for p, c in zip(prev[1], cores)]
    load = _read("/proc/loadavg").split()
    model = ""
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            model = line.split(":", 1)[-1].strip()[:120]
            break
    return {
        "percent": cpu_percent(prev[0], allc),
        "cores": len(cores) or (os.cpu_count() or 0),
        "per_core": per_core,
        "load": [_num(x) for x in load[:3]] if len(load) >= 3 else [],
        "temp_c": _cpu_temperature(),
        "model": model,
    }


# ── memory ───────────────────────────────────────────────────────────────────

def parse_meminfo(text):
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([A-Za-z_()0-9]+):\s+(\d+)\s*kB", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def memory_stats():
    mi = parse_meminfo(_read("/proc/meminfo"))
    total = mi.get("MemTotal", 0)
    avail = mi.get("MemAvailable", mi.get("MemFree", 0))
    used = max(0, total - avail)
    swap_total, swap_free = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
    arc = None                                          # TrueNAS/ZFS: the ARC cache looks like "used" RAM but is given back on demand
    for line in _read("/proc/spl/kstat/zfs/arcstats").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "size" and parts[2].isdigit():
            arc = int(parts[2])
    return {
        "total": total, "used": used, "available": avail,
        "percent": round(100.0 * used / total, 1) if total else None,
        "swap_total": swap_total, "swap_used": max(0, swap_total - swap_free),
        "zfs_arc": arc,
        "percent_without_arc": round(100.0 * max(0, used - arc) / total, 1) if (total and arc is not None) else None,
    }


# ── GPU ──────────────────────────────────────────────────────────────────────

def _run(args, timeout=4.0):
    """(returncode, stdout, stderr) of a fixed argument list - never through a shell."""
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, shell=False)
    return p.returncode, p.stdout, p.stderr


def _csv_rows(text, width):
    rows = []
    for row in csv.reader(io.StringIO(text or ""), skipinitialspace=True):
        if len(row) == width:
            rows.append([c.strip() for c in row])
    return rows


def parse_gpus(text):
    gpus = []
    for r in _csv_rows(text, len(_GPU_FIELDS)):
        d = dict(zip(_GPU_FIELDS, r))
        idx = _num(d["index"])
        gpus.append({
            "index": int(idx) if idx is not None else len(gpus), "name": d["name"][:80], "uuid": d["uuid"][:64],
            "util_percent": _num(d["utilization.gpu"]), "mem_util_percent": _num(d["utilization.memory"]),
            "mem_used_mib": _num(d["memory.used"]), "mem_total_mib": _num(d["memory.total"]),
            "temp_c": _num(d["temperature.gpu"]), "power_w": _num(d["power.draw"]), "power_limit_w": _num(d["power.limit"]),
            "fan_percent": _num(d["fan.speed"]), "clock_mhz": _num(d["clocks.sm"]), "clock_max_mhz": _num(d["clocks.max.sm"]),
            "pstate": d["pstate"][:8] if d["pstate"].startswith("P") else None,
            "power_min_w": _num(d["power.min_limit"]), "power_max_w": _num(d["power.max_limit"]), "power_default_w": _num(d["power.default_limit"]),
            "processes": [],
        })
    return gpus


def parse_gpu_apps(text):
    apps = []
    for r in _csv_rows(text, len(_APP_FIELDS)):
        d = dict(zip(_APP_FIELDS, r))
        used = _num(d["used_memory"])
        if used is None:
            continue
        apps.append({"pid": int(_num(d["pid"]) or 0), "name": os.path.basename(d["process_name"])[:60] or "process",
                     "mem_mib": used, "gpu_uuid": d["gpu_uuid"]})
    return apps


def gpu_stats(run=_run, which=shutil.which):
    exe = which("nvidia-smi")
    if not exe:
        return {"available": False, "devices": [],
                "reason": "nvidia-smi is not available inside this container. Give the nd-world service the GPU (see the System Monitor "
                          "section of docs/GPU_SETUP.md) and restart it."}
    try:
        code, out, err = run([exe, "--query-gpu=" + ",".join(_GPU_FIELDS), "--format=csv,noheader,nounits"])
    except (OSError, subprocess.SubprocessError) as e:
        return {"available": False, "devices": [], "reason": "nvidia-smi could not be run: " + re.sub(r"\s+", " ", str(e))[:160]}
    if code != 0:
        why = re.sub(r"[^\x20-\x7e]+", " ", (err or out or "")).strip()[:200]
        return {"available": False, "devices": [], "reason": "nvidia-smi failed" + (": " + why if why else ".")}
    gpus = parse_gpus(out)
    if not gpus:
        return {"available": False, "devices": [], "reason": "nvidia-smi reported no GPUs."}
    try:
        code2, out2, _e = run([exe, "--query-compute-apps=" + ",".join(_APP_FIELDS), "--format=csv,noheader,nounits"])
        if code2 == 0:
            by_uuid = {g["uuid"]: g for g in gpus}
            for a in parse_gpu_apps(out2):
                g = by_uuid.get(a["gpu_uuid"])
                if g is not None and len(g["processes"]) < 8:
                    g["processes"].append({"pid": a["pid"], "name": a["name"], "mem_mib": a["mem_mib"]})
    except (OSError, subprocess.SubprocessError):
        pass
    for g in gpus:
        g.pop("uuid", None)
    return {"available": True, "devices": gpus}


# ── setting the GPU power limit ──────────────────────────────────────────────

def set_power_limit(index, watts, run=_run, which=shutil.which):
    """Set card `index` to `watts` (whole watts, inside the card's own min..max). Returns {"ok": bool, "message": str, ...}.
    The range check uses nvidia-smi's own reported limits, so an out-of-range or nonsense value is refused here, before any command
    runs. Needs permission inside the container (see docs/GPU_SETUP.md); a refusal is reported with the host-side alternative."""
    if isinstance(index, bool) or isinstance(watts, bool) or not isinstance(index, int) or not isinstance(watts, (int, float)):
        return {"ok": False, "message": "index and watts must be numbers."}
    if watts != watts or watts in (float("inf"), float("-inf")) or int(watts) != watts:
        return {"ok": False, "message": "watts must be a whole number."}
    watts = int(watts)
    current = gpu_stats(run=run, which=which)
    if not current["available"]:
        return {"ok": False, "message": current.get("reason", "No GPU available.")}
    card = next((g for g in current["devices"] if g["index"] == index), None)
    if card is None:
        return {"ok": False, "message": "There is no GPU with that index."}
    lo, hi = card.get("power_min_w"), card.get("power_max_w")
    if lo is None or hi is None:
        return {"ok": False, "message": "This card does not report a power-limit range, so the limit cannot be set from here."}
    if not (lo <= watts <= hi):
        return {"ok": False, "message": "The limit must be between %d and %d W for this card." % (round(lo), round(hi))}
    exe = which("nvidia-smi")
    try:
        code, out, err = run([exe, "-i", str(index), "-pl", str(watts)], 10.0)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "message": "nvidia-smi could not be run: " + re.sub(r"\s+", " ", str(e))[:160]}
    with _lock:
        _cache["data"] = None                              # the next reading must show the new limit
    text = re.sub(r"[^\x20-\x7e\n]+", " ", (out or "") + " " + (err or "")).strip()
    if code != 0:
        denied = "permission" in text.lower() or "not supported" in text.lower()
        hint = (" The container is not allowed to change it. Set it from the TrueNAS shell instead: sudo nvidia-smi -i %d -pl %d "
                "(add it under System → Advanced → Init/Shutdown Scripts as a post-init command to keep it after a reboot)." % (index, watts)) if denied else ""
        return {"ok": False, "message": ("nvidia-smi refused: " + text[:200] if text else "nvidia-smi failed.") + hint}
    return {"ok": True, "message": "Power limit set to %d W. It lasts until the driver reloads or the machine restarts." % watts, "watts": watts}


# ── everything, briefly cached so many open pages cost one reading a second ───

def snapshot():
    with _lock:
        now = time.time()
        if _cache["data"] is not None and now - _cache["t"] < CACHE_SECONDS:
            return _cache["data"]
        data = {"time": now, "cpu": cpu_stats(), "memory": memory_stats(), "gpu": gpu_stats()}
        _cache["t"], _cache["data"] = now, data
        return data
