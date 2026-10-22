//! Investigation boards (app/main.py): the canvas page and its reading list, for a board of the viewer's active world. (The HTML export
//! stays Python's: board_export.html fills a dict with `.update()`, which MiniJinja's values cannot do.)
//! A board stored in a shape Python would trip over (or read differently) is left to Python. `None` = not sure.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{HeaderMap, Response, Uri},
};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::layout::PageCtx;
use crate::pages::{render_with, section_ok, Pages};

/// The board named by `/boards/<slug>[/rest]` of the viewer's active world, with the page context
fn board(cfg: &Config, uri: &Uri, headers: &HeaderMap, rest: &str, check_section: bool) -> Option<(Connection, PageCtx, Map<String, Value>, String)> {
    let tail = uri.path().strip_prefix("/boards/")?;
    let slug = tail.strip_suffix(rest)?;
    if slug.is_empty() || slug == "new" || !slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return None;
    }
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    let b = db.query_row("SELECT * FROM invest_boards WHERE slug = ?1 ORDER BY id LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    if b.get("world_id")?.as_i64() != world.get("id")?.as_i64() {
        return None;
    }
    if check_section && !section_ok(&p, Some(&world), "boards")? {
        return None;
    }
    Some((db, p, b, slug.to_string()))
}

fn loads(b: &Map<String, Value>, col: &str, default: &str) -> Option<Value> {
    let raw = b.get(col).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or(default);
    serde_json::from_str(raw).ok()
}

/// (nodes, groups) of nodes_json: a legacy bare list, or {nodes, groups}
fn nodes_and_groups(b: &Map<String, Value>) -> Option<(Value, Value)> {
    match loads(b, "nodes_json", "[]")? {
        v @ Value::Array(_) => Some((v, json!([]))),
        Value::Object(o) => Some((o.get("nodes").cloned().unwrap_or(json!([])), o.get("groups").cloned().unwrap_or(json!([])))),
        _ => None,
    }
}

/// `GET /boards/<slug>` (app/main.py `board_view`)
pub fn board_view(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, mut p, b, _) = board(cfg, uri, headers, "", true)?;
    let wid = b.get("world_id")?.as_i64()?;
    let mut st = db.prepare("SELECT id, name, kind, image_url FROM entities WHERE world_id = ?1 ORDER BY id").ok()?;
    let ents = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let (nodes, groups) = nodes_and_groups(&b)?;
    let edges = loads(&b, "edges_json", "[]")?;
    let mut payload = Map::new();
    payload.insert("nodes".into(), nodes);
    payload.insert("groups".into(), groups);
    p.ctx.insert("board".into(), Value::Object(b));
    p.ctx.insert("nodes_json".into(), json!(nd_render::py_dumps(&Value::Object(payload))));
    p.ctx.insert("edges_json".into(), json!(nd_render::py_dumps(&edges)));
    p.ctx.insert("entity_list_json".into(), json!(nd_render::py_dumps(&Value::Array(ents.into_iter().map(Value::Object).collect()))));
    render_with(pages, "board.html", p.ctx, headers)
}

/// A node id as a dict key Python and the gateway agree on (None for one Python compares differently: floats, bools, lists...)
fn id_key(v: Option<&Value>) -> Option<String> {
    match v {
        None | Some(Value::Null) => Some("n:".into()),
        Some(Value::Number(n)) if n.is_i64() => Some(format!("i:{}", n.as_i64()?)),
        Some(Value::String(s)) => Some(format!("s:{s}")),
        _ => None,
    }
}

fn truthy(v: Option<&Value>) -> Option<&Value> {
    v.filter(|v| match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    })
}

/// `GET /boards/<slug>/notes` (app/main.py `board_notes`): the nodes as a reading list
pub fn board_notes(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, mut p, b, _) = board(cfg, uri, headers, "/notes", true)?;
    let (nodes, _) = nodes_and_groups(&b)?;
    let nodes = nodes.as_array()?.clone();
    let edges = loads(&b, "edges_json", "[]")?;
    // names = {id: title or "Untitled"} over every node (each must be a dict there)
    let mut names: Vec<(String, Value)> = vec![];
    for n in &nodes {
        let n = n.as_object()?;
        let k = id_key(n.get("id"))?;
        let title = truthy(n.get("title")).cloned().unwrap_or(json!("Untitled"));
        match names.iter_mut().find(|(x, _)| *x == k) {
            Some(e) => e.1 = title,
            None => names.push((k, title)),
        }
    }
    let name_of = |k: &str| names.iter().find(|(x, _)| x == k).map(|(_, t)| t.clone());
    let mut links: Vec<(String, Vec<String>)> = vec![];
    let mut add = |k: &str, t: &Value| -> Option<()> {
        let t = t.as_str()?.to_string(); // sorted() over titles: text only here
        match links.iter_mut().find(|(x, _)| x == k) {
            Some(e) => e.1.push(t),
            None => links.push((k.to_string(), vec![t])),
        }
        Some(())
    };
    for e in edges.as_array()? {
        let e = e.as_object()?;
        let (a, c) = (id_key(e.get("from"))?, id_key(e.get("to"))?);
        if let (Some(na), Some(nc)) = (name_of(&a), name_of(&c)) {
            add(&a, &nc)?;
            add(&c, &na)?;
        }
    }
    let mut notes = vec![];
    for (i, n) in nodes.iter().filter_map(|n| n.as_object()).enumerate() {
        let tags: Vec<Value> = match truthy(n.get("tags")) {
            None => vec![],
            Some(Value::Array(a)) => a.iter().filter(|t| t.is_string()).cloned().collect(),
            Some(Value::String(s)) => s.chars().map(|c| json!(c.to_string())).collect(),
            Some(Value::Object(o)) => o.keys().map(|k| json!(k)).collect(),
            Some(_) => return None,
        };
        let k = id_key(n.get("id"))?;
        let mut l: Vec<String> = links.iter().find(|(x, _)| *x == k).map(|(_, v)| v.clone()).unwrap_or_default();
        l.sort();
        l.dedup();
        notes.push(json!({
            "index": i + 1,
            "title": truthy(n.get("title")).cloned().unwrap_or(json!("Untitled")),
            "type": truthy(n.get("type")).cloned().unwrap_or(json!("note")),
            "status": truthy(n.get("status")).cloned().unwrap_or(json!("unknown")),
            "body": truthy(n.get("body")).cloned().unwrap_or(json!("")),
            "tags": tags,
            "links": l,
            "entity_id": n.get("entity_id").cloned().unwrap_or(Value::Null),
        }));
    }
    let c = &mut p.ctx;
    c.insert("board".into(), Value::Object(b));
    c.insert("notes".into(), Value::Array(notes));
    c.insert("type_icons".into(), json!({"person": "👤", "location": "📍", "event": "⚡", "clue": "🔍", "faction": "🏛", "note": "📝"}));
    c.insert("status_marks".into(), json!({"verified": "✔ verified", "suspect": "? suspect", "dismissed": "— dismissed", "confirmed_false": "✗ false"}));
    c.insert("status_names".into(), json!({"verified": "Verified", "suspect": "Suspect", "dismissed": "Dismissed", "confirmed_false": "False"}));
    render_with(pages, "board_notes.html", p.ctx, headers)
}
