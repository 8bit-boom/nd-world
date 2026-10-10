"""Fan control for the System Monitor: the motherboard fan headers (PWM) through Linux hwmon, with an optional "follow the GPU
temperature" mode.

Why this exists: a passive datacenter card (a Tesla T10, a V100 ...) has no fan of its own, so `nvidia-smi` cannot speed one up.
Its fan sits on a motherboard header, and the BIOS fan curve can only follow the board's own sensors (CPU, system, VRM) - never the
GPU. The board's fan chip (an ITE IT87xx on most Gigabyte boards) shows up as /sys/class/hwmon/hwmonN/pwmM once a kernel driver
for it is loaded; this module reads and writes those files. No driver, no fans listed - and it says so instead of failing.

Safety, because a GPU with no airflow gets hot fast:
  * a manual speed is never below MIN_PERCENT, and the curve never goes below it either;
  * in "follow the GPU" mode, an unreadable GPU temperature or one at/above PANIC_TEMP drives the fan to 100 %;
  * the mode and the curve are kept in a small JSON file next to the database, and re-applied by a background thread that only
    exists when that mode is on. If this app is NOT running, the fan stays at the last value written - the page says so.

A leaf module (no router imports). The sysfs root is a parameter so the tests run against a temporary directory.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

HWMON_ROOT = os.environ.get("FAN_HWMON_ROOT", "/sys/class/hwmon")
MIN_PERCENT = 20
PANIC_TEMP = 85.0
LOOP_SECONDS = 5.0
DEFAULT_CURVE = [[40, 30], [55, 50], [65, 75], [75, 100]]       # [GPU C, fan %] - linear between points
MAX_POINTS = 8

_PWM_RE = re.compile(r"^pwm(\d+)$")
_lock = threading.Lock()
_thread = {"t": None, "stop": threading.Event()}


def _config_path():
    from .database import DB_PATH
    return Path(DB_PATH).parent / "fan_control.json"


def _read(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(4096).strip()
    except OSError:
        return ""


def _int(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(int(value)))


def list_fans(root=None):
    """Every PWM output hwmon exposes: [{id, chip, channel, label, rpm, percent, manual, writable}]. `id` is "<hwmon dir>/pwmN"."""
    root = root or HWMON_ROOT
    out = []
    try:
        chips = sorted(os.listdir(root))
    except OSError:
        return out
    for chip in chips:
        base = os.path.join(root, chip)
        name = _read(base + "/name") or chip
        try:
            files = os.listdir(base)
        except OSError:
            continue
        for fn in sorted(files):
            m = _PWM_RE.match(fn)
            if not m:
                continue
            n = m.group(1)
            raw = _int(_read(f"{base}/pwm{n}"))
            if raw is None:
                continue
            enable = _int(_read(f"{base}/pwm{n}_enable"))
            label = _read(f"{base}/fan{n}_label")
            out.append({"id": f"{chip}/pwm{n}", "chip": name, "channel": int(n), "label": label or f"Fan {n}",
                        "rpm": _int(_read(f"{base}/fan{n}_input")), "percent": round(max(0, min(255, raw)) * 100 / 255),
                        "manual": enable == 1, "writable": os.access(f"{base}/pwm{n}", os.W_OK)})
    return out


def _find(fan_id, root=None):
    """(directory, channel) for a fan id that list_fans produced - anything else is refused (no path from the caller is used)."""
    root = root or HWMON_ROOT
    for f in list_fans(root):
        if f["id"] == fan_id:
            return os.path.join(root, fan_id.split("/")[0]), f["channel"], f
    return None


def set_percent(fan_id, percent, root=None):
    """Hold one fan at `percent` (MIN_PERCENT..100) - switches the header to manual mode. Returns {"ok", "message"}."""
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent != percent:
        return {"ok": False, "message": "percent must be a number."}
    percent = max(MIN_PERCENT, min(100, int(round(percent))))
    hit = _find(fan_id, root)
    if hit is None:
        return {"ok": False, "message": "There is no such fan."}
    base, n, info = hit
    if not info["writable"]:
        return {"ok": False, "message": "This fan is read-only here - the container needs the host's /sys mounted (see docs/GPU_SETUP.md, fan control)."}
    try:
        if os.path.exists(f"{base}/pwm{n}_enable"):
            _write(f"{base}/pwm{n}_enable", 1)
        _write(f"{base}/pwm{n}", round(percent * 255 / 100))
    except OSError as e:
        return {"ok": False, "message": "The fan chip refused the change: " + re.sub(r"\s+", " ", str(e))[:120]}
    return {"ok": True, "message": f"Fan held at {percent} %.", "percent": percent}


def give_back(fan_id, root=None):
    """Hand one header back to the motherboard's own (BIOS) curve."""
    hit = _find(fan_id, root)
    if hit is None:
        return {"ok": False, "message": "There is no such fan."}
    base, n, info = hit
    if not info["writable"]:
        return {"ok": False, "message": "This fan is read-only here."}
    try:
        _write(f"{base}/pwm{n}_enable", 2)             # 2 = "automatic" in the hwmon ABI (the chip's own curve)
    except OSError as e:
        return {"ok": False, "message": "The fan chip refused the change: " + re.sub(r"\s+", " ", str(e))[:120]}
    return {"ok": True, "message": "Handed back to the BIOS fan curve."}


# ── the curve ────────────────────────────────────────────────────────────────

def clean_curve(raw):
    """A list of [temp C, fan %] points: numbers, temps 20..110 strictly rising, % MIN_PERCENT..100 never falling. None if unusable."""
    if not isinstance(raw, list) or not (2 <= len(raw) <= MAX_POINTS):
        return None
    pts = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in p):
            return None
        t, pct = float(p[0]), float(p[1])
        if t != t or pct != pct or not (20 <= t <= 110):
            return None
        pts.append([round(t), max(MIN_PERCENT, min(100, round(pct)))])
    pts.sort(key=lambda q: q[0])
    for a, b in zip(pts, pts[1:]):
        if b[0] <= a[0] or b[1] < a[1]:
            return None
    return pts


def percent_for(temp, curve):
    """The fan % for a GPU temperature: linear between points, the first point's value below it, 100 above the last. An unreadable
    temperature or one at PANIC_TEMP and over is 100 - no airflow guess is better than a full-speed fan."""
    if temp is None or temp != temp or temp >= PANIC_TEMP:
        return 100
    if temp <= curve[0][0]:
        return curve[0][1]
    for (t0, p0), (t1, p1) in zip(curve, curve[1:]):
        if temp <= t1:
            return round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0))
    return 100


# ── saved settings + the background loop ─────────────────────────────────────

def load_config():
    try:
        data = json.loads(_config_path().read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"follow_gpu": bool(data.get("follow_gpu")), "fan_ids": [x for x in data.get("fan_ids", []) if isinstance(x, str)][:8],
            "curve": clean_curve(data.get("curve")) or [list(p) for p in DEFAULT_CURVE]}


def save_config(cfg):
    path = _config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg))
    os.replace(tmp, path)


def apply_once(gpu_temp, cfg=None, root=None):
    """One tick of "follow the GPU": set every chosen fan for `gpu_temp`. Returns the percent applied (None when off)."""
    cfg = cfg or load_config()
    if not cfg["follow_gpu"] or not cfg["fan_ids"]:
        return None
    pct = percent_for(gpu_temp, cfg["curve"])
    for fid in cfg["fan_ids"]:
        set_percent(fid, pct, root)
    return pct


def _hottest_gpu():
    from . import system_stats
    g = system_stats.gpu_stats()
    temps = [d.get("temp_c") for d in g.get("devices", []) if isinstance(d.get("temp_c"), (int, float))] if g.get("available") else []
    return max(temps) if temps else None


def _loop(stop):
    while not stop.wait(LOOP_SECONDS):
        try:
            cfg = load_config()
            if not cfg["follow_gpu"]:
                return                                                    # switched off: the thread ends, nothing polls
            apply_once(_hottest_gpu(), cfg)
        except Exception:                                                 # never let the loop die - keep trying
            continue


def start():
    """Start the background loop if "follow the GPU" is saved as on. Safe to call again; a no-op when it is off."""
    with _lock:
        if _thread["t"] is not None and _thread["t"].is_alive():
            return
        if not load_config()["follow_gpu"]:
            return
        _thread["stop"] = threading.Event()
        t = threading.Thread(target=_loop, args=(_thread["stop"],), name="fan-follow-gpu", daemon=True)
        _thread["t"] = t
        t.start()


def stop():
    with _lock:
        _thread["stop"].set()
        _thread["t"] = None


def status(root=None):
    cfg = load_config()
    fans = list_fans(root)
    return {"fans": fans, "follow_gpu": cfg["follow_gpu"], "fan_ids": cfg["fan_ids"], "curve": cfg["curve"],
            "min_percent": MIN_PERCENT, "panic_temp": PANIC_TEMP, "root_exists": os.path.isdir(root or HWMON_ROOT),
            "running": _thread["t"] is not None and _thread["t"].is_alive(), "time": time.time()}
