//! The MCP server (app/mcp_server.py) at POST /mcp: bearer-token auth (an ApiToken's sha256), the streamable-HTTP transport the MCP
//! SDK runs stateless (each POST one JSON-RPC message, the answer one SSE `message` event), initialize / ping / tools/list as the SDK
//! answers them (the tool list itself is the SDK's own serialization, from app_data.json), and tools/call for the tools ported here.
//! Anything else - a tool not ported yet, arguments the SDK would coerce or refuse (its pydantic messages), a batch, a GET, a missing or
//! unknown token - is Python's: None, and the request goes there unchanged.

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode};
use bytes::Bytes;
use rusqlite::{types::Value as Sql, Connection};
use serde_json::{json, Map, Value};
use std::sync::Arc;

use crate::config::Config;

/// The token's user: (id, is_gm)
#[derive(Clone, Copy)]
struct User {
    id: i64,
    gm: bool,
}

/// What a tool hands back: a list (each item its own text block, `structuredContent` {"result": [...]}), a dict (one block, no
/// structured content - the tools' `-> dict` has no output schema) or a string
enum Out {
    List(Vec<Value>),
    Dict(Value),
}

/// A tool's failure: the text of the exception Python raises (shown as "Error executing tool <name>: <text>")
type Fail = String;

/// Ok(None): not the gateway's to answer
type ToolResult = Result<Option<Out>, Fail>;

fn data() -> &'static Value {
    &nd_render::app::data()["mcp"]
}

fn sha256_hex(s: &str) -> String {
    use sha2::Digest;
    sha2::Sha256::digest(s.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

/// The request's own checks the SDK makes before reading the message: both media types accepted, a JSON body, a protocol version
/// header it knows (when there is one)
fn transport_ok(headers: &HeaderMap) -> bool {
    let h = |k: &str| headers.get(k).and_then(|v| v.to_str().ok()).unwrap_or("");
    let accept: Vec<String> = h("accept").split(',').map(|m| m.trim().to_string()).collect();
    if !accept.iter().any(|m| m.starts_with("application/json")) || !accept.iter().any(|m| m.starts_with("text/event-stream")) {
        return false;
    }
    if !h("content-type").starts_with("application/json") {
        return false;
    }
    match headers.get("mcp-protocol-version") {
        None => true,
        Some(v) => v.to_str().ok().is_some_and(|v| data()["supported"].as_array().is_some_and(|s| s.iter().any(|x| x.as_str() == Some(v)))),
    }
}

fn sse(id: &Value, result: &str) -> Response<Body> {
    let msg = format!("{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{result}}}", serde_json::to_string(id).unwrap_or_default());
    Response::builder().status(StatusCode::OK).header("cache-control", "no-cache, no-transform").header("content-type", "text/event-stream")
        .header("x-accel-buffering", "no").header("x-nd-served-by", "gateway")
        .body(Body::from(format!("event: message\r\ndata: {msg}\r\n\r\n"))).unwrap()
}

/// `pydantic_core.to_json(x, indent=2)`: what a tool's result reads as in a text block
fn pretty(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => serde_json::to_string_pretty(other).unwrap_or_default(),
    }
}

fn call_result(out: Result<Out, Fail>, name: &str) -> String {
    let text = |t: String| json!({"type": "text", "text": t});
    let v = match out {
        Ok(Out::List(items)) => {
            // _convert_to_content flattens a list: every item is a block of its own
            json!({"content": items.iter().map(|i| text(pretty(i))).collect::<Vec<_>>(), "structuredContent": {"result": items}, "isError": false})
        }
        Ok(Out::Dict(d)) => json!({"content": [text(pretty(&d))], "isError": false}),
        Err(e) => json!({"content": [text(format!("Error executing tool {name}: {e}"))], "isError": true}),
    };
    serde_json::to_string(&v).unwrap_or_default()
}

/// POST /mcp
pub async fn handle(cfg: Arc<Config>, headers: HeaderMap, body: Bytes) -> Option<Response<Body>> {
    if !transport_ok(&headers) {
        return None;
    }
    let auth = headers.get("authorization").and_then(|v| v.to_str().ok()).unwrap_or("");
    let token = if auth.len() >= 7 && auth[..7].eq_ignore_ascii_case("bearer ") { auth[7..].to_string() } else { String::new() };
    if token.is_empty() {
        return None;
    }
    let Ok(Value::Object(msg)) = serde_json::from_slice::<Value>(&body) else { return None };
    if msg.get("jsonrpc").and_then(|v| v.as_str()) != Some("2.0") || msg.keys().any(|k| !matches!(k.as_str(), "jsonrpc" | "id" | "method" | "params")) {
        return None;
    }
    let method = msg.get("method")?.as_str()?.to_string();
    let id = msg.get("id").cloned();
    if let Some(i) = &id {
        if !(i.is_i64() || i.is_string()) {
            return None;
        }
    }
    let params = match msg.get("params") {
        None => Map::new(),
        Some(Value::Object(p)) => p.clone(),
        Some(_) => return None,
    };
    let c = cfg.clone();
    tokio::task::spawn_blocking(move || -> Option<Response<Body>> {
        let db = crate::writes::open(&c)?;
        let user = db.query_row("SELECT u.id, u.is_gm FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?1 ORDER BY t.id LIMIT 1",
            [sha256_hex(&token)], |r| Ok(User { id: r.get(0)?, gm: r.get::<_, Option<bool>>(1)?.unwrap_or(false) })).ok()?;
        let touch = |db: &Connection| db.execute("UPDATE api_tokens SET last_used_at = ?1 WHERE token_hash = ?2", rusqlite::params![crate::orm::now_stamp(), sha256_hex(&token)]).ok();
        match (method.as_str(), &id) {
            ("notifications/initialized", None) if params.is_empty() => {
                touch(&db)?;
                Some(Response::builder().status(StatusCode::ACCEPTED).header("content-type", "application/json").header("x-nd-served-by", "gateway").body(Body::empty()).unwrap())
            }
            ("ping", Some(id)) if params.is_empty() => {
                touch(&db)?;
                Some(sse(id, "{}"))
            }
            ("tools/list", Some(id)) if params.is_empty() => {
                touch(&db)?;
                Some(sse(id, data()["tools_list_json"].as_str()?))
            }
            ("initialize", Some(id)) => {
                let asked = params.get("protocolVersion")?.as_str()?;
                let ci = params.get("clientInfo")?.as_object()?;
                if !params.get("capabilities")?.is_object() || !ci.get("name")?.is_string() || !ci.get("version")?.is_string()
                    || params.keys().any(|k| !matches!(k.as_str(), "protocolVersion" | "capabilities" | "clientInfo")) {
                    return None;
                }
                let d = data();
                let version = if d["supported"].as_array()?.iter().any(|v| v.as_str() == Some(asked)) { asked } else { d["latest"].as_str()? };
                let result = json!({"protocolVersion": version, "capabilities": {"experimental": {}, "prompts": {"listChanged": false},
                    "resources": {"subscribe": false, "listChanged": false}, "tools": {"listChanged": false}},
                    "serverInfo": {"name": d["name"], "version": d["version"]}, "instructions": d["instructions"]});
                touch(&db)?;
                Some(sse(id, &serde_json::to_string(&result).ok()?))
            }
            ("tools/call", Some(id)) => {
                if params.keys().any(|k| !matches!(k.as_str(), "name" | "arguments")) {
                    return None;
                }
                let name = params.get("name")?.as_str()?.to_string();
                let args = match params.get("arguments") {
                    None | Some(Value::Null) => Map::new(),
                    Some(Value::Object(a)) => a.clone(),
                    Some(_) => return None,
                };
                let out = call(&db, user, &name, &args)?;
                touch(&db)?;
                Some(sse(id, &call_result(out, &name)))
            }
            _ => None,
        }
    }).await.ok()?
}

// ── arguments, as the tool's pydantic model would take them ───────────────────────────────────────────────

/// A parameter of a tool: its annotation
#[derive(Clone, Copy)]
enum P {
    Int,
    OptInt,
    Str,
    OptStr,
}

/// The arguments by name, or None when the SDK would coerce or refuse one (its own messages - Python's)
fn args_of(args: &Map<String, Value>, spec: &[(&str, P)]) -> Option<Map<String, Value>> {
    let mut out = Map::new();
    for (name, p) in spec {
        let v = args.get(*name);
        let ok = match (p, v) {
            (P::Int | P::Str, None) => false,
            (P::OptInt | P::OptStr, None | Some(Value::Null)) => true,
            (P::Int | P::OptInt, Some(Value::Number(n))) => n.is_i64(),
            (P::Str, Some(Value::String(_))) => true,
            // pre_parse_json: a string for a parameter not annotated `str` is read as JSON first when it holds a list, a dict, ...
            (P::OptStr, Some(Value::String(s))) => !matches!(crate::map_layout::py_loads(s), Ok(Value::Null | Value::Bool(_) | Value::Array(_) | Value::Object(_))),
            _ => false,
        };
        if !ok {
            return None;
        }
        out.insert((*name).into(), v.cloned().unwrap_or(Value::Null));
    }
    Some(out)
}

fn call(db: &Connection, user: User, name: &str, args: &Map<String, Value>) -> Option<Result<Out, Fail>> {
    use P::*;
    let r = match name {
        "list_worlds" => list_worlds(db, user),
        "list_facts" => { let a = args_of(args, &[("world_id", Int), ("game_session_id", OptInt)])?; list_facts(db, user, &a) }
        "list_quests" => { let a = args_of(args, &[("world_id", Int), ("status", OptStr)])?; list_quests(db, user, &a) }
        "search_entities" => { let a = args_of(args, &[("world_id", Int), ("query", Str), ("kind", OptStr)])?; search_entities(db, user, &a) }
        "get_entity" => { let a = args_of(args, &[("entity_id", Int)])?; get_entity(db, user, &a) }
        "get_rules" => { let a = args_of(args, &[("world_id", Int)])?; get_rules(db, user, &a) }
        "list_sessions" => { let a = args_of(args, &[("world_id", Int)])?; list_sessions(db, user, &a) }
        "list_tables" => { let a = args_of(args, &[("world_id", Int)])?; list_tables(db, user, &a) }
        _ => return None,
    };
    match r {
        Ok(Some(o)) => Some(Ok(o)),
        Ok(None) => None,
        Err(e) => Some(Err(e)),
    }
}

// ── the tools' shared checks ──────────────────────────────────────────────────────────────────────────────

fn int(a: &Map<String, Value>, k: &str) -> i64 {
    a.get(k).and_then(|v| v.as_i64()).unwrap_or(0)
}

fn sql_json(v: Sql) -> Value {
    match v {
        Sql::Null => Value::Null,
        Sql::Integer(i) => json!(i),
        Sql::Real(f) => json!(f),
        Sql::Text(t) => json!(t),
        Sql::Blob(_) => Value::Null,
    }
}

/// A Boolean column as Python reads it (None stays None)
fn sql_bool(v: Sql) -> Value {
    match v {
        Sql::Integer(i) => json!(i != 0),
        _ => Value::Null,
    }
}

/// `_load_world`: (section_access_json, custom_kinds_json, rules_md) of a world this token reaches; Err(Python's PermissionError text)
fn load_world(db: &Connection, wid: i64, user: User) -> Result<Option<(Option<String>, Option<String>, Option<String>)>, Fail> {
    let row = db.query_row("SELECT section_access_json, custom_kinds_json, rules_md FROM worlds WHERE id = ?1", [wid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)));
    let refuse = || Err(format!("World {wid} not found or not accessible to this token"));
    let w = match row {
        Ok(w) => w,
        Err(rusqlite::Error::QueryReturnedNoRows) => return refuse(),
        Err(_) => return Ok(None),
    };
    if !user.gm && db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, user.id], |_| Ok(())).is_err() {
        return refuse();
    }
    Ok(Some(w))
}

type World = (Option<String>, Option<String>, Option<String>);

/// `world_can_view_section` for the token's user (a non-GM reads at the player tier: the MCP request never carries is_assistant)
fn can_view(user: User, w: &World, section: &str) -> Option<bool> {
    let level = nd_perm::sections::section_level(user.gm, if user.gm { None } else { Some("player") }, w.0.as_deref(), w.1.as_deref(), section)?;
    Some(level == "read" || level == "edit")
}

/// `_require_view_section`
fn require_view(user: User, w: &World, section: &str) -> Result<Option<()>, Fail> {
    match can_view(user, w, section) {
        None => Ok(None),
        Some(true) => Ok(Some(())),
        Some(false) => Err(format!("This token's role has no access to the '{section}' section of this world")),
    }
}

fn rows(db: &Connection, sql: &str, params: &[&dyn rusqlite::ToSql], f: impl Fn(&rusqlite::Row) -> rusqlite::Result<Value>) -> Option<Vec<Value>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map(params, |r| f(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r)
}

macro_rules! try_some {
    ($e:expr) => {
        match $e? {
            Some(x) => x,
            None => return Ok(None),
        }
    };
}

// ── the tools ─────────────────────────────────────────────────────────────────────────────────────────────

fn list_worlds(db: &Connection, user: User) -> ToolResult {
    let f = |r: &rusqlite::Row| Ok(json!({"id": r.get::<_, i64>(0)?, "name": sql_json(r.get(1)?), "slug": sql_json(r.get(2)?)}));
    let out = if user.gm {
        rows(db, "SELECT id, name, slug FROM worlds ORDER BY name", &[], f)
    } else {
        rows(db, "SELECT id, name, slug FROM worlds WHERE id IN (SELECT world_id FROM world_memberships WHERE user_id = ?1) ORDER BY name", &[&user.id], f)
    };
    Ok(out.map(Out::List))
}

fn list_facts(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let wid = int(a, "world_id");
    let w = try_some!(load_world(db, wid, user));
    try_some!(require_view(user, &w, "facts"));
    let vis = if user.gm { "" } else { " AND visible_to_players IS NOT 0" };
    let facts = rows(db, &format!("SELECT id, content, visible_to_players, game_session_id FROM facts WHERE world_id = ?1{vis} ORDER BY created_at DESC LIMIT 60"), &[&wid],
        |r| Ok(json!({"id": r.get::<_, i64>(0)?, "content": sql_json(r.get(1)?), "visible_to_players": sql_bool(r.get(2)?), "game_session_id": sql_json(r.get(3)?)})));
    let Some(mut facts) = facts else { return Ok(None) };
    if let Some(gs) = a.get("game_session_id").filter(|v| !v.is_null()) {
        facts.retain(|f| f["game_session_id"] == *gs);
    }
    Ok(Some(Out::List(facts)))
}

fn list_quests(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let wid = int(a, "world_id");
    let w = try_some!(load_world(db, wid, user));
    try_some!(require_view(user, &w, "quests"));
    let mut sql = "SELECT id, title, status, category, summary FROM quests WHERE world_id = ?1".to_string();
    if !user.gm {
        sql.push_str(" AND visible_to_players IS NOT 0");
    }
    let status = a.get("status").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(str::to_string);
    let f = |r: &rusqlite::Row| Ok(json!({"id": r.get::<_, i64>(0)?, "title": sql_json(r.get(1)?), "status": sql_json(r.get(2)?), "category": sql_json(r.get(3)?), "summary": sql_json(r.get(4)?)}));
    let out = match &status {
        Some(s) => rows(db, &format!("{sql} AND status = ?2 ORDER BY title"), &[&wid, s], f),
        None => rows(db, &format!("{sql} ORDER BY title"), &[&wid], f),
    };
    Ok(out.map(Out::List))
}

fn search_entities(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let wid = int(a, "world_id");
    let w = try_some!(load_world(db, wid, user));
    let kind = a.get("kind").and_then(|v| v.as_str()).filter(|k| !k.is_empty()).map(str::to_string);
    if let Some(k) = &kind {
        try_some!(require_view(user, &w, &format!("kind_{k}")));
    }
    let query = a.get("query").and_then(|v| v.as_str()).unwrap_or("");
    let Some(found) = crate::retrieval::find_relevant_entities(db, wid, query, 25, (user.id, user.gm)) else { return Ok(None) };
    let mut out = vec![];
    for e in found {
        let ek = e.get("kind").and_then(|v| v.as_str()).unwrap_or("").to_string();
        if kind.as_ref().is_some_and(|k| *k != ek) {
            continue;
        }
        if !user.gm {
            match can_view(user, &w, &format!("kind_{ek}")) {
                None => return Ok(None),
                Some(false) => continue,
                Some(true) => {}
            }
        }
        let g = |k: &str| e.get(k).cloned().unwrap_or(Value::Null);
        out.push(json!({"id": g("id"), "kind": g("kind"), "subtype": g("subtype"), "name": g("name"), "summary": g("summary")}));
    }
    Ok(Some(Out::List(out)))
}

fn get_entity(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let eid = int(a, "entity_id");
    let row = db.query_row("SELECT id, kind, subtype, name, summary, body, tags, folder, visible_to_players, updated_at, world_id FROM entities WHERE id = ?1", [eid],
        |r| Ok((0..11).map(|i| r.get::<_, Sql>(i)).collect::<Result<Vec<_>, _>>()?));
    let mut e = match row {
        Ok(e) => e,
        Err(rusqlite::Error::QueryReturnedNoRows) => return Err(format!("Entity {eid} not found")),
        Err(_) => return Ok(None),
    };
    let Sql::Integer(wid) = e[10] else { return Ok(None) };
    let w = try_some!(load_world(db, wid, user));
    let kind = match &e[1] { Sql::Text(t) => t.clone(), _ => return Ok(None) };
    try_some!(require_view(user, &w, &format!("kind_{kind}")));
    let visible = sql_bool(e[8].clone());
    if !user.gm && visible != json!(true)
        && db.query_row("SELECT 1 FROM entity_player_access WHERE entity_id = ?1 AND user_id = ?2", [eid, user.id], |_| Ok(())).is_err() {
        return Err(format!("Entity {eid} is not visible to this token"));
    }
    let updated = match std::mem::replace(&mut e[9], Sql::Null) {
        Sql::Text(t) => crate::edits::py_isoformat_pub(Some(&json!(t))),
        _ => Value::Null,
    };
    let v = |i: usize| sql_json(e[i].clone());
    Ok(Some(Out::Dict(json!({"id": v(0), "kind": v(1), "subtype": v(2), "name": v(3), "summary": v(4), "body": v(5), "tags": v(6), "folder": v(7),
        "visible_to_players": visible, "updated_at": updated}))))
}

/// `_player_rules_md`: the rules without their GM-only `:::gm` blocks
fn player_rules_md(md: &str) -> String {
    let (skeleton, blocks) = nd_render::rules::extract_blocks(md);
    let gm: Vec<&str> = blocks.iter().filter(|b| b.btype == "gm").map(|b| b.sentinel.as_str()).collect();
    nd_render::text::py_splitlines(&skeleton).into_iter().filter(|l| !gm.contains(&crate::edits::py_strip_pub(l).as_str())).collect::<Vec<_>>().join("\n")
}

fn get_rules(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let wid = int(a, "world_id");
    let w = try_some!(load_world(db, wid, user));
    try_some!(require_view(user, &w, "rules"));
    let md = w.2.clone().unwrap_or_default();
    if crate::edits::py_strip_pub(&md).is_empty() {
        return Ok(Some(Out::Dict(json!({"rules_md": "", "note": "This world has no custom rules — the app's bundled core rules apply."}))));
    }
    let md = if user.gm { md } else { player_rules_md(&md) };
    let len = md.chars().count();
    Ok(Some(Out::Dict(json!({"rules_md": md, "length": len}))))
}

fn list_sessions(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let wid = int(a, "world_id");
    let w = try_some!(load_world(db, wid, user));
    try_some!(require_view(user, &w, "sessions"));
    let out = rows(db, "SELECT id, session_num, title, session_date, player_summary_published, player_summary FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC", &[&wid], |r| {
        let mut row = json!({"id": r.get::<_, i64>(0)?, "session_num": sql_json(r.get(1)?), "title": sql_json(r.get(2)?), "session_date": sql_json(r.get(3)?)});
        if user.gm {
            let published = matches!(r.get::<_, Sql>(4)?, Sql::Integer(i) if i != 0);
            let text: Option<String> = r.get(5)?;
            row["recap_published"] = json!(published && !crate::edits::py_strip_pub(text.as_deref().unwrap_or("")).is_empty());
        }
        Ok(row)
    });
    Ok(out.map(Out::List))
}

fn list_tables(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    if !user.gm {
        return Err("This action requires a GM token".into());
    }
    let wid = int(a, "world_id");
    try_some!(load_world(db, wid, user));
    let Some(tables) = rows(db, "SELECT id, name, category, description, entries_json FROM random_tables WHERE world_id IS NULL OR world_id = ?1 ORDER BY name", &[&wid],
        |r| Ok(json!([r.get::<_, i64>(0)?, sql_json(r.get(1)?), sql_json(r.get(2)?), sql_json(r.get(3)?), sql_json(r.get(4)?)]))) else { return Ok(None) };
    let mut out = vec![];
    for t in tables {
        let raw = t[4].as_str().filter(|s| !s.is_empty()).unwrap_or("[]");
        // len(json.loads(entries_json or "[]")), with Python's own failures
        let n = match crate::map_layout::py_loads(raw)? {
            Value::Array(a) => a.len(),
            Value::Object(o) => o.len(),
            Value::String(s) => s.chars().count(),
            Value::Bool(_) => return Err("object of type 'bool' has no len()".into()),
            Value::Number(n) => return Err(format!("object of type '{}' has no len()", if n.is_f64() { "float" } else { "int" })),
            Value::Null => return Ok(None), // null, NaN, Infinity: Python's to say
        };
        out.push(json!({"id": t[0], "name": t[1], "category": t[2], "description": t[3], "entry_count": n}));
    }
    Ok(Some(Out::List(out)))
}
