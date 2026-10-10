"""Fan control (app/fan_control.py, /api/system/fans): hwmon PWM read / write against a fake sysfs tree, stable ids, the follow-the-GPU
curve and its safety rules, the "is this output inverted?" check, the hand-back / shutdown rules, and the GM-only routes."""
import os

import pytest

from app import fan_control as fc

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

FAN = "it8688@it87.2656/pwm4"


def _chip(root, dirname, name, device, pwm=128, enable=1, rpm=1450):
    d = root / dirname
    d.mkdir()
    (d / "name").write_text(name + "\n")
    (d / "pwm4").write_text(f"{pwm}\n")
    (d / "pwm4_enable").write_text(f"{enable}\n")
    (d / "fan4_input").write_text(f"{rpm}\n")
    (d / "fan4_label").write_text("GPU fan\n")
    (d / "temp1_input").write_text("40000\n")                       # not a pwm: ignored
    if device:
        os.symlink(f"../../{device}", d / "device")
    return d


@pytest.fixture
def hwmon(tmp_path, monkeypatch):
    _chip(tmp_path, "hwmon3", "it8688", "it87.2656")
    other = tmp_path / "hwmon0"
    other.mkdir()
    (other / "name").write_text("k10temp\n")                           # a chip with no fans
    monkeypatch.setattr(fc, "HWMON_ROOT", str(tmp_path))
    monkeypatch.setattr(fc, "_config_path", lambda: tmp_path / "fan_control.json")
    monkeypatch.setattr(fc, "_boot_id", lambda: "boot-1")
    fc._checked.clear()
    fc._state["v"] = None
    return tmp_path


def _pwm(hwmon, d="hwmon3"):
    return int((hwmon / d / "pwm4").read_text())


# ── identity ─────────────────────────────────────────────────────────────────

def test_lists_only_pwm_outputs_with_a_stable_id(hwmon):
    fans = fc.list_fans()
    assert [(f["id"], f["chip"], f["label"], f["rpm"], f["percent"], f["manual"], f["writable"]) for f in fans] == \
        [(FAN, "it8688", "GPU fan", 1450, 50, True, True)]


def test_the_id_survives_the_kernel_renumbering_hwmon(hwmon):
    """hwmonN is handed out in probe order, so the same chip can be hwmon3 today and hwmon8 tomorrow. A saved choice must still
    point at the same output - and never at a different chip that now sits at the old number."""
    fc.save_config({"follow_gpu": True, "fan_ids": [FAN], "curve": fc.DEFAULT_CURVE})
    (hwmon / "hwmon3").rename(hwmon / "hwmon8")
    _chip(hwmon, "hwmon3", "it8792", "it87.2560", pwm=40, rpm=3000)            # a different chip took the old number
    ids = sorted(f["id"] for f in fc.list_fans())
    assert ids == [FAN, "it8792@it87.2560/pwm4"]
    assert fc.set_percent(FAN, 90)["ok"] and _pwm(hwmon, "hwmon8") == round(90 * 255 / 100) and _pwm(hwmon, "hwmon3") == 40


def test_status_says_which_fans_this_app_holds(hwmon):
    assert fc.status()["fans"][0]["held"] is False                       # manual mode alone is not "held": that is how this BIOS sets it
    fc.set_percent(FAN, 70)
    assert fc.status()["fans"][0]["held"] is True
    fc.give_back(FAN)
    assert fc.status()["fans"][0]["held"] is False


def test_two_chips_with_the_same_name_stay_apart(hwmon):
    _chip(hwmon, "hwmon5", "it8688", "it87.2700")
    assert len({f["id"] for f in fc.list_fans()}) == 2


def test_no_hwmon_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "HWMON_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(fc, "_config_path", lambda: tmp_path / "fan_control.json")
    assert fc.list_fans() == [] and fc.status()["root_exists"] is False


# ── writing ──────────────────────────────────────────────────────────────────

def test_set_percent_writes_manual_mode_and_pwm_with_a_floor(hwmon):
    (hwmon / "hwmon3/pwm4_enable").write_text("2\n")
    assert fc.set_percent(FAN, 60)["ok"]
    assert (hwmon / "hwmon3/pwm4_enable").read_text() == "1" and _pwm(hwmon) == 153
    assert fc.set_percent(FAN, 0)["percent"] == fc.MIN_PERCENT                  # never stopped
    assert _pwm(hwmon) == round(fc.MIN_PERCENT * 255 / 100)
    assert fc.set_percent(FAN, 400)["percent"] == 100


def test_refuses_unknown_fans_and_junk(hwmon):
    assert not fc.set_percent("../../etc/passwd", 50)["ok"]
    assert not fc.set_percent("it8688@it87.2656/pwm9", 50)["ok"]
    assert not fc.set_percent("hwmon3/pwm4", 50)["ok"]                          # the old, unstable id form is no longer accepted
    assert not fc.set_percent(FAN, "fast")["ok"]
    assert not fc.set_percent(FAN, True)["ok"]


def test_give_back_puts_the_bios_values_back_not_a_guess(hwmon):
    (hwmon / "hwmon3/pwm4_enable").write_text("1\n")                    # this board: the BIOS holds a fixed duty in manual mode
    (hwmon / "hwmon3/pwm4").write_text("128\n")
    fc.set_percent(FAN, 90)
    fc.set_percent(FAN, 70)                                              # a second change must not overwrite the remembered original
    assert _pwm(hwmon) == round(70 * 255 / 100)
    assert fc.give_back(FAN)["ok"]
    assert _pwm(hwmon) == 128 and (hwmon / "hwmon3/pwm4_enable").read_text() == "1"
    again = fc.give_back(FAN)                                            # nothing held now: leaves it alone
    assert again["ok"] and "not held" in again["message"] and _pwm(hwmon) == 128


def test_a_note_from_before_a_reboot_is_not_used(hwmon, monkeypatch):
    fc.set_percent(FAN, 90)                                              # remembers the original under boot-1
    monkeypatch.setattr(fc, "_boot_id", lambda: "boot-2")
    (hwmon / "hwmon3/pwm4").write_text("77\n")
    assert "not held" in fc.give_back(FAN)["message"] and (hwmon / "hwmon3/pwm4").read_text().strip() == "77"


def test_set_percent_warns_when_the_chip_does_not_keep_the_value(hwmon, monkeypatch):
    real = fc._write
    def sticky(path, value):                                              # a BIOS that rewrites the duty straight away
        real(path, 128 if path.endswith("/pwm4") else value)
    monkeypatch.setattr(fc, "_write", sticky)
    r = fc.set_percent(FAN, 90)
    assert r["ok"] and r.get("warning") and "BIOS may be rewriting" in r["message"]


def test_a_failed_note_means_the_fan_is_not_touched(hwmon, monkeypatch):
    def boom(_fans):
        raise OSError("read-only file system")
    monkeypatch.setattr(fc, "_save_orig", boom)
    r = fc.set_percent(FAN, 90)
    assert not r["ok"] and "left alone" in r["message"] and _pwm(hwmon) == 128


# ── the curve ────────────────────────────────────────────────────────────────

def test_curve_validation():
    assert fc.clean_curve([[40, 30], [60, 60], [70, 90]]) == [[40, 30], [60, 60], [70, 90]]
    assert fc.clean_curve([[40, 5], [70, 90]]) == [[40, 20], [70, 90]]           # floor applies
    assert fc.clean_curve([[70, 90], [40, 30]]) == [[40, 30], [70, 90]]          # order of entry does not matter
    assert fc.clean_curve([[40, 60], [70, 30]]) is None                          # a hotter GPU may never slow the fan
    assert fc.clean_curve([[40, 30], [40, 90]]) is None
    assert fc.clean_curve([[40, 30]]) is None and fc.clean_curve("x") is None
    assert fc.clean_curve([[10, 30], [70, 90]]) is None and fc.clean_curve([["a", 1], [2, 3]]) is None


def test_a_curve_that_never_gets_the_air_moving_is_refused():
    assert fc.clean_curve([[40, 30], [60, 60]]) is None                          # tops out at 60 %
    assert fc.clean_curve([[40, 30], [90, 100]]) is None                         # reaches 100 % only after the panic temperature
    assert fc.clean_curve([[40, 30], [75, 80]]) == [[40, 30], [75, 80]]          # exactly at the limit is fine
    assert fc.clean_curve(fc.DEFAULT_CURVE) == fc.DEFAULT_CURVE


def test_follow_curve_and_panic_rules():
    curve = [[40, 30], [60, 60], [75, 100]]
    assert fc.percent_for(30, curve) == 30 and fc.percent_for(50, curve) == 45 and fc.percent_for(70, curve) == 60 + round(40 * 10 / 15)
    assert fc.percent_for(77, curve) == 100                                      # above the last point: the last point's value
    assert fc.percent_for(None, curve) == 100                                    # no reading -> full speed
    assert fc.percent_for(fc.PANIC_TEMP, curve) == 100 and fc.percent_for(float("nan"), curve) == 100


def test_what_is_displayed_is_what_runs():
    curve = [[40, 30], [70, 85]]
    assert fc.percent_for(75, curve) == 85                                       # not a jump to 100 beyond the drawn curve


def test_speed_up_at_once_slow_down_gently():
    assert fc.smooth(None, 40) == 40 and fc.smooth(50, 80) == 80 and fc.smooth(50, 50) == 50
    assert fc.smooth(80, 30) == 80 - fc.RAMP_DOWN and fc.smooth(33, 30) == 30
    assert fc.smooth(100, 100) == 100


# ── following ────────────────────────────────────────────────────────────────

CFG = {"follow_gpu": True, "fan_ids": [FAN], "curve": [[40, 30], [60, 60], [75, 100]]}


def test_apply_once_drives_the_chosen_fans(hwmon):
    (hwmon / "hwmon3/pwm4").write_text("60\n")                           # 24 % now
    assert fc.apply_once(50, CFG) == 45 and _pwm(hwmon) == round(45 * 255 / 100)
    assert fc.apply_once(50, {**CFG, "follow_gpu": False}) is None
    st = fc._state["v"]
    assert st["gpu_temp"] == 50 and st["target"] == 45 and st["applied"] == {FAN: 45} and st["errors"] == []


def test_a_cooling_gpu_slows_the_fan_in_steps_and_a_hot_one_speeds_it_at_once(hwmon):
    (hwmon / "hwmon3/pwm4").write_text(str(round(80 * 255 / 100)) + "\n")
    fc.apply_once(40, CFG)                                               # target 30 %: only 5 points down this tick
    assert round(_pwm(hwmon) * 100 / 255) == 80 - fc.RAMP_DOWN
    fc.apply_once(74, CFG)                                               # hot: straight up
    assert round(_pwm(hwmon) * 100 / 255) == fc.percent_for(74, CFG["curve"]) == 97              # 60 + 40 * 14/15


def test_a_steady_gpu_does_not_keep_rewriting_the_chip(hwmon, monkeypatch):
    fc.apply_once(50, CFG)
    writes = []
    real = fc._write
    monkeypatch.setattr(fc, "_write", lambda p, v: (writes.append(p), real(p, v)))
    fc.apply_once(50, CFG)
    assert writes == []


def test_an_unreadable_temperature_means_full_speed(hwmon):
    fc.apply_once(None, CFG)
    assert _pwm(hwmon) == 255


def test_a_saved_fan_that_is_not_there_is_reported_not_guessed(hwmon):
    fc.apply_once(50, {**CFG, "fan_ids": ["it8688@it87.9999/pwm1"]})
    assert "not found" in fc._state["v"]["errors"][0] and _pwm(hwmon) == 128


# ── the inverted-output check ────────────────────────────────────────────────

def _fake_fan(hwmon, ratio_for_full, d="hwmon3"):
    """A sleep() that lets the 'fan' react: at full duty its rpm becomes before * ratio_for_full."""
    def sleep(_s):
        if int((hwmon / d / "pwm4").read_text()) == 255:
            (hwmon / d / "fan4_input").write_text(str(int(1450 * ratio_for_full)))
    return sleep


def test_check_reports_a_fan_that_speeds_up_and_puts_everything_back(hwmon):
    (hwmon / "hwmon3/pwm4").write_text("100\n")
    r = fc.check_fan(FAN, sleep=_fake_fan(hwmon, 1.6))
    assert r["ok"] and r["verdict"] == "speeds_up" and r["rpm_before"] == 1450
    assert _pwm(hwmon) == 100 and (hwmon / "hwmon3/pwm4_enable").read_text().strip() == "1"
    assert fc.verdict(FAN) == "speeds_up"


def test_check_flags_an_inverted_output(hwmon):
    (hwmon / "hwmon3/pwm4").write_text("100\n")
    r = fc.check_fan(FAN, sleep=_fake_fan(hwmon, 0.6))
    assert r["verdict"] == "inverted" and "INVERTED" not in r["message"] and "inverted" in r["message"]
    assert fc.verdict(FAN) == "inverted"


def test_check_says_when_nothing_changed_or_there_is_no_signal(hwmon):
    (hwmon / "hwmon3/pwm4").write_text("100\n")
    assert fc.check_fan(FAN, sleep=_fake_fan(hwmon, 1.0))["verdict"] == "no_change"
    (hwmon / "hwmon3/fan4_input").write_text("0\n")
    assert fc.check_fan(FAN, sleep=lambda s: None)["verdict"] == "no_signal"
    (hwmon / "hwmon3/fan4_input").unlink()
    assert fc.check_fan(FAN, sleep=lambda s: None)["verdict"] == "no_signal"


def test_check_never_runs_a_fan_that_is_already_at_full_or_is_being_followed(hwmon):
    (hwmon / "hwmon3/pwm4").write_text("255\n")
    assert not fc.check_fan(FAN, sleep=lambda s: None)["ok"]
    (hwmon / "hwmon3/pwm4").write_text("100\n")
    fc.save_config(CFG)
    assert "following" in fc.check_fan(FAN, sleep=lambda s: None)["message"]


def test_check_keeps_a_hold_a_hold(hwmon):
    fc.set_percent(FAN, 50)
    fc.check_fan(FAN, sleep=_fake_fan(hwmon, 1.5))
    assert _pwm(hwmon) == round(50 * 255 / 100)
    assert fc.give_back(FAN)["message"] == "Put back as the BIOS had it." and _pwm(hwmon) == 128      # the BIOS value, not the hold


# ── stopping ─────────────────────────────────────────────────────────────────

def test_shutdown_raises_a_slow_held_fan_and_never_lowers_one(hwmon):
    fc.set_percent(FAN, 30)
    fc.shutdown_safe()
    assert round(_pwm(hwmon) * 100 / 255) == fc.SHUTDOWN_PERCENT
    fc.set_percent(FAN, 90)
    fc.shutdown_safe()
    assert round(_pwm(hwmon) * 100 / 255) == 90


def test_shutdown_leaves_fans_it_never_touched_alone(hwmon):
    fc.shutdown_safe()
    assert _pwm(hwmon) == 128


# ── the routes ───────────────────────────────────────────────────────────────

def _gm(client, seed):
    client.cookies.clear()
    login(client, seed.gm.email, GM_PASSWORD)


def test_routes_are_gm_only(client, seed, hwmon):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/system/fans").status_code == 403
    assert client.post("/api/system/fans", json={"fan": FAN, "percent": 50}).status_code == 403
    assert client.post("/api/system/fans/check", json={"fan": FAN}).status_code == 403


def test_hold_auto_and_status(client, seed, hwmon):
    _gm(client, seed)
    d = client.get("/api/system/fans").json()
    assert d["fans"][0]["id"] == FAN and d["min_percent"] == 20 and d["follow_gpu"] is False and d["missing"] == [] and d["any_writable"] is True
    assert client.post("/api/system/fans", json={"fan": FAN, "percent": 55}).json()["ok"] is True
    assert client.post("/api/system/fans", json={"fan": "nope", "percent": 55}).status_code == 400
    assert client.post("/api/system/fans", json={"fan": FAN, "auto": True}).json()["ok"] is True
    assert _pwm(hwmon) == 128


def test_follow_validation_save_hand_back_and_inverted_refusal(client, seed, hwmon, monkeypatch):
    monkeypatch.setattr(fc, "gpu_temperature", lambda: 52.0)
    _gm(client, seed)
    post = lambda **b: client.post("/api/system/fans", json=b)
    assert post(follow_gpu=True, fans=[]).status_code == 400
    assert post(follow_gpu=True, fans=["zzz"]).status_code == 400
    assert post(follow_gpu=True, fans=[FAN], curve=[[40, 80], [60, 30]]).status_code == 400            # slows when hotter
    assert post(follow_gpu=True, fans=[FAN], curve=[[40, 30], [60, 60]]).status_code == 400            # never reaches 80 %
    ok = post(follow_gpu=True, fans=[FAN], curve=[[40, 30], [60, 60], [72, 100]])
    assert ok.status_code == 200 and fc.load_config()["follow_gpu"] is True
    assert _pwm(hwmon) == round((30 + 30 * 12 / 20) * 255 / 100)                                       # 52 C on that curve = 48 %
    assert client.get("/api/system/fans").json()["state"]["target"] == 48
    # taking a followed fan by hand ends its following
    r = post(fan=FAN, percent=90).json()
    assert r["ok"] and "no longer follows" in r["message"] and fc.load_config()["follow_gpu"] is False
    # follow again, then switch it off: the fan goes back as the BIOS had it
    post(follow_gpu=True, fans=[FAN], curve=[[40, 30], [60, 60], [72, 100]])
    off = post(follow_gpu=False)
    assert off.status_code == 200 and "Put back" in off.json()["message"] and _pwm(hwmon) == 128
    # an output the check found inverted may not follow the GPU
    fc._checked[FAN] = {"verdict": "inverted", "t": 0}
    assert post(follow_gpu=True, fans=[FAN]).status_code == 400
    fc.stop()


def test_following_can_be_switched_off_when_the_driver_is_not_loaded(client, seed, hwmon, monkeypatch):
    monkeypatch.setattr(fc, "gpu_temperature", lambda: 52.0)
    _gm(client, seed)
    assert client.post("/api/system/fans", json={"follow_gpu": True, "fans": [FAN]}).status_code == 200
    (hwmon / "hwmon3" / "pwm4").unlink()                                   # the chip is gone (driver unloaded / reboot)
    assert fc.list_fans() == []
    off = client.post("/api/system/fans", json={"follow_gpu": False})
    assert off.status_code == 200 and fc.load_config()["follow_gpu"] is False
    fc.stop()


def test_a_refused_hold_leaves_the_fan_following(client, seed, hwmon, monkeypatch):
    monkeypatch.setattr(fc, "gpu_temperature", lambda: 52.0)
    _gm(client, seed)
    client.post("/api/system/fans", json={"follow_gpu": True, "fans": [FAN]})
    real = fc._write
    def refuse(path, value):
        if path.endswith("/pwm4"):
            raise OSError("Permission denied")
        real(path, value)
    monkeypatch.setattr(fc, "_write", refuse)
    r = client.post("/api/system/fans", json={"fan": FAN, "percent": 90})
    assert r.status_code == 400 and fc.load_config()["follow_gpu"] is True and fc.load_config()["fan_ids"] == [FAN]
    fc.stop()


def test_check_route(client, seed, hwmon, monkeypatch):
    monkeypatch.setattr(fc, "CHECK_SECONDS", 0)
    _gm(client, seed)
    (hwmon / "hwmon3/pwm4").write_text("100\n")
    r = client.post("/api/system/fans/check", json={"fan": FAN})
    assert r.status_code == 200 and r.json()["verdict"] == "no_change" and _pwm(hwmon) == 100          # nothing reacts in a fake tree
    assert client.post("/api/system/fans/check", json={"fan": "nope"}).status_code == 400
    assert client.post("/api/system/fans/check", json={}).status_code == 400
