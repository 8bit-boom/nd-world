//! app/routers/bulk_edit.py: the find-and-replace tool's preview and apply (`/api/bulk-edit/parse` is an AI call and lives in ai.rs).
//! Plain string work, done as Python does it. Where Python's `re.IGNORECASE` / `str.lower()` could read a character differently than ASCII
//! case folding (the dotless i, the long s, the Kelvin sign, a capital dotted I), the request is Python's.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode, Uri};
use serde_json::{json, Value};

use crate::config::Config;

const MAX_CANDIDATES: usize = 200;
const ENTITY_FIELDS: [&str; 3] = ["name", "summary", "body"];
const PC_FIELDS: [&str; 4] = ["race", "char_class", "backstory", "notes"];
const RADIUS: usize = 100;

pub async fn handle(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes, apply: bool) -> Option<Response<Body>> {
    tokio::task::spawn_blocking(move || run(&cfg, &uri, &headers, &body, apply)).await.ok().flatten()
}

/// whether Python's case-insensitive reading of `value` could differ from ASCII folding
fn ci_unsafe(value: &str) -> bool {
    value.chars().any(|c| matches!(c, '\u{0131}' | '\u{017f}' | '\u{212a}' | '\u{0130}'))
}

/// (new value, occurrences) - `_apply_replace`
fn apply_replace(value: &str, find: &str, replace: &str, ci: bool) -> (String, usize) {
    if value.is_empty() || find.is_empty() {
        return (value.to_string(), 0);
    }
    if !ci {
        let n = value.matches(find).count();
        return (if n > 0 { value.replace(find, replace) } else { value.to_string() }, n);
    }
    let v: Vec<char> = value.chars().collect();
    let f: Vec<char> = find.chars().map(|c| c.to_ascii_lowercase()).collect();
    let mut out = String::new();
    let (mut i, mut n) = (0usize, 0usize);
    while i < v.len() {
        if i + f.len() <= v.len() && v[i..i + f.len()].iter().zip(&f).all(|(a, b)| a.to_ascii_lowercase() == *b) {
            out.push_str(replace);
            i += f.len();
            n += 1;
        } else {
            out.push(v[i]);
            i += 1;
        }
    }
    (if n > 0 { out } else { value.to_string() }, n)
}

/// `_find_index`: a character position, -1 when absent
fn find_index(value: &str, needle: &str, ci: bool) -> i64 {
    if !ci {
        return value.find(needle).map_or(-1, |b| value[..b].chars().count() as i64);
    }
    let v: Vec<char> = value.chars().flat_map(|c| c.to_lowercase()).collect();
    let n: Vec<char> = needle.chars().flat_map(|c| c.to_lowercase()).collect();
    if n.is_empty() {
        return 0;
    }
    if n.len() > v.len() {
        return -1;
    }
    (0..=v.len() - n.len()).find(|&i| v[i..i + n.len()] == n[..]).map_or(-1, |i| i as i64)
}

fn excerpt(value: &str, around: i64) -> String {
    let v: Vec<char> = value.chars().collect();
    if v.len() <= RADIUS * 2 {
        return value.to_string();
    }
    if around < 0 {
        let head: String = v[..RADIUS * 2].iter().collect();
        return format!("{}…", head.trim_end());
    }
    let start = (around as usize).saturating_sub(RADIUS);
    let end = (around as usize + RADIUS).min(v.len());
    let mid: String = v[start..end].iter().collect();
    format!("{}{}{}", if start > 0 { "…" } else { "" }, mid, if end < v.len() { "…" } else { "" })
}

fn candidate(kind: &str, id: i64, name: &str, field: &str, before: &str, after: &str, count: usize, find: &str, replace: &str, ci: bool) -> Value {
    let bi = find_index(before, find, ci);
    let ai = if replace.is_empty() { bi } else { find_index(after, replace, ci) };
    json!({"target_type": kind, "target_id": id, "target_name": name, "field": field, "occurrences": count,
           "before_excerpt": excerpt(before, bi), "after_excerpt": excerpt(after, if ai >= 0 { ai } else { bi })})
}

fn detail(status: StatusCode, msg: &str) -> Option<Response<Body>> {
    Some(crate::ai_more::detail(status, msg))
}

fn run(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8], apply: bool) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if !crate::edits::plain_numbers(body) {
        return None;
    }
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    // pydantic's shapes; anything it would coerce or reject is Python's
    let find_raw = b.get("find")?.as_str()?;
    let replace = match b.get("replace") { None => "", Some(Value::String(s)) => s.as_str(), _ => return None };
    let ci = match b.get("case_insensitive") { None => false, Some(Value::Bool(x)) => *x, _ => return None };
    let targets: Vec<Value> = if apply { b.get("targets")?.as_array()?.clone() } else { vec![] };
    for t in &targets {
        let o = t.as_object()?;
        o.get("target_type")?.as_str()?;
        o.get("field")?.as_str()?;
        o.get("target_id")?.as_i64().filter(|_| o["target_id"].is_i64())?;
    }
    let Some(world) = p.world.clone() else { return detail(StatusCode::BAD_REQUEST, "No active world") };
    let wid = world.get("id")?.as_i64()?;
    if !crate::pages::edit_level(&p, &world, "bulk_edit")? {
        return Some(crate::idpages::refusal(403));
    }
    let find = crate::edits::py_strip_pub(find_raw);
    if find.is_empty() {
        return detail(StatusCode::BAD_REQUEST, "No search text provided");
    }
    if ci && (!find.is_ascii() || find.contains('\u{0}')) {
        return None;
    }
    if apply {
        if targets.is_empty() {
            return detail(StatusCode::BAD_REQUEST, "No targets selected");
        }
        if targets.len() > MAX_CANDIDATES {
            return detail(StatusCode::BAD_REQUEST, &format!("Too many targets — limit is {MAX_CANDIDATES}"));
        }
        db.execute_batch("BEGIN IMMEDIATE").ok()?;
        let r = apply_targets(&db, wid, &find, replace, ci, &targets);
        return match r {
            Some(updated) if db.execute_batch("COMMIT").is_ok() => crate::edits::json_response(&json!({"updated": updated})),
            _ => {
                let _ = db.execute_batch("ROLLBACK");
                None
            }
        };
    }
    if find.len() > 40_000 {
        return None; // SQLite's "LIKE or GLOB pattern too complex"
    }
    let pattern = format!("%{find}%");
    let mut candidates: Vec<Value> = vec![];
    let get = |r: &rusqlite::Row, i: usize| -> rusqlite::Result<String> { Ok(r.get::<_, Option<String>>(i)?.unwrap_or_default()) };

    let clause = |table: &str, fields: &[&str]| fields.iter().map(|f| format!("lower({table}.{f}) LIKE lower(?1)")).collect::<Vec<_>>().join(" OR ");
    let sql = format!("SELECT id, name, summary, body FROM entities WHERE world_id = ?2 AND ({}) LIMIT {MAX_CANDIDATES}", clause("entities", &ENTITY_FIELDS));
    let ents: Vec<(i64, [String; 4])> = db.prepare(&sql).ok()?
        .query_map(rusqlite::params![pattern, wid], |r| Ok((r.get::<_, i64>(0)?, [get(r, 1)?, get(r, 2)?, get(r, 3)?, String::new()]))).ok()?
        .collect::<Result<_, _>>().ok()?;
    let sql = format!("SELECT entity_notes.id, entity_notes.content, entities.name FROM entity_notes JOIN entities ON entities.id = entity_notes.entity_id \
                       WHERE entities.world_id = ?2 AND lower(entity_notes.content) LIKE lower(?1) LIMIT {MAX_CANDIDATES}");
    let notes: Vec<(i64, String, String)> = db.prepare(&sql).ok()?
        .query_map(rusqlite::params![pattern, wid], |r| Ok((r.get::<_, i64>(0)?, get(r, 1)?, get(r, 2)?))).ok()?
        .collect::<Result<_, _>>().ok()?;
    let sql = format!("SELECT id, name, race, char_class, backstory, notes FROM player_characters WHERE world_id = ?2 AND ({}) LIMIT {MAX_CANDIDATES}", clause("player_characters", &PC_FIELDS));
    let pcs: Vec<(i64, String, [String; 4])> = db.prepare(&sql).ok()?
        .query_map(rusqlite::params![pattern, wid], |r| Ok((r.get::<_, i64>(0)?, get(r, 1)?, [get(r, 2)?, get(r, 3)?, get(r, 4)?, get(r, 5)?]))).ok()?
        .collect::<Result<_, _>>().ok()?;
    if ci && (ents.iter().any(|(_, f)| f.iter().any(|v| ci_unsafe(v))) || notes.iter().any(|n| ci_unsafe(&n.1)) || pcs.iter().any(|(_, _, f)| f.iter().any(|v| ci_unsafe(v)))) {
        return None;
    }
    for (id, f) in &ents {
        for (i, field) in ENTITY_FIELDS.iter().enumerate() {
            let (nv, n) = apply_replace(&f[i], &find, replace, ci);
            if n > 0 {
                candidates.push(candidate("entity", *id, &f[0], field, &f[i], &nv, n, &find, replace, ci));
            }
        }
    }
    for (id, content, ename) in &notes {
        let (nv, n) = apply_replace(content, &find, replace, ci);
        if n > 0 {
            candidates.push(candidate("entity_note", *id, ename, "content", content, &nv, n, &find, replace, ci));
        }
    }
    for (id, name, f) in &pcs {
        for (i, field) in PC_FIELDS.iter().enumerate() {
            let (nv, n) = apply_replace(&f[i], &find, replace, ci);
            if n > 0 {
                candidates.push(candidate("player_character", *id, name, field, &f[i], &nv, n, &find, replace, ci));
            }
        }
    }
    candidates.truncate(MAX_CANDIDATES);
    let total: u64 = candidates.iter().filter_map(|c| c["occurrences"].as_u64()).sum();
    crate::edits::json_response(&json!({"candidates": candidates, "total_occurrences": total,
        "truncated": ents.len() >= MAX_CANDIDATES || notes.len() >= MAX_CANDIDATES || pcs.len() >= MAX_CANDIDATES}))
}

fn apply_targets(db: &rusqlite::Connection, wid: i64, find: &str, replace: &str, ci: bool, targets: &[Value]) -> Option<i64> {
    let stamp = crate::orm::now_stamp();
    let mut updated = 0;
    for t in targets {
        let (kind, field, id) = (t["target_type"].as_str()?, t["field"].as_str()?, t["target_id"].as_i64()?);
        let (table, fields): (&str, &[&str]) = match kind {
            "entity" => ("entities", &ENTITY_FIELDS),
            "entity_note" => ("entity_notes", &["content"]),
            "player_character" => ("player_characters", &PC_FIELDS),
            _ => continue,
        };
        if !fields.contains(&field) {
            continue;
        }
        let row: Option<(Option<String>, Option<i64>)> = {
            let sql = match kind {
                "entity_note" => format!("SELECT n.{field}, (SELECT e.world_id FROM entities e WHERE e.id = n.entity_id) FROM entity_notes n WHERE n.id = ?1"),
                _ => format!("SELECT {field}, world_id FROM {table} WHERE id = ?1"),
            };
            db.query_row(&sql, [id], |r| Ok((r.get(0)?, r.get(1)?))).ok()
        };
        let Some((current, row_world)) = row else { continue };
        if row_world != Some(wid) {
            continue;
        }
        let current = current.unwrap_or_default();
        if ci && ci_unsafe(&current) {
            return None;
        }
        let (nv, n) = apply_replace(&current, find, replace, ci);
        if n > 0 {
            db.execute(&format!("UPDATE {table} SET {field} = ?1, updated_at = ?2 WHERE id = ?3"), rusqlite::params![nv, stamp, id]).ok()?;
            updated += 1;
        }
    }
    Some(updated)
}
