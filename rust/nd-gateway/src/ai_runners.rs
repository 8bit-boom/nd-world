//! The in-memory AI jobs (app/live_jobs.py) the gateway runs itself: each has a start route (the checks, a job id at once) and a poll
//! route that reads the job's status dict, both as the Python routes answer them; the run is crate::runners' (the AI slot for its whole
//! length, the Background Jobs page's cancel / restart / remove). A poll for an id the gateway does not hold is Python's.

use axum::body::Body;
use axum::http::{HeaderMap, Method, Response, StatusCode, Uri};
use serde_json::{json, Map, Value};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::ai::{backend, py_repr, Backend};
use crate::ai_more::{crash, detail};
use crate::config::Config;
use crate::layout::PageCtx;
use crate::runners::{now, Rerun};

fn ok(v: Value) -> Response<Body> {
    crate::edits::json_response(&v).unwrap_or_else(crash)
}

fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

fn truthy(v: Option<&Value>) -> bool {
    crate::edits::py_truthy_pub(v)
}

/// `bool(body.get(k, default))`
fn flag(b: &Map<String, Value>, k: &str, default: bool) -> bool {
    match b.get(k) { None => default, v => truthy(v) }
}

/// `str(body.get(k) or "").strip()`
fn text(b: &Map<String, Value>, k: &str) -> String {
    match b.get(k) { v if !truthy(v) => String::new(), Some(v) => crate::edits::py_strip_pub(&py_str(v)), None => String::new() }
}

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Python's `round(x, 2)` (correctly rounded, ties to even) as a JSON number
fn round2(x: f64) -> Value {
    json!(format!("{x:.2}").parse::<f64>().unwrap_or(x))
}

/// Whether a request is one of this module's start or poll routes
pub fn serves(method: &Method, path: &str) -> bool {
    let start = *method == Method::POST && path.ends_with("/start");
    let poll = *method == Method::GET && path.rsplit('/').next().is_some_and(|i| !i.is_empty() && i.bytes().all(|b| b.is_ascii_digit()));
    (start || poll) && kind_of(path).is_some()
}

/// The job kind a start / poll path belongs to
fn kind_of(path: &str) -> Option<&'static str> {
    if path.starts_with("/api/maps/") && path.contains("/ai-markers/") {
        Some("map markers")
    } else if path.starts_with("/api/cockpit/ai/find/") {
        Some("cockpit find")
    } else if path.starts_with("/api/quests/suggest/") {
        Some("quest sync")
    } else if path.starts_with("/api/ai/auto-tag/") {
        Some("auto-tag")
    } else {
        None
    }
}

/// The routes this module serves: (method, path)
pub async fn route(cfg: Arc<Config>, method: &Method, uri: &Uri, headers: &HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let path = uri.path().to_string();
    let kind = kind_of(&path)?;
    if *method == Method::GET {
        let id: i64 = path.rsplit('/').next()?.parse().ok()?;
        let state = crate::runners::poll(kind, id)?;
        let (cfg2, uri2, headers2) = (cfg.clone(), uri.clone(), headers.clone());
        let p = tokio::task::spawn_blocking(move || page(&cfg2, &uri2, &headers2, "GET")).await.ok()??;
        return poll_answer(kind, &p, &state);
    }
    if *method != Method::POST || !path.ends_with("/start") {
        return None;
    }
    let (uri2, headers2) = (uri.clone(), headers.clone());
    tokio::task::spawn_blocking(move || {
        let p = page(&cfg, &uri2, &headers2, "POST")?;
        let db = crate::writes::open(&cfg)?;
        match kind {
            "map markers" => map_markers_start(cfg.clone(), &db, &p, &path, &body),
            "cockpit find" => cockpit_find_start(cfg.clone(), &db, &p, &body),
            "quest sync" => quest_sync_start(cfg.clone(), &db, &p, &body),
            "auto-tag" => auto_tag_start(cfg.clone(), &db, &p, &body),
            _ => None,
        }
    }).await.ok().flatten()
}

fn page(cfg: &Config, uri: &Uri, headers: &HeaderMap, method: &str) -> Option<PageCtx> {
    let db = crate::writes::open(cfg)?;
    let n = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method, uri, headers, n)?;
    (p.user_id > 0).then_some(p)
}

/// A poll route's answer for a job the gateway holds
fn poll_answer(kind: &str, p: &PageCtx, job: &Map<String, Value>) -> Option<Response<Body>> {
    let status = job.get("status").and_then(|v| v.as_str()).unwrap_or("");
    let elapsed = || json!((now() - job.get("started").and_then(|v| v.as_f64()).unwrap_or(0.0)).round_ties_even() as i64);
    match kind {
        "map markers" | "cockpit find" => {
            if !p.is_gm {
                return None;
            }
            let key = if kind == "map markers" { "markers" } else { "results" };
            Some(ok(match status {
                "running" => json!({"status": "running", "elapsed": elapsed()}),
                "error" => json!({"status": "error", "error": job.get("error")}),
                _ => json!({"status": "done", key: job.get(key)}),
            }))
        }
        "auto-tag" => Some(ok(if status == "error" { json!({"status": "error", "error": job.get("error")}) } else {
            json!({"status": status, "processed": job.get("processed"), "total": job.get("total"), "tagged": job.get("tagged")})
        })),
        "quest sync" => Some(ok(match status {
            "running" => json!({"status": "running", "elapsed": elapsed()}),
            "error" => json!({"status": "error", "error": job.get("error")}),
            _ => json!({"status": "done", "suggestions": job.get("suggestions")}),
        })),
        _ => None,
    }
}

/// The JSON body object (None: not an object - Python's own error)
fn json_body(body: &[u8]) -> Option<Map<String, Value>> {
    match serde_json::from_slice::<Value>(body).ok()? {
        Value::Object(o) => Some(o),
        _ => None,
    }
}

fn world_has_vault(db: &rusqlite::Connection, wid: i64) -> bool {
    db.query_row("SELECT obsidian_vault_path FROM worlds WHERE id = ?1", [wid], |r| r.get::<_, Option<String>>(0)).ok().flatten()
        .is_some_and(|p| !crate::edits::py_strip_pub(&p).is_empty())
}

// ── map markers (app/main.py `map_ai_markers_start` / `_map_ai_markers_task`) ──

fn map_file(cfg: &Config, slug: &str) -> Option<Map<String, Value>> {
    let jf = crate::maps::maps_dir(cfg)?.join(format!("{slug}.json"));
    match serde_json::from_str::<Value>(&std::fs::read_to_string(jf).ok()?).ok()? {
        Value::Object(o) => Some(o),
        _ => None,
    }
}

fn map_markers_start(cfg: Arc<Config>, db: &rusqlite::Connection, p: &PageCtx, path: &str, body: &[u8]) -> Option<Response<Body>> {
    let slug = path.strip_prefix("/api/maps/")?.strip_suffix("/ai-markers/start")?.to_string();
    if !p.is_gm || slug.contains('/') {
        return None;
    }
    let map = map_file(&cfg, &slug)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let map_world = match map.get("world_id") { None => 1, Some(v) => v.as_i64().filter(|_| !v.is_f64())? };
    if map_world != wid {
        return None;
    }
    let b = json_body(body)?;
    let prompt = text(&b, "prompt");
    if prompt.is_empty() {
        return Some(detail(StatusCode::BAD_REQUEST, "Describe what to place first"));
    }
    let count = match b.get("count") { v if !truthy(v) => 6, Some(v) => crate::ai::reply_int(v).unwrap_or(6), None => 6 }.clamp(1, 12);
    let (model, think, use_rag) = (text(&b, "model"), flag(&b, "think", true), flag(&b, "use_rag", true));
    if use_rag && world_has_vault(db, wid) {
        return None; // the vault layer of the lore lookup is Python's
    }
    let bk = backend(db)?;
    let id = crate::runners::new_job("map markers", json!({"status": "running", "started": now(), "markers": null, "error": ""}), 12);
    let rerun: Rerun = Arc::new(move |id| {
        let (cfg, bk, slug, prompt, model) = (cfg.clone(), bk.clone(), slug.clone(), prompt.clone(), model.clone());
        Box::pin(async move {
            let res = map_markers_task(cfg, bk, &slug, wid, &prompt, count, &model, think, use_rag).await;
            crate::runners::update("map markers", id, |e| match res {
                Ok(markers) => {
                    e.insert("status".into(), json!("done"));
                    e.insert("markers".into(), markers);
                }
                Err(m) => {
                    e.insert("status".into(), json!("error"));
                    e.insert("error".into(), json!(m));
                }
            });
        })
    });
    crate::runners::run("map markers", "map markers", id, rerun);
    Some(ok(json!({"job_id": id, "status": "running"})))
}

#[allow(clippy::too_many_arguments)]
async fn map_markers_task(cfg: Arc<Config>, b: Backend, slug: &str, wid: i64, prompt: &str, count: i64, model: &str, think: bool, use_rag: bool) -> Result<Value, String> {
    let (c, s, pr) = (cfg.clone(), slug.to_string(), prompt.to_string());
    let prepared = tokio::task::spawn_blocking(move || -> Result<(i64, i64, String, Vec<String>, String), String> {
        let map = map_file(&c, &s).ok_or("Map file is missing or unreadable")?;
        let dim = |k: &str| -> Result<i64, String> {
            match map.get(k) { v if !truthy(v) => Ok(2000), Some(v) => crate::ai::reply_int(v).map_err(|_| format!("invalid literal for int() with base 10: {}", py_repr(v))), None => Ok(2000) }
        };
        let (width, height) = (dim("width")?, dim("height")?);
        let mut existing: Vec<String> = vec![];
        for m in map.get("markers").filter(|v| truthy(Some(v))).and_then(|v| v.as_array()).cloned().unwrap_or_default() {
            if let Some(n) = m.as_object().and_then(|o| o.get("name")).filter(|v| truthy(Some(v))) {
                existing.push(py_str(n).to_lowercase());
            }
        }
        let db = crate::writes::open(&c).ok_or("database")?;
        let overlay: Option<Option<String>> = db.query_row("SELECT custom_markers_json FROM map_overlays WHERE slug = ?1 ORDER BY id LIMIT 1", [&s], |r| r.get(0)).ok();
        if let Some(raw) = overlay {
            if let Ok(Value::Array(list)) = serde_json::from_str::<Value>(raw.as_deref().filter(|r| !r.is_empty()).unwrap_or("[]")) {
                for m in list {
                    if let Some(l) = m.as_object().and_then(|o| o.get("label")).filter(|v| truthy(Some(v))) {
                        existing.push(py_str(l).to_lowercase());
                    }
                }
            }
        }
        let name = map.get("name").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_else(|| s.clone());
        let material = format!("Map: {name} ({width}x{height} px)\nGM request: {pr}");
        let rag = if use_rag {
            let q = take(&format!("{pr}\n{}", map.get("name").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_default()), 1500);
            take(&crate::retrieval::smart_world_context(&db, wid, &q, 8, 2).unwrap_or_default(), 3000)
        } else {
            String::new()
        };
        let user = format!("=== MAP ===\n{material}\n\n=== WORLD LORE (ground the markers in this) ===\n{}\n\n=== EXISTING MARKER LABELS (do not duplicate) ===\n{}",
            if rag.is_empty() { "(none)".to_string() } else { rag },
            { let l: Vec<String> = existing.iter().take(60).map(|l| format!("- {l}")).collect(); if l.is_empty() { "(none)".to_string() } else { l.join("\n") } });
        Ok((width, height, user, existing, String::new()))
    }).await.map_err(|_| "Cancelled.".to_string())??;
    let (width, height, user, existing, _) = prepared;
    let system = format!("You are placing points of interest on a tabletop RPG battle map. For each marker choose a short evocative label, a one-sentence description, and a plausible position on the map. Return STRICT JSON only:\n{{\"markers\": [{{\"label\": str, \"note\": str, \"lat\": number, \"lng\": number, \"color\": \"#rrggbb\"}}]}}\nRules: lat is the vertical position in pixels from the TOP (0..{height}), lng is the horizontal position from the LEFT (0..{width}) — spread markers out, don't cluster them in one corner; exactly {count} markers; labels must not duplicate the existing labels listed below; colors optional. No comments, no markdown fences.");
    let m = if model.is_empty() { b.model.clone() } else { model.to_string() };
    let schema = json!({"type": "object", "properties": {"markers": {"type": "array"}}, "required": ["markers"]});
    let raw = crate::ai::generate_chat_format(&b, &m, &system, &[json!({"role": "user", "content": user})], think, &Map::new(), Some(&schema)).await;
    // re.search(r"\{.*\}", raw, re.S): from the first "{" to the last "}"
    let parsed = match (raw.find('{'), raw.rfind('}')) {
        (Some(a), Some(z)) if z > a => serde_json::from_str::<Value>(&raw[a..=z]).ok(),
        _ => None,
    };
    let mut markers = vec![];
    if let Some(Value::Object(parsed)) = parsed {
        let mut seen: Vec<String> = existing;
        let items = match parsed.get("markers") { v if !truthy(v) => vec![], Some(Value::Array(a)) => a.clone(),
            Some(Value::String(s)) => s.chars().map(|c| json!(c.to_string())).collect(), Some(Value::Object(o)) => o.keys().map(|k| json!(k)).collect(),
            _ => return Err("'int' object is not iterable".into()) };
        for item in items {
            if markers.len() as i64 >= count {
                break;
            }
            let Value::Object(item) = item else { continue };
            let label = take(&text(&item, "label"), 80);
            if label.is_empty() || seen.contains(&label.to_lowercase()) {
                continue;
            }
            seen.push(label.to_lowercase());
            let num = |k: &str| -> Option<f64> {
                match item.get(k) { v if !truthy(v) => Some(0.0), Some(Value::Number(n)) => n.as_f64(), Some(Value::Bool(t)) => Some(*t as i64 as f64),
                    Some(Value::String(s)) => s.trim().parse::<f64>().ok().filter(|f| f.is_finite() || s.trim().to_lowercase().contains("inf")), _ => None }
            };
            let (Some(lat), Some(lng)) = (num("lat"), num("lng")) else { continue };
            let (lat, lng) = (lat.clamp(0.0, height as f64), lng.clamp(0.0, width as f64));
            let color = match item.get("color") { v if !truthy(v) => String::new(), Some(v) => py_str(v), None => String::new() };
            let hex = color.len() == 7 && color.starts_with('#') && color[1..].bytes().all(|c| c.is_ascii_hexdigit());
            markers.push(json!({"label": label, "note": take(&text(&item, "note"), 300), "lat": round2(lat), "lng": round2(lng),
                "color": if hex { color } else { "#ff4466".into() }}));
        }
    }
    if markers.is_empty() {
        return Err("The model's reply contained no usable markers — try rephrasing.".into());
    }
    Ok(Value::Array(markers))
}

/// Finish a job: its status dict to done (with `key`) or error
fn settle(kind: &'static str, id: i64, key: &str, res: Result<Value, String>) {
    crate::runners::update(kind, id, |e| match res {
        Ok(v) => {
            e.insert("status".into(), json!("done"));
            e.insert(key.into(), v);
        }
        Err(m) => {
            e.insert("status".into(), json!("error"));
            e.insert("error".into(), json!(m));
        }
    });
}

/// cockpit.py / quests.py `_extract_json`: the reply's object, fences tolerated
fn extract_object(raw: &str) -> Result<Value, String> {
    let mut t = crate::edits::py_strip_pub(raw);
    if t.starts_with("```") {
        static A: std::sync::OnceLock<[regex::Regex; 2]> = std::sync::OnceLock::new();
        let re = A.get_or_init(|| [regex::Regex::new(r"^```[a-zA-Z0-9]*\n?").unwrap(), regex::Regex::new(r"\n?```\s*$").unwrap()]);
        t = re[1].replace(&re[0].replace(&t, ""), "").into_owned();
    }
    match (t.find('{'), t.rfind('}')) {
        (Some(a), Some(z)) if z > a => serde_json::from_str(&t[a..=z]).map_err(|e| e.to_string()),
        _ => Err("no JSON object in the model's reply".into()),
    }
}

// ── cockpit find (app/routers/cockpit.py) ──────────────────────────────────

fn cockpit_find_start(cfg: Arc<Config>, db: &rusqlite::Connection, p: &PageCtx, body: &[u8]) -> Option<Response<Body>> {
    if !p.is_gm {
        return None;
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let b = json_body(body)?;
    let query = text(&b, "query");
    let entity_id = match b.get("entity_id") { v if !truthy(v) => None, Some(v) => crate::ai::reply_int(v).ok(), None => None };
    if query.is_empty() && entity_id.is_none() {
        return Some(detail(StatusCode::BAD_REQUEST, "Type what to look for first — or seed me from an entity card."));
    }
    if world_has_vault(db, wid) {
        return None;
    }
    let bk = backend(db)?;
    let id = crate::runners::new_job("cockpit find", json!({"status": "running", "started": now(), "results": null, "error": ""}), 12);
    let rerun: Rerun = Arc::new(move |id| {
        let (cfg, bk, query) = (cfg.clone(), bk.clone(), query.clone());
        Box::pin(async move {
            let res = cockpit_find_task(cfg, bk, wid, &query, entity_id).await;
            settle("cockpit find", id, "results", res);
        })
    });
    crate::runners::run("cockpit find", "cockpit find", id, rerun);
    Some(ok(json!({"job_id": id, "status": "running"})))
}

async fn cockpit_find_task(cfg: Arc<Config>, b: Backend, wid: i64, query: &str, entity_id: Option<i64>) -> Result<Value, String> {
    let (c, q) = (cfg.clone(), query.to_string());
    let (user, cands) = tokio::task::spawn_blocking(move || -> Result<(String, Vec<(i64, String, String)>), String> {
        let db = crate::writes::open(&c).ok_or("database")?;
        let mut material = q.clone();
        let mut exclude = vec![0i64];
        if let Some(eid) = entity_id {
            let ent = db.query_row("SELECT * FROM entities WHERE id = ?1", [eid], |r| crate::layout::row_map(r)).ok()
                .filter(|e| e.get("world_id").and_then(|v| v.as_i64()) == Some(wid)).ok_or("Focused entity not found in this world")?;
            exclude.push(eid);
            let s = |k: &str| ent.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
            material = format!("{}{}\n{}", s("name"), if s("summary").is_empty() { String::new() } else { format!(" — {}", s("summary")) }, take(&s("body"), 1500));
            if !q.is_empty() {
                material = format!("{q}\n\n{material}");
            }
        }
        let rag = take(&crate::retrieval::smart_world_context(&db, wid, &take(&material, 1500), 10, 4).unwrap_or_default(), 4000);
        let ex = exclude.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        let mut st = db.prepare(&format!("SELECT id, kind, name, summary FROM entities WHERE world_id = ?1 AND id NOT IN ({ex}) ORDER BY name LIMIT 200")).map_err(|e| e.to_string())?;
        let rows: Vec<(i64, Option<String>, Option<String>, Option<String>)> = st.query_map([wid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
            .map_err(|e| e.to_string())?.filter_map(Result::ok).collect();
        let board: Vec<String> = rows.iter().map(|(id, k, n, sm)| {
            let k = k.clone().filter(|k| !k.is_empty()).unwrap_or_else(|| "?".into());
            let mut l = format!("- id={id} [{k}] {}", n.clone().unwrap_or_else(|| "None".into()));
            if let Some(sm) = sm.as_ref().filter(|s| !s.is_empty()) { l.push_str(&format!(" — {}", take(sm, 100))); }
            l
        }).collect();
        let board = if board.is_empty() { "(none)".to_string() } else { board.join("\n") };
        let user = format!("=== FOCUS ===\n{}\n\n=== RETRIEVED WORLD EXCERPTS ===\n{}\n\n=== CANDIDATES ===\n{board}", take(&material, 3000), if rag.is_empty() { "(none)".to_string() } else { rag });
        Ok((user, rows.into_iter().map(|(i, k, n, _)| (i, k.unwrap_or_default(), n.unwrap_or_default())).collect()))
    }).await.map_err(|_| "Cancelled.".to_string())??;
    let system = "You are a campaign co-GM's research assistant. From the CANDIDATE list and the RETRIEVED WORLD EXCERPTS, select the entities and notes most connected to the FOCUS and give each a one-line reason. Return STRICT JSON only:\n{\"results\": [{\"id\": int, \"reason\": str}]}\nRules: only propose ids that appear verbatim in the candidate list; 3-8 results, most relevant first; reasons are one short in-world sentence; return an empty list when nothing is genuinely connected. No comments, no markdown fences.";
    let schema = json!({"type": "object", "properties": {"results": {"type": "array"}}, "required": ["results"]});
    let model = b.model.clone();
    let raw = crate::ai::generate_chat_format(&b, &model, system, &[json!({"role": "user", "content": user})], true, &Map::new(), Some(&schema)).await;
    let parsed = extract_object(&raw)?;
    let Value::Object(parsed) = parsed else { return Err("'list' object has no attribute 'get'".into()) };
    let items = match parsed.get("results") { v if !truthy(v) => vec![], Some(Value::Array(a)) => a.clone(), _ => return Err("unsupported reply".into()) };
    let mut out = vec![];
    let mut seen = vec![];
    for item in items.into_iter().take(8) {
        let Value::Object(item) = item else { continue };
        let Some(eid) = item.get("id").and_then(|v| crate::ai::reply_int(v).ok()) else { continue };
        let Some((_, kind, name)) = cands.iter().find(|c| c.0 == eid) else { continue };
        if seen.contains(&eid) {
            continue;
        }
        seen.push(eid);
        let reason = match item.get("reason") { v if !truthy(v) => String::new(), Some(v) => py_str(v), None => String::new() };
        out.push(json!({"id": eid, "name": name, "kind": kind, "reason": take(&reason, 300)}));
    }
    Ok(Value::Array(out))
}

// ── quest sync (app/routers/quests.py) ─────────────────────────────────────

fn quest_sync_start(cfg: Arc<Config>, db: &rusqlite::Connection, p: &PageCtx, body: &[u8]) -> Option<Response<Body>> {
    if !p.is_gm {
        return None;
    }
    let Some(w) = p.world.as_ref() else { return Some(detail(StatusCode::BAD_REQUEST, "No active world")) };
    let wid = w.get("id")?.as_i64()?;
    let b = json_body(body)?;
    let session_id = match b.get("session_id") { v if !truthy(v) => None, Some(v) => Some(crate::ai::reply_int(v).ok()?), None => None };
    let txt = text(&b, "text");
    if session_id.is_none() && txt.is_empty() {
        return Some(detail(StatusCode::BAD_REQUEST, "Provide session_id or text"));
    }
    let (model, use_rag, think) = (text(&b, "model"), flag(&b, "use_rag", true), flag(&b, "think", true));
    if use_rag && world_has_vault(db, wid) {
        return None;
    }
    let bk = backend(db)?;
    let id = crate::runners::new_job("quest sync", json!({"status": "running", "started": now(), "suggestions": null, "error": ""}), 12);
    let rerun: Rerun = Arc::new(move |id| {
        let (cfg, bk, txt, model) = (cfg.clone(), bk.clone(), txt.clone(), model.clone());
        Box::pin(async move {
            let res = quest_sync_task(cfg, bk, wid, session_id, &txt, &model, use_rag, think).await;
            settle("quest sync", id, "suggestions", res);
        })
    });
    crate::runners::run("quest sync", "quest sync", id, rerun);
    Some(ok(json!({"job_id": id, "status": "running"})))
}

#[allow(clippy::too_many_arguments)]
async fn quest_sync_task(cfg: Arc<Config>, b: Backend, wid: i64, session_id: Option<i64>, txt: &str, model: &str, use_rag: bool, think: bool) -> Result<Value, String> {
    let (c, t) = (cfg.clone(), txt.to_string());
    let (user, quest_ids) = tokio::task::spawn_blocking(move || -> Result<(String, Vec<i64>), String> {
        let db = crate::writes::open(&c).ok_or("database")?;
        let mut material = String::new();
        let gs = session_id.and_then(|sid| db.query_row("SELECT * FROM game_sessions WHERE id = ?1 AND world_id = ?2", rusqlite::params![sid, wid], |r| crate::layout::row_map(r)).ok());
        if let Some(gs) = &gs {
            // _session_quest_material
            let s = |k: &str| crate::edits::py_strip_pub(gs.get(k).and_then(|v| v.as_str()).unwrap_or(""));
            let mut parts = vec![];
            let summary = s("summary");
            if !summary.is_empty() {
                parts.push(format!("Session summary:\n{}", take(&summary, 3000)));
            }
            let transcript = s("live_transcript");
            if !transcript.is_empty() && summary.is_empty() {
                let n = transcript.chars().count();
                parts.push(format!("Session transcript (tail):\n{}", transcript.chars().skip(n.saturating_sub(6000)).collect::<String>()));
            }
            let mut st = db.prepare("SELECT content FROM facts WHERE game_session_id = ?1 ORDER BY created_at").map_err(|e| e.to_string())?;
            let facts: Vec<Option<String>> = st.query_map([gs.get("id").and_then(|v| v.as_i64()).unwrap_or(0)], |r| r.get(0)).map_err(|e| e.to_string())?.filter_map(Result::ok).collect();
            if !facts.is_empty() {
                let tail = &facts[facts.len().saturating_sub(30)..];
                parts.push(format!("Logged facts:\n{}", tail.iter().map(|f| format!("- {}", f.as_deref().unwrap_or("None"))).collect::<Vec<_>>().join("\n")));
            }
            material = crate::edits::py_strip_pub(&parts.join("\n\n"));
        }
        if material.is_empty() && !t.is_empty() {
            material = take(&t, 8000);
        }
        if let (true, Some(gs)) = (material.is_empty(), gs.as_ref()) {
            let num = gs.get("session_num").map(|v| match v { Value::Null => "None".to_string(), other => py_str(other) }).unwrap_or_else(|| "None".into());
            material = format!("Session #{num}: {}", gs.get("title").and_then(|v| v.as_str()).unwrap_or(""));
        }
        let mut st = db.prepare("SELECT id, status, category, title, summary FROM quests WHERE world_id = ?1 ORDER BY title").map_err(|e| e.to_string())?;
        let quests: Vec<(i64, Option<String>, Option<String>, Option<String>, Option<String>)> = st.query_map([wid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))
            .map_err(|e| e.to_string())?.filter_map(Result::ok).collect();
        let board: Vec<String> = quests.iter().map(|(id, st, cat, title, sm)| {
            let or = |v: &Option<String>, d: &str| v.clone().filter(|x| !x.is_empty()).unwrap_or_else(|| d.into());
            let mut l = format!("- id={id} [{}/{}] {}", or(st, "active"), or(cat, "main"), title.clone().unwrap_or_else(|| "None".into()));
            if let Some(sm) = sm.as_ref().filter(|x| !x.is_empty()) { l.push_str(&format!(" — {}", take(sm, 120))); }
            l
        }).collect();
        let board = if board.is_empty() { "(no quests yet)".to_string() } else { board.join("\n") };
        let mut world_ctx = String::new();
        if use_rag {
            let rag = crate::retrieval::smart_world_context(&db, wid, &take(&material, 1500), 8, 2).unwrap_or_default();
            if !rag.is_empty() {
                world_ctx = format!("\n\n=== World lore (ground the suggestions in this) ===\n{}", take(&rag, 4000));
            }
        }
        Ok((format!("=== CURRENT QUEST BOARD ===\n{board}\n\n=== SESSION MATERIAL ===\n{material}{world_ctx}"), quests.iter().map(|q| q.0).collect()))
    }).await.map_err(|_| "Cancelled.".to_string())??;
    let system = "You are a campaign co-GM. Compare the session material against the CURRENT QUEST BOARD and report what changed. Return STRICT JSON only:\n{\"new_quests\": [{\"title\": str, \"summary\": str, \"category\": \"main\"|\"side\"|\"personal\"}], \"quest_updates\": [{\"quest_id\": int, \"status\": \"active\"|\"complete\"|\"failed\", \"note\": str}]}\nRules: only propose quest_updates whose quest_id appears verbatim in the current quest board; only mark complete/failed when the session says it plainly; keep new quest titles short and in-world; propose a new quest only for a genuine open plot thread the session introduced (not a one-off scene beat); it is fine to return empty arrays if nothing changed. No comments, no markdown fences.";
    let schema = json!({"type": "object", "properties": {"new_quests": {"type": "array"}, "quest_updates": {"type": "array"}}, "required": ["new_quests", "quest_updates"]});
    let m = if model.is_empty() { b.model.clone() } else { model.to_string() };
    let raw = crate::ai::generate_chat_format(&b, &m, system, &[json!({"role": "user", "content": user})], think, &Map::new(), Some(&schema)).await;
    // quests.py _extract_json: the whole reply (fences off), else its outermost object
    let mut t = crate::edits::py_strip_pub(&raw);
    if t.starts_with("```") {
        static A: std::sync::OnceLock<[regex::Regex; 2]> = std::sync::OnceLock::new();
        let re = A.get_or_init(|| [regex::Regex::new(r"^```[a-zA-Z]*\n?").unwrap(), regex::Regex::new(r"\n?```$").unwrap()]);
        t = re[1].replace(&re[0].replace(&t, ""), "").into_owned();
    }
    let invalid = || "The model's reply wasn't valid JSON — try again.".to_string();
    let parsed = match serde_json::from_str::<Value>(&t) {
        Ok(v) => v,
        Err(_) => match (t.find('{'), t.rfind('}')) {
            (Some(a), Some(z)) if z > a => serde_json::from_str::<Value>(&t[a..=z]).map_err(|_| invalid())?,
            _ => return Err(invalid()),
        },
    };
    let Value::Object(parsed) = parsed else { return Err(format!("AttributeError: '{}' object has no attribute 'get'", if parsed.is_array() { "list" } else { "str" })) };
    let list = |k: &str| -> Result<Vec<Value>, String> {
        match parsed.get(k) { v if !truthy(v) => Ok(vec![]), Some(Value::Array(a)) => Ok(a.clone()), Some(Value::String(_)) | Some(Value::Object(_)) => Ok(vec![]),
            _ => Err("TypeError: object is not iterable".into()) }
    };
    let (new_q, upd) = (list("new_quests")?, list("quest_updates")?);
    let statuses = ["active", "complete", "failed", "secret"];
    let new_quests: Vec<Value> = new_q.iter().filter_map(|n| n.as_object()).filter(|n| !text(n, "title").is_empty()).take(12).map(|n| {
        let cat = n.get("category").and_then(|c| c.as_str()).filter(|c| ["main", "side", "personal"].contains(c)).unwrap_or("side");
        json!({"title": take(&text(n, "title"), 256), "summary": take(&text(n, "summary"), 512), "category": cat})
    }).collect();
    let updates: Vec<Value> = upd.iter().filter_map(|u| u.as_object())
        .filter_map(|u| u.get("quest_id").filter(|v| v.is_i64() || v.is_u64()).and_then(|v| v.as_i64()).filter(|q| quest_ids.contains(q)).map(|q| (q, u)))
        .take(24).map(|(q, u)| {
            let st = u.get("status").and_then(|c| c.as_str()).filter(|c| statuses.contains(c)).unwrap_or("active");
            json!({"quest_id": q, "status": st, "note": take(&text(u, "note"), 512)})
        }).collect();
    Ok(json!({"new_quests": new_quests, "quest_updates": updates}))
}

// ── auto-tag (app/routers/ai.py) ───────────────────────────────────────────

fn auto_tag_start(cfg: Arc<Config>, db: &rusqlite::Connection, p: &PageCtx, body: &[u8]) -> Option<Response<Body>> {
    if !(p.is_gm || p.role.as_deref() == Some("assistant")) {
        return None;
    }
    let Some(w) = p.world.as_ref() else { return Some(detail(StatusCode::BAD_REQUEST, "No active world")) };
    let wid = w.get("id")?.as_i64()?;
    let b = match serde_json::from_slice::<Value>(body) {
        Ok(Value::Object(o)) => o,
        Ok(_) => return None,
        Err(_) => return Some(detail(StatusCode::BAD_REQUEST, "Invalid JSON body")),
    };
    let ids = match b.get("entity_ids") { Some(Value::Array(a)) if !a.is_empty() => a.clone(), _ => return Some(detail(StatusCode::BAD_REQUEST, "Select at least one entity first")) };
    let mut nums = vec![];
    for i in &ids {
        match crate::ai::reply_int(i) { Ok(n) => nums.push(n), Err(_) => return Some(detail(StatusCode::BAD_REQUEST, "Invalid entity ids")) }
    }
    nums.truncate(500);
    let (overwrite, think) = (flag(&b, "overwrite", false), flag(&b, "think", true));
    let marks = nums.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
    let only_empty = if overwrite { "" } else { " AND (tags IS NULL OR tags = '')" };
    let mut st = db.prepare(&format!("SELECT id FROM entities WHERE world_id = ?1 AND id IN ({marks}){only_empty} ORDER BY id")).ok()?;
    let ents: Vec<i64> = st.query_map([wid], |r| r.get(0)).ok()?.filter_map(Result::ok).collect();
    if ents.is_empty() {
        return Some(detail(StatusCode::BAD_REQUEST, "Nothing to tag — the selected entities already have tags."));
    }
    let bk = backend(db)?;
    let total = ents.len();
    let id = crate::runners::new_job("auto-tag", json!({"status": "running", "started": now(), "processed": 0, "total": total, "tagged": 0, "error": ""}), 12);
    let rerun: Rerun = Arc::new(move |id| {
        let (cfg, bk, ents) = (cfg.clone(), bk.clone(), ents.clone());
        Box::pin(async move {
            if let Err(m) = auto_tag_task(cfg, bk, id, &ents, overwrite, think).await {
                crate::runners::update("auto-tag", id, |e| {
                    e.insert("status".into(), json!("error"));
                    e.insert("error".into(), json!(m));
                });
            }
        })
    });
    crate::runners::run("auto-tag", "auto-tag", id, rerun);
    Some(ok(json!({"job_id": id, "total": total, "status": "running"})))
}

/// `_auto_tag_normalize`
fn tag_list(raw: Option<&Value>) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    for t in raw.and_then(|v| v.as_array()).cloned().unwrap_or_default() {
        let t = take(&crate::edits::py_strip_pub(&py_str(&t)), 40);
        if t.is_empty() || out.iter().any(|o| o.to_lowercase() == t.to_lowercase()) {
            continue;
        }
        out.push(t);
        if out.len() >= 10 {
            break;
        }
    }
    out
}

async fn auto_tag_task(cfg: Arc<Config>, b: Backend, job: i64, ids: &[i64], overwrite: bool, think: bool) -> Result<(), String> {
    let system = "You are tagging a tabletop RPG campaign's entities and notes. For EACH entity in the list choose 3-8 short tags describing what it is (type, faction, role, location, mood). Return STRICT JSON only:\n{\"entities\": [{\"id\": int, \"tags\": [str]}]}\nRules: every id in your reply must appear verbatim in the list; each tag is 1-3 words; no comments, no markdown fences.";
    let schema = json!({"type": "object", "properties": {"entities": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "tags": {"type": "array"}}}}}});
    let total = ids.len();
    let model = b.model.clone();
    for (n, batch) in ids.chunks(8).enumerate() {
        let (c, bt) = (cfg.clone(), batch.to_vec());
        let listing = tokio::task::spawn_blocking(move || -> Option<String> {
            let db = crate::writes::open(&c)?;
            let mut lines = vec![];
            for id in bt {
                let Ok(e) = db.query_row("SELECT * FROM entities WHERE id = ?1", [id], |r| crate::layout::row_map(r)) else { continue };
                let s = |k: &str| e.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
                let kind = if s("kind").is_empty() { "?".to_string() } else { s("kind") };
                let mut l = format!("- id={id} [{kind}] {}", s("name"));
                if !s("summary").is_empty() { l.push_str(&format!(" — {}", take(&s("summary"), 150))); }
                lines.push(l);
                let excerpt = take(&s("body").split_whitespace().collect::<Vec<_>>().join(" "), 600);
                if !excerpt.is_empty() { lines.push(format!("  text: {excerpt}")); }
            }
            Some(lines.join("\n"))
        }).await.ok().flatten().ok_or("database")?;
        let raw = crate::ai::generate_chat_format(&b, &model, system, &[json!({"role": "user", "content": format!("=== ENTITIES TO TAG ===\n{listing}")})], think, &Map::new(), Some(&schema)).await;
        let parsed = match (raw.find('{'), raw.rfind('}')) { (Some(a), Some(z)) if z > a => serde_json::from_str::<Value>(&raw[a..=z]).ok(), _ => None };
        let (c, bt) = (cfg.clone(), batch.to_vec());
        tokio::task::spawn_blocking(move || -> Result<(), String> {
            let Some(Value::Object(parsed)) = parsed else { return Ok(()) };
            let items = match parsed.get("entities") { v if !truthy(v) => vec![], Some(Value::Array(a)) => a.clone(), Some(Value::String(_)) | Some(Value::Object(_)) => vec![],
                _ => return Err("'int' object is not iterable".into()) };
            let db = crate::writes::open(&c).ok_or("database")?;
            for item in items {
                let Value::Object(item) = item else { continue };
                let Some(eid) = item.get("id").and_then(|v| crate::ai::reply_int(v).ok()) else { continue };
                if !bt.contains(&eid) {
                    continue;
                }
                let tags = tag_list(item.get("tags"));
                if tags.is_empty() {
                    continue;
                }
                let current: Option<String> = db.query_row("SELECT tags FROM entities WHERE id = ?1", [eid], |r| r.get(0)).ok().flatten();
                if !overwrite && !crate::edits::py_strip_pub(current.as_deref().unwrap_or("")).is_empty() {
                    continue;
                }
                crate::orm::update(&db, "entities", eid, &[("tags", rusqlite::types::Value::Text(take(&tags.join(", "), 512)))]);
            }
            Ok(())
        }).await.map_err(|_| "Cancelled.".to_string())??;
        let (c, all) = (cfg.clone(), ids.to_vec());
        let tagged = tokio::task::spawn_blocking(move || -> usize {
            let Some(db) = crate::writes::open(&c) else { return 0 };
            all.iter().filter(|i| db.query_row("SELECT tags FROM entities WHERE id = ?1", [**i], |r| r.get::<_, Option<String>>(0)).ok().flatten()
                .is_some_and(|t| !crate::edits::py_strip_pub(&t).is_empty())).count()
        }).await.unwrap_or(0);
        let processed = total.min(n * 8 + batch.len());
        crate::runners::update("auto-tag", job, |e| {
            e.insert("processed".into(), json!(processed));
            e.insert("tagged".into(), json!(tagged));
        });
    }
    crate::runners::update("auto-tag", job, |e| { e.insert("status".into(), json!("done")); });
    Ok(())
}
