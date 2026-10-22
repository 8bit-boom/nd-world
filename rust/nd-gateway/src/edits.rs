//! Phase 3: the app's form and JSON writes, made by the gateway (docs/RUST_WRITES.md). Same rule as the dice roll: the gateway answers
//! **only the successful case**, after re-doing every check Python does; anything else - a refusal, a missing row, input Python would read
//! differently or reject, a row of another world (whose access rules are not ported) - goes to Python **unchanged**, body included.
//! Rows are written as SQLAlchemy writes them (crate::orm: Python-side defaults, `onupdate`, no UPDATE when nothing changed).

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Request, Response, StatusCode, Uri};
use percent_encoding::utf8_percent_encode;
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::form::{content_type, py_int, Form};
use crate::layout::PageCtx;
use crate::orm;
use crate::proxy::Proxy;

const MAX_BODY: usize = 256 * 1024;

pub struct Ctx<'a> {
    pub db: Connection,
    pub p: PageCtx,
    pub uri: &'a Uri,
    pub headers: &'a HeaderMap,
    pub body: &'a [u8],
}

impl Ctx<'_> {
    pub fn form(&self) -> Option<Form> {
        if content_type(self.headers) != "application/x-www-form-urlencoded" {
            return None;
        }
        Form::parse(self.body)
    }

    /// `parse_json(body)` as an object (anything else makes Python fail - its own answer)
    pub fn json(&self) -> Option<Map<String, Value>> {
        match serde_json::from_slice::<Value>(self.body).ok()? {
            Value::Object(o) => Some(o),
            _ => None,
        }
    }

    pub fn world_id(&self) -> Option<i64> {
        self.p.world.as_ref()?.get("id")?.as_i64()
    }

    pub fn is_assistant(&self) -> bool {
        self.p.role.as_deref() == Some("assistant")
    }

    /// `world_can_edit_section(request, active world, section)`
    pub fn can_edit_section(&self, section: &str) -> Option<bool> {
        crate::pages::edit_level(&self.p, self.p.world.as_ref()?, section)
    }

    /// `world_can_edit_row(request, active world, section, created_by)`
    pub fn can_edit_row(&self, section: &str, created_by: Option<i64>) -> Option<bool> {
        Some(self.can_edit_section(section)? && (self.p.is_gm || self.is_assistant() || created_by == Some(self.p.user_id)))
    }
}

/// Starlette's RedirectResponse(url, 303)
pub fn redirect(url: &str) -> Option<Response<Body>> {
    let loc = utf8_percent_encode(url, crate::pages::LOCATION_SAFE).to_string();
    Response::builder()
        .status(StatusCode::SEE_OTHER)
        .header(header::LOCATION, loc)
        .header(header::CONTENT_LENGTH, "0")
        .header("x-nd-served-by", "gateway")
        .body(Body::empty())
        .ok()
}

/// FastAPI's JSONResponse: `json.dumps(content, ensure_ascii=False, separators=(",", ":"))`, keys in the handler's order
pub fn json_response(v: &Value) -> Option<Response<Body>> {
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/json")
        .header("x-nd-served-by", "gateway")
        .body(Body::from(serde_json::to_string(v).ok()?))
        .ok()
}

type Handler = fn(&mut Ctx, &[i64]) -> Option<Response<Body>>;

/// (pattern with `{}` for an integer id, handler)
const ROUTES: &[(&str, Handler)] = &[
    ("/quests/new", quest_create),
    ("/quests/{}/edit", quest_edit),
    ("/quests/{}/delete", quest_delete),
    ("/api/quests/{}/status", quest_status),
];

/// The handler for a POST path and the ids in it (digits only, as FastAPI's int converter reads them here)
fn route(path: &str) -> Option<(Handler, Vec<i64>)> {
    'outer: for (pat, h) in ROUTES {
        let (mut rest, mut ids) = (path, vec![]);
        let parts: Vec<&str> = pat.split("{}").collect();
        for (i, part) in parts.iter().enumerate() {
            rest = rest.strip_prefix(part).map_or_else(|| "\u{0}", |r| r);
            if rest == "\u{0}" {
                continue 'outer;
            }
            if i + 1 < parts.len() {
                let n = rest.bytes().take_while(|b| b.is_ascii_digit()).count();
                if n == 0 || n > 15 {
                    continue 'outer;
                }
                ids.push(rest[..n].parse().ok()?);
                rest = &rest[n..];
            }
        }
        if rest.is_empty() {
            return Some((*h, ids));
        }
    }
    None
}

pub fn handles(method: &Method, path: &str) -> bool {
    method == Method::POST && route(path).is_some()
}

fn run(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let (h, ids) = route(uri.path())?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let mut ctx = Ctx { db, p, uri, headers, body };
    h(&mut ctx, &ids)
}

/// Entry point: reads the (small) body, tries to answer, and otherwise forwards the request - body included - to Python.
pub async fn serve(cfg: Arc<Config>, proxy: &Proxy, req: Request<Body>, peer: Option<std::net::SocketAddr>) -> Response<Body> {
    let small = req.headers().get(header::CONTENT_LENGTH).and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<usize>().ok()).map_or(false, |n| n <= MAX_BODY);
    if !small {
        return proxy.forward(req, peer).await;
    }
    let (parts, body) = req.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, MAX_BODY).await else {
        return Response::builder().status(StatusCode::BAD_REQUEST).body(Body::empty()).unwrap();
    };
    let (uri, headers) = (parts.uri.clone(), parts.headers.clone());
    let b = bytes.clone();
    let answer = tokio::task::spawn_blocking(move || run(&cfg, &uri, &headers, &b)).await.ok().flatten();
    match answer {
        Some(r) => r,
        None => proxy.forward(Request::from_parts(parts, Body::from(bytes)), peer).await,
    }
}

// ── helpers ──────────────────────────────────────────────────────────────────

fn s(t: &str) -> Sql {
    Sql::Text(t.to_string())
}

fn strip(t: &str) -> String {
    nd_render::text::py_strip(t).to_string()
}

/// `str(form.get(name, default)).strip() or fallback`
fn field_or(f: &Form, name: &str, default: &str, fallback: &str) -> String {
    let v = strip(f.get(name).unwrap_or(default));
    if v.is_empty() { fallback.to_string() } else { v }
}

/// `int(form[name]) if form.get(name) else None` (Err: a value Python's int() would read differently - Python's call)
fn opt_int_field(f: &Form, name: &str) -> Result<Option<i64>, ()> {
    match f.get(name) {
        None | Some("") => Ok(None),
        Some(v) => py_int(v).map(Some).ok_or(()),
    }
}

/// `raw if json.loads(raw) succeeds else "[]"` - text this parser rejects but Python might read (NaN...) is Python's call
fn links_json(f: &Form) -> Option<String> {
    let raw = match f.get("linked_entities_json") {
        None | Some("") => "[]",
        Some(v) => v,
    };
    serde_json::from_str::<Value>(raw).ok()?;
    Some(raw.to_string())
}

fn row(db: &Connection, sql: &str, id: i64) -> Option<Map<String, Value>> {
    db.query_row(sql, [id], |r| crate::layout::row_map(r)).ok()
}

// ── quests (app/routers/quests.py) ───────────────────────────────────────────

/// The quest by id, if it belongs to the viewer's active world and they may edit it
fn editable_quest(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let q = row(&c.db, "SELECT * FROM quests WHERE id = ?1", id)?;
    if q.get("world_id")?.as_i64() != c.world_id() {
        return None;
    }
    c.can_edit_row("quests", q.get("created_by_user_id").and_then(|v| v.as_i64()))?.then_some(q)
}

fn quest_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("quests")? {
        return None;
    }
    let parent = opt_int_field(&f, "parent_id").ok()?;
    let party = opt_int_field(&f, "assigned_party_id").ok()?;
    let owner = if c.p.is_gm || c.is_assistant() { Sql::Null } else { Sql::Integer(c.p.user_id) };
    let id = orm::insert(
        &c.db,
        "quests",
        &[
            ("world_id", Sql::Integer(wid)),
            ("title", s(&field_or(&f, "title", "", "Untitled Quest"))),
            ("status", s(&field_or(&f, "status", "active", "active"))),
            ("category", s(&field_or(&f, "category", "main", "main"))),
            ("summary", s(&strip(f.get("summary").unwrap_or("")))),
            ("body", s(f.get("body").unwrap_or(""))),
            ("parent_id", parent.map_or(Sql::Null, Sql::Integer)),
            ("assigned_party_id", party.map_or(Sql::Null, Sql::Integer)),
            ("created_by_user_id", owner),
            ("linked_entities_json", s(&links_json(&f)?)),
        ],
    )?;
    redirect(&format!("/quests/{id}"))
}

fn quest_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let f = c.form()?;
    let q = editable_quest(c, id)?;
    let old_title = q.get("title")?.as_str()?.to_string();
    let title = field_or(&f, "title", &old_title, &old_title);
    let parent = match f.get("parent_id") {
        None | Some("") => None,
        Some(v) => Some(py_int(v)?).filter(|p| *p != id),
    };
    let party = opt_int_field(&f, "assigned_party_id").ok()?;
    let links = links_json(&f)?;
    let tx = c.db.unchecked_transaction().ok()?;
    orm::update(
        &tx,
        "quests",
        id,
        &[
            ("title", s(&title)),
            ("status", s(&field_or(&f, "status", "active", "active"))),
            ("category", s(&field_or(&f, "category", "main", "main"))),
            ("summary", s(&strip(f.get("summary").unwrap_or("")))),
            ("body", s(f.get("body").unwrap_or(""))),
            ("parent_id", parent.map_or(Sql::Null, Sql::Integer)),
            ("assigned_party_id", party.map_or(Sql::Null, Sql::Integer)),
            ("linked_entities_json", s(&links)),
        ],
    )?;
    tx.commit().ok()?;
    redirect(&format!("/quests/{id}?saved=1"))
}

fn quest_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    editable_quest(c, id)?;
    let tx = c.db.unchecked_transaction().ok()?;
    orm::bulk_update(&tx, "quests", &[("parent_id", Sql::Null)], "quests.parent_id = ?", &[Sql::Integer(id)])?;
    tx.execute("DELETE FROM quests WHERE id = ?1", [id]).ok()?;
    tx.commit().ok()?;
    redirect("/quests")
}

fn quest_status(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let body = c.json()?;
    let q = editable_quest(c, id)?;
    let status = match body.get("status") {
        None => String::new(),
        Some(Value::String(v)) => strip(v),
        Some(_) => return None, // str() of another JSON type ("None", "True", "[1, 2]"): Python's call
    };
    let mut current = q.get("status").cloned().unwrap_or(Value::Null);
    if !status.is_empty() {
        let tx = c.db.unchecked_transaction().ok()?;
        orm::update(&tx, "quests", id, &[("status", s(&status))])?;
        orm::touch(&tx, q.get("world_id")?.as_i64()?)?;
        tx.commit().ok()?;
        current = json!(status);
    }
    json_response(&json!({"status": current}))
}
