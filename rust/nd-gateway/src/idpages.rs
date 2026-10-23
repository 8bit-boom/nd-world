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
    ("/pages/", "", page_viewer),
    ("/quests/", "", quest_detail),
    ("/entity/", "/edit", entity_edit),
    ("/sessions/", "", session_detail),
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
pub(crate) fn load_custom_kinds(world: &Map<String, Value>) -> Vec<Value> {
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

/// `GET /pages/<id>` (app/routers/pages.py `pages_viewer`): one document, full page; a hidden one only for the GM
fn page_viewer(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let world = p.world.clone()?;
    if !p.is_gm {
        let s = |k: &str| world.get(k).and_then(|v| v.as_str());
        if nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), "pages")? == "none" {
            return None;
        }
    }
    let doc = row(db, "SELECT * FROM page_docs WHERE id = ?1", id)?;
    if doc.get("world_id")?.as_i64() != world.get("id")?.as_i64() {
        return None;
    }
    let visible = !matches!(doc.get("visible_to_players"), Some(Value::Null) | Some(Value::Number(_)) if doc.get("visible_to_players").and_then(|v| v.as_i64()).unwrap_or(0) == 0);
    if !visible && !p.is_gm {
        return None;
    }
    p.ctx.insert("doc".into(), Value::Object(doc));
    render_with(pages, "page_viewer.html", p.ctx, headers)
}

pub(crate) fn rows(db: &Connection, sql: &str, id: i64) -> Option<Vec<Value>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map([id], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r.into_iter().map(Value::Object).collect())
}

/// Python's truthiness of a stored boolean column (NULL and 0 are false)
fn flag(v: Option<&Value>) -> bool {
    v.and_then(|v| v.as_i64()).unwrap_or(0) != 0
}

/// `GET /quests/<id>` (app/routers/quests.py `quest_detail`) - for a quest of the viewer's active world (one of another world, which
/// Python checks against the viewer's world access, is Python's)
fn quest_detail(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    let q = row(db, "SELECT * FROM quests WHERE id = ?1", id)?;
    if q.get("world_id")?.as_i64() != Some(wid) || !crate::pages::section_ok(&p, Some(&world), "quests")? {
        return None;
    }
    if !flag(q.get("visible_to_players")) && !p.is_gm {
        return None;
    }
    let mut linked = vec![];
    if let Some(list) = json_or_empty_list(q.get("linked_entities_json"))?.as_array() {
        for l in list {
            let l = l.as_object()?;
            let eid = l.get("entity_id")?.as_i64()?;
            let role = match l.get("role") {
                None => json!(""),
                Some(v) => v.clone(),
            };
            if let Some(e) = row(db, "SELECT id, name FROM entities WHERE id = ?1", eid) {
                linked.push(json!({"entity": {"id": e.get("id")?, "name": e.get("name")?}, "role": role}));
            }
        }
    } else {
        return None;
    }
    let edit = crate::pages::edit_level(&p, &world, "quests")?;
    let can_edit = edit && (p.is_gm || p.role.as_deref() == Some("assistant") || q.get("created_by_user_id").and_then(|v| v.as_i64()) == Some(p.user_id));
    let d = nd_render::app::data();
    p.ctx.insert("parties".into(), Value::Array(rows(db, "SELECT * FROM parties WHERE world_id = ?1 ORDER BY name, id", wid)?));
    let mut st = db.prepare("SELECT * FROM quests WHERE world_id = ?1 AND id != ?2 ORDER BY title, id").ok()?;
    let others = st.query_map([wid, id], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    p.ctx.insert("quests".into(), Value::Array(others.into_iter().map(Value::Object).collect()));
    p.ctx.insert("entities".into(), Value::Array(rows(db, "SELECT * FROM entities WHERE world_id = ?1 ORDER BY name, id", wid)?));
    p.ctx.insert("quest".into(), Value::Object(q));
    p.ctx.insert("linked_entities".into(), Value::Array(linked));
    p.ctx.insert("statuses".into(), d.get("quest_statuses")?.clone());
    p.ctx.insert("categories".into(), d.get("quest_categories")?.clone());
    p.ctx.insert("can_edit_this".into(), json!(can_edit));
    render_with(pages, "quests/detail.html", p.ctx, headers)
}

/// `_entity_templates_payload`: [{id, name, kind, fields}] of the templates this world may use (built-in first, by name)
pub fn entity_templates_payload(db: &Connection, wid: i64) -> Option<Vec<Value>> {
    let mut out = vec![];
    for t in rows(db, "SELECT * FROM entity_templates WHERE world_id IS NULL OR world_id = ?1 ORDER BY is_builtin DESC, name, id", wid)? {
        let t = t.as_object()?;
        out.push(json!({"id": t.get("id")?, "name": t.get("name")?, "kind": t.get("kind")?, "fields": json_or_empty_list(t.get("fields_json"))?}));
    }
    Some(out)
}

/// `GET /new?kind=&folder=&name=` (app/main.py `new_form`): the empty entity form, for a viewer with an active world
pub fn new_entity_form(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let q = |k: &str| crate::pages::query_param(uri, k);
    let kind = q("kind").unwrap_or_else(|| "character".into());
    let folder = q("folder").unwrap_or_default();
    let name: String = nd_render::text::py_strip(&q("name").unwrap_or_default()).chars().take(256).collect();
    let players = rows(
        &db,
        "SELECT users.* FROM users JOIN world_memberships ON world_memberships.user_id = users.id WHERE world_memberships.world_id = ?1 \
         ORDER BY users.display_name, world_memberships.id",
        wid,
    )?;
    let mut st = db.prepare("SELECT DISTINCT folder FROM entities WHERE world_id = ?1 AND kind = ?2 AND folder IS NOT NULL").ok()?;
    let mut folders = st
        .query_map(rusqlite::params![wid, kind], |r| r.get::<_, String>(0))
        .ok()?
        .collect::<Result<Vec<_>, _>>()
        .ok()?
        .into_iter()
        .filter(|f| !f.is_empty())
        .collect::<Vec<_>>();
    folders.sort();
    folders.dedup();
    let ctx = &mut p.ctx;
    ctx.insert("entity".into(), Value::Null);
    ctx.insert("kind".into(), json!(kind));
    ctx.insert("world_players".into(), Value::Array(players));
    ctx.insert("allowed_player_ids".into(), json!([]));
    ctx.insert("folder_options".into(), json!(folders));
    ctx.insert("prefill_folder".into(), json!(folder));
    ctx.insert("prefill_name".into(), json!(name));
    ctx.insert("entity_templates".into(), Value::Array(entity_templates_payload(&db, wid)?));
    ctx.insert("custom_fields".into(), json!({}));
    ctx.insert("gallery_images".into(), Value::Array(crate::gallery::all_world_image_urls(&db, wid)?));
    render_with(pages, "entities/form.html", p.ctx, headers)
}

/// `GET /entity/<id>/edit` (app/main.py `edit_form`) - for an entity of the viewer's active world (the auth gate has already let
/// only an editor through)
fn entity_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let e = row(db, "SELECT * FROM entities WHERE id = ?1", id)?;
    if e.get("world_id")?.as_i64() != Some(wid) {
        return None;
    }
    let kind = e.get("kind")?.as_str()?.to_string();
    let players = rows(
        db,
        "SELECT users.* FROM users JOIN world_memberships ON world_memberships.user_id = users.id WHERE world_memberships.world_id = ?1 \
         ORDER BY users.display_name, world_memberships.id",
        wid,
    )?;
    let mut st = db.prepare("SELECT DISTINCT user_id FROM entity_player_access WHERE entity_id = ?1 ORDER BY user_id").ok()?;
    let allowed = st.query_map([id], |r| r.get::<_, i64>(0)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut st = db.prepare("SELECT DISTINCT folder FROM entities WHERE world_id = ?1 AND kind = ?2 AND folder IS NOT NULL").ok()?;
    let mut folders: Vec<String> =
        st.query_map(rusqlite::params![wid, kind], |r| r.get::<_, String>(0)).ok()?.collect::<Result<Vec<_>, _>>().ok()?.into_iter().filter(|f| !f.is_empty()).collect();
    folders.sort();
    folders.dedup();
    let raw = e.get("custom_fields_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("{}");
    let custom: Value = serde_json::from_str(raw).ok()?;
    let ctx = &mut p.ctx;
    ctx.insert("entity".into(), Value::Object(e));
    ctx.insert("kind".into(), json!(kind));
    ctx.insert("world_players".into(), Value::Array(players));
    ctx.insert("allowed_player_ids".into(), json!(allowed));
    ctx.insert("folder_options".into(), json!(folders));
    ctx.insert("prefill_folder".into(), json!(""));
    ctx.insert("entity_templates".into(), Value::Array(entity_templates_payload(db, wid)?));
    ctx.insert("custom_fields".into(), custom);
    ctx.insert("gallery_images".into(), Value::Array(crate::gallery::all_world_image_urls(db, wid)?));
    render_with(pages, "entities/form.html", p.ctx, headers)
}

/// `GET /rules` (app/main.py `rules_page`): the world's rules (or the bundled core rules) with ::: blocks, statblocks, heading ids,
/// sections and the world's rules.json overlay
pub fn rules_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use nd_render::rules;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    if let Some(w) = &world {
        if !crate::pages::section_ok(&p, Some(w), "rules")? {
            return None;
        }
    }
    let own = world.as_ref().and_then(|w| w.get("rules_md")).and_then(|v| v.as_str()).filter(|s| !nd_render::text::py_strip(s).is_empty());
    let md = match own {
        Some(s) => s.to_string(),
        None => {
            let path = cfg.templates_dir.parent()?.join("core_rules.md");
            match std::fs::read(&path) {
                Ok(bytes) => String::from_utf8(bytes).ok()?, // invalid UTF-8 ("errors=ignore" in Python): Python's
                Err(_) => String::new(),
            }
        }
    };
    let md = nd_render::text::strip_legacy_anchors(&md);
    let md = nd_render::text::strip_legacy_anchors(&rules::strict_unescape(&md)?);
    let content = if nd_render::text::py_strip(&md).is_empty() {
        "<p>No rules have been added for this world yet.</p>".to_string()
    } else {
        rules::render_rules_content(&md, p.is_gm)
    };
    let mut sections = rules::split_sections_h23(&content);
    let mut tabs = None;
    if let Some(w) = &world {
        let overlay = rules::parse_overlay(w.get("rules_json").and_then(|v| v.as_str())).ok()?;
        let (s, t) = rules::apply_overlay(sections, overlay.as_ref(), p.is_gm)?;
        sections = s;
        tabs = t;
    }
    let toc: Vec<Value> = sections
        .iter()
        .filter(|s| s.get("id").and_then(|v| v.as_str()).map_or(false, |x| !x.is_empty()))
        .map(|s| json!({"level": s["level"], "text": s["title"], "id": s["id"]}))
        .collect();
    let is_custom = own.is_some();
    let c = &mut p.ctx;
    c.insert("toc".into(), Value::Array(toc));
    c.insert("sections".into(), Value::Array(sections));
    c.insert("tabs_mode".into(), json!(tabs.is_some()));
    c.insert("tabs".into(), tabs.map(Value::Array).unwrap_or(Value::Null));
    c.insert("is_custom_rules".into(), json!(is_custom));
    c.insert("can_edit_rules".into(), json!(p.is_gm));
    render_with(pages, "rules.html", p.ctx, headers)
}

/// `GET /sessions/<id>` (app/routers/sessions.py `session_detail`): the session page - its party, linked combats, featured entities and
/// characters, the party's characters, the session's own facts (GM-only ones for a GM only) with their tag cloud, prep, loot, and the
/// gallery picker for a GM
fn session_detail(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let Some(gs) = row(db, "SELECT * FROM game_sessions WHERE id = ?1", id) else { return Some(refusal(404)) };
    let gw = gs.get("world_id")?.as_i64()?;
    // world_row_visible: the session's world must be reachable, and the sessions section open to this role
    if !p.is_gm {
        if db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [gw, p.user_id], |_| Ok(())).is_err() {
            return Some(refusal(404));
        }
        let w = row(db, "SELECT * FROM worlds WHERE id = ?1", gw)?;
        let s = |k: &str| w.get(k).and_then(|v| v.as_str());
        if nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), "sessions")? == "none" {
            return Some(refusal(404));
        }
    }
    let parties = rows(db, "SELECT * FROM parties WHERE world_id = ?1 ORDER BY name", gw)?;
    let combats = rows(db, "SELECT * FROM combat_sessions WHERE game_session_id = ?1", id)?;
    let npcs = json_or_empty_list(gs.get("npcs_json"))?;
    let list = npcs.as_array()?.clone();
    let kind_of = |n: &Value| n.get("kind").and_then(|k| k.as_str()).unwrap_or("entity").to_string();
    let (mut names, mut selected) = (vec![], vec![]);
    for n in &list {
        let eid = n.get("entity_id")?.as_i64()?;
        if kind_of(n) == "player_character" {
            if let Some(pc) = row(db, "SELECT name FROM player_characters WHERE id = ?1", eid) {
                names.push(pc.get("name").cloned().unwrap_or(Value::Null));
                selected.push(json!(format!("pc:{eid}")));
            }
        } else if n.get("kind").is_none() || kind_of(n) == "entity" {
            if let Some(e) = row(db, "SELECT name FROM entities WHERE id = ?1", eid) {
                names.push(e.get("name").cloned().unwrap_or(Value::Null));
                selected.push(json!(eid));
            }
        }
    }
    let party_pcs = match gs.get("party_id").and_then(|v| v.as_i64()) {
        Some(pid) => match row(db, "SELECT member_pc_ids_json FROM parties WHERE id = ?1", pid) {
            Some(party) => {
                let ids = json_or_empty_list(party.get("member_pc_ids_json"))?;
                let ids: Vec<i64> = ids.as_array()?.iter().map(|v| v.as_i64()).collect::<Option<_>>()?;
                if ids.is_empty() { vec![] } else {
                    let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
                    let mut st = db.prepare(&format!("SELECT * FROM player_characters WHERE id IN ({list})")).ok()?;
                    let got: Vec<Map<String, Value>> = st.query_map([], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
                    got.into_iter().map(Value::Object).collect()
                }
            }
            None => vec![],
        },
        None => vec![],
    };
    let vis = if p.is_gm { "" } else { " AND visible_to_players IS NOT 0" };
    let facts = rows(db, &format!("SELECT * FROM facts WHERE game_session_id = ?1{vis} ORDER BY created_at ASC, id ASC"), id)?;
    let mut counts: Vec<(String, i64)> = vec![];
    for f in &facts {
        for t in f.get("tags").and_then(|v| v.as_str()).unwrap_or("").split(',') {
            let t = crate::edits::py_strip_pub(t);
            if t.is_empty() {
                continue;
            }
            match counts.iter_mut().find(|(k, _)| *k == t) {
                Some(c) => c.1 += 1,
                None => counts.push((t, 1)),
            }
        }
    }
    counts.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    let gallery = match (wid, p.is_gm) {
        (Some(w), true) => crate::gallery::all_world_image_urls(db, w)?,
        _ => vec![],
    };
    let next = gs.get("session_num").cloned().unwrap_or(Value::Null);
    let prep = json_or_empty_list(gs.get("prep_json"))?;
    let loot = json_or_empty_list(gs.get("loot_json"))?;
    let mut gsv = gs.clone();
    let live = gs.get("live_transcript").and_then(|v| v.as_str()).is_some_and(|t| !t.is_empty());
    gsv.insert("has_live_transcript".into(), json!(live));
    let ctx = &mut p.ctx;
    ctx.insert("gsession".into(), Value::Object(gsv));
    ctx.insert("gallery_images".into(), Value::Array(gallery));
    ctx.insert("parties".into(), Value::Array(parties));
    ctx.insert("next_num".into(), next);
    ctx.insert("linked_combats".into(), Value::Array(combats));
    ctx.insert("npc_names".into(), Value::Array(names));
    ctx.insert("party_pcs".into(), Value::Array(party_pcs));
    ctx.insert("npc_selected_json".into(), Value::Array(selected));
    ctx.insert("prep".into(), prep);
    ctx.insert("loot".into(), loot);
    ctx.insert("npcs".into(), npcs);
    ctx.insert("facts".into(), Value::Array(facts));
    ctx.insert("fact_top_tags".into(), json!(counts.into_iter().map(|(t, n)| json!([t, n])).collect::<Vec<_>>()));
    render_with(pages, "sessions/detail.html", p.ctx, headers)
}

/// FastAPI's answer to `HTTPException(status)` with no detail: {"detail": "<the status phrase>"}
pub fn refusal(status: u16) -> Response<Body> {
    let code = axum::http::StatusCode::from_u16(status).unwrap_or(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
    let phrase = code.canonical_reason().unwrap_or("Error");
    Response::builder().status(code).header("content-type", "application/json").header("x-nd-served-by", "gateway")
        .body(Body::from(format!("{{\"detail\":\"{phrase}\"}}"))).unwrap()
}
