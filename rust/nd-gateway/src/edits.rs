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
use crate::form::{content_type, parse_multipart, py_int, Form};
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
        if !plain_numbers(self.body) {
            return None;
        }
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

/// Whether every whole number in a JSON text fits in 64 bits (Python keeps a bigger one exact; here it would turn into a float and be
/// written back differently)
fn plain_numbers(raw: &[u8]) -> bool {
    let (mut in_str, mut esc, mut run, mut frac) = (false, false, 0usize, false);
    for &b in raw {
        if in_str {
            if esc {
                esc = false;
            } else if b == b'\\' {
                esc = true;
            } else if b == b'"' {
                in_str = false;
            }
            continue;
        }
        match b {
            b'"' => in_str = true,
            b'0'..=b'9' => {
                run += 1;
                if run > 18 && !frac {
                    return false;
                }
                continue;
            }
            b'.' | b'e' | b'E' if run > 0 => frac = true,
            b'-' | b'+' => {}
            _ => {
                run = 0;
                frac = false;
            }
        }
        if !b.is_ascii_digit() && !matches!(b, b'.' | b'e' | b'E' | b'-' | b'+') {
            run = 0;
            frac = false;
        }
    }
    true
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
        .body(Body::from(nd_render::py_dumps_compact(v)))
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
    ("/boards/new", board_create),
    ("/sessions/new", session_create),
    ("/sessions/{}/edit", session_edit),
    ("/api/sessions/{}/prep/toggle", prep_toggle),
    ("/api/sessions/{}/prep/add", prep_add),
    ("/api/sessions/{}/prep/{}/delete", prep_delete),
    ("/api/entity/{}/field", entity_field),
    ("/api/entity/{}/rename", entity_rename),
    ("/api/entities/bulk-folder", bulk_folder),
    ("/api/entities/bulk-visibility", bulk_visibility),
    ("/api/entity/{}/alias", entity_alias),
    ("/api/characters/{}/hp-async", pc_hp),
    ("/api/characters/{}/shock", pc_shock),
    ("/api/characters/{}/pp", pc_pp),
    ("/api/characters/{}/mp", pc_mp),
    ("/parties/new", party_new),
    ("/parties/{}/edit", party_edit),
    ("/parties/{}/delete", party_delete),
    ("/api/parties/{}/location", party_location),
    ("/api/parties/{}/loot", party_loot),
    ("/api/parties/{}/members/toggle", party_member_toggle),
    ("/new", entity_new),
    ("/entity/{}/edit", entity_edit),
    ("/entity/{}/duplicate", entity_duplicate),
    ("/entity/{}/delete", entity_delete),
    ("/entity/{}/link/{}", entity_link),
    ("/entity/{}/unlink/{}", entity_unlink),
    ("/entity/{}/notes/new", note_add),
    ("/entity/{}/notes/{}/toggle", note_toggle),
    ("/entity/{}/notes/{}/delete", note_delete),
    ("/entity/{}/notes/{}/edit", note_edit),
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

type SlugHandler = fn(&mut Ctx, &str) -> Option<Response<Body>>;

/// (prefix, suffix, handler) for paths addressed by a slug: `<prefix><slug><suffix>`, the slug plain letters, digits, `-` and `_`
const SLUG_ROUTES: &[(&str, &str, SlugHandler)] = &[("/boards/", "/save", board_save), ("/boards/", "/delete", board_delete), ("/kind/", "/bulk-delete", bulk_delete)];

fn slug_route(path: &str) -> Option<(SlugHandler, String)> {
    for (pre, post, h) in SLUG_ROUTES {
        if let Some(slug) = path.strip_prefix(pre).and_then(|r| r.strip_suffix(post)) {
            if !slug.is_empty() && slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
                return Some((*h, slug.to_string()));
            }
        }
    }
    None
}

pub fn handles(method: &Method, path: &str) -> bool {
    method == Method::POST && (route(path).is_some() || slug_route(path).is_some())
}

fn run(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let by_id = route(uri.path());
    let by_slug = if by_id.is_none() { slug_route(uri.path()) } else { None };
    if by_id.is_none() && by_slug.is_none() {
        return None;
    }
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let mut ctx = Ctx { db, p, uri, headers, body };
    match (by_id, by_slug) {
        (Some((h, ids)), _) => h(&mut ctx, &ids),
        (_, Some((h, slug))) => h(&mut ctx, &slug),
        _ => None,
    }
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

// ── investigation boards (app/main.py) ───────────────────────────────────────

/// A FastAPI `Form(default)` parameter: an empty value counts as missing (None = required and missing: FastAPI's 422)
fn form_param<'a>(f: &'a Form, name: &str, default: Option<&'a str>) -> Option<&'a str> {
    match f.get(name) {
        Some(v) if !v.is_empty() => Some(v),
        _ => default,
    }
}

fn board_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("boards")? {
        return None;
    }
    let name = form_param(&f, "name", None)?.to_string();
    if !name.is_ascii() {
        return None; // the slug lowercases first: a non-ASCII letter may turn ASCII in Python
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
    let base = slug.trim_matches('-').to_string();
    let base = if base.is_empty() { "board".to_string() } else { base };
    let tx = c.db.unchecked_transaction().ok()?;
    let taken = |sl: &str| -> Option<bool> { tx.query_row("SELECT count(*) FROM invest_boards WHERE slug = ?1", [sl], |r| r.get::<_, i64>(0)).ok().map(|n| n > 0) };
    let mut slug = base.clone();
    let mut i = 2;
    while taken(&slug)? {
        slug = format!("{base}-{i}");
        i += 1;
    }
    orm::insert(
        &tx,
        "invest_boards",
        &[
            ("world_id", Sql::Integer(wid)),
            ("name", s(&name)),
            ("slug", s(&slug)),
            ("description", s(form_param(&f, "description", Some(""))?)),
            ("canvas_bg", s(form_param(&f, "canvas_bg", Some("cork"))?)),
        ],
    )?;
    tx.commit().ok()?;
    redirect(&format!("/boards/{slug}"))
}

/// The board by slug, of the viewer's active world, with board edit rights there
fn editable_board(c: &Ctx, slug: &str) -> Option<Map<String, Value>> {
    let b = c.db.query_row("SELECT * FROM invest_boards WHERE slug = ?1 ORDER BY id LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    if b.get("world_id")?.as_i64() != c.world_id() || !c.can_edit_section("boards")? {
        return None;
    }
    Some(b)
}

fn board_save(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let body = c.json()?;
    let b = editable_board(c, slug)?;
    let pick = |k: &str| body.get(k).cloned().unwrap_or(json!([]));
    let mut nodes = Map::new();
    nodes.insert("nodes".into(), pick("nodes"));
    nodes.insert("groups".into(), pick("groups"));
    orm::update(
        &c.db,
        "invest_boards",
        b.get("id")?.as_i64()?,
        &[("nodes_json", s(&nd_render::py_dumps(&Value::Object(nodes)))), ("edges_json", s(&nd_render::py_dumps(&pick("edges"))))],
    )?;
    json_response(&json!({"ok": true}))
}

fn board_delete(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let b = editable_board(c, slug)?;
    c.db.execute("DELETE FROM invest_boards WHERE id = ?1", [b.get("id")?.as_i64()?]).ok()?;
    redirect("/boards")
}

// ── sessions (app/routers/sessions.py) ───────────────────────────────────────

/// `int(form.get(name) or default)`
fn int_or(f: &Form, name: &str, default: i64) -> Option<i64> {
    match f.get(name) {
        None | Some("") => Some(default),
        Some(v) => py_int(v),
    }
}

fn session_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("sessions")? {
        return None;
    }
    let date = strip(f.get("session_date").unwrap_or(""));
    let id = orm::insert(
        &c.db,
        "game_sessions",
        &[
            ("world_id", Sql::Integer(wid)),
            ("title", s(&field_or(&f, "title", "", "Untitled Session"))),
            ("session_num", Sql::Integer(int_or(&f, "session_num", 1)?)),
            ("session_date", if date.is_empty() { Sql::Null } else { s(&date) }),
            ("summary", s(f.get("summary").unwrap_or(""))),
            ("party_id", opt_int_field(&f, "party_id").ok()?.map_or(Sql::Null, Sql::Integer)),
            ("xp_awarded", Sql::Integer(int_or(&f, "xp_awarded", 0)?)),
        ],
    )?;
    redirect(&format!("/sessions/{id}"))
}

/// The session by id, of the viewer's active world, with session edit rights there (`_require_edit_section` by membership)
fn editable_session(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let gs = row(&c.db, "SELECT * FROM game_sessions WHERE id = ?1", id)?;
    if gs.get("world_id")?.as_i64() != c.world_id() || !c.can_edit_section("sessions")? {
        return None;
    }
    Some(gs)
}

fn session_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let f = c.form()?;
    let gs = editable_session(c, id)?;
    let old_title = gs.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let old_num = gs.get("session_num").and_then(|v| v.as_i64())?;
    let date = strip(f.get("session_date").unwrap_or(""));
    // npc_entity_ids: entity ids, and "pc:<id>" for player characters (each list in the order given)
    let (mut ents, mut pcs) = (vec![], vec![]);
    for v in f.0.iter().filter(|(k, _)| k == "npc_entity_ids").map(|(_, v)| v.as_str()) {
        match v.strip_prefix("pc:") {
            Some(p) => pcs.push(py_int(p)?),
            None => ents.push(py_int(v)?),
        }
    }
    let name_of = |table: &str, i: i64| -> Value {
        c.db.query_row(&format!("SELECT name FROM {table} WHERE id = ?1"), [i], |r| r.get::<_, Option<String>>(0)).ok().flatten().map_or(json!(""), |n| json!(n))
    };
    let mut npcs = vec![];
    for i in &ents {
        npcs.push(json!({"entity_id": i, "name": name_of("entities", *i), "kind": "entity"}));
    }
    for i in &pcs {
        npcs.push(json!({"entity_id": i, "name": name_of("player_characters", *i), "kind": "player_character"}));
    }
    orm::update(
        &c.db,
        "game_sessions",
        id,
        &[
            ("title", s(&field_or(&f, "title", &old_title, &old_title))),
            ("session_num", Sql::Integer(int_or(&f, "session_num", old_num)?)),
            ("session_date", if date.is_empty() { Sql::Null } else { s(&date) }),
            ("summary", s(f.get("summary").unwrap_or(""))),
            ("party_id", opt_int_field(&f, "party_id").ok()?.map_or(Sql::Null, Sql::Integer)),
            ("npcs_json", s(&nd_render::py_dumps(&Value::Array(npcs)))),
        ],
    )?;
    redirect(&format!("/sessions/{id}?saved=1"))
}

/// The session's prep list (`json.loads(gs.prep_json or "[]")`) - a list, or Python's call
fn prep_list(gs: &Map<String, Value>) -> Option<Vec<Value>> {
    let raw = gs.get("prep_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    match serde_json::from_str::<Value>(raw).ok()? {
        Value::Array(a) => Some(a),
        _ => None,
    }
}

fn save_prep(c: &Ctx, id: i64, prep: Vec<Value>) -> Option<Response<Body>> {
    let v = Value::Array(prep);
    orm::update(&c.db, "game_sessions", id, &[("prep_json", s(&nd_render::py_dumps(&v)))])?;
    json_response(&json!({"prep": v}))
}

fn prep_toggle(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let gs = editable_session(c, ids[0])?;
    // int(body.get("index", -1)): a missing key is -1; null (int(None)) and other types are Python's
    let idx = match body.get("index") {
        None => -1,
        Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
        Some(_) => return None,
    };
    let mut prep = prep_list(&gs)?;
    if idx >= 0 && (idx as usize) < prep.len() {
        let item = prep[idx as usize].as_object_mut()?;
        let done = match item.get("done") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => false,
            Some(Value::Bool(true)) => true,
            _ => return None, // `not` of another value: Python's truthiness rules
        };
        item.insert("done".into(), json!(!done));
    }
    save_prep(c, ids[0], prep)
}

fn prep_add(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let gs = editable_session(c, ids[0])?;
    let task = strip(json_text(&body, "task", "")?);
    let mut prep = prep_list(&gs)?;
    if !task.is_empty() {
        prep.push(json!({"task": task, "done": false}));
    }
    save_prep(c, ids[0], prep)
}

fn prep_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let gs = editable_session(c, ids[0])?;
    let mut prep = prep_list(&gs)?;
    let idx = ids[1];
    if idx >= 0 && (idx as usize) < prep.len() {
        prep.remove(idx as usize);
    }
    save_prep(c, ids[0], prep)
}

// ── entity links and notes (app/main.py; auth_gate lets only the GM / assistants reach these) ──

/// The entity by id, when it is of the viewer's active world (another world's is Python's)
fn world_entity(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let e = row(&c.db, "SELECT * FROM entities WHERE id = ?1", id)?;
    (e.get("world_id")?.as_i64() == c.world_id()).then_some(e)
}

fn entity_link(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (src, tgt) = (world_entity(c, ids[0])?, world_entity(c, ids[1])?);
    if src.get("world_id") != tgt.get("world_id") {
        return None;
    }
    c.db.execute("INSERT OR IGNORE INTO entity_links (source_id, target_id) VALUES (?1, ?2)", [ids[0], ids[1]]).ok()?;
    redirect(&format!("/entity/{}", ids[0]))
}

fn entity_unlink(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    world_entity(c, ids[0])?;
    world_entity(c, ids[1])?;
    c.db.execute("DELETE FROM entity_links WHERE source_id = ?1 AND target_id = ?2", [ids[0], ids[1]]).ok()?;
    redirect(&format!("/entity/{}", ids[0]))
}

/// A required FastAPI `Form(...)` text: present and non-empty (an empty value is FastAPI's 422: Python's)
fn required(f: &Form, name: &str) -> Option<String> {
    f.get(name).filter(|v| !v.is_empty()).map(str::to_string)
}

/// `bool(visible)` for an optional `Form(None)`: an empty value counts as missing
fn flag_param(f: &Form, name: &str) -> i64 {
    f.get(name).map_or(0, |v| !v.is_empty() as i64)
}

fn note_add(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    world_entity(c, ids[0])?;
    let content = strip(&required(&f, "content")?);
    if !content.is_empty() {
        orm::insert(
            &c.db,
            "entity_notes",
            &[
                ("entity_id", Sql::Integer(ids[0])),
                ("author_id", Sql::Integer(c.p.user_id)),
                ("content", s(&content)),
                ("visible_to_players", Sql::Integer(flag_param(&f, "visible"))),
            ],
        )?;
    }
    redirect(&format!("/entity/{}", ids[0]))
}

/// The note, when it belongs to the entity (of the active world)
fn entity_note(c: &Ctx, entity_id: i64, note_id: i64) -> Option<Map<String, Value>> {
    world_entity(c, entity_id)?;
    let n = row(&c.db, "SELECT * FROM entity_notes WHERE id = ?1", note_id)?;
    (n.get("entity_id")?.as_i64() == Some(entity_id)).then_some(n)
}

fn note_toggle(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let n = entity_note(c, ids[0], ids[1])?;
    let shown = n.get("visible_to_players").and_then(|v| v.as_i64()).unwrap_or(0) != 0;
    orm::update(&c.db, "entity_notes", ids[1], &[("visible_to_players", Sql::Integer(!shown as i64))])?;
    redirect(&format!("/entity/{}", ids[0]))
}

fn note_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    world_entity(c, ids[0])?;
    if let Some(n) = row(&c.db, "SELECT * FROM entity_notes WHERE id = ?1", ids[1]) {
        if n.get("entity_id")?.as_i64() == Some(ids[0]) {
            c.db.execute("DELETE FROM entity_notes WHERE id = ?1", [ids[1]]).ok()?;
        }
    }
    redirect(&format!("/entity/{}", ids[0]))
}

fn note_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    entity_note(c, ids[0], ids[1])?;
    let content = strip(&required(&f, "content")?);
    let mut set: Vec<(&str, Sql)> = vec![];
    if !content.is_empty() {
        set.push(("content", s(&content)));
    }
    set.push(("visible_to_players", Sql::Integer(flag_param(&f, "visible"))));
    orm::update(&c.db, "entity_notes", ids[1], &set)?;
    redirect(&format!("/entity/{}", ids[0]))
}

/// `s[:n]` in code points
fn take_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// A pydantic `str` field: a JSON string (anything else is pydantic's 422: Python's); `default` when absent
fn json_str<'a>(b: &'a Map<String, Value>, name: &str, default: Option<&'a str>) -> Option<&'a str> {
    match b.get(name) {
        None => default,
        Some(Value::String(v)) => Some(v),
        _ => None,
    }
}

/// `deps.can_edit_content`: the GM or a GM-assistant
fn content_editor(c: &Ctx) -> bool {
    c.p.is_gm || c.is_assistant()
}

fn entity_field(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (field, value) = (json_str(&b, "field", None)?, json_str(&b, "value", Some(""))?);
    if !content_editor(c) || field != "subtype" {
        return None;
    }
    let e = world_entity(c, ids[0])?;
    let value = take_chars(&nd_render::text::py_split_join(value), 64);
    let old = e.get("subtype").and_then(|v| v.as_str()).unwrap_or("").to_string();
    if !value.is_empty() || e.get("subtype").is_some_and(|v| !v.is_null()) {
        let new = if value.is_empty() { Sql::Null } else { s(&value) };
        orm::update(&c.db, "entities", ids[0], &[("subtype", new)])?;
    }
    json_response(&json!({"ok": true, "id": ids[0], "field": "subtype", "value": value, "old": old}))
}

fn entity_rename(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let name = json_str(&b, "name", None)?;
    if !content_editor(c) {
        return None;
    }
    let e = world_entity(c, ids[0])?;
    let name = take_chars(&nd_render::text::py_split_join(name), 256);
    let old = e.get("name").and_then(|v| v.as_str())?.to_string();
    if name.is_empty() || name == old {
        return None; // the 422s: Python's
    }
    orm::update(&c.db, "entities", ids[0], &[("name", s(&name))])?;
    json_response(&json!({"ok": true, "id": ids[0], "name": name, "old": old}))
}

fn bulk_folder(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let world = c.world_id()?;
    // only plain integer ids (Python's int() also takes numeric strings, floats and bools: those requests are Python's)
    let ids: Vec<i64> = match b.get("entity_ids") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(a)) => a.iter().map(|v| v.as_i64()).collect::<Option<_>>()?,
        _ => return None,
    };
    let folder = strip(json_str(&b, "folder", Some(""))?);
    let folder = if folder.is_empty() { Sql::Null } else { s(&folder) };
    let mut hit: Vec<i64> = vec![];
    for id in ids {
        let w: Option<i64> = c.db.query_row("SELECT world_id FROM entities WHERE id = ?1", [id], |r| r.get(0)).ok().flatten();
        if w == Some(world) && !hit.contains(&id) {
            hit.push(id);
        }
    }
    for id in &hit {
        orm::update(&c.db, "entities", *id, &[("folder", folder.clone())])?;
    }
    json_response(&json!({"updated": hit.len()}))
}

/// `urllib.parse.quote(s)` (safe="/")
const QUOTE: &percent_encoding::AsciiSet =
    &percent_encoding::NON_ALPHANUMERIC.remove(b'/').remove(b'_').remove(b'.').remove(b'-').remove(b'~');

/// The entity form (multipart from the browser, or urlencoded): its fields, when the only file part is an empty `image_file` (no file
/// chosen) - an upload is Python's
fn entity_form(c: &Ctx) -> Option<Form> {
    if content_type(c.headers) == "multipart/form-data" {
        let (f, files) = parse_multipart(c.headers, c.body)?;
        if files.iter().any(|p| p.name != "image_file" || !p.filename.is_empty() || p.len != 0) || f.has("image_file") {
            return None;
        }
        return Some(f);
    }
    c.form()
}

/// A FastAPI `Form(default)` text: an empty value counts as missing
fn opt<'a>(f: &'a Form, name: &str, default: &'a str) -> &'a str {
    f.get(name).filter(|v| !v.is_empty()).unwrap_or(default)
}

/// `_normalize_aliases`; None when an alias is not ASCII (its casefold() is Python's)
fn normalize_aliases(raw: &str) -> Option<Option<String>> {
    let (mut seen, mut out): (Vec<String>, Vec<String>) = (vec![], vec![]);
    for part in raw.split(',') {
        let alias = strip(part);
        if alias.is_empty() {
            continue;
        }
        if !alias.is_ascii() {
            return None;
        }
        let alias = take_chars(&alias, 64);
        let key = alias.to_ascii_lowercase();
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        out.push(alias);
        if out.len() >= 20 {
            break;
        }
    }
    Some((!out.is_empty()).then(|| out.join(", ")))
}

/// The columns the create / edit forms set, as Python sets them, and the players for `_sync_entity_access`
struct EntityFields {
    cols: Vec<(&'static str, Sql)>,
    players: Vec<i64>,
    image_url: String,
}

fn entity_fields(f: &Form) -> Option<EntityFields> {
    let (kind, name) = (required(f, "kind")?, required(f, "name")?);
    // List[int] = Form([]): every value must be an int to pydantic (only plain digits here)
    let ids: Vec<i64> = f.all("allowed_player_ids").iter().map(|v| py_int(v)).collect::<Option<_>>()?;
    let template = match opt(f, "template_id", "") {
        "" => Sql::Null,
        v if v.bytes().all(|b| b.is_ascii_digit()) && v.len() <= 15 => Sql::Integer(v.parse().ok()?),
        v if v.is_ascii() => Sql::Null,
        _ => return None, // str.isdigit() knows other scripts' digits
    };
    let custom = opt(f, "custom_fields_json", "{}");
    if serde_json::from_str::<Value>(custom).is_err() {
        return None; // json.loads accepts more (NaN, ...) than serde: Python decides
    }
    let visibility = opt(f, "visibility_mode", "everyone");
    let or_null = |v: &str| if v.is_empty() { Sql::Null } else { s(v) };
    let folder = strip(opt(f, "folder", ""));
    let cols = vec![
        ("kind", s(&kind)),
        ("subtype", or_null(opt(f, "subtype", ""))),
        ("folder", or_null(&folder)),
        ("name", s(&name)),
        ("tags", or_null(opt(f, "tags", ""))),
        ("aliases", normalize_aliases(opt(f, "aliases", ""))?.map_or(Sql::Null, |a| s(&a))),
        ("roleplay_personality", s(&take_chars(&strip(opt(f, "roleplay_personality", "")), 4000))),
        ("summary", or_null(opt(f, "summary", ""))),
        ("body", or_null(opt(f, "body", ""))),
        ("visible_to_players", Sql::Integer((visibility == "everyone") as i64)),
        ("rag_priority", Sql::Integer(flag_param(f, "rag_priority"))),
        ("template_id", template),
        ("custom_fields_json", s(custom)),
    ];
    let mut players = vec![];
    if visibility == "players" {
        for id in ids {
            if !players.contains(&id) {
                players.push(id);
            }
        }
    }
    Some(EntityFields { cols, players, image_url: strip(opt(f, "image_url", "")) })
}

/// `_sync_entity_access`
fn sync_access(db: &Connection, entity_id: i64, players: &[i64]) -> Option<()> {
    db.execute("DELETE FROM entity_player_access WHERE entity_id = ?1", [entity_id]).ok()?;
    for uid in players {
        db.execute("INSERT INTO entity_player_access (entity_id, user_id) VALUES (?1, ?2)", [entity_id, *uid]).ok()?;
    }
    Some(())
}

fn entity_new(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let f = entity_form(c)?;
    let world = c.world_id()?;
    let slug = c.p.world.as_ref()?.get("slug")?.as_str()?.to_string();
    let e = entity_fields(&f)?;
    let mut cols = e.cols;
    cols.push(("world_id", Sql::Integer(world)));
    cols.push(("image_url", if e.image_url.is_empty() { Sql::Null } else { s(&e.image_url) }));
    let id = orm::insert(&c.db, "entities", &cols)?;
    sync_access(&c.db, id, &e.players)?;
    if !opt(&f, "save_and_new", "").is_empty() {
        let q = |v: &str| utf8_percent_encode(v, QUOTE).to_string();
        let kind = required(&f, "kind")?;
        return redirect(&format!("/new?kind={}&folder={}&w={slug}", q(&kind), q(&strip(opt(&f, "folder", "")))));
    }
    redirect(&format!("/entity/{id}"))
}

fn entity_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = entity_form(c)?;
    world_entity(c, ids[0])?;
    let e = entity_fields(&f)?;
    let mut cols = e.cols;
    if !e.image_url.is_empty() {
        cols.push(("image_url", s(&e.image_url)));
    } else if !opt(&f, "remove_image", "").is_empty() {
        cols.push(("image_url", Sql::Null));
    }
    orm::update(&c.db, "entities", ids[0], &cols)?;
    sync_access(&c.db, ids[0], &e.players)?;
    redirect(&format!("/entity/{}", ids[0]))
}

fn entity_duplicate(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let src = world_entity(c, ids[0])?;
    let v = |k: &str| -> Option<Sql> {
        Some(match src.get(k)? {
            Value::Null => Sql::Null,
            Value::String(t) => s(t),
            Value::Number(n) if n.is_i64() => Sql::Integer(n.as_i64()?),
            Value::Bool(b) => Sql::Integer(*b as i64),
            _ => return None,
        })
    };
    let name = format!("{} (copy)", src.get("name")?.as_str()?);
    let mut cols: Vec<(&str, Sql)> = vec![("name", s(&name))];
    for k in ["world_id", "kind", "subtype", "folder", "tags", "aliases", "image_url", "summary", "body", "visible_to_players", "template_id",
              "custom_fields_json"] {
        cols.push((k, v(k)?));
    }
    let id = orm::insert(&c.db, "entities", &cols)?;
    redirect(&format!("/entity/{id}"))
}

fn entity_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    world_entity(c, ids[0])?;
    let id = ids[0];
    for sql in [
        "DELETE FROM entity_links WHERE source_id = ?1 OR target_id = ?1",
        "DELETE FROM entity_player_access WHERE entity_id = ?1",
        "DELETE FROM entity_notes WHERE entity_id = ?1",
        "DELETE FROM entity_voice_hints WHERE entity_id = ?1",
        "DELETE FROM entity_relations WHERE source_id = ?1 OR target_id = ?1",
        "DELETE FROM entities WHERE id = ?1",
    ] {
        c.db.execute(sql, [id]).ok()?;
    }
    redirect("/")
}

/// The ids of a JSON list as plain integers (None when one is anything else: Python's int() reads more, so that request is Python's)
fn json_ids(v: Option<&Value>) -> Option<Vec<i64>> {
    match v {
        None | Some(Value::Null) => Some(vec![]),
        Some(Value::Array(a)) => a.iter().map(|v| v.as_i64()).collect(),
        Some(Value::Bool(false)) => Some(vec![]),
        _ => None,
    }
}

/// The ids among `ids` that are entities of `world`, each once (the `Entity.id.in_(ids)` query), in id order
fn world_entity_ids(db: &Connection, world: i64, ids: &[i64], kind: Option<&str>) -> Vec<i64> {
    let mut hit: Vec<i64> = vec![];
    for id in ids {
        let row: Option<(i64, String)> = db.query_row("SELECT world_id, kind FROM entities WHERE id = ?1", [id], |r| Ok((r.get(0)?, r.get(1)?))).ok();
        if let Some((w, k)) = row {
            if w == world && kind.map_or(true, |kd| kd == k) && !hit.contains(id) {
                hit.push(*id);
            }
        }
    }
    hit.sort();
    hit
}

fn bulk_visibility(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let world = c.world_id()?;
    let ids = json_ids(b.get("entity_ids"))?;
    let mode = strip(json_str(&b, "visibility_mode", Some(""))?);
    if !matches!(mode.as_str(), "everyone" | "gm" | "players") {
        return None; // the 400: Python's
    }
    let players = if mode == "players" { json_ids(b.get("allowed_player_ids"))? } else { vec![] };
    let mut uniq: Vec<i64> = vec![];
    for p in players {
        if !uniq.contains(&p) {
            uniq.push(p);
        }
    }
    let hit = world_entity_ids(&c.db, world, &ids, None);
    for id in &hit {
        orm::update(&c.db, "entities", *id, &[("visible_to_players", Sql::Integer((mode == "everyone") as i64))])?;
    }
    for id in &hit {
        sync_access(&c.db, *id, &uniq)?;
    }
    json_response(&json!({"updated": hit.len()}))
}

fn entity_alias(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let alias = json_str(&b, "alias", None)?;
    if !c.p.is_gm {
        return None;
    }
    let e = world_entity(c, ids[0])?;
    let alias = take_chars(&nd_render::text::py_split_join(&alias.replace(',', " ")), 64);
    let name = e.get("name").and_then(|v| v.as_str()).unwrap_or("");
    if alias.chars().count() < 2 || !alias.is_ascii() || !name.is_ascii() || alias.eq_ignore_ascii_case(name) {
        return None; // the 422s, and casefold() beyond ASCII: Python's
    }
    let existing: Vec<String> = e.get("aliases").and_then(|v| v.as_str()).unwrap_or("").split(',').map(strip).filter(|a| !a.is_empty()).collect();
    if existing.iter().any(|a| !a.is_ascii() || a.eq_ignore_ascii_case(&alias)) {
        return None;
    }
    let joined = existing.into_iter().chain([alias]).collect::<Vec<_>>().join(", ");
    let aliases = normalize_aliases(&joined)?;
    let v = aliases.as_ref().map_or(Sql::Null, |a| s(a));
    orm::update(&c.db, "entities", ids[0], &[("aliases", v)])?;
    json_response(&json!({"ok": true, "aliases": aliases}))
}

fn bulk_delete(c: &mut Ctx, kind: &str) -> Option<Response<Body>> {
    let f = c.form()?;
    let world = c.world_id()?;
    let slug = c.p.world.as_ref()?.get("slug")?.as_str()?.to_string();
    let mut ids = vec![];
    for v in f.all("entity_ids") {
        match py_int(v) {
            Some(n) => ids.push(n),
            None if v.is_empty() => {}             // int("") fails: skipped
            None => return None,                    // " 5", "+5", "1_0"...: Python's
        }
    }
    let hit = world_entity_ids(&c.db, world, &ids, Some(kind));
    for id in &hit {
        // as Python's bulk delete: links, access, notes, relations, the entity (voice hints stay, unlike the single delete)
        for sql in [
            "DELETE FROM entity_links WHERE source_id = ?1 OR target_id = ?1",
            "DELETE FROM entity_player_access WHERE entity_id = ?1",
            "DELETE FROM entity_notes WHERE entity_id = ?1",
            "DELETE FROM entity_relations WHERE source_id = ?1 OR target_id = ?1",
            "DELETE FROM entities WHERE id = ?1",
        ] {
            c.db.execute(sql, [id]).ok()?;
        }
    }
    let q = |v: &str| utf8_percent_encode(v, QUOTE).to_string();
    let mut params = vec![];
    if let Some(folder) = f.get("folder") {
        params.push(format!("folder={}", q(folder)));
    }
    let query = f.get("q").unwrap_or("");
    if !query.is_empty() {
        params.push(format!("q={}", q(query)));
    }
    let mut url = format!("/kind/{kind}");
    if !params.is_empty() {
        url += &format!("?{}", params.join("&"));
    }
    url += if url.contains('?') { "&" } else { "?" };
    redirect(&format!("{url}w={slug}"))
}

// ── parties (app/routers/parties.py) ─────────────────────────────────────────

/// `_can_manage_parties`: the GM or an assistant, with parties:edit
fn can_manage_parties(c: &Ctx) -> Option<bool> {
    Some(c.can_edit_section("parties")? && content_editor(c))
}

/// A stored JSON list of plain integers (None for anything else: Python's `in` / `==` would compare it its own way)
fn int_list(v: Option<&Value>) -> Option<Vec<i64>> {
    let raw = v.and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    match serde_json::from_str::<Value>(raw).ok()? {
        Value::Array(a) => a.iter().map(|x| x.as_i64()).collect(),
        _ => None,
    }
}

fn ints_json(ids: &[i64]) -> String {
    nd_render::py_dumps(&json!(ids))
}

/// `_party_for_write` + `_party_edit_level`: the party of the active world and the viewer's level ("full" / "member" / "none")
fn party_for_write(c: &Ctx, id: i64) -> Option<(Map<String, Value>, &'static str)> {
    let p = row(&c.db, "SELECT * FROM parties WHERE id = ?1", id)?;
    if p.get("world_id")?.as_i64() != c.world_id() {
        return None; // a GM's write in another world, or a 404: Python's
    }
    if can_manage_parties(c)? {
        return Some((p, "full"));
    }
    if !c.can_edit_section("parties")? {
        return Some((p, "none"));
    }
    let pcs = int_list(p.get("member_pc_ids_json"))?;
    let mut mine = false;
    for pc in pcs {
        let owner: Option<i64> = c.db.query_row("SELECT owner_user_id FROM player_characters WHERE id = ?1", [pc], |r| r.get(0)).ok().flatten();
        mine |= owner == Some(c.p.user_id);
    }
    Some((p, if mine { "member" } else { "none" }))
}

fn party_new(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let world = c.world_id()?;
    if !can_manage_parties(c)? {
        return None;
    }
    let name = strip(opt(&f, "name", "New Party"));
    let name = if name.is_empty() { "New Party".to_string() } else { name };
    let id = orm::insert(&c.db, "parties", &[("world_id", Sql::Integer(world)), ("name", s(&name))])?;
    orm::touch(&c.db, world)?;
    redirect(&format!("/parties/{id}"))
}

fn party_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let (p, level) = party_for_write(c, ids[0])?;
    let world = p.get("world_id")?.as_i64()?;
    let mut cols: Vec<(&str, Sql)> = vec![("notes", s(&strip(f.get("notes").unwrap_or("")))), ("goals", s(&strip(f.get("goals").unwrap_or(""))))];
    match level {
        "none" => return None,
        "full" => {
            if !c.p.is_gm {
                return None; // an assistant's view of hidden companions (filter_visible_entities): Python's
            }
            let old = p.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let name = strip(f.get("name").unwrap_or(&old));
            cols.push(("name", s(if name.is_empty() { &old } else { &name })));
            let pcs: Vec<i64> = f.all("member_pc_ids").iter().map(|v| py_int(v)).collect::<Option<_>>()?;
            let ents: Vec<i64> = f.all("member_entity_ids").iter().map(|v| py_int(v)).collect::<Option<_>>()?;
            let in_world = |table: &str, id: i64| -> bool {
                c.db.query_row(&format!("SELECT world_id FROM {table} WHERE id = ?1"), [id], |r| r.get::<_, Option<i64>>(0)).ok().flatten() == Some(world)
            };
            let pcs: Vec<i64> = pcs.into_iter().filter(|i| in_world("player_characters", *i)).collect();
            let ents: Vec<i64> = ents.into_iter().filter(|i| in_world("entities", *i)).collect();
            cols.push(("member_pc_ids_json", s(&ints_json(&pcs))));
            cols.push(("member_entity_ids_json", s(&ints_json(&ents))));
        }
        _ => {}
    }
    orm::update(&c.db, "parties", ids[0], &cols)?;
    orm::touch(&c.db, world)?;
    redirect(&format!("/parties/{}", ids[0]))
}

fn party_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (p, level) = party_for_write(c, ids[0])?;
    if level != "full" {
        return None;
    }
    orm::bulk_update(&c.db, "quests", &[("assigned_party_id", Sql::Null)], "assigned_party_id = ?", &[Sql::Integer(ids[0])])?;
    c.db.execute("DELETE FROM parties WHERE id = ?1", [ids[0]]).ok()?;
    orm::touch(&c.db, p.get("world_id")?.as_i64()?)?;
    redirect("/parties")
}

fn party_location(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (p, level) = party_for_write(c, ids[0])?;
    if level != "full" {
        return None;
    }
    let kind = b.get("kind").and_then(|v| v.as_str());
    let loc = match kind {
        Some(k @ ("map" | "schematic")) => {
            let slug = strip(json_str(&b, "slug", Some(""))?);
            if slug.is_empty() {
                return None;
            }
            // float(x) of a JSON number (strings, bools and the rest: Python's)
            let num = |k: &str| -> Option<f64> { b.get(k).filter(|v| v.is_number())?.as_f64() };
            let (a, bk) = if k == "map" { ("lat", "lng") } else { ("x", "y") };
            let mut m = Map::new();
            m.insert("kind".into(), json!(k));
            m.insert("slug".into(), json!(slug));
            m.insert(a.into(), Value::from(num(a)?));
            m.insert(bk.into(), Value::from(num(bk)?));
            Value::Object(m)
        }
        _ if b.get("kind").map_or(true, |v| v.is_string() || v.is_null()) => json!({}),
        _ => return None,
    };
    if !loc.as_object()?.values().all(|v| !v.as_f64().is_some_and(|f| !f.is_finite())) {
        return None;
    }
    orm::update(&c.db, "parties", ids[0], &[("location_json", s(&nd_render::py_dumps(&loc)))])?;
    orm::touch(&c.db, p.get("world_id")?.as_i64()?)?;
    json_response(&json!({"location": loc}))
}

fn party_member_toggle(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (p, level) = party_for_write(c, ids[0])?;
    if level != "full" {
        return None;
    }
    let text = |k: &str| -> Option<String> {
        match b.get(k) {
            None | Some(Value::Null) | Some(Value::Bool(false)) => Some(String::new()),
            Some(Value::String(v)) => Some(v.clone()),
            _ => None,
        }
    };
    let kind = text("kind")?;
    if kind != "pc" && (kind != "entity" || !c.p.is_gm) {
        return None; // a bad kind's 400, and a non-GM's view of hidden entities: Python's
    }
    let member_id = match b.get("id") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => 0,
        Some(Value::String(v)) if v.is_empty() => 0,
        Some(Value::Number(n)) => n.as_i64()?,
        _ => return None,
    };
    let name = strip(&text("name")?);
    let world = p.get("world_id")?.as_i64()?;
    let table = if kind == "pc" { "player_characters" } else { "entities" };
    let found: Option<(i64, Option<i64>)> = if member_id != 0 {
        c.db.query_row(&format!("SELECT id, world_id FROM {table} WHERE id = ?1"), [member_id], |r| Ok((r.get(0)?, r.get(1)?))).ok()
    } else if !name.is_empty() {
        c.db.query_row(&format!("SELECT id, world_id FROM {table} WHERE world_id = ?1 AND name = ?2 LIMIT 1"), rusqlite::params![world, name],
                       |r| Ok((r.get(0)?, r.get(1)?))).ok()
    } else {
        None
    };
    let (mid, mw) = found?;
    if mw != Some(world) {
        return None;
    }
    let field = if kind == "pc" { "member_pc_ids_json" } else { "member_entity_ids_json" };
    let mut list = int_list(p.get(field))?;
    let added = match list.iter().position(|x| *x == mid) {
        Some(i) => {
            list.remove(i);
            false
        }
        None => {
            list.push(mid);
            true
        }
    };
    orm::update(&c.db, "parties", ids[0], &[(field, s(&ints_json(&list)))])?;
    orm::touch(&c.db, world)?;
    json_response(&json!({"ok": true, "added": added}))
}

const LOOT_NAME_MAX: usize = 120;
const LOOT_NOTES_MAX: usize = 500;
const LOOT_QTY_MAX: i64 = 9999;

/// `load_loot` for a stash that is already clean (every item an object with its own string `lid`, text name / notes, an int qty and int
/// claims); None for anything `load_loot` would repair (derived lids, str() of other values): Python's
fn load_loot(p: &Map<String, Value>) -> Option<Vec<Map<String, Value>>> {
    let raw = p.get("loot_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let Value::Array(items) = serde_json::from_str::<Value>(raw).ok()? else { return None };
    let mut seen: Vec<String> = vec![];
    let mut out = vec![];
    for it in items {
        let Value::Object(mut m) = it else { return None };
        let lid = m.get("lid")?.as_str().filter(|l| !l.is_empty() && !seen.iter().any(|x| x == l))?.to_string();
        seen.push(lid.clone());
        let name = match m.get("name") {
            None | Some(Value::Null) => "Item".to_string(),
            Some(Value::String(n)) if n.is_empty() => "Item".to_string(),
            Some(Value::String(n)) => take_chars(n, LOOT_NAME_MAX),
            _ => return None,
        };
        let qty = match m.get("qty") {
            None | Some(Value::Null) => 1,
            Some(Value::Number(n)) => n.as_i64()?.max(1),
            _ => return None,
        };
        let notes = match m.get("notes") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(n)) => take_chars(n, LOOT_NOTES_MAX),
            _ => return None,
        };
        let claimed: Vec<i64> = match m.get("claimed_by") {
            None | Some(Value::Null) => vec![],
            Some(Value::Array(a)) => a.iter().map(|x| x.as_i64()).collect::<Option<_>>()?,
            _ => return None,
        };
        m.insert("lid".into(), json!(lid));
        m.insert("name".into(), json!(name));
        m.insert("qty".into(), json!(qty));
        m.insert("notes".into(), json!(notes));
        m.insert("claimed_by".into(), json!(claimed));
        out.push(m);
    }
    Some(out)
}

/// `_loot_int` for a JSON int (strings, floats and bools are int()'s: Python's; out of range is the 400: Python's)
fn loot_int(b: &Map<String, Value>, key: &str, default: Option<i64>, lo: Option<i64>, hi: Option<i64>) -> Option<i64> {
    let v = match b.get(key) {
        None => default?,
        Some(Value::Number(n)) => n.as_i64()?,
        _ => return None,
    };
    (lo.map_or(true, |l| v >= l) && hi.map_or(true, |h| v <= h)).then_some(v)
}

fn party_loot(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (p, level) = party_for_write(c, ids[0])?;
    if level == "none" {
        return None;
    }
    let mut loot = load_loot(&p)?;
    let members = int_list(p.get("member_pc_ids_json"))?;
    let index = |loot: &[Map<String, Value>]| -> Option<usize> {
        match b.get("lid") {
            Some(Value::Null) | None => {
                let i = loot_int(&b, "index", Some(-1), None, None)?;
                (0..loot.len() as i64).contains(&i).then_some(i as usize)
            }
            Some(lid) => loot.iter().position(|it| it.get("lid") == Some(lid)), // gone: the 409, Python's
        }
    };
    match b.get("action").and_then(|v| v.as_str()) {
        Some("add") => {
            let name = strip(b.get("name")?.as_str()?);
            if name.is_empty() {
                return None;
            }
            let notes = json_str(&b, "notes", Some(""))?;
            let qty = loot_int(&b, "qty", Some(1), Some(1), Some(LOOT_QTY_MAX))?;
            let lid = format!("{:08x}", rand::random::<u32>());
            let mut m = Map::new();
            m.insert("lid".into(), json!(lid));
            m.insert("name".into(), json!(take_chars(&name, LOOT_NAME_MAX)));
            m.insert("qty".into(), json!(qty));
            m.insert("notes".into(), json!(take_chars(&strip(notes), LOOT_NOTES_MAX)));
            m.insert("claimed_by".into(), json!([]));
            loot.push(m);
        }
        Some("remove") => {
            let i = index(&loot)?;
            loot.remove(i);
        }
        Some(a @ ("claim" | "unclaim")) => {
            let i = index(&loot)?;
            let pc = loot_int(&b, "pc_id", None, None, None)?;
            if !members.contains(&pc) {
                return None;
            }
            if level != "full" {
                let owner: Option<i64> = c.db.query_row("SELECT owner_user_id FROM player_characters WHERE id = ?1", [pc], |r| r.get(0)).ok().flatten();
                if owner != Some(c.p.user_id) {
                    return None;
                }
            }
            let claimed = loot[i].get_mut("claimed_by")?.as_array_mut()?;
            let at = claimed.iter().position(|x| x.as_i64() == Some(pc));
            match (a, at) {
                ("claim", None) => claimed.push(json!(pc)),
                ("unclaim", Some(k)) => {
                    claimed.remove(k);
                }
                _ => {}
            }
        }
        _ => return None, // give (the character's equipment, by its system) and bad actions: Python's
    }
    let list = Value::Array(loot.into_iter().map(Value::Object).collect());
    orm::update(&c.db, "parties", ids[0], &[("loot_json", s(&nd_render::py_dumps(&list)))])?;
    orm::touch(&c.db, p.get("world_id")?.as_i64()?)?;
    json_response(&json!({"loot": list}))
}

// ── characters (app/routers/characters.py, app/pc_stats.py, app/char_extras.py) ──

/// The character by id, when it is of the active world and the viewer may manage it (`_can_manage_character`: the GM or its owner)
fn managed_pc(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let pc = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", id)?;
    if pc.get("world_id")?.as_i64() != c.world_id() {
        return None;
    }
    (c.p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(c.p.user_id)).then_some(pc)
}

fn col_int(m: &Map<String, Value>, k: &str) -> i64 {
    m.get(k).and_then(|v| v.as_i64()).unwrap_or(0)
}

/// `stat_values(pc)`; None for stats Python reads its own way (text values, unhashable ids, JSON serde does not take)
fn stat_values(pc: &Map<String, Value>) -> Option<Vec<(String, i64)>> {
    let raw = pc.get("stats_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let stats = match serde_json::from_str::<Value>(raw) {
        Ok(v) => v,
        Err(_) => return None,
    };
    let mut out: Vec<(String, i64)> = vec![];
    for s in stats.as_array().map(|a| a.as_slice()).unwrap_or(&[]) {
        let Some(o) = s.as_object() else { continue };
        let id = match o.get("id") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => continue,
            Some(Value::String(x)) if x.is_empty() => continue,
            Some(Value::String(x)) => x.clone(),
            Some(Value::Number(n)) if n.as_f64() == Some(0.0) => continue,
            Some(Value::Number(n)) => format!("\u{0}#{n}"), // a number id: never a stat name
            _ => return None,
        };
        let v = match o.get("value") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => 0,
            Some(Value::Number(n)) => n.as_f64()?.trunc() as i64,
            Some(Value::String(x)) if x.is_empty() => 0,
            _ => return None,
        };
        out.retain(|(k, _)| *k != id);
        out.push((id, v));
    }
    Some(out)
}

/// `pc_maxima(pc)`: (hp, shock, pp, mp, native)
fn pc_maxima(db: &Connection, pc: &Map<String, Value>) -> Option<(i64, i64, i64, i64, bool)> {
    let stats = stat_values(pc)?;
    let tpl = pc.get("sheet_template_id").and_then(|v| v.as_i64()).filter(|t| *t != 0);
    let mut native = !stats.is_empty() || tpl.is_none();
    if native {
        if let Some(t) = tpl {
            let mode: Option<String> = db.query_row("SELECT sheet_mode FROM sheet_templates WHERE id = ?1", [t], |r| r.get(0)).ok().flatten();
            native = mode.as_deref() != Some("custom");
        }
    }
    let get = |k: &str| stats.iter().find(|(id, _)| id == k).map_or(0, |(_, v)| *v);
    let (phys, ment) = if native {
        (get("str") + get("dex") + get("bod") + get("per"), get("wil") + get("int") + get("cha") + get("itu"))
    } else {
        (0, 0)
    };
    let (hp, shock) = (col_int(pc, "max_hp"), col_int(pc, "shock_max"));
    Some((if hp > 0 { hp } else if native { phys + 10 } else { 0 }, if shock > 0 { shock } else { ment }, phys, ment, native))
}

/// `int_field(body, key)` for a JSON int (floats, numeric text and bools are int()'s: Python's)
fn body_int(b: &Map<String, Value>, key: &str) -> Option<i64> {
    match b.get(key) {
        None => Some(0),
        Some(Value::Number(n)) => n.as_i64().filter(|v| v.abs() < 1 << 53),
        _ => None,
    }
}

/// `char_extras.log` (with `actor_name` of the viewer), keeping the newest 200 per character
fn char_log(c: &Ctx, pc: &Map<String, Value>, kind: &str, text: &str, undo: &Value) -> Option<()> {
    let (name, email): (Option<String>, Option<String>) =
        c.db.query_row("SELECT display_name, email FROM users WHERE id = ?1", [c.p.user_id], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
    let actor = [name, email].into_iter().flatten().find(|v| !v.is_empty()).unwrap_or_else(|| "Someone".into());
    let pc_id = pc.get("id")?.as_i64()?;
    orm::insert(
        &c.db,
        "character_log",
        &[
            ("world_id", Sql::Integer(pc.get("world_id")?.as_i64()?)),
            ("character_id", Sql::Integer(pc_id)),
            ("actor", s(&take_chars(&actor, 120))),
            ("kind", s(&take_chars(kind, 24))),
            ("text", s(&take_chars(text, 300))),
            ("undo_json", s(&nd_render::py_dumps(undo))),
        ],
    )?;
    c.db.execute(
        "DELETE FROM character_log WHERE id IN (SELECT id FROM character_log WHERE character_id = ?1 ORDER BY id DESC LIMIT -1 OFFSET 200)",
        [pc_id],
    )
    .ok()?;
    Some(())
}

/// `_log_num`: a numeric change, with its undo
fn log_num(c: &Ctx, pc: &Map<String, Value>, op: &str, label: &str, before: i64, after: i64) -> Option<()> {
    if before != after {
        char_log(c, pc, op, &format!("{label} {before} \u{2192} {after}"), &json!({"op": op, "value": before}))?;
    }
    Some(())
}

fn pc_hp(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let action = b.get("action").cloned().unwrap_or(json!("set"));
    let val = body_int(&b, "value")?;
    let before = col_int(&pc, "current_hp");
    let (max_hp, ..) = pc_maxima(&c.db, &pc)?;
    let temp = col_int(&pc, "temp_hp");
    let mut set: Vec<(&str, Sql)> = vec![];
    let mut after = pc.clone();
    let cur = match action.as_str() {
        Some("delta") => (before + val).min(max_hp + temp).max(0),
        Some("temp") => {
            set.push(("temp_hp", Sql::Integer(val.max(0))));
            after.insert("temp_hp".into(), json!(val.max(0)));
            before
        }
        Some("max") => {
            set.push(("max_hp", Sql::Integer(val.max(0))));
            after.insert("max_hp".into(), json!(val.max(0)));
            let (new_max, ..) = pc_maxima(&c.db, &after)?;
            if new_max != 0 { before.min(new_max) } else { before }
        }
        _ => val.min(max_hp + temp).max(0),
    };
    if action.as_str() != Some("temp") {
        set.push(("current_hp", Sql::Integer(cur)));
        after.insert("current_hp".into(), json!(cur));
    }
    orm::update(&c.db, "player_characters", ids[0], &set)?;
    log_num(c, &pc, "hp", "HP", before, cur)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    let (max_after, ..) = pc_maxima(&c.db, &after)?;
    let field = |k: &str| after.get(k).cloned().unwrap_or(Value::Null);
    let or0 = |k: &str| if after.get(k).is_none_or(|v| v.is_null()) { json!(0) } else { field(k) };
    json_response(&json!({
        "current_hp": field("current_hp"),
        "max_hp": max_after,
        "temp_hp": or0("temp_hp"),
        "death_success": or0("death_saves_success"),
        "death_failure": or0("death_saves_failure"),
        "secondary_current": field("secondary_resource_current"),
    }))
}

/// The Shock / PP / MP steppers: (column, log op, label, which maximum, whether "rest" restores half)
fn pc_pool(c: &mut Ctx, id: i64, col: &str, op: &str, label: &str, which: usize, rest: bool, max_key: &str) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, id)?;
    let action = b.get("action").cloned().unwrap_or(json!("set"));
    let val = body_int(&b, "value")?;
    let before = col_int(&pc, col);
    let m = pc_maxima(&c.db, &pc)?;
    let max = [m.0, m.1, m.2, m.3][which];
    let cur = match action.as_str() {
        Some("delta") => (before + val).min(max).max(0),
        Some("set") => val.min(max).max(0),
        Some("rest") if rest => (before + max.div_euclid(2)).min(max),
        _ => before,
    };
    orm::update(&c.db, "player_characters", id, &[(col, Sql::Integer(cur))])?;
    log_num(c, &pc, op, label, before, cur)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    let mut out = Map::new();
    out.insert(col.into(), json!(cur));
    out.insert(max_key.into(), json!(max));
    json_response(&Value::Object(out))
}

fn pc_shock(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    pc_pool(c, ids[0], "shock_current", "shock", "Shock", 1, false, "shock_max")
}

fn pc_pp(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    pc_pool(c, ids[0], "pp_current", "pp", "PP", 2, true, "pp_max")
}

fn pc_mp(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    pc_pool(c, ids[0], "mp_current", "mp", "MP", 3, true, "mp_max")
}
