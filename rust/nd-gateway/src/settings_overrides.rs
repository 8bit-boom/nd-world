//! `POST /settings/system/model-override` and `/delete` (app/main.py `settings_model_override_save` /
//! `settings_model_override_delete`): per-model generation-tuning overrides stored in
//! `ollama_model_overrides_json`. `_refresh_settings_overrides` moves at the switch.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode, Uri};
use rusqlite::types::Value as Sql;
use serde_json::{json, Value};

use crate::config::Config;
use crate::pages::Pages;

/// _MODEL_OVERRIDE_FIELD_SPEC: (option key, label, float, lo, hi)
const FIELDS: &[(&str, &str, bool, Option<f64>, Option<f64>)] = &[
    ("temperature", "Temperature", true, Some(0.0), Some(2.0)),
    ("top_p", "Top P", true, Some(0.0), Some(1.0)),
    ("top_k", "Top K", false, Some(0.0), None),
    ("repeat_penalty", "Repeat penalty", true, Some(0.0), Some(5.0)),
    ("num_predict", "Max output tokens", false, Some(-2.0), None),
    ("num_ctx", "Context length", false, Some(1.0), None),
    ("seed", "Seed", false, None, None),
    ("num_gpu", "GPU layers", false, Some(0.0), None),
];

fn fmt_bound(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 9e15 { format!("{}", v as i64) } else { format!("{v}") }
}

fn parse_one(label: &str, raw: &str, float: bool, lo: Option<f64>, hi: Option<f64>) -> Result<Option<f64>, String> {
    let t = raw.trim();
    if t.is_empty() {
        return Ok(None);
    }
    let v: f64 = if float {
        t.parse().map_err(|_| format!("{label} must be a number"))?
    } else {
        t.parse::<i64>().map_err(|_| format!("{label} must be a whole number"))? as f64
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

fn overrides_json(db: &rusqlite::Connection) -> Option<Value> {
    let raw: Option<String> = db.query_row("SELECT ollama_model_overrides_json FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get(0)).ok();
    match raw.as_deref().filter(|t| !t.is_empty()) {
        Some(t) => serde_json::from_str(t).ok(),
        None => Some(json!({})),
    }
}

fn save_overrides(db: &rusqlite::Connection, overrides: &Value) -> Option<()> {
    let id: i64 = db.query_row("SELECT id FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get(0)).ok()?;
    crate::orm::update(db, "app_settings", id, &[("ollama_model_overrides_json", Sql::Text(nd_render::py_dumps(overrides)))]).map(|_| ())
}

/// The save: the model's options from the filled fields, an entry kept only when something is actually set
fn model_override_save(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, form: &crate::form::Form) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let model = form.get("model")?.trim().to_string();
    if model.is_empty() {
        return render_400(pages, cfg, uri, headers, "Pick a model to override first");
    }
    let mut options = serde_json::Map::new();
    for (key, label, float, lo, hi) in FIELDS {
        let val = match parse_one(label, form.get(key).unwrap_or(""), *float, *lo, *hi) {
            Ok(Some(v)) => if *float { json!(v) } else { json!(v as i64) },
            Ok(None) => continue,
            Err(e) => return render_400(pages, cfg, uri, headers, &e),
        };
        options.insert(key.to_string(), val);
    }
    let keep_alive: String = form.get("keep_alive").unwrap_or("").trim().chars().take(32).collect();
    let thinks = form.has("thinking");
    let mut overrides = overrides_json(&db)?.as_object().cloned().unwrap_or_default();
    if !options.is_empty() || !keep_alive.is_empty() || thinks {
        let mut entry = serde_json::Map::new();
        entry.insert("options".into(), Value::Object(options));
        entry.insert("keep_alive".into(), json!(keep_alive));
        if thinks {
            entry.insert("thinking".into(), json!(true));
        }
        overrides.insert(model, Value::Object(entry));
    } else {
        overrides.remove(&model);
    }
    let overrides = Value::Object(overrides);
    save_overrides(&db, &overrides)?;
    crate::edits::redirect("/settings?tab=system")
}

/// The delete: the model's entry dropped
fn model_override_delete(db: &rusqlite::Connection, form: &crate::form::Form) -> Option<Response<Body>> {
    let model = form.get("model")?.trim().to_string();
    let mut overrides = overrides_json(db)?.as_object().cloned().unwrap_or_default();
    overrides.remove(&model);
    let overrides = Value::Object(overrides);
    save_overrides(db, &overrides)?;
    crate::edits::redirect("/settings?tab=system")
}

fn render_400(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, message: &str) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let mut p = crate::settings::page_context(&db, cfg, "POST", uri, headers, "system")?;
    p.ctx.insert("system_error".into(), json!(message));
    let html = pages.env.get_template("settings.html").ok()?.render(nd_render::pyvalue::to_value(&Value::Object(p.ctx))).ok()?;
    let mut r = crate::pages::html(StatusCode::BAD_REQUEST, html, headers);
    r.headers_mut().insert("x-nd-served-by", axum::http::HeaderValue::from_static("gateway"));
    Some(r)
}

/// The async entries: parse the form, run, forward when the form itself is unreadable
pub async fn save_srv(cfg: std::sync::Arc<Config>, pages: std::sync::Arc<Pages>, uri: Uri, headers: HeaderMap, body: bytes::Bytes)
    -> Option<Response<Body>> {
    let parsed = tokio::task::spawn_blocking(move || {
        let form = crate::form::Form::parse(&body)?;
        let answer = model_override_save(&pages, &cfg, &uri, &headers, &form);
        Some((form, answer))
    }).await.ok().flatten();
    match parsed {
        Some((_, Some(resp))) => Some(resp),
        _ => None,
    }
}

pub async fn delete_srv(cfg: std::sync::Arc<Config>, pages: std::sync::Arc<Pages>, uri: Uri, headers: HeaderMap, body: bytes::Bytes)
    -> Option<Response<Body>> {
    let parsed = tokio::task::spawn_blocking(move || {
        let db = crate::writes::open(&cfg)?;
        let form = crate::form::Form::parse(&body)?;
        let answer = model_override_delete(&db, &form);
        Some((form, answer))
    }).await.ok().flatten();
    match parsed {
        Some((_, Some(resp))) => Some(resp),
        _ => None,
    }
}

