"""Regression tests for install-comfyui-gguf-krea2.sh — the standalone
script that closes the one gap the "Krea 2 GGUF Quick Setup" panel
(tests/test_krea2_gguf_quick_setup.py) documents but can't do itself:
nd-world's own container has no filesystem access to SwarmUI's
custom_nodes folder, so installing the RealRebelAI/ComfyUI-GGUF_KREA-2
fork (needed because upstream city96/ComfyUI-GGUF doesn't parse Krea 2's
ops — city96/ComfyUI-GGUF#464) has to happen as a script run directly on
the SwarmUI host, mirroring install-comfyui-manager.sh's existing
structure.

These are plain-text/shell-syntax checks, not a real install (no SwarmUI
instance to install against in CI) — matching this repo's existing
convention for compose/doc-file regression coverage (see
test_job_shutdown.py, test_swarmui_gpu_setup.py)."""
import os
import subprocess
from pathlib import Path

import pytest

from .conftest import GM_PASSWORD, login

_REPO_ROOT = Path(__file__).parent.parent
_SCRIPT = _REPO_ROOT / "install-comfyui-gguf-krea2.sh"


# The +x bit lives in the git index; POSIX filesystems materialize it on
# checkout, NTFS cannot. CI (docker-publish, Ubuntu) and the SwarmUI host
# where this script actually runs are POSIX — a Windows dev checkout is not.
@pytest.mark.skipif(os.name == "nt", reason="NTFS cannot materialize the git +x mode bit")
def test_script_exists_and_is_executable():
    assert _SCRIPT.exists()
    assert _SCRIPT.stat().st_mode & 0o111, "install-comfyui-gguf-krea2.sh is not executable"


def test_script_is_valid_shell_syntax():
    result = subprocess.run(["bash", "-n", str(_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_script_clones_the_correct_fork():
    text = _SCRIPT.read_text()
    assert "RealRebelAI/ComfyUI-GGUF_KREA-2" in text
    assert 'git clone "$GGUF_REPO"' in text


def test_script_removes_the_conflicting_upstream_node_before_cloning():
    """The whole reason this can't just be install-comfyui-manager.sh
    pointed at a different repo: the fork shares node/class names with
    city96/ComfyUI-GGUF, which SwarmUI may have already auto-installed —
    leaving both in custom_nodes would register duplicate node classes."""
    text = _SCRIPT.read_text()
    assert 'UPSTREAM_NODE_NAME="ComfyUI-GGUF"' in text
    # Backs it up (mv), doesn't delete it outright.
    assert 'mv "$UPSTREAM_DIR" "$BACKUP_DIR"' in text
    # The removal must happen before the clone, not after — scoped to the
    # host-filesystem fallback path (install_via_docker() has its own
    # earlier clone/mv pair, checked separately in
    # test_comfyui_node_installers_docker_exec.py).
    fallback_text = text.split("Fall back: host filesystem search", 1)[1]
    remove_idx = fallback_text.index('mv "$UPSTREAM_DIR"')
    clone_idx = fallback_text.index('git clone "$GGUF_REPO"')
    assert remove_idx < clone_idx


def test_script_mirrors_the_manager_installers_swarmui_detection():
    """Same SwarmUI-path auto-detection as install-comfyui-manager.sh —
    not a from-scratch reimplementation with different search paths."""
    manager_text = (_REPO_ROOT / "install-comfyui-manager.sh").read_text()
    krea2_text = _SCRIPT.read_text()
    assert "dlbackend/ComfyUI" in manager_text
    assert "dlbackend/ComfyUI" in krea2_text
    assert "find_swarmui()" in krea2_text


# ── Docs/UI cross-references to the new script ───────────────────────────────

def test_krea2_template_note_references_the_installer_script():
    from app.imagegen_templates import TEMPLATES
    krea2 = next(t for t in TEMPLATES if t["id"] == "krea2")
    assert "install-comfyui-gguf-krea2.sh" in krea2["note"]


def test_image_gen_tab_note_references_the_installer_script(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    assert "install-comfyui-gguf-krea2.sh" in r.text


def test_gpu_setup_doc_has_a_swarmui_performance_section():
    text = (_REPO_ROOT / "docs/GPU_SETUP.md").read_text()
    assert "## 5a." in text
    section = text.split("## 5a.", 1)[1].split("## 6.", 1)[0]
    assert "install-comfyui-gguf-krea2.sh" in section
    assert "FP16" in section
