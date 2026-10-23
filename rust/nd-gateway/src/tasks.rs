//! AI requests run in the background (`X-ND-Background: 1`, app/ai_background.py) for the routes the gateway answers itself: a 202 with
//! a task id at once, the route run to completion in the AI slot, its answer kept in memory and polled from `GET /api/ai/tasks/<id>`.
//! The gateway's task ids start with "gw" so the task routes know which process holds a task; `GET /api/ai/tasks` lists both. A route the
//! gateway would hand to Python keeps the header and is started by Python's own middleware.

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode, Uri};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::config::Config;

const MAX_RUNNING_PER_USER: usize = 8;
const MAX_RUNNING: usize = 48;
const RETAIN_SECONDS: f64 = 3600.0;
const MAX_RETAINED: usize = 300;

#[derive(Clone)]
struct Task {
    user_id: i64,
    label: String,
    status: &'static str,
    started: f64,
    finished: Option<f64>,
    http_status: u16,
    content_type: String,
    location: String,
    body: Vec<u8>,
    abort: Option<tokio::task::AbortHandle>,
}

fn store() -> &'static Mutex<HashMap<String, Task>> {
    static S: OnceLock<Mutex<HashMap<String, Task>>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(HashMap::new()))
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn sweep(tasks: &mut HashMap<String, Task>) {
    let t = now();
    tasks.retain(|_, k| k.finished.map_or(true, |f| t - f <= RETAIN_SECONDS));
    if tasks.len() > MAX_RETAINED {
        let mut done: Vec<(String, f64)> = tasks.iter().filter_map(|(id, k)| k.finished.map(|f| (id.clone(), f))).collect();
        done.sort_by(|a, b| a.1.total_cmp(&b.1));
        for (id, _) in done.into_iter().take(tasks.len() - MAX_RETAINED) {
            tasks.remove(&id);
        }
    }
}

/// `label_for(path)`: app/ai_background.py `_LABELS`, first match wins
pub fn label_for(path: &str) -> String {
    let end = |s: &str| path.ends_with(s);
    let has = |s: &str| path.contains(s);
    let label = if end("/api/ai/tts") { "Text to speech" }
        else if end("/speak") { "NPC voice" }
        else if end("/recap-song") { "Recap song" }
        else if end("/transcribe") { "Transcription" }
        else if end("/prep/generate") { "Session prep" }
        else if has("/ai/summarize") || has("/ai/expand") || has("/ai/condense") { "Session summary" }
        else if end("/ai-tactics") { "Combat tactics" }
        else if end("/ai-insights") { "Party insights" }
        else if end("/generate-mystery") { "Mystery board" }
        else if end("/ai-day") { "Calendar day" }
        else if end("/calendar/ai-design") { "Calendar design" }
        else if end("/api/media-rename/suggest") { "Media names" }
        else if end("/tables/generate-ai") { "Random table" }
        else if has("/api/ai/generate/") || has("/entity-from") || has("/entities-from") { "Entity draft" }
        else if has("/api/ai/chat") { "AI chat" }
        else if end("/api/ai/assist") { "AI suggestions" }
        else if has("/api/facts/") { "Facts" }
        else if end("/api/bulk-edit/parse") { "Bulk edit" }
        else if end("/character-from-images") { "Character import" }
        else { return path.into() };
    label.into()
}

fn json_answer(status: StatusCode, v: &Value, extra: Option<(&str, String)>) -> Response<Body> {
    let mut b = Response::builder().status(status).header("content-type", "application/json").header("x-nd-served-by", "gateway");
    if let Some((k, v)) = extra {
        b = b.header(k, v);
    }
    b.body(Body::from(nd_render::py_dumps_compact(v))).unwrap()
}

/// Whether a request asks to run in the background
pub fn wants_background(headers: &HeaderMap) -> bool {
    headers.get("x-nd-background").and_then(|v| v.to_str().ok()).is_some_and(|v| v.trim() == "1")
}

/// Start `run` (the gateway's own answer to the request) as a background task for `user_id`: the 202, or the 429 at the limits
pub fn start<F>(user_id: i64, path: &str, headers: &HeaderMap, run: F) -> Response<Body>
where
    F: std::future::Future<Output = Response<Body>> + Send + 'static,
{
    let mut tasks = store().lock().unwrap_or_else(|e| e.into_inner());
    sweep(&mut tasks);
    let running = |u: Option<i64>| tasks.values().filter(|t| matches!(t.status, "queued" | "running") && u.map_or(true, |u| t.user_id == u)).count();
    if running(Some(user_id)) >= MAX_RUNNING_PER_USER || running(None) >= MAX_RUNNING {
        return json_answer(StatusCode::TOO_MANY_REQUESTS, &json!({"detail": "Too many AI tasks are already running — wait for one to finish (or cancel it)."}), None);
    }
    let label = headers.get("x-nd-task-label").map(|v| v.as_bytes().iter().map(|b| *b as char).take(60).collect::<String>()).filter(|l| !l.is_empty())
        .unwrap_or_else(|| label_for(path));
    let id = format!("gw{}", (0..16).map(|_| { let c = rand::random::<u8>() % 62; (match c { 0..=9 => b'0' + c, 10..=35 => b'a' + c - 10, _ => b'A' + c - 36 }) as char }).collect::<String>());
    let task_id = id.clone();
    let handle = tokio::spawn(async move {
        let slot = crate::jobs::slot("task").await;
        set(&task_id, |t| t.status = "running");
        let resp = run.await;
        drop(slot);
        let (parts, body) = resp.into_parts();
        let bytes = body.collect().await.map(|c| c.to_bytes().to_vec()).unwrap_or_default();
        let header = |k: &str| parts.headers.get(k).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let (ctype, location) = (header("content-type"), header("location"));
        set(&task_id, |t| {
            t.http_status = parts.status.as_u16();
            t.content_type = ctype;
            t.location = location;
            t.body = bytes;
            t.status = "done";
            t.finished = Some(now());
        });
    });
    tasks.insert(id.clone(), Task { user_id, label: label.clone(), status: "queued", started: now(), finished: None, http_status: 0, content_type: String::new(),
        location: String::new(), body: vec![], abort: Some(handle.abort_handle()) });
    json_answer(StatusCode::ACCEPTED, &json!({"task_id": id, "status": "running", "label": label}), Some(("x-nd-task", id.clone())))
}

fn set(id: &str, f: impl FnOnce(&mut Task)) {
    if let Ok(mut tasks) = store().lock() {
        if let Some(t) = tasks.get_mut(id) {
            f(t);
        }
    }
}

/// `Task.view(with_body)`
fn view(id: &str, t: &Task, with_body: bool) -> Value {
    let end = t.finished.unwrap_or_else(now);
    let mut d = json!({"id": id, "label": t.label, "status": t.status, "elapsed": (end - t.started) as i64, "started": t.started});
    if t.status == "queued" {
        d["position"] = json!(1);
    }
    if t.status == "done" {
        d["http_status"] = json!(t.http_status);
        d["content_type"] = json!(t.content_type);
        if !t.location.is_empty() {
            d["location"] = json!(t.location);
        }
        if with_body {
            let text = String::from_utf8_lossy(&t.body).to_string();
            d["body"] = if t.content_type.to_lowercase().contains("json") { serde_json::from_str(&text).unwrap_or(json!(text)) } else { json!(text) };
        }
    }
    d
}

/// The signed-in caller: (user id, is_gm)
fn caller(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<(i64, bool)> {
    let db = crate::writes::open(cfg)?;
    let n = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, n)?;
    (p.user_id > 0).then_some((p.user_id, p.is_gm))
}

/// `GET` / `DELETE /api/ai/tasks/<gw id>`: the gateway's task (None for another id: Python's)
pub async fn task_route(cfg: Arc<Config>, method: &axum::http::Method, uri: Uri, headers: HeaderMap) -> Option<Response<Body>> {
    let id = uri.path().strip_prefix("/api/ai/tasks/")?.to_string();
    if !id.starts_with("gw") {
        return None;
    }
    let (uid, gm) = tokio::task::spawn_blocking(move || caller(&cfg, &uri, &headers)).await.ok()??;
    let mut tasks = store().lock().ok()?;
    sweep(&mut tasks);
    let missing = |m: &str| json_answer(StatusCode::NOT_FOUND, &json!({"detail": m}), None);
    let Some(t) = tasks.get(&id).filter(|t| t.user_id == uid || gm).cloned() else {
        return Some(missing(if method == axum::http::Method::GET { "Unknown AI task (it may have finished long ago, or the server restarted)" } else { "Unknown AI task" }));
    };
    if method == axum::http::Method::GET {
        return Some(json_answer(StatusCode::OK, &view(&id, &t, true), None));
    }
    if matches!(t.status, "queued" | "running") {
        if let Some(a) = &t.abort {
            a.abort();
        }
        if let Some(k) = tasks.get_mut(&id) {
            k.status = "cancelled";
            k.finished = Some(now());
        }
        return Some(json_answer(StatusCode::OK, &json!({"ok": true, "cancelled": true}), None));
    }
    tasks.remove(&id);
    Some(json_answer(StatusCode::OK, &json!({"ok": true, "cancelled": false}), None))
}

/// The gateway's tasks of a user, for `GET /api/ai/tasks` (merged into Python's list by the caller)
pub fn tasks_for(user_id: i64) -> Vec<Value> {
    let mut tasks = store().lock().unwrap_or_else(|e| e.into_inner());
    sweep(&mut tasks);
    tasks.iter().filter(|(_, t)| t.user_id == user_id).map(|(id, t)| view(id, t, false)).collect()
}

/// `GET /api/ai/tasks`: Python's list with the gateway's tasks added, newest first, at most 50
pub async fn list(cfg: Arc<Config>, uri: Uri, headers: HeaderMap, python: Response<Body>) -> Response<Body> {
    let (parts, body) = python.into_parts();
    let bytes = body.collect().await.map(|c| c.to_bytes()).unwrap_or_default();
    let mine = tokio::task::spawn_blocking(move || caller(&cfg, &uri, &headers)).await.ok().flatten();
    let merged = match (parts.status.is_success(), mine, serde_json::from_slice::<Value>(&bytes)) {
        (true, Some((uid, _)), Ok(Value::Object(mut o))) => {
            let mut all: Vec<Value> = o.get("tasks").and_then(|t| t.as_array()).cloned().unwrap_or_default();
            all.extend(tasks_for(uid));
            all.sort_by(|a, b| b["started"].as_f64().unwrap_or(0.0).total_cmp(&a["started"].as_f64().unwrap_or(0.0)));
            all.truncate(50);
            o.insert("tasks".into(), Value::Array(all));
            Some(nd_render::py_dumps_compact(&Value::Object(o)))
        }
        _ => None,
    };
    match merged {
        Some(body) => {
            let mut resp = Response::from_parts(parts, Body::from(body));
            resp.headers_mut().remove("content-length");
            resp
        }
        None => Response::from_parts(parts, Body::from(bytes)),
    }
}
