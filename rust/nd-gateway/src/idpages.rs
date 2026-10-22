//! Pages addressed by a row id in the path (`/tables/<id>/edit`, `/handout/<id>`, ...), each the port of one Python handler. Same rule as
//! every gateway page: `None` = not sure, the request goes to Python unchanged (a missing row, a refusal, anything unusual).

use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{HeaderMap, Response, Uri},
};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::layout::PageCtx;
use crate::pages::{render_with, Pages};

type Handler = fn(&Pages, &Connection, PageCtx, i64, &HeaderMap) -> Option<Response<Body>>;

/// (path before the id, path after the id, handler)
const ROUTES: &[(&str, &str, Handler)] = &[
    ("/tables/", "/edit", table_edit),
    ("/handout/", "", handout),
    ("/entity-templates/", "/edit", entity_template_edit),
    ("/display/combat/", "", display_combat),
];

/// The handler for a path like `/tables/12/edit`, and the id (digits only, as FastAPI's int converter reads them here)
pub fn route(path: &str) -> Option<(Handler, i64)> {
    for (pre, post, h) in ROUTES {
        if let Some(rest) = path.strip_prefix(pre).and_then(|r| r.strip_suffix(post)) {
            if !rest.is_empty() && rest.len() < 16 && rest.bytes().all(|b| b.is_ascii_digit()) {
                return Some((*h, rest.parse().ok()?));
            }
        }
    }
    None
}

pub fn serve(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (h, id) = route(uri.path())?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    h(pages, &db, p, id, headers)
}

fn row(db: &Connection, sql: &str, id: i64) -> Option<Map<String, Value>> {
    db.query_row(sql, [id], |r| crate::layout::row_map(r)).ok()
}

/// json.loads(text or "[]") - None for text Python would fail on
fn json_or_empty_list(v: Option<&Value>) -> Option<Value> {
    let t = v.and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    serde_json::from_str(t).ok()
}

/// `GET /tables/<id>/edit` (app/routers/tables.py `table_edit_form`)
fn table_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let t = row(db, "SELECT * FROM random_tables WHERE id = ?1", id)?;
    if !crate::pages::can_edit_table_row(db, &p, &t)? {
        return None;
    }
    let entries = json_or_empty_list(t.get("entries_json"))?;
    p.ctx.insert("tbl".into(), Value::Object(t));
    p.ctx.insert("entries".into(), entries);
    render_with(pages, "tables/form.html", p.ctx, headers)
}

/// `GET /handout/<id>` (app/routers/handouts.py `handout_single`): a bare print page
fn handout(pages: &Pages, db: &Connection, p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let e = row(db, "SELECT * FROM entities WHERE id = ?1", id)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    if e.get("world_id")?.as_i64() != Some(wid) {
        return None;
    }
    let body = match e.get("body") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => nd_render::md::render_md(s),
        _ => return None,
    };
    let mut ctx = Map::new();
    ctx.insert("request".into(), p.ctx.get("request")?.clone());
    if let Some(nd) = p.ctx.get("__nd") {
        ctx.insert("__nd".into(), nd.clone());
    }
    ctx.insert("entities".into(), json!([{"entity": Value::Object(e), "body_html": body}]));
    render_with(pages, "handout.html", ctx, headers)
}

/// `GET /entity-templates/<id>/edit` (app/main.py `entity_template_edit_form`)
fn entity_template_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let t = row(db, "SELECT * FROM entity_templates WHERE id = ?1", id)?;
    let fields = json_or_empty_list(t.get("fields_json"))?;
    p.ctx.insert("kinds".into(), nd_render::app::data().get("kinds")?.clone());
    p.ctx.insert("tpl".into(), Value::Object(t));
    p.ctx.insert("fields".into(), fields);
    render_with(pages, "entity_template_form.html", p.ctx, headers)
}

/// `GET /display/combat/<id>` (app/routers/display.py `display_combat_page`): the fight's turn order on the table's TV, GM only
fn display_combat(pages: &Pages, db: &Connection, p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    if !p.is_gm {
        return None;
    }
    let world = p.world.as_ref()?;
    let cs = row(db, "SELECT world_id, is_template, name FROM combat_sessions WHERE id = ?1", id)?;
    if cs.get("world_id")?.as_i64() != world.get("id")?.as_i64() || cs.get("is_template").and_then(|v| v.as_i64()).unwrap_or(0) != 0 {
        return None;
    }
    let title: String = match cs.get("name") {
        Some(Value::String(s)) => nd_render::text::py_strip(s).chars().take(200).collect(),
        _ => String::new(),
    };
    let mut ctx = Map::new();
    for k in ["request", "__nd", "world"] {
        if let Some(v) = p.ctx.get(k) {
            ctx.insert(k.into(), v.clone());
        }
    }
    ctx.insert("combat_id".into(), json!(id));
    ctx.insert("title".into(), json!(title));
    render_with(pages, "display_combat.html", ctx, headers)
}
