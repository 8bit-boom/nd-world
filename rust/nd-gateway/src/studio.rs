//! The Unsloth Studio status routes behind `/api/ai/unsloth/*` (app/routers/ai.py "Unsloth Studio extras", app/unsloth_extras.py): the
//! read-only passthroughs the AI page and Settings call - the model hub list, image-model status, auto-switch and per-model overrides, the
//! consolidated status card, the key check, the version and the diagnostic probe. Each asks Studio exactly what Python asks and answers with
//! Studio's own JSON (or Python's wording for an error); the key-death banner state is kept here, in memory, as Python keeps it. Studio
//! answering over https, a body that is not the shape Python reads, or anything Python would fail on is left to Python (None).

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode, Uri};
use serde_json::{json, Map, Value};

use crate::ai::Backend;
use crate::config::Config;

/// One failed call to Studio: its status, Python's message for it, and what kind of failure it was
struct Failure {
    status: u16,
    msg: String,
    /// `StudioEndpointMissing`: a 404 that means "this build has no such route"
    missing: bool,
    /// a transport failure (nothing answered)
    unreachable: bool,
}

// the key-death banner's two timestamps (f64 bits; 0 = never), as `_last_auth_failure` / `_last_auth_ok`
static LAST_FAIL: AtomicU64 = AtomicU64::new(0);
static LAST_OK: AtomicU64 = AtomicU64::new(0);

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

pub(crate) fn note_auth_failure() {
    LAST_FAIL.store(now().to_bits(), Ordering::Relaxed);
}

pub(crate) fn note_auth_ok() {
    LAST_OK.store(now().to_bits(), Ordering::Relaxed);
}

/// `auth_status()`: whether Studio rejected the key with no success since; `url` is None when no key is set
fn auth_status(url: Option<&str>) -> Value {
    let (fail, ok) = (f64::from_bits(LAST_FAIL.load(Ordering::Relaxed)), f64::from_bits(LAST_OK.load(Ordering::Relaxed)));
    let configured = url.is_some();
    let standing = configured && fail > 0.0 && fail > ok;
    json!({
        "configured": configured,
        "url": url.unwrap_or(""),
        "key_failed": standing,
        "failed_at": if standing { json!(fail) } else { Value::Null },
        "ok_at": if ok != 0.0 { json!(ok) } else { Value::Null },
        "hint": if standing {
            "Studio rejected the API key (401). Keys do not survive Studio container recreations (TrueNAS app updates recreate it) \u{2014} re-create the key in Studio \u{2192} Settings \u{2192} API, then paste it into nd-world Settings \u{2192} System."
        } else { "" },
    })
}

fn base_url(b: &Backend) -> String {
    b.base_url()
}

fn query_string(query: &[(&str, &str)]) -> String {
    use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
    const SET: &AsciiSet = &NON_ALPHANUMERIC.remove(b'-').remove(b'_').remove(b'.').remove(b'~');
    if query.is_empty() {
        return String::new();
    }
    format!("?{}", query.iter().map(|(k, v)| format!("{}={}", utf8_percent_encode(k, SET), utf8_percent_encode(v, SET))).collect::<Vec<_>>().join("&"))
}

/// `_is_route_missing`: a 404 with no JSON object body, or the bare "not found" wording
fn route_missing(body: &[u8], status: u16) -> bool {
    match serde_json::from_slice::<Value>(body) {
        Ok(Value::Object(o)) if !o.is_empty() => {
            let m = crate::studio_key::error_message(body, status);
            let m = m.trim().to_lowercase();
            let m = m.trim_end_matches('.');
            m == "not found" || m == "api endpoint not found" || m == "endpoint not found" || m.contains("endpoint not found")
        }
        _ => true,
    }
}

/// What `_request` makes of Studio's answer: its JSON (an empty or non-JSON body is `{}`), or the failure
fn interpret(path: &str, status: u16, body: &[u8]) -> Result<Value, Failure> {
    let auth_relevant = path.starts_with("/v1/") || path.starts_with("/api/inference/");
    if status == 401 && auth_relevant {
        note_auth_failure();
    }
    if status == 404 && route_missing(body, status) {
        return Err(Failure { status: 404, msg: format!("This Studio build doesn't provide {path} \u{2014} update Studio to use it"), missing: true, unreachable: false });
    }
    if status >= 400 {
        return Err(Failure { status, msg: crate::studio_key::error_message(body, status), missing: false, unreachable: false });
    }
    if status < 300 && auth_relevant {
        note_auth_ok();
    }
    if body.is_empty() {
        return Ok(json!({}));
    }
    if !crate::edits::plain_numbers(body) || body.windows(3).any(|w| w == b"NaN") || body.windows(8).any(|w| w == b"Infinity") {
        return Err(Failure { status: 0, msg: String::new(), missing: false, unreachable: false }); // not ours to re-serialise
    }
    Ok(serde_json::from_slice::<Value>(body).unwrap_or_else(|_| json!({})))
}

/// `_request("GET", path, params=...)`
async fn get(b: &Backend, path: &str, query: &[(&str, &str)]) -> Result<Value, Failure> {
    let uri = format!("{}{}{}", base_url(b), path, query_string(query));
    let uri: hyper::Uri = uri.parse().map_err(|e| Failure { status: 503, msg: format!("Unsloth Studio unreachable: InvalidURL: {e}"), missing: false, unreachable: true })?;
    let (status, body) = crate::studio_key::studio_get(uri, &b.key)
        .await
        .map_err(|e| Failure { status: 503, msg: format!("Unsloth Studio unreachable: {e}"), missing: false, unreachable: true })?;
    interpret(path, status, &body)
}

/// `_request(method, path, json_body=...)` for the actions (60 s)
async fn send(b: &Backend, method: &str, path: &str, body: Option<&Value>) -> Result<Value, Failure> {
    use http_body_util::BodyExt;
    let unreachable = |e: String| Failure { status: 503, msg: format!("Unsloth Studio unreachable: {e}"), missing: false, unreachable: true };
    let req = crate::ai::backend_request(b, method, path, body).ok_or_else(|| unreachable("InvalidURL".into()))?;
    let call = async {
        let resp = crate::ai::client().request(req).await.map_err(|e| unreachable(if e.is_connect() { format!("ConnectError: {e}") } else { format!("HTTPError: {e}") }))?;
        let status = resp.status().as_u16();
        let bytes = resp.into_body().collect().await.map_err(|e| unreachable(format!("HTTPError: {e}")))?.to_bytes();
        Ok::<_, Failure>((status, bytes))
    };
    let (status, bytes) = tokio::time::timeout(std::time::Duration::from_secs(60), call).await.map_err(|_| unreachable("ReadTimeout: timed out".into()))??;
    interpret(path, status, &bytes)
}

/// `HTTPException(exc.status_code, str(exc))`; a failure that is only "not ours" (status 0) declines
fn failure_response(f: Failure) -> Option<Response<Body>> {
    if f.status == 0 {
        return None;
    }
    StatusCode::from_u16(f.status).ok().map(|s| crate::ai_more::detail(s, &f.msg))
}

fn ok(v: &Value) -> Option<Response<Body>> {
    crate::edits::json_response(v)
}

fn py_truthy(v: Option<&Value>) -> bool {
    crate::edits::py_truthy_pub(v)
}

/// What the gate learned in the blocking part
struct Gate {
    backend: Option<Backend>,
    /// Studio's address as Python reports it when a key is set ("" otherwise)
    url: String,
    saved_key: String,
    saved_url: String,
}

/// `GET /api/ai/unsloth/...` for a signed-in GM; None = Python's (anyone else, a Studio over https, ...)
pub async fn route(cfg: Arc<Config>, uri: Uri, headers: HeaderMap) -> Option<Response<Body>> {
    // GET /api/ai/imagegen/models with Studio as the image backend (app/ai.py imagegen_models): the image models Studio has cached
    if let Some(token) = uri.path().strip_prefix("/api/ai/unsloth/check/") {
        return if crate::config::python_free() { poll_check(&cfg, &uri, &headers, token).await } else { None };
    }
    let path = if uri.path() == "/api/ai/imagegen/models" { "imagegen-models".to_string() } else { uri.path().strip_prefix("/api/ai/unsloth/")?.to_string() };
    if !matches!(
        path.as_str(),
        "models" | "image/status" | "image/load-progress" | "auto-switch" | "status" | "context-overrides" | "audio-models" | "version" | "probe"
            | "hub/download-progress" | "gguf-variants" | "key-check" | "auth-status" | "imagegen-models"
    ) {
        return None;
    }
    let repo_id = match crate::pages::query_param(&uri, "repo_id") {
        Some(r) => Some(r),
        None if matches!(path.as_str(), "hub/download-progress" | "gguf-variants") => return None, // the 422 for the missing parameter
        None => None,
    };
    let (c, u, h) = (cfg.clone(), uri.clone(), headers.clone());
    let gate = tokio::task::spawn_blocking(move || -> Option<Gate> {
        let db = crate::writes::open(&c)?;
        let n = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        let p = crate::layout::build(&db, &c, "GET", &u, &h, n)?;
        if !p.is_gm {
            return None;
        }
        let (saved_key, saved_url): (Option<String>, Option<String>) = db
            .query_row("SELECT llm_api_key, COALESCE(NULLIF(llm_url, ''), ollama_url) FROM app_settings ORDER BY id LIMIT 1", [], |r| Ok((r.get(0)?, r.get(1)?)))
            .ok()?;
        let backend = crate::ai::backend(&db);
        let url = backend.as_ref().map(base_url).unwrap_or_default();
        Some(Gate { backend, url, saved_key: saved_key.unwrap_or_default(), saved_url: saved_url.unwrap_or_default().trim_end_matches('/').to_string() })
    })
    .await
    .ok()??;
    let env_key = std::env::var("UNSLOTH_API_KEY").unwrap_or_default();
    let effective_key = if gate.saved_key.is_empty() { env_key.clone() } else { gate.saved_key.clone() };

    // the two routes that answer without a key
    if path == "auth-status" {
        if effective_key.is_empty() {
            return ok(&auth_status(None));
        }
        let b = gate.backend.as_ref()?;
        return ok(&auth_status(Some(&base_url(b))));
    }
    if path == "key-check" {
        let url = crate::studio_key::effective_url(&gate.saved_url, &effective_key);
        let res = if effective_key.is_empty() {
            json!({"ok": false, "status": 0, "url": url, "message": "No Studio API key is set."})
        } else {
            gate.backend.as_ref()?; // https and the like: Python's
            let res = crate::studio_key::verify_key(&url, &effective_key).await;
            match res.get("status").and_then(|s| s.as_i64()) {
                Some(401) => note_auth_failure(),
                Some(200) => note_auth_ok(),
                _ => {}
            }
            res
        };
        let mut info = res.as_object().cloned().unwrap_or_default();
        let sources = crate::studio_key::key_sources(&gate.saved_key, &env_key);
        if let Some(o) = sources.as_object() {
            for (k, v) in o {
                info.insert(k.clone(), v.clone());
            }
        }
        let explain = crate::studio_key::key_explanation(&info);
        info.insert("explain".into(), json!(explain));
        return ok(&Value::Object(info));
    }

    let b = gate.backend.as_ref()?; // no key is apis.rs's 400; https is Python's
    match path.as_str() {
        "imagegen-models" => {
            let default = std::env::var("UNSLOTH_IMAGE_MODEL").unwrap_or_default();
            if effective_key.is_empty() || default.is_empty() {
                return None; // Studio is not the image backend: apis.rs answers
            }
            image_models(b, &default).await
        }
        "models" => models(b).await,
        "image/status" => passthrough(b, "/api/inference/images/status", &[]).await,
        "image/load-progress" => passthrough(b, "/api/inference/images/load-progress", &[]).await,
        "auto-switch" => passthrough(b, "/api/settings/openai-auto-switch", &[]).await,
        "hub/download-progress" => passthrough(b, "/api/hub/download-progress", &[("repo_id", repo_id.as_deref()?)]).await,
        "gguf-variants" => passthrough(b, "/api/hub/gguf-variants", &[("repo_id", repo_id.as_deref()?)]).await,
        "context-overrides" => match get(b, "/api/settings/openai-auto-switch/overrides", &[]).await {
            Ok(data) => ok(&json!({"available": true, "data": data})),
            Err(f) if f.missing => ok(&json!({"available": false,
                "hint": "This Studio build doesn't expose per-model overrides \u{2014} update Studio to manage them here."})),
            Err(f) => failure_response(f),
        },
        "audio-models" => audio_models(&cfg, b).await,
        "version" => version(b).await,
        "probe" => probe(b).await,
        "status" => status(b).await,
        _ => None,
    }
}

/// `str(body.get(key) or "").strip()` for a JSON body value; None when the value is not text, nothing or false (Python would format it)
fn text_field(body: &Map<String, Value>, key: &str) -> Option<String> {
    match body.get(key) {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Some(String::new()),
        Some(Value::String(s)) => Some(nd_render::text::py_strip(s).to_string()),
        Some(_) => None,
    }
}

/// `POST /api/ai/unsloth/...` actions for a signed-in GM: hub download, image load (file name given) / unload, auto-switch and per-model
/// overrides. None: Python's (anyone else, a body that is not a JSON object, a Studio over https, ...).
pub async fn post(cfg: Arc<Config>, uri: Uri, headers: HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let path = uri.path().strip_prefix("/api/ai/unsloth/")?.to_string();
    let check = matches!(path.as_str(), "stt-check" | "tts-check") && crate::config::python_free();
    if !check && !matches!(path.as_str(), "hub/download" | "image/load" | "image/unload" | "auto-switch" | "context-overrides" | "update") {
        return None;
    }
    let (c, u, h) = (cfg.clone(), uri.clone(), headers.clone());
    let b = tokio::task::spawn_blocking(move || -> Option<Backend> {
        let db = crate::writes::open(&c)?;
        let n = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        let p = crate::layout::build(&db, &c, "POST", &u, &h, n)?;
        if !p.is_gm {
            return None;
        }
        crate::ai::backend(&db)
    })
    .await
    .ok()??;
    if check {
        let obj = serde_json::from_slice::<Value>(body).ok()?.as_object()?.clone();
        return health_check(&cfg, b, path == "tts-check", &obj).await;
    }
    if path == "image/unload" {
        return match send(&b, "POST", "/api/inference/images/unload", None).await {
            Ok(v) => ok(&v),
            Err(f) => failure_response(f),
        };
    }
    if path == "update" {
        // studio_update(): POST /api/update, then /update on a missing route; a 405 (Python may retry as PUT) is Python's
        for p in ["/api/update", "/update"] {
            match send(&b, "POST", p, None).await {
                Ok(v) if v.is_object() => return ok(&json!({"ok": true, "result": v})),
                Ok(_) => return None,
                Err(f) if f.missing => continue,
                Err(f) if f.status == 405 => return None,
                Err(f) => return failure_response(Failure { msg: format!("Unsloth Studio update: {}", f.msg), ..f }),
            }
        }
        return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, "This Studio build doesn't expose an update API \u{2014} update the Docker image (or Desktop app) and recreate the container."));
    }
    let parsed: Value = serde_json::from_slice(body).ok()?;
    let obj = parsed.as_object()?;
    let bad = |msg: &str| Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, msg));
    let result = match path.as_str() {
        "hub/download" => {
            let repo = text_field(obj, "repo_id")?;
            if repo.is_empty() {
                return bad("repo_id is required");
            }
            let variant = text_field(obj, "gguf_variant")?;
            let mut req = Map::new();
            req.insert("repo_id".into(), json!(repo));
            if !variant.is_empty() {
                req.insert("gguf_variant".into(), json!(variant));
            }
            send(&b, "POST", "/api/hub/download", Some(&Value::Object(req))).await
        }
        "image/load" => {
            let repo = text_field(obj, "repo_id")?;
            if repo.is_empty() {
                return bad("repo_id is required");
            }
            let file = text_field(obj, "gguf_filename")?;
            if file.is_empty() {
                return None; // the repo's default file is looked up (Studio's variants, then Hugging Face): Python's
            }
            send(&b, "POST", "/api/inference/images/load", Some(&json!({"model_path": repo, "model_kind": "gguf", "gguf_filename": file}))).await
        }
        "auto-switch" => {
            let mut req = Map::new();
            for key in ["enabled", "media_auto_switch_model"] {
                match obj.get(key) {
                    None | Some(Value::Null) => {}
                    Some(v) => {
                        req.insert(key.into(), json!(py_truthy(Some(v))));
                    }
                }
            }
            match obj.get("auto_unload_idle_seconds") {
                None | Some(Value::Null) => {}
                Some(Value::Number(n)) if n.is_i64() => {
                    req.insert("auto_unload_idle_seconds".into(), json!(n.as_i64()?.max(0)));
                }
                Some(Value::Bool(v)) => {
                    req.insert("auto_unload_idle_seconds".into(), json!(*v as i64));
                }
                Some(_) => return None, // int() of anything else
            }
            send(&b, "PUT", "/api/settings/openai-auto-switch", Some(&Value::Object(req))).await
        }
        _ => {
            let model = text_field(obj, "model_id")?;
            if model.is_empty() {
                return bad("model_id is required");
            }
            match send(&b, "PUT", "/api/settings/openai-auto-switch/overrides", Some(&parsed)).await {
                Err(f) if f.missing => return Some(crate::ai_more::detail(StatusCode::NOT_FOUND, "This Studio build doesn't expose per-model overrides")),
                other => other,
            }
        }
    };
    match result {
        Ok(v) => ok(&v),
        Err(f) => failure_response(f),
    }
}

/// `_client().list()` on a Studio backend: (model id, loaded flag) from `/v1/models`; [] when Studio fails. None when an id is not text
/// (Python would fail on it later)
async fn v1_models(b: &Backend) -> Option<Vec<(String, bool)>> {
    let Ok(uri) = format!("{}/v1/models", base_url(b)).parse::<hyper::Uri>() else { return Some(vec![]) };
    let got = tokio::time::timeout(std::time::Duration::from_secs(8), crate::studio_key::studio_get(uri, &b.key)).await;
    let Ok(Ok((status, body))) = got else { return Some(vec![]) };
    if status >= 400 {
        return Some(vec![]);
    }
    let Ok(Value::Object(o)) = serde_json::from_slice::<Value>(&body) else { return Some(vec![]) };
    let mut out = vec![];
    match o.get("data") {
        Some(Value::Array(a)) => {
            for m in a {
                let Some(m) = m.as_object() else { continue };
                match m.get("id") {
                    Some(Value::String(id)) if !id.is_empty() => out.push((id.clone(), py_truthy(m.get("loaded")))),
                    Some(v) if py_truthy(Some(v)) => return None,
                    _ => {}
                }
            }
        }
        Some(v) if py_truthy(Some(v)) => return Some(vec![]), // iterating it fails: the empty list
        _ => {}
    }
    Some(out)
}

/// `POST /api/ai/status` (app/ai.py `status`): what the AI dot on every page shows. On a Studio backend: "ok" with the model names `/v1/models`
/// lists, or "unavailable" when it fails or says something unreadable (Python's `except Exception`). The legacy Ollama back end stays Python's.
pub async fn ai_status_route(cfg: Arc<Config>) -> Option<Response<Body>> {
    let c = cfg.clone();
    let got = tokio::task::spawn_blocking(move || -> Option<Result<Backend, String>> {
        let db = crate::writes::open(&c)?;
        Some(crate::ai::backend(&db).ok_or_else(|| legacy_model(&db)))
    })
    .await
    .ok()??;
    let b = match got {
        Ok(b) => b,
        // no Studio key: Python asks the legacy Ollama client, which is not there in this image - "unavailable"
        Err(model) if crate::config::python_free() => return ok(&json!({"status": "unavailable", "model": model, "backend": "ollama"})),
        Err(_) => return None,
    };
    let down = || ok(&json!({"status": "unavailable", "model": b.model, "backend": "unsloth"}));
    let uri: hyper::Uri = format!("{}/v1/models", base_url(&b)).parse().ok()?;
    let got = tokio::time::timeout(std::time::Duration::from_secs(15), crate::studio_key::studio_get(uri, &b.key)).await;
    let Ok(Ok((status, body))) = got else { return down() };
    if status >= 400 || !crate::edits::plain_numbers(&body) {
        return if status >= 400 { down() } else { None };
    }
    let Ok(Value::Object(o)) = serde_json::from_slice::<Value>(&body) else { return down() };
    let mut ids = vec![];
    match o.get("data") {
        None | Some(Value::Null) => {}
        Some(Value::Array(a)) => {
            for m in a {
                let Some(m) = m.as_object() else { continue };
                match m.get("id") {
                    Some(Value::String(id)) if !id.is_empty() => ids.push(json!(id)),
                    Some(v) if py_truthy(Some(v)) => return None, // a model name that is not text: Python's
                    _ => {}
                }
            }
        }
        Some(_) => return None,
    }
    ok(&json!({"status": "ok", "model": b.model, "loaded_models": ids, "backend": "unsloth"}))
}

/// `GET /api/ai/models` with no Studio key (Python's `ai_models` with an empty `loaded`): the legacy catalogue plus the saved models, none loaded
fn legacy_models(cfg: &Config, default: &str) -> Option<Response<Body>> {
    let data: Value = std::fs::read_to_string(cfg.db_path.parent()?.join("ai_models.json")).ok()
        .and_then(|t| if crate::edits::plain_numbers(t.as_bytes()) && !t.contains("NaN") && !t.contains("Infinity") { serde_json::from_str(&t).ok() } else { None })
        .unwrap_or_else(|| json!({"custom": [], "hidden": []}));
    let list = |k: &str| data.get(k).and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let (hidden, custom) = (list("hidden"), list("custom"));
    let builtins = [
        json!({"id": "gemma4:26b", "label": "Gemma 4 26B"}),
        json!({"id": "hf.co/noctrex/gemma-4-26B-A4B-it-MXFP4_MOE-GGUF:gemma-4-26B-A4B-it-MXFP4_MOE.gguf", "label": "Gemma 4 26B MXFP4"}),
        json!({"id": "hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:gemma-4-26B-A4B-it-UD-IQ4_NL.gguf", "label": "Gemma 4 26B IQ4_NL (Unsloth)", "thinking": true}),
    ];
    let mut models: Vec<Value> = vec![];
    for m in builtins.iter().filter(|m| !hidden.contains(&m["id"])) {
        let mut e = m.as_object()?.clone();
        e.insert("loaded".into(), json!(false));
        e.insert("builtin".into(), json!(true));
        models.push(Value::Object(e));
    }
    for m in &custom {
        let o = m.as_object()?;
        let id = o.get("id")?;
        if builtins.iter().any(|b| b["id"] == *id) || hidden.contains(id) {
            continue;
        }
        let mut e = o.clone();
        e.insert("loaded".into(), json!(false));
        e.insert("builtin".into(), json!(false));
        models.push(Value::Object(e));
    }
    ok(&json!({"models": models, "default": default, "available": [], "defaults": crate::apis::model_defaults(cfg)?}))
}

/// `effective_ollama_model()` with no Studio key: the Settings override, else OLLAMA_MODEL, else gemma4:26b
fn legacy_model(db: &rusqlite::Connection) -> String {
    let saved: Option<String> = db.query_row("SELECT llm_model FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get(0)).ok().flatten();
    saved.filter(|m| !m.is_empty()).or_else(|| std::env::var("OLLAMA_MODEL").ok().filter(|m| !m.is_empty())).unwrap_or_else(|| "gemma4:26b".into())
}

/// `GET /api/ai/models` and `/api/ai/resident` on a Studio backend (the legacy Ollama backend is Python's). GM only.
pub async fn ai_models_route(cfg: Arc<Config>, uri: Uri, headers: HeaderMap) -> Option<Response<Body>> {
    let path = uri.path().to_string();
    let (c, u, h) = (cfg.clone(), uri.clone(), headers.clone());
    let got = tokio::task::spawn_blocking(move || -> Option<Result<Backend, String>> {
        let db = crate::writes::open(&c)?;
        let n = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        let p = crate::layout::build(&db, &c, "GET", &u, &h, n)?;
        if !p.is_gm {
            return None;
        }
        Some(crate::ai::backend(&db).ok_or_else(|| legacy_model(&db)))
    })
    .await
    .ok()??;
    let b = match got {
        Ok(b) => b,
        // no Studio key: the legacy Ollama client is not in this image, so nothing is loaded - the catalogue still lists
        Err(model) if crate::config::python_free() && path == "/api/ai/models" => return legacy_models(&cfg, &model),
        Err(_) => return None,
    };
    if path == "/api/ai/debug" {
        // `debug_info()` when Studio answers both probes; with no Python the failure is answered here too (the page's status line waits on it)
        let unreachable = |why: String| {
            ok(&json!({"ollama_url": base_url(&b), "ollama_reachable": false, "backend": "unsloth", "error": why, "default_model": b.model,
                "stt": {"ok": false, "reason": "Unsloth Studio unreachable", "backend": "unsloth", "url": base_url(&b)}}))
        };
        let uri: hyper::Uri = format!("{}/v1/models", base_url(&b)).parse().ok()?;
        let probe = tokio::time::timeout(std::time::Duration::from_secs(10), crate::studio_key::studio_get(uri, &b.key)).await;
        let (status, body) = match probe {
            Ok(Ok(r)) => r,
            Ok(Err(e)) if crate::config::python_free() => return unreachable(format!("ConnectError: {e}")),
            Err(_) if crate::config::python_free() => return unreachable("ReadTimeout: timed out".into()),
            _ => return None,
        };
        if status >= 400 {
            return if crate::config::python_free() { unreachable(format!("HTTP {status}")) } else { None };
        }
        if !crate::edits::plain_numbers(&body) {
            return None;
        }
        let Value::Object(o) = serde_json::from_slice::<Value>(&body).ok()? else { return None };
        let mut ids = vec![];
        if let Some(Value::Array(a)) = o.get("data") {
            for m in a {
                match m.as_object().and_then(|m| m.get("id")) {
                    Some(Value::String(id)) if !id.is_empty() => ids.push(json!(id)),
                    Some(v) if py_truthy(Some(v)) => return None,
                    _ => {}
                }
            }
        } else if o.get("data").is_some_and(|d| py_truthy(Some(d))) {
            return None;
        }
        let stt = match tokio::time::timeout(std::time::Duration::from_secs(10), get(&b, "/api/hub/cached-gguf", &[])).await {
            Ok(Ok(_)) => json!({"ok": true, "backend": "unsloth", "url": base_url(&b)}),
            _ if crate::config::python_free() => json!({"ok": false, "reason": "Unsloth Studio did not answer in time", "backend": "unsloth", "url": base_url(&b)}),
            _ => return None,
        };
        return ok(&json!({"ollama_url": base_url(&b), "ollama_reachable": true, "backend": "unsloth", "loaded_models": ids, "default_model": b.model, "stt": stt}));
    }
    let listed = v1_models(&b).await?;
    if path == "/api/ai/resident" {
        let models: Vec<Value> = listed.iter().filter(|m| m.1)
            .map(|m| json!({"model": m.0, "size_bytes": null, "size_vram_bytes": null, "size_ram_bytes": null, "expires_at": null})).collect();
        return ok(&json!({"models": models}));
    }
    // the catalogue: the Studio built-in, the ones saved in ai_models.json (minus the hidden), each marked loaded / built in
    let data: Value = match std::fs::read(cfg.db_path.parent()?.join("ai_models.json")) {
        Ok(bytes) => {
            let t = String::from_utf8(bytes).ok()?;
            if !crate::edits::plain_numbers(t.as_bytes()) || t.contains("NaN") || t.contains("Infinity") {
                return None;
            }
            serde_json::from_str(&t).unwrap_or_else(|_| json!({"custom": [], "hidden": []}))
        }
        Err(_) => json!({"custom": [], "hidden": []}),
    };
    let data = data.as_object()?;
    let hidden: Vec<Value> = match data.get("hidden") {
        None => vec![],
        Some(Value::Array(a)) => a.clone(),
        Some(_) => return None,
    };
    let custom: Vec<Value> = match data.get("custom") {
        None => vec![],
        Some(Value::Array(a)) => a.clone(),
        Some(_) => return None,
    };
    let builtin = json!({"id": "unsloth/gemma-4-26B-A4B-it-GGUF", "label": "Gemma 4 26B (Unsloth, IQ4_NL)", "thinking": true});
    let mut all: Vec<Value> = vec![];
    if !hidden.contains(&builtin["id"]) {
        all.push(builtin.clone());
    }
    for m in &custom {
        let id = m.as_object()?.get("id")?;
        if *id != builtin["id"] && !hidden.contains(id) {
            all.push(m.clone());
        }
    }
    const KNOWN: [&str; 3] = ["gemma4:26b", "hf.co/noctrex/gemma-4-26B-A4B-it-MXFP4_MOE-GGUF:gemma-4-26B-A4B-it-MXFP4_MOE.gguf",
        "hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:gemma-4-26B-A4B-it-UD-IQ4_NL.gguf"];
    if listed.iter().any(|m| !m.0.is_ascii()) {
        return None; // str.lower
    }
    let loaded_lower: Vec<String> = listed.iter().map(|m| m.0.to_ascii_lowercase()).collect();
    let is_loaded = |id: &str| {
        let ml = id.to_ascii_lowercase();
        loaded_lower.iter().any(|ll| ml == *ll || ll.contains(&ml) || ml.contains(ll.as_str()))
    };
    let mut result = vec![];
    for m in &all {
        let o = m.as_object()?;
        let id = o.get("id")?.as_str()?;
        if !id.is_ascii() {
            return None;
        }
        let mut entry = o.clone();
        entry.insert("loaded".into(), json!(is_loaded(id)));
        entry.insert("builtin".into(), json!(KNOWN.contains(&id)));
        result.push((id.to_ascii_lowercase(), Value::Object(entry)));
    }
    let listed_lower: Vec<String> = result.iter().map(|r| r.0.clone()).collect();
    for (id, _) in &listed {
        let ll = id.to_ascii_lowercase();
        if !listed_lower.iter().any(|r| ll == *r || r.contains(&ll) || ll.contains(r.as_str())) {
            result.push((ll, json!({"id": id, "label": id, "loaded": true, "builtin": false})));
        }
    }
    // the model hub's cached chat GGUFs — the FINE-TUNED exports among them, which /v1/models does not list until
    // loaded, so a model fine-tuned in Studio was not choosable before (hub_cached = /api/hub/cached-gguf)
    if let Ok(Some(cached)) = hub_cached(&b).await {
        for entry in cached {
            if !entry.get("capabilities").and_then(|c| c.get("can_chat")).map(|v| py_truthy(Some(v))).unwrap_or(false) {
                continue;
            }
            let Some(id) = entry.get("repo_id").and_then(|r| r.as_str()).map(str::trim).filter(|s| !s.is_empty()) else { continue };
            if !id.is_ascii() {
                return None; // str.lower
            }
            let ll = id.to_ascii_lowercase();
            if result.iter().any(|r| ll == r.0 || r.0.contains(&ll) || ll.contains(r.0.as_str())) {
                continue;
            }
            let loaded = is_loaded(id);
            result.push((ll, json!({"id": id, "label": format!("{id} (Studio hub)"), "loaded": loaded, "builtin": false, "fine_tuned": true})));
        }
    }
    let models: Vec<Value> = result.into_iter().map(|r| r.1).collect();
    ok(&json!({
        "models": models, "default": b.model,
        "available": listed.iter().map(|m| json!(m.0)).collect::<Vec<_>>(),
        "defaults": crate::apis::model_defaults(&cfg)?,
    }))
}

async fn passthrough(b: &Backend, path: &str, query: &[(&str, &str)]) -> Option<Response<Body>> {
    match get(b, path, query).await {
        Ok(v) => ok(&v),
        Err(f) => failure_response(f),
    }
}

/// `hub_cached()`: the cached rows (`data.get("cached") or []`); None when the answer is not a shape Python reads
async fn hub_cached(b: &Backend) -> Result<Option<Vec<Value>>, Failure> {
    let data = get(b, "/api/hub/cached-gguf", &[]).await?;
    let Some(o) = data.as_object() else { return Ok(None) };
    match o.get("cached") {
        None | Some(Value::Null) => Ok(Some(vec![])),
        Some(Value::Array(a)) => Ok(Some(a.clone())),
        Some(other) if !py_truthy(Some(other)) => Ok(Some(vec![])),
        Some(_) => Ok(None),
    }
}

/// `imagegen_models()` for Studio: the cached image checkpoints Studio can run (a task that mentions "image" and does not end in "unsupported"); none cached
/// (or Studio did not answer) = the configured default, so the picker is never left empty
async fn image_models(b: &Backend, default: &str) -> Option<Response<Body>> {
    let mut out: Vec<Value> = vec![];
    if let Ok(Some(cached)) = hub_cached(b).await {
        for m in &cached {
            let Some(m) = m.as_object() else { continue };
            let task = m.get("task").and_then(|t| t.as_str()).unwrap_or("").to_lowercase();
            if let Some(Value::String(repo)) = m.get("repo_id") {
                if !repo.is_empty() && task.contains("image") && !task.ends_with("unsupported") {
                    out.push(json!(repo));
                }
            }
        }
    }
    if out.is_empty() {
        out.push(json!(default));
    }
    ok(&json!({"models": out}))
}

/// `GET /api/ai/unsloth/models`: every cached hub model with its task, size and whether it is loaded
async fn models(b: &Backend) -> Option<Response<Body>> {
    let cached = match hub_cached(b).await {
        Ok(Some(c)) => c,
        Ok(None) => return None,
        Err(f) => return failure_response(f),
    };
    let loaded: Vec<String> = crate::tuning::studio_models(b).await;
    let mut out = vec![];
    for c in &cached {
        let c = c.as_object()?;
        let caps = match c.get("capabilities") {
            None | Some(Value::Null) => Map::new(),
            Some(Value::Object(o)) => o.clone(),
            Some(v) if !py_truthy(Some(v)) => Map::new(),
            Some(_) => return None,
        };
        let is_loaded = |k: &str| match c.get(k) {
            Some(Value::String(s)) => loaded.contains(s),
            Some(Value::Array(_)) | Some(Value::Object(_)) => false,
            _ => false,
        };
        if matches!(c.get("load_id"), Some(Value::Array(_) | Value::Object(_))) || matches!(c.get("repo_id"), Some(Value::Array(_) | Value::Object(_))) {
            return None; // unhashable in Python's set lookup
        }
        out.push(json!({
            "repo_id": c.get("repo_id").cloned().unwrap_or(json!("")),
            "task": c.get("task").cloned().unwrap_or(json!("")),
            "size_bytes": c.get("size_bytes").cloned().unwrap_or(Value::Null),
            "requires_variant": py_truthy(caps.get("requires_variant")),
            "can_download": caps.get("can_download").map(|v| py_truthy(Some(v))).unwrap_or(true),
            "loaded": is_loaded("load_id") || is_loaded("repo_id"),
        }));
    }
    ok(&json!({"models": out}))
}

/// `_classify_audio_model`: "tts" | "stt" | ""
fn classify_audio(row: &Map<String, Value>) -> &'static str {
    let text = |k: &str| match row.get(k) {
        None | Some(Value::Null) => String::new(),
        Some(v) if !py_truthy(Some(v)) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => crate::ai::py_repr(other),
    };
    let hay = format!("{} {}", text("task"), text("repo_id")).to_lowercase();
    if ["speech-recognition", "speech-to-text", "automatic-speech", "transcri", "whisper", "asr"].iter().any(|h| hay.contains(h)) {
        return "stt";
    }
    if ["text-to-speech", "tts", "voice"].iter().any(|h| hay.contains(h)) {
        return "tts";
    }
    ""
}

async fn audio_models(cfg: &Config, b: &Backend) -> Option<Response<Body>> {
    let cached = match hub_cached(b).await {
        Ok(Some(c)) => c,
        Ok(None) => return None,
        Err(f) => return failure_response(f),
    };
    let (mut tts, mut stt, mut seen): (Vec<Value>, Vec<Value>, Vec<Value>) = (vec![], vec![], vec![]);
    for row in &cached {
        let row = row.as_object()?;
        let task = match row.get("task") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(v) if !py_truthy(Some(v)) => String::new(),
            Some(other) => crate::ai::py_repr(other),
        };
        if !task.is_empty() && !seen.iter().any(|t| t.as_str() == Some(&task)) {
            seen.push(json!(task));
        }
        let kind = classify_audio(row);
        if kind.is_empty() {
            continue;
        }
        let entry = json!({
            "repo_id": row.get("repo_id").cloned().unwrap_or(json!("")),
            "task": task,
            "size_bytes": row.get("size_bytes").cloned().unwrap_or(Value::Null),
            "capabilities": if py_truthy(row.get("capabilities")) { row.get("capabilities").cloned().unwrap_or(json!({})) } else { json!({}) },
        });
        if kind == "stt" { stt.push(entry) } else { tts.push(entry) }
    }
    let (tts_model, tts_voice, _, _) = crate::tts::prefs(cfg);
    ok(&json!({
        "tts": tts, "stt": stt, "tasks_seen": seen,
        "stt_standards": ["tiny", "base", "small", "medium", "turbo", "large-v3-turbo", "large-v3", "large"],
        "defaults": {"tts_model": tts_model, "tts_voice": tts_voice, "stt_model": crate::stt::stt_model(cfg)},
    }))
}

/// `studio_version()` through the route: the first version-ish field of the first build-specific answer; a failure other than "no such
/// endpoint" is an uncaught exception in Python (its 500), so those are left to it
async fn version(b: &Backend) -> Option<Response<Body>> {
    let mut found = String::new();
    for path in ["/api/version", "/version", "/api/about"] {
        match get(b, path, &[]).await {
            Err(f) if f.missing => continue,
            Err(_) => return None,
            Ok(data) => {
                if let Some(o) = data.as_object() {
                    for k in ["version", "build", "app_version", "studio_version"] {
                        if py_truthy(o.get(k)) {
                            found = match &o[k] {
                                Value::String(s) => s.clone(),
                                other => crate::ai::py_repr(other),
                            };
                            break;
                        }
                    }
                }
                break;
            }
        }
    }
    ok(&json!({"available": true, "version": found}))
}

const PROBE_PATHS: [&str; 9] = ["/api/version", "/version", "/api/about", "/api/status", "/api/health", "/health", "/api/hub/cached-gguf", "/v1/models", "/api/settings/voice"];

/// `studio_probe()`: read-only GETs over the candidate paths, 404s being data
async fn probe(b: &Backend) -> Option<Response<Body>> {
    let one = |path: &'static str| async move {
        let uri: hyper::Uri = format!("{}{}", base_url(b), path).parse().ok()?;
        let res = tokio::time::timeout(std::time::Duration::from_secs(6), crate::studio_key::studio_get(uri, &b.key)).await;
        Some(match res {
            Err(_) | Ok(Err(_)) => {
                let class = match res {
                    Err(_) => "ReadTimeout",
                    Ok(Err(e)) => if e.starts_with("ConnectError") { "ConnectError" } else if e.starts_with("ReadTimeout") { "ReadTimeout" } else { "HTTPError" },
                    _ => "HTTPError",
                };
                json!({"path": path, "status": null, "note": format!("unreachable: {class}")})
            }
            Ok(Ok((status, body))) => {
                let text = String::from_utf8_lossy(&body).to_string();
                let snippet: String = text.chars().take(120).collect::<String>().replace('\n', " ");
                let snippet = nd_render::text::py_strip(&snippet).to_string();
                let mut out = json!({"path": path, "status": status, "snippet": snippet});
                if status == 200 {
                    if let Ok(Value::Object(o)) = serde_json::from_slice::<Value>(&body) {
                        for k in ["version", "build", "app_version", "studio_version"] {
                            if py_truthy(o.get(k)) {
                                out["version"] = json!(match &o[k] { Value::String(s) => s.clone(), other => crate::ai::py_repr(other) });
                                break;
                            }
                        }
                    }
                }
                out
            }
        })
    };
    let results = futures_util::future::join_all(PROBE_PATHS.iter().map(|p| one(p))).await;
    let raw: Vec<Value> = results.into_iter().collect::<Option<_>>()?;
    let version = raw.iter().find_map(|r| r.get("version").cloned()).unwrap_or(Value::Null);
    ok(&json!({"results": raw, "version": version}))
}

/// `_ctx_of`: the first positive context length among an entry's known keys
fn ctx_of(entry: &Value) -> Option<i64> {
    let o = entry.as_object()?;
    for k in ["max_seq_length", "context_length", "n_ctx", "ctx_size"] {
        let n = match o.get(k) {
            Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f.trunc() as i64)),
            Some(Value::String(s)) => crate::form::py_int(nd_render::text::py_strip(s)),
            Some(Value::Bool(b)) => Some(*b as i64),
            _ => None,
        };
        if let Some(n) = n.filter(|n| *n > 0) {
            return Some(n);
        }
    }
    None
}

/// `context_length_for(data, model_id)`: the context length Studio will load the model with, from whatever shape the overrides answer has
fn context_length_for(data: &Value, model_id: &str) -> Option<i64> {
    let mid = model_id.trim().to_lowercase();
    if mid.is_empty() {
        return None;
    }
    let same = |v: Option<&Value>| v.and_then(|x| x.as_str()).is_some_and(|s| s.trim().to_lowercase() == mid);
    let node = match data.as_object().and_then(|o| o.get("overrides")) {
        Some(n @ (Value::Object(_) | Value::Array(_))) => n,
        _ => data,
    };
    match node {
        Value::Object(o) => o.iter().find(|(k, _)| k.trim().to_lowercase() == mid).and_then(|(_, v)| ctx_of(v)),
        Value::Array(a) => a
            .iter()
            .find(|e| e.as_object().is_some_and(|o| ["model_id", "model", "repo_id"].iter().any(|k| same(o.get(*k)))))
            .and_then(ctx_of),
        _ => None,
    }
}

/// `GET /api/ai/unsloth/status`: the status card - loaded models, auto-switch, auth health, and a context-size mismatch warning
async fn status(b: &Backend) -> Option<Response<Body>> {
    let mut out = Map::new();
    out.insert("reachable".into(), json!(true));
    out.insert("auth".into(), auth_status(Some(&base_url(b))));
    let mut silent = 0;
    match get(b, "/api/inference/loaded-models", &[]).await {
        Ok(data) => {
            let v = match data.as_object().and_then(|o| o.get("models")) {
                Some(m @ Value::Array(_)) => m.clone(),
                _ => data,
            };
            out.insert("loaded_models".into(), v);
        }
        Err(f) if f.status == 0 => return None,
        Err(f) => {
            out.insert("loaded_models_error".into(), json!(format!("{}: {}", f.status, f.msg)));
            silent += f.unreachable as i32;
        }
    }
    match get(b, "/api/settings/openai-auto-switch", &[]).await {
        Ok(v) => {
            out.insert("auto_switch".into(), v);
        }
        Err(f) if f.status == 0 => return None,
        Err(f) => {
            out.insert("auto_switch_error".into(), json!(format!("{}: {}", f.status, f.msg)));
            silent += f.unreachable as i32;
        }
    }
    if silent >= 2 {
        out.insert("reachable".into(), json!(false));
        return ok(&Value::Object(out));
    }
    if let Ok(data) = get(b, "/api/settings/openai-auto-switch/overrides", &[]).await {
        if let Some(studio) = context_length_for(&data, &b.model) {
            if studio != b.context_tokens {
                out.insert("context_mismatch".into(), json!({"studio": studio, "nd_world": b.context_tokens, "model": b.model}));
            }
        }
    }
    ok(&Value::Object(out))
}


// ── the "does this speech model work" checks (app/unsloth_extras.py run_check / stt_health / tts_health) ──
// A cold Studio can spend minutes loading the speech model, longer than one request may wait: the answer is {pending, token} after ~20 s and the page polls
// GET /api/ai/unsloth/check/<token> (which waits up to ~25 s) until the check is done. Only in a Python-free image; with Python it keeps its own.

type CheckSlot = Arc<std::sync::Mutex<Option<Value>>>;

struct Check {
    key: String,
    started: std::time::Instant,
    result: CheckSlot,
}

fn checks() -> &'static std::sync::Mutex<std::collections::HashMap<String, Check>> {
    static C: std::sync::OnceLock<std::sync::Mutex<std::collections::HashMap<String, Check>>> = std::sync::OnceLock::new();
    C.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()))
}

/// a second of a soft 220 Hz tone, 16 kHz mono 16-bit WAV: the HTTP status is the signal, never the transcript
fn tone_wav() -> Vec<u8> {
    let n = 16000usize;
    let mut out = Vec::with_capacity(44 + n * 2);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&((36 + n * 2) as u32).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&16000u32.to_le_bytes());
    out.extend_from_slice(&32000u32.to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&((n * 2) as u32).to_le_bytes());
    for i in 0..n {
        let v = (3000.0 * (2.0 * std::f64::consts::PI * 220.0 * i as f64 / 16000.0).sin()) as i16;
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

async fn run_one_check(cfg: Arc<Config>, b: Backend, tts: bool, model: String, voice: String, instructions: String, language: String) -> Value {
    let budget = std::env::var("UNSLOTH_SLOW_CHECK_TIMEOUT_SECONDS").ok().and_then(|v| v.trim().parse::<f64>().ok()).filter(|v| *v > 0.0).unwrap_or(600.0);
    let work = async {
        if tts {
            let (d_model, _, d_instr, d_lang) = crate::tts::prefs(&cfg);
            let model = if model.is_empty() { d_model } else { model };
            let instructions = if instructions.is_empty() { d_instr } else { instructions };
            let language = if language.is_empty() { d_lang } else { language };
            match crate::tts::speech(&b, "Ready.", &model, &voice, &instructions, &language).await {
                Ok((audio, ctype)) => json!({"ok": true, "message": format!("Studio synthesized {} bytes \u{2014} model is ready. Studio's audio: {}.", audio.len(), ctype)}),
                Err((status, msg)) => json!({"ok": false, "status": status, "message": msg}),
            }
        } else {
            let model = if model.is_empty() { "small".to_string() } else { model };
            match crate::stt::stt_call_pub(&b, tone_wav(), "nd-health-check.wav", &model).await {
                Ok(_) => json!({"ok": true, "message": "Studio accepted the audio \u{2014} model is ready.", "format": "wav"}),
                Err((status, mut msg, _)) => {
                    if status == 409 && msg.to_lowercase().contains("not downloaded") {
                        msg = format!("{msg} Download it in Studio first (Settings \u{2192} Voice), then test again.");
                    }
                    json!({"ok": false, "status": status, "message": msg})
                }
            }
        }
    };
    match tokio::time::timeout(Duration::from_secs_f64(budget), work).await {
        Ok(v) => v,
        Err(_) => json!({"ok": false, "message": format!("Studio did not answer within {budget} s. The first request after Studio starts has to load the speech model, which can take minutes on a slow or CPU-only box - try again in a minute, or watch it load with `docker logs -f nd-world-unsloth`.")}),
    }
}

fn pending_answer(token: &str, started: std::time::Instant) -> Value {
    json!({"ok": Value::Null, "pending": true, "token": token, "elapsed": started.elapsed().as_secs(),
        "message": "Studio is still working - the first request after it starts is loading the speech model, which can take a few minutes. Waiting for it..."})
}

async fn wait_slot(slot: &CheckSlot, secs: u64) -> Option<Value> {
    let end = std::time::Instant::now() + Duration::from_secs(secs);
    loop {
        if let Some(v) = slot.lock().ok().and_then(|g| g.clone()) {
            return Some(v);
        }
        if std::time::Instant::now() >= end {
            return None;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

async fn health_check(cfg: &Arc<Config>, b: Backend, tts: bool, obj: &Map<String, Value>) -> Option<Response<Body>> {
    let model = text_field(obj, "model")?;
    let (voice, instructions, language) = if tts { (text_field(obj, "voice")?, text_field(obj, "instructions")?, text_field(obj, "language")?) } else { Default::default() };
    let key = format!("{}|{model}|{voice}|{instructions}|{language}", if tts { "tts" } else { "stt" });
    let (token, slot, started) = {
        let mut map = checks().lock().ok()?;
        map.retain(|_, c| c.started.elapsed() < Duration::from_secs(600) || c.result.lock().map_or(true, |g| g.is_none()));
        if let Some((t, c)) = map.iter().find(|(_, c)| c.key == key && c.result.lock().map_or(false, |g| g.is_none())) {
            (t.clone(), c.result.clone(), c.started)
        } else {
            let token = format!("{:024x}", rand::random::<u128>() >> 32);
            let slot: CheckSlot = Arc::new(std::sync::Mutex::new(None));
            let started = std::time::Instant::now();
            map.insert(token.clone(), Check { key, started, result: slot.clone() });
            let (cfg2, slot2) = (cfg.clone(), slot.clone());
            tokio::spawn(async move {
                let v = run_one_check(cfg2, b, tts, model, voice, instructions, language).await;
                if let Ok(mut g) = slot2.lock() {
                    *g = Some(v);
                }
            });
            (token, slot, started)
        }
    };
    let answer = wait_slot(&slot, 20).await.unwrap_or_else(|| pending_answer(&token, started));
    ok(&answer)
}

/// `GET /api/ai/unsloth/check/<token>` (a GM): the finished result, or pending again; 404 once it is gone
async fn poll_check(cfg: &Arc<Config>, uri: &Uri, headers: &HeaderMap, token: &str) -> Option<Response<Body>> {
    let (c, u, h) = (cfg.clone(), uri.clone(), headers.clone());
    let gm = tokio::task::spawn_blocking(move || -> Option<bool> {
        let db = crate::writes::open(&c)?;
        let n = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        Some(crate::layout::build(&db, &c, "GET", &u, &h, n)?.is_gm)
    })
    .await
    .ok()??;
    if !gm {
        return None;
    }
    let found = checks().lock().ok()?.get(token).map(|c| (c.result.clone(), c.started));
    let Some((slot, started)) = found else {
        return Some(crate::ai_more::detail(StatusCode::NOT_FOUND, "That check is gone (nd-world restarted, or it finished a while ago) - press the test button again."));
    };
    let answer = wait_slot(&slot, 25).await.unwrap_or_else(|| pending_answer(token, started));
    ok(&answer)
}
