"""Regression tests for the shared Docker-detection fix in
install-comfyui-manager.sh and install-comfyui-gguf-krea2.sh.

Both scripts install a custom node into SwarmUI's bundled ComfyUI backend.
The normal deployment for this repo runs SwarmUI in a Docker container
(docker-compose.yml / truenas-compose.yml), where the ComfyUI venv is
created with a Python interpreter whose absolute path only exists inside
that container — running the venv's pip from a bind-mounted host path
fails with "bad interpreter" even though the files are visible there, and
docker-compose.yml's SwarmUI uses a named Docker volume for dlbackend with
no host path at all. Both scripts now try `docker exec` against a running
"*swarmui*" container first, and only fall back to a host filesystem
search for a genuinely non-containerized (bare VM/jail) install.

Plain-text/shell-syntax checks, matching this repo's existing convention
for install-script regression coverage (test_krea2_gguf_node_installer.py,
test_swarmui_volta_torch_fix.py) — no live Docker/SwarmUI in CI."""
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_MANAGER_SCRIPT = _REPO_ROOT / "install-comfyui-manager.sh"
_KREA2_SCRIPT = _REPO_ROOT / "install-comfyui-gguf-krea2.sh"
_SCRIPTS = {"manager": _MANAGER_SCRIPT, "krea2": _KREA2_SCRIPT}


def test_both_scripts_exist_and_are_executable():
    for script in _SCRIPTS.values():
        assert script.exists()
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


def test_both_scripts_are_valid_shell_syntax():
    for script in _SCRIPTS.values():
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


def test_both_scripts_define_docker_container_detection():
    for name, script in _SCRIPTS.items():
        text = script.read_text()
        assert "find_swarmui_container()" in text, name
        assert "docker ps --format '{{.Names}}'" in text, name
        assert "grep -i swarmui" in text, name


def test_both_scripts_try_docker_before_the_host_path_fallback():
    for name, script in _SCRIPTS.items():
        text = script.read_text()
        docker_call_idx = text.index('install_via_docker "$FOUND_CONTAINER"')
        fallback_idx = text.index("Fall back: host filesystem search")
        assert docker_call_idx < fallback_idx, name


def test_both_scripts_run_pip_install_via_docker_exec_against_the_container_venv():
    for name, script in _SCRIPTS.items():
        text = script.read_text()
        assert 'CONTAINER_VENV_PYTHON="/SwarmUI/dlbackend/ComfyUI/venv/bin/python"' in text, name
        assert 'docker exec "$container" "$CONTAINER_VENV_PYTHON" -s -m pip install' in text, name


def test_both_scripts_accept_an_exact_container_name_as_the_argument():
    for name, script in _SCRIPTS.items():
        text = script.read_text()
        assert 'docker ps --format \'{{.Names}}\' 2>/dev/null | grep -qx "$1"' in text, name


def test_search_paths_include_the_lowercase_truenas_mount_casing():
    """truenas-compose.yml's actual bind-mount path is lowercase
    /mnt/DeadPool/apps/swarmui — the original SEARCH_PATHS only listed the
    SwarmUI-cased variant and would never match a real deployment's host
    path."""
    for name, script in _SCRIPTS.items():
        text = script.read_text()
        assert "/mnt/DeadPool/apps/swarmui" in text, name
        assert "/mnt/DeadPool/apps/SwarmUI" in text, name


def test_krea2_script_removes_the_conflicting_upstream_node_inside_docker_too():
    """The host-fallback path's upstream-node conflict removal
    (test_krea2_gguf_node_installer.py) has an equivalent inside
    install_via_docker() — otherwise a containerized SwarmUI install (the
    normal case for this repo) would hit the duplicate-node-class conflict
    the host-fallback path was written to avoid."""
    text = _KREA2_SCRIPT.read_text()
    via_docker_start = text.index("install_via_docker()")
    via_docker_end = text.index("if [[ -z \"${1:-}\" ]]")
    via_docker_body = text[via_docker_start:via_docker_end]
    assert 'docker exec "$container" mv "$upstream_dir" "$backup_dir"' in via_docker_body
    # Removal must happen before the clone, within the docker-exec path too.
    remove_idx = via_docker_body.index('mv "$upstream_dir" "$backup_dir"')
    clone_idx = via_docker_body.index('git clone "$GGUF_REPO" "$fork_dir"')
    assert remove_idx < clone_idx
