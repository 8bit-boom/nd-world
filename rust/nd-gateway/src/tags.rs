//! The image prompt tag lists (app/routers/ai.py "Tag sources"): `GET /api/ai/imagegen/tags[/sources|/status]` and `POST .../tags/fetch|activate|delete`.
//! Answered here only without Python (ND_PYTHON_FREE): next to Python its process keeps a parsed-list cache this one could not keep fresh, so a
//! hybrid install leaves all of it to Python. The files are Python's own: `<data>/tags/<id>_tags.csv`, `active_source.txt`, `custom_sources.json`.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode, Uri};
use serde_json::{json, Value};

use crate::config::Config;

type Rows = Arc<Vec<(String, i64, i64)>>;

fn cache() -> &'static Mutex<HashMap<String, Rows>> {
    static C: OnceLock<Mutex<HashMap<String, Rows>>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(HashMap::new()))
}

fn builtin() -> Vec<Value> {
    vec![
        json!({"id": "danbooru", "label": "Danbooru", "description": "General anime/art tags sorted by usage \u{b7} SDXL optimised \u{b7} ~4 MB",
               "url": "https://github.com/BetaDoggo/danbooru-tag-list/releases/download/Model-Tags/ChenkinNoob-XL-V0.3_underscore.csv", "swarmui_name": "danbooru",
               "cat_colors": {"0": "#aaa", "1": "#c0a060", "3": "#a060c0", "4": "#60a0c0", "5": "#60c080"}}),
        json!({"id": "e621", "label": "e621", "description": "Furry / anthro art tags from e621 \u{b7} ~3 MB",
               "url": "https://github.com/DominikDoom/a1111-sd-webui-tagcomplete/raw/main/tags/e621.csv", "swarmui_name": "e621",
               "cat_colors": {"0": "#aaa", "1": "#60c080", "3": "#a060c0", "4": "#60a0c0"}}),
    ]
}

fn dir(cfg: &Config) -> Option<PathBuf> {
    Some(cfg.db_path.parent()?.join("tags"))
}

fn tag_file(cfg: &Config, id: &str) -> Option<PathBuf> {
    Some(dir(cfg)?.join(format!("{id}_tags.csv")))
}

/// custom_sources.json as Python reads it: a list is appended after the built-ins; anything unreadable is ignored
fn custom_sources(cfg: &Config) -> Vec<Value> {
    let Some(d) = dir(cfg) else { return vec![] };
    match std::fs::read_to_string(d.join("custom_sources.json")).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
        Some(Value::Array(a)) => a,
        _ => vec![],
    }
}

/// every source; None when a custom entry has no text id / label (Python's KeyError, a 500)
fn all_sources(cfg: &Config) -> Option<Vec<Value>> {
    let mut v = builtin();
    v.extend(custom_sources(cfg));
    for s in &v {
        s.get("id")?.as_str()?;
        s.get("label")?;
    }
    Some(v)
}

fn active_id(cfg: &Config) -> Option<String> {
    let d = dir(cfg)?;
    if let Ok(t) = std::fs::read_to_string(d.join("active_source.txt")) {
        let t = nd_render::text::py_strip(&t);
        if !t.is_empty() {
            return Some(t.to_string());
        }
    }
    Some(if tag_file(cfg, "danbooru")?.exists() { "danbooru".into() } else { String::new() })
}

fn csv_rows(text: &str) -> Vec<Vec<String>> {
    // csv.reader: quoted fields ("" is a quote), \n or \r\n ends a row
    let mut rows = vec![];
    let mut row: Vec<String> = vec![];
    let mut field = String::new();
    let mut quoted = false;
    let mut chars = text.chars().peekable();
    let mut any = false;
    while let Some(ch) = chars.next() {
        any = true;
        if quoted {
            if ch == '"' {
                if chars.peek() == Some(&'"') {
                    chars.next();
                    field.push('"');
                } else {
                    quoted = false;
                }
            } else {
                field.push(ch);
            }
            continue;
        }
        match ch {
            '"' if field.is_empty() => quoted = true,
            ',' => row.push(std::mem::take(&mut field)),
            '\r' => {}
            '\n' => {
                row.push(std::mem::take(&mut field));
                rows.push(std::mem::take(&mut row));
                any = false;
            }
            c => field.push(c),
        }
    }
    if any {
        row.push(field);
        rows.push(row);
    }
    rows
}

fn py_int(s: &str) -> Option<i64> {
    let t = nd_render::text::py_strip(s);
    t.parse::<i64>().ok()
}

fn load(cfg: &Config, id: &str) -> Option<Rows> {
    if let Some(r) = cache().lock().ok()?.get(id) {
        return Some(r.clone());
    }
    let f = tag_file(cfg, id)?;
    if !f.exists() {
        return Some(Arc::new(vec![]));
    }
    let text = String::from_utf8(std::fs::read(&f).ok()?).ok()?;
    let mut out = vec![];
    for row in csv_rows(text.trim_start_matches('\u{feff}').trim_start_matches('\u{feff}')) {
        if row.len() < 2 {
            continue;
        }
        let Some(cat) = py_int(&row[1]) else { continue };
        let cnt = if row.len() > 2 {
            match py_int(&row[2]) {
                Some(c) => c,
                None => continue,
            }
        } else {
            0
        };
        out.push((row[0].clone(), cat, cnt));
    }
    let rows = Arc::new(out);
    cache().lock().ok()?.insert(id.to_string(), rows.clone());
    Some(rows)
}

fn gm(cfg: &Config, method: &str, uri: &Uri, headers: &HeaderMap) -> Option<()> {
    let db = crate::writes::open(cfg)?;
    let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method, uri, headers, n)?;
    p.is_gm.then_some(())
}

/// The three GET routes, without Python
pub fn get(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    gm(cfg, "GET", uri, headers)?;
    let active = active_id(cfg)?;
    let body = match uri.path() {
        "/api/ai/imagegen/tags/sources" => {
            let mut out = vec![];
            for s in all_sources(cfg)? {
                let id = s["id"].as_str()?.to_string();
                let downloaded = tag_file(cfg, &id)?.exists();
                let count = if downloaded { load(cfg, &id)?.len() } else { 0 };
                let builtin = builtin().iter().any(|b| b["id"] == s["id"]);
                out.push(json!({"id": id, "label": s["label"], "description": s.get("description").cloned().unwrap_or(json!("")), "builtin": builtin,
                                "downloaded": downloaded, "count": count, "active": id == active}));
            }
            json!({"sources": out, "active": active})
        }
        "/api/ai/imagegen/tags/status" => {
            if active.is_empty() {
                json!({"loaded": false, "count": 0, "active": ""})
            } else {
                let n = load(cfg, &active)?.len();
                json!({"loaded": n > 0, "count": n, "active": active})
            }
        }
        _ => {
            let q = crate::pages::query_param(uri, "q").unwrap_or_default();
            let limit = match crate::pages::query_param(uri, "limit") {
                None => 25,
                Some(l) if !l.is_empty() && l.bytes().all(|c| c.is_ascii_digit()) && l.len() <= 9 => l.parse::<i64>().ok()?,
                Some(_) => return None,
            };
            if q.is_empty() || active.is_empty() {
                return crate::edits::json_response(&json!({"tags": []}));
            }
            if limit < 1 {
                return None;
            }
            let rows = load(cfg, &active)?;
            let colors = all_sources(cfg)?.into_iter().find(|s| s["id"].as_str() == Some(active.as_str())).and_then(|s| s.get("cat_colors").cloned());
            let colors = match colors {
                None => json!({"0": "#aaa"}),
                Some(c) => c,
            };
            let qn = q.to_lowercase().replace(' ', "_");
            let (mut exact, mut prefix) = (vec![], vec![]);
            for t in rows.iter() {
                if t.0 == qn {
                    exact.push(t);
                } else if t.0.starts_with(&qn) {
                    prefix.push(t);
                }
                if (exact.len() + prefix.len()) as i64 >= limit * 3 {
                    break;
                }
            }
            let tags: Vec<Value> = exact.into_iter().chain(prefix).take(limit as usize)
                .map(|t| json!({"tag": t.0, "category": t.1, "count": t.2, "color": colors.get(t.1.to_string()).cloned().unwrap_or(json!("#aaa"))}))
                .collect();
            json!({"tags": tags})
        }
    };
    crate::edits::json_response(&body)
}

fn write_custom(cfg: &Config, custom: &[Value]) {
    if let Some(d) = dir(cfg) {
        let _ = std::fs::write(d.join("custom_sources.json"), nd_render::py_dumps(&Value::Array(custom.to_vec())));
    }
}

fn safe_id(id: &str) -> bool {
    !id.is_empty() && !id.contains(['/', '\\', '\0']) && id != "." && id != ".."
}

/// `POST .../tags/fetch|activate|delete`, without Python
pub async fn post(cfg: Arc<Config>, uri: Uri, headers: HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let c = cfg.clone();
    let (u, h) = (uri.clone(), headers.clone());
    tokio::task::spawn_blocking(move || gm(&c, "POST", &u, &h)).await.ok()??;
    let parsed: Value = serde_json::from_slice(body).ok().filter(|_| crate::edits::plain_numbers(body))?;
    let obj = parsed.as_object()?;
    let text = |k: &str, default: &str| -> Option<String> {
        match obj.get(k) {
            None => Some(default.to_string()),
            Some(Value::String(s)) => Some(s.clone()),
            _ => None,
        }
    };
    let ok = |v: Value| crate::edits::json_response(&v);
    match uri.path() {
        "/api/ai/imagegen/tags/activate" => {
            let id = text("source_id", "\u{0}")?;
            if id == "\u{0}" || !safe_id(&id) {
                return None;
            }
            let f = tag_file(&cfg, &id)?;
            if !f.exists() {
                return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, &format!("Source '{id}' not downloaded yet")));
            }
            let d = dir(&cfg)?;
            std::fs::create_dir_all(&d).ok()?;
            std::fs::write(d.join("active_source.txt"), &id).ok()?;
            let n = load(&cfg, &id)?.len();
            ok(json!({"ok": true, "active": id, "count": n}))
        }
        "/api/ai/imagegen/tags/delete" => {
            let id = text("source_id", "\u{0}")?;
            if id == "\u{0}" || !safe_id(&id) {
                return None;
            }
            let f = tag_file(&cfg, &id)?;
            if f.exists() {
                std::fs::remove_file(&f).ok()?;
            }
            cache().lock().ok()?.remove(&id);
            if active_id(&cfg)? == id {
                let d = dir(&cfg)?;
                std::fs::create_dir_all(&d).ok()?;
                std::fs::write(d.join("active_source.txt"), "").ok()?;
            }
            let cf = dir(&cfg)?.join("custom_sources.json");
            if let Some(Value::Array(list)) = std::fs::read_to_string(&cf).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
                if list.iter().all(|s| s.get("id").is_some()) {
                    let kept: Vec<Value> = list.into_iter().filter(|s| s["id"] != json!(id)).collect();
                    write_custom(&cfg, &kept);
                }
            }
            ok(json!({"ok": true}))
        }
        _ => {
            let source_id = { let s = text("source_id", "danbooru")?; let s = nd_render::text::py_strip(&s).to_string(); if s.is_empty() { "danbooru".to_string() } else { s } };
            let url_in = nd_render::text::py_strip(&text("url", "")?).to_string();
            let label_in = nd_render::text::py_strip(&text("label", "")?).to_string();
            let mut source = all_sources(&cfg)?.into_iter().find(|s| s["id"].as_str() == Some(source_id.as_str()));
            let fetch_url = if !url_in.is_empty() { url_in } else { source.as_ref().and_then(|s| s["url"].as_str()).unwrap_or("").to_string() };
            if fetch_url.is_empty() {
                return ok(json!({"ok": false, "error": "No URL for this source"}));
            }
            if !safe_id(&source_id) {
                return ok(json!({"ok": false, "error": "Invalid source id"}));
            }
            let d = dir(&cfg)?;
            std::fs::create_dir_all(&d).ok()?;
            if source.is_none() {
                let label = if label_in.is_empty() { source_id.clone() } else { label_in };
                let mut custom = match std::fs::read_to_string(d.join("custom_sources.json")).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
                    Some(Value::Array(a)) => a,
                    _ => vec![],
                };
                if !custom.iter().any(|s| s["id"] == json!(source_id)) {
                    custom.push(json!({"id": source_id, "label": label, "description": "Custom", "url": fetch_url, "swarmui_name": source_id, "cat_colors": {}}));
                    write_custom(&cfg, &custom);
                }
                source = all_sources(&cfg)?.into_iter().find(|s| s["id"].as_str() == Some(source_id.as_str()));
            }
            let got = crate::kiy::fetch(&fetch_url, Duration::from_secs(120), "nd-world/1.0").await;
            let data = match got {
                Some((status, data)) if status < 400 => data,
                Some((status, _)) => return ok(json!({"ok": false, "error": format!("HTTP Error {status}: {}", reason(status))})),
                None => return ok(json!({"ok": false, "error": "<urlopen error unreachable>"})),
            };
            let tf = tag_file(&cfg, &source_id)?;
            std::fs::write(&tf, &data).ok()?;
            let ac = std::env::var("SWARMUI_AC_DIR").unwrap_or_else(|_| "/data/swarmui-ac".into());
            let ac = if ac.is_empty() { None } else { Some(PathBuf::from(ac)).filter(|p| p.exists()) };
            if let Some(ac) = &ac {
                let name = source.as_ref().and_then(|s| s["swarmui_name"].as_str()).unwrap_or(&source_id).to_string();
                if safe_id(&name) {
                    let _ = std::fs::write(ac.join(format!("{name}.csv")), &data);
                }
            }
            cache().lock().ok()?.remove(&source_id);
            let n = load(&cfg, &source_id)?.len();
            if active_id(&cfg)?.is_empty() {
                std::fs::write(d.join("active_source.txt"), &source_id).ok()?;
            }
            ok(json!({"ok": true, "bytes": data.len(), "count": n, "swarmui_ac": ac.is_some()}))
        }
    }
}

fn reason(status: u16) -> &'static str {
    StatusCode::from_u16(status).ok().and_then(|s| s.canonical_reason()).unwrap_or("")
}
