//! The Unsloth Studio status routes behind `/api/ai/unsloth/*` (app/routers/ai.py "Unsloth Studio extras", app/unsloth_extras.py): the
//! read-only passthroughs the AI page and Settings call - the model hub list, image-model status, auto-switch and per-model overrides, the
//! consolidated status card, the key check, the version and the diagnostic probe. Each asks Studio exactly what Python asks and answers with
//! Studio's own JSON (or Python's wording for an error); the key-death banner state is kept here, in memory, as Python keeps it. Studio
//! answering over https, a body that is not the shape Python reads, or anything Python would fail on is left to Python (None).

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

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
    format!("http://{}{}", b.host, b.base_path)
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
    let path = uri.path().strip_prefix("/api/ai/unsloth/")?.to_string();
    if !matches!(
        path.as_str(),
        "models" | "image/status" | "image/load-progress" | "auto-switch" | "status" | "context-overrides" | "audio-models" | "version" | "probe"
            | "hub/download-progress" | "gguf-variants" | "key-check" | "auth-status"
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
    if !matches!(path.as_str(), "hub/download" | "image/load" | "image/unload" | "auto-switch" | "context-overrides") {
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
    if path == "image/unload" {
        return match send(&b, "POST", "/api/inference/images/unload", None).await {
            Ok(v) => ok(&v),
            Err(f) => failure_response(f),
        };
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
