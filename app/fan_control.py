"""Fan control for the System Monitor: the motherboard fan headers (PWM) through Linux hwmon, with an optional "follow the GPU
temperature" mode.

Why this exists: a passive datacenter card (a Tesla T10, a V100 ...) has no fan of its own, so `nvidia-smi` cannot speed one up.
Its fan sits on a motherboard header, and the BIOS fan curve can only follow the board's own sensors - never the GPU's die. A board's
fan chip (an ITE IT87xx on most Gigabyte boards) shows up as pwmN files under /sys/class/hwmon once a kernel driver for it is loaded;
this module reads and writes those files. No driver, no fans listed - and it says so instead of failing. A header wired to a chip the
driver does not support cannot be reached at all (on an X570 Aorus Master the stock driver finds only the secondary IT8792E).

What is and is not known about the hardware is kept honest here, because a GPU with no airflow gets hot fast:
  * an output is only ever identified by its chip name + device address + channel (`it8792@it87.2656/pwm3`), never by the `hwmonN`
    directory number, which the kernel may hand out differently after a reboot or a driver reload;
  * a speed is never below MIN_PERCENT, and a "follow the GPU" curve has to reach TOP_PERCENT well before PANIC_TEMP;
  * in "follow the GPU" mode an unreadable GPU temperature, or one at/above PANIC_TEMP, drives the fan to 100 %; the fan speeds up at
    once but slows down gently (RAMP_DOWN points per tick), so it does not hunt;
  * `check_fan` runs a fan briefly at 100 % and reports whether its rpm really rose - an output whose rpm FALLS with a higher duty is
    inverted, and "100 % when hot" would then stop that fan;
  * when this app stops it raises every fan it holds to at least SHUTDOWN_PERCENT (never lowers one): another container may still be
    loading the GPU. If the app is killed outright nothing can run - the page and docs say so;
  * "BIOS" puts back exactly the mode and duty the BIOS had set when this app first took the header over (remembered per boot).

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
PANIC_TEMP = 80.0
TOP_PERCENT = 80                 # a curve must reach at least this ...
TOP_MARGIN = 5                   # ... by PANIC_TEMP - TOP_MARGIN
LOOP_SECONDS = 5.0
RAMP_DOWN = 5                    # percentage points a followed fan may slow per tick
SHUTDOWN_PERCENT = 60
CHECK_SECONDS = 6
DEFAULT_CURVE = [[45, 35], [55, 55], [65, 80], [72, 100]]       # [GPU C, fan %] - linear between points
MAX_POINTS = 8

_PWM_RE = re.compile(r"^pwm(\d+)$")
_lock = threading.Lock()
_thread = {"t": None, "stop": threading.Event()}
_state = {"v": None}             # what the last follow tick did, for the page
_checked = {}                    # fan id -> {"verdict", "t"}: the result of check_fan this run


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


def _clean_name(text):
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", str(text))[:40] or "chip"


def _device_name(base):
    """The platform / bus device behind a hwmon directory ("it87.2656") - stable across reboots, unlike hwmonN."""
    try:
        return os.path.basename(os.readlink(base + "/device").rstrip("/"))
    except OSError:
        return ""


def list_fans(root=None):
    """Every PWM output hwmon exposes: [{id, dir, chip, channel, label, rpm, percent, raw, enable, manual, writable}].
    `id` is "<chip name>@<device>/pwmN" - never the hwmonN number, which can change between boots."""
    root = root or HWMON_ROOT
    out = []
    try:
        chips = sorted(os.listdir(root))
    except OSError:
        return out
    for chip in chips:
        base = os.path.join(root, chip)
        name = _read(base + "/name") or chip
        where = _device_name(base) or chip
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
            out.append({"id": f"{_clean_name(name)}@{_clean_name(where)}/pwm{n}", "dir": chip, "chip": name, "channel": int(n),
                        "label": label or f"Fan {n}", "rpm": _int(_read(f"{base}/fan{n}_input")),
                        "percent": round(max(0, min(255, raw)) * 100 / 255), "raw": raw, "enable": enable, "manual": enable == 1,
                        "writable": os.access(f"{base}/pwm{n}", os.W_OK)})
    return out


def _find(fan_id, root=None):
    """(directory, channel, info) for a fan id that list_fans produced - anything else is refused (no path from the caller is used)."""
    root = root or HWMON_ROOT
    for f in list_fans(root):
        if f["id"] == fan_id:
            return os.path.join(root, f["dir"]), f["channel"], f
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


def _oserr(e):
    return re.sub(r"\s+", " ", str(e))[:120]


def _apply(base, n, raw):
    """Manual mode at duty `raw` (0-255). Writes the mode only when it is not already manual, so a steady loop touches the chip once."""
    if os.path.exists(f"{base}/pwm{n}_enable") and _int(_read(f"{base}/pwm{n}_enable")) != 1:
        _write(f"{base}/pwm{n}_enable", 1)
    _write(f"{base}/pwm{n}", raw)


def set_percent(fan_id, percent, root=None):
    """Hold one fan at `percent` (MIN_PERCENT..100) - switches the header to manual mode. The first time, what the BIOS had set
    (mode and duty) is written down so give_back can put exactly that back. Returns {"ok", "message"}."""
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent != percent:
        return {"ok": False, "message": "percent must be a number."}
    percent = max(MIN_PERCENT, min(100, int(round(percent))))
    hit = _find(fan_id, root)
    if hit is None:
        return {"ok": False, "message": "There is no such fan (the driver may not be loaded)."}
    base, n, info = hit
    if not info["writable"]:
        return {"ok": False, "message": "This fan is read-only here - the container needs write access to the fan chip's folder (see docs/GPU_SETUP.md, Fan control)."}
    raw_target = round(percent * 255 / 100)
    try:
        saved = _load_orig()
        if fan_id not in saved:
            saved[fan_id] = {"enable": info["enable"], "pwm": info["raw"]}
            _save_orig(saved)
    except OSError as e:
        return {"ok": False, "message": "Could not write down what the BIOS had set (" + _oserr(e) + "), so the fan was left alone."}
    try:
        _apply(base, n, raw_target)
    except OSError as e:
        return {"ok": False, "message": "The fan chip refused the change: " + _oserr(e)}
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
        return {"ok": False, "message": "There is no such fan (the driver may not be loaded)."}
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
        return {"ok": False, "message": "The fan chip refused the change: " + _oserr(e)}
    saved.pop(fan_id, None)
    try:
        _save_orig(saved)
    except OSError:
        pass
    return {"ok": True, "message": "Put back as the BIOS had it."}


# ── is this output a fan that really speeds up? ──────────────────────────────

def check_fan(fan_id, root=None, sleep=time.sleep, seconds=None):
    """Run one fan at 100 % for a few seconds and compare its rpm, then put it back EXACTLY as it was (a hold stays a hold).
    Never slows a fan: it only goes up. verdict: "speeds_up" | "inverted" (rpm fell - higher duty = slower: do not use) |
    "no_change" (a fan that ignores PWM, or a pump with a narrow range) | "no_signal" (no rpm reading - listen to it instead)."""
    hit = _find(fan_id, root)
    if hit is None:
        return {"ok": False, "verdict": "", "message": "There is no such fan (the driver may not be loaded)."}
    base, n, info = hit
    if not info["writable"]:
        return {"ok": False, "verdict": "", "message": "This fan is read-only here."}
    cfg = load_config()
    if cfg["follow_gpu"] and fan_id in cfg["fan_ids"]:
        return {"ok": False, "verdict": "", "message": "This fan is following the GPU right now; stop that first so the check is not fought."}
    before = info["rpm"]
    if before is None:
        return {"ok": False, "verdict": "no_signal", "message": "This output has no speed (rpm) signal, so it cannot be checked here - listen to the fan while you test it."}
    if before == 0:
        return {"ok": False, "verdict": "no_signal", "message": "0 rpm: nothing spinning on this output (or its speed wire is not connected)."}
    if info["percent"] >= 95:
        return {"ok": False, "verdict": "", "message": "It is already at full speed, so a speed-up cannot be seen. Lower it first (not a pump!) or check it when it is slower."}
    prev_enable, prev_raw = info["enable"], info["raw"]
    try:
        _apply(base, n, 255)
    except OSError as e:
        return {"ok": False, "verdict": "", "message": "The fan chip refused the change: " + _oserr(e)}
    try:
        sleep(CHECK_SECONDS if seconds is None else seconds)
        after = _int(_read(f"{base}/fan{n}_input"))
    finally:
        try:                                                    # always put it back, even if the request is cancelled
            _write(f"{base}/pwm{n}", prev_raw)
            if prev_enable is not None and os.path.exists(f"{base}/pwm{n}_enable"):
                _write(f"{base}/pwm{n}_enable", prev_enable)
        except OSError:
            pass
    if after is None:
        return {"ok": False, "verdict": "no_signal", "message": "The speed signal disappeared during the check."}
    ratio = after / before
    if ratio >= 1.10:
        verdict, msg = "speeds_up", f"{before} → {after} rpm (+{round((ratio - 1) * 100)} %): this output speeds the fan up when asked."
    elif ratio <= 0.90:
        verdict, msg = "inverted", f"{before} → {after} rpm: it got SLOWER at 100 %. This output looks inverted - do not use it to cool anything."
    else:
        verdict, msg = "no_change", f"{before} → {after} rpm: no clear change. It may ignore PWM (a pump, or a fixed-speed fan) - do not rely on it."
    _checked[fan_id] = {"verdict": verdict, "t": time.time()}
    return {"ok": True, "verdict": verdict, "message": msg, "rpm_before": before, "rpm_after": after}


def verdict(fan_id):
    return (_checked.get(fan_id) or {}).get("verdict", "")


# ── the curve ────────────────────────────────────────────────────────────────

def clean_curve(raw):
    """A list of [temp C, fan %] points: numbers, temps 20..110 strictly rising, % MIN_PERCENT..100 never falling, and the last point
    must reach TOP_PERCENT by PANIC_TEMP - TOP_MARGIN (a curve that never gets the air moving is refused, not obeyed). None if unusable."""
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
    if pts[-1][1] < TOP_PERCENT or pts[-1][0] > PANIC_TEMP - TOP_MARGIN:
        return None
    return pts


def percent_for(temp, curve):
    """The fan % for a GPU temperature: linear between points, the first point's value below it, the last point's above it. An
    unreadable temperature, or one at PANIC_TEMP and over, is 100 - no airflow guess is better than a full-speed fan."""
    if temp is None or temp != temp or temp >= PANIC_TEMP:
        return 100
    if temp <= curve[0][0]:
        return curve[0][1]
    for (t0, p0), (t1, p1) in zip(curve, curve[1:]):
        if temp <= t1:
            return round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0))
    return curve[-1][1]


def smooth(current, target):
    """Speed up at once, slow down gently: no hunting around a curve point, and a one-off bad reading fades instead of snapping back."""
    if current is None or target >= current:
        return target
    return max(target, current - RAMP_DOWN)


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
    """One tick of "follow the GPU": move every chosen fan toward the curve's value for `gpu_temp`. Returns the target percent
    (None when off); what each fan actually got, and anything that went wrong, is kept for the page (status()["state"])."""
    cfg = cfg or load_config()
    if not cfg["follow_gpu"] or not cfg["fan_ids"]:
        return None
    target = percent_for(gpu_temp, cfg["curve"])
    by_id = {f["id"]: f for f in list_fans(root)}
    applied, errors = {}, []
    for fid in cfg["fan_ids"]:
        f = by_id.get(fid)
        if f is None:
            errors.append(f"{fid}: not found (is the fan-chip driver loaded?)")
            continue
        current = f["percent"] if f["manual"] else None
        want = smooth(current, target)
        if current is not None and abs(want - current) <= 1:
            applied[fid] = current                                # already there: no write
            continue
        r = set_percent(fid, want, root)
        if r["ok"]:
            applied[fid] = want
        else:
            errors.append(f"{fid}: {r['message']}")
    _state["v"] = {"t": time.time(), "gpu_temp": gpu_temp, "target": target, "applied": applied, "errors": errors}
    return target


def gpu_temperature():
    """The hottest GPU's die temperature in C, or None when it cannot be read."""
    from . import system_stats
    g = system_stats.gpu_stats()
    temps = [d.get("temp_c") for d in g.get("devices", []) if isinstance(d.get("temp_c"), (int, float))] if g.get("available") else []
    return max(temps) if temps else None


_hottest_gpu = gpu_temperature                                    # the name the first version used


def _loop(stop):
    while not stop.wait(LOOP_SECONDS):
        try:
            cfg = load_config()
            if not cfg["follow_gpu"]:
                return                                                    # switched off: the thread ends, nothing polls
            apply_once(gpu_temperature(), cfg)
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


def shutdown_safe(root=None):
    """The app is stopping. Another container may still be loading the GPU, so no fan this app holds is left slow: any held fan below
    SHUTDOWN_PERCENT is raised to it (never lowered). Quiet again with the BIOS button, or after a reboot."""
    try:
        held = list(_load_orig())
        if not held:
            return
        by_id = {f["id"]: f for f in list_fans(root)}
        for fid in held:
            f = by_id.get(fid)
            if f is not None and f["percent"] < SHUTDOWN_PERCENT:
                set_percent(fid, SHUTDOWN_PERCENT, root)
    except Exception:                                                     # shutdown must never hang on a fan
        pass


def status(root=None):
    cfg = load_config()
    fans = list_fans(root)
    ids = {f["id"] for f in fans}
    held = set(_load_orig())                                       # fans THIS app took over (manual mode alone is also how some BIOSes work)
    for f in fans:
        f["held"] = f["id"] in held
    return {"fans": fans, "follow_gpu": cfg["follow_gpu"], "fan_ids": cfg["fan_ids"], "curve": cfg["curve"],
            "missing": [i for i in cfg["fan_ids"] if i not in ids], "any_writable": any(f["writable"] for f in fans),
            "min_percent": MIN_PERCENT, "panic_temp": PANIC_TEMP, "top_percent": TOP_PERCENT, "top_margin": TOP_MARGIN,
            "shutdown_percent": SHUTDOWN_PERCENT, "checked": {k: v["verdict"] for k, v in _checked.items()},
            "root_exists": os.path.isdir(root or HWMON_ROOT), "state": _state["v"],
            "running": _thread["t"] is not None and _thread["t"].is_alive(), "time": time.time()}
