//! `GET /lore-forge` (the management page), `GET /lore-forge/status` (JSON) and the POST actions
//! (`/lore-forge/{install,setup-env,uninstall,train}`, `/lore-forge/runs/{id}/{stop,export,install-model,use-model}`).
//! GM only, for the active world; answered only without Python (see mod.rs). The dispatch arms live in lib.rs.
//!
//! The POST handler splits in two: the synchronous half (validation, filesystem and database writes — below, in
//! `perform`, run inside `spawn_blocking`) and the async half (starting the runner, whose spawn needs the async pool —
//! the `__start` sentinel the sync half returns). `None` anywhere means "not for the gateway"; the proxy forwards.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, Response, StatusCode, Uri};
use serde_json::{json, Value};

use super::jobs::{self, RunParams};
use super::{lf_dir, preset, run_dir, safe_run_id, DEFAULT_METHOD, DEFAULT_PRESET, DEFAULT_QUANT};
use crate::config::Config;
use crate::pages::Pages;

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn reply(code: StatusCode, v: Value) -> Response<Body> {
    Response::builder().status(code).header(header::CONTENT_TYPE, "application/json").header("x-nd-served-by", "gateway").body(Body::from(v.to_string())).unwrap()
}

fn detail(code: StatusCode, m: &str) -> Response<Body> {
    reply(code, json!({"detail": m}))
}

type Db = crate::writes::Pooled;

/// The GM's active world plus the page context — None when the caller is not the GM or has no world
fn gm_ctx(cfg: &Config, method: &str, uri: &Uri, headers: &HeaderMap) -> Option<(Db, crate::layout::PageCtx)> {
    let db = crate::writes::open(cfg)?;
    let now = now_secs();
    let p = crate::layout::build(&db, cfg, method, uri, headers, now)?;
    if !p.is_gm || p.world.is_none() {
        return None;
    }
    Some((db, p))
}

/// A run's live runner entry, keyed by the run_id it carries (train/export/model kinds)
fn live_entry(base: &std::path::Path, kind: &'static str, run_id: &str) -> Option<Value> {
    crate::runners::find(kind, |e| e.get("run_id").and_then(|r| r.as_str()) == Some(run_id))
        .and_then(|id| crate::runners::entry(kind, id))
        .or_else(|| {
            // after a restart the registry is empty: the last recorded job id from state.json
            let st = super::read_state(base);
            let id = st.get("jobs").and_then(|j| j.get(kind)).and_then(|j| j.as_i64())?;
            crate::runners::entry(kind, id)
        })
}

/// The one payload both the page and the status route render from.
pub fn page_state(db: &rusqlite::Connection, cfg: &Config, world_id: i64) -> Value {
    let Some(base) = lf_dir(cfg) else { return json!({"error": "no database folder"}) };
    let st = super::read_state(&base);
    let mut runs: Vec<Value> = st.get("runs").and_then(|r| r.as_object()).map(|o| o.values().cloned().collect()).unwrap_or_default();
    runs.sort_by_key(|r| std::cmp::Reverse(r.get("created").and_then(|c| c.as_str()).map(String::from).unwrap_or_default()));
    for r in runs.iter_mut() {
        let Some(run_id) = r.get("id").and_then(|i| i.as_str()).map(String::from) else { continue };
        // the training entry while it runs; the export entry while an export runs; a finished export's numbers come
        // from the run meta itself
        let kind = if r.get("status").and_then(|s| s.as_str()) == Some("running") {
            "lore_forge_train"
        } else if r.get("export_status").and_then(|s| s.as_str()) == Some("running") {
            "lore_forge_export"
        } else {
            continue;
        };
        if let Some(e) = live_entry(&base, kind, &run_id) {
            if let Some(o) = r.as_object_mut() {
                o.insert("live".into(), e);
            }
        }
    }
    // the world's teacher examples (the chat rows the training can ride on)
    let teacher = crate::teacher::gen::world_dir(cfg.db_path.parent().unwrap_or(std::path::Path::new("/data")), world_id);
    let teacher_count = std::fs::read_to_string(teacher.join("examples.jsonl")).map(|t| t.lines().filter(|l| !l.trim().is_empty()).count()).unwrap_or(0);
    let b = crate::ai::backend(db);
    let model = b.as_ref().map(|b| b.model.clone()).unwrap_or_else(|| {
        let row: Option<String> = db.query_row("SELECT COALESCE(NULLIF(llm_model, ''), NULLIF(ollama_model, '')) FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get(0)).ok().flatten();
        row.unwrap_or_else(|| {
            let env = std::env::var("OLLAMA_MODEL").unwrap_or_default();
            if env.is_empty() { "gemma4:26b".into() } else { env }
        })
    });
    json!({
        "installed": super::installed(&base),
        "branch": st.get("branch").cloned().unwrap_or(Value::Null),
        "installed_at": st.get("installed_at").cloned().unwrap_or(Value::Null),
        "install_error": st.get("install_error").cloned().unwrap_or(Value::Null),
        "env": st.get("env").cloned().unwrap_or_else(|| json!({})),
        "env_ready": super::env_ready(&base),
        "models": super::catalog(&base),
        "presets": super::presets(),
        "methods": super::METHODS.iter().map(|(id, label, blurb)| json!({"id": id, "label": label, "blurb": blurb})).collect::<Vec<_>>(),
        "quants": super::QUANTS,
        "runs": runs,
        "jobs": jobs::jobs_view(&base),
        "teacher_examples": teacher_count,
        "backend": if b.is_some() { "unsloth" } else { "ollama" },
        "model": model,
        "studio_hub_dir": std::env::var("ND_STUDIO_HUB_DIR").unwrap_or_default(),
        "dir": base.to_string_lossy(),
        "repo_url": format!("https://github.com/{}/{}", super::REPO_OWNER, super::REPO_NAME),
    })
}

/// `GET /lore-forge` (is_page) and `GET /lore-forge/status` — dispatched from lib.rs
pub fn get(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, is_page: bool) -> Option<Response<Body>> {
    let (db, mut p) = gm_ctx(cfg, "GET", uri, headers)?;
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()).unwrap_or(0);
    let st = page_state(&db, cfg, wid);
    if !is_page {
        return Some(reply(StatusCode::OK, st));
    }
    p.ctx.insert("lf".into(), st);
    crate::pages::render_with(pages, "lore_forge.html", p.ctx, headers)
}

fn str_field(body: &Value, k: &str, default: &str) -> String {
    body.get(k).and_then(|x| x.as_str()).map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).unwrap_or_else(|| default.to_string())
}

/// The POST actions. `None` = not for the gateway (the proxy forwards to Python, which 404s).
pub async fn post(cfg: Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let path = uri.path().to_string();
    let parsed: Value = if body.iter().all(|b| b.is_ascii_whitespace()) {
        json!({})
    } else {
        serde_json::from_slice(&body).unwrap_or_else(|_| json!({}))
    };
    let c = cfg.clone();
    let u = uri.clone();
    let h = headers.clone();
    // the synchronous half: validation + filesystem/db writes, off the async pool
    let action = tokio::task::spawn_blocking(move || -> Option<Result<Value, (StatusCode, String)>> {
        let (db, p) = gm_ctx(&c, "POST", &u, &h)?;
        let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()).unwrap_or(0);
        let wname = p.world.as_ref().and_then(|w| w.get("name")).and_then(|v| v.as_str()).unwrap_or("world").to_string();
        let wslug = p.world.as_ref().and_then(|w| w.get("slug")).and_then(|v| v.as_str()).unwrap_or("world").to_string();
        let world_map = p.world.clone().unwrap_or_default();
        Some(perform(&c, &db, &path, &parsed, wid, &wname, &wslug, &world_map))
    }).await.ok()??;
    match action {
        Ok(v) => {
            // actions that start a runner answer {"__start": kind, ...params}; their spawn needs the async pool
            let kind = v.get("__start").and_then(|s| s.as_str()).map(String::from);
            match kind.as_deref() {
                Some("install") => match jobs::start_install(&cfg) {
                    Ok(_) => Some(reply(StatusCode::ACCEPTED, json!({"ok": true}))),
                    Err(e) => Some(detail(StatusCode::CONFLICT, &e)),
                },
                Some("setup-env") => match jobs::start_setup_env(&cfg) {
                    Ok(_) => Some(reply(StatusCode::ACCEPTED, json!({"ok": true}))),
                    Err(e) => Some(detail(StatusCode::CONFLICT, &e)),
                },
                Some("train") => {
                    let params = RunParams {
                        run_id: v.get("run_id").and_then(|r| r.as_str()).unwrap_or("").to_string(),
                        world_id: v.get("world_id").and_then(|w| w.as_i64()).unwrap_or(0),
                        world_name: v.get("world").and_then(|w| w.as_str()).unwrap_or("").to_string(),
                        model: v.get("model").cloned().unwrap_or(Value::Null),
                        preset_id: v.get("preset").and_then(|p| p.as_str()).unwrap_or(DEFAULT_PRESET).to_string(),
                        method: v.get("method").and_then(|m| m.as_str()).unwrap_or(DEFAULT_METHOD).to_string(),
                    };
                    let run_id = params.run_id.clone();
                    match jobs::start_train(&cfg, params) {
                        Ok(_) => Some(reply(StatusCode::ACCEPTED, json!({"ok": true, "run_id": run_id}))),
                        Err(e) => Some(detail(StatusCode::CONFLICT, &e)),
                    }
                }
                Some("export") => {
                    let run_id = v.get("run_id").and_then(|r| r.as_str()).unwrap_or("").to_string();
                    let quant = v.get("quant").and_then(|q| q.as_str()).unwrap_or(DEFAULT_QUANT).to_string();
                    match jobs::start_export(&cfg, &run_id, &quant) {
                        Ok(_) => Some(reply(StatusCode::ACCEPTED, json!({"ok": true}))),
                        Err(e) => Some(detail(StatusCode::CONFLICT, &e)),
                    }
                }
                Some("model") => {
                    let run_id = v.get("run_id").and_then(|r| r.as_str()).unwrap_or("").to_string();
                    let name = v.get("name").and_then(|n| n.as_str()).unwrap_or("").to_string();
                    let targets: Vec<String> = v.get("targets").and_then(|t| t.as_array())
                        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
                    match jobs::start_install_model(&cfg, &run_id, &name, &targets.iter().map(|s| s.as_str()).collect::<Vec<_>>()) {
                        Ok(_) => Some(reply(StatusCode::ACCEPTED, json!({"ok": true}))),
                        Err(e) => Some(detail(StatusCode::CONFLICT, &e)),
                    }
                }
                _ => Some(reply(StatusCode::OK, v)),
            }
        }
        Err((code, msg)) => Some(detail(code, &msg)),
    }
}

/// The synchronous part of every action: validation + filesystem/db writes, returning either the answer JSON or an
/// error. Actions that start a runner return `{"__start": "kind", ...}` (the async half above spawns the job).
fn perform(cfg: &Config, db: &Db, path: &str, body: &Value, world_id: i64, world_name: &str, world_slug: &str, world: &serde_json::Map<String, Value>) -> Result<Value, (StatusCode, String)> {
    let base = lf_dir(cfg).ok_or((StatusCode::INTERNAL_SERVER_ERROR, "No database folder.".into()))?;
    match path {
        "/lore-forge/install" => Ok(json!({"__start": "install"})),
        "/lore-forge/setup-env" => {
            if !super::installed(&base) {
                return Err((StatusCode::CONFLICT, "Install Lore Forge first.".into()));
            }
            Ok(json!({"__start": "setup-env"}))
        }
        "/lore-forge/uninstall" => {
            jobs::start_uninstall(cfg).map_err(|e| (StatusCode::CONFLICT, e))?;
            Ok(json!({"ok": true, "removed": base.to_string_lossy()}))
        }
        "/lore-forge/train" => {
            if !super::installed(&base) {
                return Err((StatusCode::CONFLICT, "Install Lore Forge first.".into()));
            }
            if !super::env_ready(&base) {
                return Err((StatusCode::CONFLICT, "Set up the training environment first.".into()));
            }
            let models = super::catalog(&base);
            let repo = str_field(body, "model", "");
            let Some(model) = super::model_by_repo(&models, &repo) else {
                return Err((StatusCode::UNPROCESSABLE_ENTITY, "Pick one of the catalog's models (its Hugging Face repo id).".into()));
            };
            let preset_id = str_field(body, "preset", DEFAULT_PRESET);
            if preset(&preset_id).is_none() {
                return Err((StatusCode::UNPROCESSABLE_ENTITY, "The preset must be one of quick / normal / best.".into()));
            }
            let method = str_field(body, "method", DEFAULT_METHOD);
            if !super::METHODS.iter().any(|(id, _, _)| *id == method) {
                return Err((StatusCode::UNPROCESSABLE_ENTITY, "That training method is not one the worker accepts.".into()));
            }
            let run_id = format!("{:x}-{}", now_secs(), tag());
            let run = run_dir(&base, &run_id).ok_or((StatusCode::BAD_REQUEST, "bad run id".into()))?;
            std::fs::create_dir_all(super::run_out(&run)).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("creating the run folder: {e}")))?;
            let members = crate::export::training_members(db, world).ok_or((StatusCode::INTERNAL_SERVER_ERROR, "The world could not be read.".into()))?;
            let l = crate::teacher::docs::load(&crate::teacher::docs::Members(&members))
                .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("The world could not be read: {e}")))?;
            let epochs = body.get("epochs").and_then(|e| e.as_i64()).filter(|n| *n > 0);
            let include_teacher = body.get("include_teacher").and_then(|b| b.as_bool()).unwrap_or(true);
            let (text_rows, chat_rows) = super::rows::train_rows(&l, &base, world_id, include_teacher);
            if text_rows.is_empty() && chat_rows.is_empty() {
                return Err((StatusCode::UNPROCESSABLE_ENTITY, "This world has no text to train on yet — write some lore first (or make teacher examples).".into()));
            }
            let mut train = String::new();
            for r in text_rows.iter().chain(chat_rows.iter()) {
                train.push_str(&r.to_string());
                train.push('\n');
            }
            std::fs::write(run.join("train.jsonl"), train).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("writing train.jsonl: {e}")))?;
            let probes = super::rows::probes(&l);
            let overrides = body.get("hyper").cloned().unwrap_or_else(|| json!({}));
            let job = super::rows::train_job(&model, &preset(&preset_id).unwrap(), &method, &overrides, epochs, &run.join("train.jsonl"), &super::run_out(&run), &probes);
            std::fs::write(run.join("job.json"), serde_json::to_vec(&job).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("{e}")))?)
                .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("writing job.json: {e}")))?;
            super::update_run(&base, &run_id, json!({
                "id": run_id, "world_id": world_id, "world": world_name, "world_slug": world_slug,
                "created": now_secs(), "status": "ready",
                "model": model, "preset": preset_id, "method": method, "epochs": epochs,
                "text_rows": text_rows.len(), "chat_rows": chat_rows.len(), "probes": probes.len(),
            }));
            Ok(json!({"__start": "train", "run_id": run_id, "world_id": world_id, "world": world_name,
                      "model": model, "preset": preset_id, "method": method}))
        }
        p if p.starts_with("/lore-forge/runs/") => {
            let parts: Vec<&str> = p.trim_start_matches("/lore-forge/runs/").split('/').collect();
            if parts.len() != 2 {
                return Err((StatusCode::NOT_FOUND, "Unknown action.".into()));
            }
            let (run_id, action) = (parts[0], parts[1]);
            if safe_run_id(run_id).is_none() {
                return Err((StatusCode::BAD_REQUEST, "bad run id".into()));
            }
            match action {
                "stop" => {
                    jobs::stop_run(cfg, run_id).map_err(|e| (StatusCode::CONFLICT, e))?;
                    Ok(json!({"ok": true}))
                }
                "export" => {
                    let quant = str_field(body, "quant", DEFAULT_QUANT);
                    if !super::QUANTS.contains(&quant.as_str()) {
                        return Err((StatusCode::UNPROCESSABLE_ENTITY, "That quant is not one the exporter writes (q4_k_m / q5_k_m / q6_k / q8_0 / f16).".into()));
                    }
                    Ok(json!({"__start": "export", "run_id": run_id, "quant": quant}))
                }
                "install-model" => {
                    let st = super::read_state(&base);
                    if st.pointer(&format!("/runs/{run_id}")).is_none() {
                        return Err((StatusCode::NOT_FOUND, "That run does not exist.".into()));
                    }
                    let suffix: String = run_id.chars().rev().take(6).collect::<String>().chars().rev().collect();
                    let name = str_field(body, "name", &format!("lore-forge-{world_slug}-{suffix}")).replace([' ', '/'], "-");
                    let t = body.get("target").and_then(|x| x.as_str()).unwrap_or("ollama");
                    let targets: Vec<&str> = match t {
                        "ollama" => vec!["ollama"],
                        "studio" => vec!["studio"],
                        "both" => vec!["ollama", "studio"],
                        _ => return Err((StatusCode::UNPROCESSABLE_ENTITY, "The target must be ollama / studio / both.".into())),
                    };
                    if targets.contains(&"studio") && std::env::var("ND_STUDIO_HUB_DIR").unwrap_or_default().trim().is_empty() {
                        return Err((StatusCode::CONFLICT, "ND_STUDIO_HUB_DIR is not set — this process cannot see the Studio hub's folder. Set it, or install into Ollama.".into()));
                    }
                    Ok(json!({"__start": "model", "run_id": run_id, "name": name, "targets": targets}))
                }
                "use-model" => {
                    let st = super::read_state(&base);
                    let meta = st.pointer(&format!("/runs/{run_id}")).ok_or((StatusCode::NOT_FOUND, "That run does not exist.".into()))?;
                    let name = str_field(body, "name", meta.get("model_name").and_then(|n| n.as_str()).unwrap_or(""));
                    if name.is_empty() {
                        return Err((StatusCode::UNPROCESSABLE_ENTITY, "Install the model first (or name it explicitly).".into()));
                    }
                    // app_settings.llm_model wins over ollama_model in the gateway's ai::backend chain — the chat
                    // answers from the trained model from the next request on
                    db.execute("UPDATE app_settings SET llm_model = ?1 WHERE id = (SELECT MIN(id) FROM app_settings)", rusqlite::params![name])
                        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("{e}")))?;
                    Ok(json!({"ok": true, "model": name}))
                }
                _ => Err((StatusCode::NOT_FOUND, "Unknown action.".into())),
            }
        }
        _ => Err((StatusCode::NOT_FOUND, "Unknown action.".into())),
    }
}

fn tag() -> String {
    use rand::Rng;
    let mut rng = rand::thread_rng();
    (0..6).map(|_| format!("{:x}", rng.gen_range(0..16))).collect()
}
