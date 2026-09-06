"""Tests for app/ollama_tuning.py's detect_hardware() — CPU/RAM/GPU
detection for the "Detected hardware" panel on Settings > System. Every
sub-detector is exercised independently by monkeypatching its I/O (subprocess
calls, /proc reads, /sys globs) rather than depending on this test machine's
real hardware, since CI/dev sandboxes vary (no GPU, no nvidia-smi, etc.).
"""
import asyncio
import types

import pytest

from app import ai as ai_module
from app import ollama_tuning as tuning


_CPUINFO_8_CORE = "\n\n".join(
    f"processor\t: {i}\nmodel name\t: AMD Ryzen 9 5900X 12-Core Processor\n"
    for i in range(8)
)
_MEMINFO_SAMPLE = "MemTotal:       65850000 kB\nMemAvailable:   50000000 kB\nSwapTotal:              0 kB\n"


# ── CPU / RAM (via /proc) ────────────────────────────────────────────────────

def test_cpu_and_ram_from_proc(tmp_path):
    cpuinfo_path = tmp_path / "cpuinfo"
    cpuinfo_path.write_text(_CPUINFO_8_CORE)
    meminfo_path = tmp_path / "meminfo"
    meminfo_path.write_text(_MEMINFO_SAMPLE)

    model, cores = tuning._read_proc_cpuinfo(cpuinfo_path)
    total, available = tuning._read_proc_meminfo(meminfo_path)
    assert model == "AMD Ryzen 9 5900X 12-Core Processor"
    assert cores == 8
    assert total == 65850000 // 1024
    assert available == 50000000 // 1024


def test_missing_proc_files_are_none_not_crash(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert tuning._read_proc_cpuinfo(missing) == ("", None)
    assert tuning._read_proc_meminfo(missing) == (None, None)


def test_proc_cpuinfo_and_meminfo_do_not_raise_on_this_machine():
    """Smoke test against the REAL /proc — must never raise regardless of
    what this sandbox's hardware looks like."""
    model, cores = tuning._read_proc_cpuinfo()
    assert isinstance(model, str)
    assert cores is None or isinstance(cores, int)
    total, available = tuning._read_proc_meminfo()
    assert total is None or isinstance(total, int)
    assert available is None or isinstance(available, int)


# ── NVIDIA (nvidia-smi subprocess) ──────────────────────────────────────────

class _FakeCompletedProc:
    def __init__(self, stdout=b"", returncode=0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""

    def kill(self):
        pass


@pytest.mark.asyncio
async def test_nvidia_smi_parsed(monkeypatch):
    monkeypatch.setattr(tuning.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    async def fake_exec(*args, **kwargs):
        return _FakeCompletedProc(stdout=b"NVIDIA GeForce RTX 4090, 24564\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    gpus = await tuning._detect_nvidia_gpus()
    assert gpus == [{"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090", "vram_mb": 24564}]


@pytest.mark.asyncio
async def test_nvidia_smi_multiple_gpus(monkeypatch):
    monkeypatch.setattr(tuning.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    async def fake_exec(*args, **kwargs):
        return _FakeCompletedProc(stdout=b"NVIDIA GeForce RTX 4090, 24564\nNVIDIA GeForce RTX 3090, 24576\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    gpus = await tuning._detect_nvidia_gpus()
    assert len(gpus) == 2
    assert sum(g["vram_mb"] for g in gpus) == 24564 + 24576


@pytest.mark.asyncio
async def test_nvidia_smi_absent_falls_through(monkeypatch):
    monkeypatch.setattr(tuning.shutil, "which", lambda name: None)
    gpus = await tuning._detect_nvidia_gpus()
    assert gpus == []


@pytest.mark.asyncio
async def test_nvidia_smi_nonzero_exit_yields_nothing(monkeypatch):
    monkeypatch.setattr(tuning.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    async def fake_exec(*args, **kwargs):
        return _FakeCompletedProc(stdout=b"", returncode=1)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await tuning._detect_nvidia_gpus() == []


@pytest.mark.asyncio
async def test_nvidia_smi_timeout_is_not_fatal(monkeypatch):
    monkeypatch.setattr(tuning.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    class _HangingProc(_FakeCompletedProc):
        async def communicate(self):
            raise asyncio.TimeoutError()

    async def fake_exec(*args, **kwargs):
        return _HangingProc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await tuning._detect_nvidia_gpus() == []


@pytest.mark.asyncio
async def test_nvidia_smi_subprocess_error_is_not_fatal(monkeypatch):
    monkeypatch.setattr(tuning.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    async def fake_exec(*args, **kwargs):
        raise OSError("no such device")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await tuning._detect_nvidia_gpus() == []


# ── AMD (sysfs) ──────────────────────────────────────────────────────────────

def _make_amd_card(tmp_path, card_name, vendor="0x1002", vram_bytes=17179869184):
    card = tmp_path / "sys" / "class" / "drm" / card_name / "device"
    card.mkdir(parents=True)
    (card / "vendor").write_text(vendor)
    (card / "mem_info_vram_total").write_text(str(vram_bytes))
    return card


def test_amd_sysfs_vram(tmp_path):
    _make_amd_card(tmp_path, "card0")
    pattern = str(tmp_path / "sys" / "class" / "drm" / "card*" / "device" / "mem_info_vram_total")

    gpus = tuning._detect_amd_gpus(pattern)
    assert gpus == [{"vendor": "amd", "name": "AMD GPU", "vram_mb": 16384}]


def test_amd_sysfs_ignores_non_amd_vendor(tmp_path):
    _make_amd_card(tmp_path, "card0", vendor="0x10de")  # NVIDIA's PCI vendor id
    pattern = str(tmp_path / "sys" / "class" / "drm" / "card*" / "device" / "mem_info_vram_total")

    assert tuning._detect_amd_gpus(pattern) == []


def test_amd_sysfs_multiple_cards_summed_by_caller(tmp_path):
    _make_amd_card(tmp_path, "card0", vram_bytes=17179869184)
    _make_amd_card(tmp_path, "card1", vram_bytes=8589934592)
    pattern = str(tmp_path / "sys" / "class" / "drm" / "card*" / "device" / "mem_info_vram_total")

    gpus = tuning._detect_amd_gpus(pattern)
    assert len(gpus) == 2
    assert sum(g["vram_mb"] for g in gpus) == 16384 + 8192


def test_amd_sysfs_no_cards_found(tmp_path):
    pattern = str(tmp_path / "sys" / "class" / "drm" / "card*" / "device" / "mem_info_vram_total")
    assert tuning._detect_amd_gpus(pattern) == []


# ── detect_hardware() end-to-end resolution order ───────────────────────────

@pytest.mark.asyncio
async def test_manual_override_wins_over_detection(monkeypatch):
    async def fake_nvidia():
        return [{"vendor": "nvidia", "name": "Should Not Be Used", "vram_mb": 8192}]
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)

    hw = await tuning.detect_hardware(vram_override_mb=12000)
    assert hw["vram_total_mb"] == 12000
    assert hw["vram_source"] == "manual"


@pytest.mark.asyncio
async def test_nvidia_detection_used_when_no_override(monkeypatch):
    async def fake_nvidia():
        return [{"vendor": "nvidia", "name": "RTX 4090", "vram_mb": 24564}]
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)

    hw = await tuning.detect_hardware()
    assert hw["vram_total_mb"] == 24564
    assert hw["vram_source"] == "nvidia-smi"
    assert hw["vram_is_lower_bound"] is False


@pytest.mark.asyncio
async def test_amd_used_when_no_nvidia(monkeypatch):
    async def fake_nvidia():
        return []
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)
    monkeypatch.setattr(tuning, "_detect_amd_gpus", lambda: [{"vendor": "amd", "name": "AMD GPU", "vram_mb": 16384}])

    hw = await tuning.detect_hardware()
    assert hw["vram_total_mb"] == 16384
    assert hw["vram_source"] == "amd-sysfs"


@pytest.mark.asyncio
async def test_ollama_ps_lower_bound_when_nothing_else(monkeypatch):
    async def fake_nvidia():
        return []
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)
    monkeypatch.setattr(tuning, "_detect_amd_gpus", lambda: [])

    async def fake_resident():
        return [{"model": "gemma3:9b", "size_bytes": 9_000_000_000, "size_vram_bytes": 8_500_000_000}]
    monkeypatch.setattr(ai_module, "resident_models", fake_resident)

    hw = await tuning.detect_hardware()
    assert hw["vram_source"] == "ollama-ps"
    assert hw["vram_is_lower_bound"] is True
    assert hw["vram_total_mb"] == 8_500_000_000 // (1024 * 1024)
    assert any("lower bound" in n.lower() or "loaded" in n.lower() for n in hw["notes"])


@pytest.mark.asyncio
async def test_ollama_ps_failure_is_not_fatal(monkeypatch):
    async def fake_nvidia():
        return []
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)
    monkeypatch.setattr(tuning, "_detect_amd_gpus", lambda: [])

    async def fake_resident():
        raise ConnectionError("ollama unreachable")
    monkeypatch.setattr(ai_module, "resident_models", fake_resident)

    hw = await tuning.detect_hardware()
    assert hw["vram_total_mb"] is None
    assert hw["vram_source"] == "none"


@pytest.mark.asyncio
async def test_no_gpu_yields_none_and_a_note(monkeypatch):
    async def fake_nvidia():
        return []
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)
    monkeypatch.setattr(tuning, "_detect_amd_gpus", lambda: [])

    async def fake_resident():
        return []
    monkeypatch.setattr(ai_module, "resident_models", fake_resident)

    hw = await tuning.detect_hardware()
    assert hw["vram_total_mb"] is None
    assert hw["vram_source"] == "none"
    assert hw["notes"]


@pytest.mark.asyncio
async def test_detect_hardware_never_raises_on_this_real_machine():
    """Full integration smoke test with no monkeypatching — must complete
    without raising on whatever this sandbox's real hardware looks like."""
    hw = await tuning.detect_hardware()
    assert isinstance(hw["gpus"], list)
    assert isinstance(hw["notes"], list)


# ── GPU presets (plan settings for a card not physically installed yet) ────

@pytest.mark.asyncio
async def test_gpu_preset_used_when_no_real_gpu_detected(monkeypatch):
    async def fake_nvidia():
        return []
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)
    monkeypatch.setattr(tuning, "_detect_amd_gpus", lambda: [])

    hw = await tuning.detect_hardware(gpu_preset="v100_16gb")
    assert hw["vram_total_mb"] == 16384
    assert hw["vram_source"] == "preset"
    assert hw["gpus"] == [{"vendor": "nvidia", "name": "NVIDIA Tesla V100 16GB", "vram_mb": 16384}]
    assert any("simulating" in n.lower() for n in hw["notes"])


@pytest.mark.asyncio
async def test_manual_override_wins_over_preset(monkeypatch):
    async def fake_nvidia():
        return []
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)
    monkeypatch.setattr(tuning, "_detect_amd_gpus", lambda: [])

    hw = await tuning.detect_hardware(vram_override_mb=12000, gpu_preset="v100_16gb")
    assert hw["vram_total_mb"] == 12000
    assert hw["vram_source"] == "manual"


@pytest.mark.asyncio
async def test_real_detection_wins_over_preset(monkeypatch):
    async def fake_nvidia():
        return [{"vendor": "nvidia", "name": "RTX 4090", "vram_mb": 24564}]
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)

    hw = await tuning.detect_hardware(gpu_preset="v100_16gb")
    assert hw["vram_total_mb"] == 24564
    assert hw["vram_source"] == "nvidia-smi"


@pytest.mark.asyncio
async def test_unknown_preset_key_is_ignored(monkeypatch):
    async def fake_nvidia():
        return []
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)
    monkeypatch.setattr(tuning, "_detect_amd_gpus", lambda: [])

    async def fake_resident():
        return []
    monkeypatch.setattr(ai_module, "resident_models", fake_resident)

    hw = await tuning.detect_hardware(gpu_preset="not-a-real-preset")
    assert hw["vram_total_mb"] is None
    assert hw["vram_source"] == "none"


@pytest.mark.asyncio
async def test_v100_preset_triggers_volta_advisory_note(monkeypatch):
    """The preset's synthesized `gpus` entry must be indistinguishable from
    a real nvidia-smi reading as far as recommend_settings' own
    architecture-aware advice is concerned — a GM planning for a V100
    should see the same Volta/CUDA-13 note a physically-installed one
    would produce."""
    async def fake_nvidia():
        return []
    monkeypatch.setattr(tuning, "_detect_nvidia_gpus", fake_nvidia)
    monkeypatch.setattr(tuning, "_detect_amd_gpus", lambda: [])

    hw = await tuning.detect_hardware(gpu_preset="v100_16gb")
    note = tuning._volta_note(hw)
    assert note is not None
    assert "volta" in note.lower()


# ── GET /api/ai/hardware route ──────────────────────────────────────────────
# (route itself is added in a later task; these are placeholders removed
# once that route exists — see tests/test_ollama_options.py-style route
# tests added alongside the route implementation instead, to avoid this
# file depending on code that doesn't exist yet.)
