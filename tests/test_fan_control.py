"""Fan control (app/fan_control.py, /api/system/fans): hwmon PWM read / write against a fake sysfs tree, the GPU-follow curve and its
safety rules, and the GM-only routes."""
import os

import pytest

from app import fan_control as fc

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture
def hwmon(tmp_path, monkeypatch):
    chip = tmp_path / "hwmon3"
    chip.mkdir()
    (chip / "name").write_text("it8688\n")
    (chip / "pwm4").write_text("128\n")
    (chip / "pwm4_enable").write_text("2\n")
    (chip / "fan4_input").write_text("1450\n")
    (chip / "fan4_label").write_text("GPU fan\n")
    (chip / "temp1_input").write_text("40000\n")                       # not a pwm: ignored
    other = tmp_path / "hwmon0"
    other.mkdir()
    (other / "name").write_text("k10temp\n")                           # a chip with no fans
    monkeypatch.setattr(fc, "HWMON_ROOT", str(tmp_path))
    monkeypatch.setattr(fc, "_config_path", lambda: tmp_path / "fan_control.json")
    monkeypatch.setattr(fc, "_boot_id", lambda: "boot-1")
    return tmp_path


def test_lists_only_pwm_outputs(hwmon):
    fans = fc.list_fans()
    assert [(f["id"], f["chip"], f["label"], f["rpm"], f["percent"], f["manual"], f["writable"]) for f in fans] == \
        [("hwmon3/pwm4", "it8688", "GPU fan", 1450, 50, False, True)]


def test_no_hwmon_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "HWMON_ROOT", str(tmp_path / "missing"))
    assert fc.list_fans() == [] and fc.status()["root_exists"] is False


def test_set_percent_writes_manual_mode_and_pwm_with_a_floor(hwmon):
    r = fc.set_percent("hwmon3/pwm4", 60)
    assert r["ok"] and (hwmon / "hwmon3/pwm4_enable").read_text() == "1" and (hwmon / "hwmon3/pwm4").read_text() == "153"
    assert fc.set_percent("hwmon3/pwm4", 0)["percent"] == fc.MIN_PERCENT        # never stopped
    assert (hwmon / "hwmon3/pwm4").read_text() == str(round(fc.MIN_PERCENT * 255 / 100))
    assert fc.set_percent("hwmon3/pwm4", 400)["percent"] == 100


def test_refuses_unknown_fans_and_junk(hwmon):
    assert not fc.set_percent("../../etc/passwd", 50)["ok"]
    assert not fc.set_percent("hwmon3/pwm9", 50)["ok"]
    assert not fc.set_percent("hwmon3/pwm4", "fast")["ok"]
    assert not fc.set_percent("hwmon3/pwm4", True)["ok"]


def test_give_back_puts_the_bios_values_back_not_a_guess(hwmon):
    (hwmon / "hwmon3/pwm4_enable").write_text("1\n")                    # this board: the BIOS holds a fixed duty in manual mode
    (hwmon / "hwmon3/pwm4").write_text("128\n")
    fc.set_percent("hwmon3/pwm4", 90)
    fc.set_percent("hwmon3/pwm4", 70)                                    # a second change must not overwrite the remembered original
    assert (hwmon / "hwmon3/pwm4").read_text() == str(round(70 * 255 / 100))
    assert fc.give_back("hwmon3/pwm4")["ok"]
    assert (hwmon / "hwmon3/pwm4").read_text() == "128" and (hwmon / "hwmon3/pwm4_enable").read_text() == "1"
    again = fc.give_back("hwmon3/pwm4")                                  # nothing held now: leaves it alone
    assert again["ok"] and "not held" in again["message"] and (hwmon / "hwmon3/pwm4").read_text() == "128"


def test_a_note_from_before_a_reboot_is_not_used(hwmon, monkeypatch):
    fc.set_percent("hwmon3/pwm4", 90)                                    # remembers enable=2, pwm=128 under boot-1
    monkeypatch.setattr(fc, "_boot_id", lambda: "boot-2")
    (hwmon / "hwmon3/pwm4").write_text("77\n")
    assert "not held" in fc.give_back("hwmon3/pwm4")["message"] and (hwmon / "hwmon3/pwm4").read_text().strip() == "77"


def test_set_percent_warns_when_the_chip_does_not_keep_the_value(hwmon, monkeypatch):
    real = fc._write
    def sticky(path, value):                                              # a BIOS that rewrites the duty straight away
        real(path, 128 if path.endswith("/pwm4") else value)
    monkeypatch.setattr(fc, "_write", sticky)
    r = fc.set_percent("hwmon3/pwm4", 90)
    assert r["ok"] and r.get("warning") and "BIOS may be rewriting" in r["message"]


def test_curve_validation():
    assert fc.clean_curve([[40, 30], [60, 60]]) == [[40, 30], [60, 60]]
    assert fc.clean_curve([[40, 5], [60, 60]]) == [[40, 20], [60, 60]]          # floor applies
    assert fc.clean_curve([[60, 60], [40, 30]]) == [[40, 30], [60, 60]]          # order of entry does not matter
    assert fc.clean_curve([[40, 60], [60, 30]]) is None                          # a hotter GPU may never slow the fan
    assert fc.clean_curve([[40, 30], [40, 60]]) is None
    assert fc.clean_curve([[40, 30]]) is None and fc.clean_curve("x") is None
    assert fc.clean_curve([[10, 30], [60, 60]]) is None and fc.clean_curve([["a", 1], [2, 3]]) is None


def test_follow_curve_and_panic_rules():
    curve = [[40, 30], [60, 60], [80, 100]]
    assert fc.percent_for(30, curve) == 30 and fc.percent_for(50, curve) == 45 and fc.percent_for(70, curve) == 80
    assert fc.percent_for(None, curve) == 100                                    # no reading -> full speed
    assert fc.percent_for(fc.PANIC_TEMP, curve) == 100 and fc.percent_for(float("nan"), curve) == 100


def test_apply_once_drives_the_chosen_fans(hwmon):
    cfg = {"follow_gpu": True, "fan_ids": ["hwmon3/pwm4"], "curve": [[40, 30], [60, 60]]}
    assert fc.apply_once(50, cfg) == 45 and (hwmon / "hwmon3/pwm4").read_text() == str(round(45 * 255 / 100))
    assert fc.apply_once(50, {**cfg, "follow_gpu": False}) is None


def test_routes_are_gm_only_and_work(client, seed, hwmon, monkeypatch):
    monkeypatch.setattr(fc, "_hottest_gpu", lambda: 52.0)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/system/fans").status_code == 403
    assert client.post("/api/system/fans", json={"fan": "hwmon3/pwm4", "percent": 50}).status_code == 403
    client.cookies.clear()
    login(client, seed.gm.email, GM_PASSWORD)
    d = client.get("/api/system/fans").json()
    assert d["fans"][0]["id"] == "hwmon3/pwm4" and d["min_percent"] == 20 and d["follow_gpu"] is False
    assert client.post("/api/system/fans", json={"fan": "hwmon3/pwm4", "percent": 55}).json()["ok"] is True
    assert client.post("/api/system/fans", json={"fan": "nope", "percent": 55}).status_code == 400
    assert client.post("/api/system/fans", json={"fan": "hwmon3/pwm4", "auto": True}).json()["ok"] is True      # puts the BIOS values back
    # follow the GPU: validated, saved, applied once now
    assert client.post("/api/system/fans", json={"follow_gpu": True, "fans": []}).status_code == 400
    assert client.post("/api/system/fans", json={"follow_gpu": True, "fans": ["zzz"]}).status_code == 400
    assert client.post("/api/system/fans", json={"follow_gpu": True, "fans": ["hwmon3/pwm4"], "curve": [[40, 80], [60, 30]]}).status_code == 400
    ok = client.post("/api/system/fans", json={"follow_gpu": True, "fans": ["hwmon3/pwm4"], "curve": [[40, 30], [60, 60]]})
    assert ok.status_code == 200 and fc.load_config()["follow_gpu"] is True
    assert (hwmon / "hwmon3/pwm4").read_text() == str(round(48 * 255 / 100))     # 52 C on that curve = 30 + 30 * 12/20
    # taking a followed fan by hand ends its following
    client.post("/api/system/fans", json={"fan": "hwmon3/pwm4", "percent": 90})
    assert fc.load_config()["follow_gpu"] is False
    assert client.post("/api/system/fans", json={"follow_gpu": False}).status_code == 200
    fc.stop()
