"""Tests for app/ollama_tuning.py's settings-recommendation engine —
model_params_b and recommend_settings. Deliberately coarse by design (see
that module's docstring): a handful of fit tiers (full_gpu/partial_gpu/
cpu_only/unknown) rather than real quantization-aware VRAM math, since this
app targets a single self-hosted GM, not a fleet of GPUs.
"""
from app import ollama_tuning as tuning


# ── model_params_b ───────────────────────────────────────────────────────────

def test_params_from_parameter_size():
    assert tuning.model_params_b("llama3.1:8b", parameter_size="8.0B") == 8.0
    assert tuning.model_params_b("qwen2.5:32b", parameter_size="32.8B") == 32.8


def test_params_from_tag_name():
    assert tuning.model_params_b("qwen2.5:32b") == 32.0
    assert tuning.model_params_b("llama-3.1-70b-instruct") == 70.0
    assert tuning.model_params_b("gemma3:9b-it-q4_K_M") == 9.0


def test_params_from_size_bytes():
    # ~0.6 bytes/parameter is the rough Q4_K_M estimate this function falls
    # back to when nothing else is available.
    result = tuning.model_params_b("mystery-model", size_bytes=int(4.2e9))
    assert result is not None
    assert 6.5 < result < 7.5


def test_params_unknown_returns_none():
    assert tuning.model_params_b("mystery-model") is None


def test_params_prefers_parameter_size_over_tag_name():
    # A tag that would otherwise match "7b" but a real parameter_size that
    # disagrees (a quantized rename, say) -- the authoritative field wins.
    assert tuning.model_params_b("some-7b-nickname", parameter_size="13.0B") == 13.0


# ── recommend_settings: full GPU fit ────────────────────────────────────────

def test_full_gpu_fit_picks_large_context():
    hardware = {"vram_total_mb": 24000, "ram_total_mb": 65536, "cpu_cores": 16}
    rec = tuning.recommend_settings(model="llama3.1:8b", hardware=hardware,
                                     parameter_size="8.0B", size_bytes=int(4.9e9))
    assert rec["fit"] == "full_gpu"
    assert rec["per_request"]["num_gpu"] == 999
    assert rec["per_request"]["num_ctx"] >= 16384
    assert rec["server"]["OLLAMA_FLASH_ATTENTION"] == "1"


def test_full_gpu_fit_raises_max_loaded_models_on_big_cards():
    hardware = {"vram_total_mb": 32000, "ram_total_mb": 65536, "cpu_cores": 16}
    rec = tuning.recommend_settings(model="llama3.1:8b", hardware=hardware,
                                     parameter_size="8.0B", size_bytes=int(4.9e9))
    assert rec["server"]["OLLAMA_MAX_LOADED_MODELS"] == "2"


def test_full_gpu_fit_keeps_max_loaded_models_at_one_on_small_cards():
    hardware = {"vram_total_mb": 12000, "ram_total_mb": 32768, "cpu_cores": 8}
    rec = tuning.recommend_settings(model="llama3.2:3b", hardware=hardware,
                                     parameter_size="3.0B", size_bytes=int(2.0e9))
    assert rec["fit"] == "full_gpu"
    assert rec["server"]["OLLAMA_MAX_LOADED_MODELS"] == "1"


def test_tight_fit_switches_kv_cache_and_requires_flash_attention():
    """The one coupling this engine must never get wrong: a quantized KV
    cache is inert without flash attention active, so whenever
    OLLAMA_KV_CACHE_TYPE is recommended, OLLAMA_FLASH_ATTENTION must be
    recommended alongside it.

    Numbers picked so weights alone leave no f16 headroom even at the
    smallest context rung (2048), forcing the search into KV quantization:
    weights_mb=7700, budget=9000-1024=7976 -> f16 needs 7700+416=8116 > 7976."""
    hardware = {"vram_total_mb": 9000, "ram_total_mb": 32768, "cpu_cores": 8}
    rec = tuning.recommend_settings(model="custom-model:13b", hardware=hardware,
                                     parameter_size="13.0B", size_bytes=7700 * 1024 * 1024)
    assert rec["fit"] == "full_gpu"
    assert rec["server"].get("OLLAMA_KV_CACHE_TYPE") in ("q8_0", "q4_0")
    assert rec["server"]["OLLAMA_FLASH_ATTENTION"] == "1"
    assert rec["per_request"]["num_ctx"] >= 2048


def test_prefers_q8_0_when_it_unlocks_a_meaningfully_higher_context_than_f16():
    """Before this fix, q8_0 was only ever tried as a last resort when f16
    didn't fit AT ALL — so a model that fit f16 at a small context rung
    (8192 here) kept f16 even though halving the KV cache to q8_0 would
    reach a full rung higher (16384) for the same VRAM budget. Numbers:
    weights_mb=18120, budget=24000-1024=22976, kv_per_1k(f16)=320 ->
    f16 tops out at ctx=8192; kv_per_1k(q8_0)=160 -> q8_0 reaches 16384."""
    hardware = {"vram_total_mb": 24000, "ram_total_mb": 65536, "cpu_cores": 16}
    rec = tuning.recommend_settings(model="qwen2.5:32b", hardware=hardware,
                                     parameter_size="32.8B", size_bytes=int(19e9))
    assert rec["fit"] == "full_gpu"
    assert rec["server"]["OLLAMA_KV_CACHE_TYPE"] == "q8_0"
    assert rec["per_request"]["num_ctx"] == 16384


# ── recommend_settings: imagegen_reserve_mb (shared V100 with SwarmUI) ──────

def test_imagegen_reserve_shrinks_the_recommended_context():
    """The bug this task exists for: without a VRAM reserve for a
    concurrently-running SwarmUI checkpoint, recommend_settings happily
    fills the entire card with Ollama's KV cache — leaving SwarmUI to hit
    a CUDA out-of-memory error the moment a GM generates an image mid-chat
    (see docs/GPU_SETUP.md §3a)."""
    hardware = {"vram_total_mb": 16384, "ram_total_mb": 65536, "cpu_cores": 16}
    no_reserve = tuning.recommend_settings(model="gemma3:12b", hardware=hardware,
                                            parameter_size="12.0B", size_bytes=int(7.5e9))
    with_reserve = tuning.recommend_settings(model="gemma3:12b", hardware=hardware,
                                              parameter_size="12.0B", size_bytes=int(7.5e9),
                                              imagegen_reserve_mb=tuning.DEFAULT_IMAGEGEN_RESERVE_MB)
    assert with_reserve["per_request"]["num_ctx"] < no_reserve["per_request"]["num_ctx"]
    assert any("swarmui" in n.lower() for n in with_reserve["notes"])


def test_imagegen_reserve_can_push_a_model_into_partial_fit():
    hardware = {"vram_total_mb": 16384, "ram_total_mb": 65536, "cpu_cores": 16}
    weights = 10 * 1024 * 1024 * 1024
    without = tuning.recommend_settings(model="big:20b", hardware=hardware,
                                         parameter_size="20.0B", size_bytes=weights)
    assert without["fit"] == "full_gpu"

    with_reserve = tuning.recommend_settings(model="big:20b", hardware=hardware,
                                              parameter_size="20.0B", size_bytes=weights,
                                              imagegen_reserve_mb=tuning.DEFAULT_IMAGEGEN_RESERVE_MB)
    assert with_reserve["fit"] == "partial_gpu"
    assert any("swarmui" in n.lower() for n in with_reserve["notes"])


def test_imagegen_reserve_zero_by_default_changes_nothing():
    hardware = {"vram_total_mb": 16384, "ram_total_mb": 65536, "cpu_cores": 16}
    default = tuning.recommend_settings(model="gemma3:12b", hardware=hardware,
                                         parameter_size="12.0B", size_bytes=int(7.5e9))
    explicit_zero = tuning.recommend_settings(model="gemma3:12b", hardware=hardware,
                                               parameter_size="12.0B", size_bytes=int(7.5e9),
                                               imagegen_reserve_mb=0)
    assert default == explicit_zero


def test_imagegen_reserve_negative_is_clamped_to_zero():
    hardware = {"vram_total_mb": 16384, "ram_total_mb": 65536, "cpu_cores": 16}
    default = tuning.recommend_settings(model="gemma3:12b", hardware=hardware,
                                         parameter_size="12.0B", size_bytes=int(7.5e9))
    negative = tuning.recommend_settings(model="gemma3:12b", hardware=hardware,
                                          parameter_size="12.0B", size_bytes=int(7.5e9),
                                          imagegen_reserve_mb=-500)
    assert negative == default


# ── recommend_settings: partial GPU fit ─────────────────────────────────────

def test_partial_fit_omits_num_gpu():
    hardware = {"vram_total_mb": 8000, "ram_total_mb": 65536, "cpu_cores": 16}
    rec = tuning.recommend_settings(model="qwen2.5:32b", hardware=hardware,
                                     parameter_size="32.8B", size_bytes=int(19e9))
    assert rec["fit"] == "partial_gpu"
    assert "num_gpu" not in rec["per_request"]
    assert any("split" in n.lower() for n in rec["notes"])


# ── recommend_settings: CPU-only ────────────────────────────────────────────

def test_cpu_only_sets_num_gpu_zero_and_threads():
    hardware = {"vram_total_mb": 0, "ram_total_mb": 16384, "cpu_cores": 6, "vram_source": "manual"}
    rec = tuning.recommend_settings(model="gemma3:9b", hardware=hardware, parameter_size="9.0B")
    assert rec["fit"] == "cpu_only"
    assert rec["per_request"]["num_gpu"] == 0
    assert rec["per_request"]["num_thread"] == 6
    assert rec["per_request"]["num_ctx"] > 0


def test_cpu_only_caps_threads_at_sixteen():
    hardware = {"vram_total_mb": 0, "ram_total_mb": 16384, "cpu_cores": 64, "vram_source": "manual"}
    rec = tuning.recommend_settings(model="gemma3:9b", hardware=hardware, parameter_size="9.0B")
    assert rec["per_request"]["num_thread"] == 16


# ── recommend_settings: unknown ─────────────────────────────────────────────

def test_unknown_vram_returns_unknown_and_no_gpu_advice():
    hardware = {"vram_total_mb": None, "ram_total_mb": 16384, "cpu_cores": 4, "vram_source": "none"}
    rec = tuning.recommend_settings(model="gemma3:9b", hardware=hardware)
    assert rec["fit"] == "unknown"
    assert rec["per_request"] == {}
    assert rec["server"] == {}
    assert rec["notes"]


def test_unknown_model_size_with_known_vram_still_returns_generic_advice():
    hardware = {"vram_total_mb": 24000, "ram_total_mb": 65536, "cpu_cores": 16}
    rec = tuning.recommend_settings(model="totally-unknown-model", hardware=hardware)
    assert rec["fit"] == "unknown"
    assert rec["params_b"] is None
    assert rec["per_request"] == {"num_gpu": 999}


# ── Regression guard: every recommended server key must be real ────────────
# (The matching per_request-key guard, checked against routers.ai's own
# per-request allowlist, is added alongside that allowlist's expansion —
# see tests/test_ollama_options.py's test_recommendation_keys_are_all_real.)

_SERVER_ENV_KEYS = set(tuning.SERVER_ENV_KEYS)


def test_recommendation_server_keys_are_all_real():
    scenarios = [
        {"vram_total_mb": 24000, "ram_total_mb": 65536, "cpu_cores": 16},   # full_gpu
        {"vram_total_mb": 8000, "ram_total_mb": 65536, "cpu_cores": 16},    # partial_gpu
        {"vram_total_mb": 0, "ram_total_mb": 16384, "cpu_cores": 8},        # cpu_only
        {"vram_total_mb": None, "ram_total_mb": 16384, "cpu_cores": 4},     # unknown
    ]
    for hardware in scenarios:
        rec = tuning.recommend_settings(model="qwen2.5:32b", hardware=hardware,
                                         parameter_size="32.8B", size_bytes=int(19e9))
        for key in rec["server"]:
            assert key in _SERVER_ENV_KEYS, f"{key} (fit={rec['fit']}) not in SERVER_ENV_KEYS"


def test_recommend_settings_never_raises_for_edge_case_hardware():
    for hardware in (
        {},
        {"vram_total_mb": 0, "ram_total_mb": None, "cpu_cores": None},
        {"vram_total_mb": 1, "ram_total_mb": 1, "cpu_cores": 1},
    ):
        rec = tuning.recommend_settings(model="something:7b", hardware=hardware)
        assert "fit" in rec


def test_unsloth_recommendation_precision_note_is_arch_aware():
    """The fp16/flash-attention guidance must cover both pre-Ampere cards
    correctly: fp16 everywhere (no bf16/fp8 kernels on Volta OR Turing),
    but flash attention only disabled on Volta — Turing (T4/T10, sm_75) is
    where llama.cpp's flash-attention support starts."""
    rec = tuning._unsloth_recommendation(model="m", params_b=26.0, weights_mb=13600, vram_total_mb=16384)
    note = " ".join(rec["notes"])
    assert "fp16" in note
    assert "Turing" in note and "Volta" in note
    assert "flash attention" in note.lower()
