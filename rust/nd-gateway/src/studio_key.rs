//! `POST /settings/system/studio-key` (app/main.py `settings_studio_key`): save or clear the Unsloth
//! Studio API key. A pasted key is verified with Studio BEFORE it replaces the saved one — a 401 is
//! not saved and the message says so; a Studio that can't be asked at all saves the key anyway. The
//! verify is one GET to `{llm_url}/api/hub/cached-gguf` with the Bearer header
//! (app/unsloth_extras.py `verify_key`, httpx, redirects followed, 30 s).

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode};
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::Request as HyperRequest;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use rusqlite::types::Value as Sql;
use serde_json::{json, Map, Value};

use crate::ai::py_repr;
use crate::config::Config;

/// app/ai.py: the effective key is the saved one, else UNSLOTH_API_KEY from the environment
fn effective_key(saved: &str) -> String {
    if saved.is_empty() {
        std::env::var("UNSLOTH_API_KEY").unwrap_or_default()
    } else {
        saved.to_string()
    }
}

/// app/ai.py `effective_llm_url`: the override (llm_url or ollama_url column, rstripped of trailing
/// slashes) wins; with no URL, a key means the Studio address (never send a Bearer to Ollama), no
/// key means the Ollama address.
pub(crate) fn effective_url(saved_url: &str, key: &str) -> String {
    if !saved_url.is_empty() {
        return saved_url.to_string();
    }
    if !key.is_empty() {
        let u = std::env::var("UNSLOTH_URL").unwrap_or_default();
        let u = u.trim_end_matches('/');
        return if u.is_empty() { "http://unsloth:8000".into() } else { u.to_string() };
    }
    std::env::var("OLLAMA_URL").unwrap_or_else(|_| "http://127.0.0.1:11434".into())
}

/// app/ai.py `key_hint`: '…ab78' — the last 4 characters, '…' for a too-short key
fn key_hint(key: &str) -> String {
    let k = key.trim();
    if k.is_empty() {
        return String::new();
    }
    let chars: Vec<char> = k.chars().collect();
    if chars.len() >= 12 {
        format!("…{}", chars[chars.len() - 4..].iter().collect::<String>())
    } else {
        "…".into()
    }
}

pub(crate) fn key_sources(saved: &str, env: &str) -> Value {
    let in_use = if !saved.is_empty() {
        "settings"
    } else if !env.is_empty() {
        "env"
    } else {
        "none"
    };
    json!({"in_use": in_use, "saved_hint": key_hint(saved), "env_hint": key_hint(env)})
}

/// app/main.py `_key_explanation`: one sentence on which key is in play
pub(crate) fn key_explanation(info: &Map<String, Value>) -> String {
    let s = |k: &str| info.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    match s("in_use").as_str() {
        "settings" => {
            let (saved_hint, env_hint) = (s("saved_hint"), s("env_hint"));
            if !env_hint.is_empty() {
                format!("Using the key saved in Settings ({saved_hint}). It overrides UNSLOTH_API_KEY from the environment ({env_hint}) - remove the saved key to use the environment's.")
            } else {
                format!("Using the key saved in Settings ({saved_hint}).")
            }
        }
        "env" => format!("Using UNSLOTH_API_KEY from the environment ({}); no key is saved in Settings.", s("env_hint")),
        _ => "No Studio API key is set.".into(),
    }
}

/// app/main.py `_clean_pasted_key`: strip whitespace, a "Bearer " prefix, matching quotes; empty or
/// still-whitespace keys are the 400s (FastAPI's {"detail": ...} body)
fn clean_pasted_key(raw: &str) -> Result<String, &'static str> {
    let mut key: Vec<char> = raw.trim().chars().collect();
    if key.iter().collect::<String>().to_lowercase().starts_with("bearer ") {
        let rest: String = key[7..].iter().collect();
        key = rest.trim().chars().collect();
    }
    if key.len() >= 2 {
        let first = key[0];
        let last = key[key.len() - 1];
        if first == last && (first == '"' || first == '\'' || first == '`') {
            let inner: String = key[1..key.len() - 1].iter().collect();
            key = inner.trim().chars().collect();
        }
    }
    let key: String = key.iter().collect();
    if key.is_empty() {
        return Err("Paste the API key first (Studio → Settings → API → create a key).");
    }
    if key.chars().any(|c| c.is_whitespace()) {
        return Err("A Studio API key has no spaces or line breaks - check that only the key was pasted.");
    }
    Ok(key)
}

/// Python's `str(v)` for a JSON value: a string as it is, anything else as its repr
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// Python's `v or ""` for a JSON value that should be a string
fn truthy_str(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        Some(other) if is_truthy(other) => py_repr(other),
        _ => String::new(),
    }
}

fn is_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// app/unsloth_extras.py `_error_message`: Studio's own error text from a failed response, whatever
/// shape it took — {"error": {"message"}} / {"error": "..."} / {"error": [...]}, FastAPI
/// {"detail": ...}, a JSON list/string, or a non-JSON body. Never fails.
pub(crate) fn error_message(body: &[u8], status: u16) -> String {
    let parsed: Value = serde_json::from_slice(body).unwrap_or(Value::Null);
    let mut msg = String::new();
    if let Some(obj) = parsed.as_object() {
        match obj.get("error") {
            Some(Value::Object(err)) => msg = truthy_str(err.get("message")),
            Some(Value::String(s)) => msg = s.clone(),
            Some(Value::Array(a)) if !a.is_empty() => msg = a.iter().map(py_str).collect::<Vec<_>>().join("; "),
            _ => {}
        }
        if msg.is_empty() {
            match obj.get("detail") {
                Some(Value::String(s)) => msg = s.clone(),
                Some(Value::Array(a)) if !a.is_empty() => msg = a
                    .iter()
                    .map(|d| match d {
                        // str(d.get("msg") if isinstance(d, dict) else d) — a dict without msg says "None"
                        Value::Object(o) => py_str(o.get("msg").unwrap_or(&Value::Null)),
                        other => py_str(other),
                    })
                    .collect::<Vec<_>>()
                    .join("; "),
                Some(Value::Object(d)) => {
                    let m = truthy_str(d.get("message"));
                    msg = if m.is_empty() { py_repr(&Value::Object(d.clone())) } else { m };
                }
                _ => {}
            }
        }
        if msg.is_empty() && is_truthy(&parsed) {
            msg = py_repr(&parsed);
        }
    } else if is_truthy(&parsed) {
        msg = py_repr(&parsed);
    }
    if msg.is_empty() {
        msg = String::from_utf8_lossy(body).trim().to_string();
    }
    if msg.is_empty() {
        format!("HTTP {status}")
    } else {
        msg.chars().take(300).collect()
    }
}

/// GET {url}/api/hub/cached-gguf with the Bearer header; redirects followed (httpx's default 20),
/// 30 s overall. Err: the unreachable message tail (Python composes `f"{type(exc).__name__}: {exc}"`;
/// these are best-effort — a real Studio either answers or doesn't).
pub(crate) async fn studio_get(uri: hyper::Uri, key: &str) -> Result<(u16, Bytes), String> {
    let client = Client::builder(TokioExecutor::new()).pool_idle_timeout(Duration::from_secs(30)).build(crate::net::connector(Some(Duration::from_secs(8))));
    let mut current = uri;
    for _ in 0..20 {
        let req = HyperRequest::builder()
            .method("GET")
            .uri(current.clone())
            .header("authorization", format!("Bearer {key}"))
            .body(Full::new(Bytes::new()))
            .map_err(|e| format!("RequestError: {e}"))?;
        let resp = tokio::time::timeout(Duration::from_secs(30), client.request(req))
            .await
            .map_err(|_| "ReadTimeout: timed out".to_string())?
            .map_err(|e| {
                if e.is_connect() {
                    format!("ConnectError: {e}")
                } else {
                    format!("HTTPError: {e}")
                }
            })?;
        let status = resp.status().as_u16();
        if matches!(status, 301 | 302 | 303 | 307 | 308) {
            if let Some(loc) = resp.headers().get("location").and_then(|v| v.to_str().ok()) {
                current = resolve_location(&current, loc)?;
                let _ = resp.into_body().collect().await;
                continue;
            }
        }
        let bytes = resp.into_body().collect().await.map_err(|e| format!("HTTPError: {e}"))?.to_bytes();
        return Ok((status, bytes));
    }
    Err("TooManyRedirects: exceeded 20 redirects".into())
}

pub(crate) fn resolve_location(base: &hyper::Uri, loc: &str) -> Result<hyper::Uri, String> {
    if loc.starts_with("http://") || loc.starts_with("https://") {
        return loc.parse::<hyper::Uri>().map_err(|e| format!("InvalidURL: {e}"));
    }
    let authority = base.authority().map(|a| a.as_str()).unwrap_or("");
    let scheme = base.scheme_str().unwrap_or("http");
    let joined = if let Some(rest) = loc.strip_prefix('/') {
        format!("{scheme}://{authority}/{rest}")
    } else {
        let path = base.path();
        let dir = match path.rfind('/') {
            Some(i) => &path[..=i],
            None => "/",
        };
        format!("{scheme}://{authority}{dir}{loc}")
    };
    joined.parse::<hyper::Uri>().map_err(|e| format!("InvalidURL: {e}"))
}

/// app/unsloth_extras.py `verify_key` (the candidate branch — the gateway never verifies the
/// effective key for a banner: that note is Python's in-process state, gone at the switch)
pub(crate) async fn verify_key(url: &str, key: &str) -> Value {
    if key.is_empty() {
        return json!({"ok": false, "status": 0, "url": url, "message": "No Studio API key is set."});
    }
    let probe = match format!("{url}/api/hub/cached-gguf").parse::<hyper::Uri>() {
        Ok(u) => u,
        Err(e) => {
            return json!({"ok": false, "status": 503, "url": url, "unreachable": true,
                          "message": format!("Unsloth Studio unreachable at {url}: InvalidURL: {e}")})
        }
    };
    match studio_get(probe, key).await {
        Err(detail) => json!({"ok": false, "status": 503, "url": url, "unreachable": true,
                              "message": format!("Unsloth Studio unreachable at {url}: {detail}")}),
        Ok((status, body)) => {
            if status == 401 {
                json!({"ok": false, "status": 401, "url": url, "message": error_message(&body, 401)})
            } else if status >= 400 {
                json!({"ok": false, "status": status, "url": url, "message": error_message(&body, status)})
            } else {
                json!({"ok": true, "status": 200, "url": url, "message": "Studio accepted the key."})
            }
        }
    }
}

/// (saved key, saved URL override — llm_url or ollama_url, rstripped) from the one row
fn settings_row(db: &rusqlite::Connection) -> Option<(String, String)> {
    let (key, url, ollama_url): (Option<String>, Option<String>, Option<String>) = db
        .query_row("SELECT llm_api_key, llm_url, ollama_url FROM app_settings ORDER BY id LIMIT 1", [], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })
        .ok()?;
    let u = match url.as_deref().filter(|s| !s.is_empty()) {
        Some(u) => u,
        None => ollama_url.as_deref().unwrap_or(""),
    };
    Some((key.unwrap_or_default(), u.trim_end_matches('/').to_string()))
}

fn set_saved_key(db: &rusqlite::Connection, key: &str) -> Option<()> {
    let id: i64 = db.query_row("SELECT id FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get(0)).ok()?;
    crate::orm::update(db, "app_settings", id, &[("llm_api_key", Sql::Text(key.to_string()))]).map(|_| ())
}

fn json_answer(status: StatusCode, v: &Value) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .header("x-nd-served-by", "gateway")
        .body(Body::from(nd_render::py_dumps_compact(v)))
        .unwrap()
}

fn msg_of(v: &Value) -> String {
    v.get("message").and_then(|m| m.as_str()).unwrap_or("").to_string()
}

/// The route: clear saves "", a pasted key is verified before it replaces the saved one
pub async fn studio_key_srv(cfg: Arc<Config>, body: Bytes) -> Option<Response<Body>> {
    let parsed: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let obj = parsed.as_object().cloned().unwrap_or_default();
    let clear = obj.get("clear").map(|v| crate::edits::py_truthy_pub(Some(v))).unwrap_or(false);
    let raw_key = obj.get("api_key").and_then(|v| v.as_str()).unwrap_or("").to_string();

    let (saved_key, saved_url) = {
        let cfg = cfg.clone();
        tokio::task::spawn_blocking(move || {
            let db = crate::writes::open(&cfg)?;
            settings_row(&db)
        })
        .await
        .ok()
        .flatten()?
    };
    let env_key = std::env::var("UNSLOTH_API_KEY").unwrap_or_default();

    let mut saved = false;
    let mut saved_after = saved_key.clone();
    let res: Value;
    if clear {
        {
            let cfg = cfg.clone();
            tokio::task::spawn_blocking(move || {
                let db = crate::writes::open(&cfg)?;
                set_saved_key(&db, "")
            })
            .await
            .ok()
            .flatten()?
        }
        saved_after = String::new();
        let eff = effective_key("");
        if !eff.is_empty() {
            let url = effective_url(&saved_url, &eff);
            let mut v = verify_key(&url, &eff).await;
            v["message"] = json!(format!("The saved key was removed; the environment's key is in use now: {}", msg_of(&v)));
            res = v;
        } else {
            res = json!({"ok": false, "message": "The saved key was removed."});
        }
    } else {
        let candidate = match clean_pasted_key(&raw_key) {
            Ok(k) => k,
            Err(msg) => return Some(json_answer(StatusCode::BAD_REQUEST, &json!({"detail": msg}))),
        };
        // the URL comes from the settings BEFORE the save: the current effective key decides
        // whether a blank URL means the Studio address or the Ollama one
        let url = effective_url(&saved_url, &effective_key(&saved_key));
        let v = verify_key(&url, &candidate).await;
        if v.get("status").and_then(|s| s.as_i64()) == Some(401) {
            let mut v = v;
            let kept = if key_hint(&saved_key).is_empty() { "nothing was saved" } else { "the saved key is unchanged" };
            v["message"] = json!(format!(
                "{} - the key you pasted was not saved ({}). Check it was copied whole from the Studio at {} (Settings → API), and that this nd-world's Studio URL is that same Studio.",
                msg_of(&v),
                kept,
                url
            ));
            res = v;
        } else {
            {
                let cfg = cfg.clone();
                let c = candidate.clone();
                tokio::task::spawn_blocking(move || {
                    let db = crate::writes::open(&cfg)?;
                    set_saved_key(&db, &c)
                })
                .await
                .ok()
                .flatten()?
            }
            saved = true;
            let mut v = v;
            if !v.get("ok").and_then(|o| o.as_bool()).unwrap_or(false) {
                v["message"] = json!(format!("Saved, but couldn't confirm it: {}", msg_of(&v)));
            }
            res = v;
            saved_after = candidate;
        }
    }

    let mut out = res.as_object().cloned().unwrap_or_default();
    out.insert("saved".into(), json!(saved));
    let sources = key_sources(&saved_after, &env_key);
    if let Some(s) = sources.as_object() {
        for (k, v) in s {
            out.insert(k.clone(), v.clone());
        }
    }
    let explain = key_explanation(&out);
    out.insert("explain".into(), json!(explain));
    Some(json_answer(StatusCode::OK, &Value::Object(out)))
}
