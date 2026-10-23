//! The in-memory AI jobs (app/live_jobs.py) the gateway runs itself: each has a start route (the checks, a job id at once) and a poll
//! route that reads the job's status dict, both as the Python routes answer them; the run is crate::runners' (the AI slot for its whole
//! length, the Background Jobs page's cancel / restart / remove). A poll for an id the gateway does not hold is Python's.

use axum::body::Body;
use axum::http::{HeaderMap, Method, Response, StatusCode, Uri};
use serde_json::{json, Map, Value};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::ai::{backend, py_repr, Backend};
use crate::ai_more::{crash, detail};
use crate::config::Config;
use crate::layout::PageCtx;
use crate::runners::{now, Rerun};

fn ok(v: Value) -> Response<Body> {
    crate::edits::json_response(&v).unwrap_or_else(crash)
}

fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

fn truthy(v: Option<&Value>) -> bool {
    crate::edits::py_truthy_pub(v)
}

/// `bool(body.get(k, default))`
fn flag(b: &Map<String, Value>, k: &str, default: bool) -> bool {
    match b.get(k) { None => default, v => truthy(v) }
}

/// `str(body.get(k) or "").strip()`
fn text(b: &Map<String, Value>, k: &str) -> String {
    match b.get(k) { v if !truthy(v) => String::new(), Some(v) => crate::edits::py_strip_pub(&py_str(v)), None => String::new() }
}

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Python's `round(x, 2)` (correctly rounded, ties to even) as a JSON number
fn round2(x: f64) -> Value {
    json!(format!("{x:.2}").parse::<f64>().unwrap_or(x))
}

/// Whether a request is one of this module's start or poll routes
pub fn serves(method: &Method, path: &str) -> bool {
    let start = *method == Method::POST && path.ends_with("/start");
    let poll = *method == Method::GET && path.rsplit('/').next().is_some_and(|i| !i.is_empty() && i.bytes().all(|b| b.is_ascii_digit()));
    (start || poll) && path.starts_with("/api/maps/") && path.contains("/ai-markers/")
}

/// The routes this module serves: (method, path)
pub async fn route(cfg: Arc<Config>, method: &Method, uri: &Uri, headers: &HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let path = uri.path().to_string();
    let kind = if path.starts_with("/api/maps/") && path.contains("/ai-markers/") { "map markers" } else { return None };
    if *method == Method::GET {
        let id: i64 = path.rsplit('/').next()?.parse().ok()?;
        let state = crate::runners::poll(kind, id)?;
        let (cfg2, uri2, headers2) = (cfg.clone(), uri.clone(), headers.clone());
        let p = tokio::task::spawn_blocking(move || page(&cfg2, &uri2, &headers2, "GET")).await.ok()??;
        return poll_answer(kind, &p, &state);
    }
    if *method != Method::POST || !path.ends_with("/start") {
        return None;
    }
    let (uri2, headers2) = (uri.clone(), headers.clone());
    tokio::task::spawn_blocking(move || {
        let p = page(&cfg, &uri2, &headers2, "POST")?;
        let db = crate::writes::open(&cfg)?;
        match kind {
            "map markers" => map_markers_start(cfg.clone(), &db, &p, &path, &body),
            _ => None,
        }
    }).await.ok().flatten()
}

fn page(cfg: &Config, uri: &Uri, headers: &HeaderMap, method: &str) -> Option<PageCtx> {
    let db = crate::writes::open(cfg)?;
    let n = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method, uri, headers, n)?;
    (p.user_id > 0).then_some(p)
}

/// A poll route's answer for a job the gateway holds
fn poll_answer(kind: &str, p: &PageCtx, job: &Map<String, Value>) -> Option<Response<Body>> {
    let status = job.get("status").and_then(|v| v.as_str()).unwrap_or("");
    let elapsed = || json!((now() - job.get("started").and_then(|v| v.as_f64()).unwrap_or(0.0)).round_ties_even() as i64);
    match kind {
        "map markers" => {
            if !p.is_gm {
                return None;
            }
            Some(ok(match status {
                "running" => json!({"status": "running", "elapsed": elapsed()}),
                "error" => json!({"status": "error", "error": job.get("error")}),
                _ => json!({"status": "done", "markers": job.get("markers")}),
            }))
        }
        _ => None,
    }
}

/// The JSON body object (None: not an object - Python's own error)
fn json_body(body: &[u8]) -> Option<Map<String, Value>> {
    match serde_json::from_slice::<Value>(body).ok()? {
        Value::Object(o) => Some(o),
        _ => None,
    }
}

fn world_has_vault(db: &rusqlite::Connection, wid: i64) -> bool {
    db.query_row("SELECT obsidian_vault_path FROM worlds WHERE id = ?1", [wid], |r| r.get::<_, Option<String>>(0)).ok().flatten()
        .is_some_and(|p| !crate::edits::py_strip_pub(&p).is_empty())
}

// ── map markers (app/main.py `map_ai_markers_start` / `_map_ai_markers_task`) ──

fn map_file(cfg: &Config, slug: &str) -> Option<Map<String, Value>> {
    let jf = crate::maps::maps_dir(cfg)?.join(format!("{slug}.json"));
    match serde_json::from_str::<Value>(&std::fs::read_to_string(jf).ok()?).ok()? {
        Value::Object(o) => Some(o),
        _ => None,
    }
}

fn map_markers_start(cfg: Arc<Config>, db: &rusqlite::Connection, p: &PageCtx, path: &str, body: &[u8]) -> Option<Response<Body>> {
    let slug = path.strip_prefix("/api/maps/")?.strip_suffix("/ai-markers/start")?.to_string();
    if !p.is_gm || slug.contains('/') {
        return None;
    }
    let map = map_file(&cfg, &slug)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let map_world = match map.get("world_id") { None => 1, Some(v) => v.as_i64().filter(|_| !v.is_f64())? };
    if map_world != wid {
        return None;
    }
    let b = json_body(body)?;
    let prompt = text(&b, "prompt");
    if prompt.is_empty() {
        return Some(detail(StatusCode::BAD_REQUEST, "Describe what to place first"));
    }
    let count = match b.get("count") { v if !truthy(v) => 6, Some(v) => crate::ai::reply_int(v).unwrap_or(6), None => 6 }.clamp(1, 12);
    let (model, think, use_rag) = (text(&b, "model"), flag(&b, "think", true), flag(&b, "use_rag", true));
    if use_rag && world_has_vault(db, wid) {
        return None; // the vault layer of the lore lookup is Python's
    }
    let bk = backend(db)?;
    let id = crate::runners::new_job("map markers", json!({"status": "running", "started": now(), "markers": null, "error": ""}), 12);
    let rerun: Rerun = Arc::new(move |id| {
        let (cfg, bk, slug, prompt, model) = (cfg.clone(), bk.clone(), slug.clone(), prompt.clone(), model.clone());
        Box::pin(async move {
            let res = map_markers_task(cfg, bk, &slug, wid, &prompt, count, &model, think, use_rag).await;
            crate::runners::update("map markers", id, |e| match res {
                Ok(markers) => {
                    e.insert("status".into(), json!("done"));
                    e.insert("markers".into(), markers);
                }
                Err(m) => {
                    e.insert("status".into(), json!("error"));
                    e.insert("error".into(), json!(m));
                }
            });
        })
    });
    crate::runners::run("map markers", "map markers", id, rerun);
    Some(ok(json!({"job_id": id, "status": "running"})))
}

#[allow(clippy::too_many_arguments)]
async fn map_markers_task(cfg: Arc<Config>, b: Backend, slug: &str, wid: i64, prompt: &str, count: i64, model: &str, think: bool, use_rag: bool) -> Result<Value, String> {
    let (c, s, pr) = (cfg.clone(), slug.to_string(), prompt.to_string());
    let prepared = tokio::task::spawn_blocking(move || -> Result<(i64, i64, String, Vec<String>, String), String> {
        let map = map_file(&c, &s).ok_or("Map file is missing or unreadable")?;
        let dim = |k: &str| -> Result<i64, String> {
            match map.get(k) { v if !truthy(v) => Ok(2000), Some(v) => crate::ai::reply_int(v).map_err(|_| format!("invalid literal for int() with base 10: {}", py_repr(v))), None => Ok(2000) }
        };
        let (width, height) = (dim("width")?, dim("height")?);
        let mut existing: Vec<String> = vec![];
        for m in map.get("markers").filter(|v| truthy(Some(v))).and_then(|v| v.as_array()).cloned().unwrap_or_default() {
            if let Some(n) = m.as_object().and_then(|o| o.get("name")).filter(|v| truthy(Some(v))) {
                existing.push(py_str(n).to_lowercase());
            }
        }
        let db = crate::writes::open(&c).ok_or("database")?;
        let overlay: Option<Option<String>> = db.query_row("SELECT custom_markers_json FROM map_overlays WHERE slug = ?1 ORDER BY id LIMIT 1", [&s], |r| r.get(0)).ok();
        if let Some(raw) = overlay {
            if let Ok(Value::Array(list)) = serde_json::from_str::<Value>(raw.as_deref().filter(|r| !r.is_empty()).unwrap_or("[]")) {
                for m in list {
                    if let Some(l) = m.as_object().and_then(|o| o.get("label")).filter(|v| truthy(Some(v))) {
                        existing.push(py_str(l).to_lowercase());
                    }
                }
            }
        }
        let name = map.get("name").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_else(|| s.clone());
        let material = format!("Map: {name} ({width}x{height} px)\nGM request: {pr}");
        let rag = if use_rag {
            let q = take(&format!("{pr}\n{}", map.get("name").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_default()), 1500);
            take(&crate::retrieval::smart_world_context(&db, wid, &q, 8, 2).unwrap_or_default(), 3000)
        } else {
            String::new()
        };
        let user = format!("=== MAP ===\n{material}\n\n=== WORLD LORE (ground the markers in this) ===\n{}\n\n=== EXISTING MARKER LABELS (do not duplicate) ===\n{}",
            if rag.is_empty() { "(none)".to_string() } else { rag },
            { let l: Vec<String> = existing.iter().take(60).map(|l| format!("- {l}")).collect(); if l.is_empty() { "(none)".to_string() } else { l.join("\n") } });
        Ok((width, height, user, existing, String::new()))
    }).await.map_err(|_| "Cancelled.".to_string())??;
    let (width, height, user, existing, _) = prepared;
    let system = format!("You are placing points of interest on a tabletop RPG battle map. For each marker choose a short evocative label, a one-sentence description, and a plausible position on the map. Return STRICT JSON only:\n{{\"markers\": [{{\"label\": str, \"note\": str, \"lat\": number, \"lng\": number, \"color\": \"#rrggbb\"}}]}}\nRules: lat is the vertical position in pixels from the TOP (0..{height}), lng is the horizontal position from the LEFT (0..{width}) — spread markers out, don't cluster them in one corner; exactly {count} markers; labels must not duplicate the existing labels listed below; colors optional. No comments, no markdown fences.");
    let m = if model.is_empty() { b.model.clone() } else { model.to_string() };
    let schema = json!({"type": "object", "properties": {"markers": {"type": "array"}}, "required": ["markers"]});
    let raw = crate::ai::generate_chat_format(&b, &m, &system, &[json!({"role": "user", "content": user})], think, &Map::new(), Some(&schema)).await;
    // re.search(r"\{.*\}", raw, re.S): from the first "{" to the last "}"
    let parsed = match (raw.find('{'), raw.rfind('}')) {
        (Some(a), Some(z)) if z > a => serde_json::from_str::<Value>(&raw[a..=z]).ok(),
        _ => None,
    };
    let mut markers = vec![];
    if let Some(Value::Object(parsed)) = parsed {
        let mut seen: Vec<String> = existing;
        let items = match parsed.get("markers") { v if !truthy(v) => vec![], Some(Value::Array(a)) => a.clone(),
            Some(Value::String(s)) => s.chars().map(|c| json!(c.to_string())).collect(), Some(Value::Object(o)) => o.keys().map(|k| json!(k)).collect(),
            _ => return Err("'int' object is not iterable".into()) };
        for item in items {
            if markers.len() as i64 >= count {
                break;
            }
            let Value::Object(item) = item else { continue };
            let label = take(&text(&item, "label"), 80);
            if label.is_empty() || seen.contains(&label.to_lowercase()) {
                continue;
            }
            seen.push(label.to_lowercase());
            let num = |k: &str| -> Option<f64> {
                match item.get(k) { v if !truthy(v) => Some(0.0), Some(Value::Number(n)) => n.as_f64(), Some(Value::Bool(t)) => Some(*t as i64 as f64),
                    Some(Value::String(s)) => s.trim().parse::<f64>().ok().filter(|f| f.is_finite() || s.trim().to_lowercase().contains("inf")), _ => None }
            };
            let (Some(lat), Some(lng)) = (num("lat"), num("lng")) else { continue };
            let (lat, lng) = (lat.clamp(0.0, height as f64), lng.clamp(0.0, width as f64));
            let color = match item.get("color") { v if !truthy(v) => String::new(), Some(v) => py_str(v), None => String::new() };
            let hex = color.len() == 7 && color.starts_with('#') && color[1..].bytes().all(|c| c.is_ascii_hexdigit());
            markers.push(json!({"label": label, "note": take(&text(&item, "note"), 300), "lat": round2(lat), "lng": round2(lng),
                "color": if hex { color } else { "#ff4466".into() }}));
        }
    }
    if markers.is_empty() {
        return Err("The model's reply contained no usable markers — try rephrasing.".into());
    }
    Ok(Value::Array(markers))
}
