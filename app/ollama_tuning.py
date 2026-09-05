"""Server-level Ollama tuning, hardware detection, and settings
recommendations — backs the "Ollama server tuning" and "Detected hardware"
sections of Settings > System (app/main.py's /settings/system route,
app/templates/settings.html).

Two buckets of Ollama tuning exist in this app, and they work completely
differently:

- **Bucket C — per-request options** (AppSettings.ollama_temperature,
  ollama_num_ctx, ollama_num_gpu, and friends in app/models.py, applied via
  app.ai.effective_ollama_options()/set_ollama_generation_overrides()).
  Every one of these is a real field of Ollama's own api.Options/api.Runner
  struct (verified against Ollama's api/types.go), sent fresh with every
  chat/generate call — so changing one takes effect on the very next
  request, with no restart of anything. This module doesn't own that
  bucket; it's listed here only for contrast.

- **Bucket A — server env vars** (this module's SERVER_ENV_SPEC below).
  Ollama reads these only from its OWN process environment at start, and
  has no runtime API to change them — OLLAMA_FLASH_ATTENTION,
  OLLAMA_KV_CACHE_TYPE, OLLAMA_NUM_PARALLEL, and so on. Before this feature,
  changing one meant hand-editing docker-compose.yml's `ollama:` service and
  running `docker compose up -d ollama`. This module instead writes a GM's
  chosen values to a generated env file on a volume shared with the ollama
  container (write_server_env/render_env_file), which that container's
  entrypoint sources at start — the exact mechanism app.ai's
  active_whisper_model()/set_active_whisper_model() already use for the
  Whisper service's active-model marker. Applying a change still needs an
  actual container restart — Ollama genuinely has no other way — so
  server_env_status() reports whether the running ollama container has
  caught up yet (comparing what was written against what its entrypoint
  last stamped as applied), and the UI shows the exact restart command
  rather than pretending to restart it automatically.

  Deliberately NOT built: mounting the Docker socket into nd-world so it
  could restart/recreate the ollama container itself with one click. That
  would grant the web app root-equivalent host access to save a GM one
  pasted command — this app keeps the `world` container unprivileged
  everywhere else (contrast the `android` service's deliberate
  `privileged: true` in docker-compose.yml, which shows the project already
  knows the difference and draws the line on purpose). If the one-time
  compose edit this needs (see docs/DEPLOYMENT.md) hasn't been made yet,
  server_env_status() reports that honestly too, instead of a silent no-op.

  SERVER_ENV_SPEC is an explicit allowlist, not "any OLLAMA_* var a form
  posts": OLLAMA_HOST/OLLAMA_ORIGINS/OLLAMA_MODELS are excluded because a
  bad value can make Ollama unreachable or orphan its model store from a
  single form field, and OLLAMA_LLM_LIBRARY is excluded as a free-text
  backend override with high foot-gun and near-zero benefit for this app's
  single-GM deployment shape.

Hardware detection (detect_hardware) and the settings recommendation engine
(recommend_settings) are deliberately coarse — this is a one-GM hobby app,
not a fleet-management VRAM planner. CPU core count and system RAM are
reliably readable from nd-world's own container (they reflect the host, not
the container, since /proc is a fresh view of the same kernel). GPU/VRAM
is the honest gap: nd-world's own container isn't normally given GPU
passthrough (only the ollama/swarmui services are, in docker-compose.yml),
so detection falls back through nvidia-smi -> AMD sysfs -> a GM-entered
manual override -> a lower-bound inferred from whatever Ollama already has
loaded (app.ai.resident_models()) -> "unknown". recommend_settings then
picks one of a handful of coarse tiers (full_gpu/partial_gpu/cpu_only/
unknown) rather than doing real quantization-aware VRAM math or per-
architecture GQA head-count sizing — Ollama's own scheduler already splits
partially-offloaded models across GPU/RAM better than this app could guess,
so recommendations for that case deliberately leave GPU-layer settings
blank rather than inventing a number.
"""
import asyncio
import glob
import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

# Where nd-world writes the generated env file. Must ALSO be bind-mounted
# into the ollama container (see docker-compose.yml) for any of this to
# actually reach Ollama — if it isn't, everything here still works
# (sanitizing, writing, reading back), the file is just never consumed, and
# server_env_status() reports wired=False so the UI can say so honestly.
OLLAMA_CONFIG_DIR = Path(os.environ.get("OLLAMA_CONFIG_DIR", "/data/ollama-config"))
ENV_FILENAME = "ollama.env"
# Written by the ollama container's own entrypoint (docker-compose.yml) —
# a copy of whatever ollama.env it actually sourced at start. Comparing
# this against the currently-desired values is how server_env_status()
# knows a restart is (still) pending, rather than just assuming one is.
APPLIED_FILENAME = "ollama.env.applied"

# Every accepted value must match this charset before being written to the
# file, regardless of its declared "kind" below — belt and braces. The file
# is `.`-sourced by a POSIX shell in the ollama container's entrypoint, so
# this is a real security boundary, not just tidiness: no spaces, quotes,
# newlines, or shell metacharacters ever reach it.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.,:+/-]{1,64}$")
# Single number + optional unit only (e.g. "30m", "5h", "-1", "0") — NOT
# Go's full time.ParseDuration grammar, which also allows composite forms
# like "1h30m". A GM who wants ninety minutes can enter "90m"; supporting
# the composite form isn't worth the extra parsing surface for this app.
_DURATION = re.compile(r"^-?\d+(\.\d+)?(ns|us|ms|s|m|h)?$")

# (env_key, form_field_suffix, kind, spec, help_text)
#   kind "choice"   -> spec is a tuple of allowed strings ("" is always
#                       allowed too, meaning "omit this key entirely")
#   kind "int"      -> spec is (lo, hi), inclusive
#   kind "duration" -> spec is None, validated against _DURATION
#   kind "token"    -> spec is None, validated against _SAFE_VALUE only
SERVER_ENV_SPEC = (
    ("OLLAMA_FLASH_ATTENTION", "flash_attention", "choice", ("1", "0"),
     "Auto-on where the backend supports it on current Ollama; force it only to debug."),
    ("OLLAMA_KV_CACHE_TYPE", "kv_cache_type", "choice", ("f16", "q8_0", "q4_0"),
     "Needs flash attention active; silently falls back to f16 on unsupported architectures."),
    ("OLLAMA_NUM_PARALLEL", "num_parallel", "int", (0, 32),
     "Concurrent requests per loaded model; each slot costs its own KV cache."),
    ("OLLAMA_MAX_LOADED_MODELS", "max_loaded_models", "int", (0, 16),
     "Models resident at once, per GPU. Blank = Ollama picks automatically."),
    ("OLLAMA_MAX_QUEUE", "max_queue", "int", (1, 4096),
     "Queued request limit before Ollama starts rejecting new ones. Blank = 512."),
    ("OLLAMA_KEEP_ALIVE", "keep_alive", "duration", None,
     "Server default keep-alive, e.g. 5m, 1h, -1 (forever), 0. The per-request setting above overrides it."),
    ("OLLAMA_CONTEXT_LENGTH", "context_length", "int", (256, 1048576),
     "Server default context length. The per-request num_ctx above overrides it."),
    ("OLLAMA_LOAD_TIMEOUT", "load_timeout", "duration", None,
     "How long Ollama waits for a model to load. Raise on slow disks or very large models. Blank = 5m."),
    ("OLLAMA_GPU_OVERHEAD", "gpu_overhead", "int", (0, 137438953472),
     "Bytes of VRAM per GPU to leave unused, e.g. for another app sharing the same card."),
    ("OLLAMA_SCHED_SPREAD", "sched_spread", "choice", ("1", "0"),
     "Spread one model's layers across every visible GPU instead of filling one at a time."),
    ("OLLAMA_DEBUG", "debug", "choice", ("1", "2"),
     "Verbose (1) or trace (2) server logging."),
    ("OLLAMA_IGPU_ENABLE", "igpu_enable", "choice", ("1", "0"),
     "Allow integrated GPUs to be used, not just discrete ones."),
    ("OLLAMA_VULKAN", "vulkan", "choice", ("1", "0"),
     "Vulkan backend (on by default on non-macOS)."),
    ("HSA_OVERRIDE_GFX_VERSION", "hsa_override_gfx", "token", None,
     "AMD/ROCm GPU compatibility override, e.g. 11.0.0. See .env.example."),
    ("ROCR_VISIBLE_DEVICES", "rocr_visible", "token", None,
     "AMD/ROCm GPU selector, e.g. 0 or 0,1. See .env.example."),
    ("CUDA_VISIBLE_DEVICES", "cuda_visible", "token", None,
     "NVIDIA GPU selector, e.g. 0 or 0,1."),
)
SERVER_ENV_KEYS = tuple(spec[0] for spec in SERVER_ENV_SPEC)
SERVER_ENV_HELP = {spec[0]: spec[4] for spec in SERVER_ENV_SPEC}
FORM_PREFIX = "ollama_srv_"  # a server-env form field's name is FORM_PREFIX + suffix


def sanitize_server_env(raw: dict) -> tuple[dict, list[str]]:
    """Filter `raw` (env_key -> str, e.g. straight from a submitted form)
    down to SERVER_ENV_SPEC's allowlist, validating each value. Returns
    (clean, errors). A blank/missing value simply drops the key, which is
    how a GM clears an override back to "whatever .env / the ollama
    container's own default says" — same blank-means-unset convention this
    app already uses for the per-request Ollama fields.

    A key that isn't in SERVER_ENV_SPEC at all is dropped silently, with no
    error: the allowlist is a security boundary (OLLAMA_HOST/OLLAMA_MODELS
    etc. could make Ollama unreachable or orphan its model store if set from
    a stray form field), not user input to validate and report on."""
    clean: dict = {}
    errors: list[str] = []
    for env_key, _suffix, kind, spec, _help in SERVER_ENV_SPEC:
        value = raw.get(env_key)
        if value is None:
            continue
        value = str(value).strip()
        if not value:
            continue
        if kind == "choice":
            if value not in spec:
                errors.append(f"{env_key} must be one of: {', '.join(spec)}")
                continue
        elif kind == "int":
            lo, hi = spec
            try:
                parsed = int(value)
            except ValueError:
                errors.append(f"{env_key} must be a whole number")
                continue
            if not (lo <= parsed <= hi):
                errors.append(f"{env_key} must be between {lo} and {hi}")
                continue
            value = str(parsed)
        elif kind == "duration":
            if not _DURATION.match(value):
                errors.append(f"{env_key} must be a duration like '30m', '5h', '-1', or '0'")
                continue
        elif kind == "token":
            if not _SAFE_VALUE.match(value):
                errors.append(f"{env_key} contains characters that aren't allowed")
                continue
        if not _SAFE_VALUE.match(value):
            # Every accepted value, regardless of kind, must satisfy the
            # file-safety charset before being returned — belt and braces.
            errors.append(f"{env_key} contains characters that aren't allowed")
            continue
        clean[env_key] = value
    return clean, errors


def render_env_file(values: dict) -> str:
    """The exact bytes write_server_env writes. Deterministic key order
    (SERVER_ENV_KEYS, not dict insertion order) so saving the same values
    twice in a row produces a byte-identical file and doesn't spuriously
    flip server_env_status()'s "pending restart" banner on."""
    lines = [
        "# Generated by nd-world (Settings > System > Ollama server tuning).",
        "# Do not edit by hand -- it is overwritten on every save.",
        "# Sourced by the \"ollama\" service at container start; see docker-compose.yml.",
    ]
    for key in SERVER_ENV_KEYS:
        if key in values:
            lines.append(f"{key}={values[key]}")
    return "\n".join(lines) + "\n"


def write_server_env(values: dict) -> None:
    """Atomically write ollama.env (tmp file + Path.replace — the same
    pattern app.ai.set_active_whisper_model uses for its own shared-volume
    marker file). Raises OSError if OLLAMA_CONFIG_DIR isn't writable; the
    caller surfaces that as a form warning rather than a 500, since an
    un-bind-mounted or read-only path is a deployment step not yet taken,
    not an application bug."""
    OLLAMA_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = OLLAMA_CONFIG_DIR / ENV_FILENAME
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(render_env_file(values), encoding="utf-8")
    tmp.replace(path)


def read_server_env_file(filename: str = ENV_FILENAME) -> Optional[dict]:
    """Parse a generated env file (ollama.env or ollama.env.applied) back
    into a dict. Returns None if the file doesn't exist at all — distinct
    from {} (the file exists but every value was cleared), which matters to
    server_env_status()'s wired/pending logic."""
    path = OLLAMA_CONFIG_DIR / filename
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    result: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def server_env_status(desired: dict) -> dict:
    """What Settings > System renders for the "Ollama server tuning"
    section — whether the config dir is writable, whether the ollama
    service is actually reading these files (wired), and whether it's
    running with the current values yet (pending). Never raises."""
    writable = True
    try:
        OLLAMA_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        probe = OLLAMA_CONFIG_DIR / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        writable = False

    applied = read_server_env_file(APPLIED_FILENAME)
    wired = applied is not None
    pending = wired and applied != desired

    return {
        "config_dir": str(OLLAMA_CONFIG_DIR),
        "env_path": str(OLLAMA_CONFIG_DIR / ENV_FILENAME),
        "writable": writable,
        "wired": wired,
        "pending": pending,
        "desired": desired,
        "applied": applied,
        "restart_command": "docker compose restart ollama",
        "restart_command_truenas": "docker compose -f truenas-compose.yml restart ollama",
    }


# ── Hardware detection ──────────────────────────────────────────────────────

def _read_proc_cpuinfo(path: Path = Path("/proc/cpuinfo")) -> tuple[str, Optional[int]]:
    """(model name, core count) from /proc/cpuinfo. /proc is a fresh view
    of the HOST kernel even inside a container, so this reports the real
    host CPU nd-world and Ollama both actually run on — deliberately not
    cgroup CPU limits, which would describe nd-world's own container quota
    and make a recommendation about a DIFFERENT process (Ollama) worse, not
    better. Best-effort: ("", None) on any failure. `path` is overridable
    only so tests can point it at a fixture file."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", None
    model = ""
    count = 0
    for line in raw.splitlines():
        if not model and line.startswith("model name"):
            _, _, value = line.partition(":")
            model = value.strip()
        if line.startswith("processor"):
            count += 1
    return model, (count or None)


def _read_proc_meminfo(path: Path = Path("/proc/meminfo")) -> tuple[Optional[int], Optional[int]]:
    """(MemTotal, MemAvailable) in MB from /proc/meminfo — host figures,
    same reasoning as _read_proc_cpuinfo above (never cgroup memory.max).
    `path` is overridable only so tests can point it at a fixture file."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    values: dict = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if rest.endswith("kB"):
            try:
                values[key.strip()] = int(rest[:-2].strip()) // 1024
            except ValueError:
                pass
    return values.get("MemTotal"), values.get("MemAvailable")


async def _detect_nvidia_gpus() -> list[dict]:
    """One GPU dict per line of `nvidia-smi --query-gpu=name,memory.total`
    — only ever produces a result when the NVIDIA container runtime has
    actually given THIS container GPU access (docker-compose.yml's `world`
    service has none by default; only `ollama`/`swarmui` do), which is
    exactly the signal we want: nvidia-smi simply isn't on PATH otherwise."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            return []
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    gpus = []
    for line in out.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, mem = line.rpartition(",")
        try:
            vram_mb = int(mem.strip())
        except ValueError:
            continue
        gpus.append({"vendor": "nvidia", "name": name.strip(), "vram_mb": vram_mb})
    return gpus


def _detect_amd_gpus(pattern: str = "/sys/class/drm/card*/device/mem_info_vram_total") -> list[dict]:
    """AMD's amdgpu driver exposes total VRAM directly under /sys — no
    subprocess needed, and /sys is mounted by default in a plain container
    (no special device passthrough required to just read it), unlike the
    NVIDIA path above which only works with real GPU passthrough. Confirms
    the PCI vendor id (0x1002 = AMD) on each card before trusting it.
    `pattern` is overridable only so tests can point it at a fixture tree."""
    gpus = []
    for vram_path in sorted(glob.glob(pattern)):
        card_dir = Path(vram_path).parent
        try:
            vendor = (card_dir / "vendor").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if vendor.lower() != "0x1002":
            continue
        try:
            vram_bytes = int(Path(vram_path).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        gpus.append({"vendor": "amd", "name": "AMD GPU", "vram_mb": vram_bytes // (1024 * 1024)})
    return gpus


async def detect_hardware(vram_override_mb: Optional[int] = None) -> dict:
    """Best-effort snapshot of the host's CPU/RAM/GPU, for the "Detected
    hardware" panel on Settings > System and as recommend_settings()'s
    input. Never raises — every sub-detector swallows its own failures.

    vram_total_mb resolution order: a GM-entered override always wins
    (real knowledge beats guessing); then nvidia-smi; then AMD sysfs; then
    a LOWER BOUND inferred from whatever's already loaded in Ollama right
    now (app.ai.resident_models(), the same call the existing VRAM cockpit
    uses) — genuinely useful signal, just not the true total; then None,
    with a note explaining why and pointing at the manual field."""
    notes: list[str] = []
    cpu_model, cpu_cores = _read_proc_cpuinfo()
    try:
        cpu_affinity: Optional[int] = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpu_affinity = None
    ram_total_mb, ram_available_mb = _read_proc_meminfo()

    gpus = await _detect_nvidia_gpus()
    if not gpus:
        gpus = _detect_amd_gpus()

    vram_total_mb: Optional[int] = None
    vram_source = "none"
    vram_is_lower_bound = False

    if vram_override_mb:
        vram_total_mb = vram_override_mb
        vram_source = "manual"
    elif gpus:
        vram_total_mb = sum(g["vram_mb"] for g in gpus if g.get("vram_mb"))
        vram_source = f"{gpus[0]['vendor']}-{'smi' if gpus[0]['vendor'] == 'nvidia' else 'sysfs'}"
    else:
        try:
            from . import ai as _ai_module
            resident = await _ai_module.resident_models()
        except Exception:
            resident = []
        best_vram_bytes = max((m.get("size_vram_bytes") or 0 for m in resident), default=0)
        if best_vram_bytes:
            vram_total_mb = best_vram_bytes // (1024 * 1024)
            vram_source = "ollama-ps"
            vram_is_lower_bound = True
            notes.append(
                "VRAM inferred from a model Ollama already has loaded — a real lower "
                "bound, but likely less than your card's actual total."
            )

    if vram_source == "none":
        notes.append(
            "nd-world's own container can't see a GPU. That's normal — only the "
            "\"ollama\" service is given GPU access in docker-compose.yml. Enter "
            "your card's VRAM below, or give the \"world\" service the same GPU "
            "access as \"ollama\" if you'd rather this auto-detect."
        )

    return {
        "cpu_model": cpu_model,
        "cpu_cores": cpu_cores,
        "cpu_affinity": cpu_affinity,
        "ram_total_mb": ram_total_mb,
        "ram_available_mb": ram_available_mb,
        "gpus": gpus,
        "vram_total_mb": vram_total_mb,
        "vram_source": vram_source,
        "vram_is_lower_bound": vram_is_lower_bound,
        "notes": notes,
    }


# ── Settings recommendation engine ──────────────────────────────────────────
#
# Deliberately coarse: two small lookup tables and a handful of tiers, not a
# quantization-aware VRAM planner. This is a one-GM hobby app — the goal is
# "a sane starting point you can then tweak in the fields above", not a
# fleet-management sizing tool. In particular: partial-GPU-offload never
# guesses a num_gpu layer count (Ollama's own scheduler already splits
# layers across GPU/RAM better than this app could without a real
# per-architecture layer-size model), and there's no per-GQA-head-count or
# per-quantization-format weight math — parameter count and a coarse
# chars-per-token-style KV estimate are treated as good enough.

_PARAM_LEADING_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)")
_PARAM_IN_TAG = re.compile(r"(\d+(?:\.\d+)?)\s*[bB]\b")

# Rough KV-cache cost in MB per 1024 tokens at f16, by parameter-count class
# — (upper bound in billions of parameters, MB per 1k tokens). Deliberately
# coarse averages across current architectures; real usage varies several-
# fold with GQA head count, so this exists only to pick a rung on
# CONTEXT_LADDER below, never to be quoted as an exact number.
KV_MB_PER_1K_F16 = ((4, 64), (9, 128), (20, 208), (40, 320), (10**9, 512))
CONTEXT_LADDER = (2048, 4096, 8192, 16384, 32768, 65536)
_KV_SCALE = {"f16": 1.0, "q8_0": 0.5, "q4_0": 0.25}
# Headroom for compute buffers / fragmentation, subtracted from detected
# VRAM before fitting weights + KV cache into what's left.
_RESERVE_MB = 1024


def model_params_b(model: str, parameter_size: str = "", size_bytes: Optional[int] = None) -> Optional[float]:
    """Billions of parameters for `model`. Prefers Ollama's own
    details.parameter_size from /api/tags (e.g. "8.0B", "26.5B"), falls
    back to a "8b"/"32b"-style match in the model's own tag name (e.g.
    "qwen2.5:32b", "llama-3.1-70b-instruct"), then to a rough
    size_bytes/0.6e9 estimate (approximately Q4_K_M bytes-per-parameter).
    None if all three fail."""
    if parameter_size:
        m = _PARAM_LEADING_NUMBER.match(parameter_size)
        if m:
            return float(m.group(1))
    m = _PARAM_IN_TAG.search(model or "")
    if m:
        return float(m.group(1))
    if size_bytes:
        return size_bytes / 0.6e9
    return None


def _kv_mb_per_1k(params_b: float) -> int:
    for upper, mb in KV_MB_PER_1K_F16:
        if params_b <= upper:
            return mb
    return KV_MB_PER_1K_F16[-1][1]


def _best_ctx_fitting(weights_mb: int, budget_mb: float, params_b: float, kv_type: str) -> Optional[int]:
    """Largest CONTEXT_LADDER rung where weights_mb + this rung's KV cache
    (at kv_type's quantization) fits in budget_mb, or None if even the
    smallest rung doesn't fit."""
    kv_per_1k = _kv_mb_per_1k(params_b) * _KV_SCALE[kv_type]
    best = None
    for ctx in CONTEXT_LADDER:
        if weights_mb + kv_per_1k * (ctx / 1024) <= budget_mb:
            best = ctx
    return best


def _volta_note(hardware: dict) -> Optional[str]:
    """One advisory note when the detected GPU is a Volta-class card (V100 /
    TITAN V). Ollama still officially supports Volta (compute capability
    7.0) and flash attention + q8_0 KV cache both work on it, but NVIDIA's
    CUDA 13 toolkit dropped Volta — so a future Ollama release that moves
    to CUDA 13 builds would stop working on this card, and the fix is
    pinning the last CUDA 12 image tag (see docs/GPU_SETUP.md). Detection
    is name-based best-effort: it only runs when nd-world's own container
    can see nvidia-smi, so a card hidden behind the ollama-only GPU
    passthrough (the normal setup) simply produces no note."""
    for g in hardware.get("gpus") or []:
        name = str(g.get("name") or "")
        if "v100" in name.lower() or "volta" in name.lower() or "titan v" in name.lower():
            return (
                "Volta-class GPU detected: flash attention and q8_0 KV cache are "
                "supported and recommended, but CUDA 13 dropped Volta — if a future "
                "Ollama update stops detecting this GPU, pin the last CUDA 12-based "
                "ollama image tag (see docs/GPU_SETUP.md in the repo)."
            )
    return None


def recommend_settings(*, model: str, hardware: dict, parameter_size: str = "", size_bytes: Optional[int] = None) -> dict:
    """A starting-point settings bundle for `model` given already-detected
    `hardware` (detect_hardware()'s own return shape). Never raises — an
    unknown model size or unknown hardware just narrows what's recommended,
    reported honestly in `notes`, rather than guessing."""
    params_b = model_params_b(model, parameter_size, size_bytes)
    weights_mb = int(size_bytes / (1024 * 1024)) if size_bytes else None
    vram_total_mb = hardware.get("vram_total_mb")
    ram_total_mb = hardware.get("ram_total_mb")
    cpu_cores = hardware.get("cpu_cores") or 4

    base = {"model": model, "params_b": params_b, "weights_mb": weights_mb}

    if vram_total_mb is None:
        return {**base, "fit": "unknown", "per_request": {}, "server": {}, "notes": [
            "No GPU was detected and no VRAM was entered below — enter your "
            "card's total VRAM (or 0 if you have none) for a real recommendation.",
        ]}

    if vram_total_mb <= 0:
        ram_budget = (ram_total_mb or 8192) // 2
        num_ctx = _best_ctx_fitting(weights_mb or 0, ram_budget, params_b or 7.0, "f16") or CONTEXT_LADDER[0]
        return {**base, "fit": "cpu_only", "notes": [
            "No GPU — running on CPU only. Flash attention and KV cache "
            "quantization are GPU-side and won't help here.",
        ], "per_request": {
            "num_gpu": 0, "num_thread": max(1, min(cpu_cores, 16)), "num_ctx": num_ctx,
        }, "server": {"OLLAMA_NUM_PARALLEL": "1", "OLLAMA_KEEP_ALIVE": "30m"}}

    if weights_mb is None or params_b is None:
        return {**base, "fit": "unknown", "per_request": {"num_gpu": 999}, "server": {}, "notes": [
            "Couldn't determine this model's size, so context/bitrate can't be sized — "
            "showing a generic \"use the GPU\" setting only.",
        ]}

    budget = vram_total_mb - _RESERVE_MB

    if weights_mb < budget:
        notes: list[str] = []
        chosen_ctx = _best_ctx_fitting(weights_mb, budget, params_b, "f16")
        chosen_kv = "f16"
        if chosen_ctx is None:
            chosen_ctx = _best_ctx_fitting(weights_mb, budget, params_b, "q8_0")
            chosen_kv = "q8_0"
            if chosen_ctx is not None and chosen_ctx < 4096:
                q4_ctx = _best_ctx_fitting(weights_mb, budget, params_b, "q4_0")
                if q4_ctx is not None and q4_ctx > chosen_ctx:
                    chosen_ctx, chosen_kv = q4_ctx, "q4_0"
        if chosen_ctx is None:
            chosen_ctx, chosen_kv = CONTEXT_LADDER[0], "q4_0"
            notes.append(
                "This model barely fits — even the smallest context size is tight. "
                "Consider a smaller model or a more aggressive quantization."
            )
        volta = _volta_note(hardware)
        if volta:
            notes.append(volta)
        server = {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_KEEP_ALIVE": "30m",
                  "OLLAMA_MAX_LOADED_MODELS": "2" if vram_total_mb >= 24576 else "1"}
        if chosen_kv != "f16":
            server["OLLAMA_KV_CACHE_TYPE"] = chosen_kv
            notes.append(
                f"Using {chosen_kv} KV cache quantization to fit a longer context — "
                "needs flash attention, which is already recommended above."
            )
        return {**base, "fit": "full_gpu", "per_request": {
            "num_gpu": 999, "num_batch": 512, "num_ctx": chosen_ctx,
        }, "server": server, "notes": notes}

    partial_notes = [
        "Only part of this model fits — Ollama will split it across GPU and RAM "
        "automatically and run slower. Leave GPU layers blank.",
    ]
    volta = _volta_note(hardware)
    if volta:
        partial_notes.append(volta)
    return {**base, "fit": "partial_gpu", "per_request": {"num_ctx": 4096}, "server": {
        "OLLAMA_KEEP_ALIVE": "5m", "OLLAMA_MAX_LOADED_MODELS": "1",
    }, "notes": partial_notes}
