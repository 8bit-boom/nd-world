//! `GET /export/teacher/status` and `POST /export/teacher/{start,stop,clear}`: the Export & Backup card's endpoints. GM only, for the active
//! world, answered here only without Python (no Python counterpart: the hybrid image answers "Not available here").
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, Response, StatusCode, Uri};
use serde_json::{json, Map, Value};

use super::gen::{self, Options};
use crate::config::Config;

fn running() -> &'static Mutex<HashMap<i64, Arc<AtomicBool>>> {
    static R: OnceLock<Mutex<HashMap<i64, Arc<AtomicBool>>>> = OnceLock::new();
    R.get_or_init(|| Mutex::new(HashMap::new()))
}

fn reply(code: StatusCode, v: Value) -> Response<Body> {
    Response::builder().status(code).header(header::CONTENT_TYPE, "application/json").header("x-nd-served-by", "gateway").body(Body::from(v.to_string())).unwrap()
}

fn detail(code: StatusCode, m: &str) -> Response<Body> {
    reply(code, json!({"detail": m}))
}

/// The GM's active world: (connection, world row); None when the caller is not the GM or has no world (the auth gate's / Python's answer)
fn gm_world(cfg: &Config, method: &str, uri: &Uri, headers: &HeaderMap) -> Option<(crate::writes::Pooled, Map<String, Value>)> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method, uri, headers, now)?;
    if !p.is_gm {
        return None;
    }
    let world = p.world.clone()?;
    Some((db, world))
}

fn status_json(cfg: &Config, db: &rusqlite::Connection, world: &Map<String, Value>) -> Option<Value> {
    let wid = world.get("id")?.as_i64()?;
    let dir = gen::world_dir(cfg.db_path.parent()?, wid);
    let mut st = gen::read_state(&dir);
    let is_running = running().lock().ok()?.contains_key(&wid);
    if st["status"] == "running" && !is_running {
        st["status"] = json!("interrupted"); // the gateway restarted in the middle: Start continues (every answer is cached)
    }
    let b = crate::ai::backend(db);
    Some(json!({"world": world.get("name"), "state": st, "counts": gen::counts(&dir), "kinds": gen::KINDS.iter().map(|(id, n)| json!({"id": id, "name": n})).collect::<Vec<_>>(),
                "defaults": Options::default().to_value(), "ai_ready": b.is_some(), "model": b.map(|b| b.model)}))
}

/// `GET /export/teacher/status`
pub fn status(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, world) = gm_world(cfg, "GET", uri, headers)?;
    Some(reply(StatusCode::OK, status_json(cfg, &db, &world)?))
}

/// `POST /export/teacher/{start,stop,clear}`
pub async fn post(cfg: Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let action = uri.path().strip_prefix("/export/teacher/")?.to_string();
    let body_copy = body.clone();
    let c = cfg.clone();
    let u = uri.clone();
    let (db_dir, world, backend, members, opt) = tokio::task::spawn_blocking(move || -> Option<_> {
        let (db, world) = gm_world(&c, "POST", &u, &headers)?;
        let dir = c.db_path.parent()?.to_path_buf();
        let (backend, members) = if action == "start" {
            (crate::ai::backend(&db), crate::export::training_members(&db, &world))
        } else {
            (None, None)
        };
        let opt: Option<Options> = if action == "start" {
            if body.iter().all(|b| b.is_ascii_whitespace()) { Some(Options::default()) } else { serde_json::from_slice::<Value>(&body).ok().and_then(|v| Options::from_value(&v)) }
        } else {
            Some(Options::default())
        };
        Some((dir, world, (action, backend), members, opt))
    })
    .await
    .ok()??;
    let (action, backend) = backend;
    let wid = world.get("id")?.as_i64()?;
    let dir = gen::world_dir(&db_dir, wid);
    let is_running = running().lock().ok()?.contains_key(&wid);
    match action.as_str() {
        "start" => {
            let Some(opt) = opt else { return Some(detail(StatusCode::UNPROCESSABLE_ENTITY, "The options are not valid.")) };
            // the model the GM picked on the page ("" = the default one from Settings)
            let chosen = serde_json::from_slice::<Value>(&body_copy).ok().and_then(|v| v.get("model").and_then(|m| m.as_str().map(|m| m.trim().to_string()))).unwrap_or_default();
            if is_running {
                return Some(detail(StatusCode::CONFLICT, "Already running for this world."));
            }
            let Some(b) = backend else { return Some(detail(StatusCode::CONFLICT, "No AI model is set up. Choose one in Settings first.")) };
            let Some(members) = members else { return Some(detail(StatusCode::INTERNAL_SERVER_ERROR, "The world could not be read.")) };
            let (model, _) = crate::ai::resolve_model(&b, &chosen).await;
            let cancel = Arc::new(AtomicBool::new(false));
            running().lock().ok()?.insert(wid, cancel.clone());
            gen::set_state(&dir, json!({"status": "running", "text": "Starting\u{2026}", "model": model}));
            tokio::spawn(async move {
                gen::run(dir, members, b, model, opt, cancel).await;
                if let Ok(mut r) = running().lock() {
                    r.remove(&wid);
                }
            });
            Some(reply(StatusCode::ACCEPTED, json!({"ok": true})))
        }
        "stop" => {
            if let Some(f) = running().lock().ok()?.get(&wid) {
                f.store(true, Ordering::SeqCst);
            }
            Some(reply(StatusCode::OK, json!({"ok": true})))
        }
        "clear" => {
            if is_running {
                return Some(detail(StatusCode::CONFLICT, "Stop the run first."));
            }
            gen::clear(&dir, false);
            Some(reply(StatusCode::OK, json!({"ok": true})))
        }
        _ => None,
    }
}
