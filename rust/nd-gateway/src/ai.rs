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

fn cooldowns() -> &'static Mutex<HashMap<i64, Instant>> {
    static C: OnceLock<Mutex<HashMap<i64, Instant>>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(HashMap::new()))
}

/// The LLM backend as app.ai resolves it (Settings row first, then the environment); None when it is not Unsloth over plain http
pub struct Backend {
    pub host: String,
    pub base_path: String,
    pub key: String,
    pub model: String,
    /// Settings > System's generation options (instance-wide)
    pub options: Map<String, Value>,
    /// Settings' per-model overrides ({model: {"options": {...}}})
    pub per_model: Map<String, Value>,
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
    Some(Backend { host, base_path, key, model, options, per_model })
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
    Some(Plan::Fresh(Job { backend, requested: model, system: lines.join("\n"), messages: vec![json!({"role": "user", "content": question})], think, extra, cache: Some(key), on_done: None }))
}

type Client = hyper_util::client::legacy::Client<hyper_util::client::legacy::connect::HttpConnector, http_body_util::Full<bytes::Bytes>>;

fn client() -> &'static Client {
    static C: OnceLock<Client> = OnceLock::new();
    C.get_or_init(|| hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new()).build_http())
}

fn backend_request(b: &Backend, method: &str, path: &str, body: Option<&Value>) -> Option<hyper::Request<http_body_util::Full<bytes::Bytes>>> {
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
        let Job { backend, requested, system, messages, think, extra, cache: key, on_done } = job;
        let (model, note) = resolve_model(&backend, &requested).await;
        if let Some(n) = note {
            let _ = tx.send(sse(&json!({"note": n}))).await;
        }
        let (ptx, mut prx) = tokio::sync::mpsc::channel::<Piece>(64);
        tokio::spawn(async move { stream_chat_retrying(&backend, &model, &system, &messages, think, &extra, &ptx).await });
        let (mut answer, mut had_error) = (String::new(), false);
        loop {
            match tokio::time::timeout(HEARTBEAT, prx.recv()).await {
                Err(_) => {
                    if tx.send(": keep-alive\n\n".into()).await.is_err() {
                        return;
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
                    if tx.send(line).await.is_err() {
                        return;
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
fn surface_default(cfg: &Config, surface: &str) -> Option<String> {
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
    // _build_ollama_messages: only messages without attachments or with document ones (pictures and audio are Python's)
    let mut messages = vec![];
    for m in b.get("messages")?.as_array()? {
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
    Some(Job { backend, requested, system, messages, think, extra, cache: None, on_done: None })
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
    Some(Job { backend, requested, system, messages, think, extra, cache: None, on_done: Some(on_done) })
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

fn assist_plan(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<AssistPlan> {
    let db = crate::writes::open(cfg)?;
    assist_caller(&db, cfg, uri, headers)?;
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
    let backend = backend(&db)?;
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
    if text.is_empty() {
        return String::new();
    }
    static TOKEN: OnceLock<regex::Regex> = OnceLock::new();
    static ROLE: OnceLock<regex::Regex> = OnceLock::new();
    let t = TOKEN.get_or_init(|| regex::Regex::new(r"<\|[^<>|]{0,40}\|>").unwrap()).replace_all(text, "").replace("<start_of_turn>", "").replace("<end_of_turn>", "");
    let role = ROLE.get_or_init(|| regex::Regex::new(r"(?i)^(system|user|assistant|model)$").unwrap());
    let mut cleaned: Vec<&str> = vec![];
    for line in t.split('\n') {
        let stripped = crate::edits::py_strip_pub(line);
        if role.is_match(&stripped) && cleaned.iter().any(|l| !crate::edits::py_strip_pub(l).is_empty()) {
            break;
        }
        if !stripped.is_empty() && cleaned.last().is_some_and(|l| crate::edits::py_strip_pub(l) == stripped) {
            continue;
        }
        cleaned.push(line);
    }
    cleaned.join("\n").trim_end_matches(|c: char| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)).to_string()
}

/// `POST /api/ai/assist` for a free-text operation: {op, mode: "text", text, model}, or a 502 with the failure text; None (Python's) otherwise
pub async fn ai_assist(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let a = match tokio::task::spawn_blocking(move || assist_plan(&cfg, &uri, &headers, &body)).await.ok()?? {
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
    if b.use_rag && p.is_gm {
        return None; // a GM's lore retrieval (_build_rag_context): Python's
    }
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
        ("extra_instructions", opt_text(instruction)), ("use_rag", Sql::Integer(0)), ("rag_entity_limit", opt_int(b.rag.0)),
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

/// `start_resume_job` for an ai_assist row (single phase: straight back to "summarizing") and the run started
pub fn resume_assist_job(db: &rusqlite::Connection, id: i64, reset_attempts: bool) -> Option<()> {
    use rusqlite::types::Value as Sql;
    let resumed: i64 = db.query_row("SELECT COALESCE(resumed_count, 0) FROM audio_jobs WHERE id = ?1", [id], |r| r.get(0)).ok()?;
    crate::orm::update(db, "audio_jobs", id, &[("status", Sql::Text("summarizing".into())), ("error", Sql::Text(String::new())),
        ("chunk_current", Sql::Null), ("chunk_total", Sql::Null), ("resumed_count", Sql::Integer(if reset_attempts { 0 } else { resumed + 1 })),
        ("run_started_at", Sql::Text(crate::orm::now_stamp())), ("finished_at", Sql::Null)])?;
    start_assist_job(id);
    Some(())
}

/// `_run_job` for purpose="ai_assist": the AI slot, then one run_assist call; the row ends done (result_json), or error
async fn run_assist_job(id: i64) {
    let Some(_slot) = crate::jobs::slot("audio job").await else { return };
    let Some(cfg) = crate::jobs::cfg() else { return };
    let c = cfg.clone();
    let prep = tokio::task::spawn_blocking(move || -> Option<Result<(Assist, String), String>> {
        let db = crate::writes::open(&c)?;
        let row = db.query_row("SELECT model, think, transcript, assist_params_json FROM audio_jobs WHERE id = ?1", [id],
            |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<i64>>(1)?, r.get::<_, Option<String>>(2)?, r.get::<_, Option<String>>(3)?))).ok()?;
        let (model, think, transcript, params) = row;
        let model = match model.filter(|m| !m.is_empty()) { Some(m) => m, None => surface_default(&c, "recap").unwrap_or_default() };
        let think = think.map_or(true, |t| t != 0);
        let params = params.as_deref().filter(|p| !p.is_empty()).and_then(|p| serde_json::from_str::<Value>(p).ok()).unwrap_or(json!({}));
        let pg = |k: &str| params.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let op = pg("op");
        let set = |fields: &[(&str, rusqlite::types::Value)]| crate::orm::update(&db, "audio_jobs", id, fields);
        set(&[("status", rusqlite::types::Value::Text("summarizing".into()))]);
        let Some(backend) = backend(&db) else { return Some(Err("AI unavailable: no Unsloth Studio backend is configured".into())) };
        let (system, user) = match assist_prompt(&op, &transcript.unwrap_or_default(), &pg("meta"), &pg("instruction"), &pg("lang")) {
            Ok(p) => p,
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
        crate::edits::json_response(&json!({"ok": true}))
    }).await.ok()?
}
