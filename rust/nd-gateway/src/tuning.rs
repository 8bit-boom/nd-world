//! app/ollama_tuning.py's hardware detection and settings recommendations behind `GET /api/ai/hardware` (Settings > System's "Detected
//! hardware" panel): the host's CPU and RAM from /proc, the GPU from nvidia-smi or AMD's sysfs, a GM-entered VRAM override or a planning
//! preset, and per installed model a starting-point bundle (the context that fits, KV-cache quantization, the Unsloth quant tier). With a
//! Studio backend, `/v1/models` names the models (no sizes) and reports none resident, exactly as app/llm_client.py reads it. The
//! server env file (Bucket A) stays with the Settings page, which is Python's.

use axum::body::Body;
use axum::http::{HeaderMap, Response, Uri};
use serde_json::{json, Map, Value};
use std::sync::Arc;
use std::time::Duration;

use crate::config::Config;

const CONTEXT_LADDER: [i64; 6] = [2048, 4096, 8192, 16384, 32768, 65536];
const KV_MB_PER_1K_F16: [(f64, i64); 5] = [(4.0, 64), (9.0, 128), (20.0, 208), (40.0, 320), (1e9, 512)];

/// GPU_PRESETS as the Settings page gets it: {key: {"name", "vram_mb"}}
pub fn gpu_presets() -> serde_json::Value {
    serde_json::Value::Object(GPU_PRESETS.iter().map(|(k, n, v)| (k.to_string(), serde_json::json!({"name": n, "vram_mb": v}))).collect())
}

/// GPU_PRESETS: (key, name, vram MB)
const GPU_PRESETS: [(&str, &str, i64); 3] = [("tesla_t10_16gb", "NVIDIA Tesla T10 16GB", 16384), ("v100_16gb", "NVIDIA Tesla V100 16GB", 16384),
                                             ("v100_32gb", "NVIDIA Tesla V100 32GB", 32768)];

/// `_read_proc_cpuinfo`: (model name, core count)
fn cpuinfo(raw: Option<&str>) -> (String, Option<i64>) {
    let Some(raw) = raw else { return (String::new(), None) };
    let (mut model, mut count) = (String::new(), 0);
    for line in raw.lines() {
        if model.is_empty() && line.starts_with("model name") {
            model = line.split_once(':').map_or("", |(_, v)| v).trim().to_string();
        }
        if line.starts_with("processor") {
            count += 1;
        }
    }
    (model, (count > 0).then_some(count))
}

/// `_read_proc_meminfo`: (MemTotal, MemAvailable) in MB
fn meminfo(raw: Option<&str>) -> (Option<i64>, Option<i64>) {
    let Some(raw) = raw else { return (None, None) };
    let (mut total, mut avail) = (None, None);
    for line in raw.lines() {
        let Some((key, rest)) = line.split_once(':') else { continue };
        let rest = rest.trim();
        if let Some(n) = rest.strip_suffix("kB").and_then(|n| n.trim().parse::<i64>().ok()) {
            match key.trim() {
                "MemTotal" => total = Some(n / 1024),
                "MemAvailable" => avail = Some(n / 1024),
                _ => {}
            }
        }
    }
    (total, avail)
}

/// `len(os.sched_getaffinity(0))`: the CPUs this process may run on
fn affinity() -> Option<i64> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    let list = status.lines().find_map(|l| l.strip_prefix("Cpus_allowed_list:"))?.trim().to_string();
    let mut n = 0;
    for part in list.split(',').filter(|p| !p.is_empty()) {
        n += match part.split_once('-') {
            Some((a, b)) => b.trim().parse::<i64>().ok()? - a.trim().parse::<i64>().ok()? + 1,
            None => 1,
        };
    }
    Some(n)
}

fn on_path(exe: &str) -> bool {
    std::env::var_os("PATH").is_some_and(|p| std::env::split_paths(&p).any(|d| {
        let f = d.join(exe);
        std::fs::metadata(&f).is_ok_and(|m| m.is_file() && std::os::unix::fs::PermissionsExt::mode(&m.permissions()) & 0o111 != 0)
    }))
}

/// `_detect_nvidia_gpus`
async fn nvidia_gpus() -> Vec<Value> {
    if !on_path("nvidia-smi") {
        return vec![];
    }
    let run = tokio::process::Command::new("nvidia-smi").args(["--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        .stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null()).kill_on_drop(true).output();
    let Ok(Ok(out)) = tokio::time::timeout(Duration::from_secs(5), run).await else { return vec![] };
    if !out.status.success() {
        return vec![];
    }
    let mut gpus = vec![];
    for line in String::from_utf8_lossy(&out.stdout).lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let (name, mem) = line.rsplit_once(',').unwrap_or(("", line));
        let Ok(vram) = mem.trim().parse::<i64>() else { continue };
        gpus.push(json!({"vendor": "nvidia", "name": name.trim(), "vram_mb": vram}));
    }
    gpus
}

/// `_detect_amd_gpus`: amdgpu's VRAM total per card whose PCI vendor is AMD (0x1002)
fn amd_gpus() -> Vec<Value> {
    let Ok(cards) = std::fs::read_dir("/sys/class/drm") else { return vec![] };
    let mut paths: Vec<String> = cards.flatten().map(|e| e.file_name().to_string_lossy().to_string()).filter(|n| n.starts_with("card"))
        .map(|n| format!("/sys/class/drm/{n}/device/mem_info_vram_total")).filter(|p| std::path::Path::new(p).exists()).collect();
    paths.sort();
    let mut gpus = vec![];
    for p in paths {
        let dir = std::path::Path::new(&p).parent().map(|d| d.to_path_buf()).unwrap_or_default();
        let Ok(vendor) = std::fs::read_to_string(dir.join("vendor")) else { continue };
        if vendor.trim().to_lowercase() != "0x1002" {
            continue;
        }
        let Some(bytes) = std::fs::read_to_string(&p).ok().and_then(|t| t.trim().parse::<i64>().ok()) else { continue };
        gpus.push(json!({"vendor": "amd", "name": "AMD GPU", "vram_mb": bytes / (1024 * 1024)}));
    }
    gpus
}

/// `_volta_note`
fn volta_note(gpus: &[Value]) -> Option<String> {
    gpus.iter().any(|g| {
        let n = g.get("name").map(|v| match v { Value::String(s) => s.to_lowercase(), Value::Null => String::new(), o => o.to_string().to_lowercase() }).unwrap_or_default();
        n.contains("v100") || n.contains("volta") || n.contains("titan v")
    }).then(|| "Volta-class GPU detected: flash attention and q8_0 KV cache are supported and recommended, but CUDA 13 dropped Volta — if a future \
Ollama update stops detecting this GPU, pin the last CUDA 12-based ollama image tag (see docs/GPU_SETUP.md in the repo).".to_string())
}

/// `detect_hardware(vram_override_mb, gpu_preset)` for a Studio backend (nothing is reported resident with a size, so there is no
/// lower bound from loaded models)
pub async fn detect_hardware(vram_override_mb: Option<i64>, gpu_preset: &str) -> Value {
    let mut notes: Vec<String> = vec![];
    let (cpu_model, cpu_cores) = cpuinfo(std::fs::read_to_string("/proc/cpuinfo").ok().as_deref());
    let (ram_total, ram_avail) = meminfo(std::fs::read_to_string("/proc/meminfo").ok().as_deref());
    let mut gpus = nvidia_gpus().await;
    let mut via = if gpus.is_empty() { None } else { Some("nvidia-smi") };
    if gpus.is_empty() {
        gpus = amd_gpus();
        via = if gpus.is_empty() { None } else { Some("amd-sysfs") };
    }
    let (mut vram_total, mut source) = (None, "none".to_string());
    if let Some(v) = vram_override_mb.filter(|v| *v != 0) {
        vram_total = Some(v);
        source = "manual".into();
    } else if !gpus.is_empty() {
        vram_total = Some(gpus.iter().filter_map(|g| g["vram_mb"].as_i64()).filter(|v| *v != 0).sum::<i64>());
        source = via.unwrap_or("none").into();
    } else if let Some((_, name, vram)) = GPU_PRESETS.iter().find(|(k, _, _)| !gpu_preset.is_empty() && *k == gpu_preset) {
        gpus = vec![json!({"vendor": "nvidia", "name": name, "vram_mb": vram})];
        vram_total = Some(*vram);
        source = "preset".into();
        notes.push(format!("Simulating {name} — no physical GPU was detected here, so this is a planning preset for a card you intend to install, \
not a real reading. Switch to \"none\" once the card is in and being auto-detected."));
    }
    if source == "none" {
        notes.push("nd-world's own container can't see a GPU directly, and SwarmUI (if configured) didn't report one either. That's normal if \
only the \"ollama\"/\"swarmui\" services were given GPU access in docker-compose.yml — enter your card's VRAM below, or give the \"world\" \
service the same GPU access if you'd rather this auto-detect.".into());
    }
    if let Some(v) = volta_note(&gpus) {
        notes.push(v);
    }
    json!({"cpu_model": cpu_model, "cpu_cores": cpu_cores, "cpu_affinity": affinity(), "ram_total_mb": ram_total, "ram_available_mb": ram_avail,
        "gpus": gpus, "vram_total_mb": vram_total, "vram_source": source, "vram_is_lower_bound": false, "notes": notes})
}

/// `model_params_b(model, parameter_size, size_bytes)` with what a Studio backend knows (no parameter size, no file size): a
/// "8b" / "32B" in the model's own name
fn model_params_b(model: &str) -> Option<f64> {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"(\d+(?:\.\d+)?)\s*[bB]\b").unwrap());
    re.captures(model).and_then(|c| c[1].parse::<f64>().ok())
}

fn kv_mb_per_1k(params_b: f64) -> i64 {
    KV_MB_PER_1K_F16.iter().find(|(upper, _)| params_b <= *upper).map_or(512, |(_, mb)| *mb)
}

/// `_best_ctx_fitting`
fn best_ctx_fitting(weights_mb: i64, budget_mb: f64, params_b: f64, kv_scale: f64) -> Option<i64> {
    let per_1k = kv_mb_per_1k(params_b) as f64 * kv_scale;
    CONTEXT_LADDER.iter().copied().filter(|ctx| weights_mb as f64 + per_1k * (*ctx as f64 / 1024.0) <= budget_mb).last()
}

/// `_unsloth_recommendation` (no weights size: the guess from the parameter count)
fn unsloth_recommendation(model: &str, params_b: Option<f64>, vram_total: Option<i64>) -> Value {
    let mut rec = Map::new();
    rec.insert("model".into(), json!(model));
    let Some(p) = params_b.filter(|p| *p != 0.0) else {
        rec.insert("notes".into(), json!(["Couldn't determine this model's size — no quant recommendation."]));
        return Value::Object(rec);
    };
    let w = (p * 1000.0 * 0.6).trunc() as i64;
    let (quant, first) = match vram_total.filter(|v| *v != 0) {
        Some(v) if w as f64 >= v as f64 * 0.75 => ("a 4-bit quant (e.g. UD-IQ4_NL / Q4_K_M)", format!("~{w} MB of weights against {v} MB VRAM is tight — a 4-bit \
quant is the realistic tier, and expect the image model to evict this model on a single card (Studio auto-swaps; a chat→image→chat round trip \
costs a model load each way).")),
        Some(v) if w as f64 >= v as f64 * 0.4 => ("a 4–5-bit quant", format!("~{w} MB of weights fits {v} MB VRAM with room for context at a 4–5-bit quant.")),
        _ => ("an 8-bit quant (e.g. Q8_0)", format!("~{w} MB of weights fits comfortably — an 8-bit quant preserves quality.")),
    };
    rec.insert("quant".into(), json!(quant));
    rec.insert("notes".into(), json!([first, "Set the load-time context in Studio's per-model settings to match LLM_CONTEXT_TOKENS (default 16384). \
Precision fp16 on any pre-Ampere card — Volta (V100) AND Turing (T4/T10) have no bf16/fp8 kernels — but flash attention differs: disable it on \
a V100, leave it on for a Turing card (sm_75 is where llama.cpp's flash-attention support starts)."]));
    Value::Object(rec)
}

/// `recommend_settings(model=..., hardware=..., parameter_size="", size_bytes=None, imagegen_reserve_mb=0)`
pub fn recommend_settings(model: &str, hardware: &Value) -> Value {
    let params_b = model_params_b(model);
    let vram = hardware["vram_total_mb"].as_i64();
    let ram = hardware["ram_total_mb"].as_i64();
    let cores = hardware["cpu_cores"].as_i64().filter(|c| *c != 0).unwrap_or(4);
    let mut out = Map::new();
    out.insert("model".into(), json!(model));
    out.insert("params_b".into(), json!(params_b));
    out.insert("weights_mb".into(), Value::Null);
    let unsloth = unsloth_recommendation(model, params_b, vram);
    match vram {
        None => {
            out.insert("fit".into(), json!("unknown"));
            out.insert("per_request".into(), json!({}));
            out.insert("server".into(), json!({}));
            out.insert("notes".into(), json!(["No GPU was detected and no VRAM was entered below — enter your card's total VRAM (or 0 if you have none) \
for a real recommendation."]));
            out.insert("unsloth".into(), unloth_for(None, model, params_b));
        }
        Some(v) if v <= 0 => {
            let budget = (ram.filter(|r| *r != 0).unwrap_or(8192)).div_euclid(2);
            let ctx = best_ctx_fitting(0, budget as f64, params_b.filter(|p| *p != 0.0).unwrap_or(7.0), 1.0).unwrap_or(CONTEXT_LADDER[0]);
            out.insert("fit".into(), json!("cpu_only"));
            out.insert("notes".into(), json!(["No GPU — running on CPU only. Flash attention and KV cache quantization are GPU-side and won't help here."]));
            out.insert("per_request".into(), json!({"num_gpu": 0, "num_thread": cores.clamp(1, 16).max(1), "num_ctx": ctx}));
            out.insert("server".into(), json!({"OLLAMA_NUM_PARALLEL": "1", "OLLAMA_KEEP_ALIVE": "30m"}));
            out.insert("unsloth".into(), unsloth);
        }
        Some(_) => {
            // a Studio backend reports no file size, so nothing can be sized past "use the GPU"
            out.insert("fit".into(), json!("unknown"));
            out.insert("per_request".into(), json!({"num_gpu": 999}));
            out.insert("server".into(), json!({}));
            out.insert("notes".into(), json!(["Couldn't determine this model's size, so context/bitrate can't be sized — showing a generic \"use the GPU\" setting only."]));
            out.insert("unsloth".into(), unsloth);
        }
    }
    Value::Object(out)
}

fn unloth_for(vram: Option<i64>, model: &str, params_b: Option<f64>) -> Value {
    unsloth_recommendation(model, params_b, vram)
}

/// `GET /api/ai/hardware[?preset=...]` for a GM, with a Studio backend; None for anything else (Python's)
pub async fn hardware(cfg: Arc<Config>, uri: Uri, headers: HeaderMap) -> Option<Response<Body>> {
    let preset_q = match uri.query() {
        None => None,
        Some(q) => {
            let pairs: Vec<(String, String)> = url_pairs(q);
            if pairs.iter().any(|(k, _)| k != "preset") || pairs.len() > 1 {
                return None;
            }
            pairs.into_iter().next().map(|(_, v)| v)
        }
    };
    let (c, u, h) = (cfg.clone(), uri.clone(), headers.clone());
    let (b, override_mb, saved_preset) = tokio::task::spawn_blocking(move || -> Option<(crate::ai::Backend, Option<i64>, String)> {
        let db = crate::writes::open(&c)?;
        let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs();
        let p = crate::layout::build(&db, &c, "GET", &u, &h, n)?;
        if !p.is_gm {
            return None; // the 403: Python's
        }
        // a Studio key without an Unsloth image model leaves image generation on IMAGEGEN_TYPE: SwarmUI's GPU report is Python's
        if std::env::var("UNSLOTH_IMAGE_MODEL").unwrap_or_default().is_empty() && std::env::var("IMAGEGEN_TYPE").unwrap_or_default() == "swarmui" {
            return None;
        }
        let b = crate::ai::backend(&db)?;
        let (o, preset): (Option<i64>, Option<String>) = db.query_row("SELECT ollama_vram_override_mb, ollama_gpu_preset FROM app_settings ORDER BY id LIMIT 1", [],
            |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
        Some((b, o, preset.unwrap_or_default()))
    }).await.ok()??;
    let preset = preset_q.unwrap_or(saved_preset);
    let hw = detect_hardware(override_mb, &preset).await;
    let models = studio_models(&b).await;
    let models: Vec<Value> = models.into_iter().map(|m| {
        let rec = recommend_settings(&m, &hw);
        json!({"model": m, "size_bytes": null, "parameter_size": "", "quantization_level": "", "recommendation": rec})
    }).collect();
    crate::edits::json_response(&json!({"hardware": hw, "models": models}))
}

/// `installed_models_detail()` on a Studio backend: `/v1/models`' ids ([] on any failure)
async fn studio_models(b: &crate::ai::Backend) -> Vec<String> {
    use http_body_util::BodyExt;
    let Some(req) = crate::ai::backend_request(b, "GET", "/v1/models", None) else { return vec![] };
    let fetch = async {
        let resp = crate::ai::client().request(req).await.ok()?;
        if resp.status().as_u16() >= 400 {
            return None;
        }
        let bytes = resp.into_body().collect().await.ok()?.to_bytes();
        let v: Value = serde_json::from_slice(&bytes).ok()?;
        Some(v.get("data").and_then(|d| d.as_array()).map(|a| a.iter().filter_map(|m| m.as_object())
            .filter_map(|m| m.get("id").filter(|i| crate::edits::py_truthy_pub(Some(i))).map(|i| match i { Value::String(s) => s.clone(), o => o.to_string() })).collect())
            .unwrap_or_default())
    };
    tokio::time::timeout(Duration::from_secs(30), fetch).await.ok().flatten().unwrap_or_default()
}

fn url_pairs(q: &str) -> Vec<(String, String)> {
    q.split('&').filter(|p| !p.is_empty()).map(|p| {
        let (k, v) = p.split_once('=').unwrap_or((p, ""));
        let dec = |s: &str| percent_encoding::percent_decode_str(&s.replace('+', " ")).decode_utf8_lossy().to_string();
        (dec(k), dec(v))
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proc_readers() {
        assert_eq!(cpuinfo(Some("processor\t: 0\nmodel name\t: Ryzen 9\nprocessor\t: 1\nmodel name\t: other\n")), ("Ryzen 9".into(), Some(2)));
        assert_eq!(cpuinfo(Some("")), (String::new(), None));
        assert_eq!(meminfo(Some("MemTotal:       16384000 kB\nMemFree: 1 kB\nMemAvailable:    8192000 kB\nBad: x kB\n")), (Some(16000), Some(8000)));
    }

    #[test]
    fn recommendations() {
        let hw = json!({"vram_total_mb": 0, "ram_total_mb": 32000, "cpu_cores": 24});
        let r = recommend_settings("qwen2.5:32b", &hw);
        assert_eq!(r["fit"], "cpu_only");
        assert_eq!(r["per_request"], json!({"num_gpu": 0, "num_thread": 16, "num_ctx": 32768}));
        assert_eq!(r["unsloth"]["quant"], "an 8-bit quant (e.g. Q8_0)");
        let r = recommend_settings("gemma", &json!({"vram_total_mb": 16384}));
        assert_eq!((r["fit"].clone(), r["params_b"].clone(), r["unsloth"]["notes"][0].clone()),
                   (json!("unknown"), Value::Null, json!("Couldn't determine this model's size — no quant recommendation.")));
        let r = recommend_settings("llama-70b", &json!({"vram_total_mb": 16384}));
        assert_eq!(r["unsloth"]["quant"], "a 4-bit quant (e.g. UD-IQ4_NL / Q4_K_M)");
    }
}

pub fn gpu_preset_keys() -> Vec<&'static str> {
    GPU_PRESETS.iter().map(|(k, _, _)| *k).collect()
}

// ── the server-env spec and its sanitizer (app/ollama_tuning.py's SERVER_ENV_SPEC / sanitize_server_env) ─────

pub struct ServerEnvField {
    pub env_key: &'static str,
    pub suffix: &'static str,
    pub kind: &'static str,
    /// a "choice" field's allowed values; an "int" field's (lo, hi) as text; empty for duration / token
    pub spec: &'static [&'static str],
}

/// SERVER_ENV_SPEC, verbatim
pub const SERVER_ENV_SPEC: &[ServerEnvField] = &[
    ServerEnvField { env_key: "OLLAMA_FLASH_ATTENTION", suffix: "flash_attention", kind: "choice", spec: &["1", "0"] },
    ServerEnvField { env_key: "OLLAMA_KV_CACHE_TYPE", suffix: "kv_cache_type", kind: "choice", spec: &["f16", "q8_0", "q4_0"] },
    ServerEnvField { env_key: "OLLAMA_NUM_PARALLEL", suffix: "num_parallel", kind: "int", spec: &["0", "32"] },
    ServerEnvField { env_key: "OLLAMA_MAX_LOADED_MODELS", suffix: "max_loaded_models", kind: "int", spec: &["0", "16"] },
    ServerEnvField { env_key: "OLLAMA_MAX_QUEUE", suffix: "max_queue", kind: "int", spec: &["1", "4096"] },
    ServerEnvField { env_key: "OLLAMA_KEEP_ALIVE", suffix: "keep_alive", kind: "duration", spec: &[] },
    ServerEnvField { env_key: "OLLAMA_CONTEXT_LENGTH", suffix: "context_length", kind: "int", spec: &["256", "1048576"] },
    ServerEnvField { env_key: "OLLAMA_LOAD_TIMEOUT", suffix: "load_timeout", kind: "duration", spec: &[] },
    ServerEnvField { env_key: "OLLAMA_GPU_OVERHEAD", suffix: "gpu_overhead", kind: "int", spec: &["0", "137438953472"] },
    ServerEnvField { env_key: "OLLAMA_SCHED_SPREAD", suffix: "sched_spread", kind: "choice", spec: &["1", "0"] },
    ServerEnvField { env_key: "OLLAMA_DEBUG", suffix: "debug", kind: "choice", spec: &["1", "2"] },
    ServerEnvField { env_key: "OLLAMA_IGPU_ENABLE", suffix: "igpu_enable", kind: "choice", spec: &["1", "0"] },
    ServerEnvField { env_key: "OLLAMA_VULKAN", suffix: "vulkan", kind: "choice", spec: &["1", "0"] },
    ServerEnvField { env_key: "HSA_OVERRIDE_GFX_VERSION", suffix: "hsa_override_gfx", kind: "token", spec: &[] },
    ServerEnvField { env_key: "ROCR_VISIBLE_DEVICES", suffix: "rocr_visible", kind: "token", spec: &[] },
    ServerEnvField { env_key: "CUDA_VISIBLE_DEVICES", suffix: "cuda_visible", kind: "token", spec: &[] },
];

pub const ENV_FILENAME: &str = "ollama.env";

/// `_SAFE_VALUE`
fn srv_safe_value(v: &str) -> bool {
    !v.is_empty() && v.len() <= 64 && v.chars().all(|c| c.is_ascii_alphanumeric() || ".,:+/-_".contains(c))
}

/// `_DURATION`
fn srv_duration(v: &str) -> bool {
    let body = v.strip_prefix('-').unwrap_or(v);
    let (num, unit) = body.split_at(body.find(|c: char| c.is_ascii_alphabetic()).unwrap_or(body.len()));
    let ok_num = !num.is_empty() && num.parse::<f64>().is_ok();
    matches!((ok_num, unit), (true, "") | (true, "ns") | (true, "us") | (true, "ms") | (true, "s") | (true, "m") | (true, "h"))
}

/// `sanitize_server_env`: `raw` is the form's ollama_srv_* values keyed by env var name → (clean, errors), Python's
/// messages verbatim. A key not in the spec is dropped silently (the allowlist is a security boundary).
pub fn sanitize_server_env(raw: &Value) -> (Value, Vec<String>) {
    let mut clean = serde_json::Map::new();
    let mut errors = vec![];
    for f in SERVER_ENV_SPEC {
        let Some(value) = raw.get(f.env_key) else { continue };
        let value = match value {
            Value::String(t) => t.trim().to_string(),
            Value::Number(n) => n.to_string(),
            _ => continue,
        };
        if value.is_empty() {
            continue;
        }
        let mut stored: Option<String> = None;
        match f.kind {
            "choice" => {
                if !f.spec.contains(&value.as_str()) {
                    errors.push(format!("{} must be one of: {}", f.env_key, f.spec.join(", ")));
                    continue;
                }
            }
            "int" => {
                let (lo, hi) = (f.spec[0].parse::<i64>().unwrap_or(0), f.spec[1].parse::<i64>().unwrap_or(i64::MAX));
                match value.parse::<i64>() {
                    Err(_) => {
                        errors.push(format!("{} must be a whole number", f.env_key));
                        continue;
                    }
                    Ok(parsed) if !(lo <= parsed && parsed <= hi) => {
                        errors.push(format!("{} must be between {} and {}", f.env_key, lo, hi));
                        continue;
                    }
                    Ok(parsed) => stored = Some(parsed.to_string()),
                }
            }
            "duration" => {
                if !srv_duration(&value) {
                    errors.push(format!("{} must be a duration like '30m', '5h', '-1', or '0'", f.env_key));
                    continue;
                }
            }
            _ => {}
        }
        if !srv_safe_value(&value) {
            errors.push(format!("{} contains characters that aren't allowed", f.env_key));
            continue;
        }
        clean.insert(f.env_key.into(), json!(stored.unwrap_or(value)));
    }
    (Value::Object(clean), errors)
}

/// `render_env_file`: deterministic SERVER_ENV_KEYS order
pub fn render_env_file(values: &Value) -> String {
    let keys = ["OLLAMA_FLASH_ATTENTION", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_MAX_QUEUE",
        "OLLAMA_KEEP_ALIVE", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_LOAD_TIMEOUT", "OLLAMA_GPU_OVERHEAD", "OLLAMA_SCHED_SPREAD",
        "OLLAMA_DEBUG", "OLLAMA_IGPU_ENABLE", "OLLAMA_VULKAN", "HSA_OVERRIDE_GFX_VERSION", "ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"];
    let mut lines = vec![
        "# Generated by nd-world (Settings > System > Ollama server tuning).".to_string(),
        "# Do not edit by hand -- it is overwritten on every save.".to_string(),
        "# Sourced by the \"ollama\" service at container start; see docker-compose.yml.".to_string(),
    ];
    for key in keys {
        if let Some(v) = values.get(key).and_then(|v| v.as_str()) {
            lines.push(format!("{key}={v}"));
        }
    }
    lines.join("\n") + "\n"
}

