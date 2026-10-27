//! `POST /settings/system` (app/main.py `settings_system_save`): the big System-tab form — the Ollama / Unsloth
//! URLs and model names, the per-request generation tuning, the upload limits, the two world-feature toggles, and
//! the server-level Ollama env (tuning_save.rs's sanitizer + env file). Errors re-render settings.html at 400 with
//! Python's message via settings.rs `page_context`; success redirects. Python's in-memory side effects
//! (`ai.set_llm_override`, `_sync_ollama_server_env`, its flags cache) move at the switch — the database write and
//! the env file are byte for byte. `None` = this port declines; Python answers.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode, Uri};
use rusqlite::types::Value as Sql;
use serde_json::{json, Value};

use crate::config::Config;
use crate::pages::Pages;

fn fmt_bound(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 9e15 { format!("{}", v as i64) } else { format!("{v}") }
}

fn field_number(f: &crate::form::Form, name: &str, label: &str, float: bool, lo: Option<f64>, hi: Option<f64>) -> Result<Option<f64>, String> {
    let raw = f.get(name).unwrap_or("").trim();
    if raw.is_empty() {
        return Ok(None);
    }
    let v: f64 = if float {
        raw.parse().map_err(|_| format!("{label} must be a number"))?
    } else {
        raw.parse::<i64>().map_err(|_| format!("{label} must be a whole number"))? as f64
    };
    if let Some(lo) = lo {
        if v < lo {
            return Err(format!("{label} must be at least {}", fmt_bound(lo)));
        }
    }
    if let Some(hi) = hi {
        if v > hi {
            return Err(format!("{label} must be at most {}", fmt_bound(hi)));
        }
    }
    Ok(Some(v))
}

/// (column, label, float, lo, hi) of every optional-number field, in the handler's order
const NUMBER_FIELDS: &[(&str, &str, bool, Option<f64>, Option<f64>)] = &[
    ("ollama_temperature", "Temperature", true, Some(0.0), Some(2.0)),
    ("ollama_top_p", "Top P", true, Some(0.0), Some(1.0)),
    ("ollama_top_k", "Top K", false, Some(0.0), None),
    ("ollama_repeat_penalty", "Repeat penalty", true, Some(0.0), Some(5.0)),
    ("ollama_num_predict", "Max output tokens", false, Some(-2.0), None),
    ("ollama_num_ctx", "Context length", false, Some(1.0), None),
    ("ollama_seed", "Seed", false, None, None),
    ("ollama_mirostat", "Mirostat mode", false, Some(0.0), Some(2.0)),
    ("ollama_mirostat_tau", "Mirostat tau", true, Some(0.0), Some(100.0)),
    ("ollama_mirostat_eta", "Mirostat eta", true, Some(0.0), Some(10.0)),
    ("ollama_num_gpu", "GPU layers", false, Some(0.0), None),
    ("ollama_min_p", "Min P", true, Some(0.0), Some(1.0)),
    ("ollama_typical_p", "Typical P", true, Some(0.0), Some(1.0)),
    ("ollama_repeat_last_n", "Repeat look-back", false, Some(-1.0), Some(131072.0)),
    ("ollama_presence_penalty", "Presence penalty", true, Some(-2.0), Some(2.0)),
    ("ollama_frequency_penalty", "Frequency penalty", true, Some(-2.0), Some(2.0)),
    ("ollama_num_keep", "Tokens kept on context shift", false, Some(0.0), Some(131072.0)),
    ("ollama_num_batch", "Batch size", false, Some(1.0), Some(4096.0)),
    ("ollama_num_thread", "CPU threads", false, Some(0.0), Some(256.0)),
    ("ollama_main_gpu", "Primary GPU index", false, Some(0.0), Some(15.0)),
    ("ollama_vram_override_mb", "VRAM override (MB)", false, Some(0.0), Some(1048576.0)),
    ("llm_context_tokens", "Unsloth context window (tokens)", false, Some(1024.0), Some(1048576.0)),
    ("character_import_max_chars", "Sheet length read in one go for AI character import (characters)", false, Some(2000.0), Some(60000.0)),
    ("session_media_max", "Pictures, videos and audio per session", false, Some(1.0), Some(50.0)),
    ("max_upload_mb", "General upload limit (MB)", false, Some(1.0), None),
    ("max_gallery_upload_mb", "Gallery album upload limit (MB)", false, Some(1.0), None),
    ("max_video_mb", "Video upload limit (MB)", false, Some(1.0), None),
    ("max_audio_mb", "Audio library (MB)", false, Some(1.0), None),
    ("max_ai_attachment_mb", "AI voice-memo attachment limit (MB)", false, Some(1.0), None),
];

fn render_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, system_error: &str, status: StatusCode) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let mut p = crate::settings::page_context(&db, cfg, "POST", uri, headers, "system")?;
    p.ctx.insert("system_error".into(), json!(system_error));
    let html = pages.env.get_template("settings.html").ok()?.render(nd_render::pyvalue::to_value(&Value::Object(p.ctx))).ok()?;
    let mut r = crate::pages::html(status, html, headers);
    r.headers_mut().insert("x-nd-served-by", axum::http::HeaderValue::from_static("gateway"));
    Some(r)
}

/// The save. A validation failure re-renders settings.html at 400 (Python's message); success writes the columns and
/// the env file, then redirects. `None` = this port declines (an unreadable form etc.) — Python answers.
pub fn settings_system_save(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, form: &crate::form::Form) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let _ = now;
    let st = db.query_row("SELECT * FROM app_settings ORDER BY id LIMIT 1", [], |r| crate::layout::row_map(r)).ok()?;
    let sid = st.get("id")?.as_i64()?;

    let txt = |k: &str, rstrip: bool| -> String {
        let v = form.get(k).unwrap_or("").trim().to_string();
        if rstrip { v.trim_end_matches('/').to_string() } else { v }
    };
    let ollama_model = txt("ollama_model", false);
    let ollama_url = txt("ollama_url", true);
    let llm_url = txt("llm_url", true);
    let llm_model = txt("llm_model", false);
    let llm_api_key = txt("llm_api_key", false);
    let llm_api_key_clear = form.get("llm_api_key_clear").unwrap_or("");
    let swarmui = txt("swarmui_external_url", true);
    let android = txt("android_emulator_url", true);
    let editor = txt("editor_external_url", true);
    let keep_alive: String = txt("ollama_keep_alive", false).chars().take(32).collect();

    // the URL fields: a non-http(s) value is a 400 with Python's message
    for (label, val) in [
        ("Ollama URL", &ollama_url), ("Unsloth URL", &llm_url), ("SwarmUI external URL", &swarmui),
        ("Android emulator URL", &android), ("Content editor URL", &editor),
    ] {
        if !val.is_empty() && !val.starts_with("http://") && !val.starts_with("https://") {
            return render_page(pages, cfg, uri, headers, &format!("{label} must start with http:// or https://"), StatusCode::BAD_REQUEST);
        }
    }

    // the optional-number fields, one at a time in the handler's order (the first failure is the message)
    let mut parsed: Vec<(&str, Option<f64>)> = vec![];
    for (col, label, float, lo, hi) in NUMBER_FIELDS {
        match field_number(form, col, label, *float, *lo, *hi) {
            Ok(v) => parsed.push((col, v)),
            Err(e) => return render_page(pages, cfg, uri, headers, &e, StatusCode::BAD_REQUEST),
        }
    }

    // use_mmap: 0 / 1 / blank; gpu_preset: blank or a known preset key
    let use_mmap_raw = txt("ollama_use_mmap", false);
    let use_mmap = if use_mmap_raw == "0" || use_mmap_raw == "1" { use_mmap_raw } else { String::new() };
    let gpu_preset_raw = txt("ollama_gpu_preset", false);
    let gpu_preset = if gpu_preset_raw.is_empty() || crate::tuning::gpu_preset_keys().contains(&gpu_preset_raw.as_str()) {
        gpu_preset_raw
    } else {
        String::new()
    };

    // the server env: the form's ollama_srv_* values straight into the sanitizer
    let mut raw_srv = serde_json::Map::new();
    for f in crate::tuning::SERVER_ENV_SPEC {
        raw_srv.insert(f.env_key.into(), json!(form.get(&format!("ollama_srv_{}", f.suffix)).unwrap_or("")));
    }
    let (srv_values, srv_errors) = crate::tuning::sanitize_server_env(&Value::Object(raw_srv));
    if let Some(first) = srv_errors.first() {
        return render_page(pages, cfg, uri, headers, first, StatusCode::BAD_REQUEST);
    }

    // the write, in Python's assignment order
    let mut set: Vec<(&str, Sql)> = vec![
        ("ollama_model", Sql::Text(ollama_model)),
        ("ollama_url", Sql::Text(ollama_url)),
        ("llm_url", Sql::Text(llm_url)),
        ("llm_model", Sql::Text(llm_model)),
        ("swarmui_external_url", Sql::Text(swarmui)),
        ("android_emulator_url", Sql::Text(android)),
        ("editor_external_url", Sql::Text(editor)),
        ("dreamlands_enabled", Sql::Integer(form.has("dreamlands_enabled") as i64)),
        ("king_in_yellow_enabled", Sql::Integer(form.has("king_in_yellow_enabled") as i64)),
        ("ollama_keep_alive", Sql::Text(keep_alive)),
        ("ollama_use_mmap", Sql::Text(use_mmap)),
        ("ollama_gpu_preset", Sql::Text(gpu_preset)),
        ("ollama_server_env_json", Sql::Text(nd_render::py_dumps(&srv_values))),
    ];
    // a blank key field means "leave it"; the clear checkbox erases; a typed key replaces
    if !llm_api_key_clear.is_empty() {
        set.push(("llm_api_key", Sql::Text(String::new())));
    } else if !llm_api_key.is_empty() {
        set.push(("llm_api_key", Sql::Text(llm_api_key)));
    }
    for (col, v) in &parsed {
        let sv = match v {
            Some(f) if f.fract() == 0.0 && f.abs() < 9e15 => Sql::Integer(*f as i64),
            Some(f) => Sql::Real(*f),
            None => Sql::Null,
        };
        set.push((col, sv));
    }
    crate::orm::update(&db, "app_settings", sid, &set)?;

    // the env file: written after the DB save; a failure surfaces as a 200 page warning, not a 500
    let config_dir = std::path::Path::new(&std::env::var("OLLAMA_CONFIG_DIR").unwrap_or_else(|_| "/data/ollama-config".into())).to_path_buf();
    let _ = std::fs::create_dir_all(&config_dir);
    let env_text = crate::tuning::render_env_file(&srv_values);
    let tmp = config_dir.join(format!(".{}.tmp", crate::tuning::ENV_FILENAME));
    let path = config_dir.join(crate::tuning::ENV_FILENAME);
    return match std::fs::write(&tmp, env_text.as_bytes()).and_then(|_| std::fs::rename(&tmp, &path)) {
        Ok(_) => crate::edits::redirect("/settings?tab=system"),
        Err(exc) => render_page(pages, cfg, uri, headers,
            &format!("Saved, but couldn't write {}: {exc}", path.display()), StatusCode::OK),
    };
}

/// The async entry: parse the form, run the save, forward when the form itself is unreadable
pub async fn settings_system_save_srv(cfg: std::sync::Arc<Config>, pages: std::sync::Arc<Pages>, uri: Uri, headers: HeaderMap, body: bytes::Bytes)
    -> Option<Response<Body>> {
    let parsed = tokio::task::spawn_blocking(move || {
        let form = crate::form::Form::parse(&body)?;
        let answer = settings_system_save(&pages, &cfg, &uri, &headers, &form);
        Some((form, answer))
    }).await.ok().flatten();
    match parsed {
        Some((_, Some(resp))) => Some(resp),
        _ => None,
    }
}
