//! Small JSON routes: `GET /health` (app/main.py), the Ollama preset list (`/api/ai/presets`, app/routers/ai.py, stored in ai_models.json
//! beside the database) and the dashboard's world-summary state (`GET /api/ai/world-summary`).

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Method, Response, StatusCode, Uri};
use serde_json::{json, Map, Value};

use crate::config::Config;

pub fn serves(method: &Method, path: &str) -> bool {
    match path {
        "/health" | "/api/ai/world-summary" => *method == Method::GET,
        "/api/ai/presets" => *method == Method::GET || *method == Method::POST,
        _ => *method == Method::DELETE && path.strip_prefix("/api/ai/presets/").is_some_and(|l| !l.is_empty() && !l.contains('/')),
    }
}

fn models_path(cfg: &Config) -> Option<std::path::PathBuf> {
    Some(cfg.db_path.parent()?.join("ai_models.json"))
}

/// `_load_data()`: the file's object, or the default for a missing / unreadable one; None for a file Python would fail on
fn load_data(cfg: &Config) -> Option<Map<String, Value>> {
    let default = || serde_json::from_value::<Map<String, Value>>(json!({"custom": [], "hidden": []})).ok();
    let Ok(text) = std::fs::read_to_string(models_path(cfg)?) else { return default() };
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(o)) => Some(o),
        Ok(_) => None, // a list or the like: `.get` raises in Python
        Err(_) if text.contains("NaN") || text.contains("Infinity") => None,
        Err(_) => default(),
    }
}

fn save_data(cfg: &Config, data: &Map<String, Value>) -> Option<()> {
    std::fs::write(models_path(cfg)?, nd_render::py_dumps_indent(&Value::Object(data.clone()), 2)).ok()
}

/// `list_presets()`: the "presets" list, [] when absent; None when it is something else
fn presets(data: &Map<String, Value>) -> Option<Vec<Value>> {
    match data.get("presets") {
        None => Some(vec![]),
        Some(Value::Array(a)) => Some(a.clone()),
        Some(_) => None,
    }
}

const OPTIONS: &[(&str, char, Option<f64>, Option<f64>)] = &[
    ("temperature", 'f', Some(0.0), Some(2.0)), ("top_p", 'f', Some(0.0), Some(1.0)), ("top_k", 'i', Some(0.0), None),
    ("repeat_penalty", 'f', Some(0.0), Some(5.0)), ("num_predict", 'i', Some(-2.0), None), ("num_ctx", 'i', Some(1.0), None),
    ("seed", 'i', None, None), ("mirostat", 'i', Some(0.0), Some(2.0)), ("mirostat_tau", 'f', Some(0.0), Some(100.0)), ("mirostat_eta", 'f', Some(0.0), Some(10.0)),
    ("num_gpu", 'i', Some(0.0), Some(1000.0)), ("min_p", 'f', Some(0.0), Some(1.0)), ("typical_p", 'f', Some(0.0), Some(1.0)),
    ("repeat_last_n", 'i', Some(-1.0), Some(131072.0)), ("presence_penalty", 'f', Some(-2.0), Some(2.0)), ("frequency_penalty", 'f', Some(-2.0), Some(2.0)),
    ("num_keep", 'i', Some(0.0), Some(131072.0)), ("num_batch", 'i', Some(1.0), Some(4096.0)), ("num_thread", 'i', Some(0.0), Some(256.0)),
    ("main_gpu", 'i', Some(0.0), Some(15.0)), ("use_mmap", 'b', None, None),
];

/// `_clamp_options(raw)`: the allowed keys whose value converts and lies in range, in the order sent; None for a string value (int("5")
/// reads it its own way: Python's)
fn clamp_options(raw: &Map<String, Value>) -> Option<Map<String, Value>> {
    let mut out = Map::new();
    for (key, val) in raw {
        let Some(&(_, kind, lo, hi)) = OPTIONS.iter().find(|o| o.0 == key) else { continue };
        if val.is_null() {
            continue;
        }
        let v: f64;
        let shown: Value = match (kind, val) {
            ('b', x) => {
                out.insert(key.clone(), json!(crate::edits::py_truthy_pub(Some(x))));
                continue;
            }
            (_, Value::String(_)) => return None,
            (_, Value::Array(_) | Value::Object(_)) => continue, // TypeError: skipped
            (_, Value::Bool(b)) => {
                v = *b as i64 as f64;
                if kind == 'i' { json!(*b as i64) } else { json!(v) }
            }
            ('i', Value::Number(n)) => {
                if let Some(i) = n.as_i64() {
                    v = i as f64;
                    json!(i)
                } else {
                    let f = n.as_f64()?;
                    if !f.is_finite() || f.abs() >= 9e15 {
                        return None;
                    }
                    v = f.trunc();
                    json!(f.trunc() as i64)
                }
            }
            (_, Value::Number(n)) => {
                v = n.as_f64()?;
                json!(v)
            }
            _ => continue,
        };
        // num_ctx stops at the app's own auto-sizing ceiling: max(8192, MAX_AUTO_NUM_CTX), 32768 unless the environment says otherwise
        let hi = if key == "num_ctx" {
            let n = std::env::var("MAX_AUTO_NUM_CTX").ok().map(|s| nd_render::text::py_strip(&s).parse::<i64>().ok()).unwrap_or(Some(32768))?;
            Some(n.max(8192) as f64)
        } else { hi };
        if lo.is_some_and(|l| v < l) || hi.is_some_and(|h| v > h) {
            continue;
        }
        out.insert(key.clone(), shown);
    }
    Some(out)
}

pub fn handle(cfg: &Config, method: &Method, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let path = uri.path();
    if path == "/health" {
        let build = std::env::var("ND_BUILD").unwrap_or_default();
        let build: String = nd_render::text::py_strip(&build).chars().take(7).collect();
        return crate::edits::json_response(&json!({"status": "ok", "build": if build.is_empty() { "dev".to_string() } else { build }}));
    }
    if path == "/api/ai/world-summary" {
        return world_summary(cfg, uri, headers);
    }
    // the presets: the GM's (the auth gate refuses anyone else before this)
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method.as_str(), uri, headers, now)?;
    if !p.is_gm {
        return None;
    }
    let mut data = load_data(cfg)?;
    match *method {
        Method::GET => crate::edits::json_response(&json!({"presets": presets(&data)?})),
        Method::POST => {
            let ct = crate::form::content_type(headers);
            if ct != "application/json" {
                return None;
            }
            let b = match serde_json::from_slice::<Value>(body).ok()? {
                Value::Object(o) if crate::edits::plain_numbers(body) => o,
                _ => return None,
            };
            let text = |k: &str| -> Option<String> {
                match b.get(k) {
                    None => Some(String::new()),
                    Some(Value::String(s)) => Some(nd_render::text::py_strip(s).to_string()),
                    _ => None, // pydantic's 422
                }
            };
            let Some(Value::String(label)) = b.get("label") else { return None };
            let label = nd_render::text::py_strip(label).to_string();
            let (model, extra) = (text("model")?, text("system_extra")?);
            let options = match b.get("options") {
                None => Map::new(),
                Some(Value::Object(o)) => o.clone(),
                _ => return None,
            };
            if label.is_empty() {
                return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, "label is required"));
            }
            let clamped = clamp_options(&options)?;
            let mut list = presets(&data)?;
            if list.iter().any(|x| !x.is_object()) {
                return None;
            }
            list.retain(|x| x.get("label") != Some(&json!(label)));
            list.push(json!({"label": label, "model": model, "system_extra": extra, "options": clamped}));
            data.insert("presets".into(), Value::Array(list.clone()));
            save_data(cfg, &data)?;
            crate::edits::json_response(&json!({"ok": true, "presets": list}))
        }
        _ => {
            let raw = path.strip_prefix("/api/ai/presets/")?;
            let label = percent_encoding::percent_decode_str(raw).decode_utf8().ok()?.to_string();
            if label.contains('/') {
                return None;
            }
            let mut list = presets(&data)?;
            if list.iter().any(|x| !x.is_object()) {
                return None;
            }
            list.retain(|x| x.get("label") != Some(&json!(label)));
            data.insert("presets".into(), Value::Array(list.clone()));
            save_data(cfg, &data)?;
            crate::edits::json_response(&json!({"ok": true, "presets": list}))
        }
    }
}

/// `api_world_summary_get`: the newest summary job of the viewer's own audience tier
fn world_summary(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let Some(world) = p.world.clone() else { return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, "No active world")) };
    let can_edit = p.is_gm || p.role.as_deref() == Some("assistant");
    if !can_edit && !crate::edits::py_truthy_pub(world.get("players_can_view_world_summary")) {
        return Some(crate::idpages::refusal(403));
    }
    let wid = world.get("id")?.as_i64()?;
    let audience = if p.is_gm { "audience IN ('gm', '')" } else { "audience = 'players'" };
    let job = db.query_row(&format!("SELECT * FROM audio_jobs WHERE world_id = ?1 AND purpose = 'world_summary' AND {audience} ORDER BY id DESC LIMIT 1"), [wid],
        |r| crate::layout::row_map(r)).ok();
    let Some(job) = job else { return crate::edits::json_response(&json!({"recap": ""})) };
    let text = |k: &str| job.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let status = text("status");
    if ["pending", "transcribing", "summarizing"].contains(&status.as_str()) {
        return crate::edits::json_response(&json!({"pending": true}));
    }
    if status == "error" {
        let e = text("error");
        return crate::edits::json_response(&json!({"recap": "", "error": if e.is_empty() { "generation failed".to_string() } else { e }}));
    }
    crate::edits::json_response(&json!({"recap": text("recap"), "generated_at": crate::edits::py_isoformat_pub(job.get("finished_at"))}))
}
