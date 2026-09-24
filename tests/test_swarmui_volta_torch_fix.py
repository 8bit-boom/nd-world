"""Regression tests for fixing a critical, verified gap in nd-world's V100
guidance: SwarmUI's own first-run install script
(launchtools/comfy-install-linux.sh, confirmed by reading it directly)
installs PyTorch from the CUDA 13 wheel index for any NVIDIA GPU — and
CUDA 13 dropped Volta (compute capability 7.0 / sm_70) support entirely
(confirmed against PyTorch's own migration RFC). A V100 running that
install cannot generate images at all, not just slowly, and a prior
version of this repo's docs claimed the opposite ("should just work").

`cu126` is the last CUDA-12.x wheel index that still includes sm_70 — the
fix is reinstalling torch from there, which fix-swarmui-volta-torch.sh
automates (mirroring the existing install-comfyui-*.sh scripts'
conventions: auto-detect, check before acting, idempotent).

Plain-text/shell-syntax checks, matching this repo's established
convention for compose/doc/script regression coverage (no live SwarmUI
instance to test against in CI) — see test_swarmui_gpu_setup.py,
test_krea2_gguf_node_installer.py."""
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_SCRIPT = _REPO_ROOT / "fix-swarmui-volta-torch.sh"


def test_script_exists_and_is_executable():
    assert _SCRIPT.exists()
    assert _SCRIPT.stat().st_mode & 0o111, "fix-swarmui-volta-torch.sh is not executable"


def test_script_is_valid_shell_syntax():
    result = subprocess.run(["bash", "-n", str(_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_script_checks_before_reinstalling():
    """Idempotent: only touch torch if sm_70 is actually missing from the
    currently installed build's arch list."""
    text = _SCRIPT.read_text()
    assert 'grep -q "sm_70"' in text
    check_idx = text.index('get_arch_list()')
    reinstall_idx = text.index("pip uninstall -y torch")
    assert check_idx < reinstall_idx


def test_script_reinstalls_from_the_cu126_index_not_cu130():
    text = _SCRIPT.read_text()
    assert 'TARGET_INDEX="https://download.pytorch.org/whl/cu126"' in text
    assert "cu130" not in text


def test_script_finds_a_running_swarmui_container_by_name():
    text = _SCRIPT.read_text()
    assert "docker ps --format '{{.Names}}' | grep -i swarmui" in text


def test_gpu_setup_doc_has_the_critical_volta_torch_section():
    text = (_REPO_ROOT / "docs/GPU_SETUP.md").read_text()
    assert "## 3b." in text
    section = text.split("## 3b.", 1)[1].split("## 4.", 1)[0]
    assert "cu130" in section
    assert "cu126" in section
    assert "fix-swarmui-volta-torch.sh" in section
    # The corrected, no-longer-true claim from before this fix, called out
    # explicitly so a reader who saw the old guidance isn't left confused.
    assert "should just work" in section
    # Pinning the image tag does NOT fix this (unlike Ollama) — the single
    # most important, easy-to-miss correction.
    assert "does NOT protect you" in section or "NOT enough" in section


def test_compose_files_no_longer_claim_volta_just_works():
    """The old (wrong) claim lived in comments immediately preceding/inside
    each file's swarmui service — not indented under it in
    docker-compose.gpu.yml, since it's a block comment above the key rather
    than a value under it, so check the whole file rather than trying to
    slice out just the service body."""
    for name in ("docker-compose.gpu.yml", "truenas-compose.yml"):
        text = (_REPO_ROOT / name).read_text()
        assert "should just work" not in text
        assert "fix-swarmui-volta-torch.sh" in text or "§3b" in text


def test_readme_mentions_the_torch_fix_script():
    text = (_REPO_ROOT / "README.md").read_text()
    gpu_section = text.split("### GPU acceleration", 1)[1].split("\n### ", 1)[0]
    assert "fix-swarmui-volta-torch.sh" in gpu_section
