//! The Chronicler (`POST /api/chronicler/ask`), `POST /api/ai/stream` and NPC talk answered by the gateway: the same system prompt (facts, the
//! entities the question finds with their excerpts and notes, the GM's AI instructions), the same request to an Unsloth Studio
//! backend (OpenAI dialect, app/llm_client.py) and the same SSE stream back, with Python's error texts, keep-alives and `<think>` split.
//! Answered only on the Unsloth backend over plain http (the legacy Ollama backend stays Python's). The Chronicler's 30 s answer cache
//! and a player's 3 s cooldown are kept here, as Python keeps them in its process. docs/RUST_AI.md lists what each route leaves to Python.

use axum::body::Body;
use axum::http::{header, HeaderMap, Response, StatusCode, Uri};
use http_body_util::BodyExt;
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::config::Config;

const CHRONICLER_SYSTEM: &str = "You are the Chronicler, keeper of this campaign's history. Answer the question using ONLY the facts and entities listed below — never invent events, names, or details that aren't given to you. If the answer isn't in the provided context, say you don't know rather than guessing or making something up. Speak in a warm, slightly formal narrator's voice. Keep answers concise.";
const CACHE_TTL: Duration = Duration::from_secs(30);
const COOLDOWN: Duration = Duration::from_secs(3);
const HEARTBEAT: Duration = Duration::from_secs(12);

/// (world, user, question lowered, model, think) - the Chronicler's answer cache key
type CacheKey = (i64, i64, String, String, bool);

fn cache() -> &'static Mutex<HashMap<CacheKey, (Instant, String)>> {
    static C: OnceLock<Mutex<HashMap<CacheKey, (Instant, String)>>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(HashMap::new()))
}

/// `_model_thinking_failures` / `_prompt_token_thinking_models`: models that refused a Thinking request in this process (and of those, the
/// ones retried with the `<|think|>` token), cleared by their next accepted Thinking request
fn thinking_state() -> &'static Mutex<(std::collections::HashSet<String>, std::collections::HashSet<String>)> {
    static S: OnceLock<Mutex<(std::collections::HashSet<String>, std::collections::HashSet<String>)>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(Default::default()))
}

fn record_thinking(model: &str, think: bool, failed: bool, via_token: bool) {
    if !think {
        return;
    }
    if let Ok(mut s) = thinking_state().lock() {
        if failed {
            s.0.insert(model.to_string());
            if via_token {
                s.1.insert(model.to_string());
            }
        } else {
            s.0.remove(model);
            s.1.remove(model);
        }
    }
}

/// (`model_rejected_thinking`, `model_thinks_via_prompt_token`)
fn thinking_rejected(model: &str) -> (bool, bool) {
    thinking_state().lock().map(|s| (s.0.contains(model), s.1.contains(model))).unwrap_or((false, false))
}

fn cooldowns() -> &'static Mutex<HashMap<i64, Instant>> {
    static C: OnceLock<Mutex<HashMap<i64, Instant>>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(HashMap::new()))
}

/// The LLM backend as app.ai resolves it (Settings row first, then the environment); None when it is not Unsloth over plain http
#[derive(Clone)]
pub struct Backend {
    pub host: String,
    pub base_path: String,
    pub key: String,
    pub model: String,
    /// Settings > System's generation options (instance-wide)
    pub options: Map<String, Value>,
    /// Settings' per-model overrides ({model: {"options": {...}}})
    pub per_model: Map<String, Value>,
    /// `llm_context_tokens()`: Settings' context window, else LLM_CONTEXT_TOKENS (16,384)
    pub context_tokens: i64,
}

impl Backend {
    /// `effective_ollama_options(model)`: the instance-wide options with the model's own layered on top
    fn options_for(&self, model: &str) -> Map<String, Value> {
        let mut o = self.options.clone();
        if let Some(Value::Object(m)) = self.per_model.get(model).and_then(|m| m.get("options")) {
            for (k, v) in m {
                o.insert(k.clone(), v.clone());
            }
        }
        o
    }

    /// `_thinking_num_predict_override(think)`
    fn thinking_predict_if(&self, think: bool) -> Map<String, Value> {
        if think { self.thinking_predict() } else { Map::new() }
    }

    /// `_thinking_num_predict_override(True)`: the configured output cap plus the thinking headroom (nothing when unset or unbounded)
    fn thinking_predict(&self) -> Map<String, Value> {
        let mut out = Map::new();
        if let Some(n) = self.options.get("num_predict").and_then(|v| v.as_i64()).filter(|n| *n > 0) {
            let headroom = std::env::var("THINKING_HEADROOM_TOKENS").ok().and_then(|v| v.parse::<i64>().ok()).unwrap_or(4096).max(0);
            out.insert("num_predict".into(), json!(n + headroom));
        }
        out
    }
}

fn env(k: &str) -> String {
    std::env::var(k).unwrap_or_default()
}

pub fn backend(db: &rusqlite::Connection) -> Option<Backend> {
    let st = db.query_row("SELECT * FROM app_settings ORDER BY id LIMIT 1", [], |r| crate::layout::row_map(r)).ok().unwrap_or_default();
    let s = |k: &str| st.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let key = Some(s("llm_api_key")).filter(|k| !k.is_empty()).unwrap_or_else(|| env("UNSLOTH_API_KEY"));
    if key.is_empty() {
        return None; // the legacy Ollama backend: Python's
    }
    let url_override = Some(s("llm_url")).filter(|u| !u.is_empty()).or_else(|| Some(s("ollama_url")).filter(|u| !u.is_empty()));
    let url = url_override.map(|u| u.trim_end_matches('/').to_string()).unwrap_or_else(|| {
        let e = env("UNSLOTH_URL");
        if e.is_empty() { "http://unsloth:8000".into() } else { e.trim_end_matches('/').to_string() }
    });
    let rest = url.strip_prefix("http://")?;
    let (host, base_path) = match rest.split_once('/') {
        Some((h, p)) => (h.to_string(), format!("/{p}")),
        None => (rest.to_string(), String::new()),
    };
    let model = Some(s("llm_model")).filter(|m| !m.is_empty()).or_else(|| Some(s("ollama_model")).filter(|m| !m.is_empty()))
        .or_else(|| Some(env("UNSLOTH_MODEL")).filter(|m| !m.is_empty()))
        .unwrap_or_else(|| { let m = env("OLLAMA_MODEL"); if m.is_empty() { "gemma4:26b".into() } else { m } });
    // the instance-wide generation options (_refresh_settings_overrides), then the model's own override
    let mut options = Map::new();
    for (col, key) in [("ollama_temperature", "temperature"), ("ollama_top_p", "top_p"), ("ollama_top_k", "top_k"), ("ollama_repeat_penalty", "repeat_penalty"),
        ("ollama_num_predict", "num_predict"), ("ollama_num_ctx", "num_ctx"), ("ollama_seed", "seed"), ("ollama_num_gpu", "num_gpu"), ("ollama_min_p", "min_p"),
        ("ollama_typical_p", "typical_p"), ("ollama_repeat_last_n", "repeat_last_n"), ("ollama_presence_penalty", "presence_penalty"),
        ("ollama_frequency_penalty", "frequency_penalty"), ("ollama_num_keep", "num_keep"), ("ollama_num_batch", "num_batch"), ("ollama_num_thread", "num_thread"),
        ("ollama_main_gpu", "main_gpu")] {
        if let Some(v) = st.get(col).filter(|v| !v.is_null()) {
            options.insert(key.into(), v.clone());
        }
    }
    let raw = s("ollama_model_overrides_json");
    let per_model = match serde_json::from_str::<Value>(if raw.is_empty() { "{}" } else { &raw }) {
        Ok(Value::Object(per)) => per,
        _ => Map::new(),
    };
    if st.get("ollama_use_mmap").and_then(|v| v.as_str()).is_some_and(|m| m == "0" || m == "1") {
        options.insert("use_mmap".into(), json!(s("ollama_use_mmap") == "1"));
    }
    let context_tokens = st.get("llm_context_tokens").and_then(|v| v.as_i64()).filter(|n| *n > 0)
        .unwrap_or_else(|| env("LLM_CONTEXT_TOKENS").parse::<i64>().unwrap_or(16384).max(1024));
    Some(Backend { host, base_path, key, model, options, per_model, context_tokens })
}

/// `_translate_options`: ollama option names to the OpenAI body's (num_ctx dropped: fixed at model load; others unknown there dropped)
fn translate_options(o: &Map<String, Value>) -> Map<String, Value> {
    let mut out = Map::new();
    for (k, v) in o {
        let name = match k.as_str() {
            "num_predict" => "max_tokens",
            "repeat_penalty" => "repetition_penalty",
            "temperature" | "top_p" | "top_k" | "seed" | "min_p" => k.as_str(),
            _ => continue,
        };
        out.insert(name.into(), v.clone());
    }
    out
}

fn sse(v: &Value) -> String {
    format!("data: {}\n\n", nd_render::py_dumps(v))
}

fn stream_response(rx: tokio::sync::mpsc::Receiver<String>) -> Response<Body> {
    let stream = tokio_stream_from(rx);
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "text/event-stream; charset=utf-8")
        .header("x-accel-buffering", "no")
        .header(header::CACHE_CONTROL, "no-cache")
        .header("x-nd-served-by", "gateway")
        .body(Body::from_stream(stream))
        .unwrap()
}

fn tokio_stream_from(mut rx: tokio::sync::mpsc::Receiver<String>) -> impl futures_util::Stream<Item = Result<bytes::Bytes, std::io::Error>> {
    futures_util::stream::poll_fn(move |cx| rx.poll_recv(cx).map(|o| o.map(|s| Ok(bytes::Bytes::from(s)))))
}

/// What the blocking part decides: answer from the cache, stream a fresh answer, refuse (a player's cooldown), or leave it to Python
enum Plan {
    Cached(String),
    Fresh(Job),
    Refuse(Response<Body>),
}

/// One streamed answer to produce
struct Job {
    backend: Backend,
    requested: String,
    system: String,
    messages: Vec<Value>,
    think: bool,
    extra: Map<String, Value>,
    cache: Option<CacheKey>,
    /// run (blocking) with the answer when it finished cleanly with something in it, before [DONE]
    on_done: Option<Box<dyn FnOnce(String) + Send>>,
    /// read to the end even when the reader goes away (the NPC stream); other streams stop with their reader, as Python's do
    detached: bool,
}

fn plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Plan> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    if !p.is_gm {
        let level = nd_perm::sections::section_level(false, p.role.as_deref(), w.get("section_access_json").and_then(|v| v.as_str()), w.get("custom_kinds_json").and_then(|v| v.as_str()), "chronicler")?;
        if level == "none" {
            return None;
        }
    }
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let question = match b.get("question") {
        None => String::new(),
        Some(Value::String(q)) => q.trim_matches(|c: char| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)).to_string(),
        Some(_) => return None, // str() of another value: Python's
    };
    if question.is_empty() || question.contains('\u{130}') {
        return None;
    }
    // a GM's own model and Thinking choice (a player's are ignored server-side)
    let (model, think) = if p.is_gm {
        let model = match b.get("model") {
            v if !crate::edits::py_truthy_pub(v) => String::new(),
            Some(Value::String(m)) => m.trim_matches(|c: char| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)).to_string(),
            Some(_) => return None,
            None => String::new(),
        };
        (model, crate::edits::py_truthy_pub(b.get("think")))
    } else {
        (String::new(), false)
    };
    let backend = backend(&db)?;
    let key: CacheKey = (wid, p.user_id, question.to_lowercase(), model.clone(), think);
    if let Some((at, answer)) = cache().lock().ok()?.get(&key) {
        if at.elapsed() < CACHE_TTL {
            return Some(Plan::Cached(answer.clone()));
        }
    }
    if !p.is_gm {
        let mut cd = cooldowns().lock().ok()?;
        if cd.get(&p.user_id).is_some_and(|t| t.elapsed() < COOLDOWN) {
            let body = nd_render::py_dumps_compact(&json!({"detail": "Please wait a few seconds before asking again."}));
            return Some(Plan::Refuse(Response::builder().status(StatusCode::TOO_MANY_REQUESTS).header(header::CONTENT_TYPE, "application/json")
                .header("x-nd-served-by", "gateway").body(Body::from(body)).ok()?));
        }
        cd.insert(p.user_id, Instant::now());
    }
    // build_chronicler_system_prompt
    let viewer = (p.user_id, p.is_gm);
    let mut st = db.prepare(&format!("SELECT content FROM facts WHERE world_id = ?1{} ORDER BY created_at DESC LIMIT 60",
        if p.is_gm { "" } else { " AND visible_to_players IS NOT 0" })).ok()?;
    let facts: Vec<Option<String>> = st.query_map([wid], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
    let entities = crate::retrieval::find_relevant_entities(&db, wid, &question, 15, viewer)?;
    let mut system = CHRONICLER_SYSTEM.to_string();
    let extra = crate::retrieval::instructions_text(&db, wid, !p.is_gm)?;
    if !extra.is_empty() {
        system = format!("{system}\n\n{extra}");
    }
    let mut lines = vec![system, String::new(), "## Known facts".to_string()];
    if facts.is_empty() {
        lines.push("(none recorded yet)".into());
    } else {
        lines.extend(facts.into_iter().map(|f| format!("- {}", f.unwrap_or_else(|| "None".into()))));
    }
    if !entities.is_empty() {
        lines.push(String::new());
        lines.push("## Relevant entities".into());
        lines.push(crate::retrieval::format_context(&db, &entities, !p.is_gm, &question)?);
    }
    let extra = if think { backend.thinking_predict() } else { Map::new() };
    Some(Plan::Fresh(Job { backend, requested: model, system: lines.join("\n"), messages: vec![json!({"role": "user", "content": question})], think, extra, cache: Some(key), on_done: None, detached: false }))
}

type Client = hyper_util::client::legacy::Client<hyper_util::client::legacy::connect::HttpConnector, http_body_util::Full<bytes::Bytes>>;

pub(crate) fn client() -> &'static Client {
    static C: OnceLock<Client> = OnceLock::new();
    C.get_or_init(|| hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new()).build_http())
}

pub(crate) fn backend_request(b: &Backend, method: &str, path: &str, body: Option<&Value>) -> Option<hyper::Request<http_body_util::Full<bytes::Bytes>>> {
    let uri = format!("http://{}{}{}", b.host, b.base_path, path);
    let data = body.map(|v| serde_json::to_vec(v).unwrap_or_default()).unwrap_or_default();
    let mut req = hyper::Request::builder().method(method).uri(uri).header(header::AUTHORIZATION, format!("Bearer {}", b.key));
    if body.is_some() {
        req = req.header(header::CONTENT_TYPE, "application/json");
    }
    req.body(http_body_util::Full::new(bytes::Bytes::from(data))).ok()
}

/// `resolve_model`: the configured model, or the closest available one with a note (`/v1/models`, 10 s)
async fn resolve_model(b: &Backend, requested: &str) -> (String, Option<String>) {
    let target = if requested.is_empty() { b.model.clone() } else { requested.to_string() };
    let Some(req) = backend_request(b, "GET", "/v1/models", None) else { return (target, None) };
    let fetch = async {
        let resp = client().request(req).await.ok()?;
        if resp.status().as_u16() >= 400 {
            return Some(vec![]);
        }
        let bytes = resp.into_body().collect().await.ok()?.to_bytes();
        let v: Value = serde_json::from_slice(&bytes).ok()?;
        Some(v.get("data").and_then(|d| d.as_array()).map(|a| a.iter().filter_map(|m| m.get("id").and_then(|i| i.as_str()).filter(|s| !s.is_empty()).map(str::to_string)).collect()).unwrap_or_default())
    };
    let available: Vec<String> = match tokio::time::timeout(Duration::from_secs(10), fetch).await {
        Err(_) => return (target, None),
        Ok(v) => v.unwrap_or_default(),
    };
    if available.is_empty() || available.contains(&target) {
        return (target, None);
    }
    let tl = target.to_lowercase();
    for a in &available {
        let al = a.to_lowercase();
        if tl == al || al.contains(&tl) || tl.contains(&al) {
            return (a.clone(), Some(format!("Using {a} (closest match to requested “{target}”)")));
        }
    }
    (target, None)
}

/// `_InlineThinkSplitter`: content / thinking around a literal <think>...</think>, a tag split across chunks held back
struct Splitter {
    buf: String,
    in_think: bool,
}

impl Splitter {
    fn feed(&mut self, token: &str) -> Vec<(bool, String)> {
        self.buf.push_str(token);
        let mut out = vec![];
        while !self.buf.is_empty() {
            let tag = if self.in_think { "</think>" } else { "<think>" };
            match self.buf.find(tag) {
                None => {
                    let mut keep = 0;
                    let blen = self.buf.chars().count();
                    for k in (1..=(tag.len() - 1).min(blen)).rev() {
                        let tail: String = self.buf.chars().skip(blen - k).collect();
                        if tag.starts_with(&tail) {
                            keep = k;
                            break;
                        }
                    }
                    let emit: String = self.buf.chars().take(blen - keep).collect();
                    self.buf = self.buf.chars().skip(blen - keep).collect();
                    if !emit.is_empty() {
                        out.push((self.in_think, emit));
                    }
                    break;
                }
                Some(i) => {
                    let before = self.buf[..i].to_string();
                    let after = self.buf[i + tag.len()..].to_string();
                    if !before.is_empty() {
                        out.push((self.in_think, before));
                    }
                    self.in_think = !self.in_think;
                    self.buf = after;
                }
            }
        }
        out
    }

    fn flush(&mut self) -> Vec<(bool, String)> {
        if self.buf.is_empty() {
            return vec![];
        }
        vec![(self.in_think, std::mem::take(&mut self.buf))]
    }
}

/// One stream event from the backend
enum Piece {
    Thinking(String),
    Content(String),
    Error(String),
}

/// `stream_chat(...)` with emit_thinking on the Unsloth backend: pieces in order, ending with an error piece on a failure or an empty answer
/// true (nothing sent) when the backend refused a Thinking request before any answer: the caller retries as Python does
async fn stream_chat(b: &Backend, model: &str, system: &str, messages: &[Value], think: bool, extra: &Map<String, Value>, tx: &tokio::sync::mpsc::Sender<Piece>) -> bool {
    let mut full: Vec<Value> = if system.is_empty() { vec![] } else { vec![json!({"role": "system", "content": system})] };
    full.extend(messages.iter().cloned());
    let mut opts = b.options_for(model);
    for (k, v) in extra {
        opts.insert(k.clone(), v.clone());
    }
    let mut body = Map::new();
    body.insert("model".into(), json!(model));
    body.insert("messages".into(), Value::Array(full));
    body.insert("stream".into(), json!(true));
    body.extend(translate_options(&opts));
    body.insert("chat_template_kwargs".into(), json!({"enable_thinking": think}));
    let Some(req) = backend_request(b, "POST", "/v1/chat/completions", Some(&Value::Object(body))) else { return false };
    let resp = match client().request(req).await {
        Ok(r) => r,
        Err(e) => {
            let _ = tx.send(Piece::Error(format!("[AI unavailable: ConnectionError: ConnectError: {e}]"))).await;
            return false;
        }
    };
    let status = resp.status().as_u16();
    let mut incoming = resp.into_body();
    if status >= 400 {
        let bytes = incoming.collect().await.map(|c| c.to_bytes()).unwrap_or_default();
        let text = String::from_utf8_lossy(&bytes).to_string();
        let message = match serde_json::from_slice::<Value>(&bytes) {
            Ok(Value::Object(o)) => match o.get("error") {
                Some(Value::Object(e)) => e.get("message").map(|m| match m { Value::String(s) => s.clone(), other => other.to_string() }).unwrap_or_default(),
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                _ => String::new(),
            },
            _ => text.chars().take(500).collect(),
        };
        let message = if message.is_empty() { format!("HTTP {status}") } else { message };
        if think && thinking_rejection(status as i64, &message) {
            return true;
        }
        let _ = tx.send(Piece::Error(format!("[AI error: Unsloth {status}: {message}]"))).await;
        return false;
    }
    let mut splitter = Splitter { buf: String::new(), in_think: false };
    record_thinking(model, think, false, false);
    let (mut yielded, mut thinking_chars, mut done_reason): (bool, usize, Option<String>) = (false, 0, None);
    let mut pending = Vec::<u8>::new();
    loop {
        let frame = match incoming.frame().await {
            None => break,
            Some(Ok(f)) => f,
            Some(Err(e)) => {
                let _ = tx.send(Piece::Error(format!("[AI unavailable: ConnectionError: ReadError: {e}]"))).await;
                return false;
            }
        };
        let Ok(data) = frame.into_data() else { continue };
        pending.extend_from_slice(&data);
        while let Some(nl) = pending.iter().position(|c| *c == b'\n') {
            let line: Vec<u8> = pending.drain(..=nl).collect();
            let line = String::from_utf8_lossy(&line).trim_end_matches(['\n', '\r']).to_string();
            let Some(payload) = line.strip_prefix("data:").map(|p| p.trim()) else { continue };
            if payload.is_empty() || payload == "[DONE]" {
                continue;
            }
            let Ok(Value::Object(ev)) = serde_json::from_str::<Value>(payload) else { continue };
            if let Some(err) = ev.get("error").filter(|e| crate::edits::py_truthy_pub(Some(e))) {
                let (status, message) = match err {
                    Value::Object(o) => {
                        let m = o.get("message").filter(|m| crate::edits::py_truthy_pub(Some(m))).or_else(|| o.get("detail").filter(|m| crate::edits::py_truthy_pub(Some(m))));
                        let code = o.get("code").and_then(|c| c.as_i64()).filter(|c| (400..=599).contains(c)).unwrap_or(500);
                        (code, m.map(|m| match m { Value::String(s) => s.clone(), other => other.to_string() }).unwrap_or_else(|| err.to_string()))
                    }
                    Value::String(s) => (500, s.clone()),
                    other => (500, other.to_string()),
                };
                let message: String = message.chars().take(500).collect();
                let message = if message.is_empty() { "stream error".to_string() } else { message };
                if think && !yielded && thinking_rejection(status, &message) {
                    return true;
                }
                let _ = tx.send(Piece::Error(format!("[AI error: Unsloth {status}: {message}]"))).await;
                return false;
            }
            for choice in ev.get("choices").and_then(|c| c.as_array()).cloned().unwrap_or_default() {
                let delta = choice.get("delta").cloned().unwrap_or(json!({}));
                if let Some(t) = delta.get("reasoning_content").and_then(|t| t.as_str()).filter(|t| !t.is_empty()) {
                    let _ = tx.send(Piece::Thinking(t.to_string())).await;
                    thinking_chars += t.chars().count();
                }
                if let Some(t) = delta.get("content").and_then(|t| t.as_str()).filter(|t| !t.is_empty()) {
                    for (think, text) in splitter.feed(t) {
                        if think {
                            thinking_chars += text.chars().count();
                            let _ = tx.send(Piece::Thinking(text)).await;
                        } else {
                            yielded = true;
                            let _ = tx.send(Piece::Content(text)).await;
                        }
                    }
                }
                if let Some(r) = choice.get("finish_reason").and_then(|r| r.as_str()).filter(|r| !r.is_empty()) {
                    done_reason = Some(r.to_string());
                }
            }
        }
    }
    for (think, text) in splitter.flush() {
        if think {
            thinking_chars += text.chars().count();
            let _ = tx.send(Piece::Thinking(text)).await;
        } else {
            yielded = true;
            let _ = tx.send(Piece::Content(text)).await;
        }
    }
    if !yielded {
        let msg = if thinking_chars > 0 {
            format!("[empty response from {model} — it produced {thinking_chars} character(s) of hidden \"thinking\" output but no final answer (usually means it ran out of output budget mid-reasoning). Try a shorter prompt, a higher response-length limit, or a non-reasoning model.]")
        } else {
            let detail = done_reason.map_or("no done_reason reported".to_string(), |d| format!("done_reason={d}"));
            format!("[empty response from {model} ({detail}) — try a different model, or check the Unsloth server logs]")
        };
        let _ = tx.send(Piece::Error(msg)).await;
    }
    false
}

/// `_is_thinking_rejection` under the Unsloth backend: Ollama's wording, or any 400 not about something else
fn thinking_rejection(status: i64, message: &str) -> bool {
    if message.contains("does not support thinking") {
        return true;
    }
    if status != 400 {
        return false;
    }
    let m = message.to_lowercase();
    !["not found", "not loaded", "no model", "context", "num_ctx", "too large", "too many", "exceed", "grammar", "response_format", "invalid request"]
        .iter().any(|k| m.contains(k))
}

/// stream_chat with Python's retry of a refused Thinking request: think off, the `<|think|>` token in the system prompt for a model known
/// (UNSLOTH_KNOWN_MODELS) or set (Settings' per-model override) to think
async fn stream_chat_retrying(b: &Backend, model: &str, system: &str, messages: &[Value], think: bool, extra: &Map<String, Value>, tx: &tokio::sync::mpsc::Sender<Piece>) {
    if !stream_chat(b, model, system, messages, think, extra, tx).await {
        return;
    }
    let mut full: Vec<Value> = if system.is_empty() { vec![] } else { vec![json!({"role": "system", "content": system})] };
    full.extend(messages.iter().cloned());
    let thinks = model == "unsloth/gemma-4-26B-A4B-it-GGUF"
        || crate::edits::py_truthy_pub(b.per_model.get(model).and_then(|m| m.get("thinking")));
    record_thinking(model, true, true, thinks);
    if thinks {
        match full.first_mut() {
            Some(Value::Object(first)) if first.get("role") == Some(&json!("system")) => {
                let c = match first.get("content") { Some(Value::String(c)) => c.clone(), _ => String::new() };
                first.insert("content".into(), json!(format!("<|think|>{c}")));
            }
            _ => full.insert(0, json!({"role": "system", "content": "<|think|>"})),
        }
    }
    stream_chat(b, model, "", &full, false, extra, tx).await;
}

/// The SSE answer of a job: the model note, the pieces with keep-alives, [DONE]; a successful answer cached when the job says so
fn run(job: Job) -> Response<Body> {
    let (tx, rx) = tokio::sync::mpsc::channel::<String>(64);
    tokio::spawn(async move {
        let Job { backend, requested, system, messages, think, extra, cache: key, on_done, detached } = job;
        let (model, note) = resolve_model(&backend, &requested).await;
        if let Some(n) = note {
            let _ = tx.send(sse(&json!({"note": n}))).await;
        }
        let (ptx, mut prx) = tokio::sync::mpsc::channel::<Piece>(64);
        tokio::spawn(async move { stream_chat_retrying(&backend, &model, &system, &messages, think, &extra, &ptx).await });
        // a reader that goes away does not stop the answer: it is read to the end (and an NPC turn saved), as Python's detached
        // streams are (app/ai_background.py DETACH_PATHS)
        let (mut answer, mut had_error, mut gone) = (String::new(), false, false);
        loop {
            match tokio::time::timeout(HEARTBEAT, prx.recv()).await {
                Err(_) => {
                    if !gone && tx.send(": keep-alive\n\n".into()).await.is_err() {
                        if !detached {
                            return;
                        }
                        gone = true;
                    }
                }
                Ok(None) => break,
                Ok(Some(piece)) => {
                    let line = match piece {
                        Piece::Thinking(t) => sse(&json!({"thinking": t})),
                        Piece::Error(t) => {
                            had_error = true;
                            sse(&json!({"error": t}))
                        }
                        Piece::Content(t) => {
                            answer.push_str(&t);
                            sse(&json!({"token": t}))
                        }
                    };
                    if !gone && tx.send(line).await.is_err() {
                        if !detached {
                            return;
                        }
                        gone = true;
                    }
                }
            }
        }
        if let (Some(key), false) = (key, had_error) {
            if let Ok(mut c) = cache().lock() {
                c.retain(|_, (at, _)| at.elapsed() < CACHE_TTL);
                c.insert(key, (Instant::now(), answer.clone()));
            }
        }
        if let (Some(done), false) = (on_done, had_error) {
            if !crate::edits::py_strip_pub(&answer).is_empty() {
                let _ = tokio::task::spawn_blocking(move || done(answer)).await;
            }
        }
        let _ = tx.send("data: [DONE]\n\n".into()).await;
    });
    stream_response(rx)
}

/// `POST /api/chronicler/ask`: the answer, or None (the request goes to Python unchanged)
pub async fn chronicler_ask(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let plan = tokio::task::spawn_blocking(move || plan(&cfg, &uri, &headers, &body)).await.ok()??;
    Some(match plan {
        Plan::Refuse(r) => r,
        Plan::Cached(answer) => {
            let (tx, rx) = tokio::sync::mpsc::channel(4);
            tokio::spawn(async move {
                let _ = tx.send(sse(&json!({"token": answer}))).await;
                let _ = tx.send("data: [DONE]\n\n".to_string()).await;
            });
            stream_response(rx)
        }
        Plan::Fresh(job) => run(job),
    })
}

/// `_OPTION_ALLOWLIST`: (kind, lowest, highest) of a chat preset's generation options
const OPTION_ALLOWLIST: &[(&str, char, Option<f64>, Option<f64>)] = &[
    ("temperature", 'f', Some(0.0), Some(2.0)), ("top_p", 'f', Some(0.0), Some(1.0)), ("top_k", 'i', Some(0.0), None),
    ("repeat_penalty", 'f', Some(0.0), Some(5.0)), ("num_predict", 'i', Some(-2.0), None), ("num_ctx", 'i', Some(1.0), None),
    ("seed", 'i', None, None), ("mirostat", 'i', Some(0.0), Some(2.0)), ("mirostat_tau", 'f', Some(0.0), Some(100.0)),
    ("mirostat_eta", 'f', Some(0.0), Some(10.0)), ("num_gpu", 'i', Some(0.0), Some(1000.0)), ("min_p", 'f', Some(0.0), Some(1.0)),
    ("typical_p", 'f', Some(0.0), Some(1.0)), ("repeat_last_n", 'i', Some(-1.0), Some(131072.0)), ("presence_penalty", 'f', Some(-2.0), Some(2.0)),
    ("frequency_penalty", 'f', Some(-2.0), Some(2.0)), ("num_keep", 'i', Some(0.0), Some(131072.0)), ("num_batch", 'i', Some(1.0), Some(4096.0)),
    ("num_thread", 'i', Some(0.0), Some(256.0)), ("main_gpu", 'i', Some(0.0), Some(15.0)), ("use_mmap", 'b', None, None),
];

/// `_clamp_options`; None for a value whose conversion is Python's to decide (a string, a number past i64)
fn clamp_options(raw: &Value) -> Option<Map<String, Value>> {
    let mut out = Map::new();
    let Value::Object(raw) = raw else { return Some(out) };
    for (k, v) in raw {
        let Some(&(_, kind, lo, hi)) = OPTION_ALLOWLIST.iter().find(|o| o.0 == k) else { continue };
        if v.is_null() {
            continue;
        }
        let (val, num) = match (kind, v) {
            ('b', _) => (json!(crate::edits::py_truthy_pub(Some(v))), None),
            (_, Value::String(_)) => return None,
            ('f', Value::Bool(b)) => (json!(if *b { 1.0 } else { 0.0 }), Some(if *b { 1.0 } else { 0.0 })),
            ('f', Value::Number(n)) => {
                let f = n.as_f64()?;
                (json!(f), Some(f))
            }
            ('i', Value::Bool(b)) => (json!(*b as i64), Some(*b as i64 as f64)),
            ('i', Value::Number(n)) => {
                let i = match n.as_i64() {
                    Some(i) => i,
                    None if n.is_f64() => {
                        let f = n.as_f64()?.trunc();
                        if f.abs() > 9.0e15 { return None; }
                        f as i64
                    }
                    None => return None,
                };
                (json!(i), Some(i as f64))
            }
            _ => continue, // a list or object: TypeError, skipped
        };
        if let Some(x) = num {
            if lo.is_some_and(|lo| x < lo) || hi.is_some_and(|hi| x > hi) {
                continue;
            }
        }
        out.insert(k.clone(), val);
    }
    Some(out)
}

const PLAYER_SYSTEM_MAX_CHARS: usize = 50_000;
const MAX_ATTACHMENT_TEXT_CHARS: usize = 12_000;

/// `get_defaults()[surface]` from ai_models.json beside the database
pub(crate) fn surface_default(cfg: &Config, surface: &str) -> Option<String> {
    let path = cfg.db_path.parent().map(|p| p.join("ai_models.json"))?;
    let data = std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok());
    let defaults = match data.as_ref().and_then(|d| d.get("defaults")) {
        None => return Some(String::new()),
        Some(Value::Object(d)) => d,
        Some(_) => return None, // Python's AttributeError
    };
    if !["chat", "ask_ai", "image", "recap", "assist"].contains(&surface) {
        return Some(String::new());
    }
    match defaults.get(surface) {
        None => Some(String::new()),
        Some(Value::String(s)) => Some(s.clone()),
        Some(_) => None,
    }
}

fn stream_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Job> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if !p.is_gm {
        let w = p.world.as_ref()?;
        let on = |k: &str| crate::edits::py_truthy_pub(w.get(k));
        if !(on("players_can_ask_ai") || on("players_can_use_ai_chat")) {
            return None; // the 403 is Python's
        }
    }
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let text = |k: &str| -> Option<String> {
        match b.get(k) {
            None => Some(String::new()),
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => None,
        }
    };
    let (mut system, model, surface) = (text("system")?, text("model")?, match b.get("surface") { None => "chat".to_string(), Some(Value::String(s)) => s.clone(), _ => return None });
    let think = match b.get("think") {
        None => false,
        Some(Value::Bool(t)) => *t,
        Some(_) => return None,
    };
    let options = match b.get("options") {
        None => Map::new(),
        Some(o @ Value::Object(_)) => clamp_options(o)?,
        Some(_) => return None,
    };
    let messages = build_messages(b.get("messages")?)?;
    let surface = if p.is_gm || surface == "chat" || surface == "ask_ai" { surface } else { "chat".into() };
    let requested = if p.is_gm && !model.is_empty() { model } else { surface_default(cfg, &surface)? };
    if !p.is_gm && system.chars().count() > PLAYER_SYSTEM_MAX_CHARS {
        system = system.chars().take(PLAYER_SYSTEM_MAX_CHARS).collect();
    }
    let backend = backend(&db)?;
    let mut extra = options;
    if think && !extra.contains_key("num_predict") {
        extra.extend(backend.thinking_predict());
    }
    Some(Job { backend, requested, system, messages, think, extra, cache: None, on_done: None, detached: false })
}

/// `POST /api/ai/stream`: the answer, or None (the request goes to Python unchanged)
pub async fn ai_stream(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let job = tokio::task::spawn_blocking(move || stream_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    Some(run(job))
}


const ROLEPLAY_SYSTEM: &str = "You are roleplaying AS the character described below, speaking entirely in first person and in character — never as a narrator or assistant, never breaking character to explain yourself. Infer this character's voice, personality, and manner of speech from the context given. If asked about something the character wouldn't plausibly know, respond the way the character actually would (deflect, guess, get confused, or admit ignorance in-character) rather than stepping outside the role to say you don't have this information. Keep replies conversational — a spoken answer, not a document.";
const NPC_BODY_PREFIX_CHARS: usize = 2500;
const NPC_MAX_MODEL_TURNS: usize = 40;
const NPC_MAX_PERSONALITY_CHARS: usize = 4000;

fn chead(t: &str, n: usize) -> String {
    t.chars().take(n).collect()
}

/// str.capitalize() for ASCII text (None otherwise: Python's)
fn capitalize(t: &str) -> Option<String> {
    if !t.is_ascii() {
        return None;
    }
    let l = t.to_ascii_lowercase();
    let mut c = l.chars();
    Some(c.next().map(|f| f.to_ascii_uppercase().to_string() + c.as_str()).unwrap_or_default())
}

/// str(v) of a custom field value Python would print the same (None for a float or nested value: Python's)
fn py_str(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Bool(b) => Some(if *b { "True" } else { "False" }.into()),
        Value::Number(n) if n.is_i64() || n.is_u64() => Some(n.to_string()),
        Value::Null => Some("None".into()),
        _ => None,
    }
}

/// `_identity_context`: what the model learns about the NPC, under the caller's visibility
fn identity_context(db: &rusqlite::Connection, e: &Map<String, Value>, viewer: (i64, bool)) -> Option<String> {
    let gm = viewer.1;
    let s = |k: &str| -> Option<String> {
        match e.get(k) {
            None | Some(Value::Null) => Some(String::new()),
            Some(Value::String(t)) => Some(t.clone()),
            Some(_) => None,
        }
    };
    let clean = |t: String| -> Option<String> { if gm { Some(t) } else { crate::retrieval::strip(&t, None) } };
    let kind = s("kind")?;
    let mut header = format!("{}: {}", capitalize(if kind.is_empty() { "entity" } else { &kind })?, s("name")?);
    let subtype = s("subtype")?;
    if !subtype.is_empty() {
        header.push_str(&format!("\nSubtype: {subtype}"));
    }
    let summary = crate::edits::py_strip_pub(&clean(s("summary")?)?);
    if !summary.is_empty() {
        header.push_str(&format!("\nSummary: {summary}"));
    }
    let mut parts = vec![header];
    let body = crate::edits::py_strip_pub(&clean(s("body")?)?);
    if !body.is_empty() {
        parts.push(chead(&body, NPC_BODY_PREFIX_CHARS));
    }
    let raw = s("custom_fields_json")?;
    let fields = match serde_json::from_str::<Value>(if raw.is_empty() { "{}" } else { &raw }) {
        Ok(Value::Object(f)) => f,
        Ok(_) => return None,
        Err(_) => Map::new(),
    };
    let mut field_lines = vec![];
    for (k, v) in &fields {
        if matches!(v, Value::Null) || v == &json!("") || v == &json!([]) {
            continue;
        }
        let value = match v {
            Value::Array(a) => a.iter().map(py_str).collect::<Option<Vec<_>>>()?.join(", "),
            Value::Object(o) => o.iter().map(|(k, v)| py_str(v).map(|v| format!("{k}: {v}"))).collect::<Option<Vec<_>>>()?.join("; "),
            other => py_str(other)?,
        };
        let shown = if gm { value } else { crate::edits::py_strip_pub(&crate::retrieval::strip(&value, None)?) };
        if !shown.is_empty() {
            field_lines.push(format!("- {k}: {shown}"));
        }
    }
    if !field_lines.is_empty() {
        parts.push(format!("Custom fields:\n{}", field_lines.join("\n")));
    }
    let personality = crate::edits::py_strip_pub(&clean(s("roleplay_personality")?)?);
    if !personality.is_empty() {
        parts.push(format!("How you speak and behave (standing direction from the GM — follow it over any general instinct):\n{}", chead(&personality, NPC_MAX_PERSONALITY_CHARS)));
    }
    // entity_extra_context(notes, related, strip_gm_only=not gm)
    let eid = e.get("id")?.as_i64()?;
    let mut extra = vec![];
    let mut st = db.prepare(&format!("SELECT content, content_is_html, visible_to_players FROM entity_notes WHERE entity_id = ?1{} ORDER BY created_at DESC LIMIT 10",
        if gm { "" } else { " AND visible_to_players = 1" })).ok()?;
    let notes: Vec<(Option<String>, Option<i64>)> = st.query_map([eid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?;
    let mut note_lines = vec![];
    for (content, html) in notes {
        if html.unwrap_or(0) != 0 {
            return None; // a note stored as HTML: Python's markdown conversion
        }
        let content = content.unwrap_or_default();
        let content = crate::edits::py_strip_pub(&if gm { content } else { crate::retrieval::strip(&content, None)? });
        if !content.is_empty() {
            note_lines.push(format!("- {content}"));
        }
    }
    if !note_lines.is_empty() {
        extra.push(format!("Notes:\n{}", note_lines.join("\n")));
    }
    let visible = if gm { "" } else { " AND (e.visible_to_players IS NOT 0 OR e.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?2))" };
    let mut st = db.prepare(&format!("SELECT DISTINCT e.name FROM entities e JOIN entity_links l ON l.target_id = e.id WHERE l.source_id = ?1{visible}")).ok()?;
    let mut names: Vec<String> = if gm {
        st.query_map([eid], |r| r.get::<_, Option<String>>(0)).ok()?.collect::<Result<Vec<_>, _>>().ok()?
    } else {
        st.query_map([eid, viewer.0], |r| r.get::<_, Option<String>>(0)).ok()?.collect::<Result<Vec<_>, _>>().ok()?
    }.into_iter().map(|n| n.unwrap_or_default()).collect();
    names.sort();
    names.dedup();
    if !names.is_empty() {
        extra.push(format!("Related entities: {}", names.join(", ")));
    }
    let extra = extra.join("\n\n");
    if !extra.is_empty() {
        parts.push(extra);
    }
    Some(parts.into_iter().filter(|p| !crate::edits::py_strip_pub(p).is_empty()).collect::<Vec<_>>().join("\n\n"))
}

fn npc_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Job> {
    let eid: i64 = uri.path().strip_prefix("/api/npc-talk/")?.strip_suffix("/stream")?.parse().ok()?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if p.user_id <= 0 {
        return None;
    }
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    if !p.is_gm {
        let on = |k: &str| crate::edits::py_truthy_pub(w.get(k));
        if !(on("players_can_ask_ai") || on("players_can_use_ai_chat")) {
            return None;
        }
    }
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let Some(Value::String(message)) = b.get("message") else { return None };
    let flag = |k: &str| match b.get(k) { None => Some(false), Some(Value::Bool(t)) => Some(*t), _ => None };
    let (think, use_rag) = (flag("think")?, flag("use_rag")?);
    let model = match b.get("model") { None => String::new(), Some(Value::String(m)) => m.clone(), _ => return None };
    if use_rag && p.is_gm {
        return None; // the GM's lore retrieval (smart_world_context): Python's
    }
    let visible = if p.is_gm { "" } else { " AND (e.visible_to_players IS NOT 0 OR e.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?3))" };
    let mut st = db.prepare(&format!("SELECT e.* FROM entities e WHERE e.id = ?1 AND e.world_id = ?2{visible}")).ok()?;
    let npc = if p.is_gm { st.query_row(rusqlite::params![eid, wid], |r| crate::layout::row_map(r)) } else { st.query_row(rusqlite::params![eid, wid, p.user_id], |r| crate::layout::row_map(r)) }.ok()?;
    if !matches!(npc.get("kind").and_then(|k| k.as_str()), Some("character" | "creature" | "organization")) {
        return None;
    }
    let text = crate::edits::py_strip_pub(message);
    if text.is_empty() {
        return None;
    }
    let conv: Option<(i64, Option<String>)> = db.query_row("SELECT id, messages_json FROM chat_sessions WHERE world_id = ?1 AND user_id = ?2 AND surface = 'npc' AND entity_id = ?3 ORDER BY id LIMIT 1",
        rusqlite::params![wid, p.user_id, eid], |r| Ok((r.get(0)?, r.get(1)?))).ok();
    let mut history = match conv.as_ref().map(|c| c.1.clone().unwrap_or_default()) {
        None => vec![],
        Some(raw) => match serde_json::from_str::<Value>(if raw.is_empty() { "[]" } else { &raw }) {
            Ok(Value::Array(h)) => h,
            _ => return None,
        },
    };
    history.push(json!({"role": "user", "content": text}));
    let mut system = format!("{ROLEPLAY_SYSTEM}\n\n=== The character ===\n\n{}", identity_context(&db, &npc, (p.user_id, p.is_gm))?);
    let instructions = crate::retrieval::instructions_text(&db, wid, !p.is_gm)?;
    if !instructions.is_empty() {
        system.push_str(&format!("\n\n=== Standing setting notes from the GM — inform how you portray the character, never break character to mention them ===\n\n{instructions}"));
    }
    let mut messages = vec![];
    for m in &history[history.len().saturating_sub(NPC_MAX_MODEL_TURNS)..] {
        let Value::Object(m) = m else { return None };
        let Some(Value::String(role)) = m.get("role") else { return None };
        let content = match m.get("content") { None => String::new(), Some(Value::String(c)) => c.clone(), _ => return None };
        messages.push(json!({"role": role, "content": content}));
    }
    let requested = if p.is_gm && !model.is_empty() { model } else { surface_default(cfg, "ask_ai")? };
    let backend = backend(&db)?;
    let extra = if think { backend.thinking_predict() } else { Map::new() };
    let (cfg2, uid, title) = (cfg.clone(), p.user_id, npc.get("name").and_then(|n| n.as_str()).unwrap_or("").to_string());
    let on_done = Box::new(move |answer: String| {
        let Some(db) = crate::writes::open(&cfg2) else { return };
        let mut all = history;
        all.push(json!({"role": "assistant", "content": answer}));
        let stored = nd_render::py_dumps(&Value::Array(all));
        use rusqlite::types::Value as Sql;
        let row: Option<i64> = db.query_row("SELECT id FROM chat_sessions WHERE world_id = ?1 AND user_id = ?2 AND surface = 'npc' AND entity_id = ?3 ORDER BY id LIMIT 1",
            rusqlite::params![wid, uid, eid], |r| r.get(0)).ok();
        match row {
            Some(id) => { crate::orm::update(&db, "chat_sessions", id, &[("messages_json", Sql::Text(stored))]); }
            None => {
                crate::orm::insert(&db, "chat_sessions", &[("world_id", Sql::Integer(wid)), ("user_id", Sql::Integer(uid)), ("surface", Sql::Text("npc".into())),
                    ("entity_id", Sql::Integer(eid)), ("title", Sql::Text(title)), ("messages_json", Sql::Text(stored))]);
            }
        }
    });
    Some(Job { backend, requested, system, messages, think, extra, cache: None, on_done: Some(on_done), detached: true })
}

/// `POST /api/npc-talk/<id>/stream`: the NPC's answer (the conversation saved on a clean finish), or None (Python's)
pub async fn npc_talk_stream(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let job = tokio::task::spawn_blocking(move || npc_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    Some(run(job))
}

const ASSIST_MAX_INTERACTIVE_CHARS: usize = 60_000;
const RECAP_NUM_PREDICT_DEFAULT: i64 = 1024;

/// One AI Assist call to make (`run_assist` for a free-text operation)
struct Assist {
    backend: Backend,
    op: String,
    model: String,
    system: String,
    user: String,
    think: bool,
    extra: Map<String, Value>,
}

/// `run_assist`'s prompt for a free-text operation: (system, user), Err(Some(message)) for its ValueError, Err(None) for an operation
/// that is not a free-text one (Python's)
fn assist_prompt(op: &str, content: &str, meta: &str, instruction: &str, lang: &str) -> Result<(String, String), Option<String>> {
    let systems = nd_render::app::data()["assist_systems"].as_object().ok_or(None)?;
    let base = systems.get(op).and_then(|v| v.as_str()).ok_or(None)?;
    let strip = crate::edits::py_strip_pub;
    if op == "custom" && strip(instruction).is_empty() {
        return Err(Some("The custom operation needs an instruction — say what the AI should do.".into()));
    }
    let compose = |instruction: &str| -> String {
        let mut parts = vec![];
        if !strip(instruction).is_empty() {
            parts.push(format!("GM instruction: {}", strip(instruction)));
        }
        if !strip(meta).is_empty() {
            parts.push(strip(meta));
        }
        if !strip(content).is_empty() {
            parts.push(format!("Content:\n{}", strip(content)));
        }
        parts.join("\n\n")
    };
    let mut user = compose(instruction);
    let mut system = base.to_string();
    if op == "translate" {
        let target = if !lang.is_empty() { lang } else if !instruction.is_empty() { instruction } else { "English" };
        system = format!("{system}\nTarget language: {}.", strip(target));
        user = compose("");
    }
    if op == "code_edit" && strip(instruction).is_empty() {
        return Err(Some("The code_edit operation needs an instruction — describe the change to make.".into()));
    }
    let needs_content = ["expand", "improve", "deslop", "summarize", "translate", "rules_rewrite", "world_summary", "folk_tale", "code_edit"].contains(&op);
    if needs_content && strip(content).is_empty() {
        return Err(Some(format!("Nothing to work on — the '{op}' operation needs some content first.")));
    }
    if op == "analyze" && strip(&user).is_empty() {
        return Err(Some("Nothing to analyze — add a name or some content first.".into()));
    }
    Ok((system, if user.is_empty() { "(empty draft)".into() } else { user }))
}

/// `_free_text_call`'s options: Thinking's widening, else the recap family's cap when nothing bounds the answer
fn assist_options(backend: &Backend, think: bool) -> Map<String, Value> {
    let mut extra = backend.thinking_predict_if(think);
    if !extra.contains_key("num_predict") {
        let configured = backend.options.get("num_predict").and_then(|v| v.as_f64());
        if !configured.is_some_and(|c| c != 0.0 && c >= 0.0) {
            extra.insert("num_predict".into(), json!(RECAP_NUM_PREDICT_DEFAULT));
        }
    }
    extra
}

/// An AssistBody as Pydantic reads it: the text fields, think, use_rag (None when Pydantic would reject or coerce something)
struct AssistBody {
    op: String,
    fields: HashMap<&'static str, String>,
    think: bool,
    use_rag: bool,
    rag: (Option<i64>, Option<i64>),
}

fn assist_body(body: &[u8]) -> Option<AssistBody> {
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let Some(Value::String(op)) = b.get("op") else { return None };
    let mut fields = HashMap::new();
    for k in ["surface", "kind", "name", "summary", "body", "tags", "instruction", "lang", "model"] {
        let v = match b.get(k) {
            None => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(_) => return None,
        };
        fields.insert(k, v);
    }
    let mut rag = [None, None];
    for (i, k) in ["rag_entity_limit", "rag_notes_limit"].iter().enumerate() {
        match b.get(*k) {
            None | Some(Value::Null) => {}
            Some(Value::Number(n)) if n.is_i64() => rag[i] = n.as_i64(),
            Some(_) => return None,
        }
    }
    let flag = |k: &str| match b.get(k) { None => Some(false), Some(Value::Bool(t)) => Some(*t), _ => None };
    Some(AssistBody { op: op.clone(), fields, think: flag("think")?, use_rag: flag("use_rag")?, rag: (rag[0], rag[1]) })
}

impl AssistBody {
    fn f(&self, k: &str) -> &str {
        self.fields.get(k).map(String::as_str).unwrap_or("")
    }

    /// `_assist_meta`: compose_meta({Kind, Name, Current summary, Tags})
    fn meta(&self) -> String {
        [("Kind", "kind"), ("Name", "name"), ("Current summary", "summary"), ("Tags", "tags")].into_iter()
            .filter_map(|(label, k)| { let t = crate::edits::py_strip_pub(self.f(k)); (!t.is_empty()).then(|| format!("{label}: {t}")) })
            .collect::<Vec<_>>().join("\n")
    }
}

/// The caller as the assist routes check it: (page context, world id) for a GM or a GM-assistant in a world
fn assist_caller(db: &rusqlite::Connection, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<(crate::layout::PageCtx, i64)> {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(db, cfg, "POST", uri, headers, now)?;
    if p.user_id <= 0 || !(p.is_gm || p.role.as_deref() == Some("assistant")) {
        return None; // the 403 is Python's
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    Some((p, wid))
}

enum AssistPlan {
    Run(Assist),
    Refuse(String),
}

fn assist_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<(AssistPlan, i64)> {
    let db = crate::writes::open(cfg)?;
    let uid = assist_caller(&db, cfg, uri, headers)?.0.user_id;
    assist_plan_for(cfg, &db, body).map(|p| (p, uid))
}

fn assist_plan_for(cfg: &Config, db: &rusqlite::Connection, body: &[u8]) -> Option<AssistPlan> {
    let b = assist_body(body)?;
    if b.use_rag {
        return None; // the lore retrieval (smart_world_context): Python's
    }
    let content = if !b.f("body").is_empty() { b.f("body") } else { b.f("summary") };
    if content.chars().count() > ASSIST_MAX_INTERACTIVE_CHARS {
        return None;
    }
    let (system, user) = match assist_prompt(&b.op, content, &b.meta(), b.f("instruction"), b.f("lang")) {
        Ok(p) => p,
        Err(Some(message)) => return Some(AssistPlan::Refuse(message)),
        Err(None) => return None,
    };
    let backend = backend(db)?;
    let model = if !b.f("model").is_empty() { b.f("model").to_string() } else { surface_default(cfg, "assist")? };
    let model = if model.is_empty() { backend.model.clone() } else { model };
    let extra = assist_options(&backend, b.think);
    Some(AssistPlan::Run(Assist { backend, op: b.op.clone(), model, system, user, think: b.think, extra }))
}

/// `generate_chat` on the Unsloth backend (one non-streamed answer, Python's sentinel texts on a failure), with the retry of a refused
/// Thinking request
async fn generate_chat(b: &Backend, model: &str, system: &str, messages: &[Value], think: bool, extra: &Map<String, Value>) -> String {
    let mut full: Vec<Value> = if system.is_empty() { vec![] } else { vec![json!({"role": "system", "content": system})] };
    full.extend(messages.iter().cloned());
    let mut opts = b.options_for(model);
    for (k, v) in extra {
        opts.insert(k.clone(), v.clone());
    }
    let mut body = Map::new();
    body.insert("model".into(), json!(model));
    body.insert("messages".into(), Value::Array(full.clone()));
    body.insert("stream".into(), json!(false));
    body.extend(translate_options(&opts));
    body.insert("chat_template_kwargs".into(), json!({"enable_thinking": think}));
    let Some(req) = backend_request(b, "POST", "/v1/chat/completions", Some(&Value::Object(body))) else { return "[AI unavailable: request]".into() };
    let resp = match client().request(req).await {
        Ok(r) => r,
        Err(e) => return format!("[AI unavailable: ConnectError: {e}]"),
    };
    let status = resp.status().as_u16();
    let bytes = match resp.into_body().collect().await {
        Ok(c) => c.to_bytes(),
        Err(e) => return format!("[AI unavailable: ReadError: {e}]"),
    };
    if status >= 400 {
        let message = match serde_json::from_slice::<Value>(&bytes) {
            Ok(Value::Object(o)) => match o.get("error") {
                Some(Value::Object(e)) => e.get("message").map(|m| match m { Value::String(s) => s.clone(), other => other.to_string() }).unwrap_or_default(),
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                _ => String::new(),
            },
            _ => String::from_utf8_lossy(&bytes).chars().take(500).collect(),
        };
        let message = if message.is_empty() { format!("HTTP {status}") } else { message };
        if think && thinking_rejection(status as i64, &message) {
            let thinks = model == "unsloth/gemma-4-26B-A4B-it-GGUF" || crate::edits::py_truthy_pub(b.per_model.get(model).and_then(|m| m.get("thinking")));
            record_thinking(model, true, true, thinks);
            let mut retry = full;
            if thinks {
                match retry.first_mut() {
                    Some(Value::Object(first)) if first.get("role") == Some(&json!("system")) => {
                        let c = match first.get("content") { Some(Value::String(c)) => c.clone(), _ => String::new() };
                        first.insert("content".into(), json!(format!("<|think|>{c}")));
                    }
                    _ => retry.insert(0, json!({"role": "system", "content": "<|think|>"})),
                }
            }
            return Box::pin(generate_chat(b, model, "", &retry, false, extra)).await;
        }
        return format!("[AI error: Unsloth {status}: {message}]");
    }
    let Ok(data) = serde_json::from_slice::<Value>(&bytes) else { return "[AI unavailable: JSONDecodeError: the backend's answer is not JSON]".into() };
    record_thinking(model, think, false, false);
    let choice = data.get("choices").and_then(|c| c.as_array()).and_then(|c| c.first()).cloned().unwrap_or(json!({}));
    let msg = choice.get("message").cloned().unwrap_or(json!({}));
    let content = msg.get("content").and_then(|c| c.as_str()).unwrap_or("").to_string();
    let mut inline = 0;
    if !content.is_empty() {
        static THINK: OnceLock<regex::Regex> = OnceLock::new();
        let stripped = crate::edits::py_strip_pub(&THINK.get_or_init(|| regex::Regex::new(r"(?s)<think>.*?</think>").unwrap()).replace_all(&content, ""));
        inline = content.chars().count() - stripped.chars().count();
        if !stripped.is_empty() {
            return stripped;
        }
    }
    let thinking = msg.get("reasoning_content").and_then(|t| t.as_str()).unwrap_or("").chars().count() + inline;
    if thinking > 0 {
        return format!("[empty response from {model} — it produced {thinking} character(s) of hidden \"thinking\" output but no final answer (usually means it ran out of output budget mid-reasoning). Try a shorter prompt, a higher response-length limit, or a non-reasoning model.]");
    }
    let detail = match choice.get("finish_reason").and_then(|r| r.as_str()).filter(|r| !r.is_empty()) {
        Some(d) => format!("done_reason={d}"),
        None => "no done_reason reported".into(),
    };
    format!("[empty response from {model} ({detail}) — try a different model, or check the Unsloth server logs]")
}

/// `_clean_degenerate_recap`: leaked chat-template tokens removed, a hallucinated new turn cut off, repeated lines collapsed
fn clean_degenerate(text: &str) -> String {
    clean_degenerate_t(text).0
}

/// `_clean_degenerate_recap` with its `truncated` flag
fn clean_degenerate_t(text: &str) -> (String, bool) {
    if text.is_empty() {
        return (String::new(), false);
    }
    static TOKEN: OnceLock<regex::Regex> = OnceLock::new();
    static ROLE: OnceLock<regex::Regex> = OnceLock::new();
    let t = TOKEN.get_or_init(|| regex::Regex::new(r"<\|[^<>|]{0,40}\|>").unwrap()).replace_all(text, "").replace("<start_of_turn>", "").replace("<end_of_turn>", "");
    let role = ROLE.get_or_init(|| regex::Regex::new(r"(?i)^(system|user|assistant|model)$").unwrap());
    let mut cleaned: Vec<&str> = vec![];
    let mut truncated = false;
    for line in t.split('\n') {
        let stripped = crate::edits::py_strip_pub(line);
        if role.is_match(&stripped) && cleaned.iter().any(|l| !crate::edits::py_strip_pub(l).is_empty()) {
            truncated = true;
            break;
        }
        if !stripped.is_empty() && cleaned.last().is_some_and(|l| crate::edits::py_strip_pub(l) == stripped) {
            continue;
        }
        cleaned.push(line);
    }
    (cleaned.join("\n").trim_end_matches(|c: char| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)).to_string(), truncated)
}

/// `POST /api/ai/assist` for a free-text operation: {op, mode: "text", text, model}, or a 502 with the failure text; None (Python's) otherwise
pub async fn ai_assist(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let background = crate::tasks::wants_background(&headers);
    let path = uri.path().to_string();
    let h = headers.clone();
    let (plan, uid) = tokio::task::spawn_blocking(move || assist_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    if background {
        // X-ND-Background: 202 now, the answer kept for GET /api/ai/tasks/<id> (app/ai_background.py)
        return Some(crate::tasks::start(uid, &path, &h, async move { assist_answer(plan).await.unwrap_or_else(|| Response::new(Body::empty())) }));
    }
    assist_answer(plan).await
}

async fn assist_answer(plan: AssistPlan) -> Option<Response<Body>> {
    let a = match plan {
        AssistPlan::Run(a) => a,
        AssistPlan::Refuse(message) => {
            return Response::builder().status(StatusCode::BAD_REQUEST).header(header::CONTENT_TYPE, "application/json").header("x-nd-served-by", "gateway")
                .body(Body::from(nd_render::py_dumps_compact(&json!({"detail": message})))).ok();
        }
    };
    let system = a.system.clone();
    let text = clean_degenerate(&generate_chat(&a.backend, &a.model, &system, &[json!({"role": "user", "content": a.user})], a.think, &a.extra).await);
    if text.starts_with("[AI ") || text.starts_with("[empty response") {
        return Response::builder().status(StatusCode::BAD_GATEWAY).header(header::CONTENT_TYPE, "application/json").header("x-nd-served-by", "gateway")
            .body(Body::from(nd_render::py_dumps_compact(&json!({"detail": text})))).ok();
    }
    crate::edits::json_response(&json!({"op": a.op, "mode": "text", "text": text, "model": a.model}))
}

// ── the AI Assist job (POST /api/ai/assist-job, app/audio_jobs.py's purpose="ai_assist") ──

fn assist_job_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<i64> {
    let db = crate::writes::open(cfg)?;
    let (p, wid) = assist_caller(&db, cfg, uri, headers)?;
    let b = assist_body(body)?;
    // run_assist's ValueErrors surface on the job row at run time; an operation that is not a free-text one is Python's
    if matches!(assist_prompt(&b.op, "", "", "", ""), Err(None)) {
        return None;
    }
    backend(&db)?;
    let model = if !b.f("model").is_empty() { b.f("model").to_string() } else { surface_default(cfg, "assist")? };
    let surface = if b.f("surface").is_empty() { "assist" } else { b.f("surface") };
    let strip = crate::edits::py_strip_pub;
    let (instruction, lang) = (strip(b.f("instruction")), strip(b.f("lang")));
    let mut params = Map::new();
    for (k, v) in [("op", b.op.clone()), ("surface", surface.to_string()), ("meta", b.meta()), ("instruction", instruction.clone()), ("lang", lang)] {
        params.insert(k.into(), json!(v));
    }
    use rusqlite::types::Value as Sql;
    let opt_text = |t: String| if t.is_empty() { Sql::Null } else { Sql::Text(t) };
    let opt_int = |v: Option<i64>| v.map_or(Sql::Null, Sql::Integer);
    let id = crate::orm::insert(&db, "audio_jobs", &[("world_id", Sql::Integer(wid)), ("purpose", Sql::Text("ai_assist".into())),
        ("filename", Sql::Text(format!("AI assist · {surface}"))), ("status", Sql::Text("pending".into())),
        ("created_by_user_id", Sql::Integer(p.user_id)), ("model", opt_text(model)), ("think", Sql::Integer(b.think as i64)),
        ("extra_instructions", opt_text(instruction)), ("use_rag", Sql::Integer((b.use_rag && p.is_gm) as i64)), ("rag_entity_limit", opt_int(b.rag.0)),
        ("rag_notes_limit", opt_int(b.rag.1)), ("assist_params_json", Sql::Text(nd_render::py_dumps(&Value::Object(params)))),
        ("transcript", Sql::Text(b.f("body").to_string())), ("audio_path", Sql::Text(String::new())), ("delete_after", Sql::Integer(0))])?;
    Some(id)
}

/// `POST /api/ai/assist-job`: {job_id}, the job run here; None (Python's) for what the gateway does not run
pub async fn ai_assist_job(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let id = tokio::task::spawn_blocking(move || assist_job_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    start_assist_job(id);
    crate::edits::json_response(&json!({"job_id": id}))
}

fn start_assist_job(id: i64) {
    crate::jobs::spawn("audio_jobs", id, async move {
        run_assist_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// `start_resume_job` for a single-phase row the gateway runs (ai_assist, session_log_recap: straight back to "summarizing") and
/// its run started
pub fn resume_assist_job(db: &rusqlite::Connection, id: i64, reset_attempts: bool) -> Option<()> {
    use rusqlite::types::Value as Sql;
    let (resumed, checkpoint): (i64, Option<String>) = db.query_row("SELECT COALESCE(resumed_count, 0), checkpoint_json FROM audio_jobs WHERE id = ?1", [id],
        |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
    let cp = checkpoint.filter(|c| !c.is_empty()).and_then(|c| serde_json::from_str::<Value>(&c).ok());
    let num = |k: &[&str]| cp.as_ref().and_then(|c| k.iter().find_map(|k| c.get(*k).and_then(|v| v.as_i64()).filter(|n| *n != 0))).map_or(Sql::Null, Sql::Integer);
    crate::orm::update(db, "audio_jobs", id, &[("status", Sql::Text("summarizing".into())), ("error", Sql::Text(String::new())),
        ("chunk_current", num(&["chunks_done", "parts_done"])), ("chunk_total", num(&["chunk_total"])), ("resumed_count", Sql::Integer(if reset_attempts { 0 } else { resumed + 1 })),
        ("run_started_at", Sql::Text(crate::orm::now_stamp())), ("finished_at", Sql::Null)])?;
    let purpose: Option<String> = db.query_row("SELECT purpose FROM audio_jobs WHERE id = ?1", [id], |r| r.get(0)).ok()?;
    match purpose.as_deref() {
        Some("session_log_recap") => start_log_recap_job(id),
        Some("world_summary") => start_world_summary_job(id),
        Some("condense") => start_condense_job(id),
        Some("facts_parse") => start_facts_parse_job(id),
        Some("session_recap") => {
            let (audio, transcript, cp): (Option<String>, Option<String>, Option<String>) = db.query_row("SELECT audio_path, transcript, checkpoint_json FROM audio_jobs WHERE id = ?1", [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?;
            let transcribing = cp.as_deref().is_some_and(|c| c.contains("\"phase\": \"transcribe\""));
            if audio.as_deref().is_some_and(|a| !a.is_empty()) && (transcript.as_deref().unwrap_or("").is_empty() || transcribing) {
                crate::orm::update(db, "audio_jobs", id, &[("status", Sql::Text("transcribing".into()))])?;
                start_audio_job(id)
            } else {
                start_text_recap_job(id)
            }
        }
        _ => start_assist_job(id),
    }
    Some(())
}

/// `_run_job` for purpose="ai_assist": the AI slot, then one run_assist call; the row ends done (result_json), or error
async fn run_assist_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("audio job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<Result<(Assist, String), String>> {
        let db = crate::writes::open(&c)?;
        let row = db.query_row("SELECT model, think, transcript, assist_params_json, use_rag, rag_entity_limit, rag_notes_limit, world_id FROM audio_jobs WHERE id = ?1", [id],
            |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<i64>>(1)?, r.get::<_, Option<String>>(2)?, r.get::<_, Option<String>>(3)?,
                (r.get::<_, Option<i64>>(4)?, r.get::<_, Option<i64>>(5)?, r.get::<_, Option<i64>>(6)?, r.get::<_, Option<i64>>(7)?)))).ok()?;
        let (model, think, transcript, params, (use_rag, rag_e, rag_n, job_world)) = row;
        let model = match model.filter(|m| !m.is_empty()) { Some(m) => m, None => surface_default(&c, "recap").unwrap_or_default() };
        let think = think.map_or(true, |t| t != 0);
        let params = params.as_deref().filter(|p| !p.is_empty()).and_then(|p| serde_json::from_str::<Value>(p).ok()).unwrap_or(json!({}));
        let pg = |k: &str| params.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let op = pg("op");
        let set = |fields: &[(&str, rusqlite::types::Value)]| crate::orm::update(&db, "audio_jobs", id, fields);
        set(&[("status", rusqlite::types::Value::Text("summarizing".into()))]);
        let Some(backend) = backend(&db) else { return Some(Err("AI unavailable: no Unsloth Studio backend is configured".into())) };
        let transcript = transcript.unwrap_or_default();
        let (system, user) = match assist_prompt(&op, &transcript, &pg("meta"), &pg("instruction"), &pg("lang")) {
            Ok((system, user)) => {
                // the job's lore (_build_rag_context on the input itself), never for code_edit
                let query = [transcript.clone(), pg("meta"), pg("instruction")].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join("\n");
                let lore = match (use_rag.unwrap_or(0) != 0, job_world.filter(|w| *w != 0)) {
                    (true, Some(w)) if !crate::edits::py_strip_pub(&query).is_empty() =>
                        crate::retrieval::build_rag_context(&db, w, &query, rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT), &[]).unwrap_or_default(),
                    _ => String::new(),
                };
                (if op == "code_edit" { system } else { with_world_context(&system, &lore) }, user)
            }
            Err(m) => return Some(Err(format!("ValueError: {}", m.unwrap_or_else(|| format!("Unknown AI assist operation: '{op}'"))))),
        };
        let model = if model.is_empty() { backend.model.clone() } else { model };
        let extra = assist_options(&backend, think);
        Some(Ok((Assist { backend, op, model, system, user, think, extra }, String::new())))
    }).await.ok().flatten();
    use rusqlite::types::Value as Sql;
    let finish = move |fields: Vec<(&'static str, Sql)>| {
        let c = cfg.clone();
        tokio::task::spawn_blocking(move || {
            if let Some(db) = crate::writes::open(&c) {
                let mut f = fields;
                f.push(("finished_at", Sql::Text(crate::orm::now_stamp())));
                crate::orm::update(&db, "audio_jobs", id, &f);
            }
        })
    };
    let a = match prep {
        None => return,
        Some(Err(e)) => {
            let _ = finish(vec![("status", Sql::Text("error".into())), ("error", Sql::Text(e)), ("checkpoint_json", Sql::Text(String::new()))]).await;
            return;
        }
        Some(Ok((a, _))) => a,
    };
    let text = clean_degenerate(&generate_chat(&a.backend, &a.model, &a.system, &[json!({"role": "user", "content": a.user})], a.think, &a.extra).await);
    let failed = text.starts_with("[AI ") || text.starts_with("[empty response");
    let fields = if failed {
        vec![("status", Sql::Text("error".into())), ("error", Sql::Text(text)), ("checkpoint_json", Sql::Text(String::new()))]
    } else {
        let result = json!({"op": a.op, "mode": "text", "text": text, "model": a.model});
        vec![("status", Sql::Text("done".into())), ("result_json", Sql::Text(nd_render::py_dumps(&result))), ("checkpoint_json", Sql::Text(String::new()))]
    };
    let _ = finish(fields).await;
}

/// `POST /api/audio-jobs/<id>/cancel` for a job running here (the rest - Python's own jobs, its 400s, resume - is Python's: a job this
/// process held that Python resumes runs there, app/job_owner.py's `release` taking it over)
pub async fn audio_job_cancel(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap) -> Option<Response<Body>> {
    let id: i64 = uri.path().strip_prefix("/api/audio-jobs/")?.strip_suffix("/cancel")?.parse().ok()?;
    if !crate::jobs::is_running("audio_jobs", id) {
        return None;
    }
    tokio::task::spawn_blocking(move || -> Option<Response<Body>> {
        let db = crate::writes::open(&cfg)?;
        let (p, wid) = assist_caller(&db, &cfg, &uri, &headers)?;
        if !p.is_gm && crate::pages::edit_level(&p, p.world.as_ref()?, "background_jobs") != Some(true) {
            return None;
        }
        let status: String = db.query_row("SELECT status FROM audio_jobs WHERE id = ?1 AND world_id = ?2", rusqlite::params![id, wid],
            |r| Ok(r.get::<_, Option<String>>(0)?.unwrap_or_default())).ok()?;
        if !matches!(status.as_str(), "pending" | "transcribing" | "summarizing") || !crate::jobs::abort("audio_jobs", id) {
            return None;
        }
        use rusqlite::types::Value as Sql;
        crate::orm::update(&db, "audio_jobs", id, &[("status", Sql::Text("cancelled".into())), ("error", Sql::Text("Cancelled by GM.".into())),
            ("finished_at", Sql::Text(crate::orm::now_stamp()))])?;
        // a cancelled recording's file goes, as _run_job's `finally` removes it (an interrupted one is kept to resume)
        let (audio, delete_after): (Option<String>, Option<i64>) = db.query_row("SELECT audio_path, delete_after FROM audio_jobs WHERE id = ?1", [id], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
        if delete_after.unwrap_or(0) != 0 {
            if let Some(a) = audio.filter(|a| !a.is_empty()) { let _ = std::fs::remove_file(a); }
        }
        crate::edits::json_response(&json!({"ok": true}))
    }).await.ok()?
}


/// `_build_ollama_messages`: only messages without attachments or with document ones (pictures and audio are Python's)
fn build_messages(v: &Value) -> Option<Vec<Value>> {
    let mut messages = vec![];
    for m in v.as_array()? {
        let Value::Object(m) = m else { return None };
        let (Some(Value::String(role)), Some(Value::String(content))) = (m.get("role"), m.get("content")) else { return None };
        let mut content = content.clone();
        for a in m.get("attachments").map(|a| a.as_array()).unwrap_or(Some(&vec![]))? {
            let Value::Object(a) = a else { return None };
            let s = |k: &str| match a.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(s.clone()), _ => None };
            if a.get("kind") != Some(&json!("document")) || !matches!(a.get("url"), Some(Value::String(_))) {
                return None;
            }
            let (name, t) = (s("name")?, s("text")?);
            if !t.is_empty() {
                let snippet: String = t.chars().take(MAX_ATTACHMENT_TEXT_CHARS).collect();
                content.push_str(&format!("\n\n---\nAttached file: {}\n\n{snippet}\n---", if name.is_empty() { "document" } else { &name }));
            }
        }
        messages.push(json!({"role": role, "content": content}));
    }
    Some(messages)
}

/// `_ctx_override_if_needed(text, reserve)`: a num_ctx when the text needs more than the configured (or assumed 4,096) window
fn ctx_override(b: &Backend, text: &str, reserve: i64) -> Option<i64> {
    let sample: Vec<char> = text.chars().take(4000).collect();
    let per_token: i64 = if sample.is_empty() || (sample.iter().filter(|c| (**c as u32) > 127).count() as f64) / (sample.len() as f64) <= 0.3 { 4 } else { 2 };
    let len = text.chars().count() as i64;
    let input = (len + per_token - 1) / per_token;
    let needed = (input + reserve).max(1024);
    let mut bucket = 1024;
    while bucket < needed {
        bucket *= 2;
    }
    let max_auto = std::env::var("MAX_AUTO_NUM_CTX").ok().and_then(|v| v.parse::<i64>().ok()).unwrap_or(32768).max(8192);
    let needed = bucket.min(max_auto);
    let baseline = b.options.get("num_ctx").and_then(|v| v.as_i64()).filter(|n| *n != 0).unwrap_or(4096);
    (needed > baseline).then_some(needed)
}

// ── background chat jobs (POST /api/ai/chat/jobs, app/chat_jobs.py) ──

fn chat_job_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<i64> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if !p.is_gm {
        return None;
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let text = |k: &str| -> Option<String> {
        match b.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(s.clone()), Some(_) => None }
    };
    let (system, model) = (text("system")?, text("model")?);
    text("surface")?;
    if !matches!(b.get("think"), None | Some(Value::Bool(_))) {
        return None;
    }
    let mut options = match b.get("options") {
        None => Map::new(),
        Some(o @ Value::Object(_)) => clamp_options(o)?,
        Some(_) => return None,
    };
    let messages = build_messages(b.get("messages")?)?;
    let backend = backend(&db)?;
    if !options.contains_key("num_ctx") {
        let total = format!("{system}{}", messages.iter().map(|m| m["content"].as_str().unwrap_or("")).collect::<String>());
        if let Some(n) = ctx_override(&backend, &total, 512) {
            options.insert("num_ctx".into(), json!(n));
        }
    }
    let prompt = messages.iter().rev().find(|m| m["role"] == "user" && !m["content"].as_str().unwrap_or("").is_empty())
        .and_then(|m| m["content"].as_str()).unwrap_or("").chars().take(500).collect::<String>();
    use rusqlite::types::Value as Sql;
    let id = crate::orm::insert(&db, "chat_jobs", &[("world_id", Sql::Integer(wid)), ("prompt", Sql::Text(prompt)),
        ("messages_json", Sql::Text(nd_render::py_dumps(&Value::Array(messages)))), ("system", Sql::Text(system)),
        ("model", if model.is_empty() { Sql::Null } else { Sql::Text(model) }), ("options_json", Sql::Text(nd_render::py_dumps(&Value::Object(options)))),
        ("created_by_user_id", Sql::Integer(p.user_id)), ("status", Sql::Text("pending".into()))])?;
    Some(id)
}

/// `POST /api/ai/chat/jobs`: {job_id}, the job run here
pub async fn chat_job_create(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let id = tokio::task::spawn_blocking(move || chat_job_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    start_chat_job(id);
    crate::edits::json_response(&json!({"job_id": id}))
}

fn start_chat_job(id: i64) {
    crate::jobs::spawn("chat_jobs", id, async move {
        run_chat_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// chat_jobs' `resume_interrupted_jobs` for one row: restarted from its stored request (corrupt JSON: an error row instead)
pub fn restart_chat_job(db: &rusqlite::Connection, id: i64) -> Option<()> {
    use rusqlite::types::Value as Sql;
    let (messages, options, resumed): (Option<String>, Option<String>, i64) = db.query_row("SELECT messages_json, options_json, COALESCE(resumed_count, 0) FROM chat_jobs WHERE id = ?1", [id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?;
    let ok = |t: Option<String>, d: &str| serde_json::from_str::<Value>(t.as_deref().filter(|t| !t.is_empty()).unwrap_or(d)).is_ok();
    if !ok(messages, "[]") || !ok(options, "{}") {
        crate::orm::update(db, "chat_jobs", id, &[("status", Sql::Text("error".into())),
            ("error", Sql::Text("Stored job data was corrupt (unreadable JSON) and could not be resumed.".into()))]);
        return None;
    }
    crate::orm::update(db, "chat_jobs", id, &[("status", Sql::Text("pending".into())), ("error", Sql::Text(String::new())), ("resumed_count", Sql::Integer(resumed + 1))])?;
    start_chat_job(id);
    Some(())
}

/// chat_jobs' `_run_job`: the AI slot, then one generate_chat call (no Thinking); the row ends done (result) or error
async fn run_chat_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("chat job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<Result<(Backend, String, String, Vec<Value>, Map<String, Value>), String>> {
        let db = crate::writes::open(&c)?;
        let (messages, system, model, options): (Option<String>, Option<String>, Option<String>, Option<String>) = db.query_row(
            "SELECT messages_json, system, model, options_json FROM chat_jobs WHERE id = ?1", [id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).ok()?;
        crate::orm::update(&db, "chat_jobs", id, &[("status", rusqlite::types::Value::Text("generating".into()))]);
        let Some(backend) = backend(&db) else { return Some(Err("AI unavailable: no Unsloth Studio backend is configured".into())) };
        let messages = serde_json::from_str::<Value>(messages.as_deref().unwrap_or("[]")).ok().and_then(|v| v.as_array().cloned()).unwrap_or_default();
        let options = serde_json::from_str::<Value>(options.as_deref().unwrap_or("{}")).ok().and_then(|v| v.as_object().cloned()).unwrap_or_default();
        let model = model.filter(|m| !m.is_empty()).unwrap_or_else(|| backend.model.clone());
        Some(Ok((backend, model, system.unwrap_or_default(), messages, options)))
    }).await.ok().flatten();
    use rusqlite::types::Value as Sql;
    let fields = match prep {
        None => return,
        Some(Err(e)) => vec![("status", Sql::Text("error".into())), ("error", Sql::Text(e))],
        Some(Ok((backend, model, system, messages, options))) => {
            let text = generate_chat(&backend, &model, &system, &messages, false, &options).await;
            if text.starts_with("[AI ") || text.starts_with("[empty response") {
                vec![("status", Sql::Text("error".into())), ("error", Sql::Text(text))]
            } else {
                vec![("status", Sql::Text("done".into())), ("result", Sql::Text(text))]
            }
        }
    };
    let _ = tokio::task::spawn_blocking(move || {
        if let Some(db) = crate::writes::open(&cfg) {
            crate::orm::update(&db, "chat_jobs", id, &fields);
        }
    }).await;
}

/// `POST /api/ai/chat/jobs/<id>/cancel` for a chat job running here
pub async fn chat_job_cancel(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap) -> Option<Response<Body>> {
    let id: i64 = uri.path().strip_prefix("/api/ai/chat/jobs/")?.strip_suffix("/cancel")?.parse().ok()?;
    if !crate::jobs::is_running("chat_jobs", id) {
        return None;
    }
    tokio::task::spawn_blocking(move || -> Option<Response<Body>> {
        let db = crate::writes::open(&cfg)?;
        let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        let p = crate::layout::build(&db, &cfg, "POST", &uri, &headers, now)?;
        if !p.is_gm {
            return None;
        }
        let wid = p.world.as_ref()?.get("id")?.as_i64()?;
        let status: String = db.query_row("SELECT status FROM chat_jobs WHERE id = ?1 AND world_id = ?2", rusqlite::params![id, wid],
            |r| Ok(r.get::<_, Option<String>>(0)?.unwrap_or_default())).ok()?;
        if !matches!(status.as_str(), "pending" | "generating") || !crate::jobs::abort("chat_jobs", id) {
            return None;
        }
        use rusqlite::types::Value as Sql;
        crate::orm::update(&db, "chat_jobs", id, &[("status", Sql::Text("cancelled".into())), ("error", Sql::Text("Cancelled by GM.".into()))])?;
        crate::edits::json_response(&json!({"ok": true}))
    }).await.ok()?
}

// ── the Session Log recap (POST /api/session-log/<id>/recap, app/routers/sessions.py; the job: audio_jobs purpose="session_log_recap") ──

const RECAP_ERROR_BACKOFF_SECS: i64 = 60;
const DEFAULT_RAG_ENTITY_LIMIT: i64 = 15;
const DEFAULT_RAG_NOTES_LIMIT: i64 = 5;

fn recap_text(k: &str) -> String {
    nd_render::app::data()["recap_texts"][k].as_str().unwrap_or("").to_string()
}

/// `int(raw)` for a body field the way the route's parsers take it: None for absent / null / "", the number, or Err (Python's 400 or
/// a conversion it alone knows)
fn body_int(v: Option<&Value>) -> Result<Option<i64>, ()> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.is_empty() => Ok(None),
        Some(Value::String(s)) => {
            let t = s.trim();
            if !t.is_empty() && t.trim_start_matches(['+', '-']).bytes().all(|b| b.is_ascii_digit()) && t.matches(['+', '-']).count() <= 1 {
                t.parse::<i64>().map(Some).map_err(|_| ())
            } else {
                Err(())
            }
        }
        Some(Value::Bool(b)) => Ok(Some(*b as i64)),
        Some(Value::Number(n)) => match n.as_i64() {
            Some(i) => Ok(Some(i)),
            None => n.as_f64().filter(|f| f.is_finite() && f.abs() < 9.0e15).map(|f| Some(f.trunc() as i64)).ok_or(()),
        },
        _ => Err(()),
    }
}

/// A DateTime column's text as SQLAlchemy binds it ("YYYY-MM-DD HH:MM:SS.ffffff")
fn sql_stamp(t: &str) -> String {
    if t.len() == 19 { format!("{t}.000000") } else { t.to_string() }
}

enum LogRecap {
    Answer(Value),
    TooSoon,
    Create(i64),
}

fn session_log_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<LogRecap> {
    let sid: i64 = uri.path().strip_prefix("/api/session-log/")?.strip_suffix("/recap")?.parse().ok()?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if p.user_id <= 0 {
        return None;
    }
    let wid: i64 = db.query_row("SELECT world_id FROM game_sessions WHERE id = ?1", [sid], |r| r.get(0)).ok()?;
    let recap_touch: Option<String> = db.query_row("SELECT recap_content_touch FROM worlds WHERE id = ?1", [wid], |r| r.get(0)).ok()?;
    if !p.is_gm && db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |_| Ok(())).is_err() {
        return None; // the 404 is Python's
    }
    let gm = p.is_gm;
    let audience = if gm { "gm" } else { "players" };
    let b = if body.is_empty() { Map::new() } else {
        match serde_json::from_slice::<Value>(body).ok()? { Value::Object(m) => m, _ => return None }
    };
    let text = |k: &str| -> Option<String> {
        match b.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(crate::edits::py_strip_pub(s)), Some(_) => None }
    };
    let model = if gm { text("model")? } else { String::new() };
    let think = match b.get("think") { None => true, v => crate::edits::py_truthy_pub(v) };
    let mut use_rag = crate::edits::py_truthy_pub(b.get("use_rag"));
    let (mut rag_e, mut rag_n) = (body_int(b.get("rag_entity_limit")).ok()?, body_int(b.get("rag_notes_limit")).ok()?);
    if rag_e.is_some_and(|v| v < 0) || rag_n.is_some_and(|v| v < 0) {
        return None;
    }
    let (min_t, max_t) = (body_int(b.get("min_tokens")).ok()?, body_int(b.get("max_tokens")).ok()?);
    if min_t.is_some_and(|v| v < 1) || max_t.is_some_and(|v| v < 1) || matches!((min_t, max_t), (Some(a), Some(z)) if a > z) {
        return None;
    }
    let strictness = match b.get("strictness") {
        None | Some(Value::Null) => "guideline".to_string(),
        Some(Value::String(s)) if s.is_empty() => "guideline".to_string(),
        Some(Value::String(s)) => crate::edits::py_strip_pub(s),
        Some(_) => return None,
    };
    if !["guideline", "firm", "strict"].contains(&strictness.as_str()) {
        return None;
    }
    let extra = text("extra_instructions")?;
    if !gm && use_rag {
        (use_rag, rag_e, rag_n) = (false, None, None);
    }
    if use_rag && !crate::retrieval::session_picks(&db, sid).1.is_empty() {
        return None; // lore with the session's player characters pinned (pc_digest): Python's
    }
    let requested = if model.is_empty() { surface_default(cfg, "recap")? } else { model.clone() };
    let force = crate::edits::py_truthy_pub(b.get("force")) && gm;
    let newest: Option<String> = db.query_row("SELECT max(coalesce(updated_at, created_at)) FROM facts WHERE game_session_id = ?1", [sid], |r| r.get(0)).ok().flatten();
    let min_stamp = "0001-01-01 00:00:00.000000".to_string();
    let cutoff = [newest.map(|t| sql_stamp(&t)), recap_touch.map(|t| sql_stamp(&t))].into_iter().flatten().fold(min_stamp, |a, t| if t > a { t } else { a });
    let filter = "purpose = 'session_log_recap' AND game_session_id = ?1 AND audience = ?2 AND created_at >= ?3 AND coalesce(model, '') = ?4 \
        AND coalesce(think, 1) = ?5 AND coalesce(use_rag, 0) = ?12 AND coalesce(rag_entity_limit, 15) = ?6 AND coalesce(rag_notes_limit, 5) = ?7 \
        AND coalesce(extra_instructions, '') = ?8 AND coalesce(min_tokens, 0) = ?9 AND coalesce(max_tokens, 0) = ?10 AND coalesce(condense_strictness, 'guideline') = ?11";
    let params = rusqlite::params![sid, audience, cutoff, requested, think as i64, rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT),
        extra, min_t.unwrap_or(0), max_t.unwrap_or(0), strictness, use_rag as i64];
    let one = |status_sql: &str| -> Option<(i64, Option<String>, Option<String>)> {
        db.query_row(&format!("SELECT id, result_json, error FROM audio_jobs WHERE {status_sql} AND {filter} ORDER BY created_at DESC, id DESC LIMIT 1"), params,
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()
    };
    if !force {
        if let Some((_, result, _)) = one("status = 'done'") {
            if let Ok(v) = serde_json::from_str::<Value>(result.as_deref().unwrap_or("")) {
                return Some(LogRecap::Answer(v));
            }
        }
    }
    if let Some((id, _, _)) = one("status IN ('pending', 'transcribing', 'summarizing')") {
        return Some(LogRecap::Answer(json!({"pending": true, "job_id": id})));
    }
    backend(&db)?;
    if !force {
        let since = crate::orm::stamp_after(-RECAP_ERROR_BACKOFF_SECS);
        let recent = db.query_row(&format!("SELECT error FROM audio_jobs WHERE status = 'error' AND {filter} AND created_at >= ?13 ORDER BY created_at DESC, id DESC LIMIT 1"),
            rusqlite::params![sid, audience, cutoff, requested, think as i64, rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT),
                extra, min_t.unwrap_or(0), max_t.unwrap_or(0), strictness, use_rag as i64, since], |r| r.get::<_, Option<String>>(0)).ok();
        if let Some(error) = recent {
            let error = error.filter(|e| !e.is_empty()).unwrap_or_else(|| "Recap generation failed.".into());
            return Some(LogRecap::Answer(json!({"failed": true, "error": error})));
        }
    }
    if !gm {
        let mut cd = cooldowns().lock().ok()?;
        if cd.get(&p.user_id).is_some_and(|t| t.elapsed() < COOLDOWN) {
            return Some(LogRecap::TooSoon);
        }
        cd.insert(p.user_id, Instant::now());
    }
    use rusqlite::types::Value as Sql;
    let opt_int = |v: Option<i64>| v.map_or(Sql::Null, Sql::Integer);
    let id = crate::orm::insert(&db, "audio_jobs", &[("world_id", Sql::Integer(wid)), ("purpose", Sql::Text("session_log_recap".into())),
        ("filename", Sql::Text("Session Log recap".into())), ("status", Sql::Text("pending".into())), ("game_session_id", Sql::Integer(sid)),
        ("created_by_user_id", Sql::Integer(p.user_id)), ("audience", Sql::Text(audience.into())), ("model", Sql::Text(requested)),
        ("think", Sql::Integer(think as i64)), ("use_rag", Sql::Integer(use_rag as i64)), ("rag_entity_limit", opt_int(rag_e)), ("rag_notes_limit", opt_int(rag_n)),
        ("extra_instructions", if extra.is_empty() { Sql::Null } else { Sql::Text(extra) }), ("min_tokens", opt_int(min_t)), ("max_tokens", opt_int(max_t)),
        ("condense_strictness", Sql::Text(strictness)), ("audio_path", Sql::Text(String::new())), ("delete_after", Sql::Integer(0))])?;
    Some(LogRecap::Create(id))
}

/// `POST /api/session-log/<id>/recap`: a fresh recap, the running job, a recent failure, or a new job run here
pub async fn session_log_recap(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let plan = tokio::task::spawn_blocking(move || session_log_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    match plan {
        LogRecap::Answer(v) => crate::edits::json_response(&v),
        LogRecap::TooSoon => Response::builder().status(StatusCode::TOO_MANY_REQUESTS).header(header::CONTENT_TYPE, "application/json").header("x-nd-served-by", "gateway")
            .body(Body::from(nd_render::py_dumps_compact(&json!({"detail": "Please wait a few seconds before asking again."})))).ok(),
        LogRecap::Create(id) => {
            start_log_recap_job(id);
            crate::edits::json_response(&json!({"pending": true, "job_id": id}))
        }
    }
}

fn start_log_recap_job(id: i64) {
    crate::jobs::spawn("audio_jobs", id, async move {
        run_log_recap_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// `_recap_degeneration_ratio`: the share of repeated lines (whitespace-normalised, lowercased) once there are 4 or more
fn degeneration_ratio(recap: &str) -> f64 {
    let lines: Vec<String> = recap.split('\n').map(crate::edits::py_strip_pub).filter(|l| !l.is_empty()).collect();
    if lines.len() < 4 {
        return 0.0;
    }
    let mut seen = std::collections::HashSet::new();
    let mut dupes = 0;
    for l in &lines {
        let key = l.to_lowercase().split_whitespace().collect::<Vec<_>>().join(" ");
        if !seen.insert(key) {
            dupes += 1;
        }
    }
    dupes as f64 / lines.len() as f64
}

fn chars_per_token(text: &str) -> i64 {
    let sample: Vec<char> = text.chars().take(4000).collect();
    if sample.is_empty() { return 4 }
    if (sample.iter().filter(|c| (**c as u32) > 127).count() as f64) / (sample.len() as f64) > 0.3 { 2 } else { 4 }
}

/// `_length_target_addendum`
fn length_addendum(min_t: Option<i64>, max_t: Option<i64>, strictness: &str, cpt: i64, noun: &str, source: &str) -> String {
    let mut notes = vec![];
    if let Some(m) = min_t.filter(|v| *v != 0) {
        notes.push(if strictness == "guideline" {
            format!("at least ~{m} tokens (~{} characters) — don't cut it any shorter than that even if you could say it in fewer words", m * cpt)
        } else {
            format!("REQUIRED: the {noun} MUST be at least ~{m} tokens (~{} characters) — a shorter output is a failed request; expand with specific detail (scene beats, names, consequences) from the {source} to reach it", m * cpt)
        });
    }
    if let Some(m) = max_t.filter(|v| *v != 0) {
        notes.push(if strictness == "guideline" {
            format!("no more than ~{m} tokens (~{} characters)", m * cpt)
        } else {
            format!("REQUIRED: stay at or below ~{m} tokens (~{} characters); trim detail rather than exceeding it", m * cpt)
        });
    }
    if notes.is_empty() {
        return String::new();
    }
    format!("\n\n{} for the {noun}: {}.", if strictness == "guideline" { "Length target" } else { "Length requirements" }, notes.join(" and "))
}

/// `summarize_session_from_facts`
async fn summarize_facts(b: &Backend, facts: &[String], model: &str, extra: &str, think: bool, min_t: Option<i64>, max_t: Option<i64>, strictness: &str, lore: &str) -> String {
    if facts.is_empty() {
        return String::new();
    }
    let bullets = facts.iter().map(|f| format!("- {f}")).collect::<Vec<_>>().join("\n");
    let mut system = recap_text("_SUMMARIZE_FACTS_SYSTEM");
    if (strictness == "firm" || strictness == "strict") && !crate::edits::py_strip_pub(extra).is_empty() {
        system.push_str(&recap_text("_STRICT_COMPLIANCE_NOTE"));
    }
    if !extra.is_empty() {
        system = format!("{system}\n\nAdditional instructions from the GM (follow these too): {extra}");
    }
    system = with_world_context(&system, lore);
    system.push_str(&length_addendum(min_t, max_t, strictness, chars_per_token(&bullets), "session recap", "facts"));
    let mut opts = Map::new();
    if let Some(m) = max_t.filter(|m| *m != 0 && !think) {
        opts.insert("num_predict".into(), json!(m));
    }
    opts.extend(b.thinking_predict_if(think));
    if !opts.contains_key("num_predict") {
        let configured = b.options.get("num_predict").and_then(|v| v.as_f64());
        if !configured.is_some_and(|c| c != 0.0 && c >= 0.0) {
            opts.insert("num_predict".into(), json!(RECAP_NUM_PREDICT_DEFAULT));
        }
    }
    let msgs = [json!({"role": "user", "content": bullets})];
    let (mut recap, mut truncated) = clean_degenerate_t(&generate_chat(b, model, &system, &msgs, think, &opts).await);
    if (truncated || degeneration_ratio(&recap) > 0.4) && !opts.contains_key("repeat_penalty") {
        let mut retry_opts = opts.clone();
        retry_opts.insert("repeat_penalty".into(), json!(1.2));
        let (retry, retry_trunc) = clean_degenerate_t(&generate_chat(b, model, &system, &msgs, think, &retry_opts).await);
        let (rr, or) = (degeneration_ratio(&retry), degeneration_ratio(&recap));
        if rr < or || (rr == or && !retry_trunc && truncated) {
            (recap, truncated) = (retry, retry_trunc);
        }
    }
    let _ = truncated;
    recap
}

/// `_format_session_rewards`: the deterministic Rewards footer
fn session_rewards(xp: i64, loot: &[Value]) -> String {
    let show = |v: Option<&Value>, d: &str| match v {
        None => d.to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(t)) => if *t { "True".into() } else { "False".into() },
        Some(Value::Null) => "None".into(),
        Some(other) => other.to_string(),
    };
    let mut lines = vec![];
    if xp != 0 {
        lines.push(format!("- **XP awarded:** {xp}"));
    }
    if !loot.is_empty() {
        let items: Vec<String> = loot.iter().map(|it| format!("{} ×{}", show(it.get("name"), "?"), show(it.get("qty"), "1"))).collect();
        lines.push(format!("- **Loot given:** {}", items.join(", ")));
    }
    if lines.is_empty() { String::new() } else { format!("## Rewards\n{}", lines.join("\n")) }
}

/// `_run_job` for purpose="session_log_recap"
async fn run_log_recap_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("audio job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    use rusqlite::types::Value as Sql;
    type Prep = (Backend, String, bool, Vec<String>, String, Option<i64>, Option<i64>, String, i64, Vec<Value>, String);
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<Result<Prep, String>> {
        let db = crate::writes::open(&c)?;
        let (model, think, gsid, audience, extra, min_t, max_t, strictness): (Option<String>, Option<i64>, Option<i64>, Option<String>, Option<String>, Option<i64>, Option<i64>, Option<String>) =
            db.query_row("SELECT model, think, game_session_id, audience, extra_instructions, min_tokens, max_tokens, condense_strictness FROM audio_jobs WHERE id = ?1", [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?))).ok()?;
        let (use_rag, rag_e, rag_n, job_world): (Option<i64>, Option<i64>, Option<i64>, Option<i64>) = db.query_row("SELECT use_rag, rag_entity_limit, rag_notes_limit, world_id FROM audio_jobs WHERE id = ?1", [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).ok()?;
        crate::orm::update(&db, "audio_jobs", id, &[("status", Sql::Text("summarizing".into()))]);
        let gs: Option<(i64, Option<i64>, Option<String>)> = gsid.and_then(|g| db.query_row("SELECT world_id, xp_awarded, loot_json FROM game_sessions WHERE id = ?1", [g],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok());
        let Some((wid, xp, loot)) = gs else { return Some(Err("This session no longer exists.".into())) };
        let audience = audience.unwrap_or_else(|| "players".into());
        let mut st = db.prepare(&format!("SELECT content FROM facts WHERE game_session_id = ?1{} ORDER BY created_at",
            if audience == "gm" { "" } else { " AND visible_to_players IS NOT 0" })).ok()?;
        let facts: Vec<String> = st.query_map([gsid], |r| r.get::<_, Option<String>>(0)).ok()?.filter_map(Result::ok).map(|f| f.unwrap_or_default()).collect();
        let world_instr: Option<String> = db.query_row("SELECT recap_instructions FROM worlds WHERE id = ?1", [wid], |r| r.get(0)).ok().flatten();
        let instructions = [crate::edits::py_strip_pub(&world_instr.unwrap_or_default()), crate::edits::py_strip_pub(&extra.unwrap_or_default())]
            .into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n\n");
        let loot: Vec<Value> = serde_json::from_str::<Value>(loot.as_deref().filter(|l| !l.is_empty()).unwrap_or("[]")).ok().and_then(|v| v.as_array().cloned()).unwrap_or_default();
        let model = model.filter(|m| !m.is_empty()).or_else(|| surface_default(&c, "recap")).unwrap_or_default();
        let Some(backend) = backend(&db) else { return Some(Err("AI unavailable: no Unsloth Studio backend is configured".into())) };
        let model = if model.is_empty() { backend.model.clone() } else { model };
        let lore = match (use_rag.unwrap_or(0) != 0, job_world.filter(|w| *w != 0), facts.is_empty()) {
            (true, Some(w), false) => crate::retrieval::build_rag_context(&db, w, &facts.join("\n"), rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT),
                &gsid.map(|g| crate::retrieval::session_picks(&db, g).0).unwrap_or_default()).unwrap_or_default(),
            _ => String::new(),
        };
        Some(Ok((backend, model, think.map_or(true, |t| t != 0), facts, instructions, min_t, max_t, strictness.unwrap_or_else(|| "guideline".into()), xp.unwrap_or(0), loot, lore)))
    }).await.ok().flatten();
    let finish = |fields: Vec<(&'static str, Sql)>| {
        let c = cfg.clone();
        tokio::task::spawn_blocking(move || {
            if let Some(db) = crate::writes::open(&c) {
                let mut f = fields;
                f.push(("finished_at", Sql::Text(crate::orm::now_stamp())));
                f.push(("checkpoint_json", Sql::Text(String::new())));
                crate::orm::update(&db, "audio_jobs", id, &f);
            }
        })
    };
    let (backend, model, think, facts, instructions, min_t, max_t, strictness, xp, loot, lore) = match prep {
        None => return,
        Some(Err(e)) => {
            let _ = finish(vec![("status", Sql::Text("error".into())), ("error", Sql::Text(e))]).await;
            return;
        }
        Some(Ok(p)) => p,
    };
    if facts.is_empty() {
        let _ = finish(vec![("status", Sql::Text("done".into())), ("result_json", Sql::Text(nd_render::py_dumps(&json!({"recap": "", "empty": true}))))]).await;
        return;
    }
    let failed = |t: &str| t.starts_with("[AI ") || t.starts_with("[empty response");
    let starved = |t: &str| t.starts_with("[empty response") && t.contains("hidden \"thinking\"");
    let mut recap = summarize_facts(&backend, &facts, &model, &instructions, think, min_t, max_t, &strictness, &lore).await;
    if failed(&recap) || starved(&recap) {
        let _ = finish(vec![("status", Sql::Text("error".into())), ("error", Sql::Text(recap))]).await;
        return;
    }
    if strictness == "strict" && (min_t.is_some_and(|v| v != 0) || max_t.is_some_and(|v| v != 0)) {
        let cpt = chars_per_token(&recap);
        let est = (recap.chars().count() as i64 + cpt - 1) / cpt;
        let below = min_t.is_some_and(|m| m != 0 && (est as f64) < m as f64 * 0.85);
        let above = max_t.is_some_and(|m| m != 0 && (est as f64) > m as f64 * 1.15);
        if below || above {
            let note = if below {
                format!("Your previous draft was ~{est} tokens; the requirement is at least ~{} tokens. Expand it with more specific detail from the facts.", min_t.unwrap_or(0))
            } else {
                format!("Your previous draft was ~{est} tokens; the requirement is at most ~{} tokens. Trim it to fit.", max_t.unwrap_or(0))
            };
            let combined = [crate::edits::py_strip_pub(&instructions), crate::edits::py_strip_pub(&note)].into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n\n");
            let retry = summarize_facts(&backend, &facts, &model, &combined, think, min_t, max_t, &strictness, &lore).await;
            if !failed(&retry) && !starved(&retry) {
                recap = retry;
            }
        }
    }
    let rewards = session_rewards(xp, &loot);
    if !rewards.is_empty() {
        recap = format!("{recap}\n\n{rewards}");
    }
    let _ = finish(vec![("status", Sql::Text("done".into())), ("result_json", Sql::Text(nd_render::py_dumps(&json!({"recap": recap}))))]).await;
}


/// `_with_world_context(system, lore)`: retrieved lore ahead of the system prompt, for accuracy only
fn with_world_context(system: &str, lore: &str) -> String {
    if lore.is_empty() {
        return system.to_string();
    }
    format!("Relevant world lore and notes (for accuracy only — don't invent beyond what's here). If the input text refers to a character or place differently than this list — a different spelling, transliteration, or translation, e.g. because the input is in another language — use the exact name from this list instead; don't invent your own translation or transliteration of it:\n{lore}\n\n{system}")
}

// ── the dashboard's world summary (POST /api/ai/world-summary; the job: audio_jobs purpose="world_summary") ──

fn world_summary_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<i64> {
    let db = crate::writes::open(cfg)?;
    let (p, wid) = assist_caller(&db, cfg, uri, headers)?;
    let b = if body.is_empty() { Map::new() } else {
        match serde_json::from_slice::<Value>(body).ok()? { Value::Object(m) => m, _ => return None }
    };
    let model = match b.get("model") { None => String::new(), Some(Value::String(m)) => m.clone(), _ => return None };
    let flag = |k: &str, d: bool| match b.get(k) { None => Some(d), Some(Value::Bool(t)) => Some(*t), _ => None };
    let (think, use_rag) = (flag("think", true)?, flag("use_rag", false)?);
    let mut rag = [None, None];
    for (i, k) in ["rag_entity_limit", "rag_notes_limit"].iter().enumerate() {
        match b.get(*k) {
            None | Some(Value::Null) => {}
            Some(Value::Number(n)) if n.is_i64() => rag[i] = n.as_i64(),
            Some(_) => return None,
        }
    }
    backend(&db)?;
    let model = if model.is_empty() { surface_default(cfg, "assist")? } else { model };
    use rusqlite::types::Value as Sql;
    let opt_int = |v: Option<i64>| v.map_or(Sql::Null, Sql::Integer);
    crate::orm::insert(&db, "audio_jobs", &[("world_id", Sql::Integer(wid)), ("purpose", Sql::Text("world_summary".into())),
        ("filename", Sql::Text("World summary".into())), ("status", Sql::Text("pending".into())), ("created_by_user_id", Sql::Integer(p.user_id)),
        ("model", if model.is_empty() { Sql::Null } else { Sql::Text(model) }), ("think", Sql::Integer(think as i64)),
        ("use_rag", Sql::Integer((use_rag && p.is_gm) as i64)), ("rag_entity_limit", opt_int(rag[0])), ("rag_notes_limit", opt_int(rag[1])),
        ("audience", Sql::Text(if p.is_gm { "gm" } else { "players" }.into())), ("audio_path", Sql::Text(String::new())), ("delete_after", Sql::Integer(0))])
}

/// `POST /api/ai/world-summary`: {job_id, pending}, the job run here
pub async fn world_summary_create(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let id = tokio::task::spawn_blocking(move || world_summary_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    start_world_summary_job(id);
    crate::edits::json_response(&json!({"job_id": id, "pending": true}))
}

fn start_world_summary_job(id: i64) {
    crate::jobs::spawn("audio_jobs", id, async move {
        run_world_summary_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// `_world_summary_state_text(world, audience)`: the campaign digest the summary is written from ("" when there is nothing)
fn world_state_text(db: &rusqlite::Connection, wid: i64, audience: &str) -> Option<String> {
    let (name, description): (Option<String>, Option<String>) = match db.query_row("SELECT name, description FROM worlds WHERE id = ?1", [wid], |r| Ok((r.get(0)?, r.get(1)?))) {
        Ok(v) => v,
        Err(_) => return Some(String::new()),
    };
    let vis = if audience == "gm" { "" } else { " AND visible_to_players IS NOT 0" };
    let rows = |sql: String| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(&sql).ok()?;
        let r = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok();
        r
    };
    let entities = rows(format!("SELECT kind, name, subtype, summary FROM entities WHERE world_id = ?1{vis} ORDER BY updated_at DESC LIMIT 120"))?;
    let quests = rows(format!("SELECT status, title, summary FROM quests WHERE world_id = ?1{vis} ORDER BY updated_at DESC LIMIT 30"))?;
    let facts = rows(format!("SELECT content FROM facts WHERE world_id = ?1{vis} ORDER BY created_at DESC LIMIT 40"))?;
    let sessions = rows("SELECT session_num, title, session_date, summary FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC LIMIT 10".into())?;
    if entities.is_empty() && quests.is_empty() && facts.is_empty() && sessions.is_empty() {
        return Some(String::new());
    }
    let t = |m: &Map<String, Value>, k: &str| -> String {
        match m.get(k) { Some(Value::String(s)) => s.clone(), Some(Value::Null) | None => String::new(), Some(v) => v.to_string() }
    };
    let some = |m: &Map<String, Value>, k: &str| crate::edits::py_truthy_pub(m.get(k));
    let mut lines = vec![format!("# {}", name.unwrap_or_else(|| "None".into())), description.unwrap_or_default(), String::new()];
    if !entities.is_empty() {
        lines.push(format!("## Recent entities ({})", entities.len()));
        for e in &entities {
            let mut line = format!("- [{}] {}", t(e, "kind"), t(e, "name"));
            if some(e, "subtype") { line.push_str(&format!(" ({})", t(e, "subtype"))); }
            if some(e, "summary") { line.push_str(&format!(": {}", t(e, "summary"))); }
            lines.push(line);
        }
        lines.push(String::new());
    }
    if !quests.is_empty() {
        lines.push(format!("## Quests ({})", quests.len()));
        for q in &quests {
            let status = if some(q, "status") { t(q, "status") } else { "active".into() };
            let mut line = format!("- [{status}] {}", t(q, "title"));
            if some(q, "summary") { line.push_str(&format!(": {}", t(q, "summary"))); }
            lines.push(line);
        }
        lines.push(String::new());
    }
    if !facts.is_empty() {
        lines.push(format!("## Recent facts ({}, newest first)", facts.len()));
        for f in &facts {
            lines.push(format!("- {}", match f.get("content") { Some(Value::String(s)) => s.clone(), _ => "None".into() }));
        }
        lines.push(String::new());
    }
    if !sessions.is_empty() {
        lines.push(format!("## Recent sessions ({})", sessions.len()));
        for s in &sessions {
            let num = if some(s, "session_num") { t(s, "session_num") } else { "?".into() };
            let mut line = format!("- #{num} {}", t(s, "title"));
            if some(s, "session_date") { line.push_str(&format!(" ({})", t(s, "session_date"))); }
            if some(s, "summary") { line.push_str(&format!(": {}", t(s, "summary").chars().take(300).collect::<String>())); }
            lines.push(line);
        }
        lines.push(String::new());
    }
    Some(lines.join("\n"))
}

/// `_run_job` for purpose="world_summary": the digest built now, one world_summary run_assist call, the text to `recap`
async fn run_world_summary_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("audio job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    use rusqlite::types::Value as Sql;
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<Result<(Backend, String, bool, (String, String), Map<String, Value>), String>> {
        let db = crate::writes::open(&c)?;
        let (wid, model, think, use_rag, rag_e, rag_n, audience): (i64, Option<String>, Option<i64>, Option<i64>, Option<i64>, Option<i64>, Option<String>) =
            db.query_row("SELECT world_id, model, think, use_rag, rag_entity_limit, rag_notes_limit, audience FROM audio_jobs WHERE id = ?1", [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?))).ok()?;
        crate::orm::update(&db, "audio_jobs", id, &[("status", Sql::Text("summarizing".into()))]);
        let audience = audience.filter(|a| !a.is_empty()).unwrap_or_else(|| "players".into());
        let state = world_state_text(&db, wid, &audience)?;
        if crate::edits::py_strip_pub(&state).is_empty() {
            return Some(Err("This world has no content to summarize yet.".into()));
        }
        let lore = if use_rag.unwrap_or(0) != 0 && wid != 0 {
            crate::retrieval::build_rag_context(&db, wid, &state, rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT), &[]).unwrap_or_default()
        } else { String::new() };
        let Some(backend) = backend(&db) else { return Some(Err("AI unavailable: no Unsloth Studio backend is configured".into())) };
        let model = model.filter(|m| !m.is_empty()).or_else(|| surface_default(&c, "recap")).unwrap_or_default();
        let model = if model.is_empty() { backend.model.clone() } else { model };
        let think = think.map_or(true, |t| t != 0);
        let (system, user) = assist_prompt("world_summary", &state, "", "", "").ok()?;
        let extra = assist_options(&backend, think);
        Some(Ok((backend, model, think, (with_world_context(&system, &lore), user), extra)))
    }).await.ok().flatten();
    let fields = match prep {
        None => return,
        Some(Err(e)) => vec![("status", Sql::Text("error".into())), ("error", Sql::Text(e))],
        Some(Ok((backend, model, think, (system, user), extra))) => {
            let text = clean_degenerate(&generate_chat(&backend, &model, &system, &[json!({"role": "user", "content": user})], think, &extra).await);
            if text.starts_with("[AI ") || text.starts_with("[empty response") {
                vec![("status", Sql::Text("error".into())), ("error", Sql::Text(text))]
            } else {
                vec![("status", Sql::Text("done".into())), ("recap", Sql::Text(text))]
            }
        }
    };
    let _ = tokio::task::spawn_blocking(move || {
        if let Some(db) = crate::writes::open(&cfg) {
            let mut f = fields;
            f.push(("finished_at", Sql::Text(crate::orm::now_stamp())));
            f.push(("checkpoint_json", Sql::Text(String::new())));
            crate::orm::update(&db, "audio_jobs", id, &f);
        }
    }).await;
}


// ── Condense (POST /api/sessions/ai/condense-job; the job: audio_jobs purpose="condense") ──

fn condense_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<i64> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if p.user_id <= 0 {
        return None;
    }
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    if crate::pages::edit_level(&p, w, "sessions") != Some(true) {
        return None;
    }
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let text = |k: &str| -> Option<String> {
        match b.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(crate::edits::py_strip_pub(s)), Some(_) => None }
    };
    let recap = text("recap")?;
    if recap.is_empty() {
        return None;
    }
    let (min_t, max_t) = (body_int(b.get("min_tokens")).ok()?, body_int(b.get("max_tokens")).ok()?);
    if min_t.is_some_and(|v| v < 1) || max_t.is_some_and(|v| v < 1) || matches!((min_t, max_t), (Some(a), Some(z)) if a > z) {
        return None;
    }
    let strictness = match b.get("strictness") {
        None | Some(Value::Null) => "guideline".to_string(),
        Some(Value::String(s)) if s.is_empty() => "guideline".to_string(),
        Some(Value::String(s)) => crate::edits::py_strip_pub(s),
        Some(_) => return None,
    };
    if !["guideline", "firm", "strict"].contains(&strictness.as_str()) {
        return None;
    }
    let extra = text("extra_instructions")?;
    // _reject_if_too_long_to_condense: Python's 400
    let all = format!("{recap}{extra}");
    let cpt = chars_per_token(&all);
    let max_auto = std::env::var("MAX_AUTO_NUM_CTX").ok().and_then(|v| v.parse::<i64>().ok()).unwrap_or(32768).max(8192);
    if (all.chars().count() as i64 + cpt - 1) / cpt > max_auto - 512 {
        return None;
    }
    let use_rag = crate::edits::py_truthy_pub(b.get("use_rag")) && p.is_gm;
    let (rag_e, rag_n) = (body_int(b.get("rag_entity_limit")).ok()?, body_int(b.get("rag_notes_limit")).ok()?);
    if rag_e.is_some_and(|v| v < 0) || rag_n.is_some_and(|v| v < 0) {
        return None;
    }
    let gs_id = match b.get("game_session_id") {
        v if !crate::edits::py_truthy_pub(v) => None,
        Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
        Some(Value::String(s)) if !s.is_empty() && s.bytes().all(|c| c.is_ascii_digit()) => s.parse().ok(),
        _ => return None,
    };
    if use_rag && gs_id.is_some_and(|g| !crate::retrieval::session_picks(&db, g).1.is_empty()) {
        return None; // lore with the session's player characters pinned (pc_digest): Python's
    }
    let model = text("model")?;
    let think = match b.get("think") { None => true, v => crate::edits::py_truthy_pub(v) };
    backend(&db)?;
    use rusqlite::types::Value as Sql;
    let opt_int = |v: Option<i64>| v.map_or(Sql::Null, Sql::Integer);
    crate::orm::insert(&db, "audio_jobs", &[("world_id", Sql::Integer(wid)), ("purpose", Sql::Text("condense".into())), ("filename", Sql::Text("Condense".into())),
        ("status", Sql::Text("pending".into())), ("game_session_id", opt_int(gs_id)), ("created_by_user_id", Sql::Integer(p.user_id)),
        ("model", if model.is_empty() { Sql::Null } else { Sql::Text(model) }), ("think", Sql::Integer(think as i64)),
        ("fit_context", Sql::Integer(crate::edits::py_truthy_pub(b.get("fit_context")) as i64)),
        ("extra_instructions", if extra.is_empty() { Sql::Null } else { Sql::Text(extra) }), ("min_tokens", opt_int(min_t)), ("max_tokens", opt_int(max_t)),
        ("condense_strictness", Sql::Text(strictness)), ("use_rag", Sql::Integer(use_rag as i64)), ("rag_entity_limit", opt_int(rag_e)),
        ("rag_notes_limit", opt_int(rag_n)), ("transcript", Sql::Text(recap)), ("audio_path", Sql::Text(String::new())), ("delete_after", Sql::Integer(0))])
}

/// `POST /api/sessions/ai/condense-job`: {job_id}, the job run here
pub async fn condense_job_create(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let id = tokio::task::spawn_blocking(move || condense_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    start_condense_job(id);
    crate::edits::json_response(&json!({"job_id": id}))
}

fn start_condense_job(id: i64) {
    crate::jobs::spawn("audio_jobs", id, async move {
        run_condense_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// `condense_recap` (with `condense_call_options`' num_ctx left out: Unsloth fixes the context at load)
async fn condense(b: &Backend, recap: &str, model: &str, think: bool, extra: &str, min_t: Option<i64>, max_t: Option<i64>, lore: &str, expanded: bool, strictness: &str) -> String {
    let mut system = recap_text("_CONDENSE_RECAP_SYSTEM");
    if (strictness == "firm" || strictness == "strict") && !crate::edits::py_strip_pub(extra).is_empty() {
        system.push_str(&recap_text("_STRICT_COMPLIANCE_NOTE"));
    }
    if !extra.is_empty() {
        system = format!("{system}\n\nAdditional instructions from the GM (follow these too): {extra}");
    }
    system = with_world_context(&system, lore);
    system.push_str(&length_addendum(min_t, max_t, strictness, chars_per_token(recap), "condensed recap", "recap"));
    let mut opts = Map::new();
    if let Some(m) = max_t.filter(|m| *m != 0 && !think) {
        opts.insert("num_predict".into(), json!(m));
    }
    if expanded {
        let configured = b.options.get("num_predict").and_then(|v| v.as_i64()).unwrap_or(0).max(0);
        let headroom = std::env::var("THINKING_HEADROOM_TOKENS").ok().and_then(|v| v.parse::<i64>().ok()).unwrap_or(4096).max(0);
        let expanded_headroom = std::env::var("THINKING_EXPANDED_HEADROOM_TOKENS").ok().and_then(|v| v.parse::<i64>().ok()).unwrap_or(12288).max(headroom);
        opts.insert("num_predict".into(), json!(configured + expanded_headroom));
    } else {
        opts.extend(b.thinking_predict_if(think));
    }
    if !opts.contains_key("num_predict") {
        let configured = b.options.get("num_predict").and_then(|v| v.as_f64());
        if !configured.is_some_and(|c| c != 0.0 && c >= 0.0) {
            opts.insert("num_predict".into(), json!(RECAP_NUM_PREDICT_DEFAULT));
        }
    }
    generate_chat(b, model, &system, &[json!({"role": "user", "content": recap})], think, &opts).await
}

/// `_run_job` for purpose="condense": lore, the Thinking ladder for a starved answer, the strict-length retry, the done / error row
async fn run_condense_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("audio job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    use rusqlite::types::Value as Sql;
    struct Job { b: Backend, model: String, think: bool, text: String, instructions: String, min_t: Option<i64>, max_t: Option<i64>, strictness: String, lore: String }
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<Result<Job, String>> {
        let db = crate::writes::open(&c)?;
        let row: (i64, Option<String>, Option<i64>, Option<String>, Option<String>, Option<i64>, Option<i64>, Option<String>, Option<i64>, Option<i64>, Option<i64>, Option<i64>) =
            db.query_row("SELECT world_id, model, think, transcript, extra_instructions, min_tokens, max_tokens, condense_strictness, use_rag, rag_entity_limit, rag_notes_limit, game_session_id FROM audio_jobs WHERE id = ?1",
                [id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?, r.get(10)?, r.get(11)?))).ok()?;
        let (wid, model, think, text, extra, min_t, max_t, strictness, use_rag, rag_e, rag_n, gsid) = row;
        let text = text.unwrap_or_default();
        let lore = if use_rag.unwrap_or(0) != 0 && wid != 0 {
            let pins = gsid.map(|g| crate::retrieval::session_picks(&db, g).0).unwrap_or_default();
            crate::retrieval::build_rag_context(&db, wid, &text, rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT), &pins).unwrap_or_default()
        } else { String::new() };
        crate::orm::update(&db, "audio_jobs", id, &[("status", Sql::Text("summarizing".into()))]);
        let world_instr: Option<String> = db.query_row("SELECT recap_instructions FROM worlds WHERE id = ?1", [wid], |r| r.get(0)).ok().flatten();
        let instructions = [crate::edits::py_strip_pub(&world_instr.unwrap_or_default()), crate::edits::py_strip_pub(&extra.unwrap_or_default())]
            .into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n\n");
        let Some(b) = backend(&db) else { return Some(Err("AI unavailable: no Unsloth Studio backend is configured".into())) };
        let model = model.filter(|m| !m.is_empty()).or_else(|| surface_default(&c, "recap")).unwrap_or_default();
        let model = if model.is_empty() { b.model.clone() } else { model };
        Some(Ok(Job { b, model, think: think.map_or(true, |t| t != 0), text, instructions, min_t, max_t, strictness: strictness.unwrap_or_else(|| "guideline".into()), lore }))
    }).await.ok().flatten();
    let set = |fields: Vec<(&'static str, Sql)>| {
        let c = cfg.clone();
        tokio::task::spawn_blocking(move || { if let Some(db) = crate::writes::open(&c) { crate::orm::update(&db, "audio_jobs", id, &fields); } })
    };
    let j = match prep {
        None => return,
        Some(Err(e)) => {
            let _ = set(vec![("status", Sql::Text("error".into())), ("error", Sql::Text(e)), ("finished_at", Sql::Text(crate::orm::now_stamp())), ("checkpoint_json", Sql::Text(String::new()))]).await;
            return;
        }
        Some(Ok(j)) => j,
    };
    let failed = |t: &str| t.starts_with("[AI ") || t.starts_with("[empty response");
    let starved = |t: &str| t.starts_with("[empty response") && t.contains("hidden \"thinking\"");
    let plans: Vec<(bool, bool)> = if j.think { vec![(true, false), (true, true), (false, true)] } else { vec![(false, false), (false, true)] };
    let (mut recap, mut last) = (String::new(), (j.think, false));
    for (i, (t, x)) in plans.iter().enumerate() {
        last = (*t, *x);
        recap = condense(&j.b, &j.text, &j.model, *t, &j.instructions, j.min_t, j.max_t, &j.lore, *x, &j.strictness).await;
        if !starved(&recap) || i == plans.len() - 1 {
            break;
        }
        let (nt, nx) = plans[i + 1];
        let _ = if nt != *t {
            set(vec![("think", Sql::Integer(0)), ("think_fallback", Sql::Integer(1)), ("expanded_thinking", Sql::Integer(nx as i64))]).await
        } else {
            set(vec![("expanded_thinking", Sql::Integer(nx as i64))]).await
        };
    }
    if j.strictness == "strict" && (j.min_t.is_some_and(|v| v != 0) || j.max_t.is_some_and(|v| v != 0)) && !failed(&recap) && !starved(&recap) {
        let cpt = chars_per_token(&recap);
        let est = (recap.chars().count() as i64 + cpt - 1) / cpt;
        let below = j.min_t.is_some_and(|m| m != 0 && (est as f64) < m as f64 * 0.85);
        let above = j.max_t.is_some_and(|m| m != 0 && (est as f64) > m as f64 * 1.15);
        if below || above {
            let note = if below {
                format!("Your previous draft was ~{est} tokens; the requirement is at least ~{} tokens. Expand it with more specific detail from the recap.", j.min_t.unwrap_or(0))
            } else {
                format!("Your previous draft was ~{est} tokens; the requirement is at most ~{} tokens. Trim it to fit.", j.max_t.unwrap_or(0))
            };
            let combined = [crate::edits::py_strip_pub(&j.instructions), crate::edits::py_strip_pub(&note)].into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n\n");
            let retry = condense(&j.b, &j.text, &j.model, last.0, &combined, j.min_t, j.max_t, &j.lore, last.1, &j.strictness).await;
            if !failed(&retry) && !starved(&retry) {
                recap = retry;
            }
        }
    }
    let now = Sql::Text(crate::orm::now_stamp());
    let fields = if failed(&recap) {
        vec![("status", Sql::Text("error".into())), ("error", Sql::Text(recap)), ("finished_at", now)]
    } else {
        let (rejected, via_token) = thinking_rejected(&j.model);
        let mut f = vec![("status", Sql::Text("done".into())), ("recap", Sql::Text(recap)), ("finished_at", now)];
        if j.think && rejected {
            f.push(("think_rejected", Sql::Integer(1)));
            if via_token { f.push(("think_token_fallback", Sql::Integer(1))) } else { f.push(("think", Sql::Integer(0))) }
        }
        f
    };
    let _ = set(fields).await;
}

// ── parsing facts out of a recap (POST /api/facts/parse-job; the job: audio_jobs purpose="facts_parse") ──

fn env_i64(k: &str, d: i64) -> i64 {
    std::env::var(k).ok().and_then(|v| v.parse::<i64>().ok()).unwrap_or(d)
}

fn max_auto_ctx() -> i64 {
    env_i64("MAX_AUTO_NUM_CTX", 32768).max(8192)
}

/// `_facts_parse_chunk_plan`: (chunk characters, reserved tokens)
fn facts_chunk_plan(raw: &str, think: bool, lore_tokens: i64, system: &str) -> (usize, i64) {
    let headroom = env_i64("THINKING_HEADROOM_TOKENS", 4096).max(0);
    let reserve_for = |extra: i64| 1200 + (system.chars().count() as i64) / chars_per_token(system) + if think { headroom } else { 0 } + extra.max(0);
    let mut reserve = reserve_for(lore_tokens + 512);
    if reserve >= max_auto_ctx() {
        reserve = reserve_for(512);
    }
    let target = env_i64("FACTS_PARSE_TARGET_INPUT_TOKENS", if think { 3072 } else { 4096 }).max(1024);
    let input = target.min(max_auto_ctx() - reserve).max(1024);
    ((input * chars_per_token(raw)) as usize, reserve)
}

/// `_split_transcript_into_chunks`: pieces of at most `size` characters, broken at a paragraph, a line or a sentence end past halfway
fn split_chunks(text: &str, size: usize) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= size {
        return vec![text.to_string()];
    }
    let rfind = |w: &[char], pat: &str| -> Option<usize> {
        let p: Vec<char> = pat.chars().collect();
        (0..=w.len().saturating_sub(p.len())).rev().find(|i| w.len() >= p.len() && w[*i..*i + p.len()] == p[..])
    };
    let (mut out, mut pos, n, min_break) = (vec![], 0usize, chars.len(), (size / 2) as i64);
    while pos < n {
        let mut end = (pos + size).min(n);
        if end < n {
            let w = &chars[pos..end];
            let mut brk: i64 = rfind(w, "\n\n").map_or(-1, |i| i as i64);
            if brk < min_break {
                if let Some(i) = rfind(w, "\n").map(|i| i as i64) {
                    if i > brk { brk = i; }
                }
            }
            if brk < min_break {
                for sep in [". ", "! ", "? "] {
                    if let Some(i) = rfind(w, sep).map(|i| i as i64) {
                        if i > brk { brk = i + sep.chars().count() as i64 - 1; }
                    }
                }
            }
            if brk >= min_break {
                end = pos + brk as usize + 1;
            }
        }
        let chunk = crate::edits::py_strip_pub(&chars[pos..end].iter().collect::<String>());
        if !chunk.is_empty() {
            out.push(chunk);
        }
        pos = end;
    }
    out
}

/// One `client.chat(..., format=schema)` call (not streamed): the reply's content, or `_parse_facts_chat_call`'s ValueError text
async fn chat_with_schema(b: &Backend, model: &str, messages: &[Value], think: bool, schema: &Value) -> Result<String, String> {
    let send = |messages: Vec<Value>, think: bool| {
        let mut opts = b.options_for(model);
        let mut body = Map::new();
        body.insert("model".into(), json!(model));
        body.insert("messages".into(), Value::Array(messages));
        body.insert("stream".into(), json!(false));
        opts.remove("num_ctx");
        body.extend(translate_options(&opts));
        body.insert("chat_template_kwargs".into(), json!({"enable_thinking": think}));
        body.insert("response_format".into(), json!({"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}));
        backend_request(b, "POST", "/v1/chat/completions", Some(&Value::Object(body)))
    };
    async fn call(req: Option<hyper::Request<http_body_util::Full<bytes::Bytes>>>) -> Result<(u16, bytes::Bytes), String> {
        let req = req.ok_or_else(|| "AI unavailable: request".to_string())?;
        let resp = client().request(req).await.map_err(|e| format!("AI unavailable: ConnectError: {e}"))?;
        let status = resp.status().as_u16();
        let bytes = resp.into_body().collect().await.map_err(|e| format!("AI unavailable: ReadError: {e}"))?.to_bytes();
        Ok((status, bytes))
    }
    let error_text = |bytes: &[u8], status: u16| -> String {
        let m = match serde_json::from_slice::<Value>(bytes) {
            Ok(Value::Object(o)) => match o.get("error") {
                Some(Value::Object(e)) => e.get("message").map(|m| match m { Value::String(s) => s.clone(), other => other.to_string() }).unwrap_or_default(),
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                _ => String::new(),
            },
            _ => String::from_utf8_lossy(bytes).chars().take(500).collect(),
        };
        if m.is_empty() { format!("HTTP {status}") } else { m }
    };
    let content = |bytes: &[u8]| -> Result<String, String> {
        let v: Value = serde_json::from_slice(bytes).map_err(|_| "AI unavailable: JSONDecodeError: the backend's answer is not JSON".to_string())?;
        Ok(v.pointer("/choices/0/message/content").and_then(|c| c.as_str()).unwrap_or("").to_string())
    };
    let (status, bytes) = call(send(messages.to_vec(), think)).await?;
    if status < 400 {
        record_thinking(model, think, false, false);
        return content(&bytes);
    }
    let message = error_text(&bytes, status);
    if !(think && thinking_rejection(status as i64, &message)) {
        return Err(format!("Ollama error {status}: {message}"));
    }
    let thinks = model == "unsloth/gemma-4-26B-A4B-it-GGUF" || crate::edits::py_truthy_pub(b.per_model.get(model).and_then(|m| m.get("thinking")));
    record_thinking(model, true, true, thinks);
    let mut retry = messages.to_vec();
    if thinks {
        match retry.first_mut() {
            Some(Value::Object(first)) if first.get("role") == Some(&json!("system")) => {
                let c = match first.get("content") { Some(Value::String(c)) => c.clone(), _ => String::new() };
                first.insert("content".into(), json!(format!("<|think|>{c}")));
            }
            _ => retry.insert(0, json!({"role": "system", "content": "<|think|>"})),
        }
    }
    let (status, bytes) = call(send(retry, false)).await?;
    if status >= 400 {
        return Err(format!("Ollama error {status}: {}", error_text(&bytes, status)));
    }
    content(&bytes)
}

/// `_normalized_fact_key`
fn fact_key(content: &str) -> String {
    crate::edits::py_strip_pub(content).to_lowercase().split_whitespace().collect::<Vec<_>>().join(" ")
        .trim_end_matches(['.', '!', '?', '…']).to_string()
}

/// `parse_facts_from_recap`: the facts of each chunk merged (a repeat dropped); Err when every chunk failed (the first failure)
async fn parse_facts(b: &Backend, raw: &str, model: &str, think: bool, lore: &str, extra: &str, progress: impl Fn(usize, usize)) -> Result<Vec<Value>, String> {
    let mut system = recap_text("_RECAP_SYSTEM");
    if !extra.is_empty() {
        system = format!("{system}\n\nAdditional instructions from the GM (follow these too): {extra}");
    }
    let lore_tokens = if lore.is_empty() { 0 } else { lore.chars().count() as i64 / chars_per_token(lore) };
    let (size, _) = facts_chunk_plan(raw, think, lore_tokens, &system);
    let chunks = split_chunks(raw, size);
    let schema = nd_render::app::data()["recap_texts"]["_RECAP_FACTS_SCHEMA"].clone();
    let (mut merged, mut seen, mut errors) = (vec![], std::collections::HashSet::new(), vec![]);
    for (i, chunk) in chunks.iter().enumerate() {
        progress(i + 1, chunks.len());
        let messages = [json!({"role": "system", "content": system}), json!({"role": "user", "content": with_world_context(chunk, lore)})];
        let reply = match chat_with_schema(b, model, &messages, think, &schema).await {
            Ok(r) => r,
            Err(e) => {
                errors.push(e);
                continue;
            }
        };
        let parsed = (|| -> Option<Vec<Value>> {
            let v: Value = serde_json::from_str(&reply).ok()?;
            let facts = v.get("facts")?.as_array()?.clone();
            let mut items = vec![];
            for f in facts {
                let tags = match f.get("tags") {
                    Some(Value::Array(t)) => t.iter().filter_map(|x| x.as_str()).map(crate::edits::py_strip_pub).filter(|x| !x.is_empty()).collect::<Vec<_>>().join(", "),
                    _ => String::new(),
                };
                let content = match f.get("content")? { Value::String(s) => s.clone(), Value::Number(n) => n.to_string(), Value::Bool(t) => if *t { "True".into() } else { "False".into() }, _ => return None };
                let visible = crate::edits::py_truthy_pub(Some(f.get("visible_to_players")?));
                items.push(json!({"content": content, "visible_to_players": visible, "tags": tags}));
            }
            Some(items)
        })();
        let Some(items) = parsed else {
            errors.push("Could not parse facts from that recap — try rephrasing it.".into());
            continue;
        };
        for item in items {
            if seen.insert(fact_key(item["content"].as_str().unwrap_or(""))) {
                merged.push(item);
            }
        }
    }
    if !errors.is_empty() && errors.len() == chunks.len() {
        return Err(errors.swap_remove(0));
    }
    Ok(merged)
}

fn facts_parse_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<i64> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    if p.user_id <= 0 || crate::pages::edit_level(&p, w, "facts") != Some(true) {
        return None;
    }
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let text = |k: &str| -> Option<String> {
        match b.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(crate::edits::py_strip_pub(s)), Some(_) => None }
    };
    let raw = text("text")?;
    if raw.is_empty() {
        return None;
    }
    let gs_id = match b.get("game_session_id") {
        v if !crate::edits::py_truthy_pub(v) => None,
        Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
        Some(Value::String(s)) if s.bytes().all(|c| c.is_ascii_digit()) => s.parse().ok(),
        _ => return None,
    };
    let use_rag = crate::edits::py_truthy_pub(b.get("use_rag")) && p.is_gm;
    let (rag_e, rag_n) = (body_int(b.get("rag_entity_limit")).ok()?, body_int(b.get("rag_notes_limit")).ok()?);
    if rag_e.is_some_and(|v| v < 0) || rag_n.is_some_and(|v| v < 0) {
        return None;
    }
    if use_rag && gs_id.is_some_and(|g| !crate::retrieval::session_picks(&db, g).1.is_empty()) {
        return None;
    }
    let (model, extra) = (text("model")?, text("extra_instructions")?);
    backend(&db)?;
    use rusqlite::types::Value as Sql;
    let opt_int = |v: Option<i64>| v.map_or(Sql::Null, Sql::Integer);
    crate::orm::insert(&db, "audio_jobs", &[("world_id", Sql::Integer(wid)), ("purpose", Sql::Text("facts_parse".into())), ("filename", Sql::Text("Facts".into())),
        ("status", Sql::Text("pending".into())), ("game_session_id", opt_int(gs_id)), ("created_by_user_id", Sql::Integer(p.user_id)),
        ("model", if model.is_empty() { Sql::Null } else { Sql::Text(model) }), ("think", Sql::Integer(crate::edits::py_truthy_pub(b.get("think")) as i64)),
        ("use_rag", Sql::Integer(use_rag as i64)), ("rag_entity_limit", opt_int(rag_e)), ("rag_notes_limit", opt_int(rag_n)),
        ("extra_instructions", if extra.is_empty() { Sql::Null } else { Sql::Text(extra) }), ("transcript", Sql::Text(raw)),
        ("audio_path", Sql::Text(String::new())), ("delete_after", Sql::Integer(0))])
}

/// `POST /api/facts/parse-job`: {job_id}, the job run here
pub async fn facts_parse_job_create(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let id = tokio::task::spawn_blocking(move || facts_parse_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    start_facts_parse_job(id);
    crate::edits::json_response(&json!({"job_id": id}))
}

fn start_facts_parse_job(id: i64) {
    crate::jobs::spawn("audio_jobs", id, async move {
        run_facts_parse_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// `_run_job` for purpose="facts_parse"
async fn run_facts_parse_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("audio job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    use rusqlite::types::Value as Sql;
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<Result<(Backend, String, bool, String, String, String), String>> {
        let db = crate::writes::open(&c)?;
        let row: (i64, Option<String>, Option<i64>, Option<String>, Option<String>, Option<i64>, Option<i64>, Option<i64>, Option<i64>) =
            db.query_row("SELECT world_id, model, think, transcript, extra_instructions, use_rag, rag_entity_limit, rag_notes_limit, game_session_id FROM audio_jobs WHERE id = ?1",
                [id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?))).ok()?;
        let (wid, model, think, text, extra, use_rag, rag_e, rag_n, gsid) = row;
        let text = text.unwrap_or_default();
        let lore = if use_rag.unwrap_or(0) != 0 && wid != 0 {
            let pins = gsid.map(|g| crate::retrieval::session_picks(&db, g).0).unwrap_or_default();
            crate::retrieval::build_rag_context(&db, wid, &text, rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT), &pins).unwrap_or_default()
        } else { String::new() };
        crate::orm::update(&db, "audio_jobs", id, &[("status", Sql::Text("summarizing".into()))]);
        let Some(b) = backend(&db) else { return Some(Err("AI unavailable: no Unsloth Studio backend is configured".into())) };
        let model = model.filter(|m| !m.is_empty()).or_else(|| surface_default(&c, "recap")).unwrap_or_default();
        let model = if model.is_empty() { b.model.clone() } else { model };
        Some(Ok((b, model, think.map_or(true, |t| t != 0), text, extra.unwrap_or_default(), lore)))
    }).await.ok().flatten();
    let write = |fields: Vec<(&'static str, Sql)>| {
        let c = cfg.clone();
        std::thread::spawn(move || { if let Some(db) = crate::writes::open(&c) { crate::orm::update(&db, "audio_jobs", id, &fields); } }).join().ok()
    };
    let end = |status: &str, field: (&'static str, String)| vec![("status", Sql::Text(status.into())), (field.0, Sql::Text(field.1)), ("chunk_current", Sql::Null),
        ("chunk_total", Sql::Null), ("finished_at", Sql::Text(crate::orm::now_stamp())), ("checkpoint_json", Sql::Text(String::new()))];
    let (b, model, think, text, extra, lore) = match prep {
        None => return,
        Some(Err(e)) => { write(end("error", ("error", e))); return; }
        Some(Ok(p)) => p,
    };
    let progress = |cur: usize, total: usize| { write(vec![("chunk_current", Sql::Integer(cur as i64)), ("chunk_total", Sql::Integer(total as i64))]); };
    match parse_facts(&b, &text, &model, think, &lore, &extra, progress).await {
        Ok(facts) => { write(end("done", ("result_json", nd_render::py_dumps(&Value::Array(facts))))); }
        Err(e) => { write(end("error", ("error", e))); }
    }
}


// ── a transcript's recap (summarize_transcript), and the live-transcript recap job (audio_jobs purpose="session_recap") ──

/// The generation options summarize_transcript sends with every part (`expanded_thinking_options()` or the Thinking widening, capped)
fn transcript_options(b: &Backend, think: bool, expanded: bool) -> Map<String, Value> {
    let mut opts = Map::new();
    if expanded {
        let configured = b.options.get("num_predict").and_then(|v| v.as_i64()).unwrap_or(0).max(0);
        let headroom = env_i64("THINKING_HEADROOM_TOKENS", 4096).max(0);
        opts.insert("num_predict".into(), json!(configured + env_i64("THINKING_EXPANDED_HEADROOM_TOKENS", 12288).max(headroom)));
        return opts;
    }
    opts.extend(b.thinking_predict_if(think));
    if !opts.contains_key("num_predict") {
        let configured = b.options.get("num_predict").and_then(|v| v.as_f64());
        if !configured.is_some_and(|c| c != 0.0 && c >= 0.0) {
            opts.insert("num_predict".into(), json!(RECAP_NUM_PREDICT_DEFAULT));
        }
    }
    opts
}

/// `summarize_transcript`: one call for a transcript that fits, else a summary per part (checkpointed after each), joined in order
async fn summarize_transcript(b: &Backend, transcript: &str, model: &str, extra: &str, think: bool, lore: &str, expanded: bool,
                              resume: Option<&Value>, progress: &(dyn Fn(usize, usize) + Sync), checkpoint: &(dyn Fn(Value) + Sync)) -> String {
    let transcript = crate::edits::py_strip_pub(transcript);
    if transcript.is_empty() {
        return String::new();
    }
    let with_instr = |base: String| if extra.is_empty() { base } else { format!("{base}\n\nAdditional instructions from the GM (follow these too): {extra}") };
    let part_system = with_world_context(&with_instr(recap_text("_SUMMARIZE_TRANSCRIPT_PART_SYSTEM")), lore);
    // _transcript_chunk_char_budget
    let ctx = max_auto_ctx().min(b.context_tokens);
    let reserved = 1200 + (part_system.chars().count() as i64) / chars_per_token(&part_system) + if think { env_i64("THINKING_HEADROOM_TOKENS", 4096).max(0) } else { 0 };
    let chunk_chars = ((ctx - reserved).max(500) * chars_per_token(&transcript)) as usize;
    let opts = transcript_options(b, think, expanded);
    let chunks = split_chunks(&transcript, chunk_chars);
    if chunks.len() <= 1 {
        let system = with_world_context(&with_instr(recap_text("_SUMMARIZE_TRANSCRIPT_SYSTEM")), lore);
        return generate_chat(b, model, &system, &[json!({"role": "user", "content": transcript})], think, &opts).await;
    }
    let (mut start, mut parts) = (0usize, vec![]);
    if let Some(r) = resume {
        if r.get("phase").and_then(|v| v.as_str()) == Some("summarize") && r.get("chunk_total").and_then(|v| v.as_u64()) == Some(chunks.len() as u64)
            && r.get("chunk_chars").and_then(|v| v.as_u64()) == Some(chunk_chars as u64) {
            start = r.get("parts_done").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
            parts.push(r.get("text").and_then(|v| v.as_str()).unwrap_or("").to_string());
        }
    }
    for i in start..chunks.len() {
        progress(i + 1, chunks.len());
        let part = generate_chat(b, model, &part_system, &[json!({"role": "user", "content": chunks[i]})], think, &opts).await;
        if part.starts_with("[AI ") || part.starts_with("[empty response") {
            return part;
        }
        let part = crate::edits::py_strip_pub(&part);
        if part.is_empty() {
            return format!("[empty response from part {} of {} — the model returned no usable text for this part]", i + 1, chunks.len());
        }
        parts.push(part);
        checkpoint(json!({"phase": "summarize", "parts_done": i + 1, "chunk_total": chunks.len(), "chunk_chars": chunk_chars, "text": parts.join("\n\n")}));
    }
    parts.join("\n\n")
}

fn text_recap_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<i64> {
    let sid: i64 = uri.path().strip_prefix("/api/sessions/")?.strip_suffix("/ai/summarize-live-transcript-job")?.parse().ok()?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let w = p.world.as_ref()?;
    let (wid, transcript): (i64, Option<String>) = db.query_row("SELECT world_id, live_transcript FROM game_sessions WHERE id = ?1", [sid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
    // the section check reads the session's own world: answered here only when that is the active one
    if p.user_id <= 0 || w.get("id")?.as_i64()? != wid || crate::pages::edit_level(&p, w, "sessions") != Some(true) {
        return None;
    }
    let transcript = transcript.unwrap_or_default();
    if crate::edits::py_strip_pub(&transcript).is_empty() {
        return None;
    }
    let b = if body.is_empty() { Map::new() } else { match serde_json::from_slice::<Value>(body).ok()? { Value::Object(m) => m, _ => return None } };
    let text = |k: &str| -> Option<String> {
        match b.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(crate::edits::py_strip_pub(s)), Some(_) => None }
    };
    let use_rag = crate::edits::py_truthy_pub(b.get("use_rag")) && p.is_gm;
    let (rag_e, rag_n) = (body_int(b.get("rag_entity_limit")).ok()?, body_int(b.get("rag_notes_limit")).ok()?);
    if rag_e.is_some_and(|v| v < 0) || rag_n.is_some_and(|v| v < 0) {
        return None;
    }
    if use_rag && !crate::retrieval::session_picks(&db, sid).1.is_empty() {
        return None;
    }
    let (model, extra) = (text("model")?, text("extra_instructions")?);
    let think = match b.get("think") { None => true, v => crate::edits::py_truthy_pub(v) };
    backend(&db)?;
    use rusqlite::types::Value as Sql;
    let opt_int = |v: Option<i64>| v.map_or(Sql::Null, Sql::Integer);
    crate::orm::insert(&db, "audio_jobs", &[("world_id", Sql::Integer(wid)), ("purpose", Sql::Text("session_recap".into())), ("filename", Sql::Text("Live Transcript".into())),
        ("status", Sql::Text("pending".into())), ("game_session_id", Sql::Integer(sid)), ("created_by_user_id", Sql::Integer(p.user_id)),
        ("model", if model.is_empty() { Sql::Null } else { Sql::Text(model) }), ("think", Sql::Integer(think as i64)),
        ("extra_instructions", if extra.is_empty() { Sql::Null } else { Sql::Text(extra) }), ("use_rag", Sql::Integer(use_rag as i64)),
        ("rag_entity_limit", opt_int(rag_e)), ("rag_notes_limit", opt_int(rag_n)), ("transcript", Sql::Text(transcript)),
        ("audio_path", Sql::Text(String::new())), ("delete_after", Sql::Integer(0))])
}

/// `POST /api/sessions/<id>/ai/summarize-live-transcript-job`: {job_id}, the job run here
pub async fn text_recap_job_create(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let id = tokio::task::spawn_blocking(move || text_recap_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    start_text_recap_job(id);
    crate::edits::json_response(&json!({"job_id": id}))
}

fn start_text_recap_job(id: i64) {
    crate::jobs::spawn("audio_jobs", id, async move {
        run_text_recap_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// `_run_job` for purpose="session_recap" with the transcript already on the row: lore, summarize_transcript under the Thinking ladder
/// (resuming from the row's checkpoint), the pending facts drafted from the transcript, the done / error row
async fn run_text_recap_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("audio job").await else { return };
    text_recap_body(id).await;
}

/// The recap half of a session_recap job (the transcript already on the row), run inside the AI slot
pub(crate) async fn text_recap_body(id: i64) {
    let Some(cfg) = crate::jobs::cfg() else { return };
    use rusqlite::types::Value as Sql;
    struct Job { b: Backend, model: String, think: bool, text: String, instructions: String, lore: String, resume: Option<Value> }
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<Result<Job, String>> {
        let db = crate::writes::open(&c)?;
        let row: (i64, Option<String>, Option<i64>, Option<String>, Option<String>, Option<i64>, Option<i64>, Option<i64>, Option<i64>, Option<String>) =
            db.query_row("SELECT world_id, model, think, transcript, extra_instructions, use_rag, rag_entity_limit, rag_notes_limit, game_session_id, checkpoint_json FROM audio_jobs WHERE id = ?1",
                [id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?))).ok()?;
        let (wid, model, think, text, extra, use_rag, rag_e, rag_n, gsid, checkpoint) = row;
        let text = text.unwrap_or_default();
        let resume = checkpoint.filter(|c| !c.is_empty()).and_then(|c| serde_json::from_str::<Value>(&c).ok()).filter(|c| c.get("phase").and_then(|p| p.as_str()) == Some("summarize"));
        let lore = if use_rag.unwrap_or(0) != 0 && wid != 0 {
            let pins = gsid.map(|g| crate::retrieval::session_picks(&db, g).0).unwrap_or_default();
            crate::retrieval::build_rag_context(&db, wid, &text, rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT), &pins).unwrap_or_default()
        } else { String::new() };
        crate::orm::update(&db, "audio_jobs", id, &[("status", Sql::Text("summarizing".into()))]);
        let world_instr: Option<String> = db.query_row("SELECT recap_instructions FROM worlds WHERE id = ?1", [wid], |r| r.get(0)).ok().flatten();
        let instructions = [crate::edits::py_strip_pub(&world_instr.unwrap_or_default()), crate::edits::py_strip_pub(&extra.unwrap_or_default())]
            .into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n\n");
        let Some(b) = backend(&db) else { return Some(Err("AI unavailable: no Unsloth Studio backend is configured".into())) };
        let model = model.filter(|m| !m.is_empty()).or_else(|| surface_default(&c, "recap")).unwrap_or_default();
        let model = if model.is_empty() { b.model.clone() } else { model };
        Some(Ok(Job { b, model, think: think.map_or(true, |t| t != 0), text, instructions, lore, resume }))
    }).await.ok().flatten();
    let write = |fields: Vec<(&'static str, Sql)>| {
        let c = cfg.clone();
        std::thread::spawn(move || { if let Some(db) = crate::writes::open(&c) { crate::orm::update(&db, "audio_jobs", id, &fields); } }).join().ok();
    };
    let j = match prep {
        None => return,
        Some(Err(e)) => {
            write(vec![("status", Sql::Text("error".into())), ("error", Sql::Text(e)), ("chunk_current", Sql::Null), ("chunk_total", Sql::Null),
                ("finished_at", Sql::Text(crate::orm::now_stamp())), ("checkpoint_json", Sql::Text(String::new()))]);
            return;
        }
        Some(Ok(j)) => j,
    };
    let starved = |t: &str| t.starts_with("[empty response") && t.contains("hidden \"thinking\"");
    let plans: Vec<(bool, bool)> = if j.think { vec![(true, false), (true, true), (false, true)] } else { vec![(false, false), (false, true)] };
    let latest: std::sync::Mutex<Option<Value>> = std::sync::Mutex::new(j.resume.clone());
    let progress = |cur: usize, total: usize| write(vec![("chunk_current", Sql::Integer(cur as i64)), ("chunk_total", Sql::Integer(total as i64))]);
    let checkpoint = |state: Value| {
        if let Ok(mut l) = latest.lock() { *l = Some(state.clone()); }
        write(vec![("checkpoint_json", Sql::Text(nd_render::py_dumps(&state)))]);
    };
    let mut recap = String::new();
    for (i, (t, x)) in plans.iter().enumerate() {
        let resume = if *t == j.think { latest.lock().ok().and_then(|l| l.clone()) } else { None };
        recap = summarize_transcript(&j.b, &j.text, &j.model, &j.instructions, *t, &j.lore, *x, resume.as_ref(), &progress, &checkpoint).await;
        if !starved(&recap) || i == plans.len() - 1 {
            break;
        }
        let (nt, nx) = plans[i + 1];
        if nt != *t {
            write(vec![("think", Sql::Integer(0)), ("think_fallback", Sql::Integer(1)), ("expanded_thinking", Sql::Integer(nx as i64))]);
        } else {
            write(vec![("expanded_thinking", Sql::Integer(nx as i64))]);
        }
    }
    let now = Sql::Text(crate::orm::now_stamp());
    if recap.starts_with("[AI ") || recap.starts_with("[empty response") {
        write(vec![("status", Sql::Text("error".into())), ("error", Sql::Text(recap)), ("chunk_current", Sql::Null), ("chunk_total", Sql::Null),
            ("finished_at", now), ("checkpoint_json", Sql::Text(String::new()))]);
        return;
    }
    // _auto_extract_pending_facts: "[]" on any failure
    let pending = match parse_facts(&j.b, &j.text, &j.model, false, "", "", |_, _| {}).await {
        Ok(f) => nd_render::py_dumps(&Value::Array(f)),
        Err(_) => "[]".into(),
    };
    let (rejected, via_token) = thinking_rejected(&j.model);
    let mut f = vec![("status", Sql::Text("done".into())), ("recap", Sql::Text(recap)), ("chunk_current", Sql::Null), ("chunk_total", Sql::Null),
        ("finished_at", now), ("checkpoint_json", Sql::Text(String::new())), ("pending_facts_json", Sql::Text(pending))];
    if j.think && rejected {
        f.push(("think_rejected", Sql::Integer(1)));
        if via_token { f.push(("think_token_fallback", Sql::Integer(1))) } else { f.push(("think", Sql::Integer(0))) }
    }
    write(f);
}

// ── session recordings: the audio recap job (POST /api/sessions/ai/audio-jobs/from-clip and /complete; audio_jobs purpose="session_recap") ──

/// `request.form()` for a form without files (urlencoded or multipart)
fn small_form(headers: &HeaderMap, body: &[u8]) -> Option<crate::form::Form> {
    match crate::form::content_type(headers).as_str() {
        "multipart/form-data" => {
            let (f, files) = crate::form::parse_multipart(headers, body)?;
            files.is_empty().then_some(f)
        }
        "application/x-www-form-urlencoded" => crate::form::Form::parse(body),
        "" if body.is_empty() => Some(crate::form::Form(vec![])),
        _ => None,
    }
}

/// A `bool = Form(default)` field as Pydantic reads it (None: a 422, Python's)
fn form_bool(f: &crate::form::Form, k: &str, default: bool) -> Option<bool> {
    match f.get(k) {
        None => Some(default),
        Some(v) => match v.trim().to_lowercase().as_str() {
            "1" | "true" | "t" | "on" | "yes" | "y" => Some(true),
            "0" | "false" | "f" | "off" | "no" | "n" => Some(false),
            _ => None,
        },
    }
}

/// The recap options every audio-job form carries: (session id, model, instructions, think, use_rag, entity limit, notes limit)
fn audio_job_fields(f: &crate::form::Form, gm: bool) -> Option<(Option<i64>, String, String, bool, bool, Option<i64>, Option<i64>)> {
    let digits = |k: &str| { let t = f.get(k).unwrap_or("").trim(); (!t.is_empty() && t.bytes().all(|b| b.is_ascii_digit())).then(|| t.parse::<i64>().ok()).flatten() };
    Some((digits("game_session_id"), f.get("model").unwrap_or("").trim().to_string(), crate::edits::py_strip_pub(f.get("extra_instructions").unwrap_or("")),
        form_bool(f, "think", true)?, form_bool(f, "use_rag", false)? && gm, digits("rag_entity_limit"), digits("rag_notes_limit")))
}

const SESSION_AUDIO_EXTS: &[&str] = &[".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"];

fn audio_job_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<i64> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    if p.user_id <= 0 || crate::pages::edit_level(&p, w, "sessions") != Some(true) {
        return None;
    }
    let f = small_form(headers, body)?;
    let (gs_id, model, extra, think, use_rag, rag_e, rag_n) = audio_job_fields(&f, p.is_gm)?;
    if use_rag && gs_id.is_some_and(|g| !crate::retrieval::session_picks(&db, g).1.is_empty()) {
        return None;
    }
    backend(&db)?;
    let uploads = cfg.db_path.parent()?.join("uploads");
    let (filename, path, delete_after) = if uri.path().ends_with("/from-clip") {
        let clip_id = crate::form::py_int(f.get("clip_id")?)?;
        let (name, url): (Option<String>, Option<String>) = db.query_row("SELECT name, file_url FROM audio_clips WHERE id = ?1 AND world_id = ?2", [clip_id, wid],
            |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
        // _audio_clip_disk_path: a file under uploads/ (else Python's 404)
        let rel = url.as_deref()?.strip_prefix("/uploads/")?;
        let root = uploads.canonicalize().ok()?;
        let path = root.join(rel).canonicalize().ok().filter(|p| p.starts_with(&root) && p.is_file())?;
        let fname = path.file_name()?.to_string_lossy().to_string();
        (name.filter(|n| !n.is_empty()).unwrap_or(fname), path, false)
    } else {
        // /complete: the parts /chunk saved, reassembled into the jobs folder
        let (upload_id, filename, total) = (f.get("upload_id")?, f.get("filename")?.to_string(), crate::form::py_int(f.get("total_chunks")?)?);
        let ext = std::path::Path::new(&filename).extension().map(|e| format!(".{}", e.to_string_lossy().to_lowercase())).unwrap_or_default();
        if !SESSION_AUDIO_EXTS.contains(&ext.as_str()) || upload_id.is_empty() || !upload_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            || upload_id.len() > 64 || !(1..=2001).contains(&total) {
            return None;
        }
        let dir = uploads.join("session_audio").join("_chunks").join(upload_id);
        let parts: Vec<std::path::PathBuf> = (0..total).map(|i| dir.join(format!("{i:06}.part"))).collect();
        let mut size = 0u64;
        for part in &parts {
            size += std::fs::metadata(part).ok().filter(|m| m.is_file())?.len();
        }
        let cap = std::env::var("MAX_SESSION_AUDIO_BYTES").ok().or_else(|| std::env::var("MAX_AUDIO_UPLOAD_BYTES").ok()).and_then(|v| v.parse::<u64>().ok()).unwrap_or(3 * 1024 * 1024 * 1024);
        if size > cap {
            return None;
        }
        let jobs = uploads.join("session_audio").join("_jobs");
        std::fs::create_dir_all(&jobs).ok()?;
        let dest = jobs.join(format!("{:032x}{ext}", rand::random::<u128>()));
        let mut out = std::fs::File::create(&dest).ok()?;
        for part in &parts {
            if std::io::copy(&mut std::fs::File::open(part).ok()?, &mut out).is_err() {
                let _ = std::fs::remove_file(&dest);
                return None;
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
        (filename, dest, true)
    };
    use rusqlite::types::Value as Sql;
    let opt_int = |v: Option<i64>| v.map_or(Sql::Null, Sql::Integer);
    crate::orm::insert(&db, "audio_jobs", &[("world_id", Sql::Integer(wid)), ("purpose", Sql::Text("session_recap".into())), ("filename", Sql::Text(filename)),
        ("game_session_id", opt_int(gs_id)), ("created_by_user_id", Sql::Integer(p.user_id)), ("attachment_url", Sql::Text(String::new())),
        ("status", Sql::Text("pending".into())), ("model", if model.is_empty() { Sql::Null } else { Sql::Text(model) }),
        ("extra_instructions", if extra.is_empty() { Sql::Null } else { Sql::Text(extra) }), ("think", Sql::Integer(think as i64)),
        ("use_rag", Sql::Integer(use_rag as i64)), ("rag_entity_limit", opt_int(rag_e)), ("rag_notes_limit", opt_int(rag_n)),
        ("audio_path", Sql::Text(path.to_string_lossy().to_string())), ("delete_after", Sql::Integer(delete_after as i64))])
}

/// `POST /api/sessions/ai/audio-jobs/from-clip` and `/complete`: {job_id}, the recording transcribed and recapped here
pub async fn audio_job_create(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let id = tokio::task::spawn_blocking(move || audio_job_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    start_audio_job(id);
    crate::edits::json_response(&json!({"job_id": id}))
}

fn start_audio_job(id: i64) {
    crate::jobs::spawn("audio_jobs", id, async move {
        run_audio_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// `_run_job` for a session_recap with a recording: the transcribe phase (checkpointed, resumed), then the recap; the recording deleted
/// afterwards when the row says so
async fn run_audio_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("audio job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    use rusqlite::types::Value as Sql;
    let c = cfg.clone();
    let row = tokio::task::spawn_blocking(move || {
        let db = crate::writes::open(&c)?;
        db.query_row("SELECT audio_path, delete_after, transcript, checkpoint_json FROM audio_jobs WHERE id = ?1", [id],
            |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<i64>>(1)?, r.get::<_, Option<String>>(2)?, r.get::<_, Option<String>>(3)?))).ok()
    }).await.ok().flatten();
    let Some((audio, delete_after, transcript, checkpoint)) = row else { return };
    let audio = audio.filter(|a| !a.is_empty()).map(std::path::PathBuf::from);
    let checkpoint = checkpoint.filter(|c| !c.is_empty()).and_then(|c| serde_json::from_str::<Value>(&c).ok());
    let in_transcribe = checkpoint.as_ref().and_then(|c| c.get("phase")).and_then(|p| p.as_str()) == Some("transcribe");
    let write = |fields: Vec<(&'static str, Sql)>| {
        let c = cfg.clone();
        std::thread::spawn(move || { if let Some(db) = crate::writes::open(&c) { crate::orm::update(&db, "audio_jobs", id, &fields); } }).join().ok();
    };
    let remove_audio = || if delete_after.unwrap_or(0) != 0 { if let Some(a) = &audio { let _ = std::fs::remove_file(a); } };
    if transcript.as_deref().unwrap_or("").is_empty() || in_transcribe {
        write(vec![("status", Sql::Text("transcribing".into())), ("run_started_at", Sql::Text(crate::orm::now_stamp())), ("finished_at", Sql::Null)]);
        let c = cfg.clone();
        let b = tokio::task::spawn_blocking(move || crate::writes::open(&c).and_then(|db| backend(&db))).await.ok().flatten();
        let Some(b) = b else {
            write(vec![("status", Sql::Text("error".into())), ("error", Sql::Text("STT backend is set to Unsloth but no UNSLOTH_API_KEY is configured (Settings → System).".into())),
                ("finished_at", Sql::Text(crate::orm::now_stamp())), ("checkpoint_json", Sql::Text(String::new()))]);
            remove_audio();
            return;
        };
        let model = crate::stt::stt_model(&cfg);
        let path = audio.clone().unwrap_or_default();
        let progress = |cur: usize, total: usize| write(vec![("chunk_current", Sql::Integer(cur as i64)), ("chunk_total", Sql::Integer(total as i64))]);
        let save = |state: Value| {
            let text = state.get("text").and_then(|t| t.as_str()).unwrap_or("").to_string();
            write(vec![("checkpoint_json", Sql::Text(nd_render::py_dumps(&state))), ("transcript", Sql::Text(text))]);
        };
        let resume = checkpoint.as_ref().filter(|_| in_transcribe);
        match crate::stt::transcribe(&b, &path, &model, resume, &progress, &save).await {
            Err(e) => {
                let mut f = vec![("status", Sql::Text("error".into())), ("error", Sql::Text(e.message)), ("finished_at", Sql::Text(crate::orm::now_stamp())),
                    ("checkpoint_json", Sql::Text(String::new()))];
                if !e.partial.is_empty() {
                    f.push(("transcript", Sql::Text(e.partial)));
                }
                write(f);
                remove_audio();
                return;
            }
            Ok(t) if t.is_empty() => {
                write(vec![("status", Sql::Text("error".into())), ("finished_at", Sql::Text(crate::orm::now_stamp())), ("checkpoint_json", Sql::Text(String::new())),
                    ("error", Sql::Text("Speech-to-text finished this clip but found no speech in it — check the recording actually captured audio.".into()))]);
                remove_audio();
                return;
            }
            Ok(t) => write(vec![("transcript", Sql::Text(t)), ("chunk_current", Sql::Null), ("chunk_total", Sql::Null), ("checkpoint_json", Sql::Text(String::new()))]),
        }
    }
    text_recap_body(id).await;
    remove_audio();
}

// ── image generation jobs on Unsloth Studio (POST /api/ai/imagegen/jobs, app/image_jobs.py; SwarmUI / ComfyUI stay Python's) ──

/// ImagegenBody's fields in `_imagegen_params`' order: (name, kind, default) - kind s(tring) i(nt) f(loat) b(ool)
const IMAGEGEN_FIELDS: &[(&str, char, &str)] = &[
    ("prompt", 's', ""), ("negative", 's', ""), ("model", 's', ""), ("width", 'i', "512"), ("height", 'i', "512"), ("steps", 'i', "20"), ("cfg", 'f', "7.0"),
    ("seed", 'i', "-1"), ("uploads_dir", 'u', ""), ("sampler", 's', "euler"), ("scheduler", 's', "normal"), ("batch_size", 'i', "1"), ("loras", 's', ""),
    ("lora_weights", 's', ""), ("vae", 's', ""), ("clip_skip", 'i', "-1"), ("init_image", 's', ""), ("init_strength", 'f', "0.6"), ("upscale_model", 's', ""),
    ("upscale_factor", 'f', "1.0"), ("controlnet_image", 's', ""), ("controlnet_strength", 'f', "0.8"), ("controlnet_preprocessor", 's', ""),
    ("controlnet_model", 's', ""), ("hiresfix", 'b', "false"), ("hireswidth", 'i', "0"), ("hiresheight", 'i', "0"), ("hiresdenoisestrength", 'f', "0.5"),
    ("hiressteps", 'i', "0"), ("refiner_model", 's', ""), ("refiner_control", 'f', "0.8"), ("seamless_x", 'b', "false"), ("seamless_y", 'b', "false"),
    ("variation_seed", 'i', "-1"), ("variation_strength", 'f', "0.0"), ("freeu_enabled", 'b', "false"), ("freeu_b1", 'f', "1.3"), ("freeu_b2", 'f', "1.4"),
    ("freeu_s1", 'f', "0.9"), ("freeu_s2", 'f', "0.2"), ("dynthresh_enabled", 'b', "false"), ("dynthresh_mimic_scale", 'f', "7.0"),
    ("dynthresh_percentile", 'f', "0.999"), ("cfg_rescale", 'f', "0.0"), ("ipadapter_image", 's', ""), ("ipadapter_strength", 'f', "0.6"), ("ipadapter_model", 's', ""),
];

/// `UNSLOTH_IMAGE_MODEL`: set (with a Studio key) = image generation runs on Studio (`_get_type() == "unsloth"`)
fn unsloth_image_model() -> String {
    env("UNSLOTH_IMAGE_MODEL")
}

/// `_imagegen_params(body, uploads)`: every imagegen_generate argument in order (None: a body Pydantic would reject or coerce)
fn imagegen_params(cfg: &Config, body: &[u8]) -> Option<Map<String, Value>> {
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let mut out = Map::new();
    for (k, kind, d) in IMAGEGEN_FIELDS {
        let v = b.get(*k);
        let val = match (kind, v) {
            ('u', _) => json!(cfg.db_path.parent()?.join("uploads").to_string_lossy()),
            ('s', None) => json!(d),
            ('s', Some(Value::String(s))) => json!(s),
            ('i', None) => json!(d.parse::<i64>().ok()?),
            ('i', Some(Value::Number(n))) if n.is_i64() => json!(n.as_i64()?),
            ('f', None) => json!(d.parse::<f64>().ok()?),
            ('f', Some(Value::Number(n))) => json!(n.as_f64()?),
            ('b', None) => json!(*d == "true"),
            ('b', Some(Value::Bool(t))) => json!(t),
            _ => return None,
        };
        out.insert(k.to_string(), val);
    }
    if out["model"].as_str() == Some("") {
        out.insert("model".into(), json!(surface_default(cfg, "image")?));
    }
    Some(out)
}

fn imagegen_job_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<i64> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if !p.is_gm || unsloth_image_model().is_empty() {
        return None;
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    backend(&db)?;
    let params = imagegen_params(cfg, body)?;
    use rusqlite::types::Value as Sql;
    crate::orm::insert(&db, "image_jobs", &[("world_id", Sql::Integer(wid)), ("prompt", Sql::Text(params["prompt"].as_str()?.to_string())),
        ("params_json", Sql::Text(nd_render::py_dumps(&Value::Object(params)))), ("created_by_user_id", Sql::Integer(p.user_id)),
        ("status", Sql::Text("pending".into()))])
}

/// `POST /api/ai/imagegen/jobs` on Studio: {job_id}, the job run here
pub async fn imagegen_job_create(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let id = tokio::task::spawn_blocking(move || imagegen_job_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    start_image_job(id);
    crate::edits::json_response(&json!({"job_id": id}))
}

fn start_image_job(id: i64) {
    crate::jobs::spawn("image_jobs", id, async move {
        run_image_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// A Studio API call (`unsloth_extras._request`): Ok(json), Err(Some((status, message))) for an error, Err(None) for a missing route
async fn studio(b: &Backend, method: &str, path: &str, body: Option<&Value>) -> Result<Value, Option<(u16, String)>> {
    let req = backend_request(b, method, path, body).ok_or(Some((503, "Unsloth Studio unreachable: request".into())))?;
    let resp = client().request(req).await.map_err(|e| Some((503, format!("Unsloth Studio unreachable: ConnectError: {e}"))))?;
    let status = resp.status().as_u16();
    let bytes = resp.into_body().collect().await.map_err(|e| Some((503, format!("Unsloth Studio unreachable: ReadError: {e}"))))?.to_bytes();
    if status == 404 {
        let missing = match serde_json::from_slice::<Value>(&bytes) {
            Ok(Value::Object(o)) if !o.is_empty() => {
                let m = crate::stt::studio_message_pub(&bytes, status).trim().to_lowercase();
                let m = m.trim_end_matches('.');
                m == "not found" || m.contains("endpoint not found")
            }
            _ => true,
        };
        if missing {
            return Err(None);
        }
    }
    if status >= 400 {
        return Err(Some((status, crate::stt::studio_message_pub(&bytes, status))));
    }
    Ok(serde_json::from_slice::<Value>(&bytes).unwrap_or(json!({})))
}

/// `resolve_gguf_filename(repo)` from Studio's own variant list (the Hugging Face listing fallback is left out: "" then)
async fn gguf_filename(b: &Backend, repo: &str) -> String {
    // the query as httpx writes it: unreserved characters kept, a space as "+"
    const QUERY: &percent_encoding::AsciiSet = &percent_encoding::NON_ALPHANUMERIC.remove(b'-').remove(b'.').remove(b'_').remove(b'~');
    let q = repo.split(' ').map(|p| percent_encoding::utf8_percent_encode(p, QUERY).to_string()).collect::<Vec<_>>().join("+");
    let info = studio(b, "GET", &format!("/api/hub/gguf-variants?repo_id={q}"), None).await.unwrap_or(json!({}));
    let file = |e: &Value| -> String {
        match e {
            Value::String(s) if s.to_lowercase().ends_with(".gguf") => s.clone(),
            Value::Object(o) => ["filename", "file", "gguf_filename", "path", "name"].iter().find_map(|k| o.get(*k).and_then(|v| v.as_str()).filter(|v| v.to_lowercase().ends_with(".gguf")).map(str::to_string)).unwrap_or_default(),
            _ => String::new(),
        }
    };
    let quant = |e: &Value| -> String {
        match e {
            Value::String(s) if !s.to_lowercase().ends_with(".gguf") => s.clone(),
            Value::Object(o) => ["quant", "variant", "label", "id", "name"].iter().find_map(|k| o.get(*k).and_then(|v| v.as_str()).filter(|v| !v.is_empty() && !v.to_lowercase().ends_with(".gguf")).map(str::to_string)).unwrap_or_default(),
            _ => String::new(),
        }
    };
    let default = info.get("default_variant").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let entries = info.get("variants").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let cands: Vec<(String, String, bool)> = entries.iter().map(|e| (file(e), quant(e), e.get("downloaded").is_some_and(|d| crate::edits::py_truthy_pub(Some(d))))).collect();
    let on_disk: Vec<String> = cands.iter().filter(|c| c.2).map(|c| if c.1.is_empty() { c.0.clone() } else { c.1.clone() }).collect();
    let mut prefer = vec![];
    if !default.is_empty() && on_disk.contains(&default) { prefer.push(default.clone()); }
    prefer.extend(on_disk);
    if !default.is_empty() { prefer.push(default); }
    let named: Vec<&(String, String, bool)> = cands.iter().filter(|c| !c.0.is_empty()).collect();
    if named.is_empty() {
        return String::new();
    }
    for label in &prefer {
        for c in &named {
            if label.to_lowercase() == c.1.to_lowercase() || label.to_lowercase() == c.0.to_lowercase() {
                return c.0.clone();
            }
        }
    }
    const PREF: &[&str] = &["Q4_K_M", "Q4_K_S", "Q4_0", "Q5_K_M", "Q5_K_S", "Q6_K", "Q8_0", "BF16", "F16"];
    let rank = |t: &str| { let up = t.to_uppercase(); PREF.iter().position(|q| up.contains(q)).unwrap_or(PREF.len()) };
    named.iter().min_by_key(|c| rank(if c.1.is_empty() { &c.0 } else { &c.1 })).map(|c| c.0.clone()).unwrap_or_default()
}

/// `ensure_image_model(repo)`: load it and wait until Studio says it is loaded; Ok(false) when this Studio has no load endpoint
async fn ensure_image_model(b: &Backend, repo: &str) -> Result<bool, String> {
    let file = gguf_filename(b, repo).await;
    let mut body = json!({"model_path": repo, "model_kind": "gguf"});
    if !file.is_empty() {
        body["gguf_filename"] = json!(file);
    }
    match studio(b, "POST", "/api/inference/images/load", Some(&body)).await {
        Err(None) => return Ok(false),
        Err(Some((_, m))) => {
            let hint = if file.is_empty() && m.to_lowercase().contains("gguf") { " — nd-world could not work out which .gguf file of this repo to load (Studio named none and the repo's file list was unavailable); load it once from the Models tab, which asks for the file" } else { "" };
            return Err(format!("Could not load {repo}: {m}{hint}"));
        }
        Ok(_) => {}
    }
    let limit = env("UNSLOTH_IMAGE_LOAD_TIMEOUT_SECONDS").parse::<f64>().unwrap_or(900.0);
    let deadline = Instant::now() + Duration::from_secs_f64(limit);
    loop {
        match studio(b, "GET", "/api/inference/images/status", None).await {
            Err(e) => return Err(format!("Could not check whether {repo} finished loading: {}", e.map(|e| e.1).unwrap_or_else(|| "/api/inference/images/status".into()))),
            Ok(s) => {
                if crate::edits::py_truthy_pub(s.get("loaded")) {
                    return Ok(true);
                }
                if let Some(e) = s.get("error").filter(|e| crate::edits::py_truthy_pub(Some(e))) {
                    return Err(format!("Could not load {repo}: {}", e.as_str().map(str::to_string).unwrap_or_else(|| e.to_string())));
                }
            }
        }
        if Instant::now() >= deadline {
            return Err(format!("{repo} is still loading after {} s — Studio may still be downloading it; try again in a few minutes", limit as i64));
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

/// Save a generated picture under uploads/ai-images with its thumbnail; its url
fn save_ai_image(cfg: &Config, bytes: &[u8]) -> Option<String> {
    let dir = cfg.db_path.parent()?.join("uploads").join("ai-images");
    std::fs::create_dir_all(&dir).ok()?;
    let name = format!("{}.png", uuid_v4());
    let path = dir.join(&name);
    std::fs::write(&path, bytes).ok()?;
    if let Some(db) = crate::writes::open(cfg) {
        let preview: Option<String> = db.query_row("SELECT preview_format FROM app_settings LIMIT 1", [], |r| r.get(0)).ok().flatten();
        let preview = preview.filter(|p| matches!(p.as_str(), "avif" | "webp" | "original")).unwrap_or_else(|| "avif".into());
        let _ = crate::imaging::finish_upload(&path, "none", &preview); // best effort, like make_thumbnail
    }
    Some(format!("/uploads/ai-images/{name}"))
}

fn uuid_v4() -> String {
    let b: [u8; 16] = rand::random();
    let h = |r: std::ops::Range<usize>| b[r].iter().map(|x| format!("{x:02x}")).collect::<String>();
    let (v, var) = (format!("4{}", &h(6..8)[1..]), format!("{:x}{}", 8 | (b[8] >> 6), &h(8..10)[1..]));
    format!("{}-{}-{}-{}-{}", h(0..4), h(4..6), v, var, h(10..16))
}

/// `imagegen_generate` on Studio: the native endpoint (the /v1 one on an older Studio), the chosen model loaded once when none is
async fn imagegen_studio(cfg: &Config, b: &Backend, p: &Map<String, Value>) -> Result<Vec<String>, String> {
    let s = |k: &str| p.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let i = |k: &str| p.get(k).and_then(|v| v.as_i64()).unwrap_or(0);
    let f = |k: &str| p.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
    let native_model = { let m = s("model"); if m.is_empty() { unsloth_image_model() } else { m } };
    let mut body = json!({"model": native_model, "prompt": s("prompt"), "width": i("width"), "height": i("height"),
        "steps": if i("steps") > 0 { i("steps") } else { 9 }, "batch_size": i("batch_size").clamp(1, 8)});
    if !s("negative").is_empty() { body["negative_prompt"] = json!(s("negative")); }
    if f("cfg") > 0.0 { body["guidance"] = json!(f("cfg")); }
    if i("seed") >= 0 { body["seed"] = json!(i("seed")); }
    let uploads = cfg.db_path.parent().map(|d| d.join("uploads")).unwrap_or_default();
    if !s("init_image").is_empty() {
        let init = s("init_image");
        let rel = init.split_once("/uploads/").map(|x| x.1.to_string()).unwrap_or(init.clone());
        let cand = uploads.join(rel);
        if let (Ok(c), Ok(root)) = (cand.canonicalize(), uploads.canonicalize()) {
            if c.starts_with(&root) && c.is_file() {
                use base64::Engine;
                body["init_image"] = json!(format!("data:image/png;base64,{}", base64::engine::general_purpose::STANDARD.encode(std::fs::read(&c).unwrap_or_default())));
                body["strength"] = json!(f("init_strength"));
            }
        }
    }
    let mut autoloaded = false;
    let records = loop {
        match studio(b, "POST", "/api/inference/images/generate", Some(&body)).await {
            Ok(v) => break Some(v.get("images").and_then(|i| i.as_array()).cloned().unwrap_or_default()),
            Err(None) => break None,
            Err(Some((status, m))) => {
                let refused = m.contains("No diffusion model is loaded") || status == 409;
                if refused && !native_model.is_empty() && !autoloaded {
                    autoloaded = true;
                    if ensure_image_model(b, &native_model).await.map_err(|e| format!("Unsloth Studio: {e}"))? {
                        continue;
                    }
                    autoloaded = false;
                }
                let hint = if refused && autoloaded { format!(" — Studio still has no image model after loading {native_model}; check the Models tab") }
                    else if refused { " — enable Studio's media auto-switch (Settings → API, or Settings → System → Studio server here) or load the image model once from the Models tab".into() }
                    else { String::new() };
                return Err(format!("Unsloth Studio: {m}{hint}"));
            }
        }
    };
    let mut urls = vec![];
    match records {
        Some(records) => {
            for rec in records {
                let url = match &rec { Value::String(s) => s.clone(), Value::Object(o) => o.get("url").and_then(|u| u.as_str()).unwrap_or("").to_string(), _ => String::new() };
                if url.is_empty() {
                    return Err("Gallery record has no file url".into());
                }
                let path = if url.starts_with("http") { url.splitn(4, '/').nth(3).map(|p| format!("/{p}")).unwrap_or_default() } else { url };
                let req = backend_request(b, "GET", &path, None).ok_or("request")?;
                let resp = client().request(req).await.map_err(|e| format!("Unsloth Studio unreachable: ConnectError: {e}"))?;
                let status = resp.status().as_u16();
                if status >= 400 {
                    return Err(format!("Fetching generated image failed: HTTP {status}"));
                }
                let bytes = resp.into_body().collect().await.map_err(|e| format!("Unsloth Studio unreachable: ReadError: {e}"))?.to_bytes();
                urls.push(save_ai_image(cfg, &bytes).ok_or("could not save the picture")?);
            }
        }
        None => {
            // an older Studio: /v1/images/generations (fewer settings; no negative prompt)
            let model = { let m = s("model"); if m.is_empty() { unsloth_image_model() } else { m } };
            let mut v1 = json!({"model": model, "prompt": s("prompt"), "size": format!("{}x{}", i("width"), i("height")), "n": i("batch_size").clamp(1, 8), "response_format": "b64_json"});
            if i("seed") >= 0 { v1["seed"] = json!(i("seed")); }
            let mut loaded = false;
            let data = loop {
                let req = backend_request(b, "POST", "/v1/images/generations", Some(&v1)).ok_or("request")?;
                let resp = client().request(req).await.map_err(|e| format!("ConnectError: {e}"))?;
                let status = resp.status().as_u16();
                let bytes = resp.into_body().collect().await.map_err(|e| format!("ReadError: {e}"))?.to_bytes();
                if status >= 400 {
                    let detail = crate::stt::studio_message_pub(&bytes, status);
                    let refused = status == 503 && detail.contains("No image model loaded");
                    if refused && !model.is_empty() && !loaded {
                        loaded = true;
                        if ensure_image_model(b, &model).await.map_err(|e| format!("Unsloth Studio: {e}"))? {
                            continue;
                        }
                        loaded = false;
                    }
                    let hint = if refused && loaded { format!(" — Studio still has no image model after loading {model}; check the Models tab") }
                        else if refused { " — enable Studio's media auto-switch (Settings → API) or load the image model once".into() } else { String::new() };
                    return Err(format!("Unsloth returned HTTP {status}: {detail}{hint}"));
                }
                break serde_json::from_slice::<Value>(&bytes).map_err(|_| format!("Unsloth returned an unreadable response: {}", String::from_utf8_lossy(&bytes).chars().take(300).collect::<String>()))?;
            };
            let images = data.get("data").and_then(|d| d.as_array()).cloned().unwrap_or_default();
            if images.is_empty() {
                return Err(format!("Unsloth returned no image: {}", data.to_string().chars().take(300).collect::<String>()));
            }
            use base64::Engine;
            for e in images {
                let b64 = e.get("b64_json").and_then(|v| v.as_str()).filter(|v| !v.is_empty()).ok_or("Unsloth returned an image entry without b64_json data")?;
                let raw = base64::engine::general_purpose::STANDARD.decode(b64).map_err(|e| format!("Error: {e}"))?;
                urls.push(save_ai_image(cfg, &raw).ok_or("could not save the picture")?);
            }
        }
    }
    Ok(urls)
}

/// image_jobs' `_run_job`: the AI slot, `generating`, the pictures; `done` with result_urls_json, or `error`
async fn run_image_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("image job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    use rusqlite::types::Value as Sql;
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<(Option<Backend>, Map<String, Value>)> {
        let db = crate::writes::open(&c)?;
        let params: Option<String> = db.query_row("SELECT params_json FROM image_jobs WHERE id = ?1", [id], |r| r.get(0)).ok()?;
        crate::orm::update(&db, "image_jobs", id, &[("status", Sql::Text("generating".into()))]);
        Some((backend(&db), serde_json::from_str::<Value>(params.as_deref().unwrap_or("{}")).ok()?.as_object()?.clone()))
    }).await.ok().flatten();
    let Some((b, params)) = prep else { return };
    let result = match b {
        Some(b) if !unsloth_image_model().is_empty() => imagegen_studio(&cfg, &b, &params).await,
        _ => Err("Image generation is not configured — set IMAGEGEN_TYPE/IMAGEGEN_URL (see docker-compose.yml).".into()),
    };
    let fields = match result {
        Ok(urls) => vec![("status", Sql::Text("done".into())), ("result_urls_json", Sql::Text(nd_render::py_dumps(&json!(urls))))],
        Err(e) => vec![("status", Sql::Text("error".into())), ("error", Sql::Text(e))],
    };
    let _ = tokio::task::spawn_blocking(move || { if let Some(db) = crate::writes::open(&cfg) { crate::orm::update(&db, "image_jobs", id, &fields); } }).await;
}

/// image_jobs' `resume_interrupted_jobs` for one row: restarted from its stored parameters (corrupt JSON: an error row instead)
pub fn restart_image_job(db: &rusqlite::Connection, id: i64) -> Option<()> {
    use rusqlite::types::Value as Sql;
    let (params, resumed): (Option<String>, i64) = db.query_row("SELECT params_json, COALESCE(resumed_count, 0) FROM image_jobs WHERE id = ?1", [id], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
    if serde_json::from_str::<Value>(params.as_deref().filter(|p| !p.is_empty()).unwrap_or("{}")).is_err() {
        crate::orm::update(db, "image_jobs", id, &[("status", Sql::Text("error".into())),
            ("error", Sql::Text("Stored job data was corrupt (unreadable JSON) and could not be resumed.".into()))]);
        return None;
    }
    crate::orm::update(db, "image_jobs", id, &[("status", Sql::Text("pending".into())), ("error", Sql::Text(String::new())), ("resumed_count", Sql::Integer(resumed + 1))])?;
    start_image_job(id);
    Some(())
}

/// `POST /api/ai/imagegen/jobs/<id>/cancel` for an image job running here
pub async fn image_job_cancel(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap) -> Option<Response<Body>> {
    let id: i64 = uri.path().strip_prefix("/api/ai/imagegen/jobs/")?.strip_suffix("/cancel")?.parse().ok()?;
    if !crate::jobs::is_running("image_jobs", id) {
        return None;
    }
    tokio::task::spawn_blocking(move || -> Option<Response<Body>> {
        let db = crate::writes::open(&cfg)?;
        let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        let p = crate::layout::build(&db, &cfg, "POST", &uri, &headers, now)?;
        if !p.is_gm {
            return None;
        }
        let wid = p.world.as_ref()?.get("id")?.as_i64()?;
        let status: String = db.query_row("SELECT status FROM image_jobs WHERE id = ?1 AND world_id = ?2", rusqlite::params![id, wid],
            |r| Ok(r.get::<_, Option<String>>(0)?.unwrap_or_default())).ok()?;
        if !matches!(status.as_str(), "pending" | "generating") || !crate::jobs::abort("image_jobs", id) {
            return None;
        }
        use rusqlite::types::Value as Sql;
        crate::orm::update(&db, "image_jobs", id, &[("status", Sql::Text("cancelled".into())), ("error", Sql::Text("Cancelled by GM.".into()))])?;
        crate::edits::json_response(&json!({"ok": true}))
    }).await.ok()?
}

// ── AI video generation on Unsloth Studio (POST /api/video-jobs/start; app/video_jobs.py) ──

/// `str(x or "").strip()` for a body field: None when Python's str() of it is not something this port writes the same way
fn py_str_or_blank(v: Option<&Value>) -> Option<String> {
    match v {
        v if !crate::edits::py_truthy_pub(v) => Some(String::new()),
        Some(Value::String(s)) => Some(crate::edits::py_strip_pub(s)),
        Some(Value::Number(n)) if n.is_i64() => Some(n.to_string()),
        _ => None,
    }
}

/// `_video_job_to_dict`
fn video_job_dict(db: &rusqlite::Connection, id: i64) -> Option<Value> {
    let (prompt, status, error, vid, clip, created): (Option<String>, Option<String>, Option<String>, Option<String>, Option<i64>, Option<String>) =
        db.query_row("SELECT prompt, status, error, studio_video_id, clip_id, created_at FROM video_jobs WHERE id = ?1", [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?))).ok()?;
    let iso = |t: String| { let t = t.replacen(' ', "T", 1); t.strip_suffix(".000000").map(str::to_string).unwrap_or(t) };
    let mut out = json!({"id": id, "prompt": prompt.unwrap_or_default(), "status": status.filter(|s| !s.is_empty()).unwrap_or_else(|| "pending".into()),
        "error": error.unwrap_or_default(), "studio_video_id": vid.unwrap_or_default(), "clip_id": clip, "created_at": created.map(iso)});
    if let Some(c) = clip {
        if let Ok((url, name)) = db.query_row("SELECT file_url, name FROM video_clips WHERE id = ?1", [c], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?))) {
            out["clip_url"] = json!(url);
            out["clip_name"] = json!(name);
        }
    }
    Some(out)
}

fn video_job_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<(i64, Value)> {
    let db = crate::writes::open(cfg)?;
    let (p, wid) = assist_caller(&db, cfg, uri, headers)?;
    if crate::pages::edit_level(&p, p.world.as_ref()?, "video") != Some(true) {
        return None;
    }
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let prompt = py_str_or_blank(b.get("prompt"))?;
    if prompt.is_empty() || prompt.chars().count() > 4000 {
        return None; // Python's 400s
    }
    backend(&db)?;
    let seconds = b.get("seconds").cloned().unwrap_or(Value::Null);
    if matches!(seconds, Value::Number(ref n) if !n.is_i64()) {
        return None; // a decimal: json.dumps' spelling of it is Python's
    }
    let mut params = Map::new();
    params.insert("prompt".into(), json!(prompt));
    params.insert("model".into(), json!(py_str_or_blank(b.get("model"))?));
    params.insert("seconds".into(), seconds);
    params.insert("size".into(), json!(py_str_or_blank(b.get("size"))?));
    params.insert("name".into(), json!(py_str_or_blank(b.get("name"))?));
    use rusqlite::types::Value as Sql;
    let id = crate::orm::insert(&db, "video_jobs", &[("world_id", Sql::Integer(wid)), ("prompt", Sql::Text(prompt)),
        ("params_json", Sql::Text(nd_render::py_dumps(&Value::Object(params)))), ("created_by_user_id", Sql::Integer(p.user_id)),
        ("status", Sql::Text("pending".into()))])?;
    Some((id, video_job_dict(&db, id)?))
}

/// `POST /api/video-jobs/start`: {id, job}, the generation run here
pub async fn video_job_create(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let (id, job) = tokio::task::spawn_blocking(move || video_job_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    start_video_job(id);
    crate::edits::json_response(&json!({"id": id, "job": job}))
}

fn start_video_job(id: i64) {
    crate::jobs::spawn("video_jobs", id, async move {
        run_video_job(id).await;
        crate::jobs::Ended::Final
    });
}

/// video_jobs' resume for one row: back to pending, the poll re-attached to the Studio generation
pub fn restart_video_job(db: &rusqlite::Connection, id: i64) -> Option<()> {
    use rusqlite::types::Value as Sql;
    let resumed: i64 = db.query_row("SELECT COALESCE(resumed_count, 0) FROM video_jobs WHERE id = ?1", [id], |r| r.get(0)).ok()?;
    crate::orm::update(db, "video_jobs", id, &[("resumed_count", Sql::Integer(resumed + 1)), ("status", Sql::Text("pending".into()))])?;
    start_video_job(id);
    Some(())
}

/// `_extract_video_id`
fn video_id_of(v: &Value) -> String {
    let id = |x: Option<&Value>| match x { Some(Value::String(s)) => Some(s.clone()), Some(Value::Number(n)) if n.is_i64() => Some(n.to_string()), _ => None };
    let Value::Object(o) = v else { return String::new() };
    for k in ["id", "video_id", "job_id"] {
        if let Some(s) = id(o.get(k)) { return s; }
    }
    for w in ["data", "video", "job"] {
        match o.get(w) {
            Some(Value::Object(inner)) => {
                let v = inner.get("id").filter(|v| crate::edits::py_truthy_pub(Some(v))).or_else(|| inner.get("video_id"));
                if let Some(s) = id(v) { return s; }
            }
            Some(Value::Array(a)) => {
                if let Some(s) = a.first().and_then(|f| f.as_object()).and_then(|f| id(f.get("id"))) { return s; }
            }
            _ => {}
        }
    }
    String::new()
}

/// `_entry_state`: failed / done / running from an entry's status words or its content url
fn video_state(e: &Map<String, Value>) -> &'static str {
    let tokens_match = |v: &str, marks: &[&str]| v.split(|c: char| c.is_whitespace() || c == '_' || c == '-').filter(|t| !t.is_empty()).any(|t| marks.iter().any(|m| t.starts_with(m)));
    for k in ["status", "state", "phase"] {
        let v = match e.get(k) { Some(Value::String(s)) => s.to_lowercase(), Some(v) if crate::edits::py_truthy_pub(Some(v)) => v.to_string().to_lowercase(), _ => String::new() };
        if v.is_empty() { continue; }
        if tokens_match(&v, &["fail", "error", "cancel"]) { return "failed"; }
        if tokens_match(&v, &["succeed", "done", "complete", "ready"]) { return "done"; }
        return "running";
    }
    if ["content_url", "video_url", "output_url"].iter().any(|k| crate::edits::py_truthy_pub(e.get(*k))) { "done" } else { "running" }
}

/// video_jobs' `_run_job`: the generation (started, or the one the row remembers), polled until done, the file kept as a clip
async fn run_video_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("video job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    use rusqlite::types::Value as Sql;
    let set = |fields: Vec<(&'static str, Sql)>| {
        let c = cfg.clone();
        std::thread::spawn(move || { if let Some(db) = crate::writes::open(&c) { crate::orm::update(&db, "video_jobs", id, &fields); } }).join().ok();
    };
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<(Option<Backend>, String, Map<String, Value>, Option<String>, i64)> {
        let db = crate::writes::open(&c)?;
        let (vid, params, prompt, wid): (Option<String>, Option<String>, Option<String>, i64) = db.query_row(
            "SELECT studio_video_id, params_json, prompt, world_id FROM video_jobs WHERE id = ?1", [id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).ok()?;
        let params = serde_json::from_str::<Value>(params.as_deref().unwrap_or("{}")).ok().and_then(|v| v.as_object().cloned()).unwrap_or_default();
        Some((backend(&db), vid.unwrap_or_default(), params, prompt, wid))
    }).await.ok().flatten();
    let Some((b, mut vid, params, prompt, wid)) = prep else { return };
    let fail = |m: String| set(vec![("status", Sql::Text("error".into())), ("error", Sql::Text(m))]);
    let Some(b) = b else { return fail("No Unsloth backend configured — set UNSLOTH_API_KEY (Settings → System)".into()) };
    set(vec![("status", Sql::Text("generating".into()))]);
    if vid.is_empty() {
        let mut body = json!({"prompt": params.get("prompt").and_then(|v| v.as_str()).unwrap_or("")});
        for k in ["model", "seconds", "size"] {
            if let Some(v) = params.get(k).filter(|v| crate::edits::py_truthy_pub(Some(v))) { body[k] = v.clone(); }
        }
        match studio(&b, "POST", "/v1/videos", Some(&body)).await {
            Ok(r) => vid = video_id_of(&r),
            Err(e) => return fail(e.map(|e| e.1).unwrap_or_else(|| "/v1/videos".into())),
        }
        if vid.is_empty() {
            return fail("Studio accepted the video request but returned no generation id — its /v1/videos response shape isn't one nd-world recognizes (see docs/UNSLOTH_PHASE0_FINDINGS.md); update nd-world once verified.".into());
        }
        set(vec![("studio_video_id", Sql::Text(vid.clone()))]);
    }
    // _poll_until_done
    let max_wait = env("ND_VIDEO_JOB_MAX_SECONDS").parse::<f64>().unwrap_or(4.0 * 3600.0);
    let deadline = Instant::now() + Duration::from_secs_f64(max_wait);
    let mut errors = 0;
    loop {
        let listing = match studio(&b, "GET", "/v1/videos", None).await {
            Ok(l) => { errors = 0; Some(l) }
            Err(e) => {
                errors += 1;
                if errors >= 30 { return fail(e.map(|e| e.1).unwrap_or_else(|| "/v1/videos".into())); }
                None
            }
        };
        let entries = listing.as_ref().map(|l| if l.is_object() { l.get("data").cloned().unwrap_or(Value::Null) } else { l.clone() });
        let entry = entries.as_ref().and_then(|e| e.as_array()).and_then(|a| a.iter().filter_map(|e| e.as_object())
            .find(|e| match e.get("id") { Some(Value::String(s)) => *s == vid, Some(Value::Number(n)) => n.to_string() == vid, _ => false }).cloned());
        if let Some(e) = entry {
            match video_state(&e) {
                "failed" => {
                    let m = e.get("error").filter(|v| crate::edits::py_truthy_pub(Some(v))).or_else(|| e.get("status").filter(|v| crate::edits::py_truthy_pub(Some(v))))
                        .map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())).unwrap_or_else(|| "generation failed".into());
                    return fail(format!("Studio video generation failed: {m}"));
                }
                "done" => break,
                _ => {}
            }
        }
        if Instant::now() >= deadline {
            return fail(format!("Video generation did not finish within {:.1} h (ND_VIDEO_JOB_MAX_SECONDS) — it may still complete on the Studio side; try again with a shorter clip or raise the limit.", max_wait / 3600.0));
        }
        tokio::time::sleep(Duration::from_secs(10)).await;
    }
    set(vec![("status", Sql::Text("fetching".into()))]);
    let Some(req) = backend_request(&b, "GET", &format!("/v1/videos/{vid}/content"), None) else { return };
    let resp = match client().request(req).await {
        Ok(r) => r,
        Err(e) => return fail(format!("Unsloth Studio unreachable: ConnectError: {e}")),
    };
    let status = resp.status().as_u16();
    let ctype = resp.headers().get("content-type").and_then(|v| v.to_str().ok()).unwrap_or("video/mp4").to_lowercase();
    let bytes = match resp.into_body().collect().await { Ok(c) => c.to_bytes(), Err(e) => return fail(format!("Unsloth Studio unreachable: ReadError: {e}")) };
    if status >= 400 {
        return fail(crate::stt::studio_message_pub(&bytes, status));
    }
    if bytes.is_empty() {
        return fail("Studio returned an empty video file".into());
    }
    let ext = if ctype.contains("webm") { ".webm" } else if ctype.contains("quicktime") { ".mov" } else { ".mp4" };
    let c = cfg.clone();
    let done = tokio::task::spawn_blocking(move || -> Option<()> {
        let dir = c.db_path.parent()?.join("uploads").join("video");
        std::fs::create_dir_all(&dir).ok()?;
        let fname = format!("ai-video-{:08x}{ext}", rand::random::<u32>());
        std::fs::write(dir.join(&fname), &bytes).ok()?;
        let db = crate::writes::open(&c)?;
        let given: String = params.get("name").and_then(|v| v.as_str()).map(|n| crate::edits::py_strip_pub(n).chars().take(250).collect()).unwrap_or_default();
        let prompt = prompt.unwrap_or_default();
        let name = if given.is_empty() { (if prompt.is_empty() { "AI video".to_string() } else { prompt.clone() }).chars().take(60).collect() } else { given };
        let clip = crate::orm::insert(&db, "video_clips", &[("world_id", Sql::Integer(wid)), ("name", Sql::Text(name)),
            ("description", Sql::Text(format!("Generated by Unsloth Studio — {prompt}").chars().take(500).collect())),
            ("file_url", Sql::Text(format!("/uploads/video/{fname}"))), ("visible_to_players", Sql::Integer(0))])?;
        crate::orm::update(&db, "video_jobs", id, &[("status", Sql::Text("done".into())), ("clip_id", Sql::Integer(clip))])?;
        Some(())
    }).await.ok().flatten();
    if done.is_none() {
        fail("could not keep the generated video".into());
    }
}

/// `POST /api/video-jobs/<id>/cancel` for a video job running here
pub async fn video_job_cancel(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap) -> Option<Response<Body>> {
    let id: i64 = uri.path().strip_prefix("/api/video-jobs/")?.strip_suffix("/cancel")?.parse().ok()?;
    if !crate::jobs::is_running("video_jobs", id) {
        return None;
    }
    tokio::task::spawn_blocking(move || -> Option<Response<Body>> {
        let db = crate::writes::open(&cfg)?;
        let (_, wid) = assist_caller(&db, &cfg, &uri, &headers)?;
        let status: String = db.query_row("SELECT status FROM video_jobs WHERE id = ?1 AND world_id = ?2", rusqlite::params![id, wid],
            |r| Ok(r.get::<_, Option<String>>(0)?.unwrap_or_default())).ok()?;
        if !matches!(status.as_str(), "pending" | "generating" | "fetching") || !crate::jobs::abort("video_jobs", id) {
            return None;
        }
        use rusqlite::types::Value as Sql;
        crate::orm::update(&db, "video_jobs", id, &[("status", Sql::Text("cancelled".into())), ("error", Sql::Text("Cancelled by GM.".into()))])?;
        crate::edits::json_response(&json!({"ok": true}))
    }).await.ok()?
}

// ── the blocking recap / facts routes (app/routers/sessions.py, app/routers/facts.py): answered here, inline or as a background task ──

/// What one of these routes will do once planned (permissions and body read): its answer comes from `answer_sync`
enum SyncAi {
    Expand { b: Backend, model: String, notes: String, think: bool, instr: String },
    Condense { b: Backend, model: String, recap: String, think: bool, extra: String, min_t: Option<i64>, max_t: Option<i64>, strictness: String },
    /// `echo`: with recap_failed (summarize-transcript), else the live route's {transcript, recap}
    Transcript { b: Backend, model: String, transcript: String, think: bool, instr: String, echo: bool },
    FromFacts { b: Backend, model: String, facts: Vec<String>, think: bool, instr: String },
    Facts { b: Backend, model: String, text: String, think: bool, lore: String, extra: String },
    FolkTale(Assist),
    Compact { b: Backend, model: String, history: String, think: bool },
    /// `generate(prompt)`: the default system prompt, one user message
    Generate { b: Backend, prompt: String },
    /// `parse_entity_from_text` (one draft) or `parse_entities_batch_from_text` (many)
    EntityText { b: Backend, text: String, hint: String, batch: bool },
    Chat { b: Backend, model: String, system: String, messages: Vec<Value>, options: Map<String, Value> },
    Refuse(StatusCode, String),
}

fn sync_ai_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<(SyncAi, i64)> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if p.user_id <= 0 {
        return None;
    }
    let path = uri.path();
    let b = if body.is_empty() { Map::new() } else { match serde_json::from_slice::<Value>(body).ok()? { Value::Object(m) => m, _ => return None } };
    let text = |k: &str| -> Option<String> {
        match b.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(crate::edits::py_strip_pub(s)), Some(_) => None }
    };
    let think_default_true = match b.get("think") { None => true, v => crate::edits::py_truthy_pub(v) };
    let world_instr = |w: &Map<String, Value>| crate::edits::py_strip_pub(w.get("recap_instructions").and_then(|v| v.as_str()).unwrap_or(""));
    let combine = |a: &str, z: &str| [crate::edits::py_strip_pub(a), crate::edits::py_strip_pub(z)].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join("\n\n");
    let section_ok = |w: &Map<String, Value>, s: &str| crate::pages::edit_level(&p, w, s) == Some(true);
    let recap_model = || surface_default(cfg, "recap");
    let refuse = |code: StatusCode, m: &str| Some((SyncAi::Refuse(code, m.to_string()), p.user_id));
    let plan = match path {
        "/api/sessions/ai/expand-notes" | "/api/sessions/ai/condense-recap" | "/api/sessions/ai/summarize-transcript" => {
            let w = p.world.as_ref()?;
            if !section_ok(w, "sessions") {
                return None;
            }
            let bk = backend(&db)?;
            match path {
                "/api/sessions/ai/expand-notes" => {
                    let notes = text("notes")?;
                    if notes.is_empty() { return refuse(StatusCode::BAD_REQUEST, "No notes provided"); }
                    SyncAi::Expand { b: bk, model: recap_model()?, notes, think: think_default_true, instr: world_instr(w) }
                }
                "/api/sessions/ai/condense-recap" => {
                    let recap = text("recap")?;
                    if recap.is_empty() { return refuse(StatusCode::BAD_REQUEST, "No recap provided"); }
                    let (min_t, max_t) = (body_int(b.get("min_tokens")).ok()?, body_int(b.get("max_tokens")).ok()?);
                    if min_t.is_some_and(|v| v < 1) || max_t.is_some_and(|v| v < 1) || matches!((min_t, max_t), (Some(a), Some(z)) if a > z) {
                        return None;
                    }
                    let strictness = match b.get("strictness") {
                        None | Some(Value::Null) => "guideline".to_string(),
                        Some(Value::String(s)) if s.is_empty() => "guideline".to_string(),
                        Some(Value::String(s)) => crate::edits::py_strip_pub(s),
                        Some(_) => return None,
                    };
                    if !["guideline", "firm", "strict"].contains(&strictness.as_str()) { return None; }
                    let extra = text("extra_instructions")?;
                    let all = format!("{recap}{extra}");
                    let cpt = chars_per_token(&all);
                    if (all.chars().count() as i64 + cpt - 1) / cpt > max_auto_ctx() - 512 { return None; }
                    let model = { let m = text("model")?; if m.is_empty() { recap_model()? } else { m } };
                    SyncAi::Condense { b: bk, model, recap, think: think_default_true, extra, min_t, max_t, strictness }
                }
                _ => {
                    let transcript = text("transcript")?;
                    if transcript.is_empty() { return refuse(StatusCode::BAD_REQUEST, "No transcript provided"); }
                    let instr = combine(&world_instr(w), &text("extra_instructions")?);
                    SyncAi::Transcript { b: bk, model: recap_model()?, transcript, think: think_default_true, instr, echo: true }
                }
            }
        }
        _ if path.starts_with("/api/sessions/") => {
            let sid: i64 = path.strip_prefix("/api/sessions/")?.split('/').next()?.parse().ok()?;
            let (wid, live): (i64, Option<String>) = db.query_row("SELECT world_id, live_transcript FROM game_sessions WHERE id = ?1", [sid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
            let w = p.world.as_ref()?;
            if w.get("id")?.as_i64()? != wid {
                return None; // the section check reads the session's own world: Python's
            }
            if path.ends_with("/ai/summarize-live-transcript") {
                let live = live.unwrap_or_default();
                if crate::edits::py_strip_pub(&live).is_empty() { return None; }
                if !section_ok(w, "sessions") { return None; }
                let bk = backend(&db)?;
                SyncAi::Transcript { b: bk, model: recap_model()?, transcript: live, think: think_default_true, instr: world_instr(w), echo: false }
            } else {
                let mut st = db.prepare("SELECT content FROM facts WHERE game_session_id = ?1 ORDER BY created_at").ok()?;
                let facts: Vec<String> = st.query_map([sid], |r| r.get::<_, Option<String>>(0)).ok()?.filter_map(Result::ok).map(|f| f.unwrap_or_default()).collect();
                if facts.is_empty() || !section_ok(w, "sessions") { return None; }
                let bk = backend(&db)?;
                SyncAi::FromFacts { b: bk, model: recap_model()?, facts, think: think_default_true, instr: world_instr(w) }
            }
        }
        "/api/ai/generate/entity" | "/api/ai/generate/npc" | "/api/ai/generate/location" | "/api/ai/generate/quest" => {
            if !p.is_gm {
                return None; // the routes check nothing themselves: a GM's here, the rest Python's
            }
            let req = |k: &str| match b.get(k) { Some(Value::String(s)) => Some(s.clone()), _ => None };
            let opt = |k: &str| text_raw(&b, k);
            let prompt = match path.rsplit('/').next()? {
                "entity" => format!("Write an expanded description for this {} named '{}'. Existing notes: {}", req("type")?, req("name")?, opt("description")?),
                "npc" => format!("Create a backstory and personality for an NPC named '{}' who is a {}. Notes: {}", req("name")?, req("role")?, opt("notes")?),
                "location" => format!("Describe the location '{}' in the region '{}'. Notes: {}", req("name")?, req("region")?, opt("notes")?),
                _ => format!("Create a quest hook for '{}'. World context: {}", req("title")?, opt("context")?),
            };
            SyncAi::Generate { b: backend(&db)?, prompt }
        }
        "/api/ai/entity-from-text" | "/api/ai/entities-from-text-batch" => {
            if !(p.is_gm || p.role.as_deref() == Some("assistant")) || p.world.is_none() {
                return None;
            }
            let Some(Value::String(t)) = b.get("text") else { return None };
            let text = crate::edits::py_strip_pub(t);
            if text.is_empty() {
                return refuse(StatusCode::BAD_REQUEST, "No text provided");
            }
            let batch = path.ends_with("-batch");
            let hint = if batch { String::new() } else {
                let k = crate::edits::py_strip_pub(&text_raw(&b, "kind")?);
                if entity_kinds().contains(&k) { k } else { String::new() }
            };
            SyncAi::EntityText { b: backend(&db)?, text, hint, batch }
        }
        "/api/ai/generate/entity-smart" => {
            if !(p.is_gm || p.role.as_deref() == Some("assistant")) {
                return None;
            }
            let req = |k: &str| match b.get(k) { Some(Value::String(s)) => Some(s.clone()), _ => None };
            let (name, kind, subtype, summary) = (req("name")?, req("kind")?, text_raw(&b, "subtype")?, text_raw(&b, "summary")?);
            let related = match p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()) {
                Some(wid) => {
                    let found = crate::retrieval::find_relevant_entities(&db, wid, &format!("{name} {summary}"), 12, (p.user_id, p.is_gm))?;
                    crate::retrieval::format_context(&db, &found, !p.is_gm, "")?
                }
                None => String::new(),
            };
            let mut prompt = format!("Write an expanded lore entry for this {kind}");
            if !subtype.is_empty() { prompt.push_str(&format!(" ({subtype})")); }
            prompt.push_str(&format!(" named '{name}'."));
            if !summary.is_empty() { prompt.push_str(&format!(" Summary: {summary}.")); }
            if !related.is_empty() { prompt.push_str(&format!("\n\nRelated world lore for context:\n{related}")); }
            SyncAi::Generate { b: backend(&db)?, prompt }
        }
        "/api/ai/chat" | "/api/ai/chat/compact" => {
            let compact = path.ends_with("/compact");
            if !p.is_gm {
                // /chat: a GM's (the route itself checks nothing); /compact: the AI-chat access rule
                let w = p.world.as_ref()?;
                let on = |k: &str| crate::edits::py_truthy_pub(w.get(k));
                if !compact || !(on("players_can_ask_ai") || on("players_can_use_ai_chat")) {
                    return None;
                }
            }
            let model = text_raw(&b, "model")?;
            let system = text_raw(&b, "system")?;
            text_raw(&b, "surface")?;
            let think = match b.get("think") { None => false, Some(Value::Bool(t)) => *t, Some(_) => return None };
            let options = match b.get("options") { None => Map::new(), Some(o @ Value::Object(_)) => clamp_options(o)?, Some(_) => return None };
            let messages = build_messages(b.get("messages")?)?;
            let bk = backend(&db)?;
            let model = if model.is_empty() { bk.model.clone() } else { model };
            if !compact {
                SyncAi::Chat { b: bk, model, system, messages, options }
            } else {
                if messages.is_empty() { return refuse(StatusCode::BAD_REQUEST, "No messages to compact"); }
                let all: String = messages.iter().map(|m| m["content"].as_str().unwrap_or("")).collect();
                let cpt = chars_per_token(&all);
                let tokens = (all.chars().count() as i64 + cpt - 1) / cpt;
                let ceiling = max_auto_ctx() - 512;
                if tokens > ceiling {
                    return refuse(StatusCode::BAD_REQUEST, &format!("Too much conversation to compact in one call (≈{tokens} tokens > the {ceiling}-token ceiling) — try compacting more often, or clear the chat instead"));
                }
                // _chat_history_to_text
                let history = messages.iter().filter_map(|m| {
                    let c = crate::edits::py_strip_pub(m["content"].as_str().unwrap_or(""));
                    (!c.is_empty()).then(|| format!("{}: {c}", if m["role"] == "user" { "GM" } else { "Assistant" }))
                }).collect::<Vec<_>>().join("\n\n");
                SyncAi::Compact { b: bk, model, history, think }
            }
        }
        "/api/facts/parse" => {
            let raw = text("text")?;
            if raw.is_empty() || raw.chars().count() > 200_000 { return None; }
            if let Some(w) = p.world.as_ref() {
                if !section_ok(w, "facts") { return None; }
            }
            let use_rag = crate::edits::py_truthy_pub(b.get("use_rag")) && p.is_gm;
            let (rag_e, rag_n) = (body_int(b.get("rag_entity_limit")).ok()?, body_int(b.get("rag_notes_limit")).ok()?);
            if rag_e.is_some_and(|v| v < 0) || rag_n.is_some_and(|v| v < 0) { return None; }
            let lore = if use_rag {
                let wid = p.world.as_ref()?.get("id")?.as_i64()?;
                crate::retrieval::build_rag_context(&db, wid, &raw, rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT), &[])?
            } else { String::new() };
            let model = { let m = text("model")?; if m.is_empty() { recap_model()? } else { m } };
            let bk = backend(&db)?;
            SyncAi::Facts { b: bk, model, text: raw, think: crate::edits::py_truthy_pub(b.get("think")), lore, extra: text("extra_instructions")? }
        }
        "/api/facts/folk-tale" => {
            let w = p.world.as_ref()?;
            if !section_ok(w, "facts") { return None; }
            let wid = w.get("id")?.as_i64()?;
            let gs = match b.get("game_session_id") {
                v if !crate::edits::py_truthy_pub(v) => None,
                Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
                Some(Value::String(s)) if s.bytes().all(|c| c.is_ascii_digit()) => s.parse().ok(),
                _ => return None,
            };
            let sql = format!("SELECT content FROM facts WHERE world_id = ?1{} ORDER BY created_at", if gs.is_some() { " AND game_session_id = ?2" } else { "" });
            let mut st = db.prepare(&sql).ok()?;
            let rows: Vec<Option<String>> = match gs {
                Some(g) => st.query_map(rusqlite::params![wid, g], |r| r.get(0)).ok()?.filter_map(Result::ok).collect(),
                None => st.query_map([wid], |r| r.get(0)).ok()?.filter_map(Result::ok).collect(),
            };
            if rows.is_empty() {
                return refuse(StatusCode::BAD_REQUEST, &format!("No facts logged yet{}.", if gs.is_some() { " for that session" } else { "" }));
            }
            let content = rows.into_iter().map(|f| format!("- {}", f.unwrap_or_else(|| "None".into()))).collect::<Vec<_>>().join("\n");
            let instruction = text("instruction")?;
            let model = { let m = text("model")?; if m.is_empty() { surface_default(cfg, "assist")? } else { m } };
            let think = crate::edits::py_truthy_pub(b.get("think"));
            let use_rag = crate::edits::py_truthy_pub(b.get("use_rag")) && p.is_gm;
            let (rag_e, rag_n) = (body_int(b.get("rag_entity_limit")).ok()?, body_int(b.get("rag_notes_limit")).ok()?);
            if rag_e.is_some_and(|v| v < 0) || rag_n.is_some_and(|v| v < 0) { return None; }
            let lore = if use_rag {
                crate::retrieval::build_rag_context(&db, wid, &content, rag_e.unwrap_or(DEFAULT_RAG_ENTITY_LIMIT), rag_n.unwrap_or(DEFAULT_RAG_NOTES_LIMIT), &[])?
            } else { String::new() };
            let bk = backend(&db)?;
            let model = if model.is_empty() { bk.model.clone() } else { model };
            match assist_prompt("folk_tale", &content, "", &instruction, "") {
                Ok((system, user)) => {
                    let extra = assist_options(&bk, think);
                    SyncAi::FolkTale(Assist { backend: bk, op: "folk_tale".into(), model, system: with_world_context(&system, &lore), user, think, extra })
                }
                Err(Some(m)) => SyncAi::Refuse(StatusCode::BAD_REQUEST, m),
                Err(None) => return None,
            }
        }
        _ => return None,
    };
    Some((plan, p.user_id))
}

async fn answer_sync(plan: SyncAi) -> Response<Body> {
    let failed = |t: &str| t.starts_with("[AI ") || t.starts_with("[empty response");
    let ok = |v: Value| crate::edits::json_response(&v).unwrap_or_else(|| Response::new(Body::empty()));
    let err = |code: StatusCode, m: String| Response::builder().status(code).header(header::CONTENT_TYPE, "application/json").header("x-nd-served-by", "gateway")
        .body(Body::from(nd_render::py_dumps_compact(&json!({"detail": m})))).unwrap();
    let model_or = |b: &Backend, m: &str| if m.is_empty() { b.model.clone() } else { m.to_string() };
    match plan {
        SyncAi::Refuse(code, m) => err(code, m),
        SyncAi::Expand { b, model, notes, think, instr } => {
            let mut system = recap_text("_EXPAND_NOTES_SYSTEM");
            if !instr.is_empty() { system = format!("{system}\n\nAdditional instructions from the GM (follow these too): {instr}"); }
            let opts = assist_options(&b, think);
            let recap = generate_chat(&b, &model_or(&b, &model), &system, &[json!({"role": "user", "content": notes})], think, &opts).await;
            ok(json!({"recap": recap}))
        }
        SyncAi::Condense { b, model, recap, think, extra, min_t, max_t, strictness } => {
            let out = condense(&b, &recap, &model_or(&b, &model), think, &extra, min_t, max_t, "", false, &strictness).await;
            ok(json!({"recap": out}))
        }
        SyncAi::Transcript { b, model, transcript, think, instr, echo } => {
            let recap = summarize_transcript(&b, &transcript, &model_or(&b, &model), &instr, think, "", false, None, &|_, _| {}, &|_| {}).await;
            let f = failed(&recap);
            // summarize-transcript adds recap_failed; the live-transcript route answers the two fields alone
            if echo { ok(json!({"transcript": transcript, "recap": recap, "recap_failed": f})) } else { ok(json!({"transcript": transcript, "recap": recap})) }
        }
        SyncAi::FromFacts { b, model, facts, think, instr } => {
            let recap = summarize_facts(&b, &facts, &model_or(&b, &model), &instr, think, None, None, "guideline", "").await;
            let f = failed(&recap);
            ok(json!({"recap": recap, "recap_failed": f}))
        }
        SyncAi::Facts { b, model, text, think, lore, extra } => match parse_facts(&b, &text, &model_or(&b, &model), think, &lore, &extra, |_, _| {}).await {
            Ok(facts) => ok(json!({"facts": facts})),
            Err(e) => err(StatusCode::BAD_GATEWAY, e),
        },
        SyncAi::Compact { b, model, history, think } => {
            let summary = if history.is_empty() { String::new() } else {
                generate_chat(&b, &model, &recap_text("_COMPACT_CHAT_SYSTEM"), &[json!({"role": "user", "content": history})], think, &assist_options(&b, think)).await
            };
            if failed(&summary) { err(StatusCode::BAD_GATEWAY, format!("Compaction failed: {summary}")) } else { ok(json!({"summary": summary})) }
        }
        SyncAi::EntityText { b, text, hint, batch } => match entity_from_text(&b, &text, &hint, batch).await {
            Ok(v) => ok(v),
            Err(e) => err(StatusCode::BAD_GATEWAY, e),
        },
        SyncAi::Generate { b, prompt } => {
            let result = generate_chat(&b, &b.model, &recap_text("_SYSTEM"), &[json!({"role": "user", "content": prompt})], false, &Map::new()).await;
            ok(json!({"result": result}))
        }
        SyncAi::Chat { b, model, system, messages, options } => {
            let result = generate_chat(&b, &model, &system, &messages, false, &options).await;
            ok(json!({"result": result}))
        }
        SyncAi::FolkTale(a) => {
            let text = clean_degenerate(&generate_chat(&a.backend, &a.model, &a.system, &[json!({"role": "user", "content": a.user})], a.think, &a.extra).await);
            if failed(&text) { err(StatusCode::BAD_GATEWAY, text) } else { ok(json!({"op": a.op, "mode": "text", "text": text, "model": a.model})) }
        }
    }
}

/// The blocking recap / facts routes: the answer (or a background task's 202), None for what stays Python's
pub async fn sync_ai(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let background = crate::tasks::wants_background(&headers);
    let (path, h) = (uri.path().to_string(), headers.clone());
    let (plan, uid) = tokio::task::spawn_blocking(move || sync_ai_plan(&cfg, &uri, &headers, &body)).await.ok()??;
    if background {
        return Some(crate::tasks::start(uid, &path, &h, answer_sync(plan)));
    }
    Some(answer_sync(plan).await)
}

/// The paths `sync_ai` serves
pub fn is_sync_ai(path: &str) -> bool {
    matches!(path, "/api/sessions/ai/expand-notes" | "/api/sessions/ai/condense-recap" | "/api/sessions/ai/summarize-transcript" | "/api/facts/parse" | "/api/facts/folk-tale"
        | "/api/ai/chat" | "/api/ai/chat/compact" | "/api/ai/generate/entity" | "/api/ai/generate/npc" | "/api/ai/generate/location" | "/api/ai/generate/quest"
        | "/api/ai/generate/entity-smart" | "/api/ai/entity-from-text" | "/api/ai/entities-from-text-batch")
        || (path.starts_with("/api/sessions/") && (path.ends_with("/ai/summarize-live-transcript") || path.ends_with("/ai/summarize-from-facts"))
            && path.split('/').nth(3).is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())))
}

/// A `str` field of a Pydantic body read as given ("" when absent; None when not a string)
fn text_raw(b: &Map<String, Value>, k: &str) -> Option<String> {
    match b.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(s.clone()), Some(_) => None }
}


/// app/constants.py KINDS
fn entity_kinds() -> Vec<String> {
    nd_render::app::data()["entity_kinds"].as_array().map(|a| a.iter().filter_map(|k| k.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

/// `str(x or "").strip()` for a field of a model's JSON reply
fn reply_str(v: Option<&Value>) -> String {
    match v {
        v if !crate::edits::py_truthy_pub(v) => String::new(),
        Some(Value::String(s)) => crate::edits::py_strip_pub(s),
        Some(Value::Bool(t)) => if *t { "True".into() } else { "False".into() },
        Some(other) => py_repr(other),
        None => String::new(),
    }
}

/// `str(x)` of a parsed JSON value as Python prints it (lists and dicts as their repr)
fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(t) => if *t { "True".into() } else { "False".into() },
        Value::Number(n) => n.to_string(),
        Value::String(s) => {
            let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
            let mut out = String::from(q);
            for c in s.chars() {
                match c {
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    c if c == q => { out.push('\\'); out.push(c); }
                    c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
                    c => out.push(c),
                }
            }
            out.push(q);
            out
        }
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!("{{{}}}", o.iter().map(|(k, v)| format!("{}: {}", py_repr(&Value::String(k.clone())), py_repr(v))).collect::<Vec<_>>().join(", ")),
    }
}

/// `int(x)` for a field of a model's JSON reply (Err: Python's ValueError)
fn reply_int(v: &Value) -> Result<i64, ()> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f.trunc() as i64)).ok_or(()),
        Value::Bool(t) => Ok(*t as i64),
        Value::String(s) => s.trim().parse::<i64>().map_err(|_| ()),
        _ => Err(()),
    }
}

/// One entity draft from a model's JSON (None: not a usable one)
fn entity_draft(item: &Value, kinds: &[String]) -> Option<Value> {
    let o = item.as_object()?;
    let kind = o.get("kind").and_then(|k| k.as_str()).filter(|k| kinds.iter().any(|x| x == k))?;
    let name = reply_str(o.get("name"));
    if name.is_empty() {
        return None;
    }
    Some(json!({"kind": kind, "subtype": reply_str(o.get("subtype")), "name": name, "summary": reply_str(o.get("summary")),
        "body": reply_str(o.get("body")), "tags": reply_str(o.get("tags")), "folder": reply_str(o.get("folder")),
        "visible_to_players": o.get("visible_to_players").map_or(true, |v| crate::edits::py_truthy_pub(Some(v)))}))
}

/// `parse_entity_from_text` / `parse_entities_batch_from_text`
async fn entity_from_text(b: &Backend, text: &str, hint: &str, batch: bool) -> Result<Value, String> {
    let kinds = entity_kinds();
    let entity_props = json!({"kind": {"type": "string", "enum": kinds}, "subtype": {"type": "string"}, "name": {"type": "string"},
        "summary": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "string"}, "folder": {"type": "string"},
        "visible_to_players": {"type": "boolean"}});
    let entity = json!({"type": "object", "properties": entity_props, "required": ["kind", "name"]});
    let model = b.model.clone();
    if !batch {
        let mut system = recap_text("_ENTITY_FROM_TEXT_SYSTEM");
        if !hint.is_empty() {
            system.push_str(&format!("\n\nThe GM has selected the kind \"{hint}\" — frame the entity as a {hint} (the kind field MUST be \"{hint}\"). The text is a short creative BRIEF, not source material: invent coherent, evocative, setting-neutral details consistent with it (names, history, hooks) instead of only extracting what is stated."));
        }
        let reply = chat_with_schema(b, &model, &[json!({"role": "system", "content": system}), json!({"role": "user", "content": text})], false, &entity).await?;
        let parsed: Value = serde_json::from_str(&reply).unwrap_or(Value::Null);
        return entity_draft(&parsed, &kinds).ok_or_else(|| "Could not turn that reply into an entity — try rephrasing or picking a shorter passage.".to_string());
    }
    let pc = json!({"type": "object", "properties": {"name": {"type": "string"}, "player_name": {"type": "string"}, "race": {"type": "string"},
        "char_class": {"type": "string"}, "level": {"type": "integer"}, "xp": {"type": "integer"}, "backstory": {"type": "string"}, "notes": {"type": "string"}},
        "required": ["name"]});
    let schema = json!({"type": "object", "properties": {"entities": {"type": "array", "items": entity}, "player_characters": {"type": "array", "items": pc}},
        "required": ["entities", "player_characters"]});
    let text: String = text.chars().take(20000).collect();
    let reply = chat_with_schema(b, &model, &[json!({"role": "system", "content": recap_text("_ENTITIES_BATCH_SYSTEM")}), json!({"role": "user", "content": text})], false, &schema).await?;
    let unusable = || "Could not extract anything usable from that text — try a shorter passage, or a different model.".to_string();
    let parsed: Value = serde_json::from_str(&reply).map_err(|_| unusable())?;
    let o = parsed.as_object().ok_or_else(unusable)?;
    let list = |k: &str| -> Result<Vec<Value>, String> {
        match o.get(k) {
            v if !crate::edits::py_truthy_pub(v) => Ok(vec![]),
            Some(Value::Array(a)) => Ok(a.iter().take(25).cloned().collect()),
            _ => Err(unusable()),
        }
    };
    let entities: Vec<Value> = list("entities")?.iter().filter_map(|i| entity_draft(i, &kinds)).collect();
    let mut pcs = vec![];
    for item in list("player_characters")? {
        let Some(i) = item.as_object() else { continue };
        let name = reply_str(i.get("name"));
        if name.is_empty() { continue; }
        let num = |k: &str, d: i64| -> Result<i64, String> {
            let v = i.get(k).filter(|v| crate::edits::py_truthy_pub(Some(v)));
            if reply_str(v).is_empty() { return Ok(d); }
            reply_int(v.unwrap_or(&Value::Null)).map_err(|_| unusable())
        };
        pcs.push(json!({"name": name, "player_name": reply_str(i.get("player_name")), "race": reply_str(i.get("race")), "char_class": reply_str(i.get("char_class")),
            "level": num("level", 1)?.clamp(1, 20), "xp": num("xp", 0)?.max(0), "backstory": reply_str(i.get("backstory")), "notes": reply_str(i.get("notes"))}));
    }
    Ok(json!({"entities": entities, "player_characters": pcs}))
}
