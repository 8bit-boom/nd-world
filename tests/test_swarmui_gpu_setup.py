"""Regression tests for SwarmUI GPU passthrough (finalizing task #51's V100
backlog item): `docker-compose.gpu.yml` and `truenas-compose.yml` already
wired an NVIDIA device reservation for `ollama` (and an optional one for
`whisper`), but never one for `swarmui` — the actual GPU-bound image
generation backend — despite docs/GPU_SETUP.md and README.md's "GPU
acceleration" section only ever talking about Ollama/whisper. A GM
following those docs to give their V100 to Ollama would still be running
SwarmUI on CPU with no indication anything was missing.

These are plain-text assertions on the compose/doc files themselves,
matching this repo's existing convention (see
test_job_shutdown.py::test_compose_files_set_a_stop_grace_period_for_the_world_service)
rather than parsing the YAML — `yaml` isn't a declared project dependency."""
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent


def test_docker_compose_gpu_yml_reserves_the_gpu_for_swarmui():
    text = (_REPO_ROOT / "docker-compose.gpu.yml").read_text()
    assert "\n  swarmui:" in text
    swarmui_block = text.split("\n  swarmui:", 1)[1].split("\n\n", 1)[0]
    assert "driver: nvidia" in swarmui_block
    assert "capabilities: [gpu]" in swarmui_block


def test_truenas_compose_yml_has_a_swarmui_gpu_block_to_uncomment():
    text = (_REPO_ROOT / "truenas-compose.yml").read_text()
    swarmui_block = text.split("\n  swarmui:", 1)[1].split("\n\n", 1)[0]
    # Commented, not active — TrueNAS's own Resources/GPU picker is the
    # default path; a pasted-YAML Custom App has to uncomment this instead
    # (see the ollama service's identical pattern, already covered before
    # this fix).
    assert "# deploy:" in swarmui_block
    assert "#         - driver: nvidia" in swarmui_block
    assert "capabilities: [gpu]" in swarmui_block


def test_readme_gpu_section_mentions_swarmui():
    text = (_REPO_ROOT / "README.md").read_text()
    gpu_section = text.split("### GPU acceleration", 1)[1].split("\n### ", 1)[0]
    assert "SwarmUI" in gpu_section


def test_gpu_setup_doc_covers_swarmui_wiring_and_vram_sharing():
    text = (_REPO_ROOT / "docs/GPU_SETUP.md")
    text = text.read_text()
    assert "swarmui" in text.lower()
    # The single-GPU VRAM-sharing guidance the user's exact setup (one V100
    # running both ollama and swarmui) needs.
    assert "## 3a." in text
    assert "OLLAMA_KEEP_ALIVE" in text.split("## 3a.", 1)[1].split("## 4.", 1)[0]
