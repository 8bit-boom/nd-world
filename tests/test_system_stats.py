"""System Monitor (app/system_stats.py, app/routers/system_monitor.py): GPU / CPU / RAM readings.

The parsers get hostile and awkward input (nvidia-smi prints "[N/A]", a missing card, garbage; /proc files can be empty), the GPU
reading says WHY it is unavailable instead of failing, nothing is run through a shell, and only the GM can read it."""
import subprocess

import pytest

from app import system_stats as S

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

SMI = ("0, Tesla T10, GPU-aaaa-1111, 5, 1, 0, 16384, 63, 40.12, 150.00, [N/A], 585, 1590, P0, 60.00, 150.00, 150.00\n"
       "1, NVIDIA GeForce GT 1030, GPU-bbbb-2222, [Not Supported], [N/A], 120, 2048, 41, [N/A], [N/A], 30, 300, 1500, P8, [N/A], [N/A], [N/A]\n")


def test_nvidia_smi_output_becomes_numbers_and_na_becomes_none():
    g = S.parse_gpus(SMI)
    assert [x["name"] for x in g] == ["Tesla T10", "NVIDIA GeForce GT 1030"]
    t10 = g[0]
    assert (t10["util_percent"], t10["mem_used_mib"], t10["mem_total_mib"], t10["temp_c"], t10["power_w"], t10["power_limit_w"]) == (5.0, 0.0, 16384.0, 63.0, 40.12, 150.0)
    assert t10["fan_percent"] is None and t10["pstate"] == "P0"
    assert (t10["power_min_w"], t10["power_max_w"], t10["power_default_w"]) == (60.0, 150.0, 150.0)
    gt = g[1]
    assert gt["util_percent"] is None and gt["power_w"] is None and gt["fan_percent"] == 30.0


@pytest.mark.parametrize("junk", ["", "garbage", "1,2,3", "\x00\x01\n,,,,", "a, b, c, d, e, f, g, h, i, j, k, l, m, n, o, p, q\n", "0, x, u, nan, inf, -1, 1e999, 5, 6, 7, 8, 9, 10, Zzz, 1, 2, 3"])
def test_hostile_gpu_output_never_raises_or_leaks_non_numbers(junk):
    for g in S.parse_gpus(junk):
        for k in ("util_percent", "mem_used_mib", "mem_total_mib", "temp_c", "power_w", "power_limit_w", "clock_mhz"):
            v = g[k]
            assert v is None or (isinstance(v, float) and v == v and abs(v) < 1e12)
        assert g["pstate"] in (None, ) or str(g["pstate"]).startswith("P")


def test_gpu_apps_are_attached_to_their_card_with_a_clean_name():
    apps = S.parse_gpu_apps("123, /usr/bin/llama-server, 11264, GPU-aaaa-1111\n9, , [N/A], GPU-aaaa-1111\nx, y\n")
    assert apps == [{"pid": 123, "name": "llama-server", "mem_mib": 11264.0, "gpu_uuid": "GPU-aaaa-1111"}]


def _fake_run(outputs):
    def run(args, timeout=4.0):
        assert isinstance(args, list) and "nvidia-smi" in args[0]                      # a fixed argument list, never a shell string
        key = "apps" if any("compute-apps" in a for a in args) else "gpu"
        return outputs[key]
    return run


def test_gpu_stats_end_to_end_with_a_process_list_and_the_uuid_hidden():
    r = S.gpu_stats(run=_fake_run({"gpu": (0, SMI, ""), "apps": (0, "77, /opt/llama-server, 9000, GPU-aaaa-1111\n", "")}), which=lambda n: "/usr/bin/nvidia-smi")
    assert r["available"] is True and len(r["devices"]) == 2
    assert r["devices"][0]["processes"] == [{"pid": 77, "name": "llama-server", "mem_mib": 9000.0}]
    assert all("uuid" not in d for d in r["devices"])


def test_a_container_without_the_gpu_says_how_to_fix_it():
    r = S.gpu_stats(which=lambda n: None)
    assert r["available"] is False and "docs/GPU_SETUP.md" in r["reason"] and r["devices"] == []


def test_nvidia_smi_failures_are_reported_not_raised():
    bad = S.gpu_stats(run=_fake_run({"gpu": (9, "", "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\x07"), "apps": (0, "", "")}), which=lambda n: "/x/nvidia-smi")
    assert bad["available"] is False and "failed" in bad["reason"] and "\x07" not in bad["reason"]

    def boom(args, timeout=4.0):
        raise subprocess.TimeoutExpired(args, timeout)
    t = S.gpu_stats(run=boom, which=lambda n: "/x/nvidia-smi")
    assert t["available"] is False and "could not be run" in t["reason"]
    empty = S.gpu_stats(run=_fake_run({"gpu": (0, "\n", ""), "apps": (0, "", "")}), which=lambda n: "/x/nvidia-smi")
    assert empty["available"] is False


def test_cpu_percent_comes_from_two_readings():
    a, _ = S.parse_cpu_times("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\n")
    b, cores = S.parse_cpu_times("cpu  200 0 200 750 150 0 0 0 0 0\ncpu0 100 0 100 375 75 0 0 0 0 0\n")
    assert a == [1000, 800] and len(cores) == 1
    assert S.cpu_percent(a, b) == 66.7            # 300 more ticks, 100 of them idle: 200 busy of 300
    assert S.cpu_percent(a, a) is None and S.cpu_percent(None, b) is None and S.cpu_percent([1, 1], [0, 0]) is None
    assert S.parse_cpu_times("") == (None, []) and S.parse_cpu_times("cpu x y z")[0] is None


def test_meminfo_parses_and_used_excludes_what_the_kernel_can_give_back():
    m = S.parse_meminfo("MemTotal:       16000000 kB\nMemFree:  1000000 kB\nMemAvailable:   12000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\nbroken line\n")
    assert m["MemTotal"] == 16000000 * 1024 and m["MemAvailable"] == 12000000 * 1024
    assert S.parse_meminfo("") == {} and S.parse_meminfo("MemTotal: lots kB") == {}


def test_live_snapshot_has_the_shape_the_page_reads():
    d = S.snapshot()
    assert set(d) >= {"time", "cpu", "memory", "gpu"}
    assert d["memory"]["total"] > 0 and 0 <= d["memory"]["percent"] <= 100
    assert d["cpu"]["cores"] >= 1 and (d["cpu"]["percent"] is None or 0 <= d["cpu"]["percent"] <= 100)
    assert isinstance(d["gpu"]["available"], bool) and isinstance(d["gpu"]["devices"], list)
    assert S.snapshot() is d                                   # cached for a second: many open pages cost one reading


def test_only_the_gm_can_see_the_monitor(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/system/stats").status_code in (401, 403)
    assert client.get("/system").status_code in (401, 403)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/system/stats")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and "memory" in r.json()
    page = client.get("/system")
    assert page.status_code == 200 and "System Monitor" in page.text and "sm-gpus" in page.text


# ── setting the power limit ──────────────────────────────────────────────────

class _Smi:
    """A fake nvidia-smi that records every command it was asked to run."""
    def __init__(self, set_result=(0, "Power limit for GPU 00000000:09:00.0 was set to 100.00 W from 150.00 W.", "")):
        self.calls, self.set_result = [], set_result

    def __call__(self, args, timeout=4.0):
        self.calls.append(list(args))
        if "-pl" in args:
            return self.set_result
        return (0, "", "") if any("compute-apps" in a for a in args) else (0, SMI, "")


def test_power_limit_is_set_inside_the_cards_range_with_a_fixed_command():
    smi = _Smi()
    r = S.set_power_limit(0, 100, run=smi, which=lambda n: "/usr/bin/nvidia-smi")
    assert r["ok"] is True and "100 W" in r["message"]
    assert ["/usr/bin/nvidia-smi", "-i", "0", "-pl", "100"] in smi.calls


@pytest.mark.parametrize("index,watts", [(0, 59), (0, 151), (0, -5), (0, 0), (0, 1e9), (0, 99.5), (0, float("nan")), (0, True), (0, "100"), (0, None),
                                         (5, 100), ("0", 100), (None, 100), (True, 100), (0, [100]), (0, {"w": 1})])
def test_bad_power_limits_are_refused_before_any_command_runs(index, watts):
    smi = _Smi()
    r = S.set_power_limit(index, watts, run=smi, which=lambda n: "/usr/bin/nvidia-smi")
    assert r["ok"] is False and r["message"]
    assert not any("-pl" in c for c in smi.calls)


def test_a_card_without_a_known_range_is_never_set():
    smi = _Smi()
    r = S.set_power_limit(1, 50, run=smi, which=lambda n: "/usr/bin/nvidia-smi")        # the GT 1030 line reports [N/A] limits
    assert r["ok"] is False and "range" in r["message"] and not any("-pl" in c for c in smi.calls)


def test_a_permission_refusal_explains_the_host_side_fix():
    smi = _Smi((4, "", "Changing power management limit is not allowed: Insufficient Permissions"))
    r = S.set_power_limit(0, 100, run=smi, which=lambda n: "/usr/bin/nvidia-smi")
    assert r["ok"] is False and "sudo nvidia-smi -i 0 -pl 100" in r["message"] and "Init/Shutdown" in r["message"]


def test_power_limit_route_is_gm_only_and_validates_the_body(client, seed, monkeypatch):
    seen = []
    monkeypatch.setattr(S, "set_power_limit", lambda i, w: (seen.append((i, w)) or {"ok": True, "message": "done"}))
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/api/system/gpu-power", json={"index": 0, "watts": 100}).status_code in (401, 403) and seen == []
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/api/system/gpu-power", json={"index": 0, "watts": 100}).json() == {"ok": True, "message": "done"} and seen == [(0, 100)]
    assert client.post("/api/system/gpu-power", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/system/gpu-power", json=[1, 2]).status_code == 400
    monkeypatch.setattr(S, "set_power_limit", lambda i, w: {"ok": False, "message": "no"})
    assert client.post("/api/system/gpu-power", json={"index": 0, "watts": 1}).status_code == 400
