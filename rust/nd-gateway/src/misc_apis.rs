//! Small JSON routes: `GET /health` (app/main.py), the Ollama preset list (`/api/ai/presets`, app/routers/ai.py, stored in ai_models.json
//! beside the database) and the dashboard's world-summary state (`GET /api/ai/world-summary`).

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Method, Response, StatusCode, Uri};
use serde_json::{json, Map, Value};

use crate::config::Config;

/// `/api/npc-talk/<id>/history`: the id (plain digits)
fn npc_history_id(path: &str) -> Option<i64> {
    let d = path.strip_prefix("/api/npc-talk/")?.strip_suffix("/history")?;
    (!d.is_empty() && d.len() < 16 && d.bytes().all(|b| b.is_ascii_digit()) && !(d.len() > 1 && d.starts_with('0'))).then(|| d.parse().ok())?
}

/// `/api/characters/<id>/share` and `.../hub/sessions`: the id (plain digits)
fn character_tail(path: &str, tail: &str) -> Option<i64> {
    let d = path.strip_prefix("/api/characters/")?.strip_suffix(tail)?;
    (!d.is_empty() && d.len() < 16 && d.bytes().all(|b| b.is_ascii_digit()) && !(d.len() > 1 && d.starts_with('0'))).then(|| d.parse().ok())?
}

pub fn serves(method: &Method, path: &str) -> bool {
    if *method == Method::GET && character_tail(path, "/share").is_some() {
        return true;
    }
    if npc_history_id(path).is_some() {
        return *method == Method::GET || *method == Method::DELETE;
    }
    match path {
        "/health" | "/api/ai/world-summary" | "/api/ai/world-context" => *method == Method::GET,
        "/api/ai/save-note" => *method == Method::POST,
        "/api/ai/presets" => *method == Method::GET || *method == Method::POST,
        "/api/ai/unsloth/prefs" => *method == Method::GET,
        _ => *method == Method::DELETE && path.strip_prefix("/api/ai/presets/").is_some_and(|l| !l.is_empty() && !l.contains('/')),
    }
}

fn models_path(cfg: &Config) -> Option<std::path::PathBuf> {
    Some(cfg.db_path.parent()?.join("ai_models.json"))
}

/// `_load_data()`: the file's object, or the default for a missing / unreadable one; None for a file Python would fail on
fn load_data(cfg: &Config) -> Option<Map<String, Value>> {
    let default = || serde_json::from_value::<Map<String, Value>>(json!({"custom": [], "hidden": []})).ok();
    let Ok(text) = std::fs::read_to_string(models_path(cfg)?) else { return default() };
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(o)) => Some(o),
        Ok(_) => None, // a list or the like: `.get` raises in Python
        Err(_) if text.contains("NaN") || text.contains("Infinity") => None,
        Err(_) => default(),
    }
}

fn save_data(cfg: &Config, data: &Map<String, Value>) -> Option<()> {
    std::fs::write(models_path(cfg)?, nd_render::py_dumps_indent(&Value::Object(data.clone()), 2)).ok()
}

/// `list_presets()`: the "presets" list, [] when absent; None when it is something else
fn presets(data: &Map<String, Value>) -> Option<Vec<Value>> {
    match data.get("presets") {
        None => Some(vec![]),
        Some(Value::Array(a)) => Some(a.clone()),
        Some(_) => None,
    }
}

const OPTIONS: &[(&str, char, Option<f64>, Option<f64>)] = &[
    ("temperature", 'f', Some(0.0), Some(2.0)), ("top_p", 'f', Some(0.0), Some(1.0)), ("top_k", 'i', Some(0.0), None),
    ("repeat_penalty", 'f', Some(0.0), Some(5.0)), ("num_predict", 'i', Some(-2.0), None), ("num_ctx", 'i', Some(1.0), None),
    ("seed", 'i', None, None), ("mirostat", 'i', Some(0.0), Some(2.0)), ("mirostat_tau", 'f', Some(0.0), Some(100.0)), ("mirostat_eta", 'f', Some(0.0), Some(10.0)),
    ("num_gpu", 'i', Some(0.0), Some(1000.0)), ("min_p", 'f', Some(0.0), Some(1.0)), ("typical_p", 'f', Some(0.0), Some(1.0)),
    ("repeat_last_n", 'i', Some(-1.0), Some(131072.0)), ("presence_penalty", 'f', Some(-2.0), Some(2.0)), ("frequency_penalty", 'f', Some(-2.0), Some(2.0)),
    ("num_keep", 'i', Some(0.0), Some(131072.0)), ("num_batch", 'i', Some(1.0), Some(4096.0)), ("num_thread", 'i', Some(0.0), Some(256.0)),
    ("main_gpu", 'i', Some(0.0), Some(15.0)), ("use_mmap", 'b', None, None),
];

/// `_clamp_options(raw)`: the allowed keys whose value converts and lies in range, in the order sent; None for a string value (int("5")
/// reads it its own way: Python's)
fn clamp_options(raw: &Map<String, Value>) -> Option<Map<String, Value>> {
    let mut out = Map::new();
    for (key, val) in raw {
        let Some(&(_, kind, lo, hi)) = OPTIONS.iter().find(|o| o.0 == key) else { continue };
        if val.is_null() {
            continue;
        }
        let v: f64;
        let shown: Value = match (kind, val) {
            ('b', x) => {
                out.insert(key.clone(), json!(crate::edits::py_truthy_pub(Some(x))));
                continue;
            }
            (_, Value::String(_)) => return None,
            (_, Value::Array(_) | Value::Object(_)) => continue, // TypeError: skipped
            (_, Value::Bool(b)) => {
                v = *b as i64 as f64;
                if kind == 'i' { json!(*b as i64) } else { json!(v) }
            }
            ('i', Value::Number(n)) => {
                if let Some(i) = n.as_i64() {
                    v = i as f64;
                    json!(i)
                } else {
                    let f = n.as_f64()?;
                    if !f.is_finite() || f.abs() >= 9e15 {
                        return None;
                    }
                    v = f.trunc();
                    json!(f.trunc() as i64)
                }
            }
            (_, Value::Number(n)) => {
                v = n.as_f64()?;
                json!(v)
            }
            _ => continue,
        };
        // num_ctx stops at the app's own auto-sizing ceiling: max(8192, MAX_AUTO_NUM_CTX), 32768 unless the environment says otherwise
        let hi = if key == "num_ctx" {
            let n = std::env::var("MAX_AUTO_NUM_CTX").ok().map(|s| nd_render::text::py_strip(&s).parse::<i64>().ok()).unwrap_or(Some(32768))?;
            Some(n.max(8192) as f64)
        } else { hi };
        if lo.is_some_and(|l| v < l) || hi.is_some_and(|h| v > h) {
            continue;
        }
        out.insert(key.clone(), shown);
    }
    Some(out)
}

pub fn handle(cfg: &Config, method: &Method, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let path = uri.path();
    if path == "/health" {
        let build = std::env::var("ND_BUILD").unwrap_or_default();
        let build: String = nd_render::text::py_strip(&build).chars().take(7).collect();
        return crate::edits::json_response(&json!({"status": "ok", "build": if build.is_empty() { "dev".to_string() } else { build }}));
    }
    if path == "/api/ai/world-summary" {
        return world_summary(cfg, uri, headers);
    }
    if path == "/api/ai/world-context" {
        return world_context(cfg, uri, headers);
    }
    if path == "/api/ai/save-note" {
        return save_note(cfg, uri, headers, body);
    }
    if let Some(id) = character_tail(path, "/share") {
        return share_state(cfg, uri, headers, id);
    }
    if let Some(eid) = npc_history_id(path) {
        return npc_history(cfg, method, uri, headers, eid);
    }
    // the presets: the GM's (the auth gate refuses anyone else before this)
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method.as_str(), uri, headers, now)?;
    if !p.is_gm {
        return None;
    }
    let mut data = load_data(cfg)?;
    if path == "/api/ai/unsloth/prefs" {
        // the TTS / STT values stored beside the database: `value or default` (a stored non-string that is truthy comes back as it is)
        let get = |k: &str, default: &str| match data.get(k) {
            Some(v) if crate::edits::py_truthy_pub(Some(v)) => v.clone(),
            _ => json!(default),
        };
        return crate::edits::json_response(&json!({
            "tts_model": get("tts_model", "unsloth/orpheus-3b"), "tts_voice": get("tts_voice", ""),
            "tts_instructions": get("tts_instructions", ""), "tts_language": get("tts_language", ""),
            "stt_model": get("stt_model", "small"), "studio_console_url": get("studio_console_url", ""),
        }));
    }
    match *method {
        Method::GET => crate::edits::json_response(&json!({"presets": presets(&data)?})),
        Method::POST => {
            let ct = crate::form::content_type(headers);
            if ct != "application/json" {
                return None;
            }
            let b = match serde_json::from_slice::<Value>(body).ok()? {
                Value::Object(o) if crate::edits::plain_numbers(body) => o,
                _ => return None,
            };
            let text = |k: &str| -> Option<String> {
                match b.get(k) {
                    None => Some(String::new()),
                    Some(Value::String(s)) => Some(nd_render::text::py_strip(s).to_string()),
                    _ => None, // pydantic's 422
                }
            };
            let Some(Value::String(label)) = b.get("label") else { return None };
            let label = nd_render::text::py_strip(label).to_string();
            let (model, extra) = (text("model")?, text("system_extra")?);
            let options = match b.get("options") {
                None => Map::new(),
                Some(Value::Object(o)) => o.clone(),
                _ => return None,
            };
            if label.is_empty() {
                return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, "label is required"));
            }
            let clamped = clamp_options(&options)?;
            let mut list = presets(&data)?;
            if list.iter().any(|x| !x.is_object()) {
                return None;
            }
            list.retain(|x| x.get("label") != Some(&json!(label)));
            list.push(json!({"label": label, "model": model, "system_extra": extra, "options": clamped}));
            data.insert("presets".into(), Value::Array(list.clone()));
            save_data(cfg, &data)?;
            crate::edits::json_response(&json!({"ok": true, "presets": list}))
        }
        _ => {
            let raw = path.strip_prefix("/api/ai/presets/")?;
            let label = percent_encoding::percent_decode_str(raw).decode_utf8().ok()?.to_string();
            if label.contains('/') {
                return None;
            }
            let mut list = presets(&data)?;
            if list.iter().any(|x| !x.is_object()) {
                return None;
            }
            list.retain(|x| x.get("label") != Some(&json!(label)));
            data.insert("presets".into(), Value::Array(list.clone()));
            save_data(cfg, &data)?;
            crate::edits::json_response(&json!({"ok": true, "presets": list}))
        }
    }
}

/// `api_world_summary_get`: the newest summary job of the viewer's own audience tier
fn world_summary(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let Some(world) = p.world.clone() else { return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, "No active world")) };
    let can_edit = p.is_gm || p.role.as_deref() == Some("assistant");
    if !can_edit && !crate::edits::py_truthy_pub(world.get("players_can_view_world_summary")) {
        return Some(crate::idpages::refusal(403));
    }
    let wid = world.get("id")?.as_i64()?;
    let audience = if p.is_gm { "audience IN ('gm', '')" } else { "audience = 'players'" };
    let job = db.query_row(&format!("SELECT * FROM audio_jobs WHERE world_id = ?1 AND purpose = 'world_summary' AND {audience} ORDER BY id DESC LIMIT 1"), [wid],
        |r| crate::layout::row_map(r)).ok();
    let Some(job) = job else { return crate::edits::json_response(&json!({"recap": ""})) };
    let text = |k: &str| job.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let status = text("status");
    if ["pending", "transcribing", "summarizing"].contains(&status.as_str()) {
        return crate::edits::json_response(&json!({"pending": true}));
    }
    if status == "error" {
        let e = text("error");
        return crate::edits::json_response(&json!({"recap": "", "error": if e.is_empty() { "generation failed".to_string() } else { e }}));
    }
    crate::edits::json_response(&json!({"recap": text("recap"), "generated_at": crate::edits::py_isoformat_pub(job.get("finished_at"))}))
}

/// `GET` / `DELETE /api/npc-talk/<id>/history` (app/routers/npc_talk.py): the caller's saved conversation with one NPC, or its removal
fn npc_history(cfg: &Config, method: &Method, uri: &Uri, headers: &HeaderMap, eid: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method.as_str(), uri, headers, now)?;
    // _require_ask_ai_access: a GM always, a player only in a world that lets players ask the AI
    let world = p.world.clone();
    if !p.is_gm {
        let on = |k: &str| world.as_ref().is_some_and(|w| crate::edits::py_truthy_pub(w.get(k)));
        if !(on("players_can_ask_ai") || on("players_can_use_ai_chat")) {
            return Some(crate::idpages::refusal(403));
        }
    }
    let Some(world) = world else { return Some(crate::idpages::refusal(404)) };
    let wid = world.get("id")?.as_i64()?;
    // _npc_or_404: an entity this viewer may see, of a kind that can talk
    let vis = if p.is_gm { String::new() } else {
        format!(" AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {}))", p.user_id)
    };
    let npc = db.query_row(&format!("SELECT kind FROM entities WHERE id = ?1 AND world_id = ?2{vis}"), rusqlite::params![eid, wid], |r| r.get::<_, Option<String>>(0)).ok();
    let Some(kind) = npc else { return Some(crate::idpages::refusal(404)) };
    let kind = kind?;
    if !["character", "creature", "organization"].contains(&kind.as_str()) {
        if !kind.is_ascii() {
            return None; // str.capitalize-free here, but the message carries the kind as stored
        }
        return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, format!("A {kind} can't hold an in-character conversation \u{2014} Talk to NPCs covers characters, creatures, and organizations.")));
    }
    let row: Option<(i64, Option<String>)> = db.query_row("SELECT id, messages_json FROM chat_sessions WHERE world_id = ?1 AND user_id = ?2 AND surface = 'npc' AND entity_id = ?3 LIMIT 1",
        rusqlite::params![wid, p.user_id, eid], |r| Ok((r.get(0)?, r.get(1)?))).ok();
    if *method == Method::DELETE {
        if let Some((id, _)) = row {
            db.execute("DELETE FROM chat_sessions WHERE id = ?1", [id]).ok()?;
        }
        return crate::edits::json_response(&json!({"ok": true}));
    }
    let messages = match row {
        None => json!([]),
        Some((_, raw)) => {
            let raw = raw.filter(|s| !s.is_empty()).unwrap_or_else(|| "[]".into());
            if !crate::edits::plain_numbers(raw.as_bytes()) {
                return None;
            }
            serde_json::from_str::<Value>(&raw).ok()?
        }
    };
    crate::edits::json_response(&json!({"entity_id": eid, "messages": messages}))
}

/// `GET /api/characters/<id>/share` (app/routers/character_share.py): whether the character has a live share link, and its address
fn share_state(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let owner: Option<Option<i64>> = db.query_row("SELECT owner_user_id FROM player_characters WHERE id = ?1", [id], |r| r.get(0)).ok();
    let Some(owner) = owner else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || owner == Some(p.user_id)) {
        return Some(crate::idpages::refusal(404));
    }
    let token: Option<String> = db.query_row("SELECT token FROM character_shares WHERE character_id = ?1 AND (revoked IS NULL OR revoked = 0) ORDER BY id DESC LIMIT 1",
        [id], |r| r.get(0)).ok();
    match token {
        None => crate::edits::json_response(&json!({"active": false, "url": ""})),
        Some(tok) => {
            let host_ok = headers.get(axum::http::header::HOST).and_then(|h| h.to_str().ok()).is_some_and(|h| !h.is_empty() && h.bytes().all(|b| b.is_ascii_alphanumeric() || b"-.:[]".contains(&b)));
            if !host_ok {
                return None;
            }
            let base = crate::auth::base_url(cfg, headers)?;
            crate::edits::json_response(&json!({"active": true, "url": format!("{}/share/{tok}", base.trim_end_matches('/'))}))
        }
    }
}

/// `GET /api/ai/world-context` (app/main.py `ai_world_context`): the world as plain text for the AI chat - people, places, lore documents in
/// full, items and feats by name - through the caller's own visibility (an assistant sees what players see)
fn world_context(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let Some(world) = p.world.clone() else { return crate::edits::json_response(&json!({"context": "", "world_name": ""})) };
    let wid = world.get("id")?.as_i64()?;
    let text = |m: &Map<String, Value>, k: &str| -> Option<Option<String>> {
        match m.get(k) {
            None | Some(Value::Null) => Some(None),
            Some(Value::String(s)) => Some(Some(s.clone())),
            _ => None,
        }
    };
    let strip = |s: Option<String>| -> Option<Option<String>> {
        match s {
            Some(s) if !p.is_gm && !s.is_empty() => Some(Some(nd_perm::gmonly::strip_gm_only(&s, None, nd_render::now_minute())?)),
            other => Some(other),
        }
    };
    let vis = if p.is_gm { String::new() } else {
        format!(" AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {}))", p.user_id)
    };
    let rows = |sql: &str, params: &[&dyn rusqlite::ToSql]| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    let name = text(&world, "name")?.unwrap_or_else(|| "None".into());
    let mut lines: Vec<String> = vec![format!("# {name}"), text(&world, "description")?.unwrap_or_default(), String::new()];
    for kind in ["character", "location", "organization", "creature", "event"] {
        let ents = rows(&format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = ?2{vis} ORDER BY name"), &[&wid, &kind])?;
        if ents.is_empty() {
            continue;
        }
        lines.push(format!("## {}S ({})", kind.to_uppercase(), ents.len()));
        for e in &ents {
            let summary = strip(text(e, "summary")?)?;
            let mut line = format!("- **{}**", text(e, "name")?.unwrap_or_else(|| "None".into()));
            if let Some(sub) = text(e, "subtype")?.filter(|s| !s.is_empty()) {
                line.push_str(&format!(" [{sub}]"));
            }
            if let Some(s) = summary.filter(|s| !s.is_empty()) {
                line.push_str(&format!(": {s}"));
            }
            lines.push(line);
        }
        lines.push(String::new());
    }
    let notes = rows(&format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = 'note'{vis} ORDER BY name"), &[&wid])?;
    if !notes.is_empty() {
        lines.push("## LORE DOCUMENTS (full text)".into());
        for e in &notes {
            let mut head = format!("\n### {}", text(e, "name")?.unwrap_or_else(|| "None".into()));
            if let Some(sub) = text(e, "subtype")?.filter(|s| !s.is_empty()) {
                head.push_str(&format!(" [{sub}]"));
            }
            lines.push(head);
            let summary = strip(text(e, "summary")?)?;
            let body = strip(text(e, "body")?)?;
            if let Some(s) = summary.filter(|s| !s.is_empty()) {
                lines.push(s);
            }
            if let Some(b) = body.filter(|s| !s.is_empty()) {
                lines.push(b.chars().take(5000).collect());
            }
        }
        lines.push(String::new());
    }
    for kind in ["item", "feat"] {
        let ents = rows(&format!("SELECT name, subtype FROM entities WHERE world_id = ?1 AND kind = ?2{vis} ORDER BY subtype, name"), &[&wid, &kind])?;
        if ents.is_empty() {
            continue;
        }
        lines.push(format!("## {}S ({} total)", kind.to_uppercase(), ents.len()));
        let mut by_sub: Vec<(String, Vec<String>)> = vec![];
        for e in &ents {
            let sub = text(e, "subtype")?.filter(|s| !s.is_empty()).unwrap_or_else(|| "other".into());
            let n = text(e, "name")?.unwrap_or_else(|| "None".into());
            match by_sub.iter_mut().find(|(k, _)| *k == sub) {
                Some((_, v)) => v.push(n),
                None => by_sub.push((sub, vec![n])),
            }
        }
        for (sub, names) in by_sub {
            let mut snippet = names.iter().take(25).cloned().collect::<Vec<_>>().join(", ");
            if names.len() > 25 {
                snippet.push_str(&format!(" \u{2026} +{} more", names.len() - 25));
            }
            lines.push(format!("  [{sub}]: {snippet}"));
        }
        lines.push(String::new());
    }
    crate::edits::json_response(&json!({"context": lines.join("\n"), "world_name": name}))
}

/// `POST /api/ai/save-note` (app/main.py `ai_save_note`): a note entity from an AI answer
fn save_note(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if crate::form::content_type(headers) != "application/json" {
        return None;
    }
    let b = match serde_json::from_slice::<Value>(body).ok()? {
        Value::Object(o) if crate::edits::plain_numbers(body) => o,
        _ => return None,
    };
    let (Some(Value::String(title)), Some(Value::String(content))) = (b.get("title"), b.get("content")) else { return None };
    let subtype = match b.get("subtype") {
        None => String::new(),
        Some(Value::String(s)) => s.clone(),
        _ => return None,
    };
    let Some(world) = p.world.clone() else { return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, "No active world")) };
    let sub: String = nd_render::text::py_strip(&subtype).chars().take(64).collect();
    use rusqlite::types::Value as Sql;
    let id = crate::orm::insert(&db, "entities", &[("world_id", Sql::Integer(world.get("id")?.as_i64()?)), ("kind", Sql::Text("note".into())),
        ("subtype", if sub.is_empty() { Sql::Null } else { Sql::Text(sub) }), ("name", Sql::Text(title.clone())), ("body", Sql::Text(content.clone())),
        ("summary", Sql::Text(content.chars().take(120).collect()))])?;
    crate::edits::json_response(&json!({"id": id, "name": title}))
}
