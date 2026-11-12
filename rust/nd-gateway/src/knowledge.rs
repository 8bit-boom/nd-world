//! app/vault_sync.py and app/routers/knowledge.py: the Obsidian vault's index (`POST /api/knowledge/sync`: note chunks embedded through the
//! backend's /v1/embeddings, wikilink / frontmatter edges) and the AI relation suggestions (`/api/knowledge/suggest-relations`), plus
//! `GET /api/ai/test-chat`. Unsloth backend only; anything else is Python's.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use axum::body::Body;
use axum::http::{Response, StatusCode};
use http_body_util::BodyExt;
use regex::Regex;
use serde_json::{json, Map, Value};

use crate::ai::{backend, backend_request, client, Backend};
use crate::config::Config;
use crate::edits::py_strip_pub as strip;
use crate::layout::PageCtx;

const RELATION_SYSTEM: &str = r#"You read one page from a tabletop RPG GM's worldbuilding notes and find relationships it EXPLICITLY states between two of the KNOWN entities listed in the schema below — never a relationship you have to infer or guess, and never an entity outside that list. For each relationship you find, give "source" and "target" (the exact known-entity names involved) and "relation": a short snake_case label for what connects them, written from source to target (e.g. located_in, member_of, owns, allied_with, enemy_of, parent_of, works_for, rules). Do not report a relationship just because two names appear in the same paragraph — only ones the text actually states. If the page states no relationships between known entities, return an empty list. Respond with JSON only."#;

pub(crate) type Fut = std::pin::Pin<Box<dyn std::future::Future<Output = Response<Body>> + Send>>;

fn re(cell: &'static OnceLock<Regex>, pat: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pat).unwrap())
}

fn extract_wikilinks(text: &str) -> Vec<String> {
    static R: OnceLock<Regex> = OnceLock::new();
    let mut seen: Vec<String> = vec![];
    for m in re(&R, r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]").captures_iter(text) {
        let name = strip(&m[1]);
        if !name.is_empty() && !seen.contains(&name) {
            seen.push(name);
        }
    }
    seen
}

fn strip_value_quotes(v: &str) -> String {
    let v = strip(v);
    let c: Vec<char> = v.chars().collect();
    if c.len() >= 2 && c[0] == c[c.len() - 1] && (c[0] == '\'' || c[0] == '"') {
        return c[1..c.len() - 1].iter().collect();
    }
    v
}

#[derive(Clone)]
enum Fm {
    One(String),
    Many(Vec<String>),
}

/// `parse_frontmatter`: (fields in insertion order, body)
fn parse_frontmatter(md: &str) -> (Vec<(String, Fm)>, String) {
    static FM: OnceLock<Regex> = OnceLock::new();
    static ITEM: OnceLock<Regex> = OnceLock::new();
    let Some(m) = re(&FM, r"(?s)\A---\s*\n(.*?)\n---\s*\n?").captures(md) else { return (vec![], md.to_string()) };
    let body = md[m.get(0).unwrap().end()..].to_string();
    let raw = m[1].to_string();
    let lines: Vec<&str> = raw.split('\n').collect();
    let mut fields: Vec<(String, Fm)> = vec![];
    let mut put = |k: String, v: Fm| match fields.iter_mut().find(|(key, _)| *key == k) {
        Some(slot) => slot.1 = v,
        None => fields.push((k, v)),
    };
    let item = re(&ITEM, r"^\s*-\s*(.+?)\s*$");
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        i += 1;
        let s = strip(line);
        if s.is_empty() || s.starts_with('#') || !line.contains(':') {
            continue;
        }
        let (key, rest) = line.split_once(':').unwrap();
        let (key, rest) = (strip(key), strip(rest));
        if key.is_empty() {
            continue;
        }
        if rest.is_empty() {
            let mut items = vec![];
            while i < lines.len() && item.is_match(lines[i]) {
                items.push(strip_value_quotes(&item.captures(lines[i]).unwrap()[1]));
                i += 1;
            }
            put(key, Fm::Many(items));
        } else if rest.starts_with('[') && rest.ends_with(']') && !(rest.starts_with("[[") && rest.ends_with("]]")) {
            let inner = strip(&rest[1..rest.len() - 1]);
            let list = if inner.is_empty() { vec![] } else { inner.split(',').filter(|v| !strip(v).is_empty()).map(strip_value_quotes).collect() };
            put(key, Fm::Many(list));
        } else {
            put(key, Fm::One(strip_value_quotes(&rest)));
        }
    }
    (fields, body)
}

/// `_iter_vault_notes`: (relative path, text) for every .md, directories pruned and sorted as os.walk does
fn vault_notes(root: &Path) -> Vec<(String, String)> {
    fn walk(dir: &Path, root: &Path, out: &mut Vec<(String, String)>) {
        let Ok(rd) = std::fs::read_dir(dir) else { return };
        let (mut dirs, mut files) = (vec![], vec![]);
        for e in rd.filter_map(|e| e.ok()) {
            let name = e.file_name().to_string_lossy().to_string();
            let ft = e.file_type();
            let is_dir = e.path().is_dir();
            // a symlinked directory is listed but never entered (os.walk's followlinks=False)
            if is_dir {
                if !name.starts_with('.') && name != "node_modules" && !ft.map_or(false, |t| t.is_symlink()) {
                    dirs.push(name);
                }
            } else {
                files.push(name);
            }
        }
        dirs.sort();
        files.sort();
        for f in files {
            if !f.to_lowercase().ends_with(".md") {
                continue;
            }
            let path = dir.join(&f);
            let Ok(bytes) = std::fs::read(&path) else { continue };
            let text = decode_ignore(&bytes);
            let rel = path.strip_prefix(root).unwrap_or(&path).components().map(|c| c.as_os_str().to_string_lossy().to_string()).collect::<Vec<_>>().join("/");
            out.push((rel, text));
        }
        for d in dirs {
            walk(&dir.join(d), root, out);
        }
    }
    let mut out = vec![];
    walk(root, root, &mut out);
    out
}

/// `bytes.decode("utf-8", errors="ignore")`
fn decode_ignore(mut b: &[u8]) -> String {
    let mut out = String::new();
    loop {
        match std::str::from_utf8(b) {
            Ok(s) => {
                out.push_str(s);
                return out;
            }
            Err(e) => {
                out.push_str(std::str::from_utf8(&b[..e.valid_up_to()]).unwrap());
                let skip = e.error_len().unwrap_or(b.len() - e.valid_up_to());
                b = &b[e.valid_up_to() + skip..];
            }
        }
    }
}

/// `_entity_name_map`
fn name_map(db: &rusqlite::Connection, wid: i64) -> Option<HashMap<String, i64>> {
    let rows: Vec<(i64, Option<String>, Option<String>)> = db.prepare("SELECT id, name, aliases FROM entities WHERE world_id = ?1").ok()?
        .query_map([wid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?.collect::<Result<_, _>>().ok()?;
    let mut names: HashMap<String, i64> = HashMap::new();
    for (id, name, aliases) in &rows {
        if let Some(n) = name.as_ref().filter(|n| !n.is_empty()) {
            names.entry(n.to_lowercase()).or_insert(*id);
        }
        for alias in aliases.as_deref().unwrap_or("").split(',') {
            let a = strip(alias);
            if !a.is_empty() {
                names.entry(a.to_lowercase()).or_insert(*id);
            }
        }
    }
    for (id, name, _) in &rows {
        for v in nd_render::text::derive_name_variants(name.as_deref().unwrap_or("")) {
            names.entry(v.to_lowercase()).or_insert(*id);
        }
    }
    Some(names)
}

/// `_match_note_entity`: (display title, matched entity)
fn match_note(map: &HashMap<String, i64>, rel: &str, fm: &[(String, Fm)], body: &str) -> (String, Option<i64>) {
    static H1: OnceLock<Regex> = OnceLock::new();
    let h1 = re(&H1, r"(?m)^#\s+(.+?)\s*$").captures(body).map(|m| m[1].to_string());
    let stem = Path::new(rel).file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    let title_fm = fm.iter().find(|(k, _)| k == "title").and_then(|(_, v)| match v {
        Fm::One(s) if !s.is_empty() => Some(s.clone()),
        Fm::Many(l) if !l.is_empty() => Some(format!("[{}]", l.iter().map(|x| format!("'{x}'")).collect::<Vec<_>>().join(", "))),
        _ => None,
    });
    let cands: Vec<Option<String>> = vec![title_fm, h1, Some(stem.clone())];
    let display = cands.iter().flatten().find(|t| !t.is_empty()).cloned().unwrap_or(stem);
    let id = cands.iter().flatten().find_map(|t| (!t.is_empty()).then(|| map.get(&t.to_lowercase()).copied()).flatten());
    (display, id)
}

fn pack_embedding(vec: &[f64]) -> String {
    use base64::Engine;
    let mut bytes = Vec::with_capacity(vec.len() * 4);
    for v in vec {
        bytes.extend_from_slice(&(*v as f32).to_le_bytes());
    }
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

fn embed_model(cfg: &Config) -> String {
    let stored = cfg.db_path.parent().map(|d| d.join("ai_models.json")).and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|t| serde_json::from_str::<Value>(&t).ok()).and_then(|v| v.get("embed_model").and_then(|m| m.as_str().map(str::to_string))).filter(|m| !m.is_empty());
    stored.unwrap_or_else(|| std::env::var("UNSLOTH_EMBED_MODEL").ok().filter(|m| !m.is_empty()).unwrap_or_else(|| "unsloth/bge-small-en-v1.5".into()))
}

/// `embed_text`: one chunk's vector, None on any failure
async fn embed(b: &Backend, model: &str, text: &str) -> Option<Vec<f64>> {
    let req = backend_request(b, "POST", "/v1/embeddings", Some(&json!({"model": model, "input": [text]})))?;
    let work = async {
        let resp = client().request(req).await.ok()?;
        let status = resp.status().as_u16();
        let bytes = resp.into_body().collect().await.ok()?.to_bytes();
        if status >= 400 {
            return None;
        }
        let v: Value = serde_json::from_slice(&bytes).ok()?;
        let first = v.get("data")?.as_array()?.first()?;
        let e = first.get("embedding").filter(|e| !e.is_null()).cloned().unwrap_or(json!([]));
        let list: Vec<f64> = e.as_array()?.iter().map(|x| x.as_f64()).collect::<Option<_>>()?;
        (!list.is_empty()).then_some(list)
    };
    tokio::time::timeout(Duration::from_secs(300), work).await.ok().flatten()
}

fn busy() -> &'static Mutex<HashSet<i64>> {
    static B: OnceLock<Mutex<HashSet<i64>>> = OnceLock::new();
    B.get_or_init(|| Mutex::new(HashSet::new()))
}

struct Held(i64);
impl Drop for Held {
    fn drop(&mut self) {
        if let Ok(mut s) = busy().lock() {
            s.remove(&self.0);
        }
    }
}

fn refusal(code: StatusCode, m: String) -> Option<Fut> {
    let r = crate::ai_more::detail(code, m);
    Some(Box::pin(async move { r }))
}

fn answer(v: Value) -> Response<Body> {
    crate::edits::json_response(&v).unwrap_or_else(|| Response::new(Body::empty()))
}

pub(crate) fn plan(cfg: &std::sync::Arc<Config>, db: &rusqlite::Connection, p: &PageCtx, path: &str, body: &[u8]) -> Option<Fut> {
    if !p.is_gm {
        return None;
    }
    let Some(world) = p.world.as_ref() else { return refusal(StatusCode::NOT_FOUND, "Not Found".into()) };
    let wid = world.get("id")?.as_i64()?;
    let vault_path = match world.get("obsidian_vault_path") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => strip(s),
        _ => return None,
    };
    let suggest = path.ends_with("/suggest-relations");
    let model = if suggest {
        if body.is_empty() {
            String::new()
        } else {
            match serde_json::from_slice::<Value>(body) {
                Ok(Value::Object(o)) => match o.get("model") { None => String::new(), Some(Value::String(s)) => strip(s), Some(Value::Null) => "None".into(), _ => return None },
                Ok(_) => return None,
                Err(_) => String::new(),
            }
        }
    } else {
        String::new()
    };
    if vault_path.is_empty() {
        return refusal(StatusCode::BAD_REQUEST, "This world has no Obsidian vault path configured.".into());
    }
    let root = Path::new(&vault_path);
    if !root.is_dir() {
        return refusal(StatusCode::BAD_REQUEST, format!("Vault path does not exist or is not a directory: {vault_path}"));
    }
    let b = backend(db)?;
    let map = name_map(db, wid)?;
    if suggest {
        return suggest_plan(cfg, db, wid, root, b, map, model);
    }
    if !busy().lock().ok()?.insert(wid) {
        return Some(Box::pin(async move {
            answer(json!({"notes": 0, "chunks": 0, "edges": 0, "unmatched_notes": 0, "vault_path": vault_path, "busy": true}))
        }));
    }
    let held = Held(wid);
    let notes = vault_notes(root);
    // chunks (path, heading, text) and edges (source, target, relation, path), in vault order
    let (mut chunks, mut edges, mut unmatched): (Vec<(String, String, String)>, Vec<(i64, i64, String, String)>, Vec<String>) = (vec![], vec![], vec![]);
    for (rel, raw) in &notes {
        let (fm, body) = parse_frontmatter(raw);
        let (title, source) = match_note(&map, rel, &fm, &body);
        if source.is_none() {
            unmatched.push(rel.clone());
        }
        if let Some(src) = source {
            for (key, value) in &fm {
                if ["title", "tags", "kind", "aliases"].contains(&key.as_str()) {
                    continue;
                }
                let values: Vec<&String> = match value { Fm::One(s) => vec![s], Fm::Many(l) => l.iter().collect() };
                for v in values {
                    for link in extract_wikilinks(v) {
                        if let Some(&t) = map.get(&link.to_lowercase()).filter(|&&t| t != src) {
                            edges.push((src, t, key.clone(), rel.clone()));
                        }
                    }
                }
            }
            for link in extract_wikilinks(&body) {
                if let Some(&t) = map.get(&link.to_lowercase()).filter(|&&t| t != src) {
                    edges.push((src, t, "mentions".into(), rel.clone()));
                }
            }
        }
        for (heading, text) in crate::retrieval::rules_sections(&body) {
            let text = strip(&text);
            if text.is_empty() {
                continue;
            }
            let shown = if heading == "Introduction" || heading == "Rules" { title.clone() } else { heading };
            chunks.push((rel.clone(), shown, text));
        }
    }
    let (cfg, n_notes) = (cfg.clone(), notes.len());
    Some(Box::pin(async move {
        let _held = held;
        let model = embed_model(&cfg);
        let mut failures = 0usize;
        let mut rows: Vec<(String, String, String, Option<String>)> = vec![];
        for (rel, heading, text) in chunks {
            let emb = embed(&b, &model, &text).await.map(|v| pack_embedding(&v));
            if emb.is_none() {
                failures += 1;
            }
            rows.push((rel, heading, text, emb));
        }
        if !rows.is_empty() && failures == rows.len() {
            return crate::ai_more::crash();
        }
        let n_chunks = rows.len();
        let n_edges = edges.len();
        let cfg2 = cfg.clone();
        let wrote = tokio::task::spawn_blocking(move || -> Option<()> {
            use rusqlite::types::Value as Sql;
            let db = crate::writes::open(&cfg2)?;
            db.execute_batch("BEGIN IMMEDIATE").ok()?;
            let r = (|| -> Option<()> {
                db.execute("DELETE FROM vault_chunks WHERE world_id = ?1", [wid]).ok()?;
                db.execute("DELETE FROM entity_relations WHERE world_id = ?1 AND source_path IS NOT NULL", [wid]).ok()?;
                for (rel, heading, text, emb) in &rows {
                    crate::orm::insert(&db, "vault_chunks", &[("world_id", Sql::Integer(wid)), ("source_path", Sql::Text(rel.clone())), ("heading", Sql::Text(heading.clone())),
                        ("text", Sql::Text(text.clone())), ("embedding", emb.clone().map_or(Sql::Null, Sql::Text))])?;
                }
                for (s, t, rel, path) in &edges {
                    crate::orm::insert(&db, "entity_relations", &[("world_id", Sql::Integer(wid)), ("source_id", Sql::Integer(*s)), ("target_id", Sql::Integer(*t)),
                        ("relation", Sql::Text(rel.clone())), ("source_path", Sql::Text(path.clone()))])?;
                }
                Some(())
            })();
            if r.is_some() && db.execute_batch("COMMIT").is_ok() {
                Some(())
            } else {
                let _ = db.execute_batch("ROLLBACK");
                None
            }
        }).await.ok().flatten();
        if wrote.is_none() {
            return crate::ai_more::crash();
        }
        answer(json!({"vault_path": vault_path, "notes": n_notes, "chunks": n_chunks, "edges": n_edges, "unmatched_notes": unmatched, "embed_failures": failures}))
    }))
}

fn suggest_plan(_cfg: &std::sync::Arc<Config>, db: &rusqlite::Connection, wid: i64, root: &Path, b: Backend, map: HashMap<String, i64>, model: String) -> Option<Fut> {
    let rows: Vec<(i64, Option<String>)> = db.prepare("SELECT id, name FROM entities WHERE world_id = ?1").ok()?.query_map([wid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?;
    let mut id_by_name: HashMap<String, i64> = HashMap::new();
    for (id, name) in rows {
        if let Some(n) = name.filter(|n| !n.is_empty()) {
            id_by_name.insert(n, id);
        }
    }
    let mut known: Vec<String> = id_by_name.keys().cloned().collect();
    known.sort();
    if known.len() < 2 {
        return Some(Box::pin(async { answer(json!({"suggestions": []})) }));
    }
    let existing: HashSet<(i64, i64, String)> = db.prepare("SELECT source_id, target_id, relation FROM entity_relations WHERE world_id = ?1").ok()?
        .query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?
        .into_iter().map(|(s, t, r)| (s, t, strip(&r).to_lowercase())).collect();
    let mut todo: Vec<(String, String, i64)> = vec![];
    for (rel, raw) in vault_notes(root) {
        let (fm, body) = parse_frontmatter(&raw);
        let (_, source) = match_note(&map, &rel, &fm, &body);
        if let Some(src) = source.filter(|_| !strip(&body).is_empty()) {
            todo.push((rel, body, src));
        }
    }
    Some(Box::pin(async move {
        let model = if model.is_empty() { b.model.clone() } else { model };
        let schema = json!({"type": "object", "properties": {"relations": {"type": "array", "items": {"type": "object", "properties": {
            "source": {"type": "string", "enum": known}, "target": {"type": "string", "enum": known}, "relation": {"type": "string"}},
            "required": ["source", "target", "relation"]}}}, "required": ["relations"]});
        let system = RELATION_SYSTEM.to_string();
        let known_set: HashSet<&String> = known.iter().collect();
        let (mut out, mut seen): (Vec<Value>, HashSet<(i64, i64, String)>) = (vec![], HashSet::new());
        for (rel, body, src) in todo {
            let msgs = [json!({"role": "system", "content": system}), json!({"role": "user", "content": body})];
            let Ok(reply) = crate::ai::chat_with_schema(&b, &model, &msgs, false, &schema).await else { continue };
            let Some(Value::Array(relations)) = serde_json::from_str::<Value>(&reply).ok().and_then(|v| v.get("relations").cloned()) else { continue };
            for r in relations {
                let Value::Object(r) = r else { continue };
                let get = |k: &str| match r.get(k) { None | Some(Value::Null) => String::new(), Some(Value::String(s)) => strip(s), Some(o) => strip(&o.to_string()) };
                let (s, t, relation) = (get("source"), get("target"), get("relation"));
                if s.is_empty() || t.is_empty() || relation.is_empty() || s == t || !known_set.contains(&s) || !known_set.contains(&t) {
                    continue;
                }
                let (sid, tid) = (id_by_name[&s], id_by_name[&t]);
                if src != sid && src != tid {
                    continue;
                }
                let key = (sid, tid, strip(&relation).to_lowercase());
                if existing.contains(&key) || !seen.insert(key) {
                    continue;
                }
                out.push(json!({"source_id": sid, "source_name": s, "target_id": tid, "target_name": t, "relation": relation, "source_path": rel}));
            }
        }
        answer(json!({"suggestions": out}))
    }))
}

/// `GET /api/ai/test-chat`: a one-word probe of the model the backend would answer with
pub async fn test_chat(cfg: std::sync::Arc<Config>, uri: axum::http::Uri, headers: axum::http::HeaderMap) -> Option<Response<Body>> {
    let (cfg2, uri2, h2) = (cfg.clone(), uri.clone(), headers.clone());
    let b = tokio::task::spawn_blocking(move || {
        let db = crate::writes::open(&cfg2)?;
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs();
        let p = crate::layout::build(&db, &cfg2, "GET", &uri2, &h2, now)?;
        if !p.is_gm {
            return None;
        }
        backend(&db)
    }).await.ok()??;
    let requested = crate::pages::query_param(&uri, "model").unwrap_or_default();
    let (resolved, note) = crate::ai::resolve_model(&b, &requested).await;
    let mut extra = Map::new();
    extra.insert("num_predict".into(), json!(16));
    let result = crate::ai::generate_chat(&b, &resolved, "", &[json!({"role": "user", "content": "Say only the word OK."})], false, &extra).await;
    crate::edits::json_response(&json!({"requested": requested, "resolved": resolved, "note": note, "result": result}))
}

/// `POST /api/ai/unload`, `/api/ai/pull`, `/api/ai/benchmark` on the Unsloth backend (Ollama's own calls have no equivalent there)
pub(crate) fn ollama_plan(db: &rusqlite::Connection, p: &PageCtx, path: &str, body: &[u8]) -> Option<Fut> {
    if !p.is_gm {
        return None;
    }
    let b = backend(db)?;
    let Value::Object(o) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    match path {
        "/api/ai/unload" => {
            let Value::String(id) = o.get("model_id")? else { return None };
            let id = strip(id);
            if id.is_empty() {
                return refusal(StatusCode::BAD_REQUEST, "model_id is required".into());
            }
            refusal(StatusCode::BAD_GATEWAY, format!("Could not unload {id} — is Ollama reachable?"))
        }
        "/api/ai/pull" => {
            let Value::String(id) = o.get("model_id")? else { return None };
            let msg = format!("Model pull needs the Ollama backend. With Unsloth, download the model in Studio Model Hub instead (the id to use there: {id})");
            let text = format!("data: {}\n\ndata: [DONE]\n\n", nd_render::py_dumps(&json!({"error": msg})));
            Some(Box::pin(async move {
                Response::builder().status(StatusCode::OK).header("content-type", "text/event-stream; charset=utf-8").header("x-accel-buffering", "no")
                    .header("cache-control", "no-cache").header("x-nd-served-by", "gateway").body(Body::from(text)).unwrap()
            }))
        }
        _ => {
            let model = match o.get("model") { None => String::new(), Some(Value::String(s)) => s.clone(), _ => return None };
            Some(Box::pin(async move {
                let m = if model.is_empty() { b.model.clone() } else { model };
                let mut extra = Map::new();
                extra.insert("num_predict".into(), json!(128));
                let msgs = [json!({"role": "user", "content": "Write a two-sentence description of a rainy city street at night."})];
                match crate::ai::chat_timed(&b, &m, &msgs, &extra).await {
                    Err(e) => crate::ai_more::detail(StatusCode::BAD_GATEWAY, e),
                    Ok((eval_count, prompt_count, elapsed)) => {
                        let r1 = |x: f64| (x * 10.0).round() / 10.0;
                        let tps = if elapsed != 0 { eval_count as f64 / (elapsed as f64 / 1e9) } else { 0.0 };
                        answer(json!({"model": m, "tokens_per_sec": r1(tps), "prompt_tokens_per_sec": 0.0, "eval_count": eval_count, "eval_duration_ms": r1(elapsed as f64 / 1e6),
                            "prompt_eval_count": prompt_count, "prompt_eval_duration_ms": 0.0, "load_duration_ms": 0.0, "total_duration_ms": r1(elapsed as f64 / 1e6)}))
                    }
                }
            }))
        }
    }
}

/// the Ollama GGUF imports the page polls (`_model_imports`): id -> progress
pub(crate) fn imports() -> &'static Mutex<HashMap<String, Value>> {
    static I: OnceLock<Mutex<HashMap<String, Value>>> = OnceLock::new();
    I.get_or_init(|| Mutex::new(HashMap::new()))
}

/// `GET /api/ai/ollama/upload/status/<id>`: a finished (or failed) import is served once and forgotten
pub fn import_status(cfg: &Config, uri: &axum::http::Uri, headers: &axum::http::HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !p.is_gm || backend(&db).is_none() {
        return None;
    }
    let id = uri.path().strip_prefix("/api/ai/ollama/upload/status/")?;
    let mut map = imports().lock().ok()?;
    let Some(progress) = map.get(id).cloned() else { return Some(crate::ai_more::detail(StatusCode::NOT_FOUND, "Unknown import_id")) };
    if progress.get("status") == Some(&json!("done")) || progress.get("error").is_some() {
        map.remove(id);
    }
    answer_opt(progress)
}

fn answer_opt(v: Value) -> Option<Response<Body>> {
    crate::edits::json_response(&v)
}

/// SWARMUI_MODELS_DIR / subfolder / filename, None for anything that could leave it (`_swarmui_model_path`)
pub(crate) fn swarm_model_path(subfolder: &str, filename: &str) -> Option<std::path::PathBuf> {
    let root = std::path::PathBuf::from(std::env::var("SWARMUI_MODELS_DIR").unwrap_or_else(|_| "/data/swarmui-models".into()));
    let rel = Path::new(subfolder).join(filename);
    if rel.is_absolute() || rel.components().any(|c| matches!(c, std::path::Component::ParentDir)) {
        return None;
    }
    Some(root.join(rel))
}

/// whether SwarmUI is the configured image back end (`_get_type() == "swarmui"` with a URL)
pub(crate) fn swarm_configured(db: &rusqlite::Connection) -> bool {
    let key = db.query_row("SELECT llm_api_key FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get::<_, Option<String>>(0)).ok().flatten().filter(|k| !k.is_empty())
        .or_else(|| std::env::var("UNSLOTH_API_KEY").ok().filter(|k| !k.is_empty()));
    if key.is_some() && std::env::var("UNSLOTH_IMAGE_MODEL").map(|m| !m.is_empty()).unwrap_or(false) {
        return false;
    }
    std::env::var("IMAGEGEN_TYPE").unwrap_or_default().to_ascii_lowercase() == "swarmui" && !std::env::var("IMAGEGEN_URL").unwrap_or_default().trim_end_matches('/').is_empty()
}

/// `POST /api/ai/imagegen/{restart,update,free-memory}` and `DELETE /api/ai/imagegen/models/downloaded` with no SwarmUI to talk to
pub fn swarm_local(cfg: &Config, method: &axum::http::Method, uri: &axum::http::Uri, headers: &axum::http::HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method.as_str(), uri, headers, now)?;
    if !p.is_gm || swarm_configured(&db) {
        return None;
    }
    if *method == axum::http::Method::POST {
        return answer_opt(json!({"ok": false}));
    }
    let q = |n: &str| crate::pages::query_param(uri, n).unwrap_or_default();
    let (sub, name) = (q("subfolder"), q("filename"));
    let path = if name.is_empty() { None } else { swarm_model_path(&sub, &name) };
    match path {
        Some(f) if f.is_file() => {
            std::fs::remove_file(&f).ok()?;
            answer_opt(json!({"ok": true, "model_list_refreshed": false}))
        }
        _ => Some(crate::ai_more::detail(StatusCode::NOT_FOUND, "File not found")),
    }
}

/// `POST /api/ai/imagegen/models/download`: a model file streamed into SWARMUI_MODELS_DIR, progress as SSE
pub(crate) fn swarm_download_plan(db: &rusqlite::Connection, p: &PageCtx, body: &[u8]) -> Option<Fut> {
    if !p.is_gm || swarm_configured(db) {
        return None;
    }
    let Value::Object(o) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let Value::String(url) = o.get("url")? else { return None };
    let text = |k: &str| match o.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(s.clone()), _ => None };
    let (sub, mut filename) = (text("subfolder")?, text("filename")?);
    let url = strip(url);
    Some(Box::pin(async move {
        let (tx, rx) = tokio::sync::mpsc::channel::<String>(8);
        tokio::spawn(async move {
            let send = |v: Value| crate::ai::sse(&v);
            let finish = |tx: &tokio::sync::mpsc::Sender<String>, v: Value| {
                let tx = tx.clone();
                async move {
                    let _ = tx.send(crate::ai::sse(&v)).await;
                    let _ = tx.send("data: [DONE]\n\n".to_string()).await;
                }
            };
            let _ = &send;
            if url.is_empty() {
                return finish(&tx, json!({"error": "No URL given"})).await;
            }
            if filename.is_empty() {
                let no_q = url.split(['?', '#']).next().unwrap_or("");
                let after = no_q.split_once("://").map_or(no_q, |(_, r)| r);
                let path = after.find('/').map_or("", |i| &after[i..]);
                filename = path.trim_end_matches('/').rsplit('/').next().unwrap_or("").to_string();
            }
            if filename.is_empty() || filename.contains('/') || filename.contains('\\') {
                return finish(&tx, json!({"error": "Could not determine a safe filename from that URL — provide one explicitly"})).await;
            }
            let Some(dest) = swarm_model_path(&sub, &filename) else { return finish(&tx, json!({"error": "Invalid subfolder/filename"})).await };
            if let Some(parent) = dest.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            let tmp = dest.with_file_name(format!("{}.part", dest.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default()));
            let result = swarm_fetch(&url, &tmp, &tx).await;
            match result {
                Ok(()) => {
                    if std::fs::rename(&tmp, &dest).is_err() {
                        let _ = std::fs::remove_file(&tmp);
                        return finish(&tx, json!({"error": "OSError: could not move the file into place"})).await;
                    }
                    let bytes = std::fs::metadata(&dest).map(|m| m.len()).unwrap_or(0);
                    finish(&tx, json!({"status": "done", "subfolder": sub, "filename": filename, "bytes": bytes, "model_list_refreshed": false})).await;
                }
                Err(e) => {
                    let _ = std::fs::remove_file(&tmp);
                    finish(&tx, json!({"error": e})).await;
                }
            }
        });
        crate::ai::stream_response(rx)
    }))
}

async fn swarm_fetch(url: &str, tmp: &Path, tx: &tokio::sync::mpsc::Sender<String>) -> Result<(), String> {
    use http_body_util::BodyExt;
    use tokio::io::AsyncWriteExt;
    let mut current: hyper::Uri = url.parse().map_err(|e| format!("InvalidURL: {e}"))?;
    let mut resp = None;
    for _ in 0..20 {
        let req = hyper::Request::builder().method("GET").uri(current.clone()).header("user-agent", "python-httpx/0.27").body(http_body_util::Full::new(bytes::Bytes::new())).map_err(|e| format!("InvalidURL: {e}"))?;
        let r = tokio::time::timeout(Duration::from_secs(60), client().request(req)).await.map_err(|_| "ConnectTimeout: timed out".to_string())?.map_err(|e| format!("ConnectError: {e}"))?;
        let status = r.status().as_u16();
        if matches!(status, 301 | 302 | 303 | 307 | 308) {
            if let Some(loc) = r.headers().get("location").and_then(|v| v.to_str().ok()) {
                current = crate::studio_key::resolve_location(&current, loc)?;
                continue;
            }
        }
        resp = Some(r);
        break;
    }
    let resp = resp.ok_or_else(|| "TooManyRedirects: Exceeded maximum allowed redirects.".to_string())?;
    if resp.status().as_u16() >= 400 {
        return Err(format!("HTTP {} fetching model file", resp.status().as_u16()));
    }
    let total: u64 = resp.headers().get("content-length").and_then(|v| v.to_str().ok()).and_then(|v| v.parse().ok()).unwrap_or(0);
    let mut file = tokio::fs::File::create(tmp).await.map_err(|e| format!("OSError: {e}"))?;
    let (mut completed, mut pending) = (0u64, 0usize);
    let mut body = resp.into_body();
    loop {
        let frame = match tokio::time::timeout(Duration::from_secs(60), body.frame()).await {
            Err(_) => return Err("ReadTimeout: timed out".into()),
            Ok(None) => break,
            Ok(Some(Err(e))) => return Err(format!("ReadError: {e}")),
            Ok(Some(Ok(f))) => f,
        };
        if let Some(data) = frame.data_ref() {
            file.write_all(data).await.map_err(|e| format!("OSError: {e}"))?;
            completed += data.len() as u64;
            pending += data.len();
            if pending >= 1024 * 1024 {
                pending = 0;
                if tx.send(crate::ai::sse(&json!({"total": total, "completed": completed}))).await.is_err() {
                    return Err("client went away".into());
                }
            }
        }
    }
    file.flush().await.map_err(|e| format!("OSError: {e}"))?;
    Ok(())
}
