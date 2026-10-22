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
    ("/worlds/", "/kinds/edit", kinds_edit),
    ("/worlds/", "/rules/edit", rules_edit),
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

/// `deps.load_custom_kinds`: the world's valid custom kinds, malformed entries dropped
fn load_custom_kinds(world: &Map<String, Value>) -> Vec<Value> {
    let raw = world.get("custom_kinds_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let Ok(Value::Array(list)) = serde_json::from_str::<Value>(raw) else { return vec![] };
    let mut out = vec![];
    for e in list {
        let Some(e) = e.as_object() else { continue };
        let (Some(id), Some(label)) = (e.get("id").and_then(|v| v.as_str()), e.get("label").and_then(|v| v.as_str())) else { continue };
        if !id.starts_with("custom_") || nd_render::text::py_strip(label).is_empty() {
            continue;
        }
        let truthy = |v: Option<&Value>| v.filter(|v| !matches!(v, Value::Null | Value::Bool(false)) && v.as_str() != Some("") && v.as_f64() != Some(0.0)
            && v.as_array().map_or(true, |a| !a.is_empty()) && v.as_object().map_or(true, |o| !o.is_empty())).cloned();
        let subtypes: Vec<Value> = match e.get("subtypes") {
            Some(Value::Array(a)) => a.iter().filter(|x| x.is_string()).cloned().collect(),
            _ => vec![],
        };
        out.push(json!({"id": id, "label": label, "icon": truthy(e.get("icon")).unwrap_or(json!("🏷")), "subtypes": subtypes,
                        "created_at": truthy(e.get("created_at")).unwrap_or(json!(""))}));
    }
    out
}

/// `GET /worlds/<id>/kinds/edit` (app/routers/kinds_admin.py `kinds_edit_form`)
fn kinds_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let w = row(db, "SELECT * FROM worlds WHERE id = ?1", id)?;
    p.ctx.insert("initial_kinds".into(), Value::Array(load_custom_kinds(&w)));
    p.ctx.insert("edit_world".into(), Value::Object(w));
    p.ctx.insert("max_custom_kinds".into(), json!(25));
    render_with(pages, "kinds_edit.html", p.ctx, headers)
}

/// `GET /worlds/<id>/rules/edit` (app/main.py `world_rules_edit_form`) - for a world with no rules overlay stored; one with an overlay
/// (whose validation message the page shows) is Python's
fn rules_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let w = row(db, "SELECT * FROM worlds WHERE id = ?1", id)?;
    match w.get("rules_json") {
        None | Some(Value::Null) => {}
        Some(Value::String(s)) if nd_render::text::py_strip(s).is_empty() => {}
        _ => return None,
    }
    p.ctx.insert("edit_world".into(), Value::Object(w));
    p.ctx.insert("rules_json_error".into(), Value::Null);
    render_with(pages, "rules_edit.html", p.ctx, headers)
}
