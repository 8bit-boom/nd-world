"""Fan control through the gateway (rust/nd-gateway/src/fans.rs, `fans` in ND_RUST_ROUTES) against Python's (app/fan_control.py): the
same steps run on a fake fan-chip tree (FAN_HWMON_ROOT, shared by both servers) first by Python, then - the tree and the saved files
put back - by the gateway, with the same answers and the same values left on the chip."""
import json
import os
import shutil
from pathlib import Path

import pytest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD
from .test_gateway_golden import DIRECT, _signed_in, _via

ROOT = os.environ.get("FAN_HWMON_ROOT", "")
pytestmark = pytest.mark.skipif(not DIRECT or not ROOT, reason="needs the gateway, Python's own address and FAN_HWMON_ROOT")
FAN = "it8688@it87.2656/pwm4"


def _tree():
    root = Path(ROOT)
    shutil.rmtree(root, ignore_errors=True)
    d = root / "hwmon3"
    d.mkdir(parents=True)
    for name, text in (("name", "it8688\n"), ("pwm4", "128\n"), ("pwm4_enable", "2\n"), ("fan4_input", "1450\n"), ("fan4_label", "GPU fan\n"),
                       ("pwm2", "255\n"), ("fan2_input", "0\n"), ("temp1_input", "40000\n")):
        (d / name).write_text(text)
    (root / "devices" / "it87.2656").mkdir(parents=True)
    os.symlink("../devices/it87.2656", d / "device")
    (root / "hwmon0").mkdir()
    (root / "hwmon0" / "name").write_text("k10temp\n")


def _chip():
    d = Path(ROOT) / "hwmon3"
    return {f: (d / f).read_text().strip() for f in ("pwm4", "pwm4_enable", "pwm2")}


def _reset_files():
    from app.database import DB_PATH
    for f in ("fan_control.json", "fan_control_orig.json"):
        Path(DB_PATH).with_name(f).unlink(missing_ok=True)


def _strip(v):
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if k not in ("time", "t", "running")}
    return [_strip(x) for x in v] if isinstance(v, list) else v


def test_fan_control_is_pythons(client, seed):
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    steps = [("GET", None), ("POST", {"fan": FAN, "percent": 55}), ("GET", None), ("POST", {"fan": FAN, "percent": 10.6}),
             ("POST", {"fan": FAN, "percent": "x"}), ("POST", {"fan": "nope", "percent": 50}), ("POST", {"fan": 3}),
             ("POST", {"fan": FAN, "auto": True}), ("POST", {"fan": FAN, "auto": True}),
             ("POST", {"follow_gpu": True, "fans": [FAN], "curve": [[40, 30], [70, 90]]}), ("GET", None),
             ("POST", {"follow_gpu": True, "fans": [FAN], "curve": [[40, 30], [70, 60]]}), ("POST", {"follow_gpu": True, "fans": []}),
             ("POST", {"follow_gpu": True, "fans": ["nope"]}), ("POST", {"follow_gpu": True, "fans": "x"}),
             ("POST", {"fan": FAN, "percent": 70}), ("GET", None), ("POST", {"follow_gpu": False}), ("GET", None),
             ("CHECK", {"fan": FAN}), ("CHECK", {"fan": "pwm2-less"}), ("CHECK", {}), ("GET", None)]
    runs = []
    for side in (gm, _via(gm, base)):
        _tree()
        _reset_files()
        out = []
        for method, body in steps:
            if method == "GET":
                r = side.get("/api/system/fans")
            else:
                r = side.post("/api/system/fans/check" if method == "CHECK" else "/api/system/fans", json=body)
            out.append((method, body, r.status_code, _strip(r.json()), _chip(), r.headers.get("cache-control")))
        runs.append((out, r.headers.get("x-nd-served-by")))
    (a, _), (b, served) = runs
    for x, y in zip(a, b):
        assert x == y, (x, y)
    assert served == "gateway"
    for path in ("/api/system/fans",):
        assert player.get(path).status_code == _via(player, base).get(path).status_code == 403
    _reset_files()
