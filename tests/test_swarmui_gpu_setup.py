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


def test_gpu_setup_doc_corrects_the_swarmui_vram_hold_claim():
    """§3a used to claim SwarmUI "only loads a checkpoint into VRAM while
    actively rendering" — false. Confirmed against SwarmUI's own source
    (src/Core/Settings.cs): BackendData.ClearVRAMAfterMinutes (default 10)
    keeps the last-used checkpoint resident for that long after the LAST
    generation, the same kind of hold-time tradeoff as OLLAMA_KEEP_ALIVE,
    not an always-releases-immediately behavior."""
    text = (_REPO_ROOT / "docs/GPU_SETUP.md").read_text()
    section = text.split("## 3a.", 1)[1].split("## 3b.", 1)[0]
    assert "only loads a checkpoint into VRAM while actively rendering" not in section
    assert "ClearVRAMAfterMinutes" in section
    assert "10 minutes" in section or "default **10" in section


def test_gpu_setup_doc_does_not_recommend_the_nonexistent_whisper_model():
    """§6 recommended "ggml-large-v3-q8_0.bin" for better accuracy at lower
    VRAM — that filename doesn't exist in whisper.cpp's own repo (only
    large-v3-q5_0 and large-v3-turbo-q8_0 do; see app/ai.py's
    WHISPER_KNOWN_MODELS). A GM following this doc's exact filename would
    get a 404 from the Models tab's download button."""
    text = (_REPO_ROOT / "docs/GPU_SETUP.md").read_text()
    assert "## 6." in text
    section = text.split("## 6.", 1)[1].split("## 7.", 1)[0]
    assert "ggml-large-v3-q8_0.bin" not in section
    assert "ggml-large-v3-q5_0.bin" in section


def test_gpu_setup_doc_5a_replaces_vague_precision_advice_with_confirmed_log_lines():
    """§5a used to tell a GM to "check whichever precision setting SwarmUI's
    backend configuration exposes" — there is no such setting; ComfyUI
    picks precision automatically and only ever reports it via log lines.
    Confirmed against ComfyUI's own source (comfy/model_base.py,
    comfy/sd.py) for the exact log line text."""
    text = (_REPO_ROOT / "docs/GPU_SETUP.md").read_text()
    assert "## 5a." in text
    section = text.split("## 5a.", 1)[1].split("## 6.", 1)[0]
    assert "whichever precision setting SwarmUI's backend configuration exposes" not in section
    assert "model weight dtype" in section
    assert "manual cast" in section


def test_gpu_setup_doc_5a_covers_the_real_extra_args_field_and_default_attention():
    """§5a used to suggest --use-pytorch-cross-attention as something a GM
    might need to set — it's already ComfyUI's default on NVIDIA/PyTorch
    2.x with no xformers installed (confirmed against
    comfy/model_management.py), so the advice must say to leave it blank,
    not to set it. Also checks the real SwarmUI field name (confirmed
    against ComfyUI_SelfStartBackend.cs's ExtraArgs config) is named."""
    text = (_REPO_ROOT / "docs/GPU_SETUP.md").read_text()
    section = text.split("## 5a.", 1)[1].split("## 6.", 1)[0]
    assert "Extra Args" in section
    assert "leave it blank" in section.lower()
    assert "scaled_dot_product_attention" in section or "SDPA" in section


def test_gpu_setup_doc_5a_mentions_vae_tile_size_and_the_oom_retry_warning():
    """Confirmed against comfy/sd.py: ComfyUI auto-retries a failed VAE
    decode with tiling and logs a specific warning when it does — §5a
    should point a GM at SwarmUI's real VAE Tile Size parameter instead of
    leaving them to just wait for the automatic retry every time."""
    text = (_REPO_ROOT / "docs/GPU_SETUP.md").read_text()
    section = text.split("## 5a.", 1)[1].split("## 6.", 1)[0]
    assert "VAE Tile Size" in section
    assert "retrying with tiled VAE decoding" in section


def test_gpu_setup_doc_7_corrects_the_nothing_lost_int8_tensor_core_claim():
    """§7 overstated that Volta loses "nothing... except newer kernels"
    vs newer cards for GGUF inference — Volta's tensor cores are FP16-only
    and have no equivalent to Turing's native INT8 tensor core
    instructions, which llama.cpp's quantized (MMQ) kernels can use
    directly on Turing+ (confirmed against llama.cpp's own CUDA source,
    ggml/src/ggml-cuda/common.cuh: separate volta_mma_available() and
    turing_mma_available() checks)."""
    text = (_REPO_ROOT / "docs/GPU_SETUP.md").read_text()
    assert "## 7." in text
    section = text.split("## 7.", 1)[1]
    assert "nothing is lost vs newer cards except their\n  newer kernels" not in section
    assert "INT8 tensor core" in section
    assert "volta_mma_available" in section


def test_gpu_setup_doc_recommends_capping_max_auto_num_ctx_on_a_shared_card():
    """MAX_AUTO_NUM_CTX (app/ai.py, default 32768) bounds the auto-sized
    context window background jobs (recap/facts/condense) pin per-call —
    on a shared 16 GB V100 running SwarmUI too, that default is large
    enough to starve a concurrent image generation of VRAM."""
    text = (_REPO_ROOT / "docs/GPU_SETUP.md").read_text()
    assert "## 4." in text
    section = text.split("## 4.", 1)[1].split("## 5.", 1)[0]
    assert "MAX_AUTO_NUM_CTX=16384" in section
    assert "32768" in section
