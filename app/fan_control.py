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


def _orig_path():
    return _config_path().with_name("fan_control_orig.json")


def _boot_id():
    return _read("/proc/sys/kernel/random/boot_id") or "unknown"


def _load_orig():
    """{fan id: {"enable": n|None, "pwm": raw}} remembered since the BIOS last set the fans - and only for this boot: after a
    reboot the BIOS has set everything again, so an older note would put back a stale value."""
    try:
        data = json.loads(_orig_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("boot") != _boot_id() or not isinstance(data.get("fans"), dict):
        return {}
    return data["fans"]


def _save_orig(fans):
    path = _orig_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"boot": _boot_id(), "fans": fans}))
    os.replace(tmp, path)


def set_percent(fan_id, percent, root=None):
    """Hold one fan at `percent` (MIN_PERCENT..100) - switches the header to manual mode. The first time, what the BIOS had set
    (mode and duty) is written down so give_back can put exactly that back. Returns {"ok", "message"}."""
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent != percent:
        return {"ok": False, "message": "percent must be a number."}
    percent = max(MIN_PERCENT, min(100, int(round(percent))))
    hit = _find(fan_id, root)
    if hit is None:
        return {"ok": False, "message": "There is no such fan."}
    base, n, info = hit
    if not info["writable"]:
        return {"ok": False, "message": "This fan is read-only here - the container needs the host's /sys mounted (see docs/GPU_SETUP.md, fan control)."}
    raw_target = round(percent * 255 / 100)
    try:
        saved = _load_orig()
        if fan_id not in saved:
            saved[fan_id] = {"enable": _int(_read(f"{base}/pwm{n}_enable")), "pwm": _int(_read(f"{base}/pwm{n}"))}
            _save_orig(saved)
        if os.path.exists(f"{base}/pwm{n}_enable"):
            _write(f"{base}/pwm{n}_enable", 1)
        _write(f"{base}/pwm{n}", raw_target)
    except OSError as e:
        return {"ok": False, "message": "The fan chip refused the change: " + re.sub(r"\s+", " ", str(e))[:120]}
    got = _int(_read(f"{base}/pwm{n}"))
    if got is not None and abs(got - raw_target) > 8:
        return {"ok": True, "message": f"Asked for {percent} % but the chip reads {round(got * 100 / 255)} % - the BIOS may be rewriting this header.",
                "percent": percent, "warning": True}
    return {"ok": True, "message": f"Fan held at {percent} %.", "percent": percent}


def give_back(fan_id, root=None):
    """Put one header back as the BIOS had set it (mode and duty, remembered when this app first took it over). A fan this app never
    changed is left alone; after a reboot the BIOS has it again anyway."""
    hit = _find(fan_id, root)
    if hit is None:
        return {"ok": False, "message": "There is no such fan."}
    base, n, info = hit
    if not info["writable"]:
        return {"ok": False, "message": "This fan is read-only here."}
    saved = _load_orig()
    note = saved.get(fan_id)
    if note is None:
        return {"ok": True, "message": "This fan is not held by nd-world; it is on whatever the BIOS set. (If you changed it by hand, a reboot gives it back.)"}
    try:
        if note.get("pwm") is not None:
            _write(f"{base}/pwm{n}", note["pwm"])
        if note.get("enable") is not None and os.path.exists(f"{base}/pwm{n}_enable"):
            _write(f"{base}/pwm{n}_enable", note["enable"])
    except OSError as e:
        return {"ok": False, "message": "The fan chip refused the change: " + re.sub(r"\s+", " ", str(e))[:120]}
    saved.pop(fan_id, None)
    _save_orig(saved)
    return {"ok": True, "message": "Put back as the BIOS had it."}


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
