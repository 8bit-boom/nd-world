//! `GET /settings` and `GET /settings/visibility-rows` (app/main.py `settings_page`, `_settings_context`, `settings_visibility_rows`).
//!
//! Most of the page is the app_settings row and the active world. The parts that describe the *server process* are the gateway's own:
//! the resource sizing (app/workers.py `summary`, read from the same machine, environment and saved res_* settings), the live page-build
//! limit (`http_perf.current_render_limit()`: the gateway has no Python render gate, so None, which the page shows as the starting value)
//! and the Diagnostics tab's watchdog state (Python's watchdog thread, event-loop heartbeat and SQLAlchemy pool do not exist here, so
//! "not started" / "inactive" / "unavailable"; the event journal and dump files are read from the same directory).
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, Uri};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::pages::Pages;

fn env(k: &str) -> String {
    std::env::var(k).unwrap_or_default()
}

/// workers._env_int: a positive int, else None
fn env_int(k: &str) -> Option<i64> {
    let raw = env(k);
    let v: i64 = if raw.is_empty() { 0 } else { raw.trim().parse().ok()? };
    (v > 0).then_some(v)
}

fn env_float(k: &str, default: f64) -> f64 {
    let raw = env(k);
    if raw.is_empty() {
        return default;
    }
    raw.trim().parse().unwrap_or(default)
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

/// workers.usable_cpus: the CPUs this process may run on, capped by a cgroup CPU limit
fn usable_cpus(cgroup_root: &Path) -> i64 {
    let mut n = unsafe {
        let mut set: libc::cpu_set_t = std::mem::zeroed();
        if libc::sched_getaffinity(0, std::mem::size_of::<libc::cpu_set_t>(), &mut set) == 0 {
            libc::CPU_COUNT(&set) as i64
        } else {
            host_cpus()
        }
    };
    let read = |p: PathBuf| std::fs::read_to_string(p).ok();
    let quota = match read(cgroup_root.join("cpu.max")).map(|s| s.split_whitespace().map(str::to_string).collect::<Vec<_>>()) {
        Some(parts) if parts.len() >= 2 => match parts[0].as_str() {
            "max" => None,
            q => match (q.parse::<i64>(), parts[1].parse::<i64>()) {
                (Ok(q), Ok(p)) if p != 0 => Some(q as f64 / p as f64),
                _ => v1_quota(cgroup_root),
            },
        },
        _ => v1_quota(cgroup_root),
    };
    if let Some(q) = quota.filter(|q| *q != 0.0) {
        n = n.min((q.ceil() as i64).max(1));
    }
    n.max(1)
}

fn v1_quota(root: &Path) -> Option<f64> {
    let q: i64 = std::fs::read_to_string(root.join("cpu").join("cpu.cfs_quota_us")).ok()?.trim().parse().ok()?;
    let p: i64 = std::fs::read_to_string(root.join("cpu").join("cpu.cfs_period_us")).ok()?.trim().parse().ok()?;
    (q > 0 && p > 0).then(|| q as f64 / p as f64)
}

fn host_cpus() -> i64 {
    let n = unsafe { libc::sysconf(libc::_SC_NPROCESSORS_ONLN) };
    if n > 0 { n as i64 } else { 1 }
}

/// workers.usable_ram_gb: the machine's RAM, or a lower cgroup memory limit
fn usable_ram_gb(cgroup_root: &Path) -> f64 {
    let (page, pages) = unsafe { (libc::sysconf(libc::_SC_PAGESIZE), libc::sysconf(libc::_SC_PHYS_PAGES)) };
    let mut ram = if page > 0 && pages > 0 { page as f64 * pages as f64 / (1u64 << 30) as f64 } else { 0.0 };
    for f in [cgroup_root.join("memory.max"), cgroup_root.join("memory").join("memory.limit_in_bytes")] {
        let Ok(v) = std::fs::read_to_string(&f) else { continue };
        let v = v.trim();
        if v != "max" {
            let Ok(n) = v.parse::<i64>() else { continue };
            if (n as f64) < 2f64.powi(60) {
                let g = n as f64 / (1u64 << 30) as f64;
                ram = if ram != 0.0 { ram.min(g) } else { g };
            }
        }
        break;
    }
    ram
}

/// workers.summary() with the saved res_* settings as the overrides (workers.apply is fed from the same columns)
fn resources(st: &Map<String, Value>) -> Value {
    let root = Path::new("/sys/fs/cgroup");
    let over = |k: &str| st.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0);
    let cpus = usable_cpus(root);
    let ram = usable_ram_gb(root);
    let auto_image = (cpus / 2).clamp(1, 8);
    let image = match env_int("ND_IMAGE_WORKERS").or(over("res_image_workers")) {
        Some(n) => n.min(32),
        None => auto_image,
    };
    let render_fixed = env_int("ND_RENDERS").or(over("res_render_slots"));
    let budget = match over("res_memory_mb") {
        Some(mb) => mb as f64 / 1024.0,
        None => ram,
    };
    let tiles = match env_int("ND_TILE_WORKERS") {
        Some(n) => n.min(8),
        None => if budget >= 16.0 { 2 } else { 1 },
    };
    json!({
        "cpus": cpus, "host_cpus": host_cpus(), "ram_gb": round1(ram),
        "image_workers": image, "auto_image_workers": auto_image,
        "render_slots": render_fixed.map_or(2, |n| n.clamp(1, 16)), "render_auto": render_fixed.is_none(), "render_slots_max": (cpus / 2).clamp(2, 8),
        "tile_workers": tiles, "memory_budget_gb": round1(budget),
        "env": {"ND_IMAGE_WORKERS": env_int("ND_IMAGE_WORKERS").is_some(), "ND_RENDERS": env_int("ND_RENDERS").is_some(),
                "ND_TILE_WORKERS": env_int("ND_TILE_WORKERS").is_some()},
    })
}

/// diagnostics.diag_dir(): ND_DIAG_DIR, else "diagnostics" next to the database; created on demand, None when it cannot be
fn diag_dir(cfg: &Config) -> Option<PathBuf> {
    let raw = env("ND_DIAG_DIR");
    let raw = raw.trim();
    let p = if raw.is_empty() { cfg.db_path.parent()?.join("diagnostics") } else { PathBuf::from(raw) };
    std::fs::create_dir_all(&p).ok()?;
    Some(p)
}

fn dump_name_ok(name: &str) -> bool {
    // ^wedge-\d{8}-\d{6}(?:-\d+)?\.txt$
    let Some(rest) = name.strip_prefix("wedge-").and_then(|r| r.strip_suffix(".txt")) else { return false };
    let parts: Vec<&str> = rest.split('-').collect();
    let digits = |s: &str, n: Option<usize>| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) && n.map_or(true, |n| s.len() == n);
    match parts.as_slice() {
        [d, t] => digits(d, Some(8)) && digits(t, Some(6)),
        [d, t, x] => digits(d, Some(8)) && digits(t, Some(6)) && digits(x, None),
        _ => false,
    }
}

fn diagnostics(cfg: &Config) -> (Value, Value, Value) {
    let d = diag_dir(cfg);
    let disabled = matches!(env("ND_DIAG_DISABLED").trim().to_lowercase().as_str(), "1" | "true" | "yes" | "on");
    let status = json!({
        "enabled": !disabled, "dir": d.as_ref().map(|p| p.to_string_lossy().into_owned()),
        "stall_seconds": env_float("ND_DIAG_STALL_SECONDS", 15.0), "dump_cooldown_seconds": env_float("ND_DIAG_DUMP_COOLDOWN_SECONDS", 300.0),
        "pool_warn_cooldown_seconds": env_float("ND_DIAG_POOL_WARN_COOLDOWN_SECONDS", 300.0),
        "pool_checked_out": null, "pool_size": null, "watchdog_thread_alive": false, "heartbeat_active": false,
    });
    let events: Vec<Value> = d
        .as_ref()
        .and_then(|d| std::fs::read(d.join("events.log")).ok())
        .map(|b| {
            let text = String::from_utf8_lossy(&b).into_owned();
            let lines: Vec<&str> = nd_render::text::py_splitlines(&text);
            lines[lines.len().saturating_sub(200)..].iter().map(|l| json!(l)).collect()
        })
        .unwrap_or_default();
    let mut dumps: Vec<(f64, Value)> = vec![];
    if let Some(rd) = d.as_ref().and_then(|d| std::fs::read_dir(d).ok()) {
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let Ok(md) = e.metadata() else { continue };
            if !dump_name_ok(&name) || !md.is_file() {
                continue;
            }
            let mtime = md.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map_or(0.0, |d| d.as_secs_f64());
            let secs = mtime.floor() as i64;
            let us = ((mtime - secs as f64) * 1e6).round() as i64;
            let (days, rem) = (secs.div_euclid(86400), secs.rem_euclid(86400));
            let (y, m, dd) = crate::writes::civil_from_days(days);
            let dt = format!("{y:04}-{m:02}-{dd:02} {:02}:{:02}:{:02}.{us:06}", rem / 3600, rem % 3600 / 60, rem % 60);
            dumps.push((mtime, json!({"name": name, "size": md.len(), "modified": {"__dt__": dt}})));
        }
    }
    dumps.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    (status, Value::Array(events), Value::Array(dumps.into_iter().map(|d| d.1).collect()))
}

/// ollama_tuning.SERVER_ENV_SPEC: (env_key, form suffix, kind, spec, help)
fn server_env_spec() -> Value {
    json!([
        ["OLLAMA_FLASH_ATTENTION", "flash_attention", "choice", ["1", "0"], "Auto-on where the backend supports it on current Ollama; force it only to debug."],
        ["OLLAMA_KV_CACHE_TYPE", "kv_cache_type", "choice", ["f16", "q8_0", "q4_0"], "Needs flash attention active; silently falls back to f16 on unsupported architectures."],
        ["OLLAMA_NUM_PARALLEL", "num_parallel", "int", [0, 32], "Concurrent requests per loaded model; each slot costs its own KV cache."],
        ["OLLAMA_MAX_LOADED_MODELS", "max_loaded_models", "int", [0, 16], "Models resident at once, per GPU. Blank = Ollama picks automatically."],
        ["OLLAMA_MAX_QUEUE", "max_queue", "int", [1, 4096], "Queued request limit before Ollama starts rejecting new ones. Blank = 512."],
        ["OLLAMA_KEEP_ALIVE", "keep_alive", "duration", null, "Server default keep-alive, e.g. 5m, 1h, -1 (forever), 0. The per-request setting above overrides it."],
        ["OLLAMA_CONTEXT_LENGTH", "context_length", "int", [256, 1048576], "Server default context length. The per-request num_ctx above overrides it."],
        ["OLLAMA_LOAD_TIMEOUT", "load_timeout", "duration", null, "How long Ollama waits for a model to load. Raise on slow disks or very large models. Blank = 5m."],
        ["OLLAMA_GPU_OVERHEAD", "gpu_overhead", "int", [0, 137438953472i64], "Bytes of VRAM per GPU to leave unused, e.g. for another app sharing the same card."],
        ["OLLAMA_SCHED_SPREAD", "sched_spread", "choice", ["1", "0"], "Spread one model's layers across every visible GPU instead of filling one at a time."],
        ["OLLAMA_DEBUG", "debug", "choice", ["1", "2"], "Verbose (1) or trace (2) server logging."],
        ["OLLAMA_IGPU_ENABLE", "igpu_enable", "choice", ["1", "0"], "Allow integrated GPUs to be used, not just discrete ones."],
        ["OLLAMA_VULKAN", "vulkan", "choice", ["1", "0"], "Vulkan backend (on by default on non-macOS)."],
        ["HSA_OVERRIDE_GFX_VERSION", "hsa_override_gfx", "token", null, "AMD/ROCm GPU compatibility override, e.g. 11.0.0. See .env.example."],
        ["ROCR_VISIBLE_DEVICES", "rocr_visible", "token", null, "AMD/ROCm GPU selector, e.g. 0 or 0,1. See .env.example."],
        ["CUDA_VISIBLE_DEVICES", "cuda_visible", "token", null, "NVIDIA GPU selector, e.g. 0 or 0,1."],
    ])
}

/// ollama_tuning.read_server_env_file: None when the file is missing, else KEY=VALUE lines
fn read_server_env_file(dir: &Path, name: &str) -> Option<Map<String, Value>> {
    let p = dir.join(name);
    if !p.is_file() {
        return None;
    }
    let raw = std::fs::read(&p).ok()?;
    let raw = String::from_utf8(raw).ok()?;
    let mut out = Map::new();
    for line in nd_render::text::py_splitlines(&raw) {
        let line = nd_render::text::py_strip(line);
        if line.is_empty() || line.starts_with('#') || !line.contains('=') {
            continue;
        }
        let (k, v) = line.split_once('=')?;
        out.insert(nd_render::text::py_strip(k).to_string(), json!(nd_render::text::py_strip(v)));
    }
    Some(out)
}

/// ollama_tuning.server_env_status(desired)
fn server_env_status(desired: &Value) -> Value {
    let dir = PathBuf::from(std::env::var("OLLAMA_CONFIG_DIR").unwrap_or_else(|_| "/data/ollama-config".into()));
    let writable = std::fs::create_dir_all(&dir).is_ok() && {
        let probe = dir.join(".write_probe");
        let ok = std::fs::write(&probe, "").is_ok();
        let _ = std::fs::remove_file(&probe);
        ok
    };
    let applied = read_server_env_file(&dir, "ollama.env.applied");
    let wired = applied.is_some();
    let pending = wired && applied.as_ref().map(|a| Value::Object(a.clone())) != Some(desired.clone());
    json!({
        "config_dir": dir.to_string_lossy(), "env_path": dir.join("ollama.env").to_string_lossy(), "writable": writable, "wired": wired,
        "pending": pending, "desired": desired, "applied": applied, "restart_command": "docker compose restart ollama",
        "restart_command_truenas": "docker ps | grep ollama   # then: docker restart <that container name>",
    })
}

/// json.loads(x or "{}") that falls back to {} for anything unreadable; `None` = Python would parse what serde cannot (NaN ...)
fn json_obj(v: Option<&Value>) -> Option<Value> {
    let raw = v.and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("{}");
    match serde_json::from_str::<Value>(raw) {
        Ok(v) => Some(v),
        Err(_) if raw.contains("NaN") || raw.contains("Infinity") => None,
        Err(_) => Some(json!({})),
    }
}

fn key_hint(key: &str) -> String {
    let key = nd_render::text::py_strip(key);
    if key.is_empty() {
        return String::new();
    }
    let chars: Vec<char> = key.chars().collect();
    if chars.len() >= 12 { format!("…{}", chars[chars.len() - 4..].iter().collect::<String>()) } else { "…".into() }
}

fn env_bytes(k: &str, default: i64) -> Option<i64> {
    let raw = std::env::var(k).ok();
    match raw {
        None => Some(default),
        Some(s) => s.trim().parse().ok(),
    }
}

/// `_visibility_rows`: (id, name, kind, visible_to_players) by kind and name, and who each entity is opened to
fn visibility_rows(db: &Connection, wid: i64) -> Option<(Value, Value)> {
    let ents = crate::idpages::rows(db, "SELECT id, name, kind, visible_to_players FROM entities WHERE world_id = ?1 ORDER BY kind, name, id", wid)?;
    let mut allowed = Map::new();
    if !ents.is_empty() {
        let mut st = db
            .prepare("SELECT epa.entity_id, epa.user_id FROM entity_player_access epa JOIN entities e ON e.id = epa.entity_id WHERE e.world_id = ?1")
            .ok()?;
        let pairs = st.query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        for (eid, uid) in pairs {
            let e = allowed.entry(eid.to_string()).or_insert_with(|| json!([]));
            if !e.as_array()?.contains(&json!(uid)) {
                e.as_array_mut()?.push(json!(uid));
            }
        }
    }
    Some((Value::Array(ents), json!({ "__intkeys__": allowed })))
}

fn now() -> Option<u64> {
    Some(SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs())
}

/// `GET /settings`
pub fn page_context(db: &Connection, cfg: &Config, method: &str, uri: &Uri, headers: &HeaderMap, tab: &str) -> Option<crate::layout::PageCtx> {
    let mut p = crate::layout::build(db, cfg, method, uri, headers, now()?)?;
    if !p.is_gm {
        return None;
    }
    let st = db.query_row("SELECT * FROM app_settings ORDER BY id LIMIT 1", [], |r| crate::layout::row_map(r)).ok()?;
    let world = p.world.clone();
    let wid = world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    let (mut vis, mut allowed, mut players) = (Value::Null, json!({}), json!([]));
    if let Some(wid) = wid {
        players = Value::Array(
            crate::idpages::rows(
                &db,
                "SELECT users.* FROM users JOIN world_memberships ON world_memberships.user_id = users.id WHERE world_memberships.world_id = ?1 \
                 ORDER BY users.display_name, world_memberships.id",
                wid,
            )?,
        );
        if tab == "visibility" {
            (vis, allowed) = visibility_rows(&db, wid)?;
        }
    }
    let server_env = json_obj(st.get("ollama_server_env_json"))?;
    let overrides = json_obj(st.get("ollama_model_overrides_json"))?;
    let s = |k: &str| st.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let llm_key = Some(s("llm_api_key")).filter(|k| !k.is_empty()).unwrap_or_else(|| env("UNSLOTH_API_KEY"));
    let max_upload = env_bytes("MAX_UPLOAD_BYTES", 20 * 1024 * 1024)?;
    let limits = [
        ("max_upload_mb", "General uploads (images, maps, schematics)", max_upload),
        ("max_gallery_upload_mb", "Gallery albums", max_upload.max(env_bytes("MAX_GALLERY_UPLOAD_BYTES", 500 * 1024 * 1024)?)),
        ("max_video_mb", "Video clips", env_bytes("MAX_VIDEO_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024)?),
        ("max_audio_mb", "Audio library", env_bytes("MAX_AUDIO_UPLOAD_BYTES", 1024 * 1024 * 1024)?),
        ("max_ai_attachment_mb", "AI voice-memo attachments", env_bytes("MAX_AUDIO_UPLOAD_BYTES", 1024 * 1024 * 1024)?),
    ];
    let world_ref = world.as_ref();
    let kinds = nd_perm::sections::effective_kinds(world_ref.and_then(|w| w.get("custom_kinds_json")).and_then(|v| v.as_str()))?;
    let mut no_player: Vec<String> = nd_perm::sections::NO_PLAYER_EDIT.iter().map(|s| s.to_string()).collect();
    if world.is_some() {
        no_player.extend(kinds.iter().map(|k| format!("kind_{k}")));
    }
    no_player.sort();
    no_player.dedup();
    let mut no_assistant: Vec<&str> = nd_perm::sections::NO_ASSISTANT_EDIT.to_vec();
    no_assistant.sort();
    let section_access = match world_ref {
        Some(w) => nd_perm::sections::world_section_access(
            w.get("section_access_json").and_then(|v| v.as_str()),
            w.get("custom_kinds_json").and_then(|v| v.as_str()),
        )?,
        None => json!({}),
    };
    let nav_menus = match world_ref {
        Some(w) => nd_render::app::load_nav_menus(w)?,
        None => json!([]),
    };
    let (diag_status, diag_events, diag_dumps) = diagnostics(cfg);
    let build = env("ND_BUILD");
    let build: String = nd_render::text::py_strip(&build).chars().take(7).collect();
    let active = if ["options", "system", "visibility", "navigation", "diagnostics"].contains(&tab) { tab } else { "options" };
    let strip_slash = |k: &str| env(k).trim_end_matches('/').to_string();
    let c = &mut p.ctx;
    c.insert("resources".into(), resources(&st));
    c.insert("render_now".into(), Value::Null);
    c.insert("app_build".into(), json!(if build.is_empty() { "dev".to_string() } else { build }));
    c.insert("active_tab".into(), json!(active));
    c.insert("env_ollama_model".into(), json!(Some(env("OLLAMA_MODEL")).filter(|m| std::env::var("OLLAMA_MODEL").is_ok() || !m.is_empty()).unwrap_or_else(|| "gemma4:26b".into())));
    c.insert("env_ollama_url".into(), json!(std::env::var("OLLAMA_URL").unwrap_or_else(|_| "http://127.0.0.1:11434".into())));
    c.insert("env_unsloth_url".into(), json!(strip_slash("UNSLOTH_URL")));
    c.insert("env_unsloth_model".into(), json!(env("UNSLOTH_MODEL")));
    c.insert("llm_key_hint".into(), json!(key_hint(&s("llm_api_key"))));
    c.insert("env_unsloth_key_hint".into(), json!(key_hint(&env("UNSLOTH_API_KEY"))));
    c.insert("unsloth_configured".into(), json!(!llm_key.is_empty()));
    c.insert("llm_active".into(), json!(!llm_key.is_empty()));
    let ctx_tokens = std::env::var("LLM_CONTEXT_TOKENS").map(|v| v.trim().parse::<i64>().ok()).unwrap_or(Some(16384))?;
    c.insert("env_llm_context_tokens".into(), json!(ctx_tokens.max(1024)));
    c.insert("character_import_min".into(), json!(2000));
    c.insert("character_import_max".into(), json!(60000));
    c.insert("session_media_default".into(), json!(10));
    c.insert("session_media_hard_max".into(), json!(50));
    c.insert("env_swarmui_external_url".into(), json!(strip_slash("SWARMUI_EXTERNAL_URL")));
    c.insert("env_android_emulator_url".into(), json!(strip_slash("ANDROID_EMULATOR_URL")));
    c.insert("env_editor_external_url".into(), json!(strip_slash("EDITOR_EXTERNAL_URL")));
    c.insert(
        "upload_limit_fields".into(),
        Value::Array(limits.iter().map(|(n, l, b)| json!({"name": n, "label": l, "value": st.get(*n).cloned().unwrap_or(Value::Null), "env_default_mb": b.div_euclid(1024 * 1024)})).collect()),
    );
    c.insert("system_error".into(), Value::Null);
    c.insert("vis_entities".into(), vis);
    c.insert("world_players".into(), players);
    c.insert("allowed_by_entity".into(), allowed);
    c.insert("nav_catalog".into(), Value::Array(nd_render::app::build_catalog(world_ref)?));
    c.insert("initial_nav_menus".into(), nav_menus);
    c.insert("nav_max_menus".into(), json!(12));
    c.insert("initial_section_access".into(), section_access);
    c.insert("no_player_edit_sections".into(), json!(no_player));
    c.insert("no_assistant_edit_sections".into(), json!(no_assistant));
    c.insert("ollama_server_status".into(), server_env_status(&server_env));
    c.insert("ollama_server_env".into(), server_env);
    c.insert("ollama_server_spec".into(), server_env_spec());
    c.insert("ollama_form_prefix".into(), json!("ollama_srv_"));
    c.insert("ollama_model_overrides".into(), overrides);
    c.insert("ollama_thinking_failures".into(), json!(crate::ai::thinking_failures()));
    c.insert("gpu_presets".into(), crate::tuning::gpu_presets());
    c.insert("diag_status".into(), diag_status);
    c.insert("diag_events".into(), diag_events);
    c.insert("diag_dumps".into(), diag_dumps);
    c.insert("settings".into(), Value::Object(st));
    Some(p)
}

pub fn settings_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let p = page_context(&db, cfg, "GET", uri, headers, &crate::pages::query_param(uri, "tab").unwrap_or_else(|| "options".into()))?;
    crate::pages::render_with(pages, "settings.html", p.ctx, headers)
}

/// `GET /settings/visibility-rows`
pub fn visibility_rows_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now()?)?;
    if !p.is_gm {
        return None;
    }
    let Some(wid) = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()) else { return Some(crate::idpages::refusal(404)) };
    let (vis, allowed) = visibility_rows(&db, wid)?;
    let mut ctx = Map::new();
    for k in ["request", "kind_icons", "__nd"] {
        if let Some(v) = p.ctx.remove(k) {
            ctx.insert(k.into(), v);
        }
    }
    ctx.insert("vis_entities".into(), vis);
    ctx.insert("allowed_by_entity".into(), allowed);
    crate::pages::render_with(pages, "settings_vis_rows.html", ctx, headers)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dump_names() {
        assert!(dump_name_ok("wedge-20260101-120000.txt"));
        assert!(dump_name_ok("wedge-20260101-120000-3.txt"));
        for bad in ["wedge-2026010-120000.txt", "wedge-20260101-120000-.txt", "events.log", "wedge-20260101-120000.txt.bak", "../wedge-20260101-120000.txt"] {
            assert!(!dump_name_ok(bad), "{bad}");
        }
    }

    #[test]
    fn hints() {
        assert_eq!(key_hint(""), "");
        assert_eq!(key_hint("short"), "…");
        assert_eq!(key_hint("  sk-abcdefgh1234 "), "…1234");
    }

    #[test]
    fn cgroup_limits() {
        let d = tempfile::tempdir().unwrap();
        std::fs::write(d.path().join("cpu.max"), "150000 100000\n").unwrap();
        assert_eq!(usable_cpus(d.path()), usable_cpus(Path::new("/nonexistent")).min(2));
        std::fs::write(d.path().join("memory.max"), "1073741824\n").unwrap();
        assert_eq!(usable_ram_gb(d.path()), 1.0f64.min(usable_ram_gb(Path::new("/nonexistent"))));
    }
}
