//! The Chronicler (`POST /api/chronicler/ask`, app/routers/chronicler.py) answered by the gateway: the same system prompt (facts, the
//! entities the question finds with their excerpts and notes, the GM's AI instructions), the same request to an Unsloth Studio
//! backend (OpenAI dialect, app/llm_client.py) and the same SSE stream back, with Python's error texts, keep-alives and `<think>` split.
//! Answered only on the Unsloth backend over plain http, and for a GM only without a model or Thinking choice of their own (those -
//! capability probes, the prompt-token fallback - and the legacy Ollama backend stay Python's). The 30 s answer cache and a player's
//! 3 s cooldown are kept here, as Python keeps them in its process.

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

type CacheKey = (i64, i64, String);

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
    pub options: Map<String, Value>,
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
    if let Ok(Value::Object(per)) = serde_json::from_str::<Value>(if raw.is_empty() { "{}" } else { &raw }) {
        if let Some(Value::Object(o)) = per.get(&model).and_then(|m| m.get("options")) {
            for (k, v) in o {
                options.insert(k.clone(), v.clone());
            }
        }
    }
    Some(Backend { host, base_path, key, model, options })
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
    Fresh { backend: Backend, system: String, question: String, key: CacheKey },
    Refuse(Response<Body>),
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
    if p.is_gm {
        let model_set = matches!(b.get("model"), Some(v) if crate::edits::py_truthy_pub(Some(v)));
        if model_set || crate::edits::py_truthy_pub(b.get("think")) {
            return None; // a GM's own model / Thinking: Python's
        }
    }
    let backend = backend(&db)?;
    let key: CacheKey = (wid, p.user_id, question.to_lowercase());
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
    Some(Plan::Fresh { backend, system: lines.join("\n"), question, key })
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
async fn resolve_model(b: &Backend) -> (String, Option<String>) {
    let target = b.model.clone();
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
async fn stream_chat(b: &Backend, model: &str, system: &str, question: &str, tx: &tokio::sync::mpsc::Sender<Piece>) {
    let mut body = Map::new();
    body.insert("model".into(), json!(model));
    body.insert("messages".into(), json!([{"role": "system", "content": system}, {"role": "user", "content": question}]));
    body.insert("stream".into(), json!(true));
    body.extend(translate_options(&b.options));
    body.insert("chat_template_kwargs".into(), json!({"enable_thinking": false}));
    let Some(req) = backend_request(b, "POST", "/v1/chat/completions", Some(&Value::Object(body))) else { return };
    let resp = match client().request(req).await {
        Ok(r) => r,
        Err(e) => {
            let _ = tx.send(Piece::Error(format!("[AI unavailable: ConnectionError: ConnectError: {e}]"))).await;
            return;
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
        let _ = tx.send(Piece::Error(format!("[AI error: Unsloth {status}: {message}]"))).await;
        return;
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
                return;
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
                let _ = tx.send(Piece::Error(format!("[AI error: Unsloth {status}: {message}]"))).await;
                return;
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
}

/// The entry point: the answer, or the request forwarded to Python unchanged
pub async fn chronicler_ask(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let (c2, u2, h2, b2) = (cfg.clone(), uri.clone(), headers.clone(), body.clone());
    let plan = tokio::task::spawn_blocking(move || plan(&c2, &u2, &h2, &b2)).await.ok()??;
    let (backend, system, question, key) = match plan {
        Plan::Refuse(r) => return Some(r),
        Plan::Cached(answer) => {
            let (tx, rx) = tokio::sync::mpsc::channel(4);
            tokio::spawn(async move {
                let _ = tx.send(sse(&json!({"token": answer}))).await;
                let _ = tx.send("data: [DONE]\n\n".to_string()).await;
            });
            return Some(stream_response(rx));
        }
        Plan::Fresh { backend, system, question, key } => (backend, system, question, key),
    };
    let (tx, rx) = tokio::sync::mpsc::channel::<String>(64);
    tokio::spawn(async move {
        let (model, note) = resolve_model(&backend).await;
        if let Some(n) = note {
            let _ = tx.send(sse(&json!({"note": n}))).await;
        }
        let (ptx, mut prx) = tokio::sync::mpsc::channel::<Piece>(64);
        tokio::spawn(async move { stream_chat(&backend, &model, &system, &question, &ptx).await });
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
        if !had_error {
            if let Ok(mut c) = cache().lock() {
                c.retain(|_, (at, _)| at.elapsed() < CACHE_TTL);
                c.insert(key, (Instant::now(), answer));
            }
        }
        let _ = tx.send("data: [DONE]\n\n".into()).await;
    });
    Some(stream_response(rx))
}
