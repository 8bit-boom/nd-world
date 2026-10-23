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

use crate::ai::py_repr;
use crate::config::Config;

/// `str(v)`
fn py_str(v: &Value) -> String {
    match v {
        Value::String(t) => t.clone(),
        other => py_repr(other),
    }
}

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
    /// `int = n`
    IntOr(i64),
    Str,
    OptStr,
    /// `str = "..."`
    StrOr(&'static str),
    /// `bool = b`
    BoolOr(bool),
    OptBool,
}

/// The arguments by name, or None when the SDK would coerce or refuse one (its own messages - Python's)
fn args_of(args: &Map<String, Value>, spec: &[(&str, P)]) -> Option<Map<String, Value>> {
    let mut out = Map::new();
    for (name, p) in spec {
        let v = args.get(*name);
        let ok = match (p, v) {
            (P::Int | P::Str, None) => false,
            (P::OptInt | P::OptStr | P::OptBool, None | Some(Value::Null)) => true,
            (P::IntOr(d), None) => { out.insert((*name).into(), json!(d)); continue }
            (P::StrOr(d), None) => { out.insert((*name).into(), json!(d)); continue }
            (P::BoolOr(d), None) => { out.insert((*name).into(), json!(d)); continue }
            (P::Int | P::OptInt | P::IntOr(_), Some(Value::Number(n))) => n.is_i64(),
            (P::Str | P::StrOr(_), Some(Value::String(_))) => true,
            (P::BoolOr(_) | P::OptBool, Some(Value::Bool(_))) => true,
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
        "get_session" => { let a = args_of(args, &[("session_id", Int)])?; get_session(db, user, &a) }
        "roll_table" => { let a = args_of(args, &[("table_id", Int), ("times", IntOr(1))])?; roll_table(db, user, &a) }
        "create_fact" => { let a = args_of(args, &[("world_id", Int), ("content", Str), ("visible_to_players", BoolOr(true)), ("game_session_id", OptInt)])?; create_fact(db, user, &a) }
        "update_fact" => { let a = args_of(args, &[("fact_id", Int), ("content", OptStr), ("visible_to_players", OptBool)])?; update_fact(db, user, &a) }
        "delete_fact" => { let a = args_of(args, &[("fact_id", Int)])?; delete_fact(db, user, &a) }
        "create_entity" => {
            let a = args_of(args, &[("world_id", Int), ("kind", Str), ("name", Str), ("summary", StrOr("")), ("body", StrOr("")), ("subtype", StrOr("")),
                                    ("tags", StrOr("")), ("folder", StrOr("")), ("visible_to_players", BoolOr(true))])?;
            create_entity(db, user, &a)
        }
        "create_note" => {
            let a = args_of(args, &[("world_id", Int), ("name", Str), ("body", Str), ("summary", StrOr("")), ("visible_to_players", BoolOr(true))])?;
            create_note(db, user, &a)
        }
        "update_entity" => {
            let a = args_of(args, &[("entity_id", Int), ("name", OptStr), ("summary", OptStr), ("body", OptStr), ("subtype", OptStr), ("tags", OptStr),
                                    ("folder", OptStr), ("visible_to_players", OptBool)])?;
            update_entity(db, user, &a)
        }
        "delete_entity" => { let a = args_of(args, &[("entity_id", Int)])?; delete_entity(db, user, &a) }
        "list_characters" => { let a = args_of(args, &[("world_id", Int)])?; list_characters(db, user, &a) }
        "get_character" => { let a = args_of(args, &[("character_id", Int)])?; get_character(db, user, &a) }
        "list_parties" => { let a = args_of(args, &[("world_id", Int)])?; list_parties(db, user, &a) }
        "get_party" => { let a = args_of(args, &[("party_id", Int)])?; get_party(db, user, &a) }
        "adjust_character_resource" => {
            let a = args_of(args, &[("character_id", Int), ("resource", Str), ("delta", OptInt), ("value", OptInt)])?;
            adjust_character_resource(db, user, &a)
        }
        "set_character_conditions" => {
            let a = args_of(args, &[("character_id", Int)])?;
            let list = args.get("conditions")?.as_array()?;
            if !list.iter().all(|c| c.is_string()) {
                return None;
            }
            set_character_conditions(db, user, &a, list)
        }
        "award_character_xp" => { let a = args_of(args, &[("character_id", Int), ("amount", Int)])?; award_character_xp(db, user, &a) }
        "rest_party" => { let a = args_of(args, &[("party_id", Int), ("kind", StrOr("long"))])?; rest_party(db, user, &a) }
        "create_quest" => {
            let a = args_of(args, &[("world_id", Int), ("title", Str), ("summary", StrOr("")), ("body", StrOr("")), ("status", StrOr("active")),
                                    ("category", StrOr("main")), ("visible_to_players", BoolOr(true))])?;
            create_quest(db, user, &a)
        }
        "update_quest" => {
            let a = args_of(args, &[("quest_id", Int), ("title", OptStr), ("summary", OptStr), ("body", OptStr), ("status", OptStr), ("category", OptStr),
                                    ("visible_to_players", OptBool)])?;
            update_quest(db, user, &a)
        }
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

// ── sessions, tables, and the write tools ────────────────────────────────────────────────────────────────

fn gm_only(user: User) -> Result<(), Fail> {
    if user.gm { Ok(()) } else { Err("This action requires a GM token".into()) }
}

fn s(a: &Map<String, Value>, k: &str) -> Option<String> {
    a.get(k).and_then(|v| v.as_str()).map(str::to_string)
}

fn strip(t: &str) -> String {
    crate::edits::py_strip_pub(t)
}

fn b(a: &Map<String, Value>, k: &str) -> Option<bool> {
    a.get(k).and_then(|v| v.as_bool())
}

/// A row's columns by name (None: no such row)
fn row_of(db: &Connection, table: &str, cols: &str, id: i64) -> Result<Option<Map<String, Value>>, ()> {
    let names: Vec<&str> = cols.split(", ").collect();
    match db.query_row(&format!("SELECT {cols} FROM {table} WHERE id = ?1"), [id], |r| {
        let mut m = Map::new();
        for (i, n) in names.iter().enumerate() {
            m.insert((*n).to_string(), sql_json(r.get(i)?));
        }
        Ok(m)
    }) {
        Ok(m) => Ok(Some(m)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(_) => Err(()),
    }
}

/// `bool(x)` of a stored Boolean (NULL is False)
fn truthy_int(v: &Value) -> bool {
    v.as_i64().is_some_and(|i| i != 0)
}

/// A stored Boolean as Python reads it back (None stays None)
fn as_bool(v: &Value) -> Value {
    match v.as_i64() { Some(i) => json!(i != 0), None => Value::Null }
}

fn get_session(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let sid = int(a, "session_id");
    let Ok(gs) = row_of(db, "game_sessions", "id, world_id, session_num, title, session_date, summary, player_summary, player_summary_published, recap_audio_json", sid) else { return Ok(None) };
    let Some(gs) = gs else { return Err(format!("Session {sid} not found")) };
    let wid = gs["world_id"].as_i64().unwrap_or(0);
    let w = try_some!(load_world(db, wid, user));
    try_some!(require_view(user, &w, "sessions"));
    let mut base = json!({"id": gs["id"], "session_num": gs["session_num"], "title": gs["title"], "session_date": gs["session_date"]});
    // session_media.attached_clips: the recap's audio, in order, that this viewer may hear
    let ids: Vec<i64> = match crate::map_layout::py_loads(gs["recap_audio_json"].as_str().filter(|t| !t.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(raw)) => {
            let mut out = vec![];
            for v in raw {
                if let Some(i) = v.as_i64().filter(|_| v.is_i64() || v.is_u64()) {
                    if !out.contains(&i) {
                        out.push(i);
                    }
                }
            }
            out
        }
        _ => vec![],
    };
    let mut clips = vec![];
    for i in ids {
        let Ok(c) = row_of(db, "audio_clips", "id, world_id, name, description, file_url, visible_to_players, transcript", i) else { return Ok(None) };
        let Some(c) = c.filter(|c| c["world_id"].as_i64() == Some(wid)) else { continue };
        if user.gm || truthy_int(&c["visible_to_players"]) {
            let or_empty = |k: &str| if c[k].is_null() { json!("") } else { c[k].clone() };
            clips.push(json!({"id": c["id"], "name": c["name"], "description": or_empty("description"), "file_url": c["file_url"],
                "visible_to_players": truthy_int(&c["visible_to_players"]), "transcript": or_empty("transcript")}));
        }
    }
    let ps = gs["player_summary"].as_str().unwrap_or("").to_string();
    let published = truthy_int(&gs["player_summary_published"]);
    if user.gm {
        base["summary"] = json!(gs["summary"].as_str().unwrap_or(""));
        base["player_summary_published"] = json!(published);
        if published {
            base["player_summary"] = json!(ps);
        }
        base["recap_audio"] = json!(clips);
        return Ok(Some(Out::Dict(base)));
    }
    let shown = published && !strip(&ps).is_empty();
    base["player_summary"] = if shown { gs["player_summary"].clone() } else { json!("") };
    base["note"] = json!(if shown { "" } else { "No recap has been published for this session yet." });
    base["recap_audio"] = json!(clips);
    Ok(Some(Out::Dict(base)))
}

fn roll_table(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let tid = int(a, "table_id");
    let Ok(t) = row_of(db, "random_tables", "id, world_id, name, entries_json", tid) else { return Ok(None) };
    let Some(t) = t else { return Err(format!("Table {tid} not found")) };
    if let Some(wid) = t["world_id"].as_i64() {
        try_some!(load_world(db, wid, user));
    }
    let times = int(a, "times").clamp(1, 10) as usize;
    let entries = match crate::map_layout::py_loads(t["entries_json"].as_str().filter(|x| !x.is_empty()).unwrap_or("[]"))? {
        Value::Array(e) => e,
        _ => return Ok(None), // a dict / string of entries: Python's own failure
    };
    if entries.is_empty() {
        return Err("This table has no entries".into());
    }
    // weights = [max(0, int(e.get("weight", 1) or 1)) ...] - anything but a whole number is read Python's way
    let mut weights = vec![];
    for e in &entries {
        let Value::Object(e) = e else { return Ok(None) };
        let w = match e.get("weight") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => 1,
            Some(Value::Number(n)) if n.is_i64() => match n.as_i64().unwrap() { 0 => 1, x => x.max(0) },
            _ => return Ok(None),
        };
        weights.push(w as u64);
    }
    if weights.iter().sum::<u64>() == 0 {
        weights = vec![1; entries.len()];
    }
    let total: u64 = weights.iter().sum();
    let mut results = vec![];
    for _ in 0..times {
        let mut r = rand::random::<u64>() % total;
        let mut k = 0;
        while r >= weights[k] {
            r -= weights[k];
            k += 1;
        }
        results.push(entries[k].get("label").cloned().unwrap_or(json!("")));
    }
    Ok(Some(Out::Dict(json!({"table": t["name"], "results": results}))))
}

fn insert(db: &Connection, table: &str, vals: Vec<(&str, Sql)>) -> Option<i64> {
    crate::orm::insert(db, table, &vals)
}

fn text(t: String) -> Sql {
    Sql::Text(t)
}

fn create_fact(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let wid = int(a, "world_id");
    try_some!(load_world(db, wid, user));
    let content = strip(&s(a, "content").unwrap_or_default());
    if content.is_empty() {
        return Err("content must not be empty".into());
    }
    let vis = b(a, "visible_to_players").unwrap_or(true);
    let gs = a.get("game_session_id").and_then(|v| v.as_i64()).map_or(Sql::Null, Sql::Integer);
    let Some(id) = insert(db, "facts", vec![("world_id", Sql::Integer(wid)), ("game_session_id", gs), ("content", text(content.clone())),
        ("visible_to_players", Sql::Integer(vis as i64)), ("author_id", Sql::Integer(user.id)), ("updated_at", text(crate::orm::now_stamp()))]) else { return Ok(None) };
    Ok(Some(Out::Dict(json!({"id": id, "content": content, "visible_to_players": vis}))))
}

fn update_fact(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let fid = int(a, "fact_id");
    let Ok(f) = row_of(db, "facts", "id, world_id, content, visible_to_players", fid) else { return Ok(None) };
    let Some(f) = f else { return Err(format!("Fact {fid} not found")) };
    try_some!(load_world(db, f["world_id"].as_i64().unwrap_or(0), user));
    let mut content = f["content"].clone();
    let mut vis = as_bool(&f["visible_to_players"]);
    let mut vals: Vec<(&str, Sql)> = vec![];
    if let Some(c) = s(a, "content").map(|c| strip(&c)).filter(|c| !c.is_empty()) {
        content = json!(c);
        vals.push(("content", text(c)));
    }
    if let Some(v) = b(a, "visible_to_players") {
        vis = json!(v);
        vals.push(("visible_to_players", Sql::Integer(v as i64)));
    }
    vals.push(("updated_at", text(crate::orm::now_stamp())));
    if crate::orm::update(db, "facts", fid, &vals).is_none() {
        return Ok(None);
    }
    Ok(Some(Out::Dict(json!({"id": fid, "content": content, "visible_to_players": vis}))))
}

fn delete_fact(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let fid = int(a, "fact_id");
    let Ok(f) = row_of(db, "facts", "id, world_id", fid) else { return Ok(None) };
    let Some(f) = f else { return Err(format!("Fact {fid} not found")) };
    let wid = f["world_id"].as_i64().unwrap_or(0);
    try_some!(load_world(db, wid, user));
    if db.execute("DELETE FROM facts WHERE id = ?1", [fid]).is_err() {
        return Ok(None);
    }
    // no Fact row survives to carry a timestamp: the world's recap watermark records the removal
    crate::orm::update(db, "worlds", wid, &[("recap_content_touch", text(crate::orm::now_stamp()))]);
    Ok(Some(Out::Dict(json!({"deleted": fid}))))
}

fn create_entity(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let wid = int(a, "world_id");
    let w = try_some!(load_world(db, wid, user));
    let name = strip(&s(a, "name").unwrap_or_default());
    if name.is_empty() {
        return Err("name must not be empty".into());
    }
    // _world_entity_kinds: the fixed kinds plus the world's own, each once
    let Some(all) = nd_perm::sections::effective_kinds(w.1.as_deref()) else { return Ok(None) };
    let mut valid: Vec<String> = vec![];
    for k in all {
        if !valid.contains(&k) {
            valid.push(k);
        }
    }
    let kind = s(a, "kind").unwrap_or_default();
    if !valid.contains(&kind) {
        return Err(format!("kind must be one of: {}", valid.join(", ")));
    }
    let vis = b(a, "visible_to_players").unwrap_or(true);
    let Some(id) = insert(db, "entities", vec![("world_id", Sql::Integer(wid)), ("kind", text(kind.clone())), ("name", text(name.clone())),
        ("subtype", text(strip(&s(a, "subtype").unwrap_or_default()))), ("summary", text(strip(&s(a, "summary").unwrap_or_default()))),
        ("body", text(s(a, "body").unwrap_or_default())), ("tags", text(strip(&s(a, "tags").unwrap_or_default()))),
        ("folder", text(strip(&s(a, "folder").unwrap_or_default()))), ("visible_to_players", Sql::Integer(vis as i64))]) else { return Ok(None) };
    Ok(Some(Out::Dict(json!({"id": id, "kind": kind, "name": name}))))
}

fn create_note(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let wid = int(a, "world_id");
    try_some!(load_world(db, wid, user));
    let name = strip(&s(a, "name").unwrap_or_default());
    if name.is_empty() {
        return Err("name must not be empty".into());
    }
    let body = s(a, "body").unwrap_or_default();
    let summary = s(a, "summary").filter(|x| !x.is_empty()).unwrap_or_else(|| body.chars().take(120).collect());
    let vis = b(a, "visible_to_players").unwrap_or(true);
    let Some(id) = insert(db, "entities", vec![("world_id", Sql::Integer(wid)), ("kind", text("note".into())), ("name", text(name.clone())),
        ("summary", text(strip(&summary))), ("body", text(body)), ("visible_to_players", Sql::Integer(vis as i64))]) else { return Ok(None) };
    Ok(Some(Out::Dict(json!({"id": id, "name": name}))))
}

/// Set the changed columns only (SQLAlchemy writes nothing - not even an `onupdate` stamp - when no value really changed)
fn update_changed(db: &Connection, table: &str, id: i64, row: &Map<String, Value>, changes: Vec<(&str, Value)>) -> Option<()> {
    let mut vals: Vec<(&str, Sql)> = vec![];
    for (k, v) in changes {
        let cur = &row[k];
        let same = match &v {
            Value::Bool(bv) => cur.as_i64().is_some_and(|i| (i != 0) == *bv),
            other => cur == other,
        };
        if !same {
            vals.push((k, match v { Value::Bool(bv) => Sql::Integer(bv as i64), Value::String(t) => Sql::Text(t), _ => Sql::Null }));
        }
    }
    if vals.is_empty() {
        return Some(());
    }
    crate::orm::update(db, table, id, &vals).map(|_| ())
}

fn update_entity(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let eid = int(a, "entity_id");
    let Ok(e) = row_of(db, "entities", "id, world_id, name, summary, body, subtype, tags, folder, visible_to_players", eid) else { return Ok(None) };
    let Some(e) = e else { return Err(format!("Entity {eid} not found")) };
    try_some!(load_world(db, e["world_id"].as_i64().unwrap_or(0), user));
    let mut ch: Vec<(&str, Value)> = vec![];
    let mut name = e["name"].clone();
    if let Some(n) = s(a, "name").map(|n| strip(&n)).filter(|n| !n.is_empty()) {
        name = json!(n);
        ch.push(("name", json!(n)));
    }
    for k in ["summary", "subtype", "tags", "folder"] {
        if let Some(v) = s(a, k) {
            ch.push((k, json!(strip(&v))));
        }
    }
    if let Some(v) = s(a, "body") {
        ch.push(("body", json!(v)));
    }
    if let Some(v) = b(a, "visible_to_players") {
        ch.push(("visible_to_players", json!(v)));
    }
    if update_changed(db, "entities", eid, &e, ch).is_none() {
        return Ok(None);
    }
    Ok(Some(Out::Dict(json!({"id": eid, "name": name, "updated": true}))))
}

fn delete_entity(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let eid = int(a, "entity_id");
    let Ok(e) = row_of(db, "entities", "id, world_id", eid) else { return Ok(None) };
    let Some(e) = e else { return Err(format!("Entity {eid} not found")) };
    try_some!(load_world(db, e["world_id"].as_i64().unwrap_or(0), user));
    // an entity with notes: the ORM's own unlinking of them (and its failure) is Python's
    if db.query_row("SELECT 1 FROM entity_notes WHERE entity_id = ?1", [eid], |_| Ok(())).is_ok() {
        return Ok(None);
    }
    for sql in ["DELETE FROM entity_relations WHERE source_id = ?1 OR target_id = ?1", "DELETE FROM entity_links WHERE source_id = ?1 OR target_id = ?1",
                "DELETE FROM entities WHERE id = ?1"] {
        if db.execute(sql, [eid]).is_err() {
            return Ok(None);
        }
    }
    Ok(Some(Out::Dict(json!({"deleted": eid}))))
}

fn create_quest(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let wid = int(a, "world_id");
    try_some!(load_world(db, wid, user));
    let title = strip(&s(a, "title").unwrap_or_default());
    if title.is_empty() {
        return Err("title must not be empty".into());
    }
    let or = |k: &str, d: &str| Some(strip(&s(a, k).unwrap_or_default())).filter(|x| !x.is_empty()).unwrap_or_else(|| d.to_string());
    let status = or("status", "active");
    let vis = b(a, "visible_to_players").unwrap_or(true);
    let Some(id) = insert(db, "quests", vec![("world_id", Sql::Integer(wid)), ("title", text(title.clone())), ("summary", text(strip(&s(a, "summary").unwrap_or_default()))),
        ("body", text(s(a, "body").unwrap_or_default())), ("status", text(status.clone())), ("category", text(or("category", "main"))),
        ("visible_to_players", Sql::Integer(vis as i64))]) else { return Ok(None) };
    Ok(Some(Out::Dict(json!({"id": id, "title": title, "status": status}))))
}

fn update_quest(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    gm_only(user)?;
    let qid = int(a, "quest_id");
    let Ok(q) = row_of(db, "quests", "id, world_id, title, summary, body, status, category, visible_to_players", qid) else { return Ok(None) };
    let Some(q) = q else { return Err(format!("Quest {qid} not found")) };
    try_some!(load_world(db, q["world_id"].as_i64().unwrap_or(0), user));
    let mut ch: Vec<(&str, Value)> = vec![];
    let (mut title, mut status) = (q["title"].clone(), q["status"].clone());
    if let Some(t) = s(a, "title").map(|t| strip(&t)).filter(|t| !t.is_empty()) {
        title = json!(t);
        ch.push(("title", json!(t)));
    }
    if let Some(v) = s(a, "summary") {
        ch.push(("summary", json!(strip(&v))));
    }
    if let Some(v) = s(a, "body") {
        ch.push(("body", json!(v)));
    }
    if let Some(v) = s(a, "status").map(|v| strip(&v)).filter(|v| !v.is_empty()) {
        status = json!(v);
        ch.push(("status", json!(v)));
    }
    if let Some(v) = s(a, "category").map(|v| strip(&v)).filter(|v| !v.is_empty()) {
        ch.push(("category", json!(v)));
    }
    if let Some(v) = b(a, "visible_to_players") {
        ch.push(("visible_to_players", json!(v)));
    }
    if update_changed(db, "quests", qid, &q, ch).is_none() {
        return Ok(None);
    }
    Ok(Some(Out::Dict(json!({"id": qid, "title": title, "status": status, "updated": true}))))
}

// ── characters and parties ────────────────────────────────────────────────────────────────────────────────

/// Rows of a query as maps (the shape the shared sheet helpers read)
fn maps(db: &Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> Option<Vec<Map<String, Value>>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r)
}

fn world_row(db: &Connection, wid: i64) -> Option<Map<String, Value>> {
    maps(db, "SELECT * FROM worlds WHERE id = ?1", &[&wid])?.into_iter().next()
}

/// `_pc_state`: {system, native, vital, resources, conditions}, the way the party page and roster read them
fn pc_state(db: &Connection, pc: &Map<String, Value>) -> Option<Map<String, Value>> {
    let v = crate::edits::member_vitals(db, std::slice::from_ref(pc), None)?.into_iter().next()?;
    let native = v["native"].as_bool()?;
    let num = |k: &str| pc.get(k).and_then(|x| x.as_i64()).unwrap_or(0);
    let resources: Vec<Value> = if native {
        let (_, shock, pp, mp, _) = crate::edits::pc_maxima(db, pc)?;
        vec![json!({"label": "Shock", "current": num("shock_current"), "max": shock}), json!({"label": "PP", "current": num("pp_current"), "max": pp}),
             json!({"label": "MP", "current": num("mp_current"), "max": mp})]
    } else {
        v["resources"].as_array()?.iter().filter(|t| t["id"] != v["hp_id"]).map(|t| json!({"label": t["label"], "current": t["current"], "max": t["max"]})).collect()
    };
    // system_label(pc.sheet_template if pc.sheet_template_id else None, native)
    let system = match pc.get("sheet_template_id").and_then(|x| x.as_i64()).filter(|t| *t != 0) {
        Some(t) if !native => match db.query_row("SELECT name FROM sheet_templates WHERE id = ?1", [t], |r| r.get::<_, Option<String>>(0)) {
            Ok(n) => n.filter(|n| !n.is_empty()).unwrap_or_else(|| "a custom system".into()),
            Err(rusqlite::Error::QueryReturnedNoRows) => "Neon & Dragons".into(),
            Err(_) => return None,
        },
        _ => "Neon & Dragons".into(),
    };
    let vital = if v["max_hp"].as_i64().is_some_and(|m| m != 0) {
        json!({"label": v["hp_label"], "current": v["hp"], "max": v["max_hp"], "down": v["down"]})
    } else {
        Value::Null
    };
    let mut out = Map::new();
    out.insert("system".into(), json!(system));
    out.insert("native".into(), json!(native));
    out.insert("vital".into(), vital);
    out.insert("resources".into(), json!(resources));
    out.insert("conditions".into(), v["conditions"].clone());
    Some(out)
}

fn with_state(head: Value, state: Map<String, Value>) -> Value {
    let mut o = head.as_object().cloned().unwrap_or_default();
    o.extend(state);
    Value::Object(o)
}

fn list_characters(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let wid = int(a, "world_id");
    let w = try_some!(load_world(db, wid, user));
    try_some!(require_view(user, &w, "characters"));
    let Some(world) = world_row(db, wid) else { return Ok(None) };
    let filter = if user.gm { "" } else if truthy_int(&world["players_see_party"]) { " AND owner_user_id IS NOT NULL" } else { " AND owner_user_id = ?2" };
    let pcs = if filter.ends_with("?2") { maps(db, &format!("SELECT * FROM player_characters WHERE world_id = ?1{filter} ORDER BY name"), &[&wid, &user.id]) }
        else { maps(db, &format!("SELECT * FROM player_characters WHERE world_id = ?1{filter} ORDER BY name"), &[&wid]) };
    let Some(pcs) = pcs else { return Ok(None) };
    let mut out = vec![];
    for pc in &pcs {
        let Some(state) = pc_state(db, pc) else { return Ok(None) };
        let player = if pc["player_name"].is_null() { json!("") } else { pc["player_name"].clone() };
        out.push(with_state(json!({"id": pc["id"], "name": pc["name"], "player_name": player, "mine": pc["owner_user_id"].as_i64() == Some(user.id)}), state));
    }
    Ok(Some(Out::List(out)))
}

fn get_character(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let cid = int(a, "character_id");
    let Some((pc, wid)) = try_some!(character_for(db, user, cid, false)) else { return Ok(None) };
    let Some(state) = pc_state(db, &pc) else { return Ok(None) };
    let Some(mut sheet) = crate::edits::pc_markdown(db, &pc) else { return Ok(None) };
    if !user.gm {
        let Some(t) = crate::retrieval::strip(&sheet, None) else { return Ok(None) };
        sheet = t;
    }
    let mut out = with_state(json!({"id": pc["id"], "name": pc["name"], "world_id": wid}), state);
    out["sheet"] = json!(sheet);
    Ok(Some(Out::Dict(out)))
}

/// `_character_for`: the character this token may read (manage: change) and its world; Ok(Some(None)) is not used
fn character_for(db: &Connection, user: User, cid: i64, manage: bool) -> Result<Option<Option<(Map<String, Value>, i64)>>, Fail> {
    let Some(pcs) = maps(db, "SELECT * FROM player_characters WHERE id = ?1", &[&cid]) else { return Ok(None) };
    let Some(pc) = pcs.into_iter().next() else { return Err(format!("Character {cid} not found")) };
    let wid = pc["world_id"].as_i64().unwrap_or(0);
    let w = match load_world(db, wid, user)? { Some(w) => w, None => return Ok(None) };
    if require_view(user, &w, "characters")?.is_none() {
        return Ok(None);
    }
    let owner = pc["owner_user_id"].as_i64();
    let can_manage = user.gm || owner == Some(user.id);
    let allowed = if manage || can_manage { can_manage } else {
        let Some(world) = world_row(db, wid) else { return Ok(None) };
        truthy_int(&world["players_see_party"]) && owner.is_some()
    };
    if !allowed {
        return Err(format!("Character {cid} not found or not accessible to this token"));
    }
    Ok(Some(Some((pc, wid))))
}

/// `[i for i in json.loads(party.member_pc_ids_json or "[]") if isinstance(i, int)]` (None: read Python's way)
fn member_ids(party: &Map<String, Value>) -> Result<Option<Vec<i64>>, Fail> {
    let raw = party["member_pc_ids_json"].as_str().filter(|t| !t.is_empty()).unwrap_or("[]");
    Ok(match crate::map_layout::py_loads(raw)? {
        Value::Array(a) => {
            if a.iter().any(|v| v.is_boolean() || v.is_u64() && !v.is_i64()) {
                return Ok(None);
            }
            Some(a.iter().filter_map(|v| if v.is_i64() { v.as_i64() } else { None }).collect())
        }
        Value::Object(_) | Value::String(_) => Some(vec![]),
        _ => None,
    })
}

fn list_parties(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let wid = int(a, "world_id");
    let w = try_some!(load_world(db, wid, user));
    try_some!(require_view(user, &w, "parties"));
    let Some(world) = world_row(db, wid) else { return Ok(None) };
    let Some(mine) = rows(db, "SELECT id FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2", &[&wid, &user.id], |r| Ok(json!(r.get::<_, i64>(0)?))) else { return Ok(None) };
    let Some(parties) = maps(db, "SELECT * FROM parties WHERE world_id = ?1 ORDER BY name", &[&wid]) else { return Ok(None) };
    let mut out = vec![];
    for party in &parties {
        let Some(ids) = member_ids(party)? else { return Ok(None) };
        if !(user.gm || truthy_int(&world["players_see_party"]) || ids.iter().any(|i| mine.contains(&json!(i)))) {
            continue;
        }
        let names = if ids.is_empty() { vec![] } else {
            let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
            let Some(n) = rows(db, &format!("SELECT name FROM player_characters WHERE id IN ({list}) ORDER BY name"), &[], |r| Ok(sql_json(r.get(0)?))) else { return Ok(None) };
            n
        };
        out.push(json!({"id": party["id"], "name": party["name"], "members": names}));
    }
    Ok(Some(Out::List(out)))
}

/// `_party_for`: (party, world id, members by name) this token may read
fn party_for(db: &Connection, user: User, pid: i64, gm_only_: bool) -> Result<Option<(Map<String, Value>, i64, Vec<Map<String, Value>>)>, Fail> {
    if gm_only_ {
        gm_only(user)?;
    }
    let Some(ps) = maps(db, "SELECT * FROM parties WHERE id = ?1", &[&pid]) else { return Ok(None) };
    let Some(party) = ps.into_iter().next() else { return Err(format!("Party {pid} not found")) };
    let wid = party["world_id"].as_i64().unwrap_or(0);
    let Some(w) = load_world(db, wid, user)? else { return Ok(None) };
    if require_view(user, &w, "parties")?.is_none() {
        return Ok(None);
    }
    let Some(ids) = member_ids(&party)? else { return Ok(None) };
    let members = if ids.is_empty() { vec![] } else {
        let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        let Some(m) = maps(db, &format!("SELECT * FROM player_characters WHERE id IN ({list}) AND world_id = ?1 ORDER BY name"), &[&wid]) else { return Ok(None) };
        m
    };
    let Some(world) = world_row(db, wid) else { return Ok(None) };
    if !user.gm && !truthy_int(&world["players_see_party"]) && !members.iter().any(|m| m["owner_user_id"].as_i64() == Some(user.id)) {
        return Err(format!("Party {pid} not found or not accessible to this token"));
    }
    Ok(Some((party, wid, members)))
}

fn get_party(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let pid = int(a, "party_id");
    let Some((party, _wid, members)) = party_for(db, user, pid, false)? else { return Ok(None) };
    let mut goals = party["goals"].as_str().unwrap_or("").to_string();
    if !user.gm {
        let Some(t) = crate::retrieval::strip(&goals, None) else { return Ok(None) };
        goals = t;
    }
    let mut ms = vec![];
    for pc in &members {
        let Some(state) = pc_state(db, pc) else { return Ok(None) };
        ms.push(with_state(json!({"id": pc["id"], "name": pc["name"]}), state));
    }
    let Some(loot) = crate::edits::load_loot(&party) else { return Ok(None) };
    let loot: Vec<Value> = loot.iter().map(|i| {
        let claimed: Vec<Value> = members.iter().filter(|m| i["claimed_by"].as_array().is_some_and(|c| c.contains(&m["id"]))).map(|m| m["name"].clone()).collect();
        json!({"name": i["name"], "qty": i["qty"], "notes": i["notes"], "claimed_by": claimed})
    }).collect();
    Ok(Some(Out::Dict(json!({"id": party["id"], "name": party["name"], "goals": goals, "members": ms, "loot": loot}))))
}

// ── character changes ────────────────────────────────────────────────────────────────────────────────────

/// Write the columns whose value really changes (an unchanged flush writes nothing, not even `updated_at`)
fn write_changed(db: &Connection, table: &str, id: i64, row: &Map<String, Value>, vals: Vec<(&str, Sql)>) -> Option<()> {
    let changed: Vec<(&str, Sql)> = vals.into_iter().filter(|(k, v)| {
        let cur = row.get(*k).cloned().unwrap_or(Value::Null);
        match v {
            Sql::Integer(i) => cur.as_i64() != Some(*i) || !cur.is_i64(),
            Sql::Text(t) => cur.as_str() != Some(t.as_str()),
            Sql::Null => !cur.is_null(),
            _ => true,
        }
    }).collect();
    if changed.is_empty() {
        return Some(());
    }
    crate::orm::update(db, table, id, &changed).map(|_| ())
}

fn reread_pc(db: &Connection, cid: i64) -> Option<Map<String, Value>> {
    maps(db, "SELECT * FROM player_characters WHERE id = ?1", &[&cid])?.into_iter().next()
}

fn state_answer(db: &Connection, cid: i64) -> ToolResult {
    let Some(pc) = reread_pc(db, cid) else { return Ok(None) };
    let Some(state) = pc_state(db, &pc) else { return Ok(None) };
    Ok(Some(Out::Dict(with_state(json!({"id": pc["id"], "name": pc["name"]}), state))))
}

fn adjust_character_resource(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let (delta, value) = (a.get("delta").and_then(|v| v.as_i64()), a.get("value").and_then(|v| v.as_i64()));
    if delta.is_none() == value.is_none() {
        return Err("Pass exactly one of delta or value".into());
    }
    let cid = int(a, "character_id");
    let Some((pc, wid)) = try_some!(character_for(db, user, cid, true)) else { return Ok(None) };
    let Some((hp, shock, pp, mp, native)) = crate::edits::pc_maxima(db, &pc) else { return Ok(None) };
    let resource = s(a, "resource").unwrap_or_default();
    if native {
        let key = strip(&resource).to_lowercase();
        let (col, top) = match key.as_str() {
            "hp" => ("current_hp", hp),
            "shock" => ("shock_current", shock),
            "pp" => ("pp_current", pp),
            "mp" => ("mp_current", mp),
            _ => return Err("A native sheet's resources are: hp, shock, pp, mp".into()),
        };
        let cur = pc[col].as_i64().unwrap_or(0);
        let new = value.unwrap_or_else(|| cur + delta.unwrap_or(0));
        let new = if top > 0 { new.min(top).max(0) } else { new.max(0) };
        if write_changed(db, "player_characters", cid, &pc, vec![(col, Sql::Integer(new))]).is_none() {
            return Ok(None);
        }
    } else {
        let Some(tpl) = crate::sheets::pc_template(db, &pc) else { return Ok(None) };
        let Some(tpl) = tpl else { return Err("This character has no sheet template to take resources from".into()) };
        let Some(mut cf) = crate::sheets::custom_fields(&pc) else { return Ok(None) };
        let Some(tracks) = tpl.tracks(&cf, true) else { return Ok(None) };
        // find_track: by id or (short) label, case-insensitive
        let key = strip(&resource).to_lowercase();
        let found = if key.is_empty() { None } else {
            tracks.iter().find(|t| [py_str(&t["id"]), py_str(&t["label"])].iter().any(|x| x.to_lowercase() == key))
        };
        let Some(track) = found else {
            let names: Vec<String> = tracks.iter().map(|t| format!("{} (id {})", py_str(&t["label"]), py_str(&t["id"]))).collect();
            return Err(format!("no such resource {}; this sheet has: {}", py_repr(&json!(resource)), names.join(", ")));
        };
        let (Some(cur), Some(mx)) = (track["current"].as_i64(), track["max"].as_i64()) else { return Ok(None) };
        let new = value.unwrap_or_else(|| cur + delta.unwrap_or(0));
        let new = if mx > 0 { new.min(mx).max(0) } else { new.max(0) };
        cf.insert(format!("{}_current", py_str(&track["id"])), json!(new));
        if write_changed(db, "player_characters", cid, &pc, vec![("custom_fields_json", Sql::Text(nd_render::py_dumps(&Value::Object(cf))))]).is_none() {
            return Ok(None);
        }
    }
    crate::orm::touch(db, wid);
    state_answer(db, cid)
}

fn set_character_conditions(db: &Connection, user: User, a: &Map<String, Value>, list: &[Value]) -> ToolResult {
    let cid = int(a, "character_id");
    let Some((pc, wid)) = try_some!(character_for(db, user, cid, true)) else { return Ok(None) };
    let Some(clean) = crate::edits::clean_conditions(&json!(list)) else { return Ok(None) };
    if write_changed(db, "player_characters", cid, &pc, vec![("conditions_json", Sql::Text(nd_render::py_dumps(&json!(clean))))]).is_none() {
        return Ok(None);
    }
    crate::orm::touch(db, wid);
    state_answer(db, cid)
}

fn award_character_xp(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let cid = int(a, "character_id");
    let Some((pc, wid)) = try_some!(character_for(db, user, cid, true)) else { return Ok(None) };
    gm_only(user)?;
    let amount = int(a, "amount");
    if amount.abs() > 100000 {
        return Err("amount out of range".into());
    }
    let Some(tpl) = crate::sheets::pc_template(db, &pc) else { return Ok(None) };
    match tpl.filter(|t| t.custom()) {
        Some(t) => {
            let Some((_, xp_ids)) = t.meta() else { return Ok(None) };
            if xp_ids.is_empty() {
                return Err(format!("{} has no XP field to award", py_str(t.row.get("name").unwrap_or(&Value::Null))));
            }
            // apply_xp_award: each XP field grows from its saved value, else its default, never below 0
            let Some(mut cf) = crate::sheets::custom_fields(&pc) else { return Ok(None) };
            for fid in xp_ids {
                let Ok(saved) = crate::edits::sheet_num_pub(cf.get(&fid)) else { return Ok(None) };
                let base = match saved {
                    Some(b) => b,
                    None => {
                        let dv = t.fields.iter().find(|f| f.get("id").and_then(|v| v.as_str()) == Some(fid.as_str())).and_then(|f| f.get("default_value").cloned());
                        let Ok(d) = crate::edits::sheet_num_pub(dv.as_ref()) else { return Ok(None) };
                        d.unwrap_or(0)
                    }
                };
                cf.insert(fid, json!((base + amount).max(0)));
            }
            if write_changed(db, "player_characters", cid, &pc, vec![("custom_fields_json", Sql::Text(nd_render::py_dumps(&Value::Object(cf))))]).is_none() {
                return Ok(None);
            }
        }
        None => {
            let xp = (pc["xp"].as_i64().unwrap_or(0) + amount).max(0);
            if write_changed(db, "player_characters", cid, &pc, vec![("xp", Sql::Integer(xp))]).is_none() {
                return Ok(None);
            }
        }
    }
    crate::orm::touch(db, wid);
    Ok(Some(Out::Dict(json!({"id": pc["id"], "name": pc["name"], "awarded": amount}))))
}

fn rest_party(db: &Connection, user: User, a: &Map<String, Value>) -> ToolResult {
    let kind = s(a, "kind").unwrap_or_default();
    if kind != "short" && kind != "long" {
        return Err("kind must be \"short\" or \"long\"".into());
    }
    let pid = int(a, "party_id");
    let Some((party, wid, _members)) = party_for(db, user, pid, true)? else { return Ok(None) };
    // apply_party_rest: every listed character (any world), in the database's own order
    let raw = party["member_pc_ids_json"].as_str().filter(|t| !t.is_empty()).unwrap_or("[]");
    let Ok(Value::Array(all)) = crate::map_layout::py_loads(raw) else { return Ok(None) };
    // `PlayerCharacter.id.in_(ids)`: text that is no number never matches an integer id; numeric text, a bool or a float would
    let mut ids = vec![];
    for v in all {
        match v {
            Value::Number(n) if n.is_i64() => ids.push(n.as_i64().unwrap_or(0)),
            Value::Null => {}
            Value::String(t) if t.trim().parse::<f64>().is_err() && !t.trim().starts_with(|c: char| c.is_ascii_digit() || c == '.' || c == '-' || c == '+') => {}
            _ => return Ok(None),
        }
    }
    let pcs = if ids.is_empty() { vec![] } else {
        let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        let Some(p) = maps(db, &format!("SELECT * FROM player_characters WHERE id IN ({list})"), &[]) else { return Ok(None) };
        p
    };
    let mut rested = vec![];
    for pc in &pcs {
        let Some((_, shock, pp, mp, native)) = crate::edits::pc_maxima(db, pc) else { return Ok(None) };
        let Some(tpl) = crate::sheets::pc_template(db, pc) else { return Ok(None) };
        let ops = tpl.as_ref().map(|t| t.rest_ops(&kind)).unwrap_or_default();
        if !native && ops.is_empty() {
            continue;
        }
        let cid = pc["id"].as_i64().unwrap_or(0);
        // _refresh_feats_after_rest: the once-per-Rest feats come back
        let Some(prefs) = crate::edits::pc_prefs(db, cid) else { return Ok(None) };
        if let Some(used) = prefs.get("feat_used").filter(|v| crate::edits::py_truthy_pub(Some(v))) {
            let Some(used) = used.as_object() else { return Ok(None) };
            if used.values().any(|v| v == "rest") {
                let keep: Map<String, Value> = used.iter().filter(|(_, v)| *v != "rest").map(|(k, v)| (k.clone(), v.clone())).collect();
                if crate::edits::set_pc_pref_db(db, pc, "feat_used", (!keep.is_empty()).then(|| Value::Object(keep))).is_none() {
                    return Ok(None);
                }
            }
        }
        let mut set: Vec<(&str, Sql)> = vec![];
        if native {
            let num = |k: &str| pc[k].as_i64().unwrap_or(0);
            set.push(("pp_current", Sql::Integer(pp.min(num("pp_current") + pp.div_euclid(2)))));
            set.push(("mp_current", Sql::Integer(mp.min(num("mp_current") + mp.div_euclid(2)))));
            set.push(("shock_current", Sql::Integer(shock)));
        }
        if !ops.is_empty() {
            let Some(t) = tpl.as_ref() else { return Ok(None) };
            let Some(cf) = crate::sheets::custom_fields(pc).and_then(|cf| t.apply_rest(&cf, &kind)) else { return Ok(None) };
            set.push(("custom_fields_json", Sql::Text(nd_render::py_dumps(&Value::Object(cf)))));
        }
        if write_changed(db, "player_characters", cid, pc, set).is_none() {
            return Ok(None);
        }
        rested.push(pc["name"].clone());
    }
    crate::orm::touch(db, wid);
    Ok(Some(Out::Dict(json!({"party": party["name"], "kind": kind, "rested": rested}))))
}
