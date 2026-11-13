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

fn query_flag(uri: &Uri, key: &str, default: bool) -> bool {
    uri.query().and_then(|q| q.split('&').find_map(|kv| kv.strip_prefix(&format!("{key}=")))).map(|v| matches!(v, "1" | "true" | "on" | "yes")).unwrap_or(default)
}

fn query_num(uri: &Uri, key: &str, default: u8) -> u8 {
    uri.query().and_then(|q| q.split('&').find_map(|kv| kv.strip_prefix(&format!("{key}=")))).and_then(|v| v.parse::<u8>().ok()).map(|n| n.min(30)).unwrap_or(default)
}

fn query_count(uri: &Uri, key: &str, default: usize) -> usize {
    uri.query().and_then(|q| q.split('&').find_map(|kv| kv.strip_prefix(&format!("{key}=")))).and_then(|v| v.parse::<usize>().ok()).map(|n| n.min(1_000_000)).unwrap_or(default)
}

/// `GET /export/unsloth-studio.zip?w=<world slug>&world_book=1&teacher=1&test=10[&worlds=a,b,c | &worlds_all=1][&vram=<GB of the card>][&max_tokens=auto|N][&eval_limit=300][&info=1]`: the world's training examples as chatml JSONL for the Train page of
/// Unsloth Studio / Unsloth Desktop, zipped with a README (`info=1`: only the counts, as JSON; `worlds=a,b` or `worlds_all=1`: several worlds (or every world of the server) combined in one file; `w` picks the world like everywhere else in nd-world). GM only, for the active world; answered here only
/// without Python (no Python counterpart: the hybrid image answers "Not available here").
/// The data the query asks for (one world, several or all) and the file-name slug; `Err` is the answer to give instead.
fn studio_built(cfg: &Config, uri: &Uri, headers: &HeaderMap, detected: &Value) -> Option<Result<(super::studio::Built, String), Response<Body>>> {
    let (db, world) = gm_world(cfg, "GET", uri, headers)?;
    // the card the settings are for: the one picked on the page (`vram=<GB>`), else the one nd-world sees on its own machine, else an assumed 24 GB
    let chosen = uri.query().and_then(|q| q.split('&').find_map(|kv| kv.strip_prefix("vram="))).and_then(|v| v.parse::<f64>().ok()).filter(|g| (0.0..=1024.0).contains(g));
    let hw = super::hw::resolve(detected, chosen);
    // `max_tokens=auto` (the default): as long as this card trains, at most 8192
    let max_tokens = match uri.query().and_then(|q| q.split('&').find_map(|kv| kv.strip_prefix("max_tokens="))) {
        Some(v) if v != "auto" => v.parse::<usize>().map(|n| n.min(1_000_000)).unwrap_or(8192),
        _ => super::hw::auto_max_tokens(hw.vram_gb),
    };
    let opts = super::studio::Options { world_book: query_flag(uri, "world_book", true), teacher: query_flag(uri, "teacher", true), test_percent: query_num(uri, "test", 10),
                                          max_tokens, eval_limit: query_count(uri, "eval_limit", 300), reasoning: query_count(uri, "reasoning", 0).min(100) as u8, hw };
    let db_dir = cfg.db_path.parent()?.to_path_buf();
    let build_one = |w: &Map<String, Value>| -> Result<super::studio::Built, String> {
        let wid = w.get("id").and_then(|v| v.as_i64()).ok_or("no world")?;
        let members = crate::export::training_members(&db, w).ok_or("The world could not be read.")?;
        super::studio::build(&members, &gen::read_examples(&gen::world_dir(&db_dir, wid)), &opts)
    };
    // several worlds in one file: `worlds=slug1,slug2` (the ones named) or `worlds_all=1` (every world of this server); the GM sees them all. A world with
    // nothing to export is left out. One world only: the one in `w` / the active-world cookie.
    let named: Vec<String> = uri.query().and_then(|q| q.split('&').find_map(|kv| kv.strip_prefix("worlds="))).unwrap_or("").split(',')
        .map(|x| x.trim().to_string()).filter(|x| !x.is_empty() && x.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')).collect();
    let all = query_flag(uri, "worlds_all", false);
    let (built, slug) = if all || !named.is_empty() {
        let mut st = db.prepare("SELECT * FROM worlds ORDER BY id").ok()?;
        let rows: Vec<Map<String, Value>> = st.query_map([], |r| crate::layout::row_map(r)).ok()?.filter_map(|r| r.ok())
            .filter(|w| all || w.get("slug").and_then(|v| v.as_str()).is_some_and(|s| named.iter().any(|n| n == s))).collect();
        let parts: Vec<super::studio::Built> = rows.iter().filter_map(|w| build_one(w).ok()).collect();
        let n = parts.len();
        match super::studio::merge(parts) {
            Some(b) => (b, if all { "all-worlds".to_string() } else if n == 1 { rows.iter().find_map(|w| w.get("slug").and_then(|v| v.as_str()).map(str::to_string)).unwrap_or_default() } else { format!("{n}-worlds") }),
            None => return Some(Err(no_store(detail(StatusCode::UNPROCESSABLE_ENTITY, "None of those worlds has anything to export yet.")))),
        }
    } else {
        let slug = world.get("slug").and_then(|v| v.as_str())?.to_string();
        match build_one(&world) {
            Ok(b) => (b, slug),
            Err(e) => return Some(Err(no_store(detail(StatusCode::UNPROCESSABLE_ENTITY, &e)))),
        }
    };
    Some(Ok((super::studio::finish(built, opts.eval_limit), slug)))
}

pub fn studio(cfg: &Config, uri: &Uri, headers: &HeaderMap, detected: &Value) -> Option<Response<Body>> {
    let (built, slug) = match studio_built(cfg, uri, headers, detected)? {
        Ok(x) => x,
        Err(r) => return Some(r),
    };
    if query_flag(uri, "info", false) {
        return Some(no_store(reply(StatusCode::OK, super::studio::summary(&built))));
    }
    let build = nd_render::build_info();
    let build = build.get_attr("build").ok().and_then(|v| v.as_str().map(str::to_string)).unwrap_or_else(|| "dev".to_string());
    let zip = super::studio::zip(&built, &build)?;
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/zip")
        .header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"{slug}-unsloth-studio.zip\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(zip))
        .ok()
        .map(no_store)
}

/// The answer depends on the signed-in user and the chosen world, never on the address alone: no cache (browser, proxy or CDN) may keep it.
fn no_store(mut r: Response<Body>) -> Response<Body> {
    r.headers_mut().insert(header::CACHE_CONTROL, "private, no-store".parse().unwrap());
    r.headers_mut().insert(header::VARY, "Cookie".parse().unwrap());
    r
}

/// `POST /export/unsloth-studio/train` (same query as the zip), `GET /export/unsloth-studio/job?id=`, `POST /export/unsloth-studio/job/{cancel,pause,resume}?id=`:
/// train on the Unsloth Studio Settings points at. GM only.
pub async fn studio_train(cfg: Arc<Config>, uri: Uri, headers: HeaderMap, detected: Value, action: &'static str) -> Option<Response<Body>> {
    let method = if action == "job" { "GET" } else { "POST" };
    let target = studio_target(&cfg, method, &uri, &headers).await?;
    let Some((base, key)) = target else {
        return Some(no_store(detail(StatusCode::CONFLICT, "No Unsloth Studio API key is saved. Add the Studio address and key in Settings first.")));
    };
    let id = uri.query().and_then(|q| q.split('&').find_map(|kv| kv.strip_prefix("id="))).unwrap_or("").to_string();
    let answer = |r: Result<Value, String>| no_store(match r {
        Ok(v) => reply(StatusCode::OK, v),
        Err(e) => detail(StatusCode::BAD_GATEWAY, &e),
    });
    match action {
        "job" => Some(answer(super::train::job(&base, &key, &id).await)),
        "cancel" | "pause" | "resume" => Some(answer(super::train::act(&base, &key, &id, action).await)),
        _ if query_flag(&uri, "per_world", false) => {
            // one adapter per world: one job for every world chosen (Unsloth Studio queues them, and trains several of the same model together when it can)
            let (cfg3, uri3, headers3) = (cfg.clone(), uri.clone(), headers.clone());
            let slugs = tokio::task::spawn_blocking(move || chosen_slugs(&cfg3, &uri3, &headers3)).await.ok()??;
            if slugs.is_empty() {
                return Some(no_store(detail(StatusCode::UNPROCESSABLE_ENTITY, "Tick at least one world.")));
            }
            let mut jobs = Vec::new();
            for slug in slugs {
                let one = with_single_world(&uri, &slug);
                let (cfg4, headers4, d4) = (cfg.clone(), headers.clone(), detected.clone());
                let built = tokio::task::spawn_blocking(move || studio_built(&cfg4, &one, &headers4, &d4)).await.ok()??;
                let Ok((built, slug)) = built else { jobs.push(json!({"world": slug, "error": "Nothing to export yet."})); continue };
                let st = super::studio::settings(&built);
                if st.repo.is_empty() {
                    return Some(no_store(detail(StatusCode::UNPROCESSABLE_ENTITY, "No model fits the chosen card; pick a larger one.")));
                }
                match super::train::start(&base, &key, &built, &slug, &st).await {
                    Ok(mut j) => { j["world"] = json!(slug); j["nd"] = json!({"model": st.model, "method": st.method, "context": st.max_seq_length, "rows": built.train.len(), "eval": built.eval.len()}); jobs.push(j) }
                    Err(e) => { jobs.push(json!({"world": slug, "error": e})); break }
                }
            }
            Some(no_store(reply(StatusCode::OK, json!({"jobs": jobs, "id": jobs.iter().find_map(|j| j["id"].as_str()).unwrap_or(""), "status": "queued", "per_world": true}))))
        }
        _ => {
            let (cfg3, uri3, headers3) = (cfg.clone(), uri.clone(), headers.clone());
            let built = tokio::task::spawn_blocking(move || studio_built(&cfg3, &uri3, &headers3, &detected)).await.ok()??;
            let (built, slug) = match built {
                Ok(x) => x,
                Err(r) => return Some(r),
            };
            let st = super::studio::settings(&built);
            if st.repo.is_empty() {
                return Some(no_store(detail(StatusCode::UNPROCESSABLE_ENTITY, "No model fits the chosen card; pick a larger one.")));
            }
            let r = super::train::start(&base, &key, &built, &slug, &st).await.map(|mut j| {
                j["nd"] = json!({"model": st.model, "method": st.method, "context": st.max_seq_length, "rows": built.train.len(), "eval": built.eval.len()});
                j
            });
            Some(answer(r))
        }
    }
}

/// The Studio address and API key Settings holds (outer None: not the GM; inner None: no key saved - an Ollama address must never get a Bearer).
async fn studio_target(cfg: &Arc<Config>, method: &'static str, uri: &Uri, headers: &HeaderMap) -> Option<Option<(String, String)>> {
    let (cfg2, uri2, headers2) = (cfg.clone(), uri.clone(), headers.clone());
    tokio::task::spawn_blocking(move || {
        let (db, _) = gm_world(&cfg2, method, &uri2, &headers2)?;
        let b = crate::ai::backend(&db);
        Some(b.filter(|b| !b.key.is_empty()).map(|b| (b.base_url(), b.key.clone())))
    }).await.ok()?
}

/// `vram=studio` on the card list: the card is the one Unsloth Studio reports (it may run on another computer than nd-world); otherwise what nd-world detected itself.
pub async fn detected_for(cfg: &Arc<Config>, uri: &Uri, headers: &HeaderMap, local: Value) -> Value {
    let wants = uri.query().is_some_and(|q| q.split('&').any(|kv| kv == "vram=studio"));
    if !wants {
        return local;
    }
    let Some(Some((base, key))) = studio_target(cfg, "GET", uri, headers).await else { return local };
    super::train::hardware(&base, &key).await.ok().filter(|h| h["gpus"].as_array().is_some_and(|a| !a.is_empty())).unwrap_or(local)
}

/// The slugs of the worlds the query names (`worlds=a,b`, `worlds_all=1`, else the active world), in the database's order.
fn chosen_slugs(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Vec<String>> {
    let (db, world) = gm_world(cfg, "POST", uri, headers)?;
    let named: Vec<String> = uri.query().and_then(|q| q.split('&').find_map(|kv| kv.strip_prefix("worlds="))).unwrap_or("").split(',')
        .map(|x| x.trim().to_string()).filter(|x| !x.is_empty() && x.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')).collect();
    if !query_flag(uri, "worlds_all", false) && named.is_empty() {
        return Some(vec![world.get("slug").and_then(|v| v.as_str())?.to_string()]);
    }
    let mut st = db.prepare("SELECT slug FROM worlds ORDER BY id").ok()?;
    let all: Vec<String> = st.query_map([], |r| r.get::<_, String>(0)).ok()?.filter_map(|r| r.ok()).collect();
    Some(all.into_iter().filter(|s| query_flag(uri, "worlds_all", false) || named.contains(s)).collect())
}

/// The same query asking for one world only.
fn with_single_world(uri: &Uri, slug: &str) -> Uri {
    let kept: Vec<&str> = uri.query().unwrap_or("").split('&').filter(|kv| !kv.is_empty() && !kv.starts_with("worlds=") && !kv.starts_with("worlds_all=") && !kv.starts_with("w=") && !kv.starts_with("per_world=")).collect();
    let q = if kept.is_empty() { format!("worlds={slug}") } else { format!("{}&worlds={slug}", kept.join("&")) };
    format!("{}?{q}", uri.path()).parse().unwrap_or_else(|_| uri.clone())
}
