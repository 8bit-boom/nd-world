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
        if self.body.is_empty() && content_type(self.headers).is_empty() {
            return Some(Form(vec![])); // no body at all: Starlette's request.form() is empty
        }
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
    ("/facts/new", fact_create),
    ("/facts/{}/edit", fact_edit),
    ("/facts/{}/delete", fact_delete),
    ("/tables/new", table_create),
    ("/tables/{}/edit", table_update),
    ("/tables/{}/delete", table_delete),
    ("/api/tables/{}/roll", table_roll),
    ("/api/calendar/events", calendar_event_add),
    ("/api/calendar/events/{}/delete", calendar_event_delete),
    ("/api/calendar/advance", calendar_advance),
    ("/api/calendar/set-date", calendar_set_date),
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

// ── facts (app/routers/facts.py) ─────────────────────────────────────────────

/// `_safe_next`: "/facts" or "/sessions/<digits>", else "/facts" (None: a value Python's regex might read differently)
fn facts_next(f: &Form) -> Option<String> {
    let v = f.get("next").unwrap_or("");
    if !v.is_ascii() {
        return None;
    }
    let ok = v == "/facts" || v.strip_prefix("/sessions/").map_or(false, |d| !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit()));
    Some(if ok { v.to_string() } else { "/facts".to_string() })
}

/// `_normalize_tags_field` for a form value (text or nothing)
fn fact_tags(f: &Form) -> Sql {
    match f.get("tags").map(strip) {
        Some(t) if !t.is_empty() => s(&t),
        _ => Sql::Null,
    }
}

fn fact_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("facts")? {
        return None;
    }
    let next = facts_next(&f)?;
    let content = strip(f.get("content").unwrap_or(""));
    if content.is_empty() {
        return redirect(&next);
    }
    let session = opt_int_field(&f, "game_session_id").ok()?;
    orm::insert(
        &c.db,
        "facts",
        &[
            ("world_id", Sql::Integer(wid)),
            ("game_session_id", session.map_or(Sql::Null, Sql::Integer)),
            ("content", s(&content)),
            ("visible_to_players", Sql::Integer(f.has("visible_to_players") as i64)),
            ("tags", fact_tags(&f)),
            ("author_id", Sql::Integer(c.p.user_id)),
            ("updated_at", s(&orm::now_stamp())),
        ],
    )?;
    redirect(&next)
}

/// The fact by id, in the viewer's active world, and the right to edit facts there
fn editable_fact(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let fact = row(&c.db, "SELECT * FROM facts WHERE id = ?1", id)?;
    if fact.get("world_id")?.as_i64() != c.world_id() || !c.can_edit_section("facts")? {
        return None;
    }
    Some(fact)
}

fn fact_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let f = c.form()?;
    editable_fact(c, id)?;
    let next = facts_next(&f)?;
    let content = strip(f.get("content").unwrap_or(""));
    let session = opt_int_field(&f, "game_session_id").ok()?;
    let mut set: Vec<(&str, Sql)> = vec![];
    if !content.is_empty() {
        set.push(("content", s(&content)));
    }
    set.push(("visible_to_players", Sql::Integer(f.has("visible_to_players") as i64)));
    set.push(("tags", fact_tags(&f)));
    if let Some(sid) = session {
        set.push(("game_session_id", Sql::Integer(sid)));
    }
    set.push(("updated_at", s(&orm::now_stamp())));
    orm::update(&c.db, "facts", id, &set)?;
    redirect(&next)
}

fn fact_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let next = facts_next(&c.form()?)?;
    editable_fact(c, id)?;
    let wid = c.world_id()?;
    let tx = c.db.unchecked_transaction().ok()?;
    tx.execute("DELETE FROM facts WHERE id = ?1", [id]).ok()?;
    // _bump_recap_content_touch: the world's recap-staleness watermark (a deleted fact leaves no row to timestamp)
    orm::update(&tx, "worlds", wid, &[("recap_content_touch", s(&orm::now_stamp()))])?;
    tx.commit().ok()?;
    redirect(&next)
}

// ── random tables (app/routers/tables.py) ────────────────────────────────────

/// `_slugify`: lowercase ASCII words joined by "-", at most 50 characters, unique over every table ("-2", "-3", ...); None for a non-ASCII
/// name (Python lowercases first, which can turn a non-ASCII letter into an ASCII one)
fn table_slug(db: &Connection, name: &str) -> Option<String> {
    if !name.is_ascii() {
        return None;
    }
    let mut slug = String::new();
    let mut dash = false;
    for ch in name.to_ascii_lowercase().chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            slug.push(ch);
            dash = false;
        } else if !dash {
            slug.push('-');
            dash = true;
        }
    }
    let base: String = slug.trim_matches('-').chars().take(50).collect();
    let base = if base.is_empty() { "table".to_string() } else { base };
    let taken = |sl: &str| -> Option<bool> {
        db.query_row("SELECT count(*) FROM random_tables WHERE slug = ?1", [sl], |r| r.get::<_, i64>(0)).ok().map(|n| n > 0)
    };
    let mut slug = base.clone();
    let mut n = 2;
    while taken(&slug)? {
        slug = format!("{base}-{n}");
        n += 1;
    }
    Some(slug)
}

/// `str(form.get("entries_json", "[]") or "[]")` kept when it parses as JSON, else "[]"
fn entries_json(f: &Form) -> Option<String> {
    let raw = match f.get("entries_json") {
        None | Some("") => "[]",
        Some(v) => v,
    };
    serde_json::from_str::<Value>(raw).ok()?;
    Some(raw.to_string())
}

fn table_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("tables")? {
        return None;
    }
    let name = field_or(&f, "name", "", "Unnamed Table");
    let owner = if c.p.is_gm || c.is_assistant() { Sql::Null } else { Sql::Integer(c.p.user_id) };
    let tx = c.db.unchecked_transaction().ok()?;
    let slug = table_slug(&tx, &name)?;
    let id = orm::insert(
        &tx,
        "random_tables",
        &[
            ("world_id", Sql::Integer(wid)),
            ("name", s(&name)),
            ("slug", s(&slug)),
            ("category", s(&field_or(&f, "category", "", "general"))),
            ("description", s(&strip(f.get("description").unwrap_or("")))),
            ("is_builtin", Sql::Integer(0)),
            ("entries_json", s(&entries_json(&f)?)),
            ("created_by_user_id", owner),
        ],
    )?;
    tx.commit().ok()?;
    redirect(&format!("/tables/{id}/edit"))
}

/// The table by id when it is global or of the viewer's active world and they may edit it (`_can_edit_table_row`)
fn editable_table(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let t = row(&c.db, "SELECT * FROM random_tables WHERE id = ?1", id)?;
    match t.get("world_id").and_then(|v| v.as_i64()) {
        None => {}
        Some(w) if Some(w) == c.world_id() => {}
        Some(_) => return None,
    }
    crate::pages::can_edit_table_row(&c.db, &c.p, &t)?.then_some(t)
}

fn table_update(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let f = c.form()?;
    let t = editable_table(c, id)?;
    let mut set: Vec<(&str, Sql)> = vec![];
    let name;
    if t.get("is_builtin").and_then(|v| v.as_i64()).unwrap_or(0) == 0 {
        let old = t.get("name")?.as_str()?.to_string();
        name = field_or(&f, "name", &old, &old);
        set.push(("name", s(&name)));
        set.push(("category", s(&field_or(&f, "category", "", "general"))));
        set.push(("description", s(&strip(f.get("description").unwrap_or("")))));
    }
    set.push(("entries_json", s(&entries_json(&f)?)));
    orm::update(&c.db, "random_tables", id, &set)?;
    redirect(&format!("/tables/{id}/edit?saved=1"))
}

fn table_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let t = editable_table(c, id)?;
    if t.get("is_builtin").and_then(|v| v.as_i64()).unwrap_or(0) != 0 {
        return None;
    }
    c.db.execute("DELETE FROM random_tables WHERE id = ?1", [id]).ok()?;
    redirect("/tables")
}

fn table_roll(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    use rand::Rng;
    let t = row(&c.db, "SELECT * FROM random_tables WHERE id = ?1", ids[0])?;
    // world_row_visible: a global table, or one of the viewer's active world with the section open to them
    if let Some(w) = t.get("world_id").and_then(|v| v.as_i64()) {
        let world = c.p.world.clone()?;
        if Some(w) != c.world_id() || !crate::pages::section_ok(&c.p, Some(&world), "tables")? {
            return None;
        }
    }
    let raw = t.get("entries_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let entries = serde_json::from_str::<Value>(raw).ok()?.as_array()?.clone();
    if entries.is_empty() {
        return None;
    }
    // weights: max(0, int(e.get("weight", 1) or 1)) - whole numbers only here
    let mut weights = vec![];
    for e in &entries {
        let e = e.as_object()?;
        let w = match e.get("weight") {
            None | Some(Value::Null) => 1,
            Some(Value::Number(n)) if n.is_i64() => match n.as_i64()? {
                0 => 1,
                x => x.max(0),
            },
            Some(Value::Bool(false)) => 1,
            _ => return None,
        };
        weights.push(w);
    }
    let sum: i64 = weights.iter().sum();
    let total = if sum == 0 { entries.len() as i64 } else { sum };
    if sum == 0 {
        weights = vec![1; entries.len()];
    }
    let mut rng = rand::thread_rng();
    let roll = rng.gen_range(1..=total);
    let pick_sum: i64 = weights.iter().sum();
    let mut x = rng.gen_range(0..pick_sum);
    let mut chosen = &entries[entries.len() - 1];
    for (e, w) in entries.iter().zip(&weights) {
        if x < *w {
            chosen = e;
            break;
        }
        x -= w;
    }
    let label = chosen.get("label").cloned().unwrap_or(json!(""));
    json_response(&json!({"result": label, "roll": roll, "total": total}))
}

// ── calendar (app/routers/calendar.py) ───────────────────────────────────────

/// `_safe_int(value, default)` for a JSON value: whole numbers and missing / null only (anything else - a float, text, a bool - is
/// read by Python's int() in its own way: Python's call)
fn safe_int_json(v: Option<&Value>, default: i64) -> Option<i64> {
    match v {
        None | Some(Value::Null) => Some(default),
        Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
        _ => None,
    }
}

/// `int(body[k]) if body.get(k) else None` for a JSON value
fn opt_int_json(v: Option<&Value>) -> Result<Option<i64>, ()> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n)) if n.is_i64() => Ok(match n.as_i64() {
            Some(0) => None,
            x => x,
        }),
        Some(Value::String(t)) if t.is_empty() => Ok(None),
        Some(Value::String(t)) => py_int(t).map(Some).ok_or(()),
        Some(Value::Bool(false)) => Ok(None),
        _ => Err(()),
    }
}

/// `str(body.get(k, default))` for text (or a missing key)
fn json_text<'a>(b: &'a Map<String, Value>, k: &str, default: &'a str) -> Option<&'a str> {
    match b.get(k) {
        None => Some(default),
        Some(Value::String(t)) => Some(t),
        Some(_) => None,
    }
}

/// `_can_manage_calendar`: calendar edit rights, GM or assistant
fn can_manage_calendar(c: &Ctx) -> Option<bool> {
    Some(c.can_edit_section("calendar")? && (c.p.is_gm || c.is_assistant()))
}

fn calendar_event_add(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("calendar")? {
        return None;
    }
    let (_, days, _) = crate::calendar::for_write(&c.db, wid)?;
    let day = crate::calendar::clamp_day(&days, safe_int_json(body.get("day"), 1)?);
    let title = strip(json_text(&body, "title", "")?);
    let title = if title.is_empty() { "Event".to_string() } else { title };
    let id_of = |k: &str| -> Option<Sql> { Some(opt_int_json(body.get(k)).ok()?.map_or(Sql::Null, Sql::Integer)) };
    let owner = if c.p.is_gm || c.is_assistant() { Sql::Null } else { Sql::Integer(c.p.user_id) };
    let id = orm::insert(
        &c.db,
        "calendar_events",
        &[
            ("world_id", Sql::Integer(wid)),
            ("day", Sql::Integer(day)),
            ("title", s(&title)),
            ("notes", s(json_text(&body, "notes", "")?)),
            ("entity_id", id_of("entity_id")?),
            ("session_id", id_of("session_id")?),
            ("character_id", id_of("character_id")?),
            ("party_id", id_of("party_id")?),
            ("color", s(json_text(&body, "color", "#4488ff")?)),
            ("created_by_user_id", owner),
        ],
    )?;
    json_response(&json!({"id": id, "day": day, "title": title}))
}

fn calendar_event_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let ev = row(&c.db, "SELECT * FROM calendar_events WHERE id = ?1", ids[0])?;
    if ev.get("world_id")?.as_i64() != c.world_id() || !c.can_edit_row("calendar", ev.get("created_by_user_id").and_then(|v| v.as_i64()))? {
        return None;
    }
    c.db.execute("DELETE FROM calendar_events WHERE id = ?1", [ids[0]]).ok()?;
    json_response(&json!({"ok": true}))
}

/// Writes the calendar's config back as `json.dumps(config)` does
fn save_calendar(db: &Connection, wid: i64, config: &Value) -> Option<()> {
    let id: i64 = db.query_row("SELECT id FROM world_calendars WHERE world_id = ?1 ORDER BY id LIMIT 1", [wid], |r| r.get(0)).ok()?;
    orm::update(db, "world_calendars", id, &[("config_json", s(&nd_render::py_dumps(config)))])?;
    Some(())
}

fn calendar_advance(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let wid = c.world_id()?;
    if !can_manage_calendar(c)? {
        return None;
    }
    let (mut config, days, _) = crate::calendar::for_write(&c.db, wid)?;
    let delta = safe_int_json(body.get("days"), 1)?;
    let stored = safe_int_json(config.get("current_day"), 1)?;
    let day = crate::calendar::clamp_day(&days, stored.checked_add(delta)?);
    config.as_object_mut()?.insert("current_day".into(), json!(day));
    save_calendar(&c.db, wid, &config)?;
    let (y, m, d) = crate::calendar::resolve(&days, day);
    json_response(&json!({"current_day": day, "year": y, "month_idx": m, "dom": d}))
}

fn calendar_set_date(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let wid = c.world_id()?;
    if !can_manage_calendar(c)? {
        return None;
    }
    let (mut config, days, _) = crate::calendar::for_write(&c.db, wid)?;
    let year = safe_int_json(body.get("year"), 1)?.clamp(1, crate::calendar::MAX_YEAR_PUB);
    let idx = safe_int_json(body.get("month_idx"), 0)?.clamp(0, days.len() as i64 - 1) as usize;
    let dom = safe_int_json(body.get("dom"), 1)?.min(days[idx]).max(1);
    let day = crate::calendar::start_of(&days, year, idx) + dom - 1;
    config.as_object_mut()?.insert("current_day".into(), json!(day));
    save_calendar(&c.db, wid, &config)?;
    json_response(&json!({"current_day": day, "year": year, "month_idx": idx, "dom": dom}))
}
